"""The watcher: finds new clips, turns each one into a reel, remembers what is done."""

from __future__ import annotations

import logging
import os
import random
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .analysis import Loudness
from .captions import Hook, build_ass, build_srt
from .config import APP_DIR, FONTS_DIR, MODELS_DIR, STOP_FILE, WORK_DIR
from .credentials import Credentials
from .gameplay import Footage, list_media, pick_music, plan_segments, scan_library
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, MediaInfo, StopRequested, Tools, probe, run_ffmpeg
from .editor import plan_edit
from .faces import find_faces
from .parts import plan_parts
from .render import Layout, RenderJob, build_command, compute_layout
from .sfx import write_track
from .state import State
from .thumbnail import make_cover
from .transcribe import Transcriber, Word
from .uploads import PostInfo, UploadQueue, pretty_title

log = logging.getLogger("brainrot")

HOOK_FILES = ("title.txt", "hook.txt")
HASHTAG_FILE = "hashtags.txt"
MAX_HOOK_CHARS = 160
WAIT_LOG_EVERY = 600  # seconds between "still waiting for gameplay" messages


@dataclass
class Rendered:
    video: Path
    srt: str | None
    segments: list
    part_suffix: str
    title: str
    duration: float
    cover: Path | None = None


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


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


class Bot:
    def __init__(self, cfg: SimpleNamespace, tools: Tools, *, persist: bool = True, preview_seconds: float = 0.0):
        self.cfg = cfg
        self.tools = tools
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
        # Test renders (--clip) are never posted.
        self.uploads = UploadQueue(cfg, self.state, self.credentials, self._save) if persist else None
        if self.uploads is not None:
            self.uploads.blocked.clear()  # accounts may have been connected again since last time

    def should_stop(self) -> bool:
        return self.stop_event.is_set() or STOP_FILE.exists()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.should_stop():
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

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
                if entry.get("status") in ("done", "failed"):
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

    def run_forever(self) -> None:
        log.info("Watching %s for new clips.", self.cfg.paths.clips)
        while not self.should_stop():
            try:
                ready, _ = self.scan()
                for path in ready:
                    if self.should_stop():
                        break
                    self.process(path)
                if self.uploads is not None and not self.should_stop():
                    self.uploads.run_due(self.should_stop)
            except StopRequested:
                break
            except Exception:  # noqa: BLE001 - e.g. a folder that briefly can't be read; never stop the bot
                log.exception("Unexpected problem, will keep going")
            self._sleep(self.cfg.watch.poll_seconds)
        log.info("Bot stopped.")

    def run_once(self) -> int:
        """Process everything that is in the clips folder right now, then return."""
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

    def process(self, clip: Path) -> list[Path]:
        root = self.cfg.paths.clips
        try:
            key = clip.relative_to(root).as_posix()
        except ValueError:
            key = clip.name
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
        started = time.monotonic()
        try:
            rendered = self.make_reels(clip, gameplay)
        except StopRequested:
            log.info("Stopped while working on %s; it will be done next time.", key)
            raise
        except Exception as exc:  # noqa: BLE001 - one bad clip must never stop a 24/7 bot
            if self.should_stop():  # e.g. ffmpeg killed by Ctrl+C: not the clip's fault
                raise StopRequested() from exc
            attempts = int(entry.get("attempts", 0)) + 1
            entry.update(attempts=attempts, error=str(exc)[-2000:], updated=time.strftime("%Y-%m-%d %H:%M:%S"))
            if attempts >= self.cfg.watch.max_attempts:
                entry["status"] = "failed"
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

        outputs = [item.video for item in rendered]
        entry.update(
            status="done",
            outputs=[str(p) for p in outputs],
            updated=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if self.uploads is not None:
            hashtags = " ".join(x for x in (self.cfg.upload.hashtags, folder_hashtags(clip)) if x)
            for item in rendered:
                platforms = self.uploads.add(item.video, PostInfo(item.title, hashtags, item.duration))
                if platforms:
                    log.info("Queued %s for posting on %s", item.video.name, ", ".join(platforms))
        entry.pop("error", None)
        entry.pop("next_try", None)
        self.state.clips[key] = entry
        self._save()
        log.info("Done in %.0fs: %s", time.monotonic() - started, ", ".join(str(p) for p in outputs))
        return outputs

    def _save(self) -> None:
        if self.persist:
            self.state.save()

    def make_reels(self, clip: Path, gameplay: list[Footage]) -> list[Rendered]:
        info = probe(self.tools, clip)
        if not info.has_video:
            raise MediaError("this file has no video picture")
        if info.duration < 1:
            raise MediaError("the clip is shorter than 1 second")
        duration = min(info.duration, self.preview_seconds) if self.preview_seconds else info.duration

        music = scan_library(self.cfg.paths.music, AUDIO_EXTS, self.tools, self.state.music_cache)

        job_dir = WORK_DIR / uuid.uuid4().hex[:12]
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            words: list[Word] = []
            loudness = None
            if info.has_audio:
                wav = self._extract_voice(clip, duration, job_dir)
                loudness = Loudness.from_wav(wav)
                if self.transcriber is not None:
                    log.info("Listening to the clip to write captions...")
                    words = self.transcriber.transcribe(wav, self.should_stop)
            parts = plan_parts(duration, words, self.cfg.parts.max_seconds)
            layout = compute_layout(self.cfg.video, info.width, info.height)
            hook_text = find_hook_text(clip)
            title = hook_text.splitlines()[0].strip() if hook_text else pretty_title(clip.stem)

            rendered = []
            for index, (start, end) in enumerate(parts, 1):
                label = f"PART {index}/{len(parts)}" if len(parts) > 1 else ""
                reel_title = f"{title} (Part {index}/{len(parts)})" if len(parts) > 1 else title
                rendered.append(self._render_part(
                    clip, info, layout, start, end, words, loudness, hook_text, label, index, len(parts), job_dir,
                    gameplay, music, reel_title,
                ))

            # Everything rendered: now move the results into the output folder.
            try:
                rel_dir = clip.parent.relative_to(self.cfg.paths.clips)
            except ValueError:
                rel_dir = Path()
            out_dir = self.cfg.paths.output / rel_dir
            for item in rendered:
                suffix = item.part_suffix + ("_preview" if self.preview_seconds else "")
                final = unique_path(out_dir, clip.stem + suffix, ".mp4")
                publish(item.video, final)
                item.video = final
                if item.srt:
                    final.with_suffix(".srt").write_text(item.srt, encoding="utf-8")
                if item.cover is not None and item.cover.exists():
                    publish(item.cover, final.with_suffix(".jpg"))
                    item.cover = final.with_suffix(".jpg")
                for seg in item.segments:
                    self.state.usage[seg.key] = self.state.usage.get(seg.key, 0) + 1
            return rendered
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _clean_frame(self, job: RenderJob, plan, out: Path) -> Path:
        """The reel's picture at the cover moment, without captions or emojis (so the title fits)."""
        from copy import deepcopy

        at = plan.cover_at
        fps = job.layout.fps
        # Which moment of the clip, and of the gameplay, is on screen at that time.
        offset, source = 0.0, job.kept()[0][0]
        for a, b in job.kept():
            if at < offset + (b - a):
                source = a + (at - offset)
                break
            offset += b - a
        segments, elapsed = [], 0.0
        for seg in job.segments:
            if at < elapsed + seg.duration:
                segments = [type(seg)(seg.path, seg.key, seg.start + (at - elapsed), 1.0)]
                break
            elapsed += seg.duration
        if job.segments and not segments:
            segments = [job.segments[-1]]
        still = deepcopy(job)
        still.intervals = [(source, source + 2 / fps)]
        still.segments, still.output, still.ass_file = segments, out, None
        still.emojis, still.sfx_track, still.music, still.mute = [], None, None, []
        still.zooms = [type(z)(0.0, 1.0, z.cx, z.cy) for z in job.zooms if z.start <= at < z.end]
        still.reframe = [(0.0, cx) for t, cx in job.reframe if t <= at][-1:] or job.reframe[:1]
        cfg = deepcopy(self.cfg)
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

    def _render_part(
        self,
        clip: Path,
        info: MediaInfo,
        layout: Layout,
        start: float,
        end: float,
        words: list[Word],
        loudness: Loudness | None,
        hook_text: str,
        part_label: str,
        index: int,
        total_parts: int,
        job_dir: Path,
        gameplay: list[Footage],
        music: list[Footage],
        title: str,
    ) -> Rendered:
        cfg = self.cfg
        edit = cfg.edit
        faces = []
        if edit.face_tracking and (edit.zoom or layout.mode == "fullscreen"):
            faces = find_faces(self.tools, clip, start, end - start)
        plan = plan_edit(cfg, layout, start, end, words, loudness, faces)
        length = plan.duration
        segments = plan_segments(gameplay, length, self.state.usage, self.rng, cfg.gameplay.skip_start, cfg.gameplay.skip_end) if layout.game_box else []

        hook = None
        hook_lines = "\n".join(x for x in (hook_text, part_label) if x)
        if cfg.hook.enabled and hook_lines:
            hook = Hook(hook_lines, layout.hook_y, cfg.hook.duration)

        ass_rel = None
        if plan.chunks or hook:
            ass_path = job_dir / f"part{index}.ass"
            ass_path.write_text(
                build_ass(plan.chunks, width=layout.width, height=layout.height, duration=length,
                          captions=plan.style, hook_cfg=cfg.hook, hook=hook),
                encoding="utf-8",
            )
            ass_rel = ass_path.relative_to(APP_DIR).as_posix()

        emoji_size = edit.emoji_size
        caption_y = round(layout.height * plan.style.position)
        job = RenderJob(
            clip=clip,
            clip_start=start,
            duration=length,
            clip_has_audio=info.has_audio,
            layout=layout,
            segments=segments,
            output=job_dir / f"part{index}.mp4",
            ass_file=ass_rel,
            fonts_dir=FONTS_DIR.relative_to(APP_DIR).as_posix(),
            mirror=cfg.gameplay.mirror and self.rng.random() < 0.5,
            intervals=plan.intervals,
            clip_size=(info.width, info.height),
            zooms=plan.zooms,
            zoom_amount=edit.zoom_amount,
            reframe=plan.reframe,
            emojis=plan.emojis,
            emoji_y=caption_y - round(plan.style.font_size * 0.8) - emoji_size,
            emoji_size=emoji_size,
            mute=plan.mutes,
            duck_music=cfg.audio.music_duck,
        )
        if plan.sounds:
            job.sfx_track = write_track(plan.sounds, length, job_dir / f"sfx{index}.wav", edit.sfx_volume)
        choice = pick_music(music, length, self.rng)
        if choice:
            track, music_start = choice
            job.music, job.music_start, job.music_loop = track.path, music_start, track.duration < length + 1
        part_text = f" part {index}/{total_parts}" if total_parts > 1 else ""
        pieces = ", ".join(f"{s.key} @{s.start:.0f}s" for s in segments) or "none (full-screen clip)"
        cut = end - start - length
        log.info(
            "Rendering%s (%.1fs%s, %d zooms, %d emojis) with gameplay: %s", part_text, length,
            f", {cut:.1f}s of pauses cut" if cut > 0.05 else "", len(plan.zooms), len(plan.emojis), pieces,
        )
        run_ffmpeg(
            self.tools,
            build_command(job, cfg),
            log_path=job_dir / f"ffmpeg_part{index}.log",
            cwd=APP_DIR,
            duration=length,
            label=f"  rendering{part_text}:",
            should_stop=self.should_stop,
        )
        cover = None
        if edit.thumbnail:
            try:
                clean = self._clean_frame(job, plan, job_dir / f"cover{index}.mp4")
                cover = make_cover(self.tools, clean, 0.0, title, job_dir / f"part{index}.jpg",
                                   width=layout.width, height=layout.height, captions=plan.style, work_dir=job_dir,
                                   fonts_dir=job.fonts_dir)
            except MediaError as exc:
                log.warning("Couldn't make the cover picture: %s", exc)
        censor = set(edit.censor_words) if edit.censor else None
        srt = build_srt(plan.words, censor=censor) if cfg.captions.enabled and cfg.captions.save_srt and plan.words else None
        return Rendered(job.output, srt, segments, f"_part{index}" if total_parts > 1 else "", title, length, cover)
