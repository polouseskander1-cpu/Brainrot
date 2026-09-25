"""Cloud folders (Google Drive, Dropbox, OneDrive...).

On your computer the easy way is to keep the bot's folders inside the Google Drive / Dropbox / OneDrive
folder that their desktop apps sync: drop a clip in from your phone's Drive app and it becomes a reel,
and the reels show up in the app. The setup finds those folders for you.

On a server (no desktop apps) the bot can sync through rclone instead: set cloud.remote to an rclone
remote such as "gdrive:Brainrot"; every few minutes it copies <remote>/Clips and <remote>/Gameplay down
and the finished reels up to <remote>/Reels.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from .config import APP_DIR
from .media import popen_kwargs

log = logging.getLogger("brainrot")


def synced_folders(home: Path | None = None) -> dict[str, Path]:
    """Folders that Google Drive / Dropbox / OneDrive keep in sync on this computer."""
    home = home or Path.home()
    found: dict[str, Path] = {}
    # Google Drive for desktop: a drive letter with "My Drive" (Windows), CloudStorage (Mac).
    candidates = [home / "My Drive", home / "Google Drive"]
    if sys.platform.startswith("win"):
        candidates += [Path(f"{letter}:\\My Drive") for letter in string.ascii_uppercase[3:]]
    candidates += sorted((home / "Library" / "CloudStorage").glob("GoogleDrive-*/My Drive"))
    for path in candidates:
        if _is_dir(path):
            found["Google Drive"] = path
            break
    # Dropbox writes where its folder is.
    for info in (Path(os.environ.get("LOCALAPPDATA", "")) / "Dropbox" / "info.json", home / ".dropbox" / "info.json"):
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for kind in ("personal", "business"):
            path = Path(str((data.get(kind) or {}).get("path", "")))
            if str(path) not in ("", ".") and _is_dir(path):
                found.setdefault("Dropbox", path)
    if "Dropbox" not in found and _is_dir(home / "Dropbox"):
        found["Dropbox"] = home / "Dropbox"
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    candidates = [Path(onedrive)] if onedrive else []
    candidates += sorted((home / "Library" / "CloudStorage").glob("OneDrive*"))
    for path in candidates:
        if _is_dir(path):
            found["OneDrive"] = path
            break
    return found


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


# ------------------------------------------------------------------ rclone (servers)


def rclone_path() -> str | None:
    exe = "rclone.exe" if os.name == "nt" else "rclone"
    bundled = APP_DIR / "rclone" / exe
    return str(bundled) if bundled.is_file() else shutil.which("rclone")


class CloudSync:
    def __init__(self, cfg: SimpleNamespace):
        self.cfg = cfg
        self.last = 0.0
        self.warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.cloud.remote)

    def due(self) -> bool:
        return self.enabled and time.time() - self.last >= self.cfg.cloud.every_minutes * 60

    def jobs(self) -> list[tuple[str, str]]:
        remote = self.cfg.cloud.remote.rstrip("/")
        p = self.cfg.paths
        return [(f"{remote}/Clips", str(p.clips)), (f"{remote}/Gameplay", str(p.gameplay)), (str(p.output), f"{remote}/Reels")]

    def run(self) -> bool:
        """Pull new clips and gameplay, push new reels. Returns False if rclone isn't there or failed."""
        if not self.due():
            return True
        self.last = time.time()
        rclone = rclone_path()
        if rclone is None:
            if not self.warned:
                log.warning("cloud.remote is set, but rclone isn't installed (rclone.org/install). Cloud sync is off.")
                self.warned = True
            return False
        ok = True
        for source, target in self.jobs():
            # --ignore-existing: never overwrite, --exclude hidden/partial files still being written
            cmd = [rclone, "copy", source, target, "--ignore-existing", "--exclude", ".*", "--exclude", "*.partial",
                   "--exclude", "*.part", "--transfers", "2", "--retries", "3"]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600, **popen_kwargs())
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("Cloud sync failed: %s", exc)
                return False
            if result.returncode != 0:
                ok = False
                log.warning("Cloud sync of %s failed: %s", source, (result.stderr or "").strip().splitlines()[-1:] or result.returncode)
        return ok
