"""Sound effects made from scratch (no downloaded sounds, so no licensing worries):
whoosh (zoom-ins), boom (punchlines), pop (emojis) and bleep (censored words)."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

RATE = 48000


@dataclass
class SoundEvent:
    kind: str  # whoosh | boom | pop | bleep
    at: float  # seconds in the finished reel
    length: float = 0.0  # bleeps: how long


def _whoosh(np, rng):
    n = int(0.45 * RATE)
    t = np.linspace(0, 1, n)
    noise = rng.standard_normal(n)
    # sweep a simple resonant low-pass from low to high: the classic "whoosh"
    out = np.zeros(n)
    y1 = y2 = 0.0
    for i in range(n):
        cutoff = 0.02 + 0.25 * t[i] ** 1.5
        y1 += cutoff * (noise[i] - y1)
        y2 += cutoff * (y1 - y2)
        out[i] = y2
    envelope = np.sin(np.pi * t) ** 1.5
    out = out * envelope
    return out / (np.abs(out).max() + 1e-9) * 0.8


def _boom(np, rng):
    n = int(0.9 * RATE)
    t = np.arange(n) / RATE
    freq = 38 + 55 * np.exp(-t * 9)  # pitch drops fast: "vine boom"
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    body = np.sin(phase) * np.exp(-t * 3.2)
    click = rng.standard_normal(n) * np.exp(-t * 90) * 0.35
    out = np.tanh((body + click) * 2.2)
    return out / (np.abs(out).max() + 1e-9) * 0.9


def _pop(np, rng):
    n = int(0.12 * RATE)
    t = np.arange(n) / RATE
    freq = 900 - 500 * t / t[-1]
    out = np.sin(2 * np.pi * np.cumsum(freq) / RATE) * np.exp(-t * 40)
    return out * 0.6


def _bleep(np, length: float):
    n = max(1, int(length * RATE))
    t = np.arange(n) / RATE
    out = np.sin(2 * np.pi * 1000 * t) * 0.35
    fade = min(n // 2, int(0.005 * RATE))
    if fade:
        ramp = np.linspace(0, 1, fade)
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    return out


def write_track(events: list[SoundEvent], duration: float, path: Path, volume: float = 0.6) -> Path:
    """One stereo 48 kHz WAV with every effect at its moment (mixed under the voice later)."""
    import numpy as np

    rng = np.random.default_rng(7)
    sounds = {"whoosh": _whoosh(np, rng), "boom": _boom(np, rng), "pop": _pop(np, rng)}
    levels = {"whoosh": 0.35, "boom": 0.55, "pop": 0.45, "bleep": 1.0}  # at sfx_volume 1, next to a -14 LUFS voice
    track = np.zeros(int(duration * RATE) + RATE, dtype=np.float64)
    for event in events:
        clip = _bleep(np, event.length) if event.kind == "bleep" else sounds.get(event.kind)
        if clip is None:
            continue
        start = int(max(0.0, event.at) * RATE)
        end = min(len(track), start + len(clip))
        if end > start:
            gain = levels[event.kind] * (1.0 if event.kind == "bleep" else volume)
            track[start:end] += clip[: end - start] * gain
    track = np.clip(track[: int(duration * RATE)], -1.0, 1.0)
    pcm = (track * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(stereo.tobytes())
    return path
