"""The watcher: finds new clips, turns each one into a reel, remembers what is done."""

from __future__ import annotations

import copy
import logging
import os
import random
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .ai import AI
from .analysis import Loudness
from .captions import Hook, build_ass, build_srt, group_words, prepare_words, time_groups
from .config import APP_DIR, FONTS_DIR, MODELS_DIR, STOP_FILE, WORK_DIR
from .copywriter import merge_hashtags, write_copy
from .credentials import Credentials
from .dedupe import Fingerprints, frame_hashes, quick_hash, sound_bits, text_signature
from .editor import EditPlan, plan_edit
from .faces import find_faces
from .gameplay import Footage, list_media, pick_music, plan_segments, scan_library
from .links import LinkQueue
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, MediaInfo, StopRequested, Tools, probe, run_ffmpeg
from .moments import auto_count, find_moments, moments_from_picks, split_sentences, transcript_for_ai
from .parts import plan_parts
from .render import Layout, RenderJob, build_command, compute_layout
from .sfx import write_track
from .state import State
from .thumbnail import make_cover
from .transcribe import Transcriber, Word
from .translate import SCRIPT_FONTS, base_language, caption_settings, language_name, translate_reel
from .uploads import PostInfo, UploadQueue, pretty_title

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


@dataclass
class Rendered:
    video: Path
    srt: str | None
    segments: list
    suffix: str
    post: PostInfo
    cover: Path | None = None
    language: str = ""  # set for translated reels

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
        # Test renders (--clip) are never posted, and don't download links.
        self.uploads = UploadQueue(cfg, self.state, self.credentials, self._save) if persist else None
        if self.uploads is not None:
            self.uploads.blocked.clear()  # accounts may have been connected again since last time
        self.links = LinkQueue(cfg, self.state, tools, self._save) if persist else None
        self.fingerprints = Fingerprints(self.state, cfg.dedupe.similarity) if cfg.dedupe.enabled and persist else None

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
        while not self.should_stop():
            try:
                self._download_links()
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
            for item in rendered:
                if item.language and not self.cfg.upload.post_translations:
                    continue
                platforms = self.uploads.add(item.video, item.post)
                if platforms:
                    log.info("Queued %s for posting on %s", item.video.name, ", ".join(platforms))
        for name in ("error", "next_try", "same_as"):
            entry.pop(name, None)
        self.state.clips[key] = entry
        self._save()
        log.info("Done in %.0fs: %s", time.monotonic() - started, ", ".join(str(p) for p in outputs) or "no reels")
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
            moments = None
            if self.cfg.ai.moments and self.ai.available:
                sentences = split_sentences(ctx.words)
                source = f' called "{pretty_title(ctx.path.stem)}"' + (f' (from "{ctx.folder}")' if ctx.folder else "")
                picks = self.ai.pick_moments(transcript_for_ai(sentences), _length_text(duration), source, count,
                                             int(m.min_seconds), int(m.max_seconds))
                if picks:
                    moments = moments_from_picks(picks, sentences, duration, count, m.min_seconds, m.max_seconds)
            if not moments:
                moments = find_moments(ctx.words, duration, count, m.min_seconds, m.max_seconds, ctx.loudness)
            log.info("Long video: making %d reel(s) from its best moments: %s", len(moments),
                     ", ".join(f"{int(x.start // 60)}:{int(x.start % 60):02d}" for x in moments))
            return [Piece(x.start, x.end, f"_moment{i}", hook=x.hook) for i, x in enumerate(moments, 1)]

        parts = plan_parts(duration, ctx.words, self.cfg.parts.max_seconds)
        n = len(parts)
        return [
            Piece(start, end, f"_part{i}" if n > 1 else "", f"PART {i}/{n}" if n > 1 else "", title_suffix=f" (Part {i}/{n})" if n > 1 else "")
            for i, (start, end) in enumerate(parts, 1)
        ]

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
        hashtags = merge_hashtags(cfg.upload.hashtags, folder_hashtags(ctx.path), text.hashtags)

        item, job, plan, clean = self._render_part(ctx, piece, index, hook, title)
        item.post = PostInfo(title, hashtags, plan.duration, description)
        results = [item]
        spoken = base_language(ctx.language or cfg.captions.language)
        for lang in cfg.captions.translate_to:
            if base_language(lang) == spoken:
                continue
            translated = self._render_translation(ctx, piece, index, job, plan, clean, lang, hook, item.post)
            if translated is not None:
                results.append(translated)
        return results

    # ------------------------------------------------------------ rendering

    def _clean_frame(self, job: RenderJob, plan, out: Path) -> Path:
        """The reel's picture at the cover moment, without captions or emojis (so the title fits)."""
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
        still = copy.deepcopy(job)
        still.intervals = [(source, source + 2 / fps)]
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
                      hook_cfg=hook_cfg or self.cfg.hook, hook=hook),
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

    def _render_part(self, ctx: _Clip, piece: Piece, index: int, hook_text: str, title: str):
        cfg = self.cfg
        edit = cfg.edit
        layout = ctx.layout
        faces = []
        if edit.face_tracking and (edit.zoom or layout.mode == "fullscreen"):
            faces = find_faces(self.tools, ctx.path, piece.start, piece.end - piece.start)
        plan = plan_edit(cfg, layout, piece.start, piece.end, ctx.words, ctx.loudness, faces)
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
            normalize=ctx.loudness is None or not ctx.loudness.levels
            or max(ctx.loudness.level(a, b) for a, b in plan.intervals) > 1e-4,
        )
        if plan.sounds:
            job.sfx_track = write_track(plan.sounds, length, ctx.job_dir / f"sfx{index}.wav", edit.sfx_volume)
        choice = pick_music(ctx.music, length, self.rng)
        if choice:
            track, music_start = choice
            job.music, job.music_start, job.music_loop = track.path, music_start, track.duration < length + 1
        what = f" {piece.suffix.lstrip('_').replace('part', 'part ').replace('moment', 'moment ')}" if piece.suffix else ""
        pieces = ", ".join(f"{s.key} @{s.start:.0f}s" for s in segments) or "none (full-screen clip)"
        cut = piece.end - piece.start - length
        log.info(
            "Rendering%s (%.1fs%s, %d zooms, %d emojis) with gameplay: %s", what, length,
            f", {cut:.1f}s of pauses cut" if cut > 0.05 else "", len(plan.zooms), len(plan.emojis), pieces,
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
        srt = build_srt(plan.words, censor=censor) if cfg.captions.enabled and cfg.captions.save_srt and plan.words else None
        item = Rendered(job.output, srt, segments, piece.suffix, PostInfo(title, "", length), cover)
        return item, job, plan, clean

    def _render_translation(self, ctx: _Clip, piece: Piece, index: int, job: RenderJob, plan: EditPlan,
                            clean: Path | None, lang: str, hook_text: str, post: PostInfo) -> Rendered | None:
        """The same reel with the captions (and hook, title, caption) in another language."""
        cfg = self.cfg
        if not cfg.captions.enabled or not plan.words:
            return None
        if not self.ai.available:
            log.warning("Captions in %s need the AI (connect a key in the menu); skipping.", language_name(lang))
            return None
        result = translate_reel(self.ai, plan.words, {"hook": hook_text, "title": post.title, "caption": post.description}, lang)
        if result is None:
            return None
        words, extras = result
        style = caption_settings(plan.style, lang)
        prepared = prepare_words(words, style.uppercase, style.remove_punctuation)
        chunks = time_groups(group_words(prepared, style.max_words, style.max_chars), plan.duration)
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
        srt = build_srt(words) if cfg.captions.save_srt else None
        info = PostInfo(title, post.hashtags, post.duration, extras.get("caption", post.description))
        return Rendered(translated.output, srt, job.segments, f"{piece.suffix}_{lang}", info, cover, language=lang)
