"""Builds the ffmpeg command that assembles one reel."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .gameplay import Segment

AUDIO_RATE = 48000
LAYOUTS = ("split", "floating", "fullscreen", "side")


def even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _double_rate(rate: str) -> str:
    """"12M" -> "24M" (ffmpeg rate strings with an optional k/M suffix)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", rate.strip())
    if not match:
        return rate
    return f"{float(match.group(1)) * 2:g}{match.group(2)}"


def fit_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Largest even size with the source's shape that fits in the box (never cropped)."""
    if src_w <= 0 or src_h <= 0:
        return box_w, box_h
    scale = min(box_w / src_w, box_h / src_h)
    return min(box_w, even(src_w * scale)), min(box_h, even(src_h * scale))


@dataclass
class Layout:
    width: int
    height: int
    fps: float
    top_h: int  # split: height of the clip area at the top
    fit: bool  # the clip is shown whole inside its area, with a fill (blur/black) around it
    mode: str = "split"
    clip_box: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h of the clip area
    clip_size: tuple[int, int] = (0, 0)  # the clip scaled to fit its area
    game_box: tuple[int, int, int, int] | None = None
    card_border: int = 0  # floating: frame around the clip

    @property
    def game_h(self) -> int:
        return self.game_box[3] if self.game_box else 0

    @property
    def seam_y(self) -> int:
        """Where the progress bar goes."""
        if self.mode == "split":
            return self.top_h
        if self.mode == "floating":
            return self.clip_box[1] + self.clip_box[3] + 16
        return 0

    @property
    def hook_y(self) -> int:
        if self.mode == "split":
            return self.top_h + round(self.height * 0.035)
        if self.mode == "floating":
            return self.clip_box[1] + self.clip_box[3] + round(self.height * 0.035)
        return round(self.height * 0.12)


def compute_layout(video: SimpleNamespace, clip_width: int, clip_height: int) -> Layout:
    """split: clip in the top area (video.top_ratio of the height) and gameplay below. If the clip, scaled
    to full width, almost fits that area anyway (16:9 clips do), the area is sized to fit it exactly.
    floating: gameplay fills the screen, the clip floats on it as a framed card near the top.
    fullscreen: only the clip, cropped to fill the screen and following the speaker's face.
    side: clip on the left, gameplay on the right (for landscape videos)."""
    w, h, fps = video.width, video.height, video.fps
    mode = getattr(video, "layout", "split")
    cw, ch = clip_width or w, clip_height or h

    if mode == "fullscreen":
        return Layout(w, h, fps, h, False, mode, (0, 0, w, h), (w, h), None)
    if mode == "side":
        half = even(w / 2)
        size = fit_size(cw, ch, half, h)
        return Layout(w, h, fps, h, size != (half, h), mode, (0, 0, half, h), size, (half, 0, w - half, h))
    if mode == "floating":
        border = max(4, round(w * 0.008))
        max_w, max_h = even(w * 0.9) - 2 * border, even(h * 0.42) - 2 * border
        size = fit_size(cw, ch, max_w, max_h)
        box_w, box_h = size[0] + 2 * border, size[1] + 2 * border
        box = (even((w - box_w) / 2), even(h * 0.08), box_w, box_h)
        return Layout(w, h, fps, box[1] + box_h, False, mode, box, size, (0, 0, w, h), border)

    area = even(h * video.top_ratio)
    if clip_width > 0 and clip_height > 0:
        natural = w * clip_height / clip_width
        if abs(natural - area) < 2 or (video.snap_top and 0.85 * area <= natural <= 1.15 * area):
            top = min(even(natural), h - 2)
            return Layout(w, h, fps, top, False, "split", (0, 0, w, top), (w, top), (0, top, w, h - top))
    size = fit_size(cw, ch, w, area)
    return Layout(w, h, fps, area, True, "split", (0, 0, w, area), size, (0, area, w, h - area))


@dataclass
class Zoom:
    """A punch-in on the clip, in reel time, aimed at (cx, cy) (0..1 of the clip frame)."""

    start: float
    end: float
    cx: float = 0.5
    cy: float = 0.45


