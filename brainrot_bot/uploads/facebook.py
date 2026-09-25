"""Facebook Page Reels via the Graph API Reels publishing flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from . import http
from .http import UploadError
from .instagram import graph_checked
from .post import PostInfo

KEY = "facebook"
NAME = "Facebook Reels"
GRAPH = "https://graph.facebook.com"
MIN_SECONDS, MAX_SECONDS = 3, 90


def long_lived_user_token(app_id: str, app_secret: str, user_token: str, version: str) -> str:
    data = graph_checked(http.request("GET", f"{GRAPH}/{version}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": user_token,
    }), NAME)
    if not data.get("access_token"):
        raise UploadError("Facebook didn't return a long-lived token", retry=False)
    return data["access_token"]


def list_pages(user_token: str, version: str) -> list[dict]:
    data = graph_checked(http.request("GET", f"{GRAPH}/{version}/me/accounts", params={"fields": "id,name,access_token", "access_token": user_token}), NAME)
    return [p for p in data.get("data", []) if p.get("id") and p.get("access_token")]


def page_credentials(page: dict) -> dict:
    # A Page token made from a long-lived user token does not expire.
    return {"page_id": str(page["id"]), "page_token": page["access_token"], "account": page.get("name", "your Page")}


def upload(video: Path, post: PostInfo, cfg: SimpleNamespace, store, should_stop: Callable[[], bool] = lambda: False) -> str:
    if not MIN_SECONDS <= post.duration <= MAX_SECONDS:
        raise UploadError(f"Facebook Reels must be {MIN_SECONDS}-{MAX_SECONDS} seconds long (this one is {post.duration:.0f}s)", retry=False)
    version = cfg.upload.meta_api_version
    creds = store.get(KEY)
    page_id, token = creds.get("page_id", ""), creds.get("page_token", "")
    start = graph_checked(http.request("POST", f"{GRAPH}/{version}/{page_id}/video_reels", form={"upload_phase": "start", "access_token": token}), NAME)
    video_id = start.get("video_id")
    if not video_id:
        raise UploadError("Facebook didn't start the upload")
    upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{version}/{video_id}"
    size = video.stat().st_size
    with open(video, "rb") as handle:
        graph_checked(http.request("POST", upload_url, headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Length": str(size),
        }, data=handle, timeout=1800), NAME)
    graph_checked(http.request("POST", f"{GRAPH}/{version}/{page_id}/video_reels", form={
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": post.caption,
        "access_token": token,
    }), NAME)
    return f"https://www.facebook.com/reel/{video_id}"
