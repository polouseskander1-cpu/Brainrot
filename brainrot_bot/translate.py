"""Extra reels with the captions translated into other languages (captions.translate_to in config.yaml).

The AI translates the reel sentence by sentence; each translated sentence is shown over the time the
original sentence was spoken, split into caption-sized pieces."""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace

from .moments import split_sentences
from .transcribe import Word

log = logging.getLogger("brainrot")

NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic",
    "fa": "Persian", "ur": "Urdu", "he": "Hebrew", "hi": "Hindi", "bn": "Bengali", "id": "Indonesian",
    "ms": "Malay", "vi": "Vietnamese", "th": "Thai", "zh": "Chinese (Simplified)", "ja": "Japanese",
    "ko": "Korean", "el": "Greek", "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
    "ro": "Romanian", "cs": "Czech", "hu": "Hungarian", "tl": "Filipino", "sw": "Swahili",
}
# Fonts for scripts the bundled caption fonts don't have (Cairo is bundled; the others come with Windows).
SCRIPT_FONTS = {
    "ar": "Cairo Black", "fa": "Cairo Black", "ur": "Cairo Black", "he": "Arial", "hi": "Nirmala UI",
    "bn": "Nirmala UI", "th": "Leelawadee UI", "zh": "Microsoft YaHei", "ja": "Yu Gothic", "ko": "Malgun Gothic",
    "el": "Arial",
}
NO_SPACES = {"zh", "ja", "th"}  # written without spaces between words
PIECE_CHARS = 8  # caption piece length for those languages


def language_name(code: str) -> str:
    return NAMES.get(code.lower(), code)


def base_language(code: str | None) -> str:
    return (code or "").lower().split("-")[0].split("_")[0]


def translated_words(sentences: list[tuple[float, float]], texts: list[str], language: str) -> list[Word]:
    """Spread each translated sentence over the time its original was spoken (longer words get more time)."""
    words: list[Word] = []
    lang = base_language(language)
    for (start, end), text in zip(sentences, texts):
        if lang in NO_SPACES:
            compact = "".join(text.split())
            pieces = [compact[i : i + PIECE_CHARS] for i in range(0, len(compact), PIECE_CHARS)]
        else:
            pieces = text.split()
        if not pieces:
            continue
        weights = [len(p) + 2 for p in pieces]
        total = sum(weights)
        t = start
        for piece, weight in zip(pieces, weights):
            length = (end - start) * weight / total
            words.append(Word(piece, t, t + max(0.05, length * 0.92)))
            t += length
    return words


def caption_settings(style: SimpleNamespace, language: str) -> SimpleNamespace:
    """The reel's caption look, adjusted for the language: a font that has its letters, no per-word colors."""
    adjusted = copy.copy(style)
    lang = base_language(language)
    if lang in SCRIPT_FONTS:
        adjusted.font = SCRIPT_FONTS[lang]
    if lang in NO_SPACES:
        adjusted.max_words = 1
    # Word colors are based on English keywords, and they would split right-to-left text into pieces.
    adjusted.keyword_color = ""
    adjusted.highlight_color = ""
    if adjusted.animation == "karaoke":
        adjusted.animation = "pop"
    return adjusted


def translate_reel(ai, words: list[Word], extras: dict[str, str], language: str) -> tuple[list[Word], dict[str, str]] | None:
    """The reel's words in another language (same timing), plus translations of `extras` (hook, title...),
    in one AI request. None if the AI isn't available or the translation failed."""
    if ai is None or not ai.available or not words:
        return None
    sentences = split_sentences(words)
    keys = [key for key, text in extras.items() if text and text.strip()]
    lines = [extras[key] for key in keys] + [s.text for s in sentences]
    texts = ai.translate(lines, language_name(language))
    if texts is None:
        return None
    translated_extras = {key: texts[i] for i, key in enumerate(keys)}
    body = texts[len(keys):]
    return translated_words([(s.start, s.end) for s in sentences], body, language), translated_extras
