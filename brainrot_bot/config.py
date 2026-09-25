"""Settings: built-in defaults, overridden by whatever is in config.yaml."""

from __future__ import annotations

import copy
import logging
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

log = logging.getLogger("brainrot")

# When packaged as BrainrotBot.exe (PyInstaller), writable files live next to the exe and the
# bundled read-only files (fonts, default config) live in the unpacked bundle folder.
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
FONTS_DIR = RESOURCE_DIR / "fonts"
WORK_DIR = APP_DIR / ".work"
MODELS_DIR = APP_DIR / "models"
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"
STOP_FILE = APP_DIR / ".stop"
LOCK_FILE = APP_DIR / ".bot.lock"
PID_FILE = APP_DIR / ".bot.pid"

# The text of each post, per platform. {title}, {caption} and {hashtags} are filled in (upload.templates).
DEFAULT_TEMPLATES = {
    "youtube_title": "{title}",
    "youtube": "{caption}\n\n{hashtags}",
    "tiktok": "{caption} {hashtags}",
    "instagram": "{caption}\n\n{hashtags}",
    "facebook": "{caption}\n\n{hashtags}",
    "x": "{caption} {hashtags}",
    "pinterest_title": "{title}",
    "pinterest": "{caption} {hashtags}",
}

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
        "layout": "split",
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
        "style": "classic",
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
        "keyword_color": "#39FF6A",
        "save_srt": True,
        "translate_to": [],
    },
    "edit": {
        "cut_silences": True,
        "max_pause": 0.45,
        "zoom": True,
        "zoom_amount": 1.15,
        "face_tracking": True,
        "sfx": True,
        "sfx_volume": 0.6,
        "emojis": True,
        "emoji_size": 150,
        "censor": False,
        "censor_words": [],
        "censor_mode": "bleep",
        "thumbnail": True,
    },
    "hook": {
        "enabled": True,
        "auto": True,
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
        "music_duck": True,
    },
    "parts": {
        "max_seconds": 0,
    },
    "moments": {
        "enabled": True,
        "min_source_minutes": 5,
        "count": 0,
        "max_count": 12,
        "min_seconds": 20,
        "max_seconds": 60,
    },
    "links": {
        "enabled": True,
        "max_height": 1080,
        "playlist_limit": 5,
        "cookies_file": "",
    },
    "ai": {
        "enabled": True,
        "model": "claude-opus-5",
        "moments": True,
        "copy": True,
    },
    "dedupe": {
        "enabled": True,
        "similarity": 0.7,
    },
    "app": {
        "run_mode": "background",
        "autostart": False,
        "autostart_delay": 15,
        "setup_done": False,
    },
    "upload": {
        "youtube": False,
        "tiktok": False,
        "instagram": False,
        "facebook": False,
        "x": False,
        "pinterest": False,
        "hours_between_posts": 3,
        "post_times": "auto",
        "hashtags": "#fyp #viral #podcast",
        "post_translations": False,
        "youtube_privacy": "public",
        "youtube_category": 24,
        "tiktok_mode": "draft",
        "tiktok_privacy": "PUBLIC_TO_EVERYONE",
        "tiktok_redirect_port": 8765,
        "x_redirect_port": 8766,
        "pinterest_redirect_port": 8767,
        "meta_api_version": "v25.0",
        "stats": True,
        "templates": dict(DEFAULT_TEMPLATES),
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


KEEP_AS_DICT = {"templates"}  # settings that are lookup tables, not sections


def _to_namespace(d: dict) -> SimpleNamespace:
    return SimpleNamespace(**{k: _to_namespace(v) if isinstance(v, dict) and k not in KEEP_AS_DICT else v for k, v in d.items()})


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
    v["layout"] = _choice("video.layout", v["layout"], ("split", "floating", "fullscreen", "side"))
    if v["layout"] == "side" and v["height"] > v["width"]:
        log.warning("video.layout 'side' puts the clip and the gameplay next to each other; it is made for "
                    "landscape videos (video.width: 1920, video.height: 1080).")
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
    cap["animation"] = _choice("captions.animation", cap["animation"], ("pop", "bounce", "fade", "karaoke", "none"))
    cap["highlight_color"] = _check_color("captions.highlight_color", cap["highlight_color"], allow_empty=True)
    cap["keyword_color"] = _check_color("captions.keyword_color", cap["keyword_color"], allow_empty=True)
    from .styles import PRESETS

    cap["style"] = _choice("captions.style", cap["style"], tuple(PRESETS))

    e = c["edit"]
    for key in ("cut_silences", "zoom", "face_tracking", "sfx", "emojis", "censor", "thumbnail"):
        e[key] = bool(e[key])
    e["max_pause"] = _num("edit.max_pause", e["max_pause"], 0.1, 5)
    e["zoom_amount"] = _num("edit.zoom_amount", e["zoom_amount"], 1.02, 2.0)
    e["sfx_volume"] = _num("edit.sfx_volume", e["sfx_volume"], 0, 2)
    e["emoji_size"] = _num("edit.emoji_size", e["emoji_size"], 40, 600, integer=True) // 2 * 2
    if not isinstance(e["censor_words"], list):
        raise ConfigError("'edit.censor_words' must be a list, e.g. [\"word1\", \"word2\"]")
    e["censor_words"] = [str(w).strip().lower() for w in e["censor_words"] if str(w).strip()]
    e["censor_mode"] = _choice("edit.censor_mode", e["censor_mode"], ("bleep", "mute"))
    cap["save_srt"] = bool(cap["save_srt"])
    if isinstance(cap["translate_to"], str):
        cap["translate_to"] = cap["translate_to"].replace(",", " ").split()
    if not isinstance(cap["translate_to"], list):
        raise ConfigError("'captions.translate_to' must be a list of language codes, e.g. [es, ar]")
    languages = []
    for code in cap["translate_to"]:
        code = str(code).strip().lower().replace("_", "-")
        if not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]+)?", code):
            raise ConfigError(f"'captions.translate_to' has {code!r}; use language codes like es, ar, fr, pt-br")
        if code not in languages:
            languages.append(code)
    cap["translate_to"] = languages

    h = c["hook"]
    h["enabled"] = bool(h["enabled"])
    h["auto"] = bool(h["auto"])
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
    a["music_duck"] = bool(a["music_duck"])

    parts = c["parts"]
    parts["max_seconds"] = _num("parts.max_seconds", parts["max_seconds"] or 0, 0, 36000)
    if 0 < parts["max_seconds"] < 10:
        raise ConfigError("'parts.max_seconds' must be 0 (off) or at least 10")

    m = c["moments"]
    m["enabled"] = bool(m["enabled"])
    m["min_source_minutes"] = _num("moments.min_source_minutes", m["min_source_minutes"], 1, 1440)
    m["count"] = _num("moments.count", m["count"] or 0, 0, 50, integer=True)
    m["max_count"] = _num("moments.max_count", m["max_count"], 1, 50, integer=True)
    m["min_seconds"] = _num("moments.min_seconds", m["min_seconds"], 5, 600)
    m["max_seconds"] = _num("moments.max_seconds", m["max_seconds"], 10, 900)
    if m["max_seconds"] < m["min_seconds"] + 5:
        raise ConfigError("'moments.max_seconds' must be at least 5 more than 'moments.min_seconds'")

    lk = c["links"]
    lk["enabled"] = bool(lk["enabled"])
    lk["max_height"] = _num("links.max_height", lk["max_height"], 144, 4320, integer=True)
    lk["playlist_limit"] = _num("links.playlist_limit", lk["playlist_limit"], 1, 500, integer=True)
    lk["cookies_file"] = str(lk["cookies_file"] or "").strip()

    ai = c["ai"]
    for key in ("enabled", "moments", "copy"):
        ai[key] = bool(ai[key])
    ai["model"] = str(ai["model"] or "").strip()
    if not ai["model"]:
        raise ConfigError("'ai.model' can't be empty (default: claude-opus-5)")

    d = c["dedupe"]
    d["enabled"] = bool(d["enabled"])
    d["similarity"] = _num("dedupe.similarity", d["similarity"], 0.3, 1.0)

    t = c["tools"]
    t["ffmpeg"] = str(t["ffmpeg"] or "").strip()
    t["ffprobe"] = str(t["ffprobe"] or "").strip()

    app = c["app"]
    app["run_mode"] = _choice("app.run_mode", app["run_mode"], ("background", "window"))
    app["autostart"] = bool(app["autostart"])
    app["autostart_delay"] = _num("app.autostart_delay", app["autostart_delay"], 0, 600)
    app["setup_done"] = bool(app["setup_done"])

    up = c["upload"]
    for platform in ("youtube", "tiktok", "instagram", "facebook", "x", "pinterest"):
        up[platform] = bool(up[platform])
    up["hours_between_posts"] = _num("upload.hours_between_posts", up["hours_between_posts"], 0, 168)
    times = up["post_times"]
    if times in (None, "", False, "off", "any", "anytime"):
        up["post_times"] = []
    elif isinstance(times, str) and times.strip().lower() == "auto":
        up["post_times"] = "auto"
    else:
        if isinstance(times, str):
            times = times.replace(",", " ").split()
        if not isinstance(times, list):
            raise ConfigError("'upload.post_times' must be auto, [] (any time) or a list like [\"12:00\", \"18:30\"]")
        cleaned = []
        for value in times:
            text = str(value).strip()
            if isinstance(value, int) and 0 <= value < 1440 * 60:  # YAML 1.1 reads 12:00 as a number of minutes
                text = f"{value // 60 % 24:02d}:{value % 60:02d}"
            if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text):
                raise ConfigError(f"'upload.post_times' has {value!r}; use times like \"12:00\" or \"18:30\"")
            cleaned.append(text)
        up["post_times"] = cleaned
    up["stats"] = bool(up["stats"])
    if not isinstance(up["templates"], dict):
        raise ConfigError("'upload.templates' must be a section, e.g. tiktok: \"{caption} {hashtags}\"")
    up["templates"] = {k: str(v if v is not None else DEFAULT_TEMPLATES.get(k, "")) for k, v in up["templates"].items()}
    up["hashtags"] = str(up["hashtags"] or "").strip()
    up["post_translations"] = bool(up["post_translations"])
    up["youtube_privacy"] = _choice("upload.youtube_privacy", up["youtube_privacy"], ("public", "unlisted", "private"))
    up["youtube_category"] = _num("upload.youtube_category", up["youtube_category"], 1, 100, integer=True)
    up["tiktok_mode"] = _choice("upload.tiktok_mode", up["tiktok_mode"], ("draft", "direct"))
    up["tiktok_privacy"] = str(up["tiktok_privacy"]).strip().upper()
    if up["tiktok_privacy"] not in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"):
        raise ConfigError("'upload.tiktok_privacy' must be PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR or SELF_ONLY")
    for key in ("tiktok_redirect_port", "x_redirect_port", "pinterest_redirect_port"):
        up[key] = _num(f"upload.{key}", up[key], 1024, 65535, integer=True)
    up["meta_api_version"] = str(up["meta_api_version"]).strip()
    if not re.fullmatch(r"v\d+\.\d+", up["meta_api_version"]):
        raise ConfigError("'upload.meta_api_version' must look like v25.0")


