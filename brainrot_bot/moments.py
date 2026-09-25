"""Finding the best moments of a long video (podcast episodes, talks) to turn into reels.

Built-in rules score every run of whole sentences that fits the length limits: a strong first sentence
(a question, a bold claim, a number), a lot happening per second, energy, a clean ending, few filler
words. When an AI key is connected, Claude reads the transcript and picks the moments instead
(see ai.py); its picks are checked and repaired here the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .analysis import NUMBER_RE, Loudness, keyword_score, normalize
from .transcribe import Word

SENTENCE_END = (".", "!", "?", "…")
# A first word that usually needs earlier context ("And then he...", "Because it...").
DANGLING = set("and but because which that he she it they them this these those also then or anyway yeah".split())
FILLERS = set("um uh uhm erm hmm like basically literally".split())
# Ads, sponsor reads, intros and outros: never the best moment.
PROMO = ("sponsor", "promo code", "discount", "in the description", "link below", "subscribe", "patreon", "merch",
         "welcome back", "welcome to the", "thanks for watching", "thank you for watching", "use code", "use the code",
         "brought to you by", "check out our", "leave a comment", "left a comment", "see you next", "thanks to everyone",
         "before we start", "let's get into", "let's get started", "today's episode", "in this episode", "hit the like")
# A personal story or strong emotion keeps people watching.
STORY = ("happened to me", "i lost", "i remember", "one day", "my life", "i thought", "the day", "that night",
         "i realized", "changed my life", "never forget", "the worst", "the best thing", "true story")
EMOTION = set("shaking crying cried scared terrified afraid panic panicked shocked heartbroken furious desperate broke "
              "fired died gone destroyed ruined betrayed embarrassed".split())
MAX_SENTENCE = 20.0  # seconds; longer "sentences" (no punctuation) are split at commas or pauses


@dataclass
class Sentence:
    words: list[Word]
    index: int = 0

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Moment:
    start: float
    end: float
    score: float
    hook: str = ""
    why: str = ""
    sentences: list[Sentence] = field(default_factory=list, repr=False)

    @property
    def duration(self) -> float:
        return self.end - self.start


def split_sentences(words: list[Word], max_gap: float = 1.2) -> list[Sentence]:
    """Whole sentences, using the punctuation the speech model writes (or long pauses)."""
    sentences: list[Sentence] = []
    current: list[Word] = []
    for w in words:
        if current and (w.start - current[-1].end > max_gap or current[-1].text.rstrip("\"')”’").endswith(SENTENCE_END)):
            sentences.append(Sentence(current))
            current = []
        current.append(w)
    if current:
        sentences.append(Sentence(current))
    result: list[Sentence] = []
    for s in sentences:
        result.extend(_split_long(s))
    for i, s in enumerate(result):
        s.index = i
    return result


def _split_long(sentence: Sentence) -> list[Sentence]:
    if sentence.end - sentence.start <= MAX_SENTENCE or len(sentence.words) < 4:
        return [sentence]
    # Split at the comma (or pause) closest to the middle.
    words = sentence.words
    middle = (sentence.start + sentence.end) / 2
    best, best_cost = None, math.inf
    for i in range(1, len(words) - 1):
        gap = words[i + 1].start - words[i].end
        comma = words[i].text.endswith((",", ";", ":"))
        cost = abs(words[i].end - middle) - (5.0 if comma else 0.0) - gap * 4
        if cost < best_cost:
            best, best_cost = i, cost
    return _split_long(Sentence(words[: best + 1])) + _split_long(Sentence(words[best + 1:]))


# ------------------------------------------------------------------ built-in rules


@dataclass
class _Features:
    keywords: float
    question: bool
    exclaim: bool
    number: bool
    you: bool
    fillers: int
    dangling: float
    energy: float
    complete: bool
    n_words: int


def _features(s: Sentence, loudness: Loudness | None) -> _Features:
    texts = [normalize(w.text) for w in s.words]
    last = s.words[-1].text.rstrip("\"')”’")
    first = texts[0] if texts else ""
    dangling = 1.0 if first in DANGLING else 0.0
    if first == "so":
        dangling = 0.3
    return _Features(
        keywords=sum(keyword_score(w.text) for w in s.words),
        question=last.endswith("?"),
        exclaim=last.endswith("!"),
        number=any(NUMBER_RE.match(t) for t in texts if t),
        you=any(t in ("you", "your", "you're", "yourself") for t in texts),
        fillers=sum(1 for t in texts if t in FILLERS),
        dangling=dangling,
        energy=min(1.0, loudness.boost(s.start, s.end)) if loudness is not None else 0.0,
        complete=last.endswith(SENTENCE_END),
        n_words=len(s.words),
    )


def _window_score(sentences: list[Sentence], feats: list[_Features], i: int, j: int, target: float) -> float:
    start, end = sentences[i].start, sentences[j].end
    length = max(1.0, end - start)
    window = feats[i : j + 1]
    first, last = feats[i], feats[j]

    total = sum(f.keywords for f in window)
    # Both matter: a lot happening per second, and the whole point (with its payoff) being in the clip.
    content = 2.5 * math.tanh(total / length * 10 / 4) + 1.5 * math.tanh(total / 12)
    hook = 1.2 * first.question + 0.8 * (first.keywords >= 1) + 0.5 * first.number + 0.4 * first.you
    hook += 0.3 * (first.n_words <= 12) - 1.2 * first.dangling
    ending = (0.5 if last.complete else -0.5) + 0.4 * (last.keywords >= 1) + 0.3 * last.exclaim
    if j + 1 < len(sentences):
        following = sentences[j + 1]
        if len(following.words) <= 5 and following.start - sentences[j].end < 1.0:
            ending -= 0.4  # a short line right after ("Not one day before.") usually finishes the thought
    energy = sum(f.energy for f in window) / len(window)
    fillers = -0.15 * sum(f.fillers for f in window) / (length / 10)
    dead_air = 0.0
    for a, b in zip(sentences[i:j], sentences[i + 1 : j + 1]):
        if b.start - a.end > 1.0:
            dead_air -= 0.3 * (b.start - a.end)
    fit = -0.8 * max(0.0, target - length) / target  # too short to tell a story; longer is not better by itself
    text = " " + " ".join(s.text.lower() for s in sentences[i : j + 1]) + " "
    promo = -2.5 * min(2, sum(text.count(p) for p in PROMO))
    story = min(1.8, 0.6 * sum(text.count(p) for p in STORY))
    emotion = min(1.2, 0.4 * sum(1 for s in sentences[i : j + 1] for w in s.words if normalize(w.text) in EMOTION))
    return content + hook + ending + energy + fillers + dead_air + fit + promo + story + emotion


def find_moments(
    words: list[Word],
    duration: float,
    count: int,
    min_seconds: float = 20,
    max_seconds: float = 60,
    loudness: Loudness | None = None,
) -> list[Moment]:
    """The best `count` non-overlapping moments, best first."""
    sentences = split_sentences(words)
    if not sentences:
        return []
    feats = [_features(s, loudness) for s in sentences]
    target = min(max(22.0, min_seconds), max_seconds)
    candidates: list[tuple[float, int, int]] = []
    for i in range(len(sentences)):
        for j in range(i, len(sentences)):
            length = sentences[j].end - sentences[i].start
            if length > max_seconds:
                break
            if length >= min_seconds:
                candidates.append((_window_score(sentences, feats, i, j, target), i, j))
    if not candidates:  # the whole video is shorter than min_seconds of speech
        return [Moment(0.0, duration, 0.0, sentences=sentences)]
    candidates.sort(key=lambda c: -c[0])
    best = candidates[0][0]
    picked: list[Moment] = []
    for score, i, j in candidates:
        if len(picked) >= count:
            break
        if picked and (score < best - 4.0 or score <= 0):
            break  # the rest are much weaker than the best one
        start, end = sentences[i].start, sentences[j].end
        if any(start < m.end + 3 and end > m.start - 3 for m in picked):
            continue
        picked.append(Moment(start, end, round(score, 2), sentences=sentences[i : j + 1]))
    return [_padded(m, duration) for m in picked]


def _padded(moment: Moment, duration: float) -> Moment:
    """A little air before the first word and after the last one (pause cutting trims what's left)."""
    moment.start = max(0.0, moment.start - 0.15)
    moment.end = min(duration, moment.end + 0.4)
    return moment


def auto_count(duration: float, count: int, max_count: int) -> int:
    """How many reels to make from one long video: `count`, or about one per 8 minutes."""
    if count > 0:
        return count
    return max(1, min(max_count, round(duration / 60 / 8)))


# ------------------------------------------------------------------ checking the AI's picks


def moments_from_picks(
    picks: list[dict],
    sentences: list[Sentence],
    duration: float,
    count: int,
    min_seconds: float,
    max_seconds: float,
) -> list[Moment]:
    """Turn the AI's picks (sentence numbers) into moments that respect the limits and don't overlap."""
    moments: list[Moment] = []
    for pick in sorted(picks, key=lambda p: -float(p.get("score", 0) or 0)):
        if len(moments) >= count:
            break
        try:
            i, j = int(pick["start_sentence"]), int(pick["end_sentence"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < len(sentences)) or not (0 <= j < len(sentences)):
            continue
        i, j = min(i, j), max(i, j)
        # Too long: drop sentences from the end. Too short: add sentences after (or before).
        while j > i and sentences[j].end - sentences[i].start > max_seconds + 5:
            j -= 1
        while sentences[j].end - sentences[i].start < min_seconds:
            if j + 1 < len(sentences) and sentences[j + 1].end - sentences[i].start <= max_seconds + 5:
                j += 1
            elif i > 0 and sentences[j].end - sentences[i - 1].start <= max_seconds + 5:
                i -= 1
            else:
                break
        start, end = sentences[i].start, sentences[j].end
        if any(start < m.end and end > m.start for m in moments):
            continue
        hook = " ".join(str(pick.get("hook", "")).split())[:80]
        moments.append(_padded(Moment(start, end, float(pick.get("score", 0) or 0), hook, str(pick.get("why", ""))[:200],
                                      sentences[i : j + 1]), duration))
    return moments


def transcript_for_ai(sentences: list[Sentence]) -> str:
    """Numbered sentences with start times, e.g. '[12] 00:03:21 So the biggest mistake is...'."""
    lines = []
    for s in sentences:
        t = int(s.start)
        lines.append(f"[{s.index}] {t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d} {s.text}")
    return "\n".join(lines)
