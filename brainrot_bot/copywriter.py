"""The words around a reel: the hook shown on screen at the start, and the post's title, caption and
hashtags. Written by the AI when it's connected, otherwise by these built-in rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analysis import WORD_TO_EMOJI, keyword_score, normalize
from .moments import DANGLING, HOT_TAKE, STORY, WONDER, WONDER_WORDS, split_sentences
from .transcribe import Word

# Topic hashtags for words the bot recognizes (emoji code -> hashtag).
TOPIC_TAGS = {
    "1f4b0": "money", "1f4c8": "investing", "1f680": "business", "1f9e0": "mindset", "1f4a1": "motivation",
    "1f4aa": "fitness", "1f354": "food", "1f3ae": "gaming", "1f4da": "education", "1f602": "funny",
    "2764": "relationships", "1f48d": "relationships", "1f3b5": "music", "1f697": "cars", "2708": "travel",
    "1f64f": "faith", "1f480": "storytime", "1f631": "storytime", "1f30d": "facts", "1f4f1": "tech",
}
FILLER_START = re.compile(r"^(so|and|but|like|well|okay|ok|yeah|um|uh|you know)\b[\s,]*", re.IGNORECASE)


@dataclass
class Copy:
    hook: str = ""  # on screen during the first seconds ("" = none)
    title: str = ""  # post title (YouTube)
    caption: str = ""  # post text (TikTok, Instagram, Facebook)
    hashtags: list[str] = field(default_factory=list)
    by_ai: bool = False


def clean_sentence(text: str) -> str:
    """'so, the biggest mistake is...' -> 'The biggest mistake is...'"""
    text = " ".join(text.split()).strip(" ,;:-–—\"'“”")
    while True:
        stripped = FILLER_START.sub("", text, count=1)
        if stripped == text or not stripped:
            break
        text = stripped
    text = text.rstrip(".,;:…")
    return text[:1].upper() + text[1:]


def _first_clause(words: list[Word]) -> list[Word]:
    """'Here is my unpopular opinion, and people get angry...' -> 'Here is my unpopular opinion,'"""
    for i, w in enumerate(words):
        if w.text.endswith((",", ";", ":", "—", "-")):
            return words[: i + 1]
    return []


def best_line(words: list[Word], max_words: int = 14) -> str:
    """The line most likely to make people stop scrolling: short, a question, a hot take or a bold claim.
    A long sentence can lend its first clause ('Here is my unpopular opinion')."""
    sentences = split_sentences(words)
    best, best_score = "", 1.4
    if not sentences:
        return best
    begin = sentences[0].start
    span = max(1.0, sentences[-1].end - begin)
    for s in sentences:
        clause = _first_clause(s.words)
        candidates = ([s.words] if len(s.words) <= max_words else []) + ([clause] if 4 <= len(clause) < len(s.words) else [])
        for part in candidates:
            if len(part) < 3 or len(part) > max_words:
                continue
            score = sum(keyword_score(w.text) for w in part)
            if part[-1].text.endswith("?"):
                score += 1.5
            lowered = " " + " ".join(w.text for w in part).lower() + " "
            if any(phrase in lowered for phrase in STORY):
                score += 1.0  # "the craziest thing that ever happened to me"
            if any(phrase in lowered for phrase in HOT_TAKE):
                score += 2.0  # "unpopular opinion", "is a scam"
            if any(phrase in lowered for phrase in WONDER) or any(normalize(w.text) in WONDER_WORDS for w in part):
                score += 1.0  # "did you know", aliens, the future
            if s.start - begin < span * 0.4:
                score += 0.5  # near the start of this clip, where the hook is on screen
            if s is sentences[0]:
                score += 0.5
            if normalize(part[0].text) in DANGLING:
                score -= 1.0
            score -= 0.25 * max(0, len(part) - 8)  # it's on screen for a few seconds: shorter reads faster
            if score > best_score:
                best, best_score = clean_sentence(" ".join(w.text for w in part)), score
    return best


def opening_line(words: list[Word], max_words: int = 14) -> str:
    """The first sentence (or its first clause, or first words): a better title than none at all."""
    sentences = split_sentences(words)
    if not sentences or len(sentences[0].words) < 3:
        return ""
    first = sentences[0].words
    if len(first) <= max_words:
        return clean_sentence(" ".join(w.text for w in first))
    clause = _first_clause(first)
    if 3 <= len(clause) <= max_words:
        return clean_sentence(" ".join(w.text for w in clause))
    return clean_sentence(" ".join(w.text for w in first[: max_words - 2])) + "…"


def hashtag(text: str) -> str:
    """'Diary of a CEO' -> 'DiaryOfACEO'"""
    parts = re.findall(r"[^\W_]+", text, re.UNICODE)
    return "".join(p[:1].upper() + p[1:] for p in parts)[:40]


def topic_tags(words: list[Word], limit: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for w in words:
        tag = TOPIC_TAGS.get(WORD_TO_EMOJI.get(normalize(w.text), ""))
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
    return [tag for tag, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def merge_hashtags(*groups) -> str:
    """Hashtag lists and strings -> '#a #b #c' without duplicates (case-insensitive), in order."""
    seen, out = set(), []
    for group in groups:
        items = group.split() if isinstance(group, str) else list(group or [])
        for item in items:
            tag = "#" + re.sub(r"[^\w]", "", str(item).lstrip("#"), flags=re.UNICODE)
            if len(tag) > 1 and tag.lower() not in seen:
                seen.add(tag.lower())
                out.append(tag)
    return " ".join(out)


def built_in_copy(words: list[Word], title_hint: str, folder: str) -> Copy:
    line = best_line(words)
    if not line:  # nothing short and punchy: a question that opens the reel still makes a good hook
        sentences = split_sentences(words)
        if sentences and sentences[0].words[-1].text.rstrip().endswith("?"):
            line = opening_line(words, 12)
    tags = topic_tags(words)
    if folder:
        tags.insert(0, hashtag(folder))
    title = title_hint or line or opening_line(words) or "Watch this"
    return Copy(hook=line, title=title, caption=line or title, hashtags=[t for t in tags if t])


def transcript_text(words: list[Word], limit: int = 12000) -> str:
    return " ".join(w.text for w in words)[:limit]


def write_copy(ai, words: list[Word], title_hint: str, folder: str, length: float, use_ai: bool = True) -> Copy:
    """AI copy when possible (falling back to the built-in rules), always with a title and caption."""
    fallback = built_in_copy(words, title_hint, folder)
    if not (use_ai and ai is not None and ai.available and words):
        return fallback
    source = f' from "{folder}"' if folder else ""
    data = ai.write_copy(transcript_text(words), length, source)
    if not data:
        return fallback
    hashtags = list(data["hashtags"])
    if folder:
        hashtags.insert(0, hashtag(folder))
    return Copy(
        hook=data["hook"],
        title=title_hint or data["title"] or data["hook"] or fallback.title,
        caption=data["caption"] or data["title"] or fallback.caption,
        hashtags=hashtags,
        by_ai=True,
    )
