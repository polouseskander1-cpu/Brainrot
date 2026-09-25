"""What gets posted with each reel."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PostInfo:
    title: str  # e.g. the hook text, or the clip's file name
    hashtags: str
    duration: float

    @property
    def caption(self) -> str:
        return "\n\n".join(part for part in (self.title.strip(), self.hashtags.strip()) if part)

    @property
    def youtube_title(self) -> str:
        title = re.sub(r"[<>]", "", " ".join(self.title.split())) or "New video"
        tag = " #shorts" if self.duration <= 180 else ""
        return title[: 100 - len(tag)].rstrip() + tag

    @property
    def youtube_description(self) -> str:
        return re.sub(r"[<>]", "", self.caption)[:4500]

    def to_dict(self) -> dict:
        return {"title": self.title, "hashtags": self.hashtags, "duration": self.duration}

    @classmethod
    def from_dict(cls, data: dict) -> "PostInfo":
        return cls(str(data.get("title", "")), str(data.get("hashtags", "")), float(data.get("duration", 0)))


def pretty_title(stem: str) -> str:
    """'joe_rogan-clip_03' -> 'Joe rogan clip 03'"""
    text = " ".join(re.sub(r"[_\-.]+", " ", stem).split())
    return text[:1].upper() + text[1:] if text else "New video"
