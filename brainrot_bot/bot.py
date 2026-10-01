"""The watcher: finds new clips, turns each one into a reel, remembers what is done."""

from __future__ import annotations

import copy
import logging
import os
import queue
import random
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

from .ai import AI
from .analysis import Loudness
from .cloud import CloudSync
from .captions import Hook, build_ass, build_srt, group_words, prepare_words, time_groups
from .commentary import LEAD, TAIL, Commentator, Voiceover
from .config import APP_DIR, FONTS_DIR, MODELS_DIR, SERVER, STOP_FILE, WORK_DIR
from .copywriter import merge_hashtags, write_copy
from .credentials import Credentials
from .dedupe import Fingerprints, frame_hashes, quick_hash, sound_bits, text_signature
from .editor import EditPlan, add_voiceover, edited_length, plan_edit
from .faces import find_faces
from .gpu import setup_codec
from .gameplay import Footage, list_media, pick_music, plan_segments, scan_library
from .links import LinkQueue, add_link
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, MediaInfo, StopRequested, Tools, probe, run_ffmpeg
from .moments import auto_count, find_moments, moments_from_picks, split_sentences, transcript_for_ai
from .parts import plan_parts
from .dashboard import Dashboard, dashboard_url
from .phone import Action, Phone
from .render import Layout, RenderJob, build_command, compute_layout
from .report import stats_lines
from .sfx import write_track
from .state import State
from .thumbnail import make_cover
from .transcribe import Transcriber, Word
from .translate import SCRIPT_FONTS, base_language, caption_settings, language_name, translate_reel, translated_words
from .uploads import PostInfo, UploadQueue, pretty_title
from .versions import FOLDERS, WHY, Cut, moment_range, plan_cuts
from .versions import ranges as version_ranges
from .voice import Voice

log = logging.getLogger("brainrot")

HOOK_FILES = ("title.txt", "hook.txt")
HASHTAG_FILE = "hashtags.txt"
MAX_HOOK_CHARS = 160
WAIT_LOG_EVERY = 600  # seconds between "still waiting for gameplay" messages


@dataclass
class Piece:
    """One reel to make from a clip: the whole clip, a part of it, or one of its best moments."""

    start: float
    end: float
    suffix: str = ""  # added to the file name: _part2, _moment1
    label: str = ""  # shown under the hook: PART 2/3
    hook: str = ""  # hook written when the moment was picked
    title_suffix: str = ""  # added to the post title: (Part 2/3)
    stop: float = 0.0  # moments: how far it may be stretched (where the next moment starts)


@dataclass
class Rendered:
    video: Path
    srt: str | None
    segments: list
    suffix: str
    post: PostInfo
    cover: Path | None = None
    language: str = ""  # set for translated reels
    platforms: list[str] = field(default_factory=list)  # the platforms this version is for (versions on)
    group: str = ""  # versions of the same reel share this
    files: dict[str, Path] = field(default_factory=dict)  # platform -> its copy in Reels/<Platform>/
    note: str = ""  # what the voiceover says (shown on the phone)

    @property
    def title(self) -> str:
        return self.post.title

    @property
    def duration(self) -> float:
        return self.post.duration


@dataclass
class _Clip:
    """What we know about the clip being worked on."""

    path: Path
    key: str
    info: MediaInfo
    layout: Layout
    words: list[Word]
    loudness: Loudness | None
    language: str
    job_dir: Path
    gameplay: list[Footage]
    music: list[Footage]
    file_hook: str  # from title.txt etc.
    folder: str  # the clip's folder (the podcast / influencer name)
    signatures: list[tuple[str, list[int]]] = field(default_factory=list)
    voice_told: bool = False  # "no voiceover because..." was logged


class Duplicate(Exception):
    def __init__(self, same_as: str, how: str):
        super().__init__(f"{how} as {same_as}")
        self.same_as = same_as
        self.how = how


def find_hook_text(clip: Path) -> str:
    """Optional title shown at the start: '<clip name>.txt' next to the clip, or title.txt / hook.txt in its folder."""
    for candidate in (clip.with_suffix(".txt"), *(clip.parent / name for name in HOOK_FILES)):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8-sig", errors="replace").strip()
            except OSError:
                continue
            if text:
                return text[:MAX_HOOK_CHARS]
    return ""


def folder_hashtags(clip: Path) -> str:
    """Extra hashtags for every clip in a folder, from hashtags.txt (e.g. the influencer's own tags)."""
    path = clip.parent / HASHTAG_FILE
    try:
        return " ".join(path.read_text(encoding="utf-8-sig", errors="replace").split()) if path.is_file() else ""
    except OSError:
        return ""


def publish(src: Path, dest: Path) -> None:
    """Move a finished file into place without ever leaving a half-written file under the final name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError:  # different drive
        partial = dest.with_name("." + dest.name + ".partial")
        shutil.copyfile(src, partial)
        os.replace(partial, dest)


def copy_into(src: Path, dest: Path) -> None:
    """Like publish(), but the source stays where it is."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name("." + dest.name + ".partial")
    shutil.copyfile(src, partial)
    os.replace(partial, dest)


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def _length_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}min" if minutes >= 60 else f"{minutes}-minute"


