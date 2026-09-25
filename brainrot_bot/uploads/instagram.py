"""Instagram Reels via the Instagram API with Instagram Login (professional accounts)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from ..media import StopRequested
from . import http
from .http import UploadError
from .post import PostInfo

KEY = "instagram"
NAME = "Instagram Reels"
GRAPH = "https://graph.instagram.com"
REFRESH_AFTER = 7 * 86400  # refresh the 60-day token weekly so it never runs out
RATE_LIMIT_CODES = {4, 17, 32, 613}


def graph_error(resp: http.Response, platform: str) -> UploadError:
    error = resp.json().get("error") or {}
    code = error.get("code")
    message = error.get("error_user_msg") or error.get("message") or resp.text()
    if code == 190 or resp.status == 401:
        return UploadError(f"{platform} login is no longer valid ({message})", relogin=True)
    if code in RATE_LIMIT_CODES or resp.status == 429:
        return UploadError(f"{platform} posting limit reached ({message})", wait_hours=6)
    transient = resp.status >= 500 or bool(error.get("is_transient"))
    return UploadError(f"{platform} said: {message}", retry=transient)


def graph_checked(resp: http.Response, platform: str = NAME) -> dict:
    data = resp.json()
    if not resp.ok or "error" in data:
        raise graph_error(resp, platform)
    return data


def connect(token: str, version: str) -> dict:
    data = graph_checked(http.request("GET", f"{GRAPH}/{version}/me", params={"fields": "user_id,username", "access_token": token}))
    user_id = str(data.get("user_id") or data.get("id") or "")
    if not user_id:
        raise UploadError("Instagram didn't say which account this token belongs to", retry=False)
    now = time.time()
    return {
        "access_token": token,
        "user_id": user_id,
        "account": "@" + str(data.get("username", "")),
        "refreshed_at": now,
        "expires_at": now + 60 * 86400,
    }


def access_token(store) -> str:
    creds = store.get(KEY)
    token = creds.get("access_token", "")
    now = time.time()
    if float(creds.get("expires_at", 0)) < now:
        raise UploadError("Instagram login expired (tokens last 60 days)", relogin=True)
    if now - float(creds.get("refreshed_at", 0)) > REFRESH_AFTER:
        resp = http.request("GET", f"{GRAPH}/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": token})
        data = resp.json()
        if resp.ok and data.get("access_token"):
            token = data["access_token"]
            store.update(KEY, access_token=token, refreshed_at=now, expires_at=now + float(data.get("expires_in", 60 * 86400)))
        elif (data.get("error") or {}).get("code") == 190:
            raise UploadError("Instagram login is no longer valid", relogin=True)
    return token


def upload(video: Path, post: PostInfo, cfg: SimpleNamespace, store, should_stop: Callable[[], bool] = lambda: False) -> str:
    version = cfg.upload.meta_api_version
    token = access_token(store)
    user_id = store.get(KEY).get("user_id", "me")
    container = graph_checked(http.request("POST", f"{GRAPH}/{version}/{user_id}/media", form={
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": post.caption[:2200],
        "share_to_feed": "true",
        "access_token": token,
    }))
    container_id = container.get("id")
    if not container_id:
        raise UploadError("Instagram didn't create the upload")
    upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{version}/{container_id}"
    size = video.stat().st_size
    with open(video, "rb") as handle:
        graph_checked(http.request("POST", upload_uri, headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Length": str(size),
        }, data=handle, timeout=1800))

    # Instagram processes the video before it can be published.
    for _ in range(120):
        if should_stop():
            raise StopRequested()
        time.sleep(5)
        status = graph_checked(http.request("GET", f"{GRAPH}/{version}/{container_id}", params={"fields": "status_code,status", "access_token": token}))
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise UploadError(f"Instagram couldn't process the video: {status.get('status') or code}", retry=code == "EXPIRED")
    else:
        raise UploadError("Instagram took too long to process the video")

    published = graph_checked(http.request("POST", f"{GRAPH}/{version}/{user_id}/media_publish", form={"creation_id": container_id, "access_token": token}))
    media_id = published.get("id", "")
    try:
        link = graph_checked(http.request("GET", f"{GRAPH}/{version}/{media_id}", params={"fields": "permalink", "access_token": token})).get("permalink")
    except UploadError:
        link = None
    return link or f"posted on Instagram (media {media_id})"
