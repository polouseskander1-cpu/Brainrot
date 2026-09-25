"""Turn word timings into burned-in captions (an .ass subtitle file drawn by ffmpeg) and .srt files."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from .transcribe import Word

# A caption group ends after a word that ends with one of these (the speaker pauses there).
BREAK_AFTER = (".", "!", "?", ",", ";", ":", "…")
SENTENCE_END = (".", "!", "?", "…")
# Punctuation stripped from the edges of words when captions.remove_punctuation is on.
# "?" and "!" are kept on purpose: they add emotion.
STRIP_CHARS = ".,;:\"'“”‘’„«»()[]…–—-"

# Groups separated by a silence longer than this never share the screen.
MAX_GAP = 0.6
# How long a caption stays up after its last word when nobody speaks next.
LINGER = 0.5


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float
    pause_after: bool = False


@dataclass
class Chunk:
    words: list[CaptionWord]
    start: float
    end: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Hook:
    text: str
    y: int  # top edge of the title box, in pixels
    duration: float  # seconds on screen; 0 = the whole video


def ass_safe(text: str) -> str:
    """Characters that would be read as ASS formatting codes are swapped for look-alikes."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\r", " ").replace("\n", " ")


def prepare_words(
    words: list[Word], uppercase: bool = True, remove_punctuation: bool = True, break_after: tuple[str, ...] = BREAK_AFTER
) -> list[CaptionWord]:
    prepared: list[CaptionWord] = []
    for w in words:
        raw = w.text.strip()
        shown = ass_safe(raw)
        shown = (shown.strip(STRIP_CHARS) if remove_punctuation else shown).strip()
        if uppercase:
            shown = shown.upper()
        pause = raw.rstrip("\"'”’)]").endswith(break_after)
        if not shown:  # the "word" was only punctuation
            if prepared and pause:
                prepared[-1].pause_after = True
            continue
        prepared.append(CaptionWord(shown, w.start, w.end, pause))
    return prepared


def group_words(words: list[CaptionWord], max_words: int, max_chars: int, max_gap: float = MAX_GAP) -> list[list[CaptionWord]]:
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for w in words:
        if current:
            length = sum(len(x.text) for x in current) + len(current) + len(w.text)
            if (
                len(current) >= max_words
                or length > max_chars
                or w.start - current[-1].end > max_gap
                or current[-1].pause_after
            ):
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def time_groups(groups: list[list[CaptionWord]], total: float | None = None, linger: float = LINGER, min_duration: float = 0.3) -> list[Chunk]:
    """Each group shows from its first word until the next group starts (or shortly after its last word)."""
    chunks: list[Chunk] = []
    for i, group in enumerate(groups):
        start = group[0].start
        end = max(group[-1].end + linger, start + min_duration)
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0].start)
        if total is not None:
            end = min(end, total)
        if end - start >= 0.04:
            chunks.append(Chunk(group, start, end))
    return chunks


def make_chunks(words: list[Word], cfg: SimpleNamespace, total: float | None = None) -> list[Chunk]:
    prepared = prepare_words(words, cfg.uppercase, cfg.remove_punctuation)
    return time_groups(group_words(prepared, cfg.max_words, cfg.max_chars), total)


# ---------------------------------------------------------------- ASS output


