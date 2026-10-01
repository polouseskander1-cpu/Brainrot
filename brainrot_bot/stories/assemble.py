"""The picture and sound of a story before captions: the AI shots (or gameplay) cut to the narration.

The result is a full-screen video with the narration as its sound. The reel itself (captions, hook, music,
progress bar, cover) is then made from it by the same steps as every other reel.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..gameplay import Segment
from ..media import Tools, run_ffmpeg

AUDIO = "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
MAX_SLOW = 1.3  # a shot shorter than its scene is slowed down by at most this much, then holds its last picture
CAMERA_MOVE = 0.12  # pictures without a shot zoom this much over their scene


@dataclass
class SceneMedia:
    picture: Path  # the keyframe
    video: Path | None  # the shot made from it (None: the picture moves with a slow camera)
    seconds: float  # how long the scene is on screen
    video_seconds: float = 0.0  # how long the shot is
    has_sound: bool = False


def _encode(out: Path, fps: float) -> list[str]:
    """A nearly lossless in-between file: the reel is encoded from it once more."""
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-r", f"{fps:g}",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)]


def _narration(graph: list[str], index: int, total: float, label: str = "nar") -> str:
    graph.append(f"[{index}:a:0]{AUDIO},apad,atrim=duration={total:.3f},asetpts=PTS-STARTPTS[{label}]")
    return f"[{label}]"


def ai_scenes(tools: Tools, scenes: list[SceneMedia], narration: Path, out: Path, *, width: int, height: int, fps: float,
              ambience: float, should_stop: Callable[[], bool] | None = None) -> Path:
    """The shots one after the other, each as long as its part of the narration, with the shots' own sound
    (wind, rain, footsteps) quietly under the voice."""
    args: list[str] = []
    graph: list[str] = []
    total = sum(s.seconds for s in scenes)
    pieces = []
    for k, scene in enumerate(scenes):
        d = scene.seconds
        if scene.video is not None:
            args += ["-i", str(scene.video)]
            slow = min(MAX_SLOW, d / scene.video_seconds) if scene.video_seconds and scene.video_seconds < d else 1.0
            graph.append(
                f"[{k}:v:0]setpts=PTS-STARTPTS,setpts={slow:.4f}*PTS,fps={fps:g},"
                f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1,"
                f"tpad=stop_mode=clone:stop_duration={d + 1:.3f},trim=duration={d:.3f},setpts=PTS-STARTPTS[v{k}]"
            )
            if scene.has_sound:
                tempo = f",atempo={1 / slow:.4f}" if slow > 1.0001 else ""
                graph.append(f"[{k}:a:0]asetpts=PTS-STARTPTS{tempo},{AUDIO},apad,atrim=duration={d:.3f},asetpts=PTS-STARTPTS[a{k}]")
            else:
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f},{AUDIO}[a{k}]")
        else:
            frames = max(1, round(d * fps))
            args += ["-loop", "1", "-framerate", f"{fps:g}", "-t", f"{d + 0.5:.3f}", "-i", str(scene.picture)]
            # Even scenes slowly zoom in, odd ones out, toward the middle.
            zoom = (f"1+{CAMERA_MOVE}*on/{frames}" if k % 2 == 0 else f"{1 + CAMERA_MOVE}-{CAMERA_MOVE}*on/{frames}")
            graph.append(
                f"[{k}:v:0]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width * 2}:{height * 2},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                f"s={width}x{height}:fps={fps:g},trim=duration={d:.3f},setpts=PTS-STARTPTS,setsar=1[v{k}]"
            )
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f},{AUDIO}[a{k}]")
        pieces.append(f"[v{k}][a{k}]")
    graph.append("".join(pieces) + f"concat=n={len(scenes)}:v=1:a=1[vout][amb]")
    voice = len(scenes)
    args += ["-i", str(narration)]
    _narration(graph, voice, total)
    if ambience > 0:
        graph.append(f"[amb]volume={ambience:g}[ambq]")
        graph.append("[nar]asplit=2[narm][nark]")
        graph.append("[ambq][nark]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=600[ambd]")
        graph.append("[narm][ambd]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        graph.append("[amb]anullsink")
        graph.append("[nar]anull[aout]")
    cmd = args + ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}"] + _encode(out, fps)
    run_ffmpeg(tools, cmd, log_path=out.with_suffix(".log"), duration=total, label="  putting the scenes together:",
               should_stop=should_stop)
    return out


def gameplay(tools: Tools, segments: list[Segment], narration: Path, length: float, out: Path, *, width: int, height: int,
             fps: float, mirror: bool = False, should_stop: Callable[[], bool] | None = None) -> Path:
    """Full-screen gameplay under the narration (the game's own sound isn't used)."""
    args: list[str] = []
    graph: list[str] = []
    ratio = width / height
    flip = ",hflip" if mirror else ""
    labels = []
    for k, seg in enumerate(segments):
        args += ["-ss", f"{seg.start:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)]
        graph.append(
            f"[{k}:v:0]setpts=PTS-STARTPTS,fps={fps:g},crop='min(iw,ih*{ratio:.6f})':'min(ih,iw/{ratio:.6f})',"
            f"scale={width}:{height}:flags=bicubic,setsar=1{flip}[g{k}]"
        )
        labels.append(f"[g{k}]")
    graph.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,tpad=stop_mode=clone:stop_duration=2,"
                 f"trim=duration={length:.3f},setpts=PTS-STARTPTS[vout]")
    args += ["-i", str(narration)]
    _narration(graph, len(segments), length, "aout")
    cmd = args + ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", f"{length:.3f}"] + _encode(out, fps)
    run_ffmpeg(tools, cmd, log_path=out.with_suffix(".log"), duration=length, label="  putting the gameplay together:",
               should_stop=should_stop)
    return out
