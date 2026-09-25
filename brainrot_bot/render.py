"""Builds the ffmpeg command that assembles one reel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .gameplay import Segment


def even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _double_rate(rate: str) -> str:
    """"12M" -> "24M" (ffmpeg rate strings with an optional k/M suffix)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", rate.strip())
    if not match:
        return rate
    return f"{float(match.group(1)) * 2:g}{match.group(2)}"


@dataclass
class Layout:
    width: int
    height: int
    fps: float
    top_h: int  # height of the clip area at the top
    fit: bool  # True: clip is shown whole inside the area with a fill around it

    @property
    def game_h(self) -> int:
        return self.height - self.top_h


def compute_layout(video: SimpleNamespace, clip_width: int, clip_height: int) -> Layout:
    """Top area = video.top_ratio of the height. If the clip (scaled to full width) almost fits that
    area anyway, e.g. a 16:9 clip in the top third, the area is resized to fit it exactly (no bars)."""
    w, h = video.width, video.height
    area = even(h * video.top_ratio)
    if clip_width > 0 and clip_height > 0:
        natural = w * clip_height / clip_width
        if abs(natural - area) < 2 or (video.snap_top and 0.85 * area <= natural <= 1.15 * area):
            return Layout(w, h, video.fps, min(even(natural), h - 2), fit=False)
    return Layout(w, h, video.fps, area, fit=True)


@dataclass
class RenderJob:
    clip: Path
    clip_start: float
    duration: float
    clip_has_audio: bool
    layout: Layout
    segments: list[Segment]
    output: Path
    ass_file: str | None = None  # relative to the ffmpeg working directory
    fonts_dir: str = "fonts"
    music: Path | None = None
    music_start: float = 0.0
    music_loop: bool = False
    mirror: bool = False


def _top_filters(job: RenderJob, cfg: SimpleNamespace, graph: list[str]) -> None:
    L = job.layout
    src = f"[0:v:0]setpts=PTS-STARTPTS,fps={L.fps:g}"
    if not L.fit:
        graph.append(f"{src},scale={L.width}:{L.top_h}:flags=lanczos,setsar=1[top]")
        return
    fit = f"scale={L.width}:{L.top_h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1"
    if cfg.video.top_fill == "black":
        graph.append(f"{src},{fit},pad={L.width}:{L.top_h}:(ow-iw)/2:(oh-ih)/2:color=black[top]")
        return
    # Blurred, darkened copy of the clip behind it. Blurring a small copy is fast and just as smooth.
    bw, bh = even(L.width / 4), even(L.top_h / 4)
    graph.append(f"{src},split=2[top_bg_in][top_fg_in]")
    graph.append(
        f"[top_bg_in]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:2,"
        f"scale={L.width}:{L.top_h},eq=brightness=-0.12:saturation=1.2,setsar=1[top_bg]"
    )
    graph.append(f"[top_fg_in]{fit}[top_fg]")
    graph.append("[top_bg][top_fg]overlay=(W-w)/2:(H-h)/2[top]")


def _gameplay_filters(job: RenderJob, graph: list[str]) -> str:
    L = job.layout
    ratio = L.width / L.game_h
    flip = ",hflip" if job.mirror else ""
    labels = []
    for i in range(1, len(job.segments) + 1):
        # Crop the middle of the gameplay to the shape of the bottom area, then scale it to fill.
        graph.append(
            f"[{i}:v:0]setpts=PTS-STARTPTS,fps={L.fps:g},"
            f"crop='min(iw,ih*{ratio:.6f})':'min(ih,iw/{ratio:.6f})',"
            f"scale={L.width}:{L.game_h}:flags=bicubic,setsar=1{flip}[g{i}]"
        )
        labels.append(f"[g{i}]")
    if len(labels) == 1:
        return labels[0]
    graph.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[game]")
    return "[game]"


