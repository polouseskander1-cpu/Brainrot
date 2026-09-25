"""What you see when you double-click BrainrotBot.exe: the setup the first time, then a small menu."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from . import __version__, service, ui
from .bot import Bot
from .cli import add_file_logging, lower_priority
from .ai import CREDENTIAL_KEY as AI_KEY
from .config import DEFAULT_CONFIG_PATH, SERVER, STOP_FILE, WORK_DIR, ConfigError, load_config, update_config_file
from .credentials import Credentials
from .gameplay import list_media
from .media import AUDIO_EXTS, VIDEO_EXTS, MediaError, Tools, find_tools
from .report import stats_lines
from .uploads import PLATFORMS, platform_name
from .dashboard import dashboard_url, qr_text
from .wizard import add_video_link, choose_look, connect_ai, run_setup, setup_phone
from . import updater

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
    duplicates = sum(1 for e in clips_state.values() if e.get("status") == "duplicate")
    clip_files = list_media(cfg.paths.clips, VIDEO_EXTS)
    waiting = sum(1 for p in clip_files if clips_state.get(p.relative_to(cfg.paths.clips).as_posix(), {}).get("status") not in ("done", "failed", "duplicate"))
    links_waiting = sum(1 for i in state.get("links", {}).values() if i.get("status") == "pending")
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
        f"  Clips:       {cfg.paths.clips}  " + ui.dim(f"({waiting} waiting" + (f", {links_waiting} links to download" if links_waiting else "") + ")"),
        f"  Reels:       {cfg.paths.output}  " + ui.dim(
            f"({done} clips done" + (f", {failed} failed" if failed else "") + (f", {duplicates} duplicates skipped" if duplicates else "") + ")"),
        f"  Music:       {cfg.paths.music}  " + ui.dim(f"({music} tracks, optional)"),
    ]
    creds = Credentials(cfg.paths.credentials)
    ai_on = cfg.ai.enabled and bool(creds.get(AI_KEY))
    lines.append("  AI:          " + (f"Claude ({cfg.ai.model})" if ai_on else ui.dim("off (built-in rules)")))
    uploads = state.get("uploads", {})
    blocked = state.get("upload_blocked", {})
    posting = []
    for key in sorted(creds.data):
        platform = key.split(":")[0]
        if platform not in PLATFORMS or not getattr(cfg.upload, platform, False) or not creds.get(key):
            continue
        mine = [i for i in uploads.values() if i.get("account", i.get("platform")) == key]
        pending = sum(1 for i in mine if i.get("status") in ("pending", "waiting"))
        posted = sum(1 for i in mine if i.get("status") == "done")
        note = ui.red(" needs login!") if key in blocked else ""
        posting.append(f"{platform_name(key)} ({posted} posted, {pending} waiting){note}")
    lines.append("  Posting:     " + (("\n" + " " * 15).join(posting) if posting else ui.dim("off (reels are saved only)")))
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


def _show_dashboard(cfg: SimpleNamespace, config_path: Path, running: bool) -> None:
    ui.banner("phone dashboard")
    if not cfg.dashboard.enabled:
        if not ui.ask_yes_no("The phone dashboard is off. Switch it on?", default=True):
            return
        update_config_file(config_path, {"dashboard": {"enabled": True}})
        cfg = load_config(config_path)
        ui.say(ui.dim("It starts the next time the bot starts (Stop / Start the bot)."))
    url = dashboard_url(cfg, Credentials(cfg.paths.credentials))
    ui.say("Scan this with your phone's camera (the phone must be on the same Wi-Fi as this computer):")
    ui.say()
    qr = qr_text(url)
    if qr:
        print(qr)
    ui.say("  " + ui.yellow(url))
    ui.say()
    if not running:
        ui.say(ui.red("  The bot isn't running, so the page won't open until you start it."))
    ui.say(ui.dim("  Windows may ask to allow Brainrot Bot on private networks: say yes, or the phone can't connect."))
    ui.say(ui.dim("  Anyone with this link can approve your reels; keep it to yourself."))
    ui.pause()


def _update(release, in_window: "InWindowBot", restart) -> int | None:
    """Download and install a new version. Returns the exit code when the app should close."""
    if not updater.can_install():
        ui.say(f"Brainrot Bot {release.version} is out. Update the source code with:  git pull")
        ui.say(ui.dim(release.page))
        ui.pause()
        return None
    ui.say(f"Downloading Brainrot Bot {release.version}...")
    if in_window.running or service.is_running():
        restart(stop_only=True)
    last = [0]

    def progress(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 0
        if percent >= last[0] + 10:
            last[0] = percent - percent % 10
            ui.say(f"  {last[0]}%")

    try:
        new_app = updater.unpack(updater.download(release, progress=progress))
    except Exception as exc:  # noqa: BLE001
        ui.say(ui.red(f"The update didn't work: {exc}"))
        ui.pause()
        return None
    ui.say(ui.green("Installing... the app opens again by itself in a few seconds."))
    updater.start_install(new_app, [sys.executable, *sys.argv[1:]])
    return 0


def _show_stats(cfg: SimpleNamespace) -> None:
    ui.banner("stats")
    for line in stats_lines(_load_state(cfg)):
        ui.say("  " + line)
    ui.say()
    ui.say(ui.dim("  Views and likes are read 2 hours, 1 day, 3 days, 1 week and 1 month after each post."))
    ui.pause()


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
    if SERVER:  # on a server the container runs the bot (docker compose up -d); the setup is all that's needed here
        return 0

    try:
        tools = find_tools(cfg.tools.ffmpeg, cfg.tools.ffprobe, need_subtitles=cfg.captions.enabled or cfg.hook.enabled)
    except MediaError as exc:
        ui.say(ui.red(str(exc)))
        ui.pause()
        return 2

    in_window = InWindowBot()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    updater.clean_up()

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
    found: list = []
    if cfg.app.auto_update != "off":
        threading.Thread(target=lambda: found.append(updater.available_update(force=True)), daemon=True).start()
    try:
        return _menu(config_path, in_window, start, restart, found)
    except KeyboardInterrupt:
        if in_window.running:
            ui.say("\nStopping the bot...")
            in_window.stop()
        return 130


def _menu(config_path: Path, in_window: InWindowBot, start, restart, found: list | None = None) -> int:
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
        release = next((r for r in (found or []) if r is not None), None)
        if release is not None:
            ui.say("  " + ui.green(f"Update:      Brainrot Bot {release.version} is available (you have {__version__})"))
            if cfg.app.auto_update == "auto" and updater.can_install():
                code = _update(release, in_window, restart)
                if code is not None:
                    return code
        ui.say(ui.dim("-" * ui.WIDTH))
        failed = sum(1 for e in _load_state(cfg).get("clips", {}).values() if e.get("status") == "failed")
        creds_now = Credentials(cfg.paths.credentials)
        ai_on = bool(creds_now.get(AI_KEY))
        phone_state = " + ".join(name for key, name in (("telegram", "Telegram"), ("discord", "Discord"))
                                 if getattr(cfg.phone, key) and creds_now.get(key))

        def settings_changed() -> None:
            if running:
                restart()
            else:
                ui.pause()

        def accounts() -> None:
            run_setup(config_path, only_platforms=True)
            settings_changed()

        def ai() -> None:
            ui.banner("AI helper")
            creds = Credentials(cfg.paths.credentials)
            if connect_ai(cfg, creds) and not cfg.ai.enabled:
                update_config_file(config_path, {"ai": {"enabled": True}})
            settings_changed()

        def settings() -> None:
            run_setup(config_path)
            settings_changed()

        def phone() -> None:
            ui.banner("phone")
            creds = Credentials(cfg.paths.credentials)
            changes = setup_phone(cfg, creds, ask_first=False)
            if changes:
                update_config_file(config_path, {"phone": changes})
            settings_changed()

        def toggle() -> None:
            if running:
                restart(stop_only=True)
            else:
                start(cfg)

        def retry() -> None:
            _retry_failed(cfg, restart)
            time.sleep(1)

        def look() -> None:
            choose_look(cfg, config_path)
            settings_changed()

        exit_code: list[int] = []

        def update() -> None:
            code = _update(release, in_window, restart)
            if code is not None:
                exit_code.append(code)

        actions = [
            ("Open the gameplay folder", lambda: ui.open_folder(cfg.paths.gameplay)),
            ("Open the clips folder", lambda: ui.open_folder(cfg.paths.clips)),
            ("Open the reels folder", lambda: ui.open_folder(cfg.paths.output)),
            ("Add a video link (YouTube, TikTok, Instagram...)", lambda: add_video_link(cfg)),
            ("Phone dashboard (scan a QR code)", lambda: _show_dashboard(cfg, config_path, running)),
            ("Watch live activity", lambda: _watch_log(cfg)),
            ("Stats: views, likes, best podcasts and gameplay", lambda: _show_stats(cfg)),
            ("Look & features: caption style, layout, zooms, emojis...", look),
            ("Connect accounts / auto-posting", accounts),
            ("AI helper (Claude): " + ("connected" if ai_on else "connect"), ai),
            ("Phone (Telegram / Discord): " + (phone_state or "connect"), phone),
            ("Settings (run the setup again)", settings),
            ("Stop the bot" if running else "Start the bot", toggle),
        ]
        if failed:
            actions.append((f"Try the {failed} failed clip(s) again", retry))
        if release is not None:
            actions.append((ui.green(f"Update to Brainrot Bot {release.version}"), update))
        for number, (text, _) in enumerate(actions, 1):
            ui.say(f"  {ui.yellow(str(number))}) {text}")
        quit_text = "Quit (stops the bot)" if in_window.running else "Close this window (the bot keeps running)" if running else "Close"
        ui.say(f"  {ui.yellow('0')}) {quit_text}")
        choice = ui.ask("\nChoose", "")
        if choice == "0":
            if in_window.running:
                ui.say("Stopping the bot...")
                in_window.stop()
            return 0
        if choice.isdigit() and 1 <= int(choice) <= len(actions):
            actions[int(choice) - 1][1]()
            if exit_code:
                return exit_code[0]
