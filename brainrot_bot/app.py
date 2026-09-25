"""What you see when you double-click BrainrotBot.exe: the setup the first time, then a small menu."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from . import __version__, service, ui
from .bot import Bot
from .cli import add_file_logging, lower_priority
from .config import DEFAULT_CONFIG_PATH, STOP_FILE, WORK_DIR, ConfigError, load_config
from .credentials import Credentials
from .gameplay import list_media
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, Tools, find_tools
from .uploads import PLATFORMS, platform_name
from .wizard import run_setup

log = logging.getLogger("brainrot")


class InWindowBot:
    """Window mode: the bot runs inside this app and stops when the window closes."""

    def __init__(self):
        self.bot: Bot | None = None
        self.thread: threading.Thread | None = None
        self.lock = service.InstanceLock()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg: SimpleNamespace, tools: Tools) -> bool:
        if self.running:
            return True
        if not self.lock.acquire():
            return False
        STOP_FILE.unlink(missing_ok=True)
        service.write_pid()
        # No kill-on-close job here: ffmpeg shares this console and closes with the window anyway, and a
        # job would also take down a background bot started from this window after a settings change.
        add_file_logging(cfg.paths.logs)
        if cfg.watch.low_priority:
            lower_priority()
        self.bot = Bot(cfg, tools)

        def work() -> None:
            try:
                if self.bot.transcriber is not None:
                    try:
                        self.bot.transcriber.load()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Couldn't load the speech model yet (%s). Will try again when a clip arrives.", exc)
                self.bot.run_forever()
            except Exception:  # noqa: BLE001
                log.exception("The bot stopped because of an unexpected problem")

        self.thread = threading.Thread(target=work, name="bot", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        if self.bot is not None:
            self.bot.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=60)
        self.thread = None
        self.lock.release()


def _load_state(cfg: SimpleNamespace) -> dict:
    try:
        return json.loads(cfg.paths.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _status_lines(cfg: SimpleNamespace, running: bool, in_window: bool) -> list[str]:
    state = _load_state(cfg)
    clips_state = state.get("clips", {})
    done = sum(1 for e in clips_state.values() if e.get("status") == "done")
    failed = sum(1 for e in clips_state.values() if e.get("status") == "failed")
    clip_files = list_media(cfg.paths.clips, VIDEO_EXTS)
    waiting = sum(1 for p in clip_files if clips_state.get(p.relative_to(cfg.paths.clips).as_posix(), {}).get("status") not in ("done", "failed"))
    gameplay = len(list_media(cfg.paths.gameplay, VIDEO_EXTS))
    music = len(list_media(cfg.paths.music, AUDIO_EXTS))

    if running:
        status = ui.green("RUNNING") + (" in this window (stops when you close it)" if in_window else " in the background 24/7 (you can close this window)")
    else:
        status = ui.red("STOPPED")
    autostart = f"on ({int(cfg.app.autostart_delay)}s after login)" if service.autostart_enabled() else "off"
    lines = [
        f"  Status:      {status}",
        f"  Auto-start:  {autostart}",
        f"  Gameplay:    {cfg.paths.gameplay}  " + ui.dim(f"({gameplay} videos)") + ("" if gameplay else ui.red("  <- add some!")),
        f"  Clips:       {cfg.paths.clips}  " + ui.dim(f"({waiting} waiting)"),
        f"  Reels:       {cfg.paths.output}  " + ui.dim(f"({done} clips done" + (f", {failed} failed" if failed else "") + ")"),
        f"  Music:       {cfg.paths.music}  " + ui.dim(f"({music} tracks, optional)"),
    ]
    creds = Credentials(cfg.paths.credentials)
    uploads = state.get("uploads", {})
    blocked = state.get("upload_blocked", {})
    posting = []
    for key in PLATFORMS:
        if not (getattr(cfg.upload, key) and creds.get(key)):
            continue
        pending = sum(1 for i in uploads.values() if i.get("platform") == key and i.get("status") == "pending")
        posted = sum(1 for i in uploads.values() if i.get("platform") == key and i.get("status") == "done")
        note = ui.red(" needs login!") if key in blocked else ""
        posting.append(f"{platform_name(key)} ({posted} posted, {pending} waiting){note}")
    lines.append("  Posting:     " + (", ".join(posting) if posting else ui.dim("off (reels are saved only)")))
    return lines


def _watch_log(cfg: SimpleNamespace) -> None:
    """Show the log as it grows until Enter is pressed."""
    path = cfg.paths.logs / "bot.log"
    ui.banner("live activity")
    ui.say(ui.dim("Press Enter to go back to the menu."))
    ui.say()
    done = threading.Event()

    def follow() -> None:
        position = 0
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines()[-20:]:
                ui.say(line)
            position = len(text.encode("utf-8"))
        except OSError:
            ui.say(ui.dim("(nothing yet)"))
        while not done.is_set():
            try:
                with open(path, "rb") as handle:
                    handle.seek(0, 2)
                    size = handle.tell()
                    if size < position:  # log file rotated
                        position = 0
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
                if chunk:
                    print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
            except OSError:
                pass
            done.wait(0.5)

    thread = threading.Thread(target=follow, daemon=True)
    thread.start()
    ui.pause("")
    done.set()
    thread.join(timeout=2)


def _retry_failed(cfg: SimpleNamespace, restart) -> None:
    from .state import State

    was_running = service.is_running()
    if was_running:
        restart(stop_only=True)
    state = State(cfg.paths.state_file)
    count = state.retry_failed()
    state.save()
    ui.say(ui.green(f"{count} clip(s) will be tried again."))
    if was_running:
        restart()


def run_app(config_path: Path | None, force_setup: bool = False) -> int:
    config_path = Path(config_path or DEFAULT_CONFIG_PATH)
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        ui.say(ui.red(f"Problem in {config_path.name}: {exc}"))
        ui.pause()
        return 2
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    if force_setup or not cfg.app.setup_done:
        cfg = run_setup(config_path)

    try:
        tools = find_tools(cfg.tools.ffmpeg, cfg.tools.ffprobe, need_subtitles=cfg.captions.enabled or cfg.hook.enabled)
    except MediaError as exc:
        ui.say(ui.red(str(exc)))
        ui.pause()
        return 2

    in_window = InWindowBot()
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    def start(current: SimpleNamespace) -> None:
        if current.app.run_mode == "background":
            ui.say("Starting the bot in the background...")
            if not service.start_background(config_path):
                ui.say(ui.red("The bot didn't start. Look at logs/bot.log for the reason."))
                ui.pause()
            return
        if service.is_running() and not in_window.running:
            ui.say("The bot is running in the background. Moving it into this window...")
            service.stop_background()
        if not in_window.start(current, tools):
            ui.say(ui.red("Couldn't start the bot here (is another copy running?)."))
            ui.pause()

    def restart(stop_only: bool = False) -> None:
        ui.say("Stopping the bot (it finishes the current step first)...")
        if in_window.running:
            in_window.stop()
        service.stop_background()
        if not stop_only:
            start(load_config(config_path))

    start(cfg)
    try:
        return _menu(config_path, in_window, start, restart)
    except KeyboardInterrupt:
        if in_window.running:
            ui.say("\nStopping the bot...")
            in_window.stop()
        return 130


def _menu(config_path: Path, in_window: InWindowBot, start, restart) -> int:
    cfg = load_config(config_path)
    while True:
        try:
            cfg = load_config(config_path)
        except ConfigError as exc:  # config.yaml edited by hand while the menu is open
            ui.say(ui.red(f"Problem in {config_path.name}: {exc} (using the previous settings until it's fixed)"))
            ui.pause()
        running = in_window.running or service.is_running()
        ui.banner(f"v{__version__}")
        for line in _status_lines(cfg, running, in_window.running):
            ui.say(line)
        ui.say(ui.dim("-" * ui.WIDTH))
        failed = sum(1 for e in _load_state(cfg).get("clips", {}).values() if e.get("status") == "failed")
        items = [
            ("1", "Open the gameplay folder"),
            ("2", "Open the clips folder"),
            ("3", "Open the reels folder"),
            ("4", "Watch live activity"),
            ("5", "Connect accounts / auto-posting"),
            ("6", "Settings (run the setup again)"),
            ("7", "Stop the bot" if running else "Start the bot"),
        ]
        if failed:
            items.append(("8", f"Try the {failed} failed clip(s) again"))
        items.append(("0", "Quit (stops the bot)" if in_window.running else "Close this window (the bot keeps running)" if running else "Close"))
        for key, text in items:
            ui.say(f"  {ui.yellow(key)}) {text}")
        choice = ui.ask("\nChoose", "")
        if choice == "1":
            ui.open_folder(cfg.paths.gameplay)
        elif choice == "2":
            ui.open_folder(cfg.paths.clips)
        elif choice == "3":
            ui.open_folder(cfg.paths.output)
        elif choice == "4":
            _watch_log(cfg)
        elif choice in ("5", "6"):
            run_setup(config_path, only_platforms=choice == "5")
            if running:
                restart()
            else:
                ui.pause()
        elif choice == "7":
            if running:
                restart(stop_only=True)
            else:
                start(cfg)
        elif choice == "8" and failed:
            _retry_failed(cfg, restart)
            time.sleep(1)
        elif choice == "0":
            if in_window.running:
                ui.say("Stopping the bot...")
                in_window.stop()
            return 0
