"""The narration of a story: read in one go (so it flows like one telling), then timed word by word, which
also says where each scene starts and ends."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..commentary import align_words, prepare_audio
from ..media import Tools, run_ffmpeg
from ..transcribe import Word
from .script import Script

log = logging.getLogger("brainrot")

END_HOLD = 0.8  # the last picture stays this long after the last word
MAX_TEMPO = 1.15  # a narration that's too long is sped up by at most this much
MIN_TEMPO = 0.9  # one a little short of the minute TikTok pays for is slowed down by at most this much


@dataclass
class Narration:
    audio: Path  # 48 kHz stereo WAV, loudness evened out
    length: float  # seconds of sound
    words: list[Word]  # the written words, timed
    cuts: list[float]  # where each scene starts, then where the last one ends (len = scenes + 1)
    engine: str  # the voice that read it

    @property
    def total(self) -> float:
        return self.cuts[-1]

    def scene_lengths(self) -> list[float]:
        return [b - a for a, b in zip(self.cuts, self.cuts[1:])]


def scene_cuts(script: Script, words: list[Word], length: float, fps: float = 30.0) -> list[float]:
    """Each scene starts in the pause before its first word; the last one ends END_HOLD after the voice.
    Times are whole frames, and every scene gets at least a second."""
    counts = [len(s.narration.split()) for s in script.scenes]
    cuts = [0.0]
    first = 0
    for count in counts[:-1]:
        first += count
        if words and 0 < first < len(words):
            before, after = words[first - 1].end, words[first].start
            cut = (before + after) / 2 if after > before else after
        else:  # no timings: share the time by the number of words
            cut = length * first / max(1, sum(counts))
        cuts.append(cut)
    cuts.append(length + END_HOLD)
    frames = [round(c * fps) for c in cuts]
    for i in range(1, len(frames)):
        frames[i] = max(frames[i], frames[i - 1] + round(fps))
    return [f / fps for f in frames]


def _tempo(tools: Tools, audio: Path, tempo: float, should_stop: Callable[[], bool] | None) -> None:
    changed = audio.with_name(audio.stem + "_tempo.wav")
    run_ffmpeg(tools, ["-i", str(audio), "-af", f"atempo={tempo:.4f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                       str(changed)], log_path=changed.with_suffix(".log"), should_stop=should_stop)
    changed.replace(audio)


def narrate(voice, tools: Tools, script: Script, language: str, work: Path, hear: Callable[[Path], list[Word]] | None,
            max_seconds: float = 90.0, fps: float = 30.0, should_stop: Callable[[], bool] | None = None,
            min_seconds: float = 0.0) -> Narration:
    """Read the story aloud (voice: a Voice), time it, and find the scene cuts. The reel ends up between
    min_seconds and max_seconds long when a small change of pace can do it."""
    text = script.narration
    raw = voice.speak(text, language, work / "narration_raw.wav")
    engine = getattr(voice, "last_engine", "") or "piper"
    out = work / "narration.wav"
    length = prepare_audio(tools, raw, out, should_stop)
    total = length + END_HOLD
    if total > max_seconds and length > 1:
        tempo = min(MAX_TEMPO, total / max_seconds)
        _tempo(tools, out, tempo, should_stop)
        length /= tempo
        log.info("The narration was %.0fs; read %.0f%% faster to fit %.0fs.", length * tempo, (tempo - 1) * 100, max_seconds)
    elif min_seconds and min_seconds * MIN_TEMPO <= total < min_seconds:
        tempo = max(MIN_TEMPO, length / (min_seconds - END_HOLD + 0.1))
        _tempo(tools, out, tempo, should_stop)
        length /= tempo
        log.info("The narration was %.0fs; read %.0f%% slower to last over a minute.", length * tempo, (1 - tempo) * 100)
    heard: list[Word] = []
    if hear is not None:
        try:
            heard = hear(out)
        except Exception as exc:  # noqa: BLE001 - the captions then follow an estimate
            log.warning("Couldn't time the narration's captions (%s); using an estimate.", exc)
    words = align_words(text, heard, length)
    return Narration(out, length, words, scene_cuts(script, words, length, fps), engine)
