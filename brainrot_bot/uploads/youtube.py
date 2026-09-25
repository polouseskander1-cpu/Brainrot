"""YouTube Shorts via the YouTube Data API v3 (your own Google Cloud "Desktop app" credentials)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import urlencode

from ..media import StopRequested
from . import http
from .http import UploadError
from .oauth import browser_login, pkce_pair
from .post import PostInfo

KEY = "youtube"
NAME = "YouTube Shorts"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KB

LIMIT_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}


def read_client_file(path: Path) -> tuple[str, str]:
    """client_id and client_secret from the JSON file Google gives you for a Desktop app."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UploadError(f"couldn't read {path}: {exc}", retry=False) from exc
    section = data.get("installed") or data.get("web") or data
    client_id, secret = section.get("client_id"), section.get("client_secret")
    if not client_id or not secret:
        raise UploadError("that file has no client_id/client_secret. Download the OAuth client JSON for a *Desktop app*.", retry=False)
    return client_id, secret


def _google_error(resp: http.Response) -> UploadError:
    error = resp.json().get("error")
    if isinstance(error, str):  # token endpoint style
        if error in ("invalid_grant", "unauthorized_client", "invalid_client"):
            return UploadError(f"YouTube login is no longer valid ({error})", relogin=True)
        return UploadError(f"YouTube said: {resp.json().get('error_description') or error}", retry=resp.status >= 500)
    error = error or {}
    reasons = {e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)}
    message = error.get("message") or resp.text()
    if resp.status == 401:
        return UploadError(f"YouTube login is no longer valid ({message})", relogin=True)
    if reasons & LIMIT_REASONS:
        return UploadError(f"YouTube daily upload limit reached ({message})", wait_hours=12)
    if "youtubeSignupRequired" in reasons:
        return UploadError("this Google account has no YouTube channel yet. Create one on youtube.com first.", retry=False)
    return UploadError(f"YouTube said ({resp.status}): {message}", retry=resp.status >= 500 or resp.status == 429)


def connect(client_id: str, client_secret: str, printer: Callable[[str], None] = print) -> dict:
    verifier, challenge = pkce_pair()

    def build(redirect_uri: str, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        })

    params, redirect_uri = browser_login(build, printer=printer)
    resp = http.request("POST", TOKEN_URL, form={
        "code": params["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    data = resp.json()
    if not resp.ok or not data.get("refresh_token"):
        raise _google_error(resp) if not resp.ok else UploadError("Google didn't return a long-term login. Try again.", retry=False)
    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": data["refresh_token"],
        "access_token": data.get("access_token", ""),
        "expires_at": time.time() + float(data.get("expires_in", 3600)),
    }
    creds["account"] = channel_name(creds["access_token"])
    return creds


def channel_name(access_token: str) -> str:
    resp = http.request("GET", CHANNELS_URL, params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {access_token}"})
    if not resp.ok:
        raise _google_error(resp)
    items = resp.json().get("items") or []
    if not items:
        raise UploadError("this Google account has no YouTube channel yet. Create one on youtube.com first.", retry=False)
    return items[0].get("snippet", {}).get("title", "your channel")


def access_token(store) -> str:
    creds = store.get(KEY)
    if creds.get("access_token") and float(creds.get("expires_at", 0)) > time.time() + 120:
        return creds["access_token"]
    resp = http.request("POST", TOKEN_URL, form={
        "client_id": creds.get("client_id", ""),
        "client_secret": creds.get("client_secret", ""),
        "refresh_token": creds.get("refresh_token", ""),
        "grant_type": "refresh_token",
    })
    data = resp.json()
    if not resp.ok or not data.get("access_token"):
        raise _google_error(resp)
    store.update(KEY, access_token=data["access_token"], expires_at=time.time() + float(data.get("expires_in", 3600)))
    return data["access_token"]


def upload(video: Path, post: PostInfo, cfg: SimpleNamespace, store, should_stop: Callable[[], bool] = lambda: False) -> str:
    token = access_token(store)
    size = video.stat().st_size
    metadata = {
        "snippet": {
            "title": post.youtube_title,
            "description": post.youtube_description,
            "categoryId": str(cfg.upload.youtube_category),
        },
        "status": {"privacyStatus": cfg.upload.youtube_privacy, "selfDeclaredMadeForKids": False},
    }
    resp = http.request(
        "POST",
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        json_body=metadata,
    )
    if not resp.ok or not resp.headers.get("Location"):
        raise _google_error(resp)
    session = resp.headers["Location"]

    offset, failures = 0, 0
    with open(video, "rb") as handle:
        while True:
            if should_stop():
                raise StopRequested()
            handle.seek(offset)
            chunk = handle.read(CHUNK)
            end = offset + len(chunk) - 1
            try:
                resp = http.request("PUT", session, headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }, data=chunk, timeout=600)
            except UploadError:
                resp = None
            if resp is not None and resp.status in (200, 201):
                video_id = resp.json().get("id", "")
                return f"https://youtube.com/shorts/{video_id}" if video_id else "uploaded"
            if resp is not None and resp.status == 308:
                received = resp.headers.get("Range", "")  # e.g. "bytes=0-8388607"
                offset = int(received.rsplit("-", 1)[1]) + 1 if "-" in received else 0
                failures = 0
                continue
            if resp is not None and resp.status == 404:
                raise UploadError("YouTube upload session expired; starting over next time")
            if resp is not None and resp.status < 500:
                raise _google_error(resp)
            failures += 1  # network trouble or server error: ask YouTube how much it got, then resume
            if failures > 5:
                raise UploadError("YouTube upload kept failing (network or server trouble)")
            time.sleep(min(60, 2 ** failures))
            try:
                status = http.request("PUT", session, headers={"Authorization": f"Bearer {token}", "Content-Range": f"bytes */{size}", "Content-Length": "0"}, data=b"")
            except UploadError:
                continue
            if status.status in (200, 201):
                return f"https://youtube.com/shorts/{status.json().get('id', '')}"
            if status.status == 308:
                received = status.headers.get("Range", "")
                offset = int(received.rsplit("-", 1)[1]) + 1 if "-" in received else 0
