"""Command line entry point.

    BrainrotBot.exe / python brainrot.py   setup the first time, then the menu (normal use)
    --setup            run the setup again
    --run              just run the bot in this window with a live log (no menu)
    --once             process what is waiting, post what is due, then exit
    --clip FILE        make a reel from one video right now (never posted)
    --clip FILE --preview 15   only the first 15 seconds, to test the look
    --check            check the setup
    --stop / --status  stop / ask about the background bot
    --retry-failed     give clips that failed another chance
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__, service
from .bot import Bot
from .config import FONTS_DIR, SERVER, STOP_FILE, WORK_DIR, ConfigError, ensure_config_file, load_config
from .gameplay import list_media
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, Tools, find_tools

log = logging.getLogger("brainrot")
FORMAT = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """On Windows the log can't be renamed while the menu is reading it; just try again later."""

    def doRollover(self) -> None:  # noqa: N802
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def setup_console_logging(verbose: bool) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")  # odd file names must never crash logging on Windows
        except (OSError, ValueError):
            pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FORMAT)
    log.addHandler(handler)


def add_file_logging(logs_dir: Path) -> None:
    if any(isinstance(h, logging.FileHandler) for h in log.handlers):
        return
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = SafeRotatingFileHandler(logs_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
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
        from .gpu import NAMES, pick_codec

        codec = pick_codec(tools, cfg.video)
        encoder_ok = codec in tools.encoders
        where = f" ({NAMES[codec]}, picked by video.codec: auto)" if cfg.video.codec == "auto" and codec in NAMES else ""
        print(f"  encoder:   {codec}{where} {'OK' if encoder_ok else 'MISSING - change video.codec in config.yaml'}")
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
    print(f"  bot:       {'running' if service.is_running() else 'not running'}")
    if not list_media(cfg.paths.gameplay, VIDEO_EXTS):
        print("\n  -> Put some gameplay videos in the gameplay folder before adding clips.")
    print("\nEverything looks good." if ok else "\nFix the items marked above, then run --check again.")
    return ok


def _tray(cfg, bot: Bot, config: Path | None):
    """The icon next to the clock for the background bot (Windows)."""
    from .dashboard import get_token
    from .phone import Action
    from .tray import start_tray
    from .ui import open_folder

    return start_tray(
        cfg,
        dashboard_url=lambda: f"http://127.0.0.1:{cfg.dashboard.port}/?key={get_token(bot.credentials)}",
        open_app=lambda: service.open_window(config),
        open_folder=open_folder,
        is_paused=lambda: bool(bot.uploads is not None and bot.uploads.paused),
        toggle_pause=lambda: bot.inbox.put(Action("resume" if bot.uploads is not None and bot.uploads.paused else "pause")),
        stop=bot.stop_event.set,
    )


def run_bot(cfg, tools: Tools, *, once: bool = False, preload: bool = True, tray: bool = False, config: Path | None = None) -> int:
    """Run the watcher in this process (background process, --run or --once)."""
    lock = service.InstanceLock()
    if not lock.acquire():
        log.error("The bot is already running (in another window or in the background). Stop that one first.")
        return 3
    try:
        service.write_pid()
        STOP_FILE.unlink(missing_ok=True)
        add_file_logging(cfg.paths.logs)
        shutil.rmtree(WORK_DIR, ignore_errors=True)  # leftovers from a crash or power cut
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _raise_interrupt)
        service.kill_children_on_exit()
        if cfg.watch.low_priority:
            lower_priority()
        log.info("Brainrot bot %s | ffmpeg: %s", __version__, tools.ffmpeg)
        log.info("Clips: %s | Gameplay: %s | Output: %s", cfg.paths.clips, cfg.paths.gameplay, cfg.paths.output)
        bot = Bot(cfg, tools)
        icon = _tray(cfg, bot, config) if tray else None
        try:
            if once:
                count = bot.run_once()
                log.info("Finished: %d clip(s) processed.", count)
                return 0
            if preload and bot.transcriber is not None:
                try:
                    bot.transcriber.load()  # download/load the speech model now, not when the first clip arrives
                except Exception as exc:  # noqa: BLE001
                    log.warning("Couldn't load the speech model yet (%s). Will try again when a clip arrives.", exc)
            bot.run_forever()
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 130  # tells run_bot.bat / run_bot.sh that you stopped it on purpose (no auto-restart)
        finally:
            if icon is not None:
                icon.stop()
        return 0
    finally:
        try:
            service.PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        lock.release()