def build_command(job: RenderJob, cfg: SimpleNamespace) -> list[str]:
    """ffmpeg arguments (without the ffmpeg executable itself)."""
    L = job.layout
    duration = f"{job.duration:.3f}"
    args: list[str] = []

    if job.clip_start > 0:
        args += ["-ss", f"{job.clip_start:.3f}"]
    args += ["-t", duration, "-i", str(job.clip)]
    for seg in job.segments:
        args += ["-ss", f"{seg.start:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)]
    next_input = 1 + len(job.segments)

    music_input = None
    if job.music is not None:
        if job.music_loop:
            args += ["-stream_loop", "-1"]
        if job.music_start > 0:
            args += ["-ss", f"{job.music_start:.3f}"]
        args += ["-i", str(job.music)]
        music_input, next_input = next_input, next_input + 1

    silence_input = None
    if not job.clip_has_audio:
        args += ["-f", "lavfi", "-t", duration, "-i", "anullsrc=r=48000:cl=stereo"]
        silence_input, next_input = next_input, next_input + 1

    graph: list[str] = []
    _top_filters(job, cfg, graph)
    game = _gameplay_filters(job, graph)
    graph.append(f"[top]{game}vstack=inputs=2[stack]")
    current = "[stack]"

    if cfg.progress_bar.enabled:
        bar_h = cfg.progress_bar.height
        y = max(0, L.top_h - bar_h // 2)
        color = cfg.progress_bar.color.lstrip("#")
        graph.append(f"{current}drawbox=x=0:y={y}:w={L.width}:h={bar_h}:color=black@0.55:t=fill[bar_track]")
        graph.append(f"color=c=0x{color}:s={L.width}x{bar_h}:r={L.fps:g}[bar_fill]")
        graph.append(f"[bar_track][bar_fill]overlay=x='-w+w*t/{duration}':y={y}:shortest=1[bar]")
        current = "[bar]"

    if job.ass_file:
        graph.append(f"{current}ass=filename={job.ass_file}:fontsdir={job.fonts_dir}[subs]")
        current = "[subs]"
    graph.append(f"{current}format=yuv420p[vout]")

    # Audio: the clip's voice, evened out by a gentle compressor and loudness-normalized like the big
    # accounts (-14 LUFS), plus optional music.
    voice = f"[{silence_input}:a]" if silence_input is not None else "[0:a:0]"
    chain = "asetpts=PTS-STARTPTS,aresample=48000"
    if cfg.audio.normalize and job.clip_has_audio:
        chain += (
            ",acompressor=threshold=-24dB:ratio=3:attack=5:release=120:knee=4"
            f",loudnorm=I={cfg.audio.loudness:g}:TP=-1.5:LRA=11,aresample=48000"
        )
    chain += ",aformat=sample_fmts=fltp:channel_layouts=stereo,apad"
    graph.append(f"{voice}{chain}[voice]")
    if music_input is not None:
        fade_out = max(0.0, job.duration - 1.5)
        graph.append(
            f"[{music_input}:a:0]atrim=0:{duration},asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={cfg.audio.music_volume:g},"
            f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out:.3f}:d=1.5[music]"
        )
        graph.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        graph.append("[voice]anull[aout]")

    args += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", duration]

    v = cfg.video
    if v.codec in ("libx264", "libx265"):
        args += ["-c:v", v.codec, "-preset", v.preset, "-crf", str(v.crf)]
        # Quality-based, but capped so busy gameplay can't balloon the file size.
        args += ["-maxrate", v.bitrate, "-bufsize", _double_rate(v.bitrate)]
        args += ["-profile:v", "high"] if v.codec == "libx264" else ["-tag:v", "hvc1"]
    else:
        args += ["-c:v", v.codec, "-b:v", v.bitrate]
    args += ["-pix_fmt", "yuv420p", "-r", f"{L.fps:g}"]
    args += ["-c:a", "aac", "-b:a", v.audio_bitrate, "-ar", "48000", "-ac", "2"]
    args += ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "-max_muxing_queue_size", "4096"]
    args += list(v.extra_args)
    args.append(str(job.output))
    return args
