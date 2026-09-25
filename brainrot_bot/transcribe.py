"""Speech-to-text with word timings (faster-whisper, runs offline on your machine)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .media import StopRequested

# Windows without "developer mode" can't make symlinks; the model download works anyway, so skip the warning.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# The model download site prints "set a HF_TOKEN" / "install hf_xet" notices that don't apply to this bot.
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

log = logging.getLogger("brainrot")


@dataclass
class Word:
    text: str
    start: float
    end: float


def tidy_words(words: list[Word], min_len: float = 0.05) -> list[Word]:
    """Sort words and make their timings sane (no negative, zero-length or backwards words).
    A lone '%' (the speech model sometimes writes "90 %") is joined to the number before it."""
    cleaned: list[Word] = []
    for w in sorted(words, key=lambda w: w.start):
        text = w.text.strip()
        if not text:
            continue
        if text in ("%", "%.", "%,", "%?", "%!") and cleaned and w.start - cleaned[-1].end < 0.6:
            cleaned[-1].text += text
            cleaned[-1].end = max(cleaned[-1].end, w.end)
            continue
        start = max(0.0, w.start)
        if cleaned and start < cleaned[-1].start:
            start = cleaned[-1].start
        end = max(w.end, start + min_len)
        if cleaned and cleaned[-1].end > start:
            cleaned[-1].end = max(cleaned[-1].start + min_len, start)
        cleaned.append(Word(text, start, end))
    return cleaned


class Transcriber:
    """Loads the Whisper model once and keeps it in memory while the bot runs."""

    def __init__(self, model: str = "small", language: str = "auto", device: str = "auto", models_dir: Path | None = None):
        self.model_name = model
        self.language = None if language in ("", "auto") else language
        self.device = device
        self.models_dir = models_dir
        self._model = None
        self.last_language: str | None = None  # language heard in the last clip

    def _create(self, device: str):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed. Run: pip install -r requirements.txt") from exc
        where = "GPU if available" if device == "auto" else device.upper()
        log.info("Loading speech model '%s' (%s). The first time, it is downloaded; this can take a few minutes...", self.model_name, where)
        kwargs = {}
        if self.models_dir:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            kwargs["download_root"] = str(self.models_dir)
        compute_type = "int8" if device == "cpu" else "auto"
        return WhisperModel(self.model_name, device=device, compute_type=compute_type, **kwargs)

    def load(self) -> None:
        if self._model is None:
            try:
                self._model = self._create(self.device)
            except Exception as exc:
                if self.device == "cpu":
                    raise
                log.warning("Couldn't use the GPU for speech-to-text (%s). Using the CPU instead.", exc)
                self.device = "cpu"
                self._model = self._create("cpu")

    def transcribe(self, audio_path: Path, should_stop: Callable[[], bool] | None = None) -> list[Word]:
        self.load()
        try:
            return self._run(audio_path, should_stop)
        except StopRequested:
            raise
        except Exception as exc:
            if self.device == "cpu":
                raise
            # Typical cause: CUDA is detected but cuDNN/cuBLAS libraries are missing.
            log.warning("Speech-to-text failed on the GPU (%s). Retrying on the CPU.", exc)
            self.device = "cpu"
            self._model = self._create("cpu")
            return self._run(audio_path, should_stop)

    def _run(self, audio_path: Path, should_stop: Callable[[], bool] | None = None) -> list[Word]:
        segments, info = self._model.transcribe(
            str(audio_path),
            language=self.language,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            beam_size=5,
        )
        words: list[Word] = []
        total = float(getattr(info, "duration", 0) or 0)
        next_report = 600.0
        for segment in segments:  # the actual work happens while iterating
            if should_stop is not None and should_stop():
                raise StopRequested()
            for w in segment.words or []:
                if w.word and w.word.strip():
                    words.append(Word(w.word.strip(), float(w.start), float(w.end)))
            if total > 1200 and segment.end >= next_report:  # long videos: show progress every 10 minutes
                log.info("  listened to %d of %d minutes", segment.end // 60, total // 60)
                next_report += 600
        self.last_language = getattr(info, "language", None)
        log.info("Heard %d words (language: %s)", len(words), self.last_language or "?")
        return tidy_words(words)
