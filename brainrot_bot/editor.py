"""Deciding the edit of one reel: what to keep, where to zoom, when emojis, sounds and bleeps happen."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from .analysis import Loudness, TimeMap, emoji_path, keyword_score, quantize, speech_intervals
from .captions import Chunk, emoji_events, group_words, prepare_words, time_groups
from .faces import Face, face_at, track_x
from .render import EmojiShow, Insert, Layout, Zoom
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
    inserts: list[Insert] = field(default_factory=list)  # voiceover pauses
    voice_words: list[Word] = field(default_factory=list)  # what the voiceover says (reel time)
    voice_lines: list[tuple[str, float, float, float]] = field(default_factory=list)  # text, voice start/end, pause end


def chunk_score(chunk: Chunk, tmap: TimeMap, loudness: Loudness | None) -> float:
    score = sum(keyword_score(w.text) for w in chunk.words if not w.censored)
    if loudness is not None:
        score += min(1.5, loudness.boost(tmap.to_source(chunk.start), tmap.to_source(chunk.end)))
    return score


def kept_intervals(cfg: SimpleNamespace, fps: float, start: float, end: float, words: list[Word],
                   loudness: Loudness | None = None) -> list[tuple[float, float]]:
    """The parts of start-end that stay in the reel (pauses cut when edit.cut_silences is on)."""
    in_range = [w for w in words if start <= w.start < end]
    if cfg.edit.cut_silences and in_range:
        return speech_intervals(in_range, start, end, max_pause=cfg.edit.max_pause, fps=fps, loudness=loudness)
    return [(quantize(start, fps), quantize(end, fps))]


def edited_length(cfg: SimpleNamespace, fps: float, start: float, end: float, words: list[Word],
                  loudness: Loudness | None = None) -> float:
    """How long the reel of start-end will be, once pauses are cut."""
    return TimeMap(kept_intervals(cfg, fps, start, end, words, loudness)).duration


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
    intervals = kept_intervals(cfg, fps, start, end, words, loudness)
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


def _shift(plan: EditPlan, by: float) -> None:
    """Everything that happens in the clip moves `by` seconds later (the intro is said first)."""
    plan.words = [Word(w.text, w.start + by, w.end + by) for w in plan.words]
    for chunk in plan.chunks:
        chunk.start, chunk.end = chunk.start + by, chunk.end + by
        for w in chunk.words:
            w.start, w.end = w.start + by, w.end + by
    plan.zooms = [replace(z, start=z.start + by, end=z.end + by) for z in plan.zooms]
    plan.emojis = [replace(e, start=e.start + by, end=e.end + by) for e in plan.emojis]
    plan.sounds = [replace(x, at=x.at + by) for x in plan.sounds]
    plan.mutes = [(a + by, b + by) for a, b in plan.mutes]
    plan.reframe = [(t + by, cx) for t, cx in plan.reframe]
    plan.cover_at += by


def add_voiceover(cfg: SimpleNamespace, plan: EditPlan, lines: list, fps: float, lead: float, tail: float) -> None:
    """Pause the clip for the voiceover: the intro line before it, the outro line after it (lines have
    .where, .audio, .length and .words). The voice gets its own captions, and a whoosh marks each switch."""
    pauses = {}
    for line in lines:  # each pause is a whole number of frames
        pauses[line.where] = (line, math.ceil((lead + line.length + tail) * fps - 1e-6) / fps)
    clip_length = plan.duration
    intro_length = pauses["intro"][1] if "intro" in pauses else 0.0
    if intro_length:
        _shift(plan, intro_length)
    style = plan.style
    for where, (line, length) in sorted(pauses.items(), key=lambda item: item[0] != "intro"):
        at = 0.0 if where == "intro" else intro_length + clip_length
        plan.inserts.append(Insert(0 if where == "intro" else len(plan.intervals), length, line.audio, lead))
        words = [Word(w.text, at + lead + w.start, min(at + length, at + lead + w.end)) for w in line.words]
        plan.voice_words += words
        if words:
            plan.voice_lines.append((line.text, words[0].start, words[-1].end, at + length))
        if cfg.captions.enabled and words:
            prepared = prepare_words(words, style.uppercase, style.remove_punctuation)
            for chunk in time_groups(group_words(prepared, style.max_words, style.max_chars), at + length):
                chunk.voice = True
                plan.chunks.append(chunk)
        if cfg.edit.sfx:  # a whoosh into the clip after the intro, and out of it before the outro
            plan.sounds.append(SoundEvent("whoosh", max(0.0, at + length - 0.3) if where == "intro" else max(0.0, at - 0.15)))
    plan.duration = intro_length + clip_length + (pauses["outro"][1] if "outro" in pauses else 0.0)
    plan.chunks.sort(key=lambda c: c.start)
