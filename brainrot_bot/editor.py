"""Deciding the edit of one reel: what to keep, where to zoom, when emojis, sounds and bleeps happen."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from .analysis import Loudness, TimeMap, emoji_path, keyword_score, quantize, speech_intervals
from .captions import Chunk, emoji_events, group_words, prepare_words, time_groups
from .faces import Face, face_at, track_x
from .render import EmojiShow, Layout, Zoom
from .sfx import SoundEvent
from .styles import apply_style
from .transcribe import Word

ZOOM_GAP = 3.5  # seconds between zoom-ins at least
BOOM_GAP = 12.0


@dataclass
class EditPlan:
    intervals: list[tuple[float, float]]  # parts of the clip kept (source time)
    duration: float
    words: list[Word]  # reel time
    chunks: list[Chunk]
    style: SimpleNamespace  # captions settings with the chosen style applied
    zooms: list[Zoom] = field(default_factory=list)
    emojis: list[EmojiShow] = field(default_factory=list)
    sounds: list[SoundEvent] = field(default_factory=list)
    mutes: list[tuple[float, float]] = field(default_factory=list)
    reframe: list[tuple[float, float]] = field(default_factory=list)
    cover_at: float = 0.0


def chunk_score(chunk: Chunk, tmap: TimeMap, loudness: Loudness | None) -> float:
    score = sum(keyword_score(w.text) for w in chunk.words if not w.censored)
    if loudness is not None:
        score += min(1.5, loudness.boost(tmap.to_source(chunk.start), tmap.to_source(chunk.end)))
    return score


def plan_edit(
    cfg: SimpleNamespace,
    layout: Layout,
    start: float,
    end: float,
    words: list[Word],
    loudness: Loudness | None = None,
    faces: list[Face] | None = None,
) -> EditPlan:
    edit, fps = cfg.edit, layout.fps
    in_range = [w for w in words if start <= w.start < end]
    if edit.cut_silences and in_range:
        intervals = speech_intervals(in_range, start, end, max_pause=edit.max_pause, fps=fps, loudness=loudness)
    else:
        intervals = [(quantize(start, fps), quantize(end, fps))]
    tmap = TimeMap(intervals)
    reel_words = tmap.map_words(in_range)
    style = apply_style(cfg.captions)
    censor = set(edit.censor_words) if edit.censor else None
    prepared = prepare_words(reel_words, style.uppercase, style.remove_punctuation, censor=censor, keywords=True)
    chunks = time_groups(group_words(prepared, style.max_words, style.max_chars), tmap.duration)
    plan = EditPlan(intervals, tmap.duration, reel_words, chunks if cfg.captions.enabled else [], style)

    # Zoom in on the strongest moments, not too often.
    if edit.zoom:
        scored = sorted(((chunk_score(c, tmap, loudness), c) for c in chunks), key=lambda x: -x[0])
        limit = max(1, int(tmap.duration / 6))
        picked: list[tuple[float, Chunk]] = []
        for score, chunk in scored:
            if score < 1.0 or len(picked) >= limit:
                break
            if all(abs(chunk.start - other.start) >= ZOOM_GAP for _, other in picked):
                picked.append((score, chunk))
        picked.sort(key=lambda x: x[1].start)
        last_boom = -BOOM_GAP
        for score, chunk in picked:
            length = min(2.2, max(0.7, chunk.end - chunk.start))
            face = face_at(faces or [], tmap.to_source(chunk.start)) if edit.face_tracking else None
            zoom = Zoom(chunk.start, min(tmap.duration, chunk.start + length), face.cx if face else 0.5, face.cy if face else 0.42)
            plan.zooms.append(zoom)
            if edit.sfx:
                plan.sounds.append(SoundEvent("whoosh", max(0.0, zoom.start - 0.12)))
                if score >= 2.5 and zoom.start - last_boom >= BOOM_GAP:
                    plan.sounds.append(SoundEvent("boom", zoom.start))
                    last_boom = zoom.start

    if edit.emojis and cfg.captions.enabled:
        for code, a, b in emoji_events(chunks):
            plan.emojis.append(EmojiShow(emoji_path(code), a, b))
            if edit.sfx:
                plan.sounds.append(SoundEvent("pop", a))

    if censor is not None:
        for w in prepared:
            if w.censored:
                a, b = max(0.0, w.start - 0.02), min(tmap.duration, w.end + 0.02)
                plan.mutes.append((a, b))
                if edit.censor_mode == "bleep":
                    plan.sounds.append(SoundEvent("bleep", a, b - a))

    if layout.mode == "fullscreen":
        points = track_x(faces or [], intervals[0][0], intervals[-1][1], step=1.0)
        plan.reframe = [(tmap.to_output(t), cx) for t, cx in points]

    plan.cover_at = plan.zooms[0].start + 0.3 if plan.zooms else tmap.duration * 0.3
    return plan
