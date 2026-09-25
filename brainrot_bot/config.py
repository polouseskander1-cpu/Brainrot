"""Settings: built-in defaults, overridden by whatever is in config.yaml."""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

log = logging.getLogger("brainrot")

APP_DIR = Path(__file__).resolve().parent.parent
FONTS_DIR = APP_DIR / "fonts"
WORK_DIR = APP_DIR / ".work"
MODELS_DIR = APP_DIR / "models"
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

DEFAULTS: dict[str, Any] = {
    "folders": {
        "clips": "clips",
        "gameplay": "gameplay",
        "music": "music",
        "output": "output",
    },
    "watch": {
        "poll_seconds": 10,
        "settle_seconds": 10,
        "max_attempts": 3,
        "low_priority": True,
    },
    "video": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "top_ratio": 1 / 3,
        "snap_top": True,
        "top_fill": "blur",
        "codec": "libx264",
        "crf": 19,
        "preset": "medium",
        "bitrate": "12M",
        "audio_bitrate": "192k",
        "extra_args": [],
    },
    "gameplay": {
        "skip_start": 0,
        "skip_end": 0,
        "mirror": False,
    },
    "captions": {
        "enabled": True,
        "model": "small",
        "language": "auto",
        "device": "auto",
        "font": "Montserrat Black",
        "font_size": 100,
        "uppercase": True,
        "remove_punctuation": True,
        "max_words": 3,
        "max_chars": 18,
        "color": "#FFD400",
        "stroke_color": "#FFFFFF",
        "stroke_width": 7,
        "shadow_color": "#000000",
        "shadow_opacity": 0.6,
        "shadow_offset": 6,
        "shadow_blur": 7,
        "position": 0.5,
        "animation": "pop",
        "highlight_color": "",
        "save_srt": True,
    },
    "hook": {
        "enabled": True,
        "duration": 4,
        "font": "Montserrat Black",
        "font_size": 60,
        "text_color": "#000000",
        "box_color": "#FFFFFF",
    },
    "progress_bar": {
        "enabled": True,
        "color": "#FFD400",
        "height": 10,
    },
    "audio": {
        "normalize": True,
        "loudness": -14,
        "music_volume": 0.12,
    },
    "parts": {
        "max_seconds": 0,
    },
    "tools": {
        "ffmpeg": "",
        "ffprobe": "",
    },
}


class ConfigError(Exception):
    """config.yaml has a value we can't use."""


def _merge(base: dict, override: dict, prefix: str, unknown: list[str]) -> None:
    for key, value in override.items():
        if key not in base:
            unknown.append(prefix + str(key))
            continue
        if isinstance(base[key], dict):
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ConfigError(f"'{prefix}{key}' must be a section with settings under it")
            _merge(base[key], value, f"{prefix}{key}.", unknown)
        else:
            base[key] = value


def _to_namespace(d: dict) -> SimpleNamespace:
    return SimpleNamespace(**{k: _to_namespace(v) if isinstance(v, dict) else v for k, v in d.items()})


_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _check_color(name: str, value: Any, allow_empty: bool = False) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    if not isinstance(value, str) or not _COLOR_RE.match(value):
        raise ConfigError(f"'{name}' must be a color like \"#FFD400\" (got {value!r})")
    return "#" + value.lstrip("#").upper()


