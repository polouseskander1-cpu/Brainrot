"""Understanding a clip: keywords, emojis, swear words, pauses, loud moments."""

from __future__ import annotations

import bisect
import math
import re
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import RESOURCE_DIR
from .transcribe import Word

EMOJI_DIR = RESOURCE_DIR / "assets" / "emoji"

# emoji (Noto file code) -> words that trigger it
EMOJI_WORDS = {
    "1f4b0": "money cash dollar dollars rich richer richest million millions billion billions paid pay salary price expensive profit",
    "1f525": "fire hot lit burn burning flames",
    "2764": "love loved loving heart",
    "1f9e0": "brain smart genius intelligence iq",
    "1f4a1": "idea ideas insight",
    "1f602": "funny laugh laughing hilarious joke jokes lol",
    "1f480": "dead death die died dying kill killed killing",
    "1f92f": "crazy insane mindblowing unbelievable wild",
    "1f631": "shocked shocking scary scared terrified",
    "1f3c6": "win won winner winning champion trophy",
    "26a1": "fast faster fastest speed quick quickly instantly",
    "23f0": "time clock hours minutes deadline",
    "1f354": "food eat eating burger pizza hungry",
    "1f697": "car cars drive driving",
    "1f4f1": "phone phones iphone app apps",
    "1f30d": "world earth global planet",
    "1f680": "rocket launch growth grow growing skyrocket",
    "1f4aa": "gym workout strong muscle muscles strength",
    "1f622": "sad cry crying tears depressed",
    "1f621": "angry mad rage furious",
    "1f92b": "secret secrets hidden",
    "1f440": "look watch watching eyes",
    "1f914": "think thinking wonder",
    "1f4da": "school learn learning study studying teacher book books",
    "1f3b5": "music song songs",
    "1f3ae": "game games gaming gamer",
    "1f451": "king queen boss",
    "1f4c8": "stocks stock invest investing investment market",
    "1f6a8": "warning danger alert emergency",
    "1f4af": "hundred percent",
    "1f389": "party celebrate celebration",
    "1f634": "sleep sleeping tired",
    "1f4b8": "broke debt lost losing",
    "1f37a": "beer drink drinking drunk",
    "1f436": "dog dogs puppy",
    "1f431": "cat cats kitten",
    "2708": "plane flight travel",
    "1f3e0": "house home",
    "1f48d": "marriage married wedding wife husband",
    "1f476": "baby kids kid children",
    "1f9d1": "people person",
    "1f64f": "pray god faith blessed",
    "1f4f8": "photo camera instagram",
    "1f3a5": "video movie film youtube",
    "1f910": "quiet silent",
    "1f973": "birthday",
    "2705": "correct exactly true",
    "274c": "wrong false mistake mistakes",
}
WORD_TO_EMOJI = {word: code for code, words in EMOJI_WORDS.items() for word in words.split()}

# Words that make a phrase worth emphasizing (color, zoom, sound effect).
STRONG_WORDS = set("""
never nobody secret secrets truth lie lies crazy insane biggest best worst million millions billion billions thousand
money rich broke dead death kill killed shocking shocked amazing incredible impossible unbelievable huge massive
perfect mistake mistakes wrong important dangerous illegal banned forbidden fired quit hate forever instantly
immediately destroyed disaster genius terrifying brutal
""".split())

# Common English swear words (plus simple variants). Add your own in config.yaml (edit.censor_words).
PROFANITY = set("""
fuck fucks fucked fucking fucker motherfucker motherfuckers shit shits shitty bullshit bitch bitches
asshole assholes bastard bastards dick dicks pussy cunt cunts whore slut damn goddamn crap piss pissed
""".split())

# Words ending in -est that are not "the most ..." words.
NOT_SUPERLATIVE = set("""
test tests rest best west guest nest chest vest pest jest zest lest crest quest honest modest earnest interest
request forest harvest protest suggest invest manifest digest arrest contest priest conquest tempest attest detest
infest divest ingest congest unrest behest bequest midwest northwest southwest inquest retest pretest
""".split())

WORD_RE = re.compile(r"[a-z0-9']+")
NUMBER_RE = re.compile(r"^\$?\d[\d,.]*(%|k|m|b)?$|^(percent|million|billion|thousand|hundred)$")


def normalize(text: str) -> str:
    """'Money!' -> 'money'"""
    match = WORD_RE.findall(text.lower().replace("’", "'"))
    return "".join(match).strip("'")


def is_superlative(word: str) -> bool:
    return len(word) >= 6 and word.endswith("est") and word not in NOT_SUPERLATIVE


def keyword_score(text: str) -> float:
    word = normalize(text)
    if not word:
        return 0.0
    score = 0.0
    if word in STRONG_WORDS:
        score += 1.0
    if NUMBER_RE.match(word) or "$" in text or "%" in text:
        score += 1.2
    if word in WORD_TO_EMOJI:
        score += 0.6
    if is_superlative(word):
        score += 1.0  # biggest, craziest, fastest...
    if text.rstrip().endswith("!"):
        score += 0.6
    return score


def is_keyword(text: str) -> bool:
    return keyword_score(text) >= 1.0


def emoji_for(text: str) -> str | None:
    code = WORD_TO_EMOJI.get(normalize(text))
    if code and (EMOJI_DIR / f"emoji_u{code}.png").exists():
        return code
    return None


def emoji_path(code: str) -> Path:
    return EMOJI_DIR / f"emoji_u{code}.png"


