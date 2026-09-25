"""Duplicate protection: the same video dropped in twice (copied, renamed, re-downloaded or re-encoded)
is noticed and skipped instead of being turned into reels and posted again.

Checks, from cheapest to most thorough:
- file: the same bytes (size + the start and end of the file);
- sound: the loudness rises and falls the same way, second by second (survives re-encoding);
  videos without sound compare 8 frames instead (a tiny "difference hash" per frame);
- words: the transcript is almost the same (MinHash of 4-word phrases), which also catches the same
  moment cut from a different upload of the same podcast.
Two different clips from the same podcast look alike (same studio), so pictures alone are only used
when there is no sound to compare.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path

from .media import Tools, popen_kwargs
from .transcribe import Word

log = logging.getLogger("brainrot")

QUICK_BYTES = 4 * 1024 * 1024
FRAMES = 8
FRAME_DISTANCE = 6  # bits (of 64) two frames may differ and still count as the same picture
FRAMES_NEEDED = 7  # of 8
SOUND_STEP = 0.5  # seconds per loudness step
SOUND_MIN_STEPS = 20
SOUND_MATCH = 0.85  # share of the clear ups and downs that must agree
SOUND_FLAT = 0.06  # a change smaller than 6% counts as "about the same"
SIGNATURE_SIZE = 64
MIN_WORDS = 25  # shorter transcripts are too short to compare
MAX_ENTRIES = 5000
_MASK = (1 << 61) - 1  # Mersenne prime for the MinHash permutations


def quick_hash(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1(str(size).encode())
    with open(path, "rb") as handle:
        digest.update(handle.read(QUICK_BYTES))
        if size > 2 * QUICK_BYTES:
            handle.seek(size - QUICK_BYTES)
            digest.update(handle.read(QUICK_BYTES))
    return digest.hexdigest()


def frame_hashes(tools: Tools, path: Path, duration: float) -> list[int]:
    """A 64-bit difference hash of 8 frames spread over the video (robust to re-encoding and resizing)."""
    hashes = []
    for i in range(FRAMES):
        at = duration * (i + 0.5) / FRAMES
        cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(path),
               "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
        try:
            raw = subprocess.run(cmd, capture_output=True, timeout=120, **popen_kwargs()).stdout
        except (OSError, subprocess.SubprocessError):
            raw = b""
        if len(raw) < 72:
            continue
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (raw[row * 9 + col] > raw[row * 9 + col + 1])
        hashes.append(bits)
    return hashes


def sound_bits(levels: list[float], hop: float = 0.1) -> str:
    """Per half second: '1' if the sound gets clearly louder, '0' if clearly quieter, '-' if about the same.
    Empty when there's too little to compare (short, silent, or the same level all the time)."""
    per = max(1, round(SOUND_STEP / hop))
    steps = [sum(levels[i : i + per]) / per for i in range(0, len(levels) - per + 1, per)]
    if len(steps) < SOUND_MIN_STEPS + 1 or max(steps) < 0.003:
        return ""
    bits = []
    for a, b in zip(steps, steps[1:]):
        change = (b - a) / max(a, b, 1e-6)
        bits.append("1" if change > SOUND_FLAT else "0" if change < -SOUND_FLAT else "-")
    informative = sum(bit != "-" for bit in bits)
    return "".join(bits) if informative >= max(SOUND_MIN_STEPS, len(bits) // 2) else ""


def sound_match(a: str, b: str) -> float:
    """Share of the clear ups and downs that agree (0 when the lengths differ or there's too little)."""
    n = min(len(a), len(b))
    if n < SOUND_MIN_STEPS or abs(len(a) - len(b)) > max(2, n // 50):
        return 0.0
    both = [(x, y) for x, y in zip(a[:n], b[:n]) if x != "-" or y != "-"]
    if len(both) < SOUND_MIN_STEPS:
        return 0.0
    return sum(x == y for x, y in both) / len(both)


def text_signature(words: list[Word]) -> list[int]:
    """MinHash of the 4-word phrases in a transcript; similar transcripts get similar signatures."""
    tokens = [t for t in (re.sub(r"[^\w']", "", w.text.lower()) for w in words) if t]
    if len(tokens) < MIN_WORDS:
        return []
    shingles = {" ".join(tokens[i : i + 4]) for i in range(len(tokens) - 3)}
    values = [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") & _MASK for s in shingles]
    signature = []
    for k in range(SIGNATURE_SIZE):
        a, b = 2 * k + 1_000_003, 7919 * k + 12_345
        signature.append(min((a * v + b) & _MASK for v in values))
    return signature


def similarity(a: list[int], b: list[int]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


def same_picture(a: list[int], b: list[int]) -> bool:
    if len(a) != FRAMES or len(b) != FRAMES:
        return False
    return sum(bin(x ^ y).count("1") <= FRAME_DISTANCE for x, y in zip(a, b)) >= FRAMES_NEEDED


def _is_blank(hashes: list[int]) -> bool:
    """All frames flat (e.g. a black screen or an audio-only video): pictures can't tell videos apart."""
    return all(h in (0, (1 << 64) - 1) for h in hashes)


class Fingerprints:
    """Remembered in bot_state.json under 'fingerprints'."""

    def __init__(self, state, threshold: float = 0.7):
        self.state = state
        self.threshold = threshold

    @property
    def entries(self) -> list[dict]:
        return self.state.data.setdefault("fingerprints", [])

    @staticmethod
    def _own(entry: dict, key: str) -> bool:
        """The clip's own earlier fingerprints (when a changed file is made again)."""
        return entry["key"] == key or entry["key"].startswith(key + "#")

    def same_file(self, key: str, quick: str) -> str | None:
        return next((e["key"] for e in self.entries if e.get("quick") == quick and not self._own(e, key)), None)

    def same_sound(self, key: str, bits: str) -> str | None:
        if not bits:
            return None
        return next((e["key"] for e in self.entries if e.get("sound") and not self._own(e, key)
                     and sound_match(bits, e["sound"]) >= SOUND_MATCH), None)

    def same_video(self, key: str, duration: float, frames: list[int]) -> str | None:
        """For videos without sound: the same length and the same pictures."""
        if len(frames) != FRAMES or _is_blank(frames):
            return None
        for e in self.entries:
            if self._own(e, key) or not e.get("frames"):
                continue
            if abs(float(e.get("duration", 0)) - duration) > 0.3:
                continue
            if same_picture(frames, [int(h, 16) for h in e["frames"]]):
                return e["key"]
        return None

    def same_words(self, key: str, signature: list[int]) -> str | None:
        if not signature:
            return None
        best, best_key = 0.0, None
        for e in self.entries:
            if self._own(e, key) or not e.get("text"):
                continue
            score = similarity(signature, [int(h, 16) for h in e["text"]])
            if score > best:
                best, best_key = score, e["key"]
        return best_key if best >= self.threshold else None

    def remember(self, key: str, *, quick: str = "", duration: float = 0.0, frames: list[int] | None = None,
                 sound: str = "", signature: list[int] | None = None) -> None:
        entries = [e for e in self.entries if e["key"] != key]
        entry: dict = {"key": key}
        if quick:
            entry["quick"] = quick
        if sound:
            entry["sound"] = sound
        if frames:
            entry["duration"] = round(duration, 2)
            entry["frames"] = [f"{h:016x}" for h in frames]
        if signature:
            entry["text"] = [f"{h:x}" for h in signature]
        entries.append(entry)
        self.state.data["fingerprints"] = entries[-MAX_ENTRIES:]

    def forget(self, key: str) -> None:
        self.state.data["fingerprints"] = [e for e in self.entries if not self._own(e, key)]