def main(argv: list[str] | None = None) -> int:
    windowless = sys.stdout is None  # BrainrotBot-background.exe has no console at all
    if windowless:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
        logging.raiseExceptions = False

    parser = argparse.ArgumentParser(prog="brainrot.py", description="Turns podcast/educational clips into brainrot reels with gameplay and captions.")
    parser.add_argument("--config", type=Path, help="path to config.yaml (default: the one next to the app)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="run the setup again")
    mode.add_argument("--run", action="store_true", help="run the bot in this window with a live log (no menu)")
    mode.add_argument("--once", action="store_true", help="process the clips that are waiting, then exit")
    mode.add_argument("--clip", type=Path, metavar="FILE", help="make a reel from this one video right now")
    mode.add_argument("--check", action="store_true", help="check that ffmpeg, captions and folders are set up, then exit")
    mode.add_argument("--retry-failed", action="store_true", help="give clips that failed another chance")
    mode.add_argument("--stop", action="store_true", help="stop the bot running in the background")
    mode.add_argument("--status", action="store_true", help="say whether the bot is running")
    mode.add_argument("--dashboard", action="store_true", help="show the phone dashboard link and QR code")
    mode.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preview", type=float, default=0, metavar="SECONDS", help="with --clip: only render the first SECONDS")
    parser.add_argument("--verbose", action="store_true", help="more detailed logging")
    args = parser.parse_args(argv)

    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        ensure_config_file()
        if args.config:
            ensure_config_file(args.config)
    except OSError:
        pass

    if args.selftest:
        from .selftest import run as selftest

        return selftest()

    plain = (args.once or args.clip or args.check or args.retry_failed or args.run or args.stop or args.status or args.background
             or args.autostart or args.dashboard)
    if not plain:
        from .app import run_app  # the menu; imported here so --background never loads UI code

        try:
            return run_app(args.config, force_setup=args.setup)
        except KeyboardInterrupt:
            return 130

    if args.stop:
        print("Stopping the bot (it finishes the current step first)...")
        stopped = service.stop_background()
        print("The bot is stopped." if stopped else "Couldn't stop the bot.")
        return 0 if stopped else 1
    if args.status:
        print("The bot is running." if service.is_running() else "The bot is not running.")
        return 0

    if not windowless:
        setup_console_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("Problem in config: %s", exc)
        return 2
    if args.dashboard:
        from .credentials import Credentials
        from .dashboard import dashboard_url, qr_text

        url = dashboard_url(cfg, Credentials(cfg.paths.credentials))
        if SERVER and not os.environ.get("BRAINROT_PUBLIC_URL"):
            url = url.replace(url.split("/")[2], f"<this server's address>:{cfg.dashboard.port}")
            print("Set BRAINROT_PUBLIC_URL in docker-compose.yml (e.g. http://192.168.1.50:8770) to get a QR code.")
        else:
            print(qr_text(url))
        print(url)
        return 0
    for folder in (cfg.paths.clips, cfg.paths.gameplay, cfg.paths.music, cfg.paths.output):
        folder.mkdir(parents=True, exist_ok=True)

    if args.autostart:
        time.sleep(cfg.app.autostart_delay)  # let the computer finish logging in first
        if cfg.app.run_mode == "window" and os.name == "nt":
            service.open_window(args.config)
            return 0

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
    if cfg.video.codec != "auto" and cfg.video.codec not in tools.encoders:
        log.error("Your ffmpeg has no '%s' encoder. Change video.codec in config.yaml (auto picks one that works).", cfg.video.codec)
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

    if args.retry_failed:
        from .state import State

        was_running = service.is_running()
        if was_running and not service.stop_background():
            log.error("Couldn't stop the running bot; try again.")
            return 1
        state = State(cfg.paths.state_file)
        count = state.retry_failed()
        state.save()
        log.info("%d failed clip(s) will be tried again.", count)
        if was_running:
            service.start_background(args.config)
        return 0

    return run_bot(cfg, tools, once=args.once, tray=args.background or args.autostart, config=args.config)
