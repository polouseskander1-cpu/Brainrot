"""Menu > Stories: add ideas, OK waiting stories, and the story settings.

The menu and the background bot are separate programs, so ideas and choices made here are handed over as
small files in stories/inbox, which the bot picks up within a minute (a .txt file you drop there yourself
becomes an idea too).
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

from .. import ui
from ..config import update_config_file
from ..credentials import Credentials
from . import google, reddit, styles

INBOX = "inbox"
VIDEO_CHOICES = [
    ("veo-3.1-fast-generate-preview", "Veo 3.1 Fast: great quality, $0.10 a second (about $8-9 a story) - suggested"),
    ("veo-3.1-lite-generate-preview", "Veo 3.1 Lite: good quality, $0.05 a second (about $4-5 a story)"),
    ("veo-3.1-generate-preview", "Veo 3.1: the best, $0.40 a second (about $30 a story)"),
    (google.NO_VIDEO, "Pictures only, moved by a slow camera: about $1 a story"),
]


def drop_in_inbox(folder: Path, data: dict) -> Path:
    """Hand an idea or a choice to the bot."""
    inbox = folder / INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.json"
    partial = path.with_name("." + path.name + ".partial")
    partial.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)
    return path


def read_inbox(folder: Path) -> list[dict]:
    """Ideas and choices waiting in stories/inbox (each file is removed once read)."""
    inbox = folder / INBOX
    found = []
    try:
        files = sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                found.append(data)
        elif path.suffix.lower() == ".txt" and text.strip():
            found.append({"idea": text.strip()})
        try:
            path.unlink()
        except OSError:
            pass
    return found


def _state(cfg: SimpleNamespace) -> dict:
    try:
        return json.loads(cfg.paths.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _video_label(model: str) -> str:
    return next((text.split(":")[0] for key, text in VIDEO_CHOICES if key == model), model)


def stories_menu(cfg: SimpleNamespace, config_path: Path, connect_google) -> bool:
    """One visit to the Stories screen. Returns True when a setting changed (the bot then restarts)."""
    s = cfg.stories
    creds = Credentials(cfg.paths.credentials)
    from .. import aivoice

    google_on = bool(aivoice.api_key(aivoice.GEMINI, creds))
    reddit_on = bool((creds.get(reddit.CREDENTIAL_KEY) or {}).get("client_id"))
    data = _state(cfg)
    waiting = [(sid, e) for sid, e in data.get("stories", {}).items() if e.get("status") == "ready"]
    spent = float(data.get("story_spend", {}).get(time.strftime("%Y-%m-%d"), 0.0))
    ui.banner("stories")
    ui.say("Stories: an idea becomes a 1-minute narrated reel, as a full AI video or over gameplay. Send ideas from")
    ui.say("the phone (/idea on Telegram) or add them here. The AI helper (Claude) writes each story.")
    ui.say(ui.dim(f"AI videos today: ${spent:.2f} of ${s.daily_budget:.2f}."))
    options = [
        "Add a story idea",
        f"Stories waiting for your OK: {len(waiting)}",
        f"What an idea becomes: {'an AI video' if s.make == 'ai' else 'a story over gameplay (free)'}",
        f"AI video look: {styles.get(s.style).name}",
        f"AI video quality: {_video_label(s.video_model)}",
        f"Daily budget for AI videos: ${s.daily_budget:.2f}",
        "Ask me first: " + {"ai_only": "AI videos (they cost money)", "all": "every story", "none": "never"}[s.approval],
        "Google AI key (pictures, video, Gemini voice): " + ("connected" if google_on else "connect"),
        "Reddit: " + (", ".join("r/" + x for x in s.reddit) if s.reddit else "off") + ("" if reddit_on or not s.reddit else " (app not connected)"),
        f"Ideas from the AI when there are none: {'on' if s.ai_ideas else 'off'}",
        "Open the stories folder",
        "Turn stories off" if s.enabled else "Turn stories on",
        "Back",
    ]
    picked = ui.choose(options, default=len(options))
    folder = cfg.paths.stories
    if picked == 1:
        _add_idea(folder)
    elif picked == 2:
        _waiting(cfg, folder, waiting, google_on)
    elif picked == 3:
        make = "ai" if ui.choose(["An AI video (asks you first, costs money)", "A story over gameplay (free)"],
                                 default=1 if s.make == "ai" else 2) == 1 else "gameplay"
        update_config_file(config_path, {"stories": {"make": make}})
        return True
    elif picked == 4:
        keys = list(styles.STYLES)
        choice = ui.choose([f"{styles.STYLES[k].name}: {styles.STYLES[k].about}" for k in keys], default=keys.index(s.style) + 1)
        update_config_file(config_path, {"stories": {"style": keys[choice - 1]}})
        return True
    elif picked == 5:
        ui.say("The AI video model makes every scene (a story has about 11 scenes of 4-8 seconds). Pictures are drawn by")
        ui.say("Nano Banana 2 either way (about $0.07 each). Google's prices, October 2026.")
        keys = [k for k, _ in VIDEO_CHOICES]
        current = keys.index(s.video_model) + 1 if s.video_model in keys else 1
        choice = ui.choose([text for _, text in VIDEO_CHOICES], default=current)
        update_config_file(config_path, {"stories": {"video_model": keys[choice - 1]}})
        return True
    elif picked == 6:
        ui.say("The most AI videos may cost in a day, in US dollars (0 = no AI videos). A story that doesn't fit today")
        ui.say("waits for tomorrow.")
        answer = ui.ask("Daily budget", f"{s.daily_budget:g}").lstrip("$")
        try:
            update_config_file(config_path, {"stories": {"daily_budget": max(0.0, float(answer))}})
            return True
        except ValueError:
            ui.say(ui.red("  That isn't a number."))
            ui.pause()
    elif picked == 7:
        keys = ["ai_only", "all", "none"]
        choice = ui.choose(["AI videos only (they cost money); stories over gameplay are made right away",
                            "Every story (you read each script first)", "Never (make everything right away)"],
                           default=keys.index(s.approval) + 1)
        update_config_file(config_path, {"stories": {"approval": keys[choice - 1]}})
        return True
    elif picked == 8:
        connected = connect_google(cfg, creds)
        if connected and not s.voice and not cfg.commentary.voice.startswith("gemini") and ui.ask_yes_no(
                "Let a Google Gemini voice (Charon: calm, clear, like a real narrator) read the stories? Free tier, then ~2 cents a story",
                default=True):
            update_config_file(config_path, {"stories": {"voice": "gemini:Charon"}})
        return connected
    elif picked == 9:
        return _reddit(cfg, config_path, creds)
    elif picked == 10:
        if not s.ai_ideas:
            ui.say("When no ideas are waiting, the AI thinks of one in your topics (up to stories.per_day a day).")
            topics = ui.ask("Topics", s.topics)
            update_config_file(config_path, {"stories": {"ai_ideas": True, "topics": topics}})
        else:
            update_config_file(config_path, {"stories": {"ai_ideas": False}})
        return True
    elif picked == 11:
        ui.open_folder(folder)
    elif picked == 12:
        update_config_file(config_path, {"stories": {"enabled": not s.enabled}})
        return True
    return False


def _add_idea(folder: Path) -> None:
    ui.say("Type the idea in a sentence or two (the AI writes the story), or start with 'story:' to paste your own")
    ui.say("story, read as you wrote it. Enter on an empty line goes back.")
    text = ui.ask("Idea").strip()
    if not text:
        return
    words = text.lower().startswith("story:")
    if words:
        text = text[6:].strip()
    drop_in_inbox(folder, {"idea": text, "words": words})
    ui.say(ui.green("  Added. The bot writes it within a few minutes (while it's running); the script then waits"))
    ui.say(ui.green("  here (and on your phone) for your OK."))
    ui.pause()


def _waiting(cfg: SimpleNamespace, folder: Path, waiting: list[tuple[str, dict]], google_on: bool) -> None:
    if not waiting:
        ui.say("  No stories are waiting.")
        ui.pause()
        return
    picked = ui.choose([f"{e.get('title') or e.get('idea', '')[:60]} (about {e.get('seconds', '?')}s)" for _, e in waiting]
                       + ["Back"], default=len(waiting) + 1)
    if picked > len(waiting):
        return
    story_id, entry = waiting[picked - 1]
    try:
        script = json.loads((folder / entry.get("folder", "") / "script.json").read_text(encoding="utf-8"))
        ui.say("")
        for scene in script.get("scenes", []):
            ui.say("  " + scene.get("narration", ""))
        ui.say("")
    except (OSError, ValueError):
        pass
    cost = float(entry.get("estimate", 0.0))
    options = ([f"Make it as an AI video ({styles.get(entry.get('style', cfg.stories.style)).name}, about ${cost:.2f})",
                "Make it as an AI video in another look"] if google_on else [])
    options += ["Make it over gameplay (free)", "Write it again", "Drop it", "Back"]
    choice = options[ui.choose(options, default=len(options)) - 1]
    style = ""
    if choice.startswith("Make it as an AI video in another"):
        keys = list(styles.STYLES)
        style = keys[ui.choose([styles.STYLES[k].name for k in keys]) - 1]
    action = ("ai" if choice.startswith("Make it as an AI") else "gameplay" if choice.startswith("Make it over")
              else "rewrite" if choice.startswith("Write") else "drop" if choice.startswith("Drop") else "")
    if action:
        drop_in_inbox(folder, {"story": story_id, "choice": action, "style": style})
        ui.say(ui.green("  Done. The bot picks it up within a minute (while it's running)."))
        ui.pause()


def _reddit(cfg: SimpleNamespace, config_path: Path, creds: Credentials) -> bool:
    s = cfg.stories
    ui.say("Reddit: the bot reads the top posts of the subreddits you pick, and the AI writes a new story from the")
    ui.say("best one (never the post itself: posts belong to their authors).")
    ui.say(ui.yellow("Reddit approves every app before it may read anything, and using Reddit posts for videos you earn"))
    ui.say(ui.yellow(f"money from needs Reddit's written approval: {reddit.POLICY_PAGE}"))
    app = creds.get(reddit.CREDENTIAL_KEY) or {}
    if not app.get("client_id") or ui.ask_yes_no("Connect a different Reddit app?", default=False):
        ui.say(f"  1. Open {reddit.APPS_PAGE} > create another app > type: script (redirect uri: http://localhost:8080)")
        ui.say("  2. Ask Reddit for access to the API with that app (Reddit's form, linked on that page) and wait for the OK")
        ui.say("  3. Copy the app ID (under the app's name) and the secret")
        client_id = ui.ask("App ID (or 'skip')").strip()
        if client_id and client_id.lower() != "skip":
            secret = ui.ask("Secret").strip()
            username = ui.ask("Your Reddit username (Reddit asks apps to say who runs them)").strip()
            problem = reddit.check(client_id, secret, username)
            if problem:
                ui.say(ui.red(f"  That didn't work: {problem}"))
                ui.pause()
            else:
                creds.set(reddit.CREDENTIAL_KEY, {"client_id": client_id, "client_secret": secret, "username": username,
                                                  "account": f"u/{username}" if username else "Reddit app"})
                ui.say(ui.green("  Reddit connected."))
    ui.say("Subreddits to read, separated by spaces (empty = off). Suggested: Glitch_in_the_Matrix LetsNotMeet")
    ui.say("MaliciousCompliance tifu GetMotivated Stoicism")
    answer = ui.ask("Subreddits", " ".join(s.reddit))
    names = [reddit.clean_subreddit(x) for x in answer.replace(",", " ").split()]
    update_config_file(config_path, {"stories": {"reddit": [n for n in names if n and n.lower() not in reddit.NEVER]}})
    return True