@dataclass
class EmojiShow:
    path: Path
    start: float
    end: float


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
    # Which parts of the clip to keep (source times). Empty = clip_start .. clip_start + duration.
    intervals: list[tuple[float, float]] = field(default_factory=list)
    clip_size: tuple[int, int] = (0, 0)  # clip as displayed (width, height)
    zooms: list[Zoom] = field(default_factory=list)
    zoom_amount: float = 1.15
    reframe: list[tuple[float, float]] = field(default_factory=list)  # fullscreen: (reel time, face x 0..1)
    emojis: list[EmojiShow] = field(default_factory=list)
    emoji_y: int = 0
    emoji_size: int = 150
    sfx_track: Path | None = None
    mute: list[tuple[float, float]] = field(default_factory=list)  # reel times where the voice is silenced
    duck_music: bool = True

    def kept(self) -> list[tuple[float, float]]:
        return self.intervals or [(self.clip_start, self.clip_start + self.duration)]


class _Inputs:
    def __init__(self):
        self.args: list[str] = []
        self.count = 0

    def add(self, *args: str) -> int:
        self.args += list(args)
        self.count += 1
        return self.count - 1


def _between(spans: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)


def _piecewise(spans: list[tuple[float, float, float]], default: float) -> str:
    """ffmpeg expression: value v while between(t,a,b), else default."""
    expr = f"{default:.1f}"
    for a, b, v in reversed(spans):
        expr = f"if(between(t,{a:.3f},{b:.3f}),{v:.1f},{expr})"
    return expr


def _linear(points: list[tuple[float, float]]) -> str:
    """ffmpeg expression following (t, value) points, moving smoothly between them."""
    if not points:
        return "0"
    expr = f"{points[-1][1]:.1f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        if t1 - t0 < 1e-3:
            continue
        expr = f"if(lt(t,{t1:.3f}),{v0:.1f}+({v1 - v0:.1f})*(t-{t0:.3f})/{t1 - t0:.3f},{expr})"
    return f"if(lt(t,{points[0][0]:.3f}),{points[0][1]:.1f},{expr})"