def _num(name: str, value: Any, lo: float, hi: float, integer: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' must be a number (got {value!r})") from None
    if not lo <= number <= hi:
        raise ConfigError(f"'{name}' must be between {lo:g} and {hi:g} (got {value!r})")
    return int(round(number)) if integer else number


def _choice(name: str, value: Any, choices: tuple[str, ...]) -> str:
    text = str(value).strip().lower()
    if text not in choices:
        raise ConfigError(f"'{name}' must be one of {', '.join(choices)} (got {value!r})")
    return text


def _validate(c: dict) -> None:
    w = c["watch"]
    w["poll_seconds"] = _num("watch.poll_seconds", w["poll_seconds"], 1, 3600)
    w["settle_seconds"] = _num("watch.settle_seconds", w["settle_seconds"], 0, 3600)
    w["max_attempts"] = _num("watch.max_attempts", w["max_attempts"], 1, 100, integer=True)
    w["low_priority"] = bool(w["low_priority"])

    v = c["video"]
    # Video sizes must be even for H.264 / yuv420p.
    v["width"] = _num("video.width", v["width"], 144, 4096, integer=True) // 2 * 2
    v["height"] = _num("video.height", v["height"], 144, 4096, integer=True) // 2 * 2
    v["fps"] = _num("video.fps", v["fps"], 10, 120)
    v["top_ratio"] = _num("video.top_ratio", v["top_ratio"], 0.1, 0.9)
    v["snap_top"] = bool(v["snap_top"])
    v["top_fill"] = _choice("video.top_fill", v["top_fill"], ("blur", "black"))
    v["crf"] = _num("video.crf", v["crf"], 0, 51, integer=True)
    v["codec"] = str(v["codec"]).strip()
    v["preset"] = str(v["preset"]).strip()
    for key in ("bitrate", "audio_bitrate"):
        v[key] = str(v[key]).strip()
        if not re.fullmatch(r"\d+(\.\d+)?[kKmM]?", v[key]):
            raise ConfigError(f"'video.{key}' must look like 12M or 192k (got {v[key]!r})")
    if not isinstance(v["extra_args"], list):
        raise ConfigError("'video.extra_args' must be a list, e.g. [\"-tune\", \"film\"]")
    v["extra_args"] = [str(a) for a in v["extra_args"]]

    g = c["gameplay"]
    g["skip_start"] = _num("gameplay.skip_start", g["skip_start"], 0, 36000)
    g["skip_end"] = _num("gameplay.skip_end", g["skip_end"], 0, 36000)
    g["mirror"] = bool(g["mirror"])

    cap = c["captions"]
    cap["enabled"] = bool(cap["enabled"])
    cap["model"] = str(cap["model"]).strip()
    cap["language"] = str(cap["language"] or "auto").strip().lower()
    cap["device"] = _choice("captions.device", cap["device"], ("auto", "cpu", "cuda"))
    cap["font"] = str(cap["font"]).strip()
    cap["font_size"] = _num("captions.font_size", cap["font_size"], 10, 400, integer=True)
    cap["uppercase"] = bool(cap["uppercase"])
    cap["remove_punctuation"] = bool(cap["remove_punctuation"])
    cap["max_words"] = _num("captions.max_words", cap["max_words"], 1, 20, integer=True)
    cap["max_chars"] = _num("captions.max_chars", cap["max_chars"], 1, 200, integer=True)
    cap["color"] = _check_color("captions.color", cap["color"])
    cap["stroke_color"] = _check_color("captions.stroke_color", cap["stroke_color"])
    cap["stroke_width"] = _num("captions.stroke_width", cap["stroke_width"], 0, 50)
    cap["shadow_color"] = _check_color("captions.shadow_color", cap["shadow_color"])
    cap["shadow_opacity"] = _num("captions.shadow_opacity", cap["shadow_opacity"], 0, 1)
    cap["shadow_offset"] = _num("captions.shadow_offset", cap["shadow_offset"], 0, 50)
    cap["shadow_blur"] = _num("captions.shadow_blur", cap["shadow_blur"], 0, 50)
    cap["position"] = _num("captions.position", cap["position"], 0.05, 0.95)
    cap["animation"] = _choice("captions.animation", cap["animation"], ("pop", "none"))
    cap["highlight_color"] = _check_color("captions.highlight_color", cap["highlight_color"], allow_empty=True)
    cap["save_srt"] = bool(cap["save_srt"])

    h = c["hook"]
    h["enabled"] = bool(h["enabled"])
    h["duration"] = _num("hook.duration", h["duration"], 0, 36000)
    h["font"] = str(h["font"]).strip()
    h["font_size"] = _num("hook.font_size", h["font_size"], 10, 400, integer=True)
    h["text_color"] = _check_color("hook.text_color", h["text_color"])
    h["box_color"] = _check_color("hook.box_color", h["box_color"])

    p = c["progress_bar"]
    p["enabled"] = bool(p["enabled"])
    p["color"] = _check_color("progress_bar.color", p["color"])
    p["height"] = _num("progress_bar.height", p["height"], 2, 100, integer=True) // 2 * 2

    a = c["audio"]
    a["normalize"] = bool(a["normalize"])
    a["loudness"] = _num("audio.loudness", a["loudness"], -40, -5)
    a["music_volume"] = _num("audio.music_volume", a["music_volume"], 0, 2)

    parts = c["parts"]
    parts["max_seconds"] = _num("parts.max_seconds", parts["max_seconds"] or 0, 0, 36000)
    if 0 < parts["max_seconds"] < 10:
        raise ConfigError("'parts.max_seconds' must be 0 (off) or at least 10")

    t = c["tools"]
    t["ffmpeg"] = str(t["ffmpeg"] or "").strip()
    t["ffprobe"] = str(t["ffprobe"] or "").strip()


def load_config(path: Path | None = None) -> SimpleNamespace:
    """Read config.yaml (if present) on top of the defaults.

    The returned object has dotted access (``cfg.captions.font``) plus
    ``cfg.paths`` holding the resolved folders and ``cfg.config_path``.
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    merged = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{path.name} must contain settings like 'video:' and 'captions:'")
        unknown: list[str] = []
        _merge(merged, data, "", unknown)
        for key in unknown:
            log.warning("Ignoring unknown setting '%s' in %s (typo?)", key, path.name)
    elif path != DEFAULT_CONFIG_PATH:
        raise ConfigError(f"Config file not found: {path}")

    _validate(merged)
    cfg = _to_namespace(merged)

    base = path.resolve().parent

    def resolve(p: str) -> Path:
        candidate = Path(p).expanduser()
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    cfg.config_path = path
    cfg.paths = SimpleNamespace(
        clips=resolve(merged["folders"]["clips"]),
        gameplay=resolve(merged["folders"]["gameplay"]),
        music=resolve(merged["folders"]["music"]),
        output=resolve(merged["folders"]["output"]),
        state_file=base / "bot_state.json",
        logs=base / "logs",
    )
    return cfg
