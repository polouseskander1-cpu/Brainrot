"""Finding the speaker's face, so zooms and full-screen crops stay on them.

Uses the tiny open-source UltraFace detector (1 MB, MIT license) through onnxruntime, which the app
already has for speech recognition. Frames are sampled a couple of times per second with ffmpeg.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import RESOURCE_DIR
from .media import Tools, popen_kwargs

log = logging.getLogger("brainrot")

MODEL = RESOURCE_DIR / "assets" / "models" / "ultraface-rfb-320.onnx"
IN_W, IN_H = 320, 240


@dataclass
class Face:
    t: float  # seconds in the source clip
    cx: float  # center, 0..1 of the frame width/height
    cy: float
    size: float  # box width, 0..1 of the frame width


class FaceFinder:
    def __init__(self, model: Path = MODEL):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = 3  # the model file triggers harmless optimizer notices
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def detect(self, rgb, threshold: float = 0.7) -> list[tuple[float, float, float, float, float]]:
        """Faces in one 320x240 RGB frame as (x1, y1, x2, y2, score), coordinates 0..1."""
        import numpy as np

        image = (rgb.astype(np.float32) - 127.0) / 128.0
        batch = np.transpose(image, (2, 0, 1))[np.newaxis, ...]
        scores, boxes = self.session.run(None, {self.input: batch})
        probs = scores[0][:, 1]
        keep = probs > threshold
        return _nms(boxes[0][keep], probs[keep])


def _nms(boxes, scores, iou_limit: float = 0.3) -> list[tuple[float, float, float, float, float]]:
    order = list(scores.argsort()[::-1])
    picked = []
    while order:
        best = order.pop(0)
        x1, y1, x2, y2 = (float(v) for v in boxes[best])
        picked.append((x1, y1, x2, y2, float(scores[best])))
        remaining = []
        for other in order:
            a1, b1, a2, b2 = (float(v) for v in boxes[other])
            iw, ih = max(0.0, min(x2, a2) - max(x1, a1)), max(0.0, min(y2, b2) - max(y1, b1))
            inter = iw * ih
            union = (x2 - x1) * (y2 - y1) + (a2 - a1) * (b2 - b1) - inter
            if union <= 0 or inter / union < iou_limit:
                remaining.append(other)
        order = remaining
    return picked


_finder: FaceFinder | None = None


def _get_finder() -> FaceFinder | None:
    global _finder
    if _finder is None:
        try:
            _finder = FaceFinder()
        except Exception as exc:  # noqa: BLE001 - face tracking is a bonus, never a reason to fail
            log.warning("Face detection unavailable (%s); zooms will aim at the middle.", exc)
            return None
    return _finder


def find_faces(tools: Tools, clip: Path, start: float, duration: float, per_second: float = 2.0) -> list[Face]:
    """The biggest face in frames sampled every 1/per_second seconds of [start, start+duration)."""
    finder = _get_finder()
    if finder is None:
        return []
    import numpy as np

    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", str(clip), "-vf", f"fps={per_second:g},scale={IN_W}:{IN_H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=900, **popen_kwargs()).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Couldn't sample frames for face tracking: %s", exc)
        return []
    frame_bytes = IN_W * IN_H * 3
    faces = []
    for i in range(len(raw) // frame_bytes):
        frame = np.frombuffer(raw, dtype=np.uint8, count=frame_bytes, offset=i * frame_bytes).reshape(IN_H, IN_W, 3)
        found = finder.detect(frame)
        if found:
            x1, y1, x2, y2, _ = max(found, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
            faces.append(Face(start + i / per_second, (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1))
    return faces


def face_at(faces: list[Face], t: float, max_distance: float = 1.5) -> Face | None:
    """The face seen closest to time t (source time), if any within max_distance seconds."""
    best = min(faces, key=lambda f: abs(f.t - t), default=None)
    return best if best is not None and abs(best.t - t) <= max_distance else None


def track_x(faces: list[Face], start: float, end: float, step: float = 1.0, jump: float = 0.22) -> list[tuple[float, float]]:
    """Smoothed horizontal face position over time (source time, 0..1): follows small moves gently,
    jumps when the speaker changes (like a camera cut)."""
    points: list[tuple[float, float]] = []
    current = None
    t = start
    while t <= end + 1e-6:
        face = face_at(faces, t, max_distance=step)
        target = face.cx if face else (current if current is not None else 0.5)
        if current is None or abs(target - current) > jump:
            current = target
        else:
            current += (target - current) * 0.35
        points.append((t, current))
        t += step
    return points
