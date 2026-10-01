"""Pictures and video shots from Google's Gemini API, with the same key as the Gemini voice.

- Pictures: Nano Banana 2 (gemini-3.1-flash-image) through the Interactions API. Up to 14 reference pictures
  keep the characters the same in every scene.
- Shots: Veo 3.1 (predictLongRunning): a 4, 6 or 8 second clip, animated from its keyframe, with sound.
  Generating takes from 11 seconds to a few minutes, so a shot is started first and collected later.
  Blocked shots (Google's safety filters) aren't charged.

Neither has a free tier: they need a Google AI Studio key with billing turned on.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .. import aivoice
from ..uploads import http

log = logging.getLogger("brainrot")

IMAGE_MODEL = "gemini-3.1-flash-image"
VIDEO_MODEL = "veo-3.1-fast-generate-preview"
# US dollars per picture (1K) and per second of video (Google's prices, October 2026).
IMAGE_PRICES = {"gemini-3.1-flash-image": 0.067, "gemini-3.1-flash-lite-image": 0.034, "gemini-3-pro-image": 0.134}
VIDEO_PRICES = {
    "veo-3.1-lite-generate-preview": {"720p": 0.05, "1080p": 0.08},
    "veo-3.1-fast-generate-preview": {"720p": 0.10, "1080p": 0.12},
    "veo-3.1-generate-preview": {"720p": 0.40, "1080p": 0.40},
}
VIDEO_NAMES = {"veo-3.1-lite-generate-preview": "Veo 3.1 Lite", "veo-3.1-fast-generate-preview": "Veo 3.1 Fast",
               "veo-3.1-generate-preview": "Veo 3.1"}
NO_VIDEO = "none"  # stories.video_model: pictures only, moved by a slow camera
SECONDS = (4, 6, 8)  # the lengths Veo makes
BILLING_PAGE = "https://aistudio.google.com/usage"


class GoogleError(Exception):
    """Google couldn't make it. wait: seconds before trying again; blocked: refused by the safety filters."""

    def __init__(self, message: str, wait: float = 600.0, blocked: bool = False):
        super().__init__(message)
        self.wait = wait
        self.blocked = blocked


def image_price(model: str) -> float:
    return IMAGE_PRICES.get(model, IMAGE_PRICES[IMAGE_MODEL])


def video_price(model: str, resolution: str) -> float:
    """Dollars per second of video (0 for pictures only)."""
    if model == NO_VIDEO:
        return 0.0
    prices = VIDEO_PRICES.get(model, VIDEO_PRICES[VIDEO_MODEL])
    return prices.get(resolution, prices["720p"])


def shot_seconds(needed: float, resolution: str = "720p") -> int:
    """The shortest length Veo makes that covers this much (8 if none does). 1080p shots are always 8 seconds."""
    if resolution != "720p":
        return SECONDS[-1]
    return next((s for s in SECONDS if s >= needed - 0.05), SECONDS[-1])


def _failure(resp: http.Response, what: str) -> GoogleError:
    why = aivoice._problem(resp)
    low = why.lower()
    if "billing" in low or "limit: 0" in low or "free tier" in low or "free_tier" in low:
        return GoogleError(f"Google needs billing turned on for {what} (there's no free tier): {BILLING_PAGE} ({why})",
                           wait=24 * 3600)
    if resp.status == 429 or "quota" in low or "exhausted" in low:
        return GoogleError(f"Google's limit for {what} is used up for now ({why})", wait=3600)
    if resp.status in (401, 403) or "api key not valid" in low:
        return GoogleError(f"Google refused the key ({why}). Connect it again in the menu (Stories)", wait=24 * 3600)
    if resp.status == 404:
        return GoogleError(f"Google doesn't know this model ({why}). Check stories.image_model / stories.video_model",
                           wait=6 * 3600)
    if "location" in low and "not supported" in low:
        return GoogleError(f"Google doesn't offer {what} in your country ({why})", wait=24 * 3600)
    if "safety" in low or "blocked" in low or "policy" in low or "responsible ai" in low:
        return GoogleError(f"Google's safety filters refused this {what.rstrip('s')} ({why})", wait=0, blocked=True)
    return GoogleError(f"Google answered with error {resp.status} ({why})", wait=300 if resp.status >= 500 else 1800)


# ------------------------------------------------------------------ pictures (Nano Banana 2)


