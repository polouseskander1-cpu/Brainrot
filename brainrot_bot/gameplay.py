"""Pick random gameplay footage that lasts exactly as long as the clip."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

from .media import MediaError, Tools, probe

log = logging.getLogger("brainrot")

# Ask ffmpeg for a little more gameplay than needed so rounding never leaves the last frames empty.
SAFETY = 0.25
# Recordings shorter than this are ignored.
MIN_USEFUL = 1.0


@dataclass
class Footage:
    path: Path
    key: str  # path relative to its folder, used for the usage counters
    duration: float


@dataclass
class Segment:
    path: Path
    key: str
    start: float
    duration: float


def list_media(folder: Path, extensions: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    files = []
    for path in folder.rglob("*"):
        rel_parts = path.relative_to(folder).parts
        if any(part.startswith((".", "_", "~")) for part in rel_parts):
            continue
        if path.suffix.lower() in extensions and path.is_file():
            files.append(path)
    return sorted(files)


def scan_library(folder: Path, extensions: set[str], tools: Tools, cache: dict) -> list[Footage]:
    """Durations are cached (by size + modified time) so big folders are only probed once."""
    library: list[Footage] = []
    seen = set()
    for path in list_media(folder, extensions):
        key = path.relative_to(folder).as_posix()
        seen.add(key)
        try:
            st = path.stat()
        except OSError:
            continue
        sig = [st.st_size, int(st.st_mtime)]
        entry = cache.get(key)
        if not entry or entry.get("sig") != sig:
            try:
                duration = probe(tools, path).duration
            except MediaError as exc:
                log.warning("Skipping %s: %s", path.name, exc)
                duration = 0.0
            entry = {"sig": sig, "duration": duration}
            cache[key] = entry
        if entry["duration"] >= MIN_USEFUL:
            library.append(Footage(path, key, entry["duration"]))
    for key in list(cache):
        if key not in seen:
            del cache[key]
    return library


def _usable_range(item: Footage, skip_start: float, skip_end: float) -> tuple[float, float]:
    """The part of a recording we are allowed to use (menus at the start/end can be skipped)."""
    if item.duration - skip_start - skip_end >= MIN_USEFUL:
        return skip_start, item.duration - skip_end
    return 0.0, item.duration


def _least_used(items: list[Footage], usage: dict, rng: random.Random, avoid: str | None = None) -> Footage:
    """Random pick among the recordings used the fewest times, so every recording gets its turn."""
    pool = [i for i in items if i.key != avoid] or items
    lowest = min(usage.get(i.key, 0) for i in pool)
    return rng.choice([i for i in pool if usage.get(i.key, 0) == lowest])


def plan_segments(
    library: list[Footage],
    duration: float,
    usage: dict,
    rng: random.Random | None = None,
    skip_start: float = 0.0,
    skip_end: float = 0.0,
) -> list[Segment]:
    """Gameplay pieces that add up to ``duration`` seconds (one piece whenever possible)."""
    if not library:
        raise MediaError("the gameplay folder is empty")
    rng = rng or random.Random()
    need = duration + SAFETY

    def usable(item: Footage) -> float:
        lo, hi = _usable_range(item, skip_start, skip_end)
        return hi - lo

    long_enough = [f for f in library if usable(f) >= need]
    if long_enough:
        item = _least_used(long_enough, usage, rng)
        lo, hi = _usable_range(item, skip_start, skip_end)
        start = rng.uniform(lo, hi - need)
        return [Segment(item.path, item.key, round(start, 3), round(need, 3))]

    # No single recording is long enough: chain random pieces back to back.
    segments: list[Segment] = []
    remaining = need
    counts = dict(usage)
    last_key = None
    while remaining > SAFETY:
        if len(segments) >= 500:
            raise MediaError("the gameplay recordings are too short to cover this clip")
        item = _least_used(library, counts, rng, avoid=last_key)
        lo, hi = _usable_range(item, skip_start, skip_end)
        take = min(hi - lo, remaining)
        leftover = remaining - take
        if SAFETY < leftover < MIN_USEFUL and take - (MIN_USEFUL - leftover) >= MIN_USEFUL:
            # Leave at least a full second for the next piece instead of a split-second flash.
            take -= MIN_USEFUL - leftover
        start = rng.uniform(lo, hi - take) if hi - lo > take else lo
        segments.append(Segment(item.path, item.key, round(start, 3), round(take, 3)))
        counts[item.key] = counts.get(item.key, 0) + 1
        last_key = item.key
        remaining -= take
    return segments


def pick_music(library: list[Footage], duration: float, rng: random.Random | None = None) -> tuple[Footage, float] | None:
    """A random track and where to start it (tracks shorter than the clip are looped)."""
    if not library:
        return None
    rng = rng or random.Random()
    track = rng.choice(library)
    start = rng.uniform(0, track.duration - duration) if track.duration > duration + 5 else 0.0
    return track, round(start, 3)