def _clip_filters(job: RenderJob, inputs: _Inputs, graph: list[str]) -> tuple[str, str]:
    """The kept parts of the clip joined together: (video label, audio label)."""
    fps = job.layout.fps
    kept = job.kept()
    first, last = kept[0][0], kept[-1][1]
    clip = inputs.add(*(["-ss", f"{first:.3f}"] if first > 0 else []), "-t", f"{last - first + 0.2:.3f}", "-i", str(job.clip))
    frames = [(round((a - first) * fps), round((b - first) * fps)) for a, b in kept]
    samples = [(round(f0 * AUDIO_RATE / fps), round(f1 * AUDIO_RATE / fps)) for f0, f1 in frames]
    video_src = f"[{clip}:v:0]setpts=PTS-STARTPTS,fps={fps:g}"
    audio_src = f"[{clip}:a:0]asetpts=PTS-STARTPTS,aresample={AUDIO_RATE}" if job.clip_has_audio else None

    hold = "tpad=stop_mode=clone:stop_duration=1"  # never run out of picture before the sound ends
    if len(kept) == 1:
        graph.append(f"{video_src},trim=end_frame={frames[0][1]},setpts=PTS-STARTPTS,{hold}[cv]")
        if audio_src:
            graph.append(f"{audio_src},atrim=end_sample={samples[0][1]},asetpts=PTS-STARTPTS[ca]")
            return "[cv]", "[ca]"
        return "[cv]", ""

    n = len(kept)
    graph.append(f"{video_src},split={n}" + "".join(f"[cvs{i}]" for i in range(n)))
    for i, (f0, f1) in enumerate(frames):
        graph.append(f"[cvs{i}]trim=start_frame={f0}:end_frame={f1},setpts=PTS-STARTPTS[cvt{i}]")
    if audio_src:
        graph.append(f"{audio_src},asplit={n}" + "".join(f"[cas{i}]" for i in range(n)))
        for i, (s0, s1) in enumerate(samples):
            graph.append(f"[cas{i}]atrim=start_sample={s0}:end_sample={s1},asetpts=PTS-STARTPTS[cat{i}]")
        graph.append("".join(f"[cvt{i}][cat{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cvj][ca]")
        graph.append(f"[cvj]{hold}[cv]")
        return "[cv]", "[ca]"
    graph.append("".join(f"[cvt{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cvj]")
    graph.append(f"[cvj]{hold}[cv]")
    return "[cv]", ""


def _zoom_filters(job: RenderJob, src: str, width: int, height: int, graph: list[str], tag: str) -> str:
    """Punch-in zooms: a cropped, enlarged copy of the picture shown during each zoom moment."""
    if not job.zooms:
        return src
    amount = max(1.01, job.zoom_amount)
    zw, zh = even(width / amount), even(height / amount)
    xs, ys = [], []
    for z in job.zooms:
        x = min(max(z.cx * width - zw / 2, 0), width - zw)
        y = min(max((z.cy + 0.08) * height - zh / 2, 0), height - zh)
        xs.append((z.start, z.end, x))
        ys.append((z.start, z.end, y))
    spans = [(z.start, z.end) for z in job.zooms]
    graph.append(f"{src}split=2[{tag}zb][{tag}zi]")
    graph.append(
        f"[{tag}zi]crop=w={zw}:h={zh}:x='{_piecewise(xs, (width - zw) / 2)}':y='{_piecewise(ys, (height - zh) / 2)}',"
        f"scale={width}:{height}:flags=bicubic,setsar=1[{tag}zu]"
    )
    graph.append(f"[{tag}zb][{tag}zu]overlay=0:0:enable='{_between(spans)}'[{tag}z]")
    return f"[{tag}z]"


def _clip_area(job: RenderJob, cfg: SimpleNamespace, src: str, graph: list[str]) -> str:
    """The clip scaled into its area (+ zoom, + blurred/black fill or frame)."""
    L = job.layout
    box_w, box_h = L.clip_box[2], L.clip_box[3]

    if L.mode == "fullscreen":
        sw, sh = job.clip_size if job.clip_size[0] else (L.width, L.height)
        sw, sh = even(sw), even(sh)
        target = L.width / L.height
        if sw / sh > target:  # wider than the screen: follow the face left/right
            crop_w, crop_h = even(sh * target), sh
            points = [(t, min(max(cx * sw - crop_w / 2, 0), sw - crop_w)) for t, cx in job.reframe] or [(0.0, (sw - crop_w) / 2)]
            x_expr, y_expr = _linear(points), "0"
        else:  # taller than the screen: keep the top part (where faces usually are)
            crop_w, crop_h = sw, even(sw / target)
            x_expr, y_expr = "0", f"{max(0, (sh - crop_h) * 0.3):.1f}"
        graph.append(
            f"{src}scale={sw}:{sh}:flags=lanczos,setsar=1,crop=w={crop_w}:h={crop_h}:x='{x_expr}':y='{y_expr}',"
            f"scale={L.width}:{L.height}:flags=lanczos,setsar=1[full]"
        )
        return _zoom_filters(job, "[full]", L.width, L.height, graph, "f")

    fw, fh = L.clip_size
    graph.append(f"{src}scale={fw}:{fh}:flags=lanczos,setsar=1[cfit]")
    current = _zoom_filters(job, "[cfit]", fw, fh, graph, "c")

    if L.mode == "floating":
        b = L.card_border
        graph.append(f"{current}pad={fw + 2 * b}:{fh + 2 * b}:{b}:{b}:color=white[card]")
        return "[card]"
    if (fw, fh) == (box_w, box_h):
        return current
    if cfg.video.top_fill == "black":
        graph.append(f"{current}pad={box_w}:{box_h}:(ow-iw)/2:(oh-ih)/2:color=black[area]")
        return "[area]"
    # Blurred, darkened copy of the clip behind it. Blurring a small copy is fast and just as smooth.
    bw, bh = even(box_w / 4), even(box_h / 4)
    graph.append(f"{current}split=2[area_bg_in][area_fg]")
    graph.append(
        f"[area_bg_in]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:2,"
        f"scale={box_w}:{box_h},eq=brightness=-0.12:saturation=1.2,setsar=1[area_bg]"
    )
    graph.append("[area_bg][area_fg]overlay=(W-w)/2:(H-h)/2[area]")
    return "[area]"


def _gameplay_filters(job: RenderJob, inputs: _Inputs, graph: list[str]) -> str:
    L = job.layout
    gw, gh = L.game_box[2], L.game_box[3]
    ratio = gw / gh
    flip = ",hflip" if job.mirror else ""
    labels = []
    for seg in job.segments:
        idx = inputs.add("-ss", f"{seg.start:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path))
        # Crop the middle of the gameplay to the shape of its area, then scale it to fill.
        graph.append(
            f"[{idx}:v:0]setpts=PTS-STARTPTS,fps={L.fps:g},"
            f"crop='min(iw,ih*{ratio:.6f})':'min(ih,iw/{ratio:.6f})',"
            f"scale={gw}:{gh}:flags=bicubic,setsar=1{flip}[g{idx}]"
        )
        labels.append(f"[g{idx}]")
    if len(labels) == 1:
        return labels[0]
    graph.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[game]")
    return "[game]"


def build_command(job: RenderJob, cfg: SimpleNamespace) -> list[str]:
    """ffmpeg arguments (without the ffmpeg executable itself)."""
    L = job.layout
    inputs = _Inputs()
    graph: list[str] = []
    total = sum(b - a for a, b in job.kept())
    duration = f"{total:.3f}"

    video, voice_src = _clip_filters(job, inputs, graph)
    area = _clip_area(job, cfg, video, graph)

    if L.mode == "fullscreen" or L.game_box is None:
        current = area
    else:
        game = _gameplay_filters(job, inputs, graph)
        if L.mode == "split":
            graph.append(f"{area}{game}vstack=inputs=2[stack]")
        elif L.mode == "side":
            graph.append(f"{area}{game}hstack=inputs=2[stack]")
        else:  # floating card on full-screen gameplay
            graph.append(f"{game}{area}overlay=x={L.clip_box[0]}:y={L.clip_box[1]}[stack]")
        current = "[stack]"

    if cfg.progress_bar.enabled:
        bar_h = cfg.progress_bar.height
        y = max(0, min(L.height - bar_h, L.seam_y - bar_h // 2))
        color = cfg.progress_bar.color.lstrip("#")
        graph.append(f"{current}drawbox=x=0:y={y}:w={L.width}:h={bar_h}:color=black@0.55:t=fill[bar_track]")
        graph.append(f"color=c=0x{color}:s={L.width}x{bar_h}:r={L.fps:g}[bar_fill]")
        graph.append(f"[bar_track][bar_fill]overlay=x='-w+w*t/{duration}':y={y}:shortest=1[bar]")
        current = "[bar]"

    # Emojis over the captions: one input per emoji, shown at its moments.
    by_emoji: dict[Path, list[tuple[float, float]]] = {}
    for show in job.emojis:
        by_emoji.setdefault(show.path, []).append((show.start, show.end))
    for n, (path, spans) in enumerate(by_emoji.items()):
        idx = inputs.add("-loop", "1", "-framerate", f"{L.fps:g}", "-t", duration, "-i", str(path))
        graph.append(f"[{idx}:v:0]scale={job.emoji_size}:{job.emoji_size},format=rgba[emoji{n}]")
        graph.append(
            f"{current}[emoji{n}]overlay=x=(W-w)/2:y={max(0, job.emoji_y)}:enable='{_between(spans)}':shortest=1[emo{n}]"
        )
        current = f"[emo{n}]"

    if job.ass_file:
        graph.append(f"{current}ass=filename={job.ass_file}:fontsdir={job.fonts_dir}[subs]")
        current = "[subs]"
    graph.append(f"{current}format=yuv420p[vout]")

    # ---- audio: the voice, evened out by a gentle compressor and loudness-normalized like the big
    # accounts (-14 LUFS), censored words silenced, plus sound effects and optional ducked music.
    if not voice_src:
        idx = inputs.add("-f", "lavfi", "-t", duration, "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo")
        voice_src = f"[{idx}:a]"
    chain = "anull"
    if cfg.audio.normalize and job.clip_has_audio:
        chain = (
            "acompressor=threshold=-24dB:ratio=3:attack=5:release=120:knee=4"
            f",loudnorm=I={cfg.audio.loudness:g}:TP=-1.5:LRA=11,aresample={AUDIO_RATE}"
        )
    chain += ",aformat=sample_fmts=fltp:channel_layouts=stereo"
    if job.mute:
        chain += f",volume=volume=0:enable='{_between(job.mute)}'"
    graph.append(f"{voice_src}{chain},apad[voice]")
    mix = ["[voice]"]

    if job.music is not None:
        music_args = (["-stream_loop", "-1"] if job.music_loop else []) + (["-ss", f"{job.music_start:.3f}"] if job.music_start > 0 else [])
        idx = inputs.add(*music_args, "-i", str(job.music))
        fade_out = max(0.0, total - 1.5)
        graph.append(
            f"[{idx}:a:0]atrim=0:{duration},asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={cfg.audio.music_volume:g},"
            f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out:.3f}:d=1.5[music_raw]"
        )
        if job.duck_music:
            graph.append("[voice]asplit=2[voice_main][voice_key]")
            graph.append("[music_raw][voice_key]sidechaincompress=threshold=0.015:ratio=12:attack=15:release=400[music]")
            mix[0] = "[voice_main]"
        else:
            graph.append("[music_raw]anull[music]")
        mix.append("[music]")

    if job.sfx_track is not None:
        idx = inputs.add("-i", str(job.sfx_track))
        graph.append(f"[{idx}:a:0]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo[sfx]")
        mix.append("[sfx]")

    if len(mix) == 1:
        graph.append(f"{mix[0]}anull[aout]")
    else:
        # A limiter keeps voice + effects + music from ever clipping.
        graph.append(
            f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.93:attack=5:release=60:level=0:latency=1[aout]"
        )

    args = inputs.args + ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", duration]
    v = cfg.video
    codec = getattr(v, "resolved_codec", "") or v.codec
    args += encoder_args(codec, v)
    args += ["-pix_fmt", "yuv420p", "-r", f"{L.fps:g}"]
    args += ["-c:a", "aac", "-b:a", v.audio_bitrate, "-ar", str(AUDIO_RATE), "-ac", "2"]
    args += ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", "-max_muxing_queue_size", "4096"]
    args += list(v.extra_args)
    args.append(str(job.output))
    return args


def encoder_args(codec: str, v: SimpleNamespace) -> list[str]:
    """Quality settings for each video encoder (software x264/x265 or a graphics card's encoder)."""
    if codec in ("libx264", "libx265"):
        args = ["-c:v", codec, "-preset", v.preset, "-crf", str(v.crf)]
        # Quality-based, but capped so busy gameplay can't balloon the file size.
        args += ["-maxrate", v.bitrate, "-bufsize", _double_rate(v.bitrate)]
        return args + (["-profile:v", "high"] if codec == "libx264" else ["-tag:v", "hvc1"])
    if codec.endswith("_nvenc"):
        return ["-c:v", codec, "-preset", "p5", "-rc", "vbr", "-cq", str(min(51, v.crf + 3)), "-b:v", "0",
                "-maxrate", v.bitrate, "-bufsize", _double_rate(v.bitrate), "-profile:v", "high"]
    if codec.endswith("_qsv"):
        return ["-c:v", codec, "-preset", "slower", "-global_quality", str(min(51, v.crf + 3)), "-maxrate", v.bitrate]
    if codec.endswith("_amf"):
        return ["-c:v", codec, "-quality", "quality", "-rc", "vbr_peak", "-b:v", v.bitrate, "-maxrate", v.bitrate]
    return ["-c:v", codec, "-b:v", v.bitrate]
