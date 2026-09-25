"""Command line entry point.

    python brainrot.py                  watch the clips folder forever (the normal 24/7 mode)
    python brainrot.py --once           process what is there now, then exit
    python brainrot.py --clip FILE      make a reel from one video right now
    python brainrot.py --clip FILE --preview 15   only the first 15 seconds (to test the look)
    python brainrot.py --check          check the setup
    python brainrot.py --retry-failed   give clips that failed another chance, then keep watching
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import signal
import sys
from pathlib import Path

from . import __version__
from .bot import Bot
from .config import APP_DIR, FONTS_DIR, WORK_DIR, ConfigError, load_config
from .gameplay import list_media
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, Tools, find_tools

log = logging.getLogger("brainrot")
FORMAT = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")


def setup_console_logging(verbose: bool) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")  # odd file names must never crash logging on Windows
        except (OSError, ValueError):
            pass
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FORMAT)
    log.addHandler(handler)


def add_file_logging(logs_dir: Path) -> None:
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(logs_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError as exc:
        log.warning("Can't write log file in %s: %s", logs_dir, exc)
        return
    handler.setFormatter(FORMAT)
    log.addHandler(handler)


def lower_priority() -> None:
    """Run at lower CPU priority so the computer stays smooth (games, browsing) while the bot renders."""
    try:
        if os.name == "nt":
            import ctypes

            below_normal = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), below_normal)
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001 - nice to have only
        pass


class InstanceLock:
    """Makes sure only one bot works on the folders at a time."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self._file = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            return False
        return True


def _raise_interrupt(*_args) -> None:
    raise KeyboardInterrupt


def check_setup(cfg, tools: Tools | None, problem: str = "") -> bool:
    ok = True
    print(f"Brainrot bot {__version__}")
    print(f"  config:    {cfg.config_path}")
    if tools is None:
        print(f"  ffmpeg:    NOT FOUND - {problem}")
        ok = False
    else:
        print(f"  ffmpeg:    {tools.ffmpeg}")
        print(f"             {tools.version}")
        subtitles = "ass" in tools.filters
        print(f"  captions:  {'OK (libass)' if subtitles else 'MISSING - this ffmpeg cannot draw captions (see README)'}")
        ok &= subtitles or not (cfg.captions.enabled or cfg.hook.enabled)
        encoder_ok = cfg.video.codec in tools.encoders
        print(f"  encoder:   {cfg.video.codec} {'OK' if encoder_ok else 'MISSING - change video.codec in config.yaml'}")
        ok &= encoder_ok
    try:
        import faster_whisper  # noqa: F401

        print(f"  speech:    faster-whisper OK (model '{cfg.captions.model}')")
    except ImportError:
        print("  speech:    faster-whisper NOT INSTALLED - run: pip install -r requirements.txt")
        ok &= not cfg.captions.enabled
    fonts = sorted(p.name for p in FONTS_DIR.glob("*.[ot]tf")) if FONTS_DIR.is_dir() else []
    print(f"  fonts:     {', '.join(fonts) or 'none bundled (system fonts will be used)'}")
    for name, folder, exts in (("clips", cfg.paths.clips, VIDEO_EXTS), ("gameplay", cfg.paths.gameplay, VIDEO_EXTS), ("music", cfg.paths.music, AUDIO_EXTS)):
        print(f"  {name + ':':<10} {folder}  ({len(list_media(folder, exts))} files)")
    print(f"  output:    {cfg.paths.output}")
    if not list_media(cfg.paths.gameplay, VIDEO_EXTS):
        print("\n  -> Put some gameplay videos in the gameplay folder before adding clips.")
    print("\nEverything looks good." if ok else "\nFix the items marked above, then run --check again.")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brainrot.py", description="Turns podcast/educational clips into brainrot reels with gameplay and captions.")
    parser.add_argument("--config", type=Path, help="path to config.yaml (default: the one next to brainrot.py)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process the clips that are waiting, then exit")
    mode.add_argument("--clip", type=Path, metavar="FILE", help="make a reel from this one video right now")
    mode.add_argument("--check", action="store_true", help="check that ffmpeg, captions and folders are set up, then exit")
    mode.add_argument("--retry-failed", action="store_true", help="give clips that failed another chance, then keep watching")
    parser.add_argument("--preview", type=float, default=0, metavar="SECONDS", help="with --clip: only render the first SECONDS")
    parser.add_argument("--verbose", action="store_true", help="more detailed logging")
    args = parser.parse_args(argv)

    setup_console_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("Problem in config: %s", exc)
        return 2
    for folder in (cfg.paths.clips, cfg.paths.gameplay, cfg.paths.music, cfg.paths.output):
        folder.mkdir(parents=True, exist_ok=True)

    needs_libass = cfg.captions.enabled or cfg.hook.enabled
    try:
        tools = find_tools(cfg.tools.ffmpeg, cfg.tools.ffprobe, need_subtitles=needs_libass)
    except MediaError as exc:
        if args.check:
            return 0 if check_setup(cfg, None, str(exc)) else 1
        log.error("%s", exc)
        return 2
    if args.check:
        return 0 if check_setup(cfg, tools) else 1
    if needs_libass and "ass" not in tools.filters:
        log.error(
            "Your ffmpeg (%s) can't draw captions (it was built without libass). "
            "Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg-full | Linux: install your distro's ffmpeg package.",
            tools.ffmpeg,
        )
        return 2
    if cfg.video.codec not in tools.encoders:
        log.error("Your ffmpeg has no '%s' encoder. Change video.codec in config.yaml.", cfg.video.codec)
        return 2

    if args.clip:
        clip = args.clip.expanduser().resolve()
        if not clip.is_file():
            log.error("File not found: %s", clip)
            return 2
        bot = Bot(cfg, tools, persist=False, preview_seconds=max(0.0, args.preview))
        try:
            return 0 if bot.process(clip) else 1
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 130

    lock = InstanceLock(APP_DIR / ".bot.lock")
    if not lock.acquire():
        log.error("The bot is already running (in another window or in the background). Stop that one first.")
        return 3
    add_file_logging(cfg.paths.logs)
    shutil.rmtree(WORK_DIR, ignore_errors=True)  # leftovers from a crash or power cut
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _raise_interrupt)

    bot = Bot(cfg, tools)
    if args.retry_failed:
        count = bot.state.retry_failed()
        bot.state.save()
        log.info("%d failed clip(s) will be tried again.", count)

    log.info("Brainrot bot %s | ffmpeg: %s", __version__, tools.ffmpeg)
    log.info("Clips: %s | Gameplay: %s | Output: %s", cfg.paths.clips, cfg.paths.gameplay, cfg.paths.output)
    if cfg.watch.low_priority:
        lower_priority()
    try:
        if args.once:
            count = bot.run_once()
            log.info("Finished: %d clip(s) processed.", count)
            return 0
        if bot.transcriber is not None:
            try:
                bot.transcriber.load()  # download/load the speech model now, not when the first clip arrives
            except Exception as exc:  # noqa: BLE001
                log.warning("Couldn't load the speech model yet (%s). Will try again when a clip arrives.", exc)
        bot.run_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 130  # tells run_bot.bat / run_bot.sh that you stopped it on purpose (no auto-restart)
    return 0
