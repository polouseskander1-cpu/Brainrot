"""Optionally split long clips into Part 1, Part 2, ... cutting at natural pauses in the speech."""

from __future__ import annotations

import math

from .captions import SENTENCE_END
from .transcribe import Word


def _cut_candidates(words: list[Word]) -> list[tuple[float, float]]:
    """(time, score) for every pause between two words. Longer pauses and sentence ends score higher."""
    candidates = []
    for a, b in zip(words, words[1:]):
        gap = b.start - a.end
        if gap <= 0.05:
            continue
        score = min(gap, 1.5)
        if a.text.rstrip().endswith(SENTENCE_END):
            score += 1.0
        candidates.append(((a.end + b.start) / 2, score))
    return candidates


def plan_parts(duration: float, words: list[Word], max_seconds: float) -> list[tuple[float, float]]:
    """Return (start, end) of each part. One part covering everything when splitting is off or not needed."""
    if max_seconds <= 0 or duration <= max_seconds:
        return [(0.0, duration)]
    candidates = _cut_candidates(words)
    count = math.ceil(duration / max_seconds)
    while True:
        cuts = _choose_cuts(duration, candidates, count, max_seconds)
        bounds = list(zip([0.0] + cuts, cuts + [duration]))
        if all(end - start <= max_seconds + 0.01 for start, end in bounds) or count > duration:
            return [(round(s, 3), round(e, 3)) for s, e in bounds]
        count += 1


def _choose_cuts(duration: float, candidates: list[tuple[float, float]], count: int, max_seconds: float) -> list[float]:
    cuts: list[float] = []
    previous = 0.0
    for i in range(1, count):
        ideal = (duration - previous) / (count - i + 1)
        target = previous + ideal
        lo = previous + ideal * 0.7
        hi = min(previous + max_seconds, previous + ideal * 1.2, duration - 1.0)
        best, best_cost = None, None
        for t, score in candidates:
            if lo <= t <= hi:
                cost = abs(t - target) / ideal - score * 0.35
                if best_cost is None or cost < best_cost:
                    best, best_cost = t, cost
        cut = best if best is not None else min(target, previous + max_seconds)
        cuts.append(cut)
        previous = cut
    return cuts
