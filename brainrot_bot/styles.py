"""Ready-made caption looks. 'classic' is the yellow / white stroke / soft shadow look from config.yaml;
the others replace those settings when chosen (captions.style in config.yaml, or the menu)."""

from __future__ import annotations

import copy
from types import SimpleNamespace

PRESETS: dict[str, dict] = {
    "classic": {},
    "hormozi": {
        "font": "Montserrat Black", "font_size": 96, "color": "#FFFFFF", "stroke_color": "#000000",
        "stroke_width": 9, "shadow_opacity": 0.7, "uppercase": True, "max_words": 3, "max_chars": 18,
        "animation": "pop", "highlight_color": "#FFE500", "keyword_color": "#22E55B", "position": 0.55,
    },
    "mrbeast": {
        "font": "Luckiest Guy", "font_size": 112, "color": "#FFFFFF", "stroke_color": "#000000",
        "stroke_width": 10, "shadow_opacity": 0.8, "uppercase": True, "max_words": 2, "max_chars": 14,
        "animation": "bounce", "highlight_color": "#FF3B3B", "keyword_color": "#FFD400", "position": 0.5,
    },
    "minimal": {
        "font": "Poppins SemiBold", "font_size": 64, "color": "#FFFFFF", "stroke_color": "#000000",
        "stroke_width": 0, "shadow_opacity": 0.85, "shadow_blur": 4, "shadow_offset": 3, "uppercase": False,
        "max_words": 5, "max_chars": 30, "animation": "fade", "highlight_color": "", "keyword_color": "",
        "position": 0.72,
    },
    "karaoke": {
        "font": "Montserrat Black", "font_size": 84, "color": "#FFFFFF", "stroke_color": "#000000",
        "stroke_width": 7, "shadow_opacity": 0.6, "uppercase": True, "max_words": 4, "max_chars": 24,
        "animation": "karaoke", "highlight_color": "#FFD400", "keyword_color": "", "position": 0.5,
    },
}

DESCRIPTIONS = {
    "classic": "yellow text, white outline, soft drop shadow (your original look)",
    "hormozi": "white text, thick black outline, green keywords, yellow word being spoken",
    "mrbeast": "big comic font, 2 words at a time, bouncy, red word being spoken",
    "minimal": "clean smaller white text near the bottom, fades in",
    "karaoke": "words fill with yellow as they are spoken",
}


def apply_style(captions: SimpleNamespace) -> SimpleNamespace:
    """Captions settings with the chosen preset applied on top."""
    style = getattr(captions, "style", "classic")
    merged = copy.copy(captions)
    for key, value in PRESETS.get(style, {}).items():
        setattr(merged, key, value)
    return merged
