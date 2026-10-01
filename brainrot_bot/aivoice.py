"""Natural AI voices from the cloud for the voiceover: Google Gemini TTS and ElevenLabs.

In blind listening tests (Artificial Analysis, October 2026) Google's Gemini 3.8 Flash TTS is among the
three most natural voices, at a fraction of the others' price, with a free tier: the best value, and the
AI voice the menu suggests. ElevenLabs' v4 is the most natural of all, at about five times the price.
Each is used only when its key is connected. If a request fails (no internet, the free tier's daily limit
used up), the free voice on this PC reads that reel instead, so a reel never waits for a voice.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import wave
from pathlib import Path

from .uploads import http

log = logging.getLogger("brainrot")

GEMINI = "gemini"
ELEVENLABS = "elevenlabs"
NAMES = {GEMINI: "Google Gemini", ELEVENLABS: "ElevenLabs"}
KEY_PAGES = {GEMINI: "https://aistudio.google.com/apikey", ELEVENLABS: "https://elevenlabs.io/app/settings/api-keys"}
ENV_KEYS = {GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY"), ELEVENLABS: ("ELEVENLABS_API_KEY",)}

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.8-flash-tts"
# A selection of Google's 30 prebuilt voices that suit short-video narration (descriptions are Google's).
GEMINI_VOICES = {
    "Puck": "upbeat, male", "Charon": "informative, male", "Fenrir": "excitable, male", "Orus": "firm, male",
    "Kore": "firm, female", "Zephyr": "bright, female", "Leda": "youthful, female", "Aoede": "breezy, female",
}
GEMINI_DEFAULT = "Puck"
GEMINI_RATE = 24000  # older models answer with bare 16-bit mono PCM at this rate instead of a WAV file

ELEVENLABS_URL = "https://api.elevenlabs.io"
ELEVENLABS_MODEL = "eleven_v4"
ELEVENLABS_DEFAULT = ("JBFqnCBsd6RMkjVDRZzb", "George")  # the voice in ElevenLabs' own quick start

STYLE = ("Like a podcast host reacting to a clip in a short video: natural, warm and confident, a little "
         "excited, at a brisk conversational pace. A real person talking, not an announcer.")
_VOICE_RE = {GEMINI: re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,40}$"), ELEVENLABS: re.compile(r"^[A-Za-z0-9]{10,40}$")}


class AIVoiceError(Exception):
    """The AI voice couldn't say it. wait: seconds before trying it again (the free voice reads meanwhile)."""

    def __init__(self, message: str, wait: float = 300.0):
        super().__init__(message)
        self.wait = wait


def parse(setting: str) -> tuple[str, str] | None:
    """'gemini:Puck' -> ('gemini', 'Puck'); 'elevenlabs' -> ('elevenlabs', default voice); other voices -> None."""
    provider, _, voice = (setting or "").partition(":")
    provider = provider.strip().lower()
    if provider not in NAMES:
        return None
    voice = voice.strip() or (GEMINI_DEFAULT if provider == GEMINI else ELEVENLABS_DEFAULT[0])
    return provider, voice


def valid(setting: str) -> bool:
    found = parse(setting)
    return found is not None and bool(_VOICE_RE[found[0]].match(found[1]))


def api_key(provider: str, credentials) -> str:
    stored = (credentials.get(provider) if credentials is not None else None) or {}
    key = str(stored.get("api_key") or "").strip()
    for name in ENV_KEYS[provider]:
        key = key or os.environ.get(name, "").strip()
    return key


def _base(provider: str) -> str:
    override = os.environ.get("BRAINROT_GEMINI_BASE_URL" if provider == GEMINI else "BRAINROT_ELEVENLABS_BASE_URL")
    return (override or (GEMINI_URL if provider == GEMINI else ELEVENLABS_URL)).rstrip("/")


def _problem(resp: http.Response) -> str:
    data = resp.json()
    error = data.get("error") if isinstance(data.get("error"), dict) else data.get("detail")
    if isinstance(error, dict):
        return " ".join(str(error.get("message") or error.get("status") or error).split())[:300]
    if isinstance(error, list) and error:  # validation errors
        return " ".join(str(error[0].get("msg", error[0]) if isinstance(error[0], dict) else error[0]).split())[:300]
    return resp.text(200) or f"error {resp.status}"


def _failure(provider: str, resp: http.Response) -> AIVoiceError:
    """What a failed answer means, and how long to leave the AI voice alone."""
    name, why = NAMES[provider], _problem(resp)
    low = why.lower()
    # First: ElevenLabs reports "out of credits" with 401, the same code as a wrong key.
    if resp.status == 429 or "quota" in low or "exceed" in low:
        return AIVoiceError(f"{name}'s limit is used up for now ({why})", wait=3600)
    if resp.status in (401, 403) or "api key not valid" in low or "invalid_api_key" in low:
        return AIVoiceError(f"{name} refused the key ({why}). Connect it again in the menu", wait=24 * 3600)
    if resp.status == 404:
        return AIVoiceError(f"{name} doesn't know this voice or model ({why}). Pick the voice again in the menu",
                            wait=6 * 3600)
    if "location" in low and "not supported" in low:
        return AIVoiceError(f"{name} isn't available in your country on the free tier ({why})", wait=24 * 3600)
    return AIVoiceError(f"{name} answered with error {resp.status} ({why})", wait=600 if resp.status >= 500 else 3600)