def _bgr(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return (h[4:6] + h[2:4] + h[0:2]).upper()


def style_color(hex_color: str, opacity: float = 1.0) -> str:
    alpha = round((1 - opacity) * 255)
    return f"&H{alpha:02X}{_bgr(hex_color)}"


def inline_color(hex_color: str) -> str:
    return f"&H{_bgr(hex_color)}&"


def ass_time(seconds: float) -> str:
    cs = int(round(max(seconds, 0.0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def _rich_text(chunk: Chunk, base_hex: str, highlight_hex: str) -> str:
    """Chunk text; with a highlight color, each word lights up while it is being said."""
    if not highlight_hex:
        return chunk.text
    base, lit = inline_color(base_hex), inline_color(highlight_hex)
    parts = []
    for i, w in enumerate(chunk.words):
        on = _ms(w.start - chunk.start)
        tags = f"\\1c{base}\\t({on},{on + 1},\\1c{lit})"
        if i + 1 < len(chunk.words):
            off = _ms(chunk.words[i + 1].start - chunk.start)
            tags += f"\\t({off},{off + 1},\\1c{base})"
        parts.append(f"{{{tags}}}{w.text}")
    return " ".join(parts)


def build_ass(
    chunks: list[Chunk],
    *,
    width: int,
    height: int,
    duration: float,
    captions: SimpleNamespace,
    hook_cfg: SimpleNamespace | None = None,
    hook: Hook | None = None,
) -> str:
    c = captions
    margin = round(width * 0.07)
    font = c.font.replace(",", " ")
    header = [
        "[Script Info]",
        "; Generated by the brainrot bot",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Caption,{font},{c.font_size},{style_color(c.color)},{style_color(c.color)},"
        f"{style_color(c.stroke_color)},{style_color(c.shadow_color, c.shadow_opacity)},0,0,0,0,100,100,0,0,1,"
        f"{c.stroke_width:g},0,5,{margin},{margin},0,1",
    ]
    if hook_cfg is not None:
        pad = max(8, round(hook_cfg.font_size * 0.32))
        header.append(
            f"Style: Hook,{hook_cfg.font.replace(',', ' ')},{hook_cfg.font_size},{style_color(hook_cfg.text_color)},"
            f"{style_color(hook_cfg.text_color)},{style_color(hook_cfg.box_color)},{style_color(hook_cfg.box_color)},"
            f"0,0,0,0,100,100,0,0,3,{pad},0,8,{margin + pad},{margin + pad},0,1"
        )
    header += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    events: list[str] = []
    x, y = width // 2, round(height * c.position)
    pop = "\\fscx70\\fscy70\\t(0,60,\\fscx110\\fscy110)\\t(60,130,\\fscx100\\fscy100)" if c.animation == "pop" else ""
    shadow_alpha = round((1 - c.shadow_opacity) * 255)
    for chunk in chunks:
        start, end = ass_time(chunk.start), ass_time(min(chunk.end, duration))
        if start == end:
            continue
        if c.shadow_opacity > 0:
            # Soft drop shadow: an invisible copy of the text whose shadow is blurred.
            events.append(
                f"Dialogue: 0,{start},{end},Caption,,0,0,0,,{{\\an5\\pos({x},{y}){pop}\\1a&HFF&\\3a&HFF&"
                f"\\4c{inline_color(c.shadow_color)}\\4a&H{shadow_alpha:02X}&\\shad{c.shadow_offset:g}"
                f"\\blur{c.shadow_blur:g}}}{chunk.text}"
            )
        events.append(
            f"Dialogue: 1,{start},{end},Caption,,0,0,0,,{{\\an5\\pos({x},{y}){pop}\\blur0.6}}"
            f"{_rich_text(chunk, c.color, c.highlight_color)}"
        )

    if hook is not None and hook.text.strip():
        end_time = duration if hook.duration <= 0 else min(hook.duration, duration)
        lines = [ass_safe(line.strip()) for line in hook.text.splitlines() if line.strip()]
        fade = "\\fad(0,250)" if hook.duration > 0 else ""
        events.append(
            f"Dialogue: 2,{ass_time(0)},{ass_time(end_time)},Hook,,0,0,0,,"
            f"{{\\an8\\pos({width // 2},{hook.y})\\fscx85\\fscy85\\t(0,120,\\fscx100\\fscy100){fade}}}"
            + "\\N".join(lines)
        )

    return "\n".join(header + events) + "\n"


# ---------------------------------------------------------------- SRT output


def _srt_time(seconds: float) -> str:
    ms = _ms(seconds)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words: list[Word], max_words: int = 10, max_chars: int = 60) -> str:
    """Normal-looking subtitles (for uploading to YouTube etc.), not the brainrot style."""
    prepared = prepare_words(words, uppercase=False, remove_punctuation=False, break_after=SENTENCE_END)
    chunks = time_groups(group_words(prepared, max_words, max_chars, max_gap=1.0), linger=0.6)
    lines: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        lines += [str(i), f"{_srt_time(chunk.start)} --> {_srt_time(chunk.end)}", chunk.text, ""]
    return "\n".join(lines)
