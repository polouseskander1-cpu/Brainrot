"""Rendering on the graphics card: video.codec: auto tries the NVIDIA, Intel and AMD encoders (or Apple's
on a Mac) with a one-second test and uses the first one that really works, else the CPU (libx264)."""

from __future__ import annotations

import logging
import subprocess
import sys
from types import SimpleNamespace

from .media import Tools, popen_kwargs

log = logging.getLogger("brainrot")

NAMES = {
    "h264_nvenc": "NVIDIA graphics card",
    "h264_qsv": "Intel graphics (Quick Sync)",
    "h264_amf": "AMD graphics card",
    "h264_videotoolbox": "Mac video encoder",
    "libx264": "processor (CPU)",
}


def candidates() -> list[str]:
    if sys.platform == "darwin":
        return ["h264_videotoolbox"]
    if sys.platform.startswith("win"):
        return ["h264_nvenc", "h264_qsv", "h264_amf"]
    return ["h264_nvenc", "h264_qsv"]


def pixel_format(codec: str) -> str:
    return "nv12" if codec.endswith("_qsv") else "yuv420p"


def works(tools: Tools, codec: str, video: SimpleNamespace) -> bool:
    """Encode one second of black video with the same settings the reels use."""
    from .render import encoder_args

    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=1",
           *encoder_args(codec, video), "-pix_fmt", pixel_format(codec), "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, **popen_kwargs())
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def pick_codec(tools: Tools, video: SimpleNamespace) -> str:
    """The codec to render with: the configured one, or for 'auto' the fastest one that works here."""
    if video.codec != "auto":
        return video.codec
    for codec in candidates():
        if codec in tools.encoders and works(tools, codec, video):
            return codec
    return "libx264"


def setup_codec(cfg: SimpleNamespace, tools: Tools) -> str:
    codec = pick_codec(tools, cfg.video)
    cfg.video.resolved_codec = codec
    if cfg.video.codec == "auto":
        log.info("Rendering with the %s (%s).", NAMES.get(codec, codec), codec)
    return codec


def available(tools: Tools, video: SimpleNamespace) -> list[str]:
    """Graphics card encoders that work on this computer (for the menu)."""
    return [c for c in candidates() if c in tools.encoders and works(tools, c, video)]