class Bot:
    def __init__(self, cfg: SimpleNamespace, tools: Tools, *, persist: bool = True, preview_seconds: float = 0.0):
        self.cfg = cfg
        self.tools = tools
        setup_codec(cfg, tools)
        self.persist = persist
        self.preview_seconds = preview_seconds
        self.state = State(cfg.paths.state_file)
        self.rng = random.Random()
        self.transcriber = (
            Transcriber(cfg.captions.model, cfg.captions.language, cfg.captions.device, MODELS_DIR) if cfg.captions.enabled else None
        )
        self._seen: dict[str, tuple[tuple[int, int], float]] = {}
        self._last_wait_log = 0.0
        self.stop_event = threading.Event()
        self.credentials = Credentials(cfg.paths.credentials)
        self.ai = AI(cfg, self.credentials)
        self.voice = Voice(cfg.commentary.voice, cfg.commentary.speed, tools, MODELS_DIR, self.should_stop,
                           credentials=self.credentials, settings=cfg.commentary)
        self.commentator = Commentator(cfg, tools, self.ai, self.voice, self._hear, self.should_stop)
        # Test renders (--clip) are never posted, and don't download links.
        self.uploads = UploadQueue(cfg, self.state, self.credentials, self._save) if persist else None
        if self.uploads is not None:
            self.uploads.blocked.clear()  # accounts may have been connected again since last time
        self.links = LinkQueue(cfg, self.state, tools, self._save) if persist else None
        self.cloud = CloudSync(cfg) if persist else None
        self.fingerprints = Fingerprints(self.state, cfg.dedupe.similarity) if cfg.dedupe.enabled and persist else None
        self.activity = "starting"
        self._update_checked = 0.0
        self._update_told = ""
        self.inbox: queue.Queue[Action] = queue.Queue()  # what you ask for from the phone or the dashboard
        self.phone = Phone(cfg, self.credentials, tools, cfg.paths.state_file.with_name("phone_state.json"), WORK_DIR / "phone",
                           self.inbox) if persist else None
        self.dashboard = Dashboard(cfg, self.credentials, self.inbox, self._read_state, self._status_text,
                                   self._clip_folders) if persist and cfg.dashboard.enabled else None
        if self.phone is not None and self.phone.enabled:
            self.phone.status_provider = self._status_text
            self.phone.stats_provider = self._stats_text
            self.phone.folders_provider = self._clip_folders
            if self.dashboard is not None:
                self.phone.dashboard_provider = lambda: dashboard_url(cfg, self.credentials)
            if self.uploads is not None:
                self.uploads.approval_needed = lambda: self.phone.approval
                self.uploads.on_posted = self.phone.posted
                self.uploads.on_problem = self.phone.problem

    def should_stop(self) -> bool:
        return self.stop_event.is_set() or STOP_FILE.exists()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.should_stop():
            self._phone_actions()
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    # ------------------------------------------------------------ updates

    def _maybe_update(self) -> None:
        """Once a day: is there a new version? With app.auto_update: auto, install it now (we're idle)."""
        from . import updater

        if self.cfg.app.auto_update == "off" or not self.persist or time.time() - self._update_checked < 3600:
            return
        self._update_checked = time.time()
        release = updater.available_update(self.state.data)
        self._save()
        if release is None:
            return
        first_time = self._update_told != release.version
        self._update_told = release.version
        if not updater.can_install() or self.cfg.app.auto_update == "ask":
            if first_time:
                how = ("open the app and pick Update" if updater.can_install()
                       else "git pull, then docker compose up -d --build" if SERVER else "run: git pull")
                log.info("Brainrot Bot %s is available (you have the older one): %s. %s", release.version, how, release.page)
                if self.phone is not None:
                    self.phone.problem(f"Brainrot Bot {release.version} is available: {how}.", f"update:{release.version}")
            return
        if updater.other_windows_open():
            if first_time:
                log.info("Brainrot Bot %s is ready to install; close the Brainrot Bot window to let it update.", release.version)
            return
        log.info("Updating to Brainrot Bot %s...", release.version)
        try:
            new_app = updater.unpack(updater.download(release))
        except Exception as exc:  # noqa: BLE001 - try again tomorrow
            log.warning("Couldn't download the update: %s", exc)
            return
        updater.start_install(new_app, [sys.executable, *sys.argv[1:]])
        log.info("Restarting to finish the update.")
        self.stop_event.set()

    # ------------------------------------------------------------ the phone (Telegram / Discord)

    def _phone_actions(self) -> None:
        """Do what was asked from the phone or the dashboard (they only queue it)."""
        while True:
            try:
                action = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._do_phone_action(action)
            except Exception:  # noqa: BLE001
                log.exception("Couldn't do what the phone asked (%s)", action.kind)

    def _do_phone_action(self, action: Action) -> None:
        if action.kind in ("approve", "now", "skip") and self.uploads is not None:
            if action.kind == "skip":
                count = self.uploads.skip(action.video)
                log.info("Phone: %s won't be posted (%d post(s) dropped).", Path(action.video).name, count)
            else:
                count = self.uploads.approve(action.video, now=action.kind == "now")
                log.info("Phone: %s approved%s.", Path(action.video).name, ", posting now" if action.kind == "now" else "")
        elif action.kind == "link":
            folder = self.cfg.paths.clips / action.folder if action.folder else self.cfg.paths.clips
            add_link(folder, action.url)
            log.info("Phone: link added to %s", folder.name or folder)
        elif action.kind in ("pause", "resume") and self.uploads is not None:
            self.uploads.pause(action.kind == "pause")
            log.info("Phone: posting %s.", "paused" if action.kind == "pause" else "resumed")

    def _clip_folders(self) -> list[str]:
        root = self.cfg.paths.clips
        try:
            return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_", "~")))
        except OSError:
            return []

    def _read_state(self) -> dict:
        """A consistent copy of the saved state (for the phone thread; the bot keeps working meanwhile)."""
        import json

        try:
            return json.loads(self.cfg.paths.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _status_text(self) -> str:
        from . import __version__

        data = self._read_state()
        uploads = data.get("uploads", {})
        waiting = sum(1 for i in uploads.values() if i.get("status") == "waiting")
        pending = sum(1 for i in uploads.values() if i.get("status") == "pending")
        day_ago = time.time() - 86400
        posted = sum(1 for i in uploads.values() if i.get("status") == "done" and float(i.get("posted", 0)) > day_ago)
        clips = data.get("clips", {})
        lines = [
            f"Brainrot bot {__version__}: {self.activity}",
            f"Clips done: {sum(1 for e in clips.values() if e.get('status') == 'done')}"
            + (f", failed: {sum(1 for e in clips.values() if e.get('status') == 'failed')}" if any(e.get("status") == "failed" for e in clips.values()) else ""),
            f"Posted in the last 24 h: {posted}",
            f"Waiting to be posted: {pending}",
        ]
        if waiting:
            lines.append(f"Waiting for your OK: {waiting}")
        if data.get("posting_paused"):
            lines.append("Posting is paused (/resume to post again).")
        links = sum(1 for i in data.get("links", {}).values() if i.get("status") == "pending")
        if links:
            lines.append(f"Links to download: {links}")
        return "\n".join(lines)

    def _stats_text(self) -> str:
        return "\n".join(stats_lines(self._read_state()))

    # ------------------------------------------------------------ watching

    def scan(self) -> tuple[list[Path], int]:
        """(clips ready to process, number of new files still being copied / settling)."""
        root = self.cfg.paths.clips
        now = time.monotonic()
        ready: list[tuple[float, Path]] = []
        settling = 0
        present = set()
        for path in list_media(root, VIDEO_EXTS):
            key = path.relative_to(root).as_posix()
            present.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            entry = self.state.clips.get(key)
            if entry and entry.get("sig") == [st.st_size, int(st.st_mtime)]:
                if entry.get("status") in ("done", "failed", "duplicate"):
                    continue
                if entry.get("status") == "retry" and time.time() < entry.get("next_try", 0):
                    continue
            # Only touch a file once it has stopped changing for settle_seconds (it may still be copying).
            observed = (st.st_size, st.st_mtime_ns)
            previous = self._seen.get(key)
            if previous is None or previous[0] != observed:
                self._seen[key] = (observed, now)
                settling += 1
                continue
            if st.st_size == 0 or now - previous[1] < self.cfg.watch.settle_seconds:
                settling += 1
                continue
            ready.append((st.st_mtime, path))
        for key in list(self._seen):
            if key not in present:
                del self._seen[key]
        return [path for _, path in sorted(ready)], settling

    def _download_links(self) -> int:
        if self.links is None:
            return 0
        try:
            return self.links.run(self.should_stop)
        except StopRequested:
            raise
        except Exception:  # noqa: BLE001 - a broken links.txt must never stop the bot
            log.exception("Problem while downloading links")
            return 0

    def run_forever(self) -> None:
        log.info("Watching %s for new clips.", self.cfg.paths.clips)
        if self.phone is not None:
            self.phone.start()
        if self.dashboard is not None:
            self.dashboard.start()
        try:
            self._loop()
        finally:
            if self.phone is not None:
                self.phone.stop()
            if self.dashboard is not None:
                self.dashboard.stop()
        log.info("Bot stopped.")

    def _loop(self) -> None:
        while not self.should_stop():
            self.activity = "watching for new clips"
            try:
                self._phone_actions()
                if self.cloud is not None and self.cloud.enabled:
                    self.activity = "syncing the cloud folders"
                    self.cloud.run()
                    self.activity = "watching for new clips"
                self._download_links()
                ready, _ = self.scan()
                for path in ready:
                    if self.should_stop():
                        break
                    self.process(path)
                if self.uploads is not None and not self.should_stop():
                    self.uploads.run_due(self.should_stop)
                    if self.cfg.upload.stats:
                        self.uploads.refresh_stats(self.should_stop)
                if not self.should_stop():
                    self._maybe_update()
            except StopRequested:
                break
            except Exception:  # noqa: BLE001 - e.g. a folder that briefly can't be read; never stop the bot
                log.exception("Unexpected problem, will keep going")
            self._sleep(self.cfg.watch.poll_seconds)

    def run_once(self) -> int:
        """Download waiting links, process everything that is in the clips folder right now, then return."""
        self._download_links()
        done = 0
        attempted: set[Path] = set()
        while not self.should_stop():
            ready, settling = self.scan()
            ready = [path for path in ready if path not in attempted]
            for path in ready:
                attempted.add(path)
                done += 1 if self.process(path) else 0
            if not ready and not settling:
                break
            if not ready:
                time.sleep(1)
        if self.uploads is not None:
            while self.uploads.run_due(self.should_stop):
                pass
        return done

    def _warn_no_gameplay(self) -> None:
        if not self._last_wait_log or time.monotonic() - self._last_wait_log > WAIT_LOG_EVERY:
            log.warning("Waiting: put some gameplay videos in %s", self.cfg.paths.gameplay)
            self._last_wait_log = time.monotonic()

    # ------------------------------------------------------------ one clip

    def _key(self, clip: Path) -> str:
        try:
            return clip.relative_to(self.cfg.paths.clips).as_posix()
        except ValueError:
            return clip.name

    def process(self, clip: Path) -> list[Path]:
        key = self._key(clip)
        try:
            st = clip.stat()
        except OSError as exc:
            log.error("Can't open %s: %s", clip, exc)
            return []
        sig = [st.st_size, int(st.st_mtime)]
        entry = self.state.clips.get(key) or {}
        if entry.get("sig") != sig:
            entry = {"sig": sig, "attempts": 0}
        gameplay = scan_library(self.cfg.paths.gameplay, VIDEO_EXTS, self.tools, self.state.gameplay_cache)
        if not gameplay:
            # Not the clip's fault, so this doesn't count as a failed attempt.
            self._warn_no_gameplay()
            return []

        log.info("New clip: %s", key)
        self.activity = f"making reels from {key}"
        started = time.monotonic()
        try:
            rendered = self.make_reels(clip, gameplay)
        except StopRequested:
            log.info("Stopped while working on %s; it will be done next time.", key)
            raise
        except Duplicate as dup:
            entry.update(status="duplicate", same_as=dup.same_as, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
            entry.pop("error", None)
            log.info("Skipped %s: it's %s, which was already made into reels.", key, dup)
            self.state.clips[key] = entry
            self._save()
            return []
        except Exception as exc:  # noqa: BLE001 - one bad clip must never stop a 24/7 bot
            if self.should_stop():  # e.g. ffmpeg killed by Ctrl+C: not the clip's fault
                raise StopRequested() from exc
            attempts = int(entry.get("attempts", 0)) + 1
            entry.update(attempts=attempts, error=str(exc)[-2000:], updated=time.strftime("%Y-%m-%d %H:%M:%S"))
            if attempts >= self.cfg.watch.max_attempts:
                entry["status"] = "failed"
                if self.phone is not None:
                    self.phone.problem(f"Couldn't make a reel from {key}: {str(exc)[-300:]}", f"clip:{key}")
                log.error(
                    "Giving up on %s after %d attempt(s): %s\n  Fix the problem, then re-save the file or run: python brainrot.py --retry-failed",
                    key, attempts, exc,
                )
            else:
                delay = 60 * 2 ** (attempts - 1)
                entry.update(status="retry", next_try=time.time() + delay)
                log.error("Failed on %s (attempt %d/%d), trying again in %d min: %s", key, attempts, self.cfg.watch.max_attempts, delay // 60, exc)
            self.state.clips[key] = entry
            self._save()
            return []

        outputs = [path for item in rendered for path in (list(item.files.values()) or [item.video])]
        entry.update(
            status="done",
            outputs=[str(p) for p in outputs],
            updated=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if self.uploads is not None:
            folder = key.split("/")[0] if "/" in key else ""
            groups: dict[str, list[Rendered]] = {}
            for item in rendered:
                if item.language and not self.cfg.upload.post_translations:
                    continue
                groups.setdefault(item.group or str(item.video), []).append(item)
            for items in groups.values():
                main = items[0]  # the longest version: what the phone shows
                details = {"folder": folder, "clip": key, "gameplay": sorted({s.key for s in main.segments}), "language": main.language}
                versions = None
                if any(i.platforms for i in items):
                    versions = {p: (str(path), i.post.duration, sorted({s.key for s in i.segments}))
                                for i in items for p, path in i.files.items()}
                platforms = self.uploads.add(main.video, main.post, clip.parent, details, versions=versions)
                if platforms:
                    log.info("Queued %s for posting on %s%s", main.video.name, ", ".join(platforms),
                             " (waiting for your OK on the phone)" if self.uploads.approval_needed() else "")
                if self.phone is not None:
                    self.phone.reel_ready(main.video, main.cover, main.post.title, folder, main.post.duration, platforms,
                                          self.uploads.approval_needed() and bool(platforms), main.note)
        for name in ("error", "next_try", "same_as"):
            entry.pop(name, None)
        self.state.clips[key] = entry
        self._save()
        log.info("Done in %.0fs: %s", time.monotonic() - started, ", ".join(str(p) for p in outputs) or "no reels")
        return outputs

    def _save(self) -> None:
        if self.persist:
            self.state.save()

    def _publish_versions(self, item: Rendered, rel_dir: Path, name: str) -> None:
        first: Path | None = None
        cover = item.cover if item.cover is not None and item.cover.exists() else None
        for platform in item.platforms:
            final = unique_path(self.cfg.paths.output / FOLDERS[platform] / rel_dir, name, ".mp4")
            if first is None:
                publish(item.video, final)
                if cover is not None:
                    publish(cover, final.with_suffix(".jpg"))
                first = final
            else:
                copy_into(first, final)
                if cover is not None:
                    copy_into(first.with_suffix(".jpg"), final.with_suffix(".jpg"))
            if item.srt:
                final.with_suffix(".srt").write_text(item.srt, encoding="utf-8")
            item.files[platform] = final
        item.video = first
        item.cover = first.with_suffix(".jpg") if cover is not None else None

    def make_reels(self, clip: Path, gameplay: list[Footage]) -> list[Rendered]:
        info = probe(self.tools, clip)
        if not info.has_video:
            raise MediaError("this file has no video picture")
        if info.duration < 1:
            raise MediaError("the clip is shorter than 1 second")
        duration = min(info.duration, self.preview_seconds) if self.preview_seconds else info.duration
        key = self._key(clip)
        fp = self.fingerprints

        quick, frames, sound = "", [], ""
        if fp is not None:
            quick = quick_hash(clip)
            same = fp.same_file(key, quick)
            if same:
                raise Duplicate(same, "a copy of the same file")
            if not info.has_audio:
                frames = frame_hashes(self.tools, clip, info.duration)
                same = fp.same_video(key, info.duration, frames)
                if same:
                    raise Duplicate(same, "the same video")

        music = scan_library(self.cfg.paths.music, AUDIO_EXTS, self.tools, self.state.music_cache)

        job_dir = WORK_DIR / uuid.uuid4().hex[:12]
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            words: list[Word] = []
            loudness = None
            language = ""
            if info.has_audio:
                wav = self._extract_voice(clip, duration, job_dir)
                loudness = Loudness.from_wav(wav)
                if fp is not None and not self.preview_seconds:
                    sound = sound_bits(loudness.levels)
                    same = fp.same_sound(key, sound)
                    if same:
                        raise Duplicate(same, "the same video (same sound)")
                if self.transcriber is not None:
                    log.info("Listening to the clip to write captions...")
                    words = self.transcriber.transcribe(wav, self.should_stop)
                    language = getattr(self.transcriber, "last_language", None) or ""
            signature = text_signature(words) if fp is not None else []
            if fp is not None:
                same = fp.same_words(key, signature)
                if same:
                    raise Duplicate(same, "the same words")

            try:
                folder = clip.parent.relative_to(self.cfg.paths.clips).parts[0]
            except (ValueError, IndexError):
                folder = ""
            ctx = _Clip(
                path=clip, key=key, info=info, layout=compute_layout(self.cfg.video, info.width, info.height),
                words=words, loudness=loudness, language=language, job_dir=job_dir, gameplay=gameplay, music=music,
                file_hook=find_hook_text(clip), folder=folder,
            )
            rendered: list[Rendered] = []
            for index, piece in enumerate(self._plan_pieces(ctx, duration), 1):
                rendered += self._make_piece(ctx, piece, index)

            # Everything rendered: now move the results into the output folder.
            try:
                rel_dir = clip.parent.relative_to(self.cfg.paths.clips)
            except ValueError:
                rel_dir = Path()
            out_dir = self.cfg.paths.output / rel_dir
            for item in rendered:
                suffix = item.suffix + ("_preview" if self.preview_seconds else "")
                if item.platforms:  # one copy in each platform's folder: Reels/TikTok/<podcast>/...
                    self._publish_versions(item, rel_dir, clip.stem + suffix)
                    continue
                final = unique_path(out_dir, clip.stem + suffix, ".mp4")
                publish(item.video, final)
                item.video = final
                if item.srt:
                    final.with_suffix(".srt").write_text(item.srt, encoding="utf-8")
                if item.cover is not None and item.cover.exists():
                    publish(item.cover, final.with_suffix(".jpg"))
                    item.cover = final.with_suffix(".jpg")
                if not item.language:
                    for seg in item.segments:
                        self.state.usage[seg.key] = self.state.usage.get(seg.key, 0) + 1
            if fp is not None:
                fp.forget(key)
                fp.remember(key, quick=quick, duration=info.duration, frames=frames, sound=sound, signature=signature)
                for piece_key, piece_signature in ctx.signatures:
                    fp.remember(piece_key, signature=piece_signature)
            return rendered
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    # ------------------------------------------------------------ which reels to make

    def _plan_pieces(self, ctx: _Clip, duration: float) -> list[Piece]:
        m = self.cfg.moments
        if m.enabled and duration >= m.min_source_minutes * 60 and ctx.words:
            count = auto_count(duration, m.count, m.max_count)
            voiceover = self.commentator.expected(ctx.path)  # the voiceover makes each reel this much longer
            min_s, max_s = moment_range(self.cfg, voiceover)
            moments = None
            if self.cfg.ai.moments and self.ai.available:
                sentences = split_sentences(ctx.words)
                source = f' called "{pretty_title(ctx.path.stem)}"' + (f' (from "{ctx.folder}")' if ctx.folder else "")
                picks = self.ai.pick_moments(transcript_for_ai(sentences), _length_text(duration), source, count,
                                             round(min_s), round(max_s))
                if picks:
                    moments = moments_from_picks(picks, sentences, duration, count, min_s, max_s)
            if not moments:
                moments = find_moments(ctx.words, duration, count, min_s, max_s, ctx.loudness)
            self._stretch_moments(ctx, moments, duration, voiceover)
            log.info("Long video: making %d reel(s) from its best moments: %s", len(moments),
                     ", ".join(f"{int(x.start // 60)}:{int(x.start % 60):02d}" for x in moments))
            starts = sorted(x.start for x in moments)
            return [Piece(x.start, x.end, f"_moment{i}", hook=x.hook, stop=next((s for s in starts if s > x.start), duration))
                    for i, x in enumerate(moments, 1)]

        parts = plan_parts(duration, ctx.words, self.cfg.parts.max_seconds)
        n = len(parts)
        return [
            Piece(start, end, f"_part{i}" if n > 1 else "", f"PART {i}/{n}" if n > 1 else "", title_suffix=f" (Part {i}/{n})" if n > 1 else "")
            for i, (start, end) in enumerate(parts, 1)
        ]

    def _stretch_moments(self, ctx: _Clip, moments: list, duration: float, extra: float = 0.0) -> None:
        """Pauses get cut, so a moment can end up shorter than the longest version needs (TikTok: 1 minute).
        Add the sentences that follow until it's long enough, without running into the next moment.
        extra: seconds the voiceover adds to the reel."""
        starts = sorted(m.start for m in moments)
        for m in moments:
            m.end = self._stretch(ctx, m.start, m.end, next((s for s in starts if s > m.start), duration), extra)

    def _stretch(self, ctx: _Clip, start: float, end: float, stop: float, extra: float) -> float:
        """The new end of start-end, so its longest version (with the voiceover) is long enough."""
        targets = version_ranges(self.cfg)
        if not targets or not ctx.words:
            return end
        top = max(lo for lo, _ in targets.values())
        need, limit = top - extra, max(hi for lo, hi in targets.values() if lo == top) - extra
        fps = ctx.layout.fps
        if edited_length(self.cfg, fps, start, end, ctx.words, ctx.loudness) >= need:
            return end
        for s in split_sentences(ctx.words):
            if s.start < end - 0.5:
                continue
            new_end = min(stop, s.end + 0.4)
            if new_end > stop - 1 or edited_length(self.cfg, fps, start, new_end, ctx.words, ctx.loudness) > limit:
                break
            end = new_end
            if edited_length(self.cfg, fps, start, end, ctx.words, ctx.loudness) >= need:
                break
        return end

    def _make_piece(self, ctx: _Clip, piece: Piece, index: int) -> list[Rendered]:
        cfg = self.cfg
        words = [w for w in ctx.words if piece.start <= w.start < piece.end]
        piece_key = f"{ctx.key}#{piece.suffix or 'reel'}"
        if self.fingerprints is not None and piece.suffix.startswith("_moment"):
            signature = text_signature(words)
            same = self.fingerprints.same_words(ctx.key, signature)
            if same:
                log.info("Skipping moment %s of %s: it was already made from %s.", piece.suffix[7:], ctx.key, same.split("#")[0])
                return []
            ctx.signatures.append((piece_key, signature))
        elif self.fingerprints is not None and piece.suffix:
            ctx.signatures.append((piece_key, text_signature(words)))

        file_title = ctx.file_hook.splitlines()[0].strip() if ctx.file_hook else ""
        text = write_copy(self.ai if cfg.ai.copy else None, words, file_title, ctx.folder, piece.end - piece.start)
        hook = ctx.file_hook or ((piece.hook or text.hook) if cfg.hook.auto else "")
        title = file_title or (text.title if text.by_ai else "") or piece.hook or text.hook or pretty_title(ctx.path.stem)
        title += piece.title_suffix
        description = text.caption if text.by_ai else (hook.splitlines()[0] if hook else title)
        # Most specific first: platforms that allow only a few hashtags (Instagram: 5, X: 2) keep these.
        hashtags = merge_hashtags(folder_hashtags(ctx.path), text.hashtags, cfg.upload.hashtags)

        voiceover = self._voiceover(ctx, piece, words, index)
        extra = voiceover.added(ctx.layout.fps) if voiceover else 0.0
        if piece.stop:  # moments were picked with room for a voiceover: stretch if it came out shorter (or not at all)
            piece.end = self._stretch(ctx, piece.start, piece.end, piece.stop, extra)
        cuts = self._plan_cuts(ctx, piece, extra)
        results = []
        spoken = base_language(ctx.language or cfg.captions.language)
        for n, cut in enumerate(cuts):
            tag = f"{index}" if len(cuts) == 1 else f"{index}{chr(97 + n)}"
            part = replace(piece, start=cut.start, end=cut.end)
            item, job, plan, clean = self._render_part(ctx, part, tag, hook, title, cut.platforms, voiceover)
            item.post = PostInfo(title, hashtags, plan.duration, description)
            item.platforms, item.group = list(cut.platforms), piece.suffix or "reel"
            item.note = voiceover.summary() if voiceover else ""
            results.append(item)
            for lang in cfg.captions.translate_to:
                if base_language(lang) == spoken:
                    continue
                translated = self._render_translation(ctx, part, tag, job, plan, clean, lang, hook, item.post)
                if translated is not None:
                    translated.platforms, translated.group = list(cut.platforms), f"{item.group}_{lang}"
                    results.append(translated)
        return results

    def _hear(self, audio: Path) -> list[Word]:
        """Word timings of the voiceover (for its captions)."""
        if self.transcriber is None:
            return []
        wav = audio.with_name(audio.stem + "_16k.wav")  # what the speech model reads best
        run_ffmpeg(self.tools, ["-i", str(audio), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                   log_path=wav.with_suffix(".log"), should_stop=self.should_stop)
        return self.transcriber.transcribe(wav, self.should_stop, quiet=True)

    def _voiceover(self, ctx: _Clip, piece: Piece, words: list[Word], index: int) -> Voiceover | None:
        """The commentary said around this reel (the same for each platform's version), or None."""
        if not self.cfg.commentary.enabled:
            return None
        source = f' called "{pretty_title(ctx.path.stem)}"' + (f' (from "{ctx.folder}")' if ctx.folder else "")
        language = base_language(ctx.language or self.cfg.captions.language)
        try:
            voiceover = self.commentator.make(ctx.path, words, "" if language == "auto" else language, source,
                                              ctx.job_dir, str(index))
        except StopRequested:
            raise
        except Exception as exc:  # noqa: BLE001 - the reel is still made, without the voiceover
            log.warning("No voiceover for this reel: %s", exc)
            return None
        if voiceover is None and not ctx.voice_told:
            ctx.voice_told = True
            if not self.ai.available:
                log.info("No voiceover: it's written by the AI helper (menu > AI), or put your own words in %s",
                         ctx.path.with_name(ctx.path.stem + ".commentary.txt").name)
        return voiceover

    def _plan_cuts(self, ctx: _Clip, piece: Piece, extra: float = 0.0) -> list[Cut]:
        """The versions to render: which part of the piece each platform gets (versions off: all of it).
        extra: seconds the voiceover adds, so the clip's part is that much shorter."""
        allowed = version_ranges(self.cfg)
        if not allowed:
            return [Cut(piece.start, piece.end, piece.end - piece.start)]
        targets = {p: (max(1.0, lo - extra), hi - extra) for p, (lo, hi) in allowed.items() if hi - extra >= 3}
        for platform in allowed.keys() - targets.keys():
            log.info("No %s version of this reel: the voiceover alone is %.0fs.", FOLDERS[platform], extra)
        if not targets:  # e.g. a very long recording of yours: one reel, in Reels/<podcast>/
            return [Cut(piece.start, piece.end, piece.end - piece.start)]
        fps = ctx.layout.fps
        cuts, left_out = plan_cuts(
            piece.start, piece.end, ctx.words, targets,
            lambda a, b: edited_length(self.cfg, fps, a, b, ctx.words, ctx.loudness),
            lambda a, b, lo, hi: self._best_windows(ctx, a, b, lo, hi),
        )
        for platform, length in left_out.items():
            reason = WHY.get(platform, "no part of it fits " + "-".join(f"{x:g}" for x in allowed[platform]) + " seconds")
            log.info("No %s version of this reel: it's %.0fs long%s (%s).", FOLDERS[platform], length + extra,
                     " with the voiceover" if extra else "", reason)
        return cuts

    def _best_windows(self, ctx: _Clip, a: float, b: float, lo: float, hi: float) -> list[tuple[float, float]]:
        """The best parts of a-b to cut a shorter version from (whole sentences), best first."""
        inside = [w for w in ctx.words if a <= w.start and w.end <= b + 0.3]
        if not inside:  # nothing said: just the beginning
            return [(a, a + hi)] if b - a > hi else []
        windows: list[tuple[float, float]] = []
        # Prefer using most of the time allowed; room is left for the pauses that get cut.
        for low in (max(lo * (1.08 if lo > 30 else 1.0), min(hi - 10, 30.0)), lo):
            for m in find_moments(inside, b, 5, min(low, hi - 1), hi, ctx.loudness):
                window = (max(a, m.start), min(b, m.end))
                if window not in windows:
                    windows.append(window)
        return windows

    # ------------------------------------------------------------ rendering

    def _clean_frame(self, job: RenderJob, plan, out: Path) -> Path:
        """The reel's picture at the cover moment, without captions or emojis (so the title fits)."""
        at = plan.cover_at
        fps = job.layout.fps
        # Which moment of the clip, and of the gameplay, is on screen at that time.
        source = job.source_time(at)
        segments, elapsed = [], 0.0
        for seg in job.segments:
            if at < elapsed + seg.duration:
                segments = [type(seg)(seg.path, seg.key, seg.start + (at - elapsed), 1.0)]
                break
            elapsed += seg.duration
        if job.segments and not segments:
            segments = [job.segments[-1]]
        still = copy.deepcopy(job)
        still.intervals, still.inserts = [(source, source + 2 / fps)], []
        still.segments, still.output, still.ass_file = segments, out, None
        still.emojis, still.sfx_track, still.music, still.mute = [], None, None, []
        still.clip_has_audio = False  # a picture needs no sound (and 2 frames of silence upset loudnorm)
        still.zooms = [type(z)(0.0, 1.0, z.cx, z.cy) for z in job.zooms if z.start <= at < z.end]
        still.reframe = [(0.0, cx) for t, cx in job.reframe if t <= at][-1:] or job.reframe[:1]
        cfg = copy.deepcopy(self.cfg)
        cfg.progress_bar.enabled = False
        run_ffmpeg(self.tools, build_command(still, cfg), log_path=out.with_suffix(".log"), cwd=APP_DIR)
        return out

    def _extract_voice(self, clip: Path, duration: float, job_dir: Path) -> Path:
        """16 kHz mono copy of the sound: what speech recognition and the loudness analysis read."""
        wav = job_dir / "voice.wav"
        run_ffmpeg(
            self.tools,
            ["-i", str(clip), "-t", f"{duration:.3f}", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            log_path=job_dir / "ffmpeg_audio.log",
            should_stop=self.should_stop,
        )
        return wav

    def _write_ass(self, path: Path, chunks, layout: Layout, length: float, style, hook: Hook | None, hook_cfg=None) -> str | None:
        if not chunks and hook is None:
            return None
        path.write_text(
            build_ass(chunks, width=layout.width, height=layout.height, duration=length, captions=style,
                      hook_cfg=hook_cfg or self.cfg.hook, hook=hook, voice_color=self.cfg.commentary.caption_color),
            encoding="utf-8",
        )
        return path.relative_to(APP_DIR).as_posix()

    def _hook(self, layout: Layout, text: str, label: str) -> Hook | None:
        lines = "\n".join(x for x in (text, label) if x)
        return Hook(lines, layout.hook_y, self.cfg.hook.duration) if self.cfg.hook.enabled and lines else None

    def _cover(self, clean: Path | None, title: str, out: Path, layout: Layout, style, job: RenderJob, job_dir: Path) -> Path | None:
        if clean is None:
            return None
        try:
            return make_cover(self.tools, clean, 0.0, title, out, width=layout.width, height=layout.height,
                              captions=style, work_dir=job_dir, fonts_dir=job.fonts_dir)
        except MediaError as exc:
            log.warning("Couldn't make the cover picture: %s", exc)
            return None

    def _render_part(self, ctx: _Clip, piece: Piece, index: int | str, hook_text: str, title: str,
                     platforms: list[str] | None = None, voiceover: Voiceover | None = None):
        cfg = self.cfg
        edit = cfg.edit
        layout = ctx.layout
        faces = []
        if edit.face_tracking and (edit.zoom or layout.mode == "fullscreen"):
            faces = find_faces(self.tools, ctx.path, piece.start, piece.end - piece.start)
        plan = plan_edit(cfg, layout, piece.start, piece.end, ctx.words, ctx.loudness, faces)
        if voiceover is not None:
            add_voiceover(cfg, plan, voiceover.lines, layout.fps, LEAD, TAIL)
        length = plan.duration
        segments = (
            plan_segments(ctx.gameplay, length, self.state.usage, self.rng, cfg.gameplay.skip_start, cfg.gameplay.skip_end)
            if layout.game_box else []
        )
        ass_rel = self._write_ass(ctx.job_dir / f"part{index}.ass", plan.chunks, layout, length, plan.style,
                                  self._hook(layout, hook_text, piece.label))

        emoji_size = edit.emoji_size
        caption_y = round(layout.height * plan.style.position)
        job = RenderJob(
            clip=ctx.path,
            clip_start=piece.start,
            duration=length,
            clip_has_audio=ctx.info.has_audio,
            layout=layout,
            segments=segments,
            output=ctx.job_dir / f"part{index}.mp4",
            ass_file=ass_rel,
            fonts_dir=FONTS_DIR.relative_to(APP_DIR).as_posix(),
            mirror=cfg.gameplay.mirror and self.rng.random() < 0.5,
            intervals=plan.intervals,
            clip_size=(ctx.info.width, ctx.info.height),
            zooms=plan.zooms,
            zoom_amount=edit.zoom_amount,
            reframe=plan.reframe,
            emojis=plan.emojis,
            emoji_y=caption_y - round(plan.style.font_size * 0.8) - emoji_size,
            emoji_size=emoji_size,
            mute=plan.mutes,
            duck_music=cfg.audio.music_duck,
            normalize=ctx.loudness is None or not ctx.loudness.levels or bool(plan.inserts)
            or max(ctx.loudness.level(a, b) for a, b in plan.intervals) > 1e-4,
            inserts=plan.inserts,
        )
        if plan.sounds:
            job.sfx_track = write_track(plan.sounds, length, ctx.job_dir / f"sfx{index}.wav", edit.sfx_volume)
        choice = pick_music(ctx.music, length, self.rng)
        if choice:
            track, music_start = choice
            job.music, job.music_start, job.music_loop = track.path, music_start, track.duration < length + 1
        what = f" {piece.suffix.lstrip('_').replace('part', 'part ').replace('moment', 'moment ')}" if piece.suffix else ""
        if platforms:
            what += " for " + ", ".join(FOLDERS[p] for p in platforms)
        pieces = ", ".join(f"{s.key} @{s.start:.0f}s" for s in segments) or "none (full-screen clip)"
        voiced = sum(i.length for i in plan.inserts)
        cut = piece.end - piece.start - (length - voiced)
        log.info(
            "Rendering%s (%.1fs%s%s, %d zooms, %d emojis) with gameplay: %s", what, length,
            f", {cut:.1f}s of pauses cut" if cut > 0.05 else "", f", {voiced:.1f}s of voiceover" if voiced else "",
            len(plan.zooms), len(plan.emojis), pieces,
        )
        run_ffmpeg(
            self.tools,
            build_command(job, cfg),
            log_path=ctx.job_dir / f"ffmpeg_part{index}.log",
            cwd=APP_DIR,
            duration=length,
            label=f"  rendering{what}:",
            should_stop=self.should_stop,
        )
        clean = None
        cover = None
        if edit.thumbnail:
            try:
                clean = self._clean_frame(job, plan, ctx.job_dir / f"cover{index}.mp4")
            except MediaError as exc:
                log.warning("Couldn't make the cover picture: %s", exc)
            cover = self._cover(clean, hook_text.splitlines()[0] if hook_text else title, ctx.job_dir / f"part{index}.jpg",
                                layout, plan.style, job, ctx.job_dir)
        censor = set(edit.censor_words) if edit.censor else None
        said = sorted(plan.words + plan.voice_words, key=lambda w: w.start)
        srt = build_srt(said, censor=censor) if cfg.captions.enabled and cfg.captions.save_srt and said else None
        item = Rendered(job.output, srt, segments, piece.suffix, PostInfo(title, "", length), cover)
        return item, job, plan, clean

    def _render_translation(self, ctx: _Clip, piece: Piece, index: int | str, job: RenderJob, plan: EditPlan,
                            clean: Path | None, lang: str, hook_text: str, post: PostInfo) -> Rendered | None:
        """The same reel with the captions (and hook, title, caption) in another language."""
        cfg = self.cfg
        if not cfg.captions.enabled or not plan.words:
            return None
        if not self.ai.available:
            log.warning("Captions in %s need the AI (connect a key in the menu); skipping.", language_name(lang))
            return None
        voice = {f"voice{n}": text for n, (text, *_) in enumerate(plan.voice_lines)}
        result = translate_reel(self.ai, plan.words, {"hook": hook_text, "title": post.title, "caption": post.description, **voice},
                                lang)
        if result is None:
            return None
        words, extras = result
        style = caption_settings(plan.style, lang)
        prepared = prepare_words(words, style.uppercase, style.remove_punctuation)
        chunks = time_groups(group_words(prepared, style.max_words, style.max_chars), plan.duration)
        voice_words: list[Word] = []
        for n, (_, start, end, pause_end) in enumerate(plan.voice_lines):  # the voiceover's captions, translated
            said = translated_words([(start, end)], [extras.get(f"voice{n}", "")], lang)
            voice_words += said
            for chunk in time_groups(group_words(prepare_words(said, style.uppercase, style.remove_punctuation),
                                                 style.max_words, style.max_chars), pause_end):
                chunk.voice = True
                chunks.append(chunk)
        chunks.sort(key=lambda c: c.start)
        hook_cfg = copy.copy(cfg.hook)
        if base_language(lang) in SCRIPT_FONTS:
            hook_cfg.font = SCRIPT_FONTS[base_language(lang)]
        hook_t = extras.get("hook", "")
        tag = f"{index}_{lang}"
        translated = copy.copy(job)
        translated.output = ctx.job_dir / f"part{tag}.mp4"
        translated.ass_file = self._write_ass(ctx.job_dir / f"part{tag}.ass", chunks, ctx.layout, plan.duration, style,
                                              self._hook(ctx.layout, hook_t, piece.label), hook_cfg)
        log.info("Rendering the %s version...", language_name(lang))
        run_ffmpeg(
            self.tools, build_command(translated, cfg), log_path=ctx.job_dir / f"ffmpeg_part{tag}.log", cwd=APP_DIR,
            duration=plan.duration, label=f"  rendering ({lang}):", should_stop=self.should_stop,
        )
        title = extras.get("title", post.title)
        cover = self._cover(clean, hook_t or title, ctx.job_dir / f"part{tag}.jpg", ctx.layout, style, job, ctx.job_dir)
        srt = build_srt(sorted(words + voice_words, key=lambda w: w.start)) if cfg.captions.save_srt else None
        info = PostInfo(title, post.hashtags, post.duration, extras.get("caption", post.description))
        return Rendered(translated.output, srt, job.segments, f"{piece.suffix}_{lang}", info, cover, language=lang)