def is_profane(text: str, extra: set[str] | None = None) -> bool:
    word = normalize(text)
    return bool(word) and (word in PROFANITY or (extra is not None and word in extra))


def censor_word(text: str) -> str:
    """'FUCKING' -> 'F******'"""
    letters = [i for i, ch in enumerate(text) if ch.isalnum()]
    if len(letters) < 2:
        return text
    chars = list(text)
    for i in letters[1:]:
        chars[i] = "*"
    return "".join(chars)


# ---------------------------------------------------------------- loudness


class Loudness:
    """RMS level every 0.1 s of the 16 kHz mono speech file we already make for the transcription."""

    HOP = 0.1

    def __init__(self, levels: list[float]):
        self.levels = levels
        speech = sorted(v for v in levels if v > 0.003)
        self.median = speech[len(speech) // 2] if speech else 0.0

    @classmethod
    def from_wav(cls, path: Path) -> "Loudness":
        """Read a 16-bit WAV a minute at a time (a 3-hour podcast would not fit in memory at once)."""
        import numpy as np

        levels: list[float] = []
        with wave.open(str(path), "rb") as handle:
            rate, width, channels = handle.getframerate(), handle.getsampwidth(), handle.getnchannels()
            if width != 2 or channels < 1:
                return cls([])
            hop = max(1, int(rate * cls.HOP))
            while True:
                data = handle.readframes(hop * 600)
                if not data:
                    break
                samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
                count = len(samples) // hop
                if count == 0:
                    break
                frames = samples[: count * hop].reshape(count, hop)
                levels.extend(float(x) for x in np.sqrt((frames ** 2).mean(axis=1)))
        return cls(levels)

    def level(self, start: float, end: float) -> float:
        a, b = int(start / self.HOP), max(int(start / self.HOP) + 1, int(math.ceil(end / self.HOP)))
        window = self.levels[a:b]
        return max(window) if window else 0.0

    def is_quiet(self, start: float, end: float) -> bool:
        """True if nothing loud happens here (so it can be cut). Laughter or reactions keep the gap."""
        if not self.levels or self.median <= 0:
            return True
        return self.level(start, end) < 0.35 * self.median

    def boost(self, start: float, end: float) -> float:
        """How much louder than usual this moment is (0 = normal)."""
        if not self.levels or self.median <= 0:
            return 0.0
        return max(0.0, self.level(start, end) / self.median - 1.3)


# ---------------------------------------------------------------- cutting pauses


def quantize(t: float, fps: float) -> float:
    return round(t * fps) / fps


def speech_intervals(
    words: list[Word],
    start: float,
    end: float,
    *,
    max_pause: float = 0.4,
    pad: float = 0.12,
    fps: float = 30,
    loudness: Loudness | None = None,
) -> list[tuple[float, float]]:
    """Parts of [start, end) to keep so that pauses longer than max_pause shrink to ~2*pad.
    Leading/trailing silence is trimmed too. Gaps with laughter or other loud sound are kept."""
    inside = [w for w in words if w.start >= start and w.end <= end + 0.5]
    if not inside:
        return [(quantize(start, fps), quantize(end, fps))]
    keep: list[list[float]] = [[max(start, inside[0].start - pad), min(end, inside[0].end + pad)]]
    for prev, word in zip(inside, inside[1:]):
        gap_start, gap_end = prev.end, word.start
        quiet = loudness is None or loudness.is_quiet(gap_start + pad, gap_end - pad)
        if gap_end - gap_start > max_pause and quiet:
            keep.append([max(start, word.start - pad), min(end, word.end + pad)])
        else:
            keep[-1][1] = min(end, word.end + pad)
    keep[-1][1] = min(end, keep[-1][1] + pad)  # let the last word ring out a little
    result: list[tuple[float, float]] = []
    for a, b in keep:
        a, b = quantize(a, fps), quantize(b, fps)
        if result and a <= result[-1][1] + 1 / fps:
            result[-1] = (result[-1][0], max(result[-1][1], b))
        elif b - a >= 2 / fps:
            result.append((a, b))
    return result or [(quantize(start, fps), quantize(end, fps))]


@dataclass
class TimeMap:
    """Where a moment of the original clip ends up in the edited reel (after pauses are cut)."""

    intervals: list[tuple[float, float]]

    def __post_init__(self):
        self.offsets: list[float] = []
        total = 0.0
        for a, b in self.intervals:
            self.offsets.append(total)
            total += b - a
        self.duration = total
        self._starts = [a for a, _ in self.intervals]

    def to_output(self, t: float) -> float:
        i = max(0, bisect.bisect_right(self._starts, t) - 1)
        a, b = self.intervals[i]
        if t < a:
            return self.offsets[i]
        return self.offsets[i] + min(t, b) - a

    def to_source(self, t: float) -> float:
        """The moment of the original clip shown at time t of the reel."""
        i = max(0, bisect.bisect_right(self.offsets, t) - 1)
        a, b = self.intervals[i]
        return min(b, a + max(0.0, t - self.offsets[i]))

    def map_words(self, words: list[Word]) -> list[Word]:
        first, last = self.intervals[0][0], self.intervals[-1][1]
        mapped = []
        for w in words:
            if w.end <= first or w.start >= last:
                continue
            start, end = self.to_output(w.start), self.to_output(w.end)
            if end - start < 0.04:
                end = start + 0.04
            mapped.append(Word(w.text, start, min(end, self.duration)))
        return mapped