def ensure_config_file() -> None:
    """The .exe ships a default config.yaml inside its bundle; put an editable copy next to the exe."""
    bundled = RESOURCE_DIR / "config.yaml"
    if not DEFAULT_CONFIG_PATH.exists() and bundled.exists() and bundled != DEFAULT_CONFIG_PATH:
        shutil.copyfile(bundled, DEFAULT_CONFIG_PATH)


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
        credentials=base / "credentials",
        logs=base / "logs",
    )
    return cfg


# ---------------------------------------------------------------- editing config.yaml in place


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    plain = re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", text) and text.lower() not in ("true", "false", "yes", "no", "on", "off", "null")
    return text if plain else "'" + text.replace("'", "''") + "'"


def _split_comment(rest: str) -> tuple[str, str]:
    """'value   # comment' -> ('value', '   # comment'), ignoring # inside quotes."""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
    return rest.rstrip(), ""


def update_config_file(path: Path, updates: dict[str, dict[str, Any]]) -> None:
    """Change a few settings in config.yaml while keeping every other line and comment as it is."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for section, values in updates.items():
        start = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", line)), None)
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{section}:")
            start = len(lines) - 1
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1  # keep blank lines between sections outside this block
        for key, value in values.items():
            pattern = re.compile(rf"^(\s+){re.escape(key)}:(\s*)(.*)$")
            for i in range(start + 1, end):
                match = pattern.match(lines[i])
                if match and not lines[i].lstrip().startswith("#"):
                    _, comment = _split_comment(match.group(3))
                    lines[i] = f"{match.group(1)}{key}: {_yaml_scalar(value)}{comment}"
                    break
            else:
                lines.insert(end, f"  {key}: {_yaml_scalar(value)}")
                end += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
