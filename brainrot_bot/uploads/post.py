"""What gets posted with each reel, and how the text is written for each platform."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import DEFAULT_TEMPLATES

# (most characters, most hashtags) each platform allows or rewards.
LIMITS = {
    "youtube": (4500, 15),  # a video with more than 60 hashtags loses all of them
    "youtube_title": (100, 3),
    "tiktok": (2200, 8),
    "instagram": (2200, 5),  # Instagram ignores posts' hashtags beyond 5
    "facebook": (5000, 5),
    "x": (280, 2),
    "pinterest": (800, 5),
    "pinterest_title": (100, 0),
}


@dataclass
class PostInfo:
    title: str  # e.g. the hook text, or the clip's file name
    hashtags: str
    duration: float
    description: str = ""  # the post text; empty = the title

    @property
    def text(self) -> str:
        return (self.description or self.title).strip()

    @property
    def caption(self) -> str:
        return "\n\n".join(part for part in (self.text, self.hashtags.strip()) if part)

    def tags(self) -> list[str]:
        return [t for t in self.hashtags.split() if t.startswith("#") and len(t) > 1]

    def render(self, key: str, templates: dict | None = None) -> str:
        """The post text for one platform (key: youtube, youtube_title, tiktok, instagram, ...)."""
        template = (templates or {}).get(key) or DEFAULT_TEMPLATES.get(key) or "{caption}\n\n{hashtags}"
        max_chars, max_tags = LIMITS.get(key, (5000, 30))
        tags = " ".join(self.tags()[:max_tags])
        caption = self.text
        if key.endswith("_title"):
            caption = " ".join(caption.split())

        def fill(caption_text: str) -> str:
            text = template.replace("{title}", " ".join(self.title.split())).replace("{caption}", caption_text)
            text = text.replace("{hashtags}", tags).replace("\\n", "\n")
            text = re.sub(r"[ \t]+\n", "\n", text)
            return re.sub(r"\n{3,}", "\n\n", text).strip()

        text = fill(caption)
        measure = weighted_length if key == "x" else len
        if measure(text) > max_chars:
            # Shorten the caption (never the hashtags), ending with "..."
            room = max(0, len(caption) - (measure(text) - max_chars) - 3)
            while room > 0 and measure(fill(caption[:room].rstrip() + "...")) > max_chars:
                room -= max(1, room // 20)
            text = fill(caption[:room].rstrip() + "...") if room > 0 else fill("")
            if measure(text) > max_chars:
                text = text[:max_chars]
        return text

    @property
    def youtube_title(self) -> str:
        return youtube_title(self, None)

    @property
    def youtube_description(self) -> str:
        return re.sub(r"[<>]", "", self.render("youtube"))

    def to_dict(self) -> dict:
        return {"title": self.title, "hashtags": self.hashtags, "duration": self.duration, "description": self.description}

    @classmethod
    def from_dict(cls, data: dict) -> "PostInfo":
        return cls(str(data.get("title", "")), str(data.get("hashtags", "")), float(data.get("duration", 0)),
                   str(data.get("description", "")))


def youtube_title(post: PostInfo, templates: dict | None) -> str:
    title = re.sub(r"[<>]", "", post.render("youtube_title", templates)) or "New video"
    tag = " #shorts" if post.duration <= 180 and "#shorts" not in title.lower() else ""
    return title[: 100 - len(tag)].rstrip() + tag


def weighted_length(text: str) -> int:
    """How X counts characters: most Latin letters count 1, other scripts and emojis count 2."""
    total = 0
    for ch in text:
        code = ord(ch)
        light = code <= 0x10FF or 0x2000 <= code <= 0x200D or 0x2010 <= code <= 0x201F or 0x2032 <= code <= 0x2037
        total += 1 if light else 2
    return total


def pretty_title(stem: str) -> str:
    """'joe_rogan-clip_03' -> 'Joe rogan clip 03' ('Some Title [dQw4w9WgXcQ]' from a link -> 'Some Title')"""
    stem = re.sub(r"\s*\[[\w-]{6,}\]$", "", stem)
    text = " ".join(re.sub(r"[_\-.]+", " ", stem).split())
    return text[:1].upper() + text[1:] if text else "New video"


@dataclass
class Posted:
    """What a platform said after posting."""

    message: str  # for the log, e.g. a link or "sent to your TikTok inbox"
    url: str = ""
    post_id: str = ""  # the platform's id, used to read the views and likes later
