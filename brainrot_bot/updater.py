"""Updates from the GitHub releases page.

The app checks once a day. With app.auto_update: ask (default) the menu offers the new version;
with auto the background bot downloads it, checks it, and installs it the next time it's idle
(then starts again by itself); off never checks. Settings, logins, clips and reels are never touched:
only the program files are replaced. Running from the source code, it just says to git pull.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .config import APP_DIR, FROZEN
from .uploads import http

log = logging.getLogger("brainrot")

REPO = "polouseskander1-cpu/Brainrot"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "BrainrotBot-windows.zip"
UPDATE_DIR = APP_DIR / ".update"
CHECK_EVERY = 24 * 3600


@dataclass
class Release:
    version: str
    url: str
    size: int
    digest: str  # "sha256:..." or ""
    notes: str
    page: str


def parse_version(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", text.split("-")[0])
    return tuple(int(n) for n in numbers[:3]) or (0,)


def newer(version: str, than: str = __version__) -> bool:
    return parse_version(version) > parse_version(than)


def latest_release(timeout: float = 15) -> Release | None:
    """The newest published release, or None (no internet, rate limited, nothing there)."""
    try:
        resp = http.request("GET", LATEST, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    except http.UploadError:
        return None
    if not resp.ok:
        return None
    data = resp.json()
    if data.get("draft") or data.get("prerelease") or not data.get("tag_name"):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET), None)
    return Release(
        version=str(data["tag_name"]).lstrip("v"),
        url=asset.get("browser_download_url", "") if asset else "",
        size=int(asset.get("size", 0)) if asset else 0,
        digest=str(asset.get("digest") or "") if asset else "",
        notes=str(data.get("body") or "")[:2000],
        page=str(data.get("html_url") or f"https://github.com/{REPO}/releases/latest"),
    )


def available_update(state: dict | None = None, force: bool = False) -> Release | None:
    """A newer release, checking GitHub at most once a day (the answer is remembered in state)."""
    memory = state.setdefault("update", {}) if state is not None else {}
    if not force and time.time() - float(memory.get("checked", 0)) < CHECK_EVERY:
        found = memory.get("release")
        return Release(**found) if found and newer(found["version"]) else None
    release = latest_release()
    memory["checked"] = time.time()
    memory["release"] = release.__dict__ if release else None
    return release if release is not None and newer(release.version) else None


def download(release: Release, folder: Path = UPDATE_DIR, progress: Callable[[int, int], None] | None = None) -> Path:
    """Download the new app and check it arrived complete and unchanged."""
    if not release.url:
        raise RuntimeError("this release has no Windows download")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ASSET
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(release.url, headers={"User-Agent": http.USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as resp, open(partial, "wb") as out:  # noqa: S310 - GitHub https URL
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, release.size)
    if release.size and done != release.size:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"the download was incomplete ({done} of {release.size} bytes)")
    if release.digest.startswith("sha256:") and digest.hexdigest() != release.digest.split(":", 1)[1].lower():
        partial.unlink(missing_ok=True)
        raise RuntimeError("the download doesn't match GitHub's checksum")
    os.replace(partial, target)
    return target


def unpack(zip_path: Path, folder: Path = UPDATE_DIR) -> Path:
    """Extract the zip; returns the folder that holds BrainrotBot.exe."""
    new = folder / "new"
    shutil.rmtree(new, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():  # never write outside the update folder
            if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                raise RuntimeError(f"unexpected file in the update: {member}")
        archive.extractall(new)
    exe = next(new.rglob("BrainrotBot.exe"), None)
    if exe is None:
        raise RuntimeError("the update has no BrainrotBot.exe")
    return exe.parent


def install_script(new_app: Path, app_dir: Path, pid: int, restart: list[str]) -> str:
    """A Windows batch file: wait for this program to close, copy the new files over, start again."""
    command = subprocess.list2cmdline(restart)
    return "\r\n".join([
        "@echo off",
        "title Updating Brainrot Bot",
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)',
        "timeout /t 2 /nobreak >nul",
        f'cd /d "{app_dir}"',
        'if exist "_internal.old" rmdir /s /q "_internal.old"',
        'if exist "_internal" move "_internal" "_internal.old" >nul',  # kept until the new files are in place
        f'robocopy "{new_app}" "{app_dir}" /E /R:10 /W:2 /NFL /NDL /NJH /NJS /NP >nul',
        "if %ERRORLEVEL% GEQ 8 (",
        '  if exist "_internal.old" (rmdir /s /q "_internal" 2>nul & move "_internal.old" "_internal" >nul)',
        "  echo The update could not be copied; the old version was kept. & pause",
        ") else (",
        '  rmdir /s /q "_internal.old" 2>nul',
        ")",
        f'start "" {command}',
        "exit",
    ]) + "\r\n"


def start_install(new_app: Path, restart: list[str]) -> None:
    """Hand over to the batch file; the caller must exit right after this."""
    script = UPDATE_DIR / "install.bat"
    script.write_text(install_script(new_app, APP_DIR, os.getpid(), restart), encoding="utf-8")
    flags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
    subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=flags, close_fds=True)


def can_install() -> bool:
    return FROZEN and sys.platform.startswith("win")


def other_windows_open() -> bool:
    """Is the app window (BrainrotBot.exe) open? Its files can't be replaced while it runs."""
    if not sys.platform.startswith("win"):
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BrainrotBot.exe", "/NH"], capture_output=True, text=True,
                             timeout=20, creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "BrainrotBot.exe" in out


def clean_up() -> None:
    """Remove what's left of an installed update (done at start)."""
    shutil.rmtree(UPDATE_DIR, ignore_errors=True)
