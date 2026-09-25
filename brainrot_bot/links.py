"""Video links instead of files: put YouTube / TikTok / Instagram / X links in a links.txt file inside
a clips folder (one per line), or add one from the menu, and the bot downloads each video into that
folder, where it becomes a reel like any other clip.

Downloads use yt-dlp. YouTube also needs a JavaScript runtime (Deno) to download; the Windows app
includes one. Sites that need a login (often Instagram) work with a cookies.txt file next to config.yaml.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from .config import APP_DIR, RESOURCE_DIR
from .media import StopRequested, Tools

log = logging.getLogger("brainrot")

LINKS_FILE = "links.txt"
TEMP_DIR = ".downloading"  # inside the clip folder, so the finished file is moved in one step
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
BACKOFF_MINUTES = [5, 30, 180]


class LinkError(Exception):
    pass


def read_links(path: Path) -> list[str]:
    """Links in a links.txt (anything after # is a comment)."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    links = []
    for line in text.splitlines():
        line = line.split(" #")[0].strip()
        if line.startswith("#"):
            continue
        match = URL_RE.search(line)
        if match:
            links.append(match.group(0).rstrip(").,;'\">"))
    return links


def add_link(folder: Path, url: str) -> Path:
    """Append a link to folder/links.txt (made if needed)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LINKS_FILE
    existing = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else ""
    with open(path, "a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(url.strip() + "\n")
    return path


def find_link_files(clips_root: Path) -> list[Path]:
    if not clips_root.is_dir():
        return []
    found = []
    for path in clips_root.rglob(LINKS_FILE):
        rel = path.relative_to(clips_root).parts
        if not any(part.startswith((".", "_", "~")) for part in rel):
            found.append(path)
    return sorted(found)


def js_runtime() -> dict | None:
    """yt-dlp needs a JavaScript runtime for YouTube: the bundled Deno, or Deno / Node.js on this computer."""
    exe = "deno.exe" if os.name == "nt" else "deno"
    for candidate in (APP_DIR / "deno" / exe, RESOURCE_DIR / "deno" / exe):
        if candidate.is_file():
            return {"deno": {"path": str(candidate)}}
    if shutil.which("deno"):
        return {"deno": {}}
    node = shutil.which("node")
    if node:
        return {"node": {"path": node}}
    return None


class _Logger:
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        self.errors.append(re.sub(r"^ERROR:\s*", "", str(msg)).strip())


def download(url: str, folder: Path, tools: Tools, cfg: SimpleNamespace, should_stop: Callable[[], bool] = lambda: False) -> list[Path]:
    """Download the video(s) behind a link into folder. Returns the new files."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise LinkError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / TEMP_DIR
    temp.mkdir(exist_ok=True)
    if os.name == "nt":
        try:  # hide the temporary folder in Explorer
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(temp), 0x02)
        except Exception:  # noqa: BLE001
            pass
    files: list[Path] = []
    last_report = [0.0]

    def progress(info: dict) -> None:
        if should_stop():
            raise StopRequested()
        if info.get("status") == "downloading" and time.monotonic() - last_report[0] > 30:
            last_report[0] = time.monotonic()
            done, total = info.get("downloaded_bytes") or 0, info.get("total_bytes") or info.get("total_bytes_estimate")
            if total:
                log.info("  downloading: %d%% of %.0f MB", 100 * done / total, total / 1e6)

    def finished(path: str) -> None:
        files.append(Path(path))

    height = int(cfg.links.max_height)
    logger = _Logger()
    options = {
        "format": f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": {"default": "%(title).90B [%(id)s].%(ext)s"},
        "paths": {"home": str(folder), "temp": str(temp)},
        "windowsfilenames": True,
        "noplaylist": True,
        "playlistend": int(cfg.links.playlist_limit),
        "ffmpeg_location": str(Path(tools.ffmpeg).parent) if Path(tools.ffmpeg).is_file() else None,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": logger,
        "progress_hooks": [progress],
        "post_hooks": [finished],
        "retries": 5,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "overwrites": False,
    }
    runtime = js_runtime()
    if runtime:
        options["js_runtimes"] = runtime
    cookies = cookies_file(cfg)
    if cookies:
        options["cookiefile"] = str(cookies)
    options = {k: v for k, v in options.items() if v is not None}

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except StopRequested:
        raise
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises DownloadError and friends
        if should_stop():
            raise StopRequested() from exc
        message = logger.errors[-1] if logger.errors else str(exc)
        raise LinkError(explain(message, url, runtime is not None)) from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return [f for f in files if f.exists()]