# ------------------------------------------------------------------ Google Gemini


def _audio_of(data: dict) -> str:
    """The base64 audio in an Interactions API answer: the last audio item of the model's output steps."""
    found = ""
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("type", "model_output") != "model_output":
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "audio" and item.get("data"):
                found = item["data"]
    if not found and isinstance(data.get("output_audio"), dict):
        found = data["output_audio"].get("data") or ""
    return found


def gemini_speak(key: str, voice: str, text: str, out: Path, *, model: str = GEMINI_MODEL, style: str = STYLE,
                 timeout: float = 180) -> Path:
    """Say text with a Gemini voice into out (a WAV file)."""
    content: dict = {"type": "text", "text": text}
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style}]
    body = {
        "model": model,
        "input": [{"type": "user_input", "content": [content]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    try:
        resp = http.request("POST", f"{_base(GEMINI)}/interactions", headers={"x-goog-api-key": key}, json_body=body,
                            timeout=timeout)
    except http.UploadError as exc:
        raise AIVoiceError(f"couldn't reach Google ({exc})") from exc
    if not resp.ok:
        raise _failure(GEMINI, resp)
    try:
        audio = base64.b64decode(_audio_of(resp.json()), validate=False)
    except (ValueError, TypeError) as exc:
        raise AIVoiceError(f"Google's answer had no usable sound ({exc})") from exc
    if len(audio) < 1000:
        raise AIVoiceError("Google's answer had no sound in it")
    out = out.with_suffix(".wav")
    if audio[:4] == b"RIFF":
        out.write_bytes(audio)
    else:  # bare PCM (the models before 3.8)
        with wave.open(str(out), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(GEMINI_RATE)
            handle.writeframes(audio)
    return out


def check_gemini_key(key: str, model: str = GEMINI_MODEL) -> str:
    """'' if the key works and can use the voice model, otherwise what's wrong. Costs nothing."""
    try:
        resp = http.request("GET", f"{_base(GEMINI)}/models/{model}", headers={"x-goog-api-key": key}, timeout=30)
    except http.UploadError as exc:
        return f"couldn't reach Google ({exc})"
    if resp.ok:
        return ""
    if resp.status == 404:
        return f"the key works, but the voice model '{model}' wasn't found (check commentary.gemini_model)"
    return str(_failure(GEMINI, resp)).split(". Connect")[0]


# ------------------------------------------------------------------ ElevenLabs


def elevenlabs_speak(key: str, voice_id: str, text: str, out: Path, *, model: str = ELEVENLABS_MODEL, speed: float = 1.0,
                     timeout: float = 180) -> Path:
    """Say text with an ElevenLabs voice into out (an MP3 file)."""
    body = {"text": text, "model_id": model, "voice_settings": {"speed": round(min(1.2, max(0.7, speed)), 2)}}
    try:
        resp = http.request("POST", f"{_base(ELEVENLABS)}/v1/text-to-speech/{voice_id}", params={"output_format": "mp3_44100_128"},
                            headers={"xi-api-key": key, "Accept": "audio/mpeg"}, json_body=body, timeout=timeout)
    except http.UploadError as exc:
        raise AIVoiceError(f"couldn't reach ElevenLabs ({exc})") from exc
    if not resp.ok:
        raise _failure(ELEVENLABS, resp)
    if len(resp.body) < 1000:
        raise AIVoiceError("ElevenLabs' answer had no sound in it")
    out = out.with_suffix(".mp3")
    out.write_bytes(resp.body)
    return out


def elevenlabs_voices(key: str) -> tuple[str, list[dict]]:
    """('', the voices this account can use) or (what's wrong, []). Each voice: id, name, about."""
    try:
        resp = http.request("GET", f"{_base(ELEVENLABS)}/v2/voices", params={"page_size": 100}, headers={"xi-api-key": key},
                            timeout=30)
    except http.UploadError as exc:
        return f"couldn't reach ElevenLabs ({exc})", []
    if not resp.ok:
        return str(_failure(ELEVENLABS, resp)).split(". Connect")[0], []
    voices = []
    for v in resp.json().get("voices") or []:
        if not isinstance(v, dict) or not v.get("voice_id"):
            continue
        labels = v.get("labels") if isinstance(v.get("labels"), dict) else {}
        about = ", ".join(str(labels[k]) for k in ("gender", "age", "accent", "use_case", "description") if labels.get(k))
        voices.append({"id": str(v["voice_id"]), "name": str(v.get("name") or v["voice_id"]), "about": about})
    return "", voices
