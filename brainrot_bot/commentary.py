"""A commentary voiceover: a line before the clip that sets it up, and your take after it.

Reposted clips don't earn much anywhere: YouTube only pays for reused clips with "significant original
commentary", TikTok's Creator Rewards want original content, and Facebook holds back reposts that only
add captions. So the clip pauses (the picture holds still, the gameplay keeps going) while a voiceover
says something the clip doesn't, with its own captions in their own color. The words come from:

- your own recording: <clip>.intro.mp3 / <clip>.outro.mp3 (any audio file) next to the clip,
- your own text: <clip>.commentary.txt, read by the voice ("intro: ..." and "outro: ..." lines),
- otherwise the AI helper, which writes a take for this clip: context, a counterpoint, a question.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from .analysis import normalize
from .media import AUDIO_EXTS, Tools, run_ffmpeg
from .moments import split_sentences
from .transcribe import Word
from .translate import base_language, language_name
from .voice import Voice

log = logging.getLogger("brainrot")

WHERE = ("intro", "outro")  # before the clip, after it
TEXT_FILE = ".commentary.txt"
MAX_WORDS = {"intro": 16, "outro": 55}
LEAD = 0.25  # the pause starts this long before the voice
TAIL = 0.4  # and holds this long after it
# What the voiceover usually adds (seconds), used to pick moments of the right length before it's written.
TYPICAL = {"intro": 4.0, "outro": 10.0}
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_PREFIX_RE = re.compile(r"^\s*(intro|before|outro|after)\s*[:\-–—]\s*", re.IGNORECASE)


@dataclass
class Line:
    """One voiceover line and its sound (48 kHz stereo WAV)."""

    where: str  # intro (before the clip) or outro (after it)
    text: str
    audio: Path
    length: float  # seconds of sound
    words: list[Word] = field(default_factory=list)  # when each word is said, from the start of the sound
    own_voice: bool = False
    engine: str = ""  # the voice that read it: gemini, elevenlabs, piper or system ("" = your recording)

    @property
    def pause(self) -> float:
        """How long the clip holds still for it."""
        return LEAD + self.length + TAIL


@dataclass
class Voiceover:
    lines: list[Line]
    source: str  # "your recording", "your words" or "the AI"

    def line(self, where: str) -> Line | None:
        return next((x for x in self.lines if x.where == where), None)

    def added(self, fps: float) -> float:
        """How much longer the reel gets (each pause is a whole number of frames)."""
        return sum(math.ceil(x.pause * fps - 1e-6) / fps for x in self.lines)

    def summary(self) -> str:
        return "\n".join(f"{'Before' if x.where == 'intro' else 'After'} the clip: {x.text}" for x in self.lines if x.text)


# ------------------------------------------------------------------ your own commentary


def own_commentary(clip: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """Your recordings (<clip>.intro.mp3, <clip>.outro.m4a...) and your text (<clip>.commentary.txt)."""
    recordings: dict[str, Path] = {}
    try:
        siblings = list(clip.parent.iterdir())
    except OSError:
        siblings = []
    for where in WHERE:
        prefix = f"{clip.stem}.{where}".lower()
        for path in sorted(siblings):
            if path.suffix.lower() in AUDIO_EXTS and path.stem.lower() == prefix and path.is_file():
                recordings[where] = path
                break
    texts: dict[str, str] = {}
    text_file = clip.with_name(clip.stem + TEXT_FILE)
    if text_file.is_file():
        try:
            texts = parse_text(text_file.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            pass
    return recordings, texts


def parse_text(text: str) -> dict[str, str]:
    """Your words: said after the clip, except the part that starts with 'intro:' (or 'before:'), which is said
    before it. 'outro:' (or 'after:') starts the part said after it again. Lines starting with # are notes."""
    found: dict[str, list[str]] = {"intro": [], "outro": []}
    current = "outro"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PREFIX_RE.match(line)
        if match:
            current = "intro" if match.group(1).lower() in ("intro", "before") else "outro"
            line = line[match.end():]
        if line:
            found[current].append(line)
    return {where: " ".join(lines) for where, lines in found.items() if lines}