def cookies_file(cfg: SimpleNamespace) -> Path | None:
    name = str(cfg.links.cookies_file or "").strip()
    candidates = [Path(name).expanduser()] if name else []
    candidates.append(Path(cfg.config_path).resolve().parent / "cookies.txt")
    for path in candidates:
        if not path.is_absolute():
            path = Path(cfg.config_path).resolve().parent / path
        if path.is_file():
            return path
    return None


def explain(message: str, url: str, has_runtime: bool) -> str:
    """yt-dlp's error in plain words, with what to do about it."""
    text = " ".join(message.split())[:300]
    lower = text.lower()
    if ("youtube" in url or "youtu.be" in url) and not has_runtime and ("javascript" in lower or "n challenge" in lower or "signature" in lower):
        return f"{text} (YouTube needs Deno: install it from deno.com, then restart the bot)"
    if "login" in lower or "sign in" in lower or "cookies" in lower or "private" in lower or "age" in lower:
        return f"{text} (this video needs a login: save your browser cookies as cookies.txt next to config.yaml)"
    if "unsupported url" in lower:
        return f"{text} (this site isn't supported)"
    return text


# ------------------------------------------------------------------ the queue of links


class LinkQueue:
    """Remembers every link (bot_state.json 'links') so each is downloaded once."""

    def __init__(self, cfg: SimpleNamespace, state, tools: Tools, save: Callable[[], None]):
        self.cfg = cfg
        self.state = state
        self.tools = tools
        self.save = save

    @property
    def items(self) -> dict:
        return self.state.data.setdefault("links", {})

    def pending(self) -> list[tuple[str, str, Path]]:
        """(state key, url, folder) of links that still need downloading."""
        root = self.cfg.paths.clips
        todo = []
        now = time.time()
        for path in find_link_files(root):
            folder = path.parent
            rel = folder.relative_to(root).as_posix() if folder != root else ""
            for url in read_links(path):
                key = f"{rel}|{url}"
                item = self.items.get(key)
                if item is None:
                    item = self.items[key] = {"url": url, "folder": rel, "status": "pending", "attempts": 0, "added": now}
                if item["status"] == "pending" and item.get("next_try", 0) <= now:
                    todo.append((key, url, folder))
        return todo

    def run(self, should_stop: Callable[[], bool]) -> int:
        """Download every pending link. Returns how many videos were downloaded."""
        if not self.cfg.links.enabled:
            return 0
        count = 0
        for key, url, folder in self.pending():
            if should_stop():
                break
            count += self.download_one(key, url, folder, should_stop)
        return count

    def download_one(self, key: str, url: str, folder: Path, should_stop: Callable[[], bool]) -> int:
        item = self.items[key]
        log.info("Downloading %s ...", url)
        try:
            files = download(url, folder, self.tools, self.cfg, should_stop)
        except StopRequested:
            raise
        except LinkError as exc:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["error"] = str(exc)
            if item["attempts"] > len(BACKOFF_MINUTES):
                item["status"] = "failed"
                log.error("Couldn't download %s: %s", url, exc)
            else:
                minutes = BACKOFF_MINUTES[item["attempts"] - 1]
                item["next_try"] = time.time() + minutes * 60
                log.warning("Couldn't download %s (%s). Trying again in %d min.", url, exc, minutes)
            self.save()
            return 0
        item.update(status="done", files=[f.name for f in files], updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        item.pop("error", None)
        self.save()
        if files:
            log.info("Downloaded %s", ", ".join(f.name for f in files))
        else:
            log.info("Nothing new to download from %s (already there?)", url)
        return len(files)

    def retry_failed(self) -> int:
        count = 0
        for item in self.items.values():
            if item["status"] == "failed":
                item.update(status="pending", attempts=0, next_try=0)
                count += 1
        return count
