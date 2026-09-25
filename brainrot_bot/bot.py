"""The watcher: finds new clips, turns each one into a reel, remembers what is done."""

from __future__ import annotations

import logging
import os
import random
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .captions import Hook, build_ass, build_srt, make_chunks
from .config import APP_DIR, FONTS_DIR, MODELS_DIR, WORK_DIR
from .gameplay import Footage, list_media, pick_music, plan_segments, scan_library
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, MediaInfo, Tools, probe, run_ffmpeg
from .parts import plan_parts
from .render import Layout, RenderJob, build_command, compute_layout
from .state import State
from .transcribe import Transcriber, Word

log = logging.getLogger("brainrot")

HOOK_FILES = ("title.txt", "hook.txt")
MAX_HOOK_CHARS = 160
WAIT_LOG_EVERY = 600  # seconds between "still waiting for gameplay" messages


@dataclass
class Rendered:
    video: Path
    srt: str | None
    segments: list
    part_suffix: str


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
        log.info("Watching %s for new clips. Press Ctrl+C to stop.", self.cfg.paths.clips)
        while True:
            try:
                ready, _ = self.scan()
                for path in ready:
                    self.process(path)
            except Exception:  # noqa: BLE001 - e.g. a folder that briefly can't be read; never stop the bot
                log.exception("Unexpected problem, will keep going")
            time.sleep(self.cfg.watch.poll_seconds)

    def run_once(self) -> int:
        """Process everything that is in the clips folder right now, then return."""
        done = 0
        attempted: set[Path] = set()
        while True:
            ready, settling = self.scan()
            ready = [path for path in ready if path not in attempted]
            for path in ready:
                attempted.add(path)
                done += 1 if self.process(path) else 0
            if not ready and not settling:
                return done
            if not ready:
                time.sleep(1)

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
            outputs = self.make_reels(clip, gameplay)
        except Exception as exc:  # noqa: BLE001 - one bad clip must never stop a 24/7 bot
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

        entry.update(
            status="done",
            outputs=[str(p) for p in outputs],
            updated=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        entry.pop("error", None)
        entry.pop("next_try", None)
        self.state.clips[key] = entry
        self._save()
        log.info("Done in %.0fs: %s", time.monotonic() - started, ", ".join(str(p) for p in outputs))
        return outputs

    def _save(self) -> None:
        if self.persist:
            self.state.save()

    def make_reels(self, clip: Path, gameplay: list[Footage]) -> list[Path]:
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
            if self.transcriber is not None and info.has_audio:
                words = self._transcribe(clip, duration, job_dir)
            parts = plan_parts(duration, words, self.cfg.parts.max_seconds)
            layout = compute_layout(self.cfg.video, info.width, info.height)
            hook_text = find_hook_text(clip)

            rendered = []
            for index, (start, end) in enumerate(parts, 1):
                label = f"PART {index}/{len(parts)}" if len(parts) > 1 else ""
                rendered.append(self._render_part(clip, info, layout, start, end, words, hook_text, label, index, len(parts), job_dir, gameplay, music))

            # Everything rendered: now move the results into the output folder.
            try:
                rel_dir = clip.parent.relative_to(self.cfg.paths.clips)
            except ValueError:
                rel_dir = Path()
            out_dir = self.cfg.paths.output / rel_dir
            outputs = []
            for item in rendered:
                suffix = item.part_suffix + ("_preview" if self.preview_seconds else "")
                final = unique_path(out_dir, clip.stem + suffix, ".mp4")
                publish(item.video, final)
                if item.srt:
                    final.with_suffix(".srt").write_text(item.srt, encoding="utf-8")
                for seg in item.segments:
                    self.state.usage[seg.key] = self.state.usage.get(seg.key, 0) + 1
                outputs.append(final)
            return outputs
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _transcribe(self, clip: Path, duration: float, job_dir: Path) -> list[Word]:
        wav = job_dir / "voice.wav"
        run_ffmpeg(
            self.tools,
            ["-i", str(clip), "-t", f"{duration:.3f}", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            log_path=job_dir / "ffmpeg_audio.log",
        )
        log.info("Listening to the clip to write captions...")
        assert self.transcriber is not None
        return self.transcriber.transcribe(wav)

    def _render_part(
        self,
        clip: Path,
        info: MediaInfo,
        layout: Layout,
        start: float,
        end: float,
        words: list[Word],
        hook_text: str,
        part_label: str,
        index: int,
        total_parts: int,
        job_dir: Path,
        gameplay: list[Footage],
        music: list[Footage],
    ) -> Rendered:
        cfg = self.cfg
        length = end - start
        part_words = [Word(w.text, w.start - start, min(w.end, end) - start) for w in words if start <= w.start < end]
        segments = plan_segments(gameplay, length, self.state.usage, self.rng, cfg.gameplay.skip_start, cfg.gameplay.skip_end)

        hook = None
        hook_lines = "\n".join(x for x in (hook_text, part_label) if x)
        if cfg.hook.enabled and hook_lines:
            hook = Hook(hook_lines, layout.top_h + round(layout.height * 0.035), cfg.hook.duration)
        chunks = make_chunks(part_words, cfg.captions, length) if cfg.captions.enabled else []

        ass_rel = None
        if chunks or hook:
            ass_path = job_dir / f"part{index}.ass"
            ass_path.write_text(
                build_ass(
                    chunks,
                    width=layout.width,
                    height=layout.height,
                    duration=length,
                    captions=cfg.captions,
                    hook_cfg=cfg.hook,
                    hook=hook,
                ),
                encoding="utf-8",
            )
            ass_rel = ass_path.relative_to(APP_DIR).as_posix()

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
        )
        choice = pick_music(music, length, self.rng)
        if choice:
            track, music_start = choice
            job.music, job.music_start, job.music_loop = track.path, music_start, track.duration < length + 1
        part_text = f" part {index}/{total_parts}" if total_parts > 1 else ""
        pieces = ", ".join(f"{s.key} @{s.start:.0f}s" for s in segments)
        log.info("Rendering%s (%.1fs) with gameplay: %s", part_text, length, pieces)
        run_ffmpeg(
            self.tools,
            build_command(job, cfg),
            log_path=job_dir / f"ffmpeg_part{index}.log",
            cwd=APP_DIR,
            duration=length,
            label=f"  rendering{part_text}:",
        )
        srt = build_srt(part_words) if cfg.captions.enabled and cfg.captions.save_srt and part_words else None
        return Rendered(job.output, srt, segments, f"_part{index}" if total_parts > 1 else "")
