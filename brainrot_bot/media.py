"""Everything that talks to ffmpeg / ffprobe."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import APP_DIR

log = logging.getLogger("brainrot")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".opus", ".wma"}

# If ffmpeg has not reported any progress for this long, it is considered stuck.
STALL_TIMEOUT = 15 * 60


class MediaError(Exception):
    """ffmpeg/ffprobe failed or a file is unusable."""


def popen_kwargs() -> dict:
    """Keep ffmpeg from flashing console windows when the bot runs in the background on Windows."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


@dataclass
class Tools:
    ffmpeg: str
    ffprobe: str
    filters: set = field(default_factory=set)
    encoders: set = field(default_factory=set)
    version: str = ""


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int = 0  # as displayed (rotation and pixel aspect applied)
    height: int = 0
    has_video: bool = False
    has_audio: bool = False


# ---------------------------------------------------------------- finding ffmpeg


def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def _ffmpeg_candidates(configured: str) -> list[str]:
    found: list[str] = []
    if configured:
        found.append(configured)
    # A portable ffmpeg dropped next to the bot wins over the system one.
    for local in (APP_DIR / "ffmpeg" / "bin" / _exe("ffmpeg"), APP_DIR / "ffmpeg" / _exe("ffmpeg"), APP_DIR / _exe("ffmpeg")):
        if local.is_file():
            found.append(str(local))
    on_path = shutil.which("ffmpeg")
    if on_path:
        found.append(on_path)
    if sys.platform == "darwin":
        # Homebrew's plain "ffmpeg" has no subtitle renderer; "ffmpeg-full" does but is not on PATH.
        found += ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg"]
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            found.append(os.path.join(local_appdata, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
        found += [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]
    unique: list[str] = []
    for item in found:
        if item and os.path.isfile(item) and item not in unique:
            unique.append(item)
    return unique


def _capture(cmd: list[str], timeout: float = 60) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **popen_kwargs())
    return proc.stdout + proc.stderr


def _list_filters(ffmpeg: str) -> set[str]:
    names = set()
    for line in _capture([ffmpeg, "-hide_banner", "-filters"]).splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"[TSCA.|]{2,4}", parts[0]):
            names.add(parts[1])
    return names


def _list_encoders(ffmpeg: str) -> set[str]:
    names = set()
    for line in _capture([ffmpeg, "-hide_banner", "-encoders"]).splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[VAS][A-Z.]{5}", parts[0]):
            names.add(parts[1])
    return names


def find_tools(configured_ffmpeg: str = "", configured_ffprobe: str = "", need_subtitles: bool = True) -> Tools:
    """Locate ffmpeg + ffprobe, preferring a build that can draw subtitles (libass)."""
    candidates = _ffmpeg_candidates(configured_ffmpeg)
    if not candidates:
        raise MediaError("ffmpeg was not found. Install it (see README 'Install') or set tools.ffmpeg in config.yaml.")

    chosen = None
    for ffmpeg in candidates:
        try:
            filters = _list_filters(ffmpeg)
        except (OSError, subprocess.SubprocessError):
            continue
        if chosen is None or (need_subtitles and "ass" in filters and "ass" not in chosen[1]):
            chosen = (ffmpeg, filters)
        if not need_subtitles or "ass" in filters:
            break
    if chosen is None:
        raise MediaError(f"ffmpeg could not be started ({candidates[0]}).")
    ffmpeg, filters = chosen

    ffprobe = configured_ffprobe
    if not ffprobe:
        sibling = Path(ffmpeg).with_name(_exe("ffprobe"))
        ffprobe = str(sibling) if sibling.is_file() else (shutil.which("ffprobe") or "")
    if not ffprobe or not os.path.isfile(ffprobe):
        raise MediaError("ffprobe was not found (it ships with ffmpeg). Set tools.ffprobe in config.yaml.")

    first_line = _capture([ffmpeg, "-hide_banner", "-version"]).splitlines()
    return Tools(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        filters=filters,
        encoders=_list_encoders(ffmpeg),
        version=first_line[0] if first_line else "",
    )


# ---------------------------------------------------------------- probing


def _float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number > 0 else 0.0  # drop NaN / negatives


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                pass
    try:
        return int((stream.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0


def _display_size(stream: dict) -> tuple[int, int]:
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    sar = str(stream.get("sample_aspect_ratio") or "1:1")
    match = re.fullmatch(r"(\d+):(\d+)", sar)
    if match and int(match.group(1)) > 0 and int(match.group(2)) > 0:
        width = round(width * int(match.group(1)) / int(match.group(2)))
    if abs(_rotation(stream)) % 180 == 90:
        width, height = height, width
    return width, height


def _measure_duration(tools: Tools, path: Path) -> float:
    """Slow path for files whose header has no duration: read through the whole file."""
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy", "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, **popen_kwargs()).stdout
    except subprocess.SubprocessError:
        return 0.0
    values = re.findall(r"out_time_(?:us|ms)=(\d+)", out)
    return int(values[-1]) / 1_000_000 if values else 0.0


def probe(tools: Tools, path: Path) -> MediaInfo:
    cmd = [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, **popen_kwargs())
    except subprocess.SubprocessError as exc:
        raise MediaError(f"ffprobe timed out on {path.name}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["unknown error"])[-1].replace(f"{path}: ", "")
        raise MediaError(f"Can't read {path.name}: {detail}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"Can't read {path.name}: bad ffprobe output") from exc

    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _float((data.get("format") or {}).get("duration"))
    if not duration:
        duration = max([_float(s.get("duration")) for s in streams] or [0.0])
    if not duration:
        duration = _measure_duration(tools, path)

    info = MediaInfo(path=path, duration=duration, has_video=video is not None, has_audio=audio is not None)
    if video is not None:
        info.width, info.height = _display_size(video)
    return info


# ---------------------------------------------------------------- running ffmpeg


def _tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    output = [line for line in text.strip().splitlines() if line.strip() and not line.startswith("COMMAND: ")]
    return "\n".join(output[-lines:])


def run_ffmpeg(
    tools: Tools,
    args: list[str],
    *,
    log_path: Path,
    cwd: Path | None = None,
    duration: float = 0.0,
    label: str = "",
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Run ffmpeg, log progress every ~10%, raise MediaError with ffmpeg's own error text on failure."""
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *args]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8", errors="replace") as err_file:
        err_file.write("COMMAND: " + subprocess.list2cmdline(cmd) + "\n\n")
        err_file.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=err_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs(),
        )
        last_activity = [time.monotonic()]
        stalled = threading.Event()

        def watchdog() -> None:
            while proc.poll() is None:
                if time.monotonic() - last_activity[0] > STALL_TIMEOUT:
                    stalled.set()
                    proc.kill()
                    return
                time.sleep(2)

        threading.Thread(target=watchdog, daemon=True).start()

        next_report = 10
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                last_activity[0] = time.monotonic()
                key, _, value = line.strip().partition("=")
                if key not in ("out_time_us", "out_time_ms") or not value.isdigit() or duration <= 0:
                    continue
                pct = min(100.0, int(value) / 1_000_000 / duration * 100)
                if on_progress:
                    on_progress(pct)
                if label and pct >= next_report and pct < 100:
                    log.info("%s %d%%", label, int(pct))
                    next_report = (int(pct) // 10 + 1) * 10
            code = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise

    if stalled.is_set():
        raise MediaError(f"ffmpeg stopped making progress for {STALL_TIMEOUT // 60} minutes and was stopped")
    if code != 0:
        detail = _tail(log_path) or f"exit code {code}"
        raise MediaError(f"ffmpeg failed:\n{detail}")