def spoken(text: str, max_words: int) -> str:
    """Text ready for the voice: no emojis, hashtags, brackets or quotes, at most max_words (ending on a
    whole sentence when possible)."""
    text = _EMOJI_RE.sub("", text or "")
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", "", text)  # stage directions: [laughs] (pause) *sighs*
    text = re.sub(r"[\[\]{}()*_\"“”«»<>|~^]", "", text)
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words)
    cut = words[:max_words]
    for i in range(len(cut) - 1, max(0, len(cut) // 2) - 1, -1):
        if cut[i].endswith((".", "!", "?")):
            return " ".join(cut[: i + 1])
    return " ".join(cut).rstrip(",;:-–—") + "."


def sentences_of(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


# ------------------------------------------------------------------ sound and timing


def prepare_audio(tools: Tools, src: Path, out: Path, should_stop: Callable[[], bool] | None = None) -> float:
    """48 kHz stereo WAV with the silence at both ends trimmed and the loudness evened out. Returns its length."""
    trim = "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:detection=peak"
    run_ffmpeg(
        tools,
        ["-i", str(src), "-vn", "-map", "0:a:0", "-af",
         f"{trim},areverse,{trim},areverse,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
         "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out)],
        log_path=out.with_suffix(".log"),
        should_stop=should_stop,
    )
    with wave.open(str(out), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 48000)


def _spread(tokens: list[str], start: float, end: float) -> list[Word]:
    weights = [len(t) + 2 for t in tokens]
    total = sum(weights) or 1
    words, t = [], start
    for token, weight in zip(tokens, weights):
        length = (end - start) * weight / total
        words.append(Word(token, t, t + max(0.05, length * 0.9)))
        t += length
    return words


def align_words(text: str, heard: list[Word], length: float) -> list[Word]:
    """The written words, timed by what the speech recognition heard, so the captions show exactly the text."""
    tokens = text.split()
    if not tokens:
        return []
    heard = [Word(w.text, w.start, min(w.end, length)) for w in heard if w.start < length - 0.02]
    if not heard:
        return _spread(tokens, 0.05, max(0.1, length - 0.1))
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(a=[normalize(t) for t in tokens], b=[normalize(w.text) for w in heard], autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            w = heard[block.b + k]
            times[block.a + k] = (w.start, w.end)
    words: list[Word] = []
    n, i = len(tokens), 0
    while i < n:
        if times[i] is not None:
            words.append(Word(tokens[i], *times[i]))
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        # Words it didn't hear right fill the time between the ones around them.
        end = times[j][0] if j < n else max(heard[-1].end, (words[-1].end if words else 0.0) + 0.3 * (j - i))
        start = words[-1].end if words else max(0.0, min(heard[0].start, end - 0.3 * (j - i)))
        words += _spread(tokens[i:j], start, max(end, start + 0.05 * (j - i)))
        i = j
    if words and words[-1].end > length:  # never longer than the sound itself
        scale = length / words[-1].end
        words = [Word(w.text, w.start * scale, w.end * scale) for w in words]
    return words


# ------------------------------------------------------------------ making it


def reserve(cfg: SimpleNamespace) -> float:
    """About how long the AI's voiceover makes a reel (moments are picked that much shorter)."""
    c = cfg.commentary
    return (TYPICAL["intro"] if c.intro else 0.0) + (TYPICAL["outro"] if c.outro else 0.0) if c.enabled else 0.0


class Commentator:
    """Writes (or finds) the voiceover of a reel and speaks it."""

    def __init__(self, cfg: SimpleNamespace, tools: Tools, ai, voice: Voice,
                 transcribe: Callable[[Path], list[Word]] | None = None, should_stop: Callable[[], bool] | None = None):
        self.cfg = cfg
        self.tools = tools
        self.ai = ai
        self.voice = voice
        self.transcribe = transcribe
        self.should_stop = should_stop

    def expected(self, clip: Path) -> float:
        """The voiceover this clip will probably get (0 = none), before it's written."""
        if not self.cfg.commentary.enabled:
            return 0.0
        recordings, texts = own_commentary(clip)
        if recordings or texts:
            return 0.0  # your own: its real length is known before any cut is planned
        return reserve(self.cfg) if self.ai.available else 0.0

    def make(self, clip: Path, words: list[Word], language: str, source: str, work: Path, tag: str) -> Voiceover | None:
        """The voiceover for one reel (words: what's said in it; language: its code, '' = unknown), or None
        when there's nothing to say."""
        c = self.cfg.commentary
        if not c.enabled:
            return None
        recordings, texts = own_commentary(clip)
        if recordings or texts:
            origin = "your recording" if recordings else "your words"
            texts = {where: spoken(text, 200) for where, text in texts.items() if where not in recordings}
        else:
            texts = self._ai_texts(words, language, source)
            if not texts:
                return None
            recordings, origin = {}, "the AI"
        lines: list[Line] = []
        for where in WHERE:
            if where in recordings:
                lines.append(self._recorded(where, recordings[where], work / f"voice{tag}_{where}.wav"))
            elif texts.get(where):
                lines.append(self._say(where, texts[where], language, work / f"voice{tag}_{where}"))
        if origin == "the AI":
            lines = self._fit(lines, language, work, tag)
        lines = self._one_voice(lines, language, work, tag)
        lines = [x for x in lines if x.length > 0.2]
        if not lines:
            return None
        read = next((x.engine for x in lines if x.engine), "")
        who = "your own voice" if not read else f"voice: {self.voice.describe(language, read)}"
        log.info("Voiceover (%s, %s): %s", origin, who,
                 "; ".join(f"{'before' if x.where == 'intro' else 'after'} the clip \"{x.text}\" ({x.length:.1f}s)" for x in lines))
        return Voiceover(lines, origin)

    def _ai_texts(self, words: list[Word], language: str, source: str) -> dict[str, str]:
        c = self.cfg.commentary
        if not words or not (c.intro or c.outro) or not self.ai.available:
            return {}
        transcript = " ".join(s.text for s in split_sentences(words))
        length = words[-1].end - words[0].start
        data = self.ai.write_commentary(transcript, length, source,
                                        language_name(base_language(language)) if language else "the language of the transcript",
                                        c.persona, c.intro, c.outro)
        if not data:
            return {}
        return {where: spoken(data.get(where, ""), MAX_WORDS[where]) for where in WHERE
                if getattr(c, where) and spoken(data.get(where, ""), MAX_WORDS[where])}

    def _heard(self, audio: Path) -> list[Word]:
        if self.transcribe is None or not self.cfg.captions.enabled:
            return []
        try:
            return self.transcribe(audio)
        except Exception as exc:  # noqa: BLE001 - the captions then follow an estimate
            log.warning("Couldn't time the voiceover's captions (%s); using an estimate.", exc)
            return []

    def _recorded(self, where: str, src: Path, out: Path) -> Line:
        length = prepare_audio(self.tools, src, out, self.should_stop)
        heard = self._heard(out)
        text = " ".join(w.text for w in heard)
        return Line(where, text, out, length, heard, own_voice=True)

    def _say(self, where: str, text: str, language: str, base: Path, local: bool = False) -> Line:
        raw = self.voice.speak(text, language, base.with_name(base.name + "_raw.wav"), local=local)
        out = base.with_suffix(".wav")
        length = prepare_audio(self.tools, raw, out, self.should_stop)
        return Line(where, text, out, length, align_words(text, self._heard(out), length),
                    engine=getattr(self.voice, "last_engine", "") or "piper")

    def _one_voice(self, lines: list[Line], language: str, work: Path, tag: str) -> list[Line]:
        """If the AI voice stopped working halfway (its daily limit, say), the free voice reads the whole reel:
        two different voices in one reel would sound odd."""
        read = {x.engine for x in lines if x.engine}
        if len(read) < 2:
            return lines
        return [self._say(x.where, x.text, language, work / f"voice{tag}_{x.where}", local=True)
                if x.engine in ("gemini", "elevenlabs") else x for x in lines]

    def _fit(self, lines: list[Line], language: str, work: Path, tag: str) -> list[Line]:
        """At most commentary.max_seconds of the AI's voiceover: the take loses its middle sentences first
        (it keeps its opening and its question), then the intro goes."""
        limit = self.cfg.commentary.max_seconds
        for _ in range(4):
            if sum(x.length for x in lines) <= limit:
                break
            outro = next((x for x in lines if x.where == "outro"), None)
            parts = sentences_of(outro.text) if outro else []
            if len(parts) > 2:
                del parts[-2]
                lines[lines.index(outro)] = self._say("outro", " ".join(parts), language, work / f"voice{tag}_outro")
            elif any(x.where == "intro" for x in lines) and len(lines) > 1:
                lines = [x for x in lines if x.where != "intro"]
            else:
                break
        return lines
