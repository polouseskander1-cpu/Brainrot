"""Stories: ideas in, finished reels out.

An idea (from you on Telegram/Discord or the menu, from Reddit, or from the AI) becomes a script written by
the AI helper. The script is sent to your phone; you pick how it's made:

- AI video: every scene is a shot made by Google's Veo from a keyframe drawn by Nano Banana 2, in one of
  the looks (claymation, anime, 3D cartoon), with the characters kept the same in every shot. Costs money,
  so it waits for your OK (stories.approval) and never goes over stories.daily_budget.
- Over gameplay: the narration over full-screen gameplay, with captions. Free.

Everything a story needs is kept in its own folder (stories/<id> <title>/): the script, the narration, the
pictures and the shots, so nothing is paid for twice if the bot stops halfway.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from .. import aivoice
from ..media import StopRequested, probe
from ..translate import language_name
from ..voice import Voice
from . import assemble, google, reddit, styles
from .narrate import Narration, narrate
from .script import Script, clean, ideas_prompt, pace, story_prompt

log = logging.getLogger("brainrot")

AI = "ai"
GAMEPLAY = "gameplay"
KINDS = (AI, GAMEPLAY)
# Where a story is: written (new -> ready), waiting for its turn (approved), being made, or finished.
WAITING = ("new", "ready", "approved", "making")
STORY_STYLE = ("Like a gifted storyteller telling a gripping short story to a friend: natural and intimate, with "
               "suspense in the voice as it builds and a short pause before the reveal. A real person, not an announcer.")
MAX_PARALLEL = 4  # pictures drawn / shots made at the same time
SHOT_TIMEOUT = 20 * 60  # give up on a shot (and move its picture instead) after this long
MAX_SECONDS = 90.0  # Facebook takes reels of at most 90 seconds from apps
MINUTE = 61.0  # TikTok's Creator Rewards pay for videos over a minute
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slug(text: str, limit: int = 40) -> str:
    return _SLUG_RE.sub("_", text).strip("_")[:limit].strip("_") or "story"


def today() -> str:
    return date.today().isoformat()


def money(value: float) -> str:
    return f"${value:,.2f}"


@dataclass
class Cost:
    pictures: int
    video_seconds: int
    total: float


def estimate(cfg: SimpleNamespace, script: Script, lengths: list[float] | None = None, words_per_second: float = 2.5) -> Cost:
    """What an AI video of this script costs (US dollars): the pictures, and the seconds of video."""
    s = cfg.stories
    pictures = len(script.characters) + len(script.scenes)
    if lengths is None:  # before the narration exists: from the words of each scene
        lengths = [len(x.narration.split()) / words_per_second + 0.4 for x in script.scenes]
    seconds = 0 if s.video_model == google.NO_VIDEO else sum(google.shot_seconds(min(8.0, d), s.resolution) for d in lengths)
    total = pictures * google.image_price(s.image_model) + seconds * google.video_price(s.video_model, s.resolution)
    return Cost(pictures, seconds, round(total, 2))


class BudgetUsedUp(Exception):
    pass


class Studio:
    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.cfg
        self.tools = bot.tools
        self.state = bot.state
        self.folder: Path = bot.cfg.paths.stories
        s = bot.cfg.stories
        narrator = s.voice or bot.cfg.commentary.voice
        settings = SimpleNamespace(style=STORY_STYLE, gemini_model=bot.cfg.commentary.gemini_model,
                                   elevenlabs_model=bot.cfg.commentary.elevenlabs_model)
        self.voice = Voice(narrator, bot.cfg.commentary.speed, bot.tools, should_stop=bot.should_stop,
                           credentials=bot.credentials, settings=settings)
        self._reddit: reddit.Reddit | None = None
        self._paused_until: dict[str, float] = {}  # "google", "reddit", "ideas": after a failure
        self._told: set[str] = set()
        self._lock = threading.Lock()  # pictures are drawn in parallel: their costs are added one at a time
        for entry in self.entries.values():
            if entry.pop("budget_wait", None):
                entry["next_try"] = 0

    # ------------------------------------------------------------------ the queue

    @property
    def entries(self) -> dict:
        return self.state.data.setdefault("stories", {})

    @property
    def enabled(self) -> bool:
        return self.cfg.stories.enabled

    def google_key(self) -> str:
        return aivoice.api_key(aivoice.GEMINI, self.bot.credentials)

    def can_make_ai(self) -> bool:
        return bool(self.google_key()) and self.cfg.stories.daily_budget > 0

    def spent_today(self) -> float:
        return float(self.state.data.setdefault("story_spend", {}).get(today(), 0.0))

    def _spend(self, entry: dict, dollars: float) -> None:
        with self._lock:
            spend = self.state.data.setdefault("story_spend", {})
            spend[today()] = round(float(spend.get(today(), 0.0)) + dollars, 4)
            for day in sorted(spend)[:-60]:  # keep two months
                spend.pop(day, None)
            entry["cost"] = round(float(entry.get("cost", 0.0)) + dollars, 4)
            self.bot._save()

    def made_today(self) -> int:
        return sum(1 for e in self.entries.values() if e.get("status") == "done" and e.get("made_on") == today())

    def add_idea(self, text: str, source: str = "you", *, words: bool = False, make: str = "", style: str = "",
                 origin: dict | None = None) -> str:
        """Queue an idea. words: your own story, read as you wrote it. Returns the story's id."""
        text = (text or "").strip()
        story_id = secrets.token_hex(3)
        while story_id in self.entries:
            story_id = secrets.token_hex(3)
        self.entries[story_id] = {
            "status": "new", "idea": text[:12000], "source": source, "words": words, "make": make,
            "style": style or self.cfg.stories.style, "created": time.time(), "updated": time.time(), "cost": 0.0,
            "attempts": 0, **({"origin": origin} if origin else {}),
        }
        self.bot._save()
        what = {"you": "your story" if words else "your idea", "reddit": "a Reddit post", "ai": "an AI idea"}.get(source, source)
        log.info("Story %s: %s added (%s).", story_id, what, " ".join(text.split())[:80])
        return story_id

    def choose(self, story_id: str, choice: str, style: str = "") -> str:
        """What you picked for a story (from the phone or the menu): ai, gameplay, drop or rewrite. Returns
        what to tell you."""
        entry = self.entries.get(story_id)
        if entry is None:
            return "That story is gone."
        if entry.get("status") in ("making", "done"):
            return "That story is already being made." if entry["status"] == "making" else "That story is already made."
        if style in styles.STYLES:
            entry["style"] = style
        if choice == "drop":
            entry.update(status="dropped", updated=time.time())
            reply = "Dropped."
        elif choice == "rewrite":
            entry.update(status="new", updated=time.time(), rewrite=entry.get("title", ""), asked=False)
            reply = "Writing it again..."
        elif choice in KINDS:
            if choice == AI and not self.google_key():
                return "AI videos need a Google AI key: menu > Stories > Google AI key. Pick \"Over gameplay\" meanwhile."
            if entry.get("status") == "new":  # not written yet: just remember the choice
                entry["make"] = choice
                self.bot._save()
                return "OK, it's made that way once it's written."
            script = self.load_script(entry) if choice == AI else None
            if script is not None and not all(x.visual for x in script.scenes):
                return "This story was written without the AI helper, so it has no scenes to draw. Pick \"Over gameplay\"."
            entry.update(status="approved", make=choice, updated=time.time(), next_try=0, attempts=0)
            entry.pop("budget_wait", None)
            reply = ("Making the AI video (" + styles.get(entry["style"]).name + "). It takes about 15-30 minutes."
                     if choice == AI else "Making it over gameplay. It takes a few minutes.")
        else:
            return "?"
        self.bot._save()
        log.info("Story %s: %s", story_id, reply)
        return reply

    def summary(self) -> str:
        """For /stories and the status: what's waiting, what's being made, what today cost."""
        counts: dict[str, int] = {}
        for e in self.entries.values():
            counts[e.get("status", "")] = counts.get(e.get("status", ""), 0) + 1
        lines = [f"Stories: {counts.get('ready', 0)} waiting for your OK, {counts.get('approved', 0) + counts.get('making', 0)} "
                 f"being made, {counts.get('new', 0)} being written, {self.made_today()} made today"]
        lines.append(f"AI videos today: {money(self.spent_today())} of {money(self.cfg.stories.daily_budget)}")
        ready = [(sid, e) for sid, e in self.entries.items() if e.get("status") == "ready"]
        for sid, e in sorted(ready, key=lambda item: item[1].get("created", 0))[-5:]:
            lines.append(f"- {e.get('title') or e.get('idea', '')[:50]} ({sid})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ one step of work

    def run(self) -> bool:
        """Do the next thing: find ideas, write a script, or make a story. True if something was done."""
        if not self.enabled:
            return False
        try:
            self._inbox()
            self._find_ideas()
            for story_id, entry in self._oldest("new"):
                if time.time() >= float(entry.get("next_try", 0)):
                    return self._write(story_id, entry)
            for story_id, entry in self._oldest("making") + self._oldest("approved"):
                if time.time() < float(entry.get("next_try", 0)):
                    continue
                if entry.get("make") == AI and time.time() < self._paused_until.get("google", 0):
                    continue
                if entry.get("source") != "you" and self.made_today() >= self.cfg.stories.per_day:
                    continue  # stories.per_day holds back Reddit's and the AI's ideas, never yours
                return self._make(story_id, entry)
        except StopRequested:
            raise
        except Exception:  # noqa: BLE001 - stories must never stop the bot
            log.exception("Problem with the stories; will keep going")
        return False

    def _oldest(self, status: str) -> list[tuple[str, dict]]:
        found = [(sid, e) for sid, e in self.entries.items() if e.get("status") == status]
        return sorted(found, key=lambda item: item[1].get("created", 0))

    # ------------------------------------------------------------------ ideas

    def _inbox(self) -> None:
        """Ideas and choices from the menu (stories/inbox), and .txt files you dropped there."""
        from .menu import read_inbox

        for item in read_inbox(self.folder):
            if item.get("story"):
                log.info("Menu: %s", self.choose(str(item["story"]), str(item.get("choice", "")), str(item.get("style", ""))))
            elif str(item.get("idea", "")).strip():
                self.add_idea(str(item["idea"]), "you", words=bool(item.get("words")), make=str(item.get("make", "")))

    def _due(self, what: str, hours: float) -> bool:
        if time.time() < self._paused_until.get(what, 0):
            return False
        last = float(self.state.data.setdefault("story_checks", {}).get(what, 0))
        return time.time() - last >= hours * 3600

    def _checked(self, what: str) -> None:
        self.state.data.setdefault("story_checks", {})[what] = time.time()
        self.bot._save()

    def _find_ideas(self) -> None:
        s = self.cfg.stories
        queued = sum(1 for e in self.entries.values() if e.get("status") in WAITING)
        if queued >= 3 or self.made_today() >= s.per_day:
            return
        if s.reddit and self._due("reddit", s.reddit_every_hours):
            self._reddit_ideas()
        elif s.ai_ideas and queued == 0 and self.bot.ai.available and self._due("ideas", 1):
            self._ai_idea()

    def _reddit_client(self) -> reddit.Reddit | None:
        app = self.bot.credentials.get(reddit.CREDENTIAL_KEY) or {}
        if not app.get("client_id") or not app.get("client_secret"):
            if "reddit-app" not in self._told:
                self._told.add("reddit-app")
                log.warning("stories.reddit is set, but no Reddit app is connected (menu > Stories > Reddit).")
            return None
        if self._reddit is None or self._reddit.client_id != app["client_id"]:
            self._reddit = reddit.Reddit(app["client_id"], app["client_secret"], app.get("username", ""))
        return self._reddit

    def _reddit_ideas(self) -> None:
        client = self._reddit_client()
        if client is None:
            return
        s = self.cfg.stories
        seen = self.state.data.setdefault("reddit_seen", [])
        posts: list[reddit.Post] = []
        try:
            for subreddit in s.reddit:
                posts += client.top(subreddit)
        except reddit.RedditError as exc:
            self._paused_until["reddit"] = time.time() + exc.wait
            log.warning("Reddit: %s", exc)
            if self.bot.phone is not None:
                self.bot.phone.problem(f"Reddit: {exc}", "reddit")
            return
        self._checked("reddit")
        for post in reddit.pick(posts, set(seen), s.reddit_min_upvotes, s.reddit_per_check):
            seen.append(post.id)
            self.add_idea(post.idea, "reddit", origin={"subreddit": post.subreddit, "url": post.url, "score": post.score})
        del seen[:-2000]
        self.bot._save()

    def _ai_idea(self) -> None:
        recent = [e.get("title", "") for e in self.entries.values() if e.get("title")]
        data = self.bot.ai.ask_story_ideas(ideas_prompt(self.cfg.stories.topics, 1, recent))
        self._checked("ideas")
        if data:
            self.add_idea(data[0], "ai")
        else:
            self._paused_until["ideas"] = time.time() + 3600

    # ------------------------------------------------------------------ writing

    def _write(self, story_id: str, entry: dict) -> bool:
        s = self.cfg.stories
        idea = entry["idea"]
        if entry.get("rewrite"):
            idea += f"\n\n(Write a different version than the one called \"{entry['rewrite']}\".)"
        script = None
        if self.bot.ai.available:
            origin = entry.get("origin") or {}
            prompt = story_prompt(idea, source="you" if entry.get("words") else entry.get("source", "you"),
                                  subreddit=origin.get("subreddit", ""), language=language_name(s.language),
                                  seconds=s.seconds, question=s.end_question, words_per_second=self._pace())
            if entry.get("words"):
                prompt += ("\n\nThe idea above is the channel owner's own story: the narration must be exactly its "
                           "words, in order, split into scenes. Don't add, remove or change words (fix only obvious typos).")
            data = self.bot.ai.write_story(prompt)
            script = clean(data) if data else None
            if data and script is None:
                log.warning("Story %s: the AI's script was unusable; trying again later.", story_id)
        elif entry.get("words"):
            script = plain_script(idea)
        if script is None:
            connected = bool(self.bot.ai.api_key()) and self.cfg.ai.enabled
            if not connected and "no-ai" not in self._told:
                self._told.add("no-ai")
                log.warning("Stories are written by the AI helper (menu > AI). Without it, only your own stories "
                            "(/story on the phone) can be made, over gameplay.")
            entry.update(attempts=int(entry.get("attempts", 0)) + 1, updated=time.time(), next_try=time.time() + 900)
            if entry["attempts"] >= 3 or not connected:
                entry.update(status="failed", error="no script (the AI helper isn't connected or didn't answer)")
            self.bot._save()
            return False
        folder = self._folder(story_id, entry, script.title)
        _clear(folder, narration=True)  # a story written again starts over
        for key in ("shots", "budget_ok", "drawn_style"):
            entry.pop(key, None)
        (folder / "script.json").write_text(json.dumps(script.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        words_per_second = self._pace()
        cost = estimate(self.cfg, script, words_per_second=words_per_second)
        entry.update(title=script.title, hook=script.hook, scenes=len(script.scenes), pace=words_per_second,
                     seconds=round(script.seconds(words_per_second)), estimate=cost.total, attempts=0, updated=time.time())
        entry.pop("rewrite", None)
        entry.pop("next_try", None)
        chosen = entry.get("make") or ""  # you already picked how to make it
        make = chosen or (AI if s.make == AI and self.can_make_ai() and all(x.visual for x in script.scenes) else GAMEPLAY)
        ask = not chosen and (s.approval == "all" or (s.approval == "ai_only" and make == AI))
        if not ask:
            entry.update(status="approved", make=make)
            log.info("Story %s written: \"%s\" (%d scenes, about %ds); making it %s.", story_id, script.title,
                     len(script.scenes), entry["seconds"], "as an AI video" if make == AI else "over gameplay")
        else:
            entry.update(status="ready")
            log.info("Story %s written: \"%s\" (%d scenes, about %ds); waiting for your OK (phone or menu > Stories).",
                     story_id, script.title, len(script.scenes), entry["seconds"])
            self._ask(story_id, entry, script)
        self.bot._save()
        return True

    def _pace(self) -> float:
        """Words a second the narrator will read at."""
        return pace(self.voice.engine_for(self.cfg.stories.language), self.cfg.commentary.speed)

    def _ask(self, story_id: str, entry: dict, script: Script) -> None:
        phone = self.bot.phone
        if phone is None or not phone.enabled:
            return
        costs = {key: float(entry.get("estimate", 0.0)) for key in styles.STYLES}  # the same for every look
        origin = entry.get("origin") or {}
        source = {"you": "your story" if entry.get("words") else "your idea", "ai": "an AI idea",
                  "reddit": f"r/{origin.get('subreddit', '?')} ({origin.get('score', 0):,} upvotes)"}.get(entry.get("source"), "")
        text = story_text(script, source, entry, self.cfg, self.spent_today())
        phone.story_ready(story_id, text, costs, entry.get("style", self.cfg.stories.style), self.can_make_ai())
        entry["asked"] = True

    # ------------------------------------------------------------------ making

    def _folder(self, story_id: str, entry: dict, title: str = "") -> Path:
        """stories/<id> <title>/: everything the story needs. A rewritten story moves to its new title."""
        name = entry.get("folder") or ""
        if title and name != f"{story_id} {slug(title)}":
            new = f"{story_id} {slug(title)}"
            if name and (self.folder / name).is_dir() and not (self.folder / new).exists():
                (self.folder / name).rename(self.folder / new)
            name = new
        name = name or f"{story_id} {slug(entry.get('idea', ''))}"
        entry["folder"] = name
        folder = self.folder / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def load_script(self, entry: dict) -> Script | None:
        try:
            return Script.from_dict(json.loads((self.folder / entry["folder"] / "script.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None

    def _make(self, story_id: str, entry: dict) -> bool:
        script = self.load_script(entry)
        if script is None:
            entry.update(status="new", updated=time.time())  # the script is gone: write it again
            self.bot._save()
            return False
        kind = entry.get("make") or GAMEPLAY
        if kind == AI and not all(x.visual for x in script.scenes):
            kind = entry["make"] = GAMEPLAY
        if kind == AI and not self.google_key():
            self._wait(story_id, entry, "AI videos need a Google AI key (menu > Stories > Google AI key)", 3600)
            return False
        if entry.get("status") != "making":
            entry.update(status="making", started=time.time())
            self.bot._save()
        self.bot.activity = f"making the story \"{script.title}\""
        log.info("Story %s: making \"%s\" %s.", story_id, script.title,
                 f"as an AI video ({styles.get(entry.get('style', '')).name})" if kind == AI else "over gameplay")
        folder = self._folder(story_id, entry)
        work = self.bot.new_job_dir()
        try:
            narration = self._narration(script, folder, work)
            segments: list = []
            if kind == AI:
                base = self._ai_video(story_id, entry, script, narration, folder, work)
            else:
                base, segments = self._gameplay_video(narration, work)
            outputs = self.bot.finish_story(story_id, entry, script, base, narration, work, kind, segments)
        except StopRequested:
            raise
        except BudgetUsedUp as exc:
            entry["budget_wait"] = True  # checked again when the bot restarts (e.g. after the budget was raised)
            self._wait(story_id, entry, str(exc), _seconds_to_tomorrow())
            return False
        except google.GoogleError as exc:
            self._paused_until["google"] = time.time() + exc.wait
            self._failed(story_id, entry, str(exc), wait=max(exc.wait, 600))
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("Story %s failed", story_id)
            self._failed(story_id, entry, str(exc))
            return False
        finally:
            self.bot.remove_job_dir(work)
        entry.update(status="done", made_on=today(), updated=time.time(), outputs=[str(p) for p in outputs])
        for key in ("error", "next_try", "attempts"):
            entry.pop(key, None)
        self.bot._save()
        log.info("Story %s done%s: %s", story_id, f" (cost {money(entry.get('cost', 0))})" if entry.get("cost") else "",
                 ", ".join(str(p) for p in outputs))
        return True

    def _wait(self, story_id: str, entry: dict, why: str, seconds: float) -> None:
        entry.update(next_try=time.time() + seconds, error=why, updated=time.time())
        self.bot._save()
        log.warning("Story %s waits: %s.", story_id, why)
        if self.bot.phone is not None:
            self.bot.phone.problem(f"Story \"{entry.get('title', story_id)}\" waits: {why}.", f"story-wait:{why[:40]}")

    def _failed(self, story_id: str, entry: dict, why: str, wait: float = 0.0) -> None:
        attempts = int(entry.get("attempts", 0)) + 1
        entry.update(attempts=attempts, error=why[-1000:], updated=time.time())
        if attempts >= 3:
            entry["status"] = "failed"
            log.error("Story %s failed after %d tries: %s", story_id, attempts, why)
            if self.bot.phone is not None:
                self.bot.phone.problem(f"Couldn't make the story \"{entry.get('title', story_id)}\": {why[-300:]}",
                                       f"story:{story_id}")
        else:
            delay = max(wait, 120 * 2 ** attempts)
            entry["next_try"] = time.time() + delay
            log.error("Story %s failed (try %d/3), trying again in %d min: %s", story_id, attempts, delay // 60, why)
        self.bot._save()

    def _narration(self, script: Script, folder: Path, work: Path) -> Narration:
        """Read once and kept in the story's folder (the scenes are cut to it, so it must stay the same)."""
        saved = folder / "narration.wav"
        timing = folder / "narration.json"
        if saved.is_file() and timing.is_file():
            try:
                data = json.loads(timing.read_text(encoding="utf-8"))
                from ..transcribe import Word

                return Narration(saved, float(data["length"]), [Word(*w) for w in data["words"]], list(data["cuts"]),
                                 data.get("engine", ""))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        s = self.cfg.stories
        self.voice.settings.style = f"{STORY_STYLE} {script.voice}".strip()
        log.info("Reading the story aloud (voice: %s)...", self.voice.describe(s.language))
        narration = narrate(self.voice, self.tools, script, s.language, work, self.bot.hear, MAX_SECONDS,
                            self.cfg.video.fps, self.bot.should_stop, min_seconds=MINUTE if s.seconds >= MINUTE else 0.0)
        _move(narration.audio, saved)
        narration.audio = saved
        timing.write_text(json.dumps({"length": narration.length, "cuts": narration.cuts, "engine": narration.engine,
                                      "words": [[w.text, w.start, w.end] for w in narration.words]}), encoding="utf-8")
        log.info("Narration: %.0fs, %d scenes (%s).", narration.length, len(script.scenes),
                 ", ".join(f"{d:.1f}s" for d in narration.scene_lengths()))
        return narration

    def _gameplay_video(self, narration: Narration, work: Path) -> tuple[Path, list]:
        library = self.bot.gameplay_library()
        if not library:
            raise RuntimeError(f"put some gameplay videos in {self.cfg.paths.gameplay}")
        segments = self.bot.gameplay_segments(library, narration.total)
        v = self.cfg.video
        base = assemble.gameplay(self.tools, segments, narration.audio, narration.total, work / "story_base.mkv",
                                 width=v.width, height=v.height, fps=v.fps,
                                 mirror=self.cfg.gameplay.mirror and self.bot.rng.random() < 0.5,
                                 should_stop=self.bot.should_stop)
        return base, segments

    # ------------------------------------------------------------------ the AI video

    def _check_budget(self, entry: dict, cost: float) -> None:
        budget = self.cfg.stories.daily_budget
        if budget <= 0:
            raise BudgetUsedUp("AI videos are off (stories.daily_budget is 0)")
        if cost > budget:
            raise BudgetUsedUp(f"this AI video costs about {money(cost)}, more than the daily budget of {money(budget)} "
                               "(menu > Stories > Daily budget, or pick a cheaper video model)")
        if self.spent_today() + cost > budget:
            raise BudgetUsedUp(f"today's budget for AI videos is used up ({money(self.spent_today())} of {money(budget)}); "
                               "it's made tomorrow")

    def _ai_video(self, story_id: str, entry: dict, script: Script, narration: Narration, folder: Path, work: Path) -> Path:
        s = self.cfg.stories
        key = self.google_key()
        style = styles.get(entry.get("style", s.style))
        if entry.get("drawn_style", style.key) != style.key:  # another look was picked: draw everything again
            _clear(folder, narration=False)
            entry.pop("shots", None)
        entry["drawn_style"] = style.key
        lengths = narration.scene_lengths()
        if not entry.get("budget_ok"):  # checked once per story: a story that's been started is finished
            remaining = estimate(self.cfg, script, lengths).total - float(entry.get("cost", 0.0))
            self._check_budget(entry, max(0.0, remaining))
            entry["budget_ok"] = True
            self.bot._save()
        sheets = self._characters(entry, script, style, folder, key)
        pictures = self._keyframes(entry, script, style, folder, key, sheets)
        shots: dict[int, tuple[Path, float, bool]] = {}
        if s.video_model != google.NO_VIDEO:
            shots = self._shots(story_id, entry, script, style, folder, key, pictures, lengths)
        scenes = []
        for i, picture in enumerate(pictures):
            video, seconds, sound = shots.get(i, (None, 0.0, False))
            scenes.append(assemble.SceneMedia(picture, video, lengths[i], seconds, sound))
        v = self.cfg.video
        moved = sum(1 for x in scenes if x.video is None)
        log.info("Putting the %d scenes together%s...", len(scenes),
                 f" ({moved} of them as moving pictures)" if moved and s.video_model != google.NO_VIDEO else "")
        return assemble.ai_scenes(self.tools, scenes, narration.audio, work / "story_base.mkv", width=v.width,
                                  height=v.height, fps=v.fps, ambience=s.scene_sound, should_stop=self.bot.should_stop)

    def _parallel(self, jobs: list, run) -> list:
        """Run jobs MAX_PARALLEL at a time; the first error is raised after the others finish."""
        results: list = [None] * len(jobs)
        errors: list[Exception] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            futures = {pool.submit(run, job): n for n, job in enumerate(jobs)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
        if errors:
            raise errors[0]
        return results

    def _picture(self, entry: dict, key: str, prompt: str, out: Path, refs: list[Path]) -> Path:
        existing = next((p for p in (out.with_suffix(".png"), out.with_suffix(".jpg")) if p.is_file()), None)
        if existing is not None:
            return existing
        if self.bot.should_stop():
            raise StopRequested()
        model = self.cfg.stories.image_model
        path = google.make_picture(key, prompt, out, model=model, references=refs)
        self._spend(entry, google.image_price(model))
        return path

    def _characters(self, entry: dict, script: Script, style: styles.Style, folder: Path, key: str) -> dict[str, Path]:
        if not script.characters:
            return {}
        log.info("Drawing the %d character(s): %s...", len(script.characters), ", ".join(c.name for c in script.characters))

        def draw(c) -> Path | None:
            try:
                return self._picture(entry, key, style.character_prompt(c.name, c.look), folder / f"character_{slug(c.name, 20)}", [])
            except google.GoogleError as exc:
                if not exc.blocked:
                    raise
                log.warning("%s: %s; the scenes are drawn from the description only.", c.name, exc)
                return None

        paths = self._parallel(script.characters, draw)
        return {c.name: p for c, p in zip(script.characters, paths) if p is not None}

    def _keyframes(self, entry: dict, script: Script, style: styles.Style, folder: Path, key: str,
                   sheets: dict[str, Path]) -> list[Path]:
        log.info("Drawing the %d scenes (%s)...", len(script.scenes), style.name)

        def draw(n: int) -> Path:
            scene = script.scenes[n]
            cast = [c for c in script.cast(scene.characters) if c.name in sheets][:4]
            lines = [f"Reference picture {i}: {c.name}. {c.look}" for i, c in enumerate(cast, 1)]
            if cast:
                lines.append("Draw each character exactly as in their reference picture: the same face, hair, body, "
                             "clothes and colors, in the pose and place this scene needs.")
            prompt = style.picture_prompt(scene.visual, script.setting, "\n".join(lines), scene.camera)
            out = folder / f"scene_{n + 1:02d}"
            try:
                return self._picture(entry, key, prompt, out, [sheets[c.name] for c in cast])
            except google.GoogleError as exc:
                if not exc.blocked:
                    raise
                log.warning("Scene %d: %s; drawing the place instead.", n + 1, exc)
                place = f"An establishing shot of the place where the story happens, with nobody in it. The mood: {script.mood}."
                return self._picture(entry, key, style.picture_prompt(place, script.setting, "", "wide shot"), out, [])

        return self._parallel(list(range(len(script.scenes))), draw)

    def _shots(self, story_id: str, entry: dict, script: Script, style: styles.Style, folder: Path, key: str,
               pictures: list[Path], lengths: list[float]) -> dict[int, tuple[Path, float, bool]]:
        """Every scene's shot, MAX_PARALLEL being made at a time. A shot Google won't make (its safety filters,
        a timeout) leaves its scene as a moving picture."""
        s = self.cfg.stories
        price = google.video_price(s.video_model, s.resolution)
        ops: dict[str, str] = entry.setdefault("shots", {})  # scene number -> operation (survives a restart)
        done: dict[int, tuple[Path, float, bool]] = {}
        todo: list[int] = []
        for n in range(len(script.scenes)):
            path = folder / f"scene_{n + 1:02d}.mp4"
            if path.is_file():
                done[n] = self._shot_info(path)
            elif ops.get(str(n)) != "skip":
                todo.append(n)
        if not todo:
            return done
        log.info("Making %d video shots with %s (%d at a time; each takes 1-6 minutes)...", len(todo),
                 google.VIDEO_NAMES.get(s.video_model, s.video_model), MAX_PARALLEL)
        running: dict[int, tuple[str, float]] = {}
        while todo or running:
            while todo and len(running) < MAX_PARALLEL:
                n = todo.pop(0)
                name = ops.get(str(n), "")
                if not name:
                    scene = script.scenes[n]
                    try:
                        name = google.start_shot(key, style.video_prompt(scene.visual, scene.camera, scene.sound),
                                                 pictures[n], model=s.video_model,
                                                 seconds=google.shot_seconds(min(8.0, lengths[n]), s.resolution),
                                                 resolution=s.resolution, negative=style.avoid)
                    except google.GoogleError as exc:
                        if not exc.blocked:
                            raise
                        log.warning("Scene %d: %s; it's shown as a moving picture.", n + 1, exc)
                        ops[str(n)] = "skip"
                        self.bot._save()
                        continue
                    ops[str(n)] = name
                    self.bot._save()
                running[n] = (name, time.monotonic())
            self.bot.pause(10)
            for n, (name, started) in list(running.items()):
                shot = google.check_shot(key, name)
                if not shot.done:
                    if time.monotonic() - started > SHOT_TIMEOUT:
                        log.warning("Scene %d: Google took too long; it's shown as a moving picture.", n + 1)
                        ops[str(n)] = "skip"
                        running.pop(n)
                    continue
                running.pop(n)
                if shot.uri:
                    path = google.download_shot(key, shot.uri, folder / f"scene_{n + 1:02d}.mp4")
                    seconds = google.shot_seconds(min(8.0, lengths[n]), s.resolution)
                    self._spend(entry, seconds * price)
                    done[n] = self._shot_info(path)
                    log.info("Scene %d/%d is ready.", n + 1, len(script.scenes))
                else:
                    log.warning("Scene %d: %s; it's shown as a moving picture.", n + 1, shot.error)
                    ops[str(n)] = "skip"
                self.bot._save()
        return done

    def _shot_info(self, path: Path) -> tuple[Path, float, bool]:
        info = probe(self.tools, path)
        return path, info.duration, info.has_audio


def _move(src: Path, dest: Path) -> None:
    """Move a file, also to another drive (the work folder and the stories folder can be on different ones)."""
    try:
        os.replace(src, dest)
    except OSError:
        partial = dest.with_name(dest.name + ".partial")
        shutil.copyfile(src, partial)
        os.replace(partial, dest)
        src.unlink(missing_ok=True)


def _clear(folder: Path, narration: bool) -> None:
    """Remove a story's pictures and shots (and its narration): they belong to another script or look."""
    patterns = ["scene_*", "character_*"] + (["narration*"] if narration else [])
    for pattern in patterns:
        for path in folder.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def plain_script(text: str) -> Script:
    """Your own story without the AI: read as written, a scene per sentence or two (for gameplay only)."""
    from .script import Scene

    sentences = [x for x in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if x]
    scenes: list[Scene] = []
    for sentence in sentences:
        if scenes and len(scenes[-1].narration.split()) + len(sentence.split()) <= 20:
            scenes[-1].narration += " " + sentence
        else:
            scenes.append(Scene(sentence, ""))
    first = sentences[0] if sentences else text
    hook = " ".join(first.split()[:7]).rstrip(",;:")
    return Script(title=first[:70], hook=hook, caption=first[:200], hashtags=["story", "storytime"], voice="", mood="",
                  setting="", characters=[], scenes=scenes or [Scene(text, "")])


def story_text(script: Script, source: str, entry: dict, cfg: SimpleNamespace, spent: float) -> str:
    """The script as it's shown on the phone."""
    s = cfg.stories
    lines = [f"📝 {script.title}", f"From {source} · about {entry.get('seconds') or round(script.seconds())}s · {len(script.scenes)} scenes"
             + (f" · {script.mood}" if script.mood else ""), f"Hook: {script.hook}", ""]
    body = "\n".join(x.narration for x in script.scenes)
    lines.append(body if len(body) < 2600 else body[:2600] + "...")
    lines.append("")
    cost = estimate(cfg, script, words_per_second=float(entry.get("pace", 2.5)))
    model = "pictures only" if s.video_model == google.NO_VIDEO else google.VIDEO_NAMES.get(s.video_model, s.video_model)
    lines.append(f"🎬 AI video: about {money(cost.total)} ({model}; {money(spent)} of {money(s.daily_budget)} used today)")
    lines.append("🎮 Over gameplay: free")
    return "\n".join(lines)


def _seconds_to_tomorrow() -> float:
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()) + timedelta(minutes=5)
    return max(600.0, (tomorrow - now).total_seconds())
