"""Cover picture for each reel: a strong frame from the reel with the title in big caption-style letters."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

from .captions import ass_safe, inline_color, style_color
from .config import APP_DIR
from .media import Tools, run_ffmpeg


def cover_ass(title: str, width: int, height: int, captions: SimpleNamespace) -> str:
    size = round(width * 0.105)
    lines = textwrap.wrap(" ".join(title.split()).upper(), width=14)[:4] or ["WATCH THIS"]
    margin = round(width * 0.06)
    font = captions.font.replace(",", " ")
    text = "\\N".join(ass_safe(line) for line in lines)
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cover,{font},{size},{style_color(captions.color)},{style_color(captions.color)},"
        f"{style_color(captions.stroke_color)},{style_color('#000000', 0.7)},0,0,0,0,100,100,0,0,1,"
        f"{max(4, captions.stroke_width * 1.3):g},0,5,{margin},{margin},0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,0:00:00.00,0:00:10.00,Cover,,0,0,0,,{{\\an5\\pos({width // 2},{round(height * 0.44)})"
        f"\\1a&HFF&\\3a&HFF&\\4c{inline_color('#000000')}\\4a&H50&\\shad10\\blur12}}{text}",
        f"Dialogue: 1,0:00:00.00,0:00:10.00,Cover,,0,0,0,,{{\\an5\\pos({width // 2},{round(height * 0.44)})\\blur0.8}}{text}",
        "",
    ])


def make_cover(tools: Tools, reel: Path, at: float, title: str, out: Path, *, width: int, height: int,
               captions: SimpleNamespace, work_dir: Path, fonts_dir: str) -> Path:
    ass = work_dir / "cover.ass"
    ass.write_text(cover_ass(title, width, height, captions), encoding="utf-8")
    rel = ass.relative_to(APP_DIR).as_posix()
    run_ffmpeg(tools, [
        "-ss", f"{max(0.0, at):.3f}", "-i", str(reel), "-frames:v", "1",
        "-vf", f"eq=brightness=-0.06:saturation=1.2,ass=filename={rel}:fontsdir={fonts_dir}",
        "-q:v", "3", str(out),
    ], log_path=work_dir / "ffmpeg_cover.log", cwd=APP_DIR)
    return out
