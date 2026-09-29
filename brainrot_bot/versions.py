"""A version of every reel for each platform, each fitted to what that platform takes and pays for.

TikTok's Creator Rewards only pay for videos of one minute or longer. YouTube Shorts over one minute that
carry a copyright claim can't earn. Facebook takes reels of 3-90 seconds from apps. So each reel is cut to
(at most) a couple of lengths - typically a long one of 1-1.5 minutes and a short one under a minute - and
each platform gets the longest one that fits its range, in its own folder: Reels/TikTok/<podcast>/...
Lengths are what's left after pauses are cut, so a platform's limit is never missed by a second.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

from .transcribe import Word

FOLDERS = {"tiktok": "TikTok", "youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "x": "X",
           "pinterest": "Pinterest"}
WHY = {"tiktok": "TikTok only pays for videos of 1 minute or longer"}
_RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$")
OFF = ("", "off", "no", "none", "false")


def parse_range(name: str, value) -> tuple[float, float] | None:
    """'61-90' -> (61.0, 90.0); '' or 'off' -> None."""
    if value is None or value is False or (isinstance(value, str) and value.strip().lower() in OFF):
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        lo, hi = float(value[0]), float(value[1])
    else:
        match = _RANGE_RE.match(str(value))
        if not match:
            raise ValueError(f"'{name}' must be a length range in seconds like 61-90 (or off)")
        lo, hi = float(match.group(1)), float(match.group(2))
    if not (1 <= lo and lo + 5 <= hi <= 900):
        raise ValueError(f"'{name}' must be like 61-90: at least 1 second, the second number at least 5 more, at most 900")
    return lo, hi


def ranges(cfg: SimpleNamespace) -> dict[str, tuple[float, float]]:
    """The platforms that get their own version, and the length (seconds) each one allows."""
    v = getattr(cfg, "versions", None)
    if v is None or not v.enabled:
        return {}
    found = {}
    for platform in FOLDERS:
        r = parse_range(f"versions.{platform}", getattr(v, platform, ""))
        if r is not None:
            found[platform] = r
    return found


def moment_range(cfg: SimpleNamespace) -> tuple[float, float]:
    """How long the best moments of a long video should be: long enough for the longest version."""
    m = cfg.moments
    r = ranges(cfg)
    if not r:
        return m.min_seconds, m.max_seconds
    longest_min = max(lo for lo, _ in r.values())
    longest_max = max(hi for _, hi in r.values())
    # A little extra so the version still makes it after pauses are cut.
    return max(m.min_seconds, min(longest_min * 1.08, longest_max - 5)), max(m.max_seconds, longest_max)


@dataclass
class Cut:
    """One render: a part of the piece, and the platforms it's for."""

    start: float
    end: float
    length: float  # after pauses are cut
    platforms: list[str] = field(default_factory=list)


def plan_cuts(
    start: float,
    end: float,
    words: list[Word],
    targets: dict[str, tuple[float, float]],
    edited_length: Callable[[float, float], float],
    best_windows: Callable[[float, float, float, float], list[tuple[float, float]]],
) -> tuple[list[Cut], dict[str, float]]:
    """Which part of the piece (start-end) each platform gets.

    edited_length(a, b): the reel's length after pauses are cut.
    best_windows(a, b, min_len, max_len): the best parts of a..b of about that length, best first.
    Returns the cuts (longest first) and the platforms that got none, with the piece's length.
    """
    full = edited_length(start, end)
    cuts: list[Cut] = []
    left_out: dict[str, float] = {}
    # The platform that needs the longest version goes first, so shorter versions come out of it.
    for platform, (lo, hi) in sorted(targets.items(), key=lambda t: (-t[1][0], -t[1][1])):
        reuse = [c for c in cuts if lo <= c.length <= hi]
        if reuse:
            max(reuse, key=lambda c: c.length).platforms.append(platform)
            continue
        if lo <= full <= hi:
            cuts.append(Cut(start, end, full, [platform]))
            continue
        if full < lo:
            left_out[platform] = full
            continue
        found = None
        # A shorter version of the longest one made so far (the same moment), else of the whole piece.
        spans = [(c.start, c.end) for c in sorted(cuts, key=lambda c: -c.length)] + [(start, end)]
        for a, b in spans:
            for wa, wb in best_windows(a, b, lo, hi):
                length = edited_length(wa, wb)
                if lo <= length <= hi:
                    found = Cut(wa, wb, length, [platform])
                    break
            if found:
                break
        if found is None:
            left_out[platform] = full
        else:
            cuts.append(found)
    cuts.sort(key=lambda c: -c.length)
    return cuts, left_out
