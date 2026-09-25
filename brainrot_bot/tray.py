"""A small icon next to the clock while the bot runs in the background (Windows): open the app, the
phone dashboard or the reels folder, pause posting, or stop the bot."""

from __future__ import annotations

import logging
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from .config import RESOURCE_DIR

log = logging.getLogger("brainrot")


def icon_image(size: int = 64):
    """The app icon if it's bundled, otherwise a yellow badge drawn on the spot."""
    from PIL import Image, ImageDraw

    bundled = RESOURCE_DIR / "packaging" / "icon.ico"
    if bundled.exists():
        try:
            return Image.open(bundled).convert("RGBA").resize((size, size))
        except OSError:
            pass
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=(255, 212, 0, 255), outline=(20, 20, 20, 255), width=3)
    draw.rectangle((size * 0.3, size * 0.22, size * 0.42, size * 0.78), fill=(20, 20, 20, 255))
    draw.ellipse((size * 0.3, size * 0.22, size * 0.72, size * 0.5), fill=(20, 20, 20, 255))
    draw.ellipse((size * 0.3, size * 0.48, size * 0.76, size * 0.78), fill=(20, 20, 20, 255))
    return image


def start_tray(cfg: SimpleNamespace, *, dashboard_url: Callable[[], str], open_app: Callable[[], None], open_folder: Callable[[Path], None],
               is_paused: Callable[[], bool], toggle_pause: Callable[[], None], stop: Callable[[], None]):
    """Show the icon; returns it (or None when there's no tray: not Windows, switched off, or missing)."""
    if not cfg.app.tray or not sys.platform.startswith("win"):
        return None
    try:
        import pystray
    except Exception as exc:  # noqa: BLE001 - pystray/Pillow missing or no desktop
        log.debug("No tray icon: %s", exc)
        return None

    def safely(action: Callable[[], None]) -> Callable:
        def run(icon=None, item=None) -> None:
            try:
                action()
            except Exception:  # noqa: BLE001
                log.exception("Tray menu problem")
        return run

    def quit_bot(icon, item) -> None:
        icon.visible = False
        stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Brainrot Bot is running", None, enabled=False),
        pystray.MenuItem("Open the app", safely(open_app), default=True),
        pystray.MenuItem("Phone dashboard", safely(lambda: webbrowser.open(dashboard_url())), visible=cfg.dashboard.enabled),
        pystray.MenuItem("Open the reels folder", safely(lambda: open_folder(cfg.paths.output))),
        pystray.MenuItem(lambda item: "Resume posting" if is_paused() else "Pause posting", safely(toggle_pause)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop the bot", quit_bot),
    )
    try:
        icon = pystray.Icon("brainrot-bot", icon_image(), "Brainrot Bot", menu)
        icon.run_detached()
    except Exception as exc:  # noqa: BLE001
        log.debug("No tray icon: %s", exc)
        return None
    return icon
