"""The words around a reel: the hook shown on screen at the start, and the post's title, caption and
hashtags. Written by the AI when it's connected, otherwise by these built-in rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analysis import WORD_TO_EMOJI, keyword_score, normalize
from .moments import DANGLING, STORY, split_sentences
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


def best_line(words: list[Word], max_words: int = 12) -> str:
    """The sentence most likely to make people stop scrolling: short, a question or a bold claim."""
    sentences = split_sentences(words)
    best, best_score = "", 1.4
    total = max((s.end for s in sentences), default=1.0)
    for s in sentences:
        n = len(s.words)
        if n < 3 or n > max_words:
            continue
        score = sum(keyword_score(w.text) for w in s.words)
        if s.words[-1].text.endswith("?"):
            score += 1.5
        lowered = " " + s.text.lower() + " "
        if any(phrase in lowered for phrase in STORY):
            score += 1.0  # "the craziest thing that ever happened to me"
        if s.start < total * 0.4:
            score += 0.5
        if normalize(s.words[0].text) in DANGLING:
            score -= 1.0
        if score > best_score:
            best, best_score = clean_sentence(s.text), score
    return best


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
    tags = topic_tags(words)
    if folder:
        tags.insert(0, hashtag(folder))
    title = title_hint or line or "Watch this"
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