def _mime(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/webp" if path.suffix.lower() == ".webp" else "image/png"


def _image_of(data: dict) -> tuple[str, str]:
    """(base64 data, mime type) of the last picture the model made."""
    found = ("", "")
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("type", "model_output") != "model_output":
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "image" and item.get("data"):
                found = (item["data"], item.get("mime_type") or "image/png")
    if not found[0] and isinstance(data.get("output_image"), dict):
        found = (data["output_image"].get("data") or "", data["output_image"].get("mime_type") or "image/png")
    return found


def _refused(data: dict) -> str:
    """Why no picture came back, when the model said so in words."""
    texts = []
    for step in data.get("steps") or []:
        for item in (step.get("content") or []) if isinstance(step, dict) else []:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                texts.append(str(item["text"]))
    status = str(data.get("status") or "")
    return " ".join(" ".join(texts).split())[:200] or status


def make_picture(key: str, prompt: str, out: Path, *, model: str = IMAGE_MODEL, references: list[Path] | None = None,
                 aspect: str = "9:16", size: str = "1K", timeout: float = 300) -> Path:
    """Draw one picture into out (the suffix follows what Google sends: .png or .jpg). references: pictures it
    must stay true to (the characters), in the order the prompt mentions them."""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for ref in references or []:
        content.append({"type": "image", "mime_type": _mime(ref), "data": base64.b64encode(ref.read_bytes()).decode()})
    body = {"model": model, "input": content,
            "response_format": {"type": "image", "aspect_ratio": aspect, "image_size": size}}
    try:
        resp = http.request("POST", f"{aivoice._base(aivoice.GEMINI)}/interactions", headers={"x-goog-api-key": key},
                            json_body=body, timeout=timeout)
    except http.UploadError as exc:
        raise GoogleError(f"couldn't reach Google ({exc})", wait=300) from exc
    if not resp.ok:
        raise _failure(resp, "pictures")
    data = resp.json()
    encoded, mime = _image_of(data)
    if not encoded:
        raise GoogleError(f"Google sent no picture ({_refused(data) or 'no reason given'})", wait=0, blocked=True)
    try:
        picture = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError) as exc:
        raise GoogleError(f"Google's picture couldn't be read ({exc})", wait=0) from exc
    out = out.with_suffix(".jpg" if "jpeg" in mime or "jpg" in mime else ".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_bytes(picture)
    partial.replace(out)
    return out


def check_key(key: str, model: str = IMAGE_MODEL) -> str:
    """'' if the key works and can see the picture model, otherwise what's wrong. Costs nothing (billing itself
    is only found out by the first picture)."""
    try:
        resp = http.request("GET", f"{aivoice._base(aivoice.GEMINI)}/models/{model}", headers={"x-goog-api-key": key},
                            timeout=30)
    except http.UploadError as exc:
        return f"couldn't reach Google ({exc})"
    if resp.ok:
        return ""
    return str(_failure(resp, "pictures")).split(". Connect")[0]


# ------------------------------------------------------------------ video shots (Veo 3.1)


@dataclass
class Shot:
    """A shot being made. done: finished (uri set, or blocked/error)."""

    name: str  # the operation
    done: bool = False
    uri: str = ""
    error: str = ""
    blocked: bool = False


def start_shot(key: str, prompt: str, first_frame: Path | None, *, model: str = VIDEO_MODEL, seconds: int = 8,
               resolution: str = "720p", negative: str = "", aspect: str = "9:16") -> str:
    """Start making one shot; returns its operation name (collect it with check_shot)."""
    instance: dict = {"prompt": prompt}
    if first_frame is not None:
        instance["image"] = {"inlineData": {"mimeType": _mime(first_frame),
                                            "data": base64.b64encode(first_frame.read_bytes()).decode()}}
    parameters = {"aspectRatio": aspect, "resolution": resolution, "durationSeconds": int(seconds),
                  # Shots made from a picture may only show adults (Google's rule for image-to-video).
                  "personGeneration": "allow_adult" if first_frame is not None else "allow_all"}
    if negative:
        parameters["negativePrompt"] = negative
    try:
        resp = http.request("POST", f"{aivoice._base(aivoice.GEMINI)}/models/{model}:predictLongRunning",
                            headers={"x-goog-api-key": key}, json_body={"instances": [instance], "parameters": parameters},
                            timeout=120)
    except http.UploadError as exc:
        raise GoogleError(f"couldn't reach Google ({exc})", wait=300) from exc
    if not resp.ok:
        raise _failure(resp, "video shots")
    name = str(resp.json().get("name") or "")
    if not name:
        raise GoogleError("Google didn't start the shot (no operation name)", wait=600)
    return name


def check_shot(key: str, name: str) -> Shot:
    """Where a shot is: still being made, finished (uri) or failed."""
    try:
        resp = http.request("GET", f"{aivoice._base(aivoice.GEMINI)}/{name}", headers={"x-goog-api-key": key}, timeout=60)
    except http.UploadError as exc:
        raise GoogleError(f"couldn't reach Google ({exc})", wait=60) from exc
    if resp.status == 404:
        return Shot(name, done=True, error="Google no longer has this shot (shots are kept for 2 days)")
    if not resp.ok:
        raise _failure(resp, "video shots")
    data = resp.json()
    if not data.get("done"):
        return Shot(name)
    if isinstance(data.get("error"), dict):
        message = " ".join(str(data["error"].get("message") or data["error"]).split())[:300]
        low = message.lower()
        return Shot(name, done=True, error=message, blocked="safety" in low or "filter" in low or "policy" in low)
    answer = (data.get("response") or {}).get("generateVideoResponse") or {}
    samples = answer.get("generatedSamples") or []
    uri = ((samples[0] or {}).get("video") or {}).get("uri", "") if samples else ""
    if uri:
        return Shot(name, done=True, uri=str(uri))
    reasons = answer.get("raiMediaFilteredReasons") or []
    if answer.get("raiMediaFilteredCount") or reasons:
        return Shot(name, done=True, blocked=True,
                    error="Google's safety filters blocked it" + (f" ({' '.join(str(r) for r in reasons)[:200]})" if reasons else ""))
    return Shot(name, done=True, error="Google finished without a video")


def download_shot(key: str, uri: str, out: Path) -> Path:
    """Save a finished shot. Google answers with a redirect to the file; the key is only sent to Google's API."""
    api_host = urlparse(aivoice._base(aivoice.GEMINI)).netloc
    url = uri
    for _ in range(6):
        headers = {"x-goog-api-key": key} if urlparse(url).netloc == api_host else {}
        try:
            resp = http.request("GET", url, headers=headers, timeout=300)
        except http.UploadError as exc:
            raise GoogleError(f"couldn't download the shot ({exc})", wait=120) from exc
        if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            url = resp.headers["Location"]
            continue
        if not resp.ok:
            raise _failure(resp, "video shots")
        if len(resp.body) < 1000:
            raise GoogleError("the shot Google sent is empty", wait=120)
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_name(out.name + ".partial")
        partial.write_bytes(resp.body)
        partial.replace(out)
        return out
    raise GoogleError("too many redirects while downloading the shot", wait=600)
