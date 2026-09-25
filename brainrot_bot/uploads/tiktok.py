"""TikTok via the Content Posting API (your own TikTok developer app).

Two modes:
  draft  - the video lands in your TikTok inbox and you tap Post. Works for apps that haven't
           been audited by TikTok, and lets you add a trending sound before posting.
  direct - posted straight to your profile. TikTok only allows this publicly for audited apps.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import urlencode

from ..media import StopRequested
from . import http
from .http import UploadError
from .oauth import browser_login, pkce_pair
from .post import Posted, PostInfo

KEY = "tiktok"
NAME = "TikTok"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
MB = 1024 * 1024
CALLBACK_PATH = "/callback/"

RELOGIN_CODES = {"access_token_invalid", "scope_not_authorized", "scope_permission_missed"}
LIMIT_CODES = {"spam_risk_too_many_posts", "spam_risk_too_many_pending_share", "spam_risk_user_banned_from_posting", "reached_active_user_cap"}


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) following TikTok's rules: files up to 64 MB go in one piece;
    bigger files in 5-64 MB chunks, the remainder added to the last chunk."""
    if size <= 64 * MB:
        return size, 1
    chunk = 32 * MB
    return chunk, size // chunk


def _check(resp: http.Response) -> dict:
    body = resp.json()
    error = body.get("error") or {}
    code = error.get("code", "ok" if resp.ok else "http_error")
    if resp.ok and code == "ok":
        return body.get("data") or {}
    message = error.get("message") or resp.text()
    if code in RELOGIN_CODES or resp.status == 401:
        return_error = UploadError(f"TikTok login is no longer valid ({code})", relogin=True)
    elif code == "unaudited_client_can_only_post_to_private_accounts":
        return_error = UploadError(
            "your TikTok app isn't audited, so TikTok only allows direct posting to private accounts. "
            "Set upload.tiktok_mode to draft (or get the app audited).", retry=False)
    elif code in LIMIT_CODES:
        return_error = UploadError(f"TikTok posting limit reached ({code})", wait_hours=12)
    elif code == "rate_limit_exceeded" or resp.status == 429:
        return_error = UploadError("TikTok asked to slow down", wait_hours=0.25)
    else:
        return_error = UploadError(f"TikTok said ({code}): {message}", retry=resp.status >= 500 or code == "internal_error")
    raise return_error


def connect(client_key: str, client_secret: str, mode: str, port: int, printer: Callable[[str], None] = print,
            stats: bool = False) -> dict:
    """stats: also ask for video.list (views and likes), which needs the Display API product in the TikTok app."""
    scopes = "user.info.basic,video.upload" + (",video.publish" if mode == "direct" else "") + (",video.list" if stats else "")
    verifier, challenge = pkce_pair(hex_challenge=True)

    def build(redirect: str, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_key": client_key,
            "scope": scopes,
            "redirect_uri": redirect,
            "state": state,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    params, redirect = browser_login(build, port=port, path=CALLBACK_PATH, printer=printer)
    resp = http.request("POST", TOKEN_URL, form={
        "client_key": client_key,
        "client_secret": client_secret,
        "code": params["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    creds = {"client_key": client_key, "client_secret": client_secret, "mode": mode}
    creds.update(_token_fields(resp))
    creds["account"] = display_name(creds["access_token"])
    return creds


def _token_fields(resp: http.Response) -> dict:
    data = resp.json()
    if not data.get("access_token"):
        detail = data.get("error_description") or data.get("error") or resp.text()
        raise UploadError(f"TikTok login failed: {detail}", relogin=True)
    now = time.time()
    return {
        "access_token": data["access_token"],
        "expires_at": now + float(data.get("expires_in", 86400)),
        "refresh_token": data.get("refresh_token", ""),
        "refresh_expires_at": now + float(data.get("refresh_expires_in", 365 * 86400)),
        "open_id": data.get("open_id", ""),
        "scope": data.get("scope", ""),
    }


def display_name(token: str) -> str:
    resp = http.request("GET", f"{API}/user/info/", params={"fields": "open_id,display_name"}, headers={"Authorization": f"Bearer {token}"})
    try:
        return (_check(resp).get("user") or {}).get("display_name") or "your account"
    except UploadError:
        return "your account"


def access_token(store, account: str = KEY) -> str:
    creds = store.get(account)
    if creds.get("access_token") and float(creds.get("expires_at", 0)) > time.time() + 300:
        return creds["access_token"]
    if not creds.get("refresh_token") or float(creds.get("refresh_expires_at", 0)) < time.time():
        raise UploadError("TikTok login expired", relogin=True)
    resp = http.request("POST", TOKEN_URL, form={
        "client_key": creds.get("client_key", ""),
        "client_secret": creds.get("client_secret", ""),
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
    })
    fields = _token_fields(resp)
    store.update(account, **fields)
    return fields["access_token"]


def upload(video: Path, post: PostInfo, cfg: SimpleNamespace, store, should_stop: Callable[[], bool] = lambda: False,
           account: str = KEY) -> Posted:
    token = access_token(store, account)
    auth = {"Authorization": f"Bearer {token}"}
    size = video.stat().st_size
    chunk_size, chunks = chunk_plan(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": chunks}
    mode = cfg.upload.tiktok_mode

    if mode == "direct":
        creator = _check(http.request("POST", f"{API}/post/publish/creator_info/query/", headers=auth, json_body={}))
        options = creator.get("privacy_level_options") or []
        privacy = cfg.upload.tiktok_privacy
        if options and privacy not in options:
            raise UploadError(f"TikTok doesn't allow privacy {privacy} for this account (allowed: {', '.join(options)})", retry=False)
        max_seconds = creator.get("max_video_post_duration_sec")
        if max_seconds and post.duration > float(max_seconds):
            raise UploadError(f"this account can post videos up to {max_seconds}s on TikTok", retry=False)
        body = {
            "post_info": {
                "title": post.render("tiktok", getattr(cfg.upload, "templates", None)),
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": source,
        }
        init = _check(http.request("POST", f"{API}/post/publish/video/init/", headers=auth, json_body=body))
    else:
        init = _check(http.request("POST", f"{API}/post/publish/inbox/video/init/", headers=auth, json_body={"source_info": source}))

    publish_id, upload_url = init.get("publish_id", ""), init.get("upload_url", "")
    if not upload_url:
        raise UploadError("TikTok didn't return an upload address")
    with open(video, "rb") as handle:
        for index in range(chunks):
            if should_stop():
                raise StopRequested()
            start = index * chunk_size
            end = size - 1 if index == chunks - 1 else start + chunk_size - 1
            handle.seek(start)
            data = handle.read(end - start + 1)
            resp = http.request("PUT", upload_url, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }, data=data, timeout=600)
            if resp.status not in (200, 201, 206):
                raise UploadError(f"TikTok upload failed ({resp.status}): {resp.text()}", retry=resp.status >= 500 or resp.status == 429)

    # TikTok processes the video for a little while; wait to see how it went.
    status, data = "PROCESSING_UPLOAD", {}
    for _ in range(24):
        time.sleep(5)
        data = _check(http.request("POST", f"{API}/post/publish/status/fetch/", headers=auth, json_body={"publish_id": publish_id}))
        status = data.get("status", "")
        if status == "FAILED":
            raise UploadError(f"TikTok couldn't process the video: {data.get('fail_reason') or 'unknown reason'}")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            break
    if status == "SEND_TO_USER_INBOX" or (mode == "draft" and status != "PUBLISH_COMPLETE"):
        return Posted("sent to your TikTok inbox - open TikTok and tap the notification to post it")
    if status != "PUBLISH_COMPLETE":
        return Posted("uploaded to TikTok (still processing)")
    # TikTok spells it "publicaly"; only public posts have an id.
    ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
    post_id = str(ids[0]) if ids else ""
    return Posted("posted on TikTok", "", post_id)


def stats(post_id: str, cfg: SimpleNamespace, store, account: str = KEY) -> dict:
    """Views, likes, comments and shares (needs the video.list permission)."""
    token = access_token(store, account)
    resp = http.request("POST", f"{API}/video/query/", params={"fields": "id,view_count,like_count,comment_count,share_count"},
                        headers={"Authorization": f"Bearer {token}"}, json_body={"filters": {"video_ids": [post_id]}})
    videos = _check(resp).get("videos") or []
    if not videos:
        return {}
    v = videos[0]
    return {"views": int(v.get("view_count", 0)), "likes": int(v.get("like_count", 0)),
            "comments": int(v.get("comment_count", 0)), "shares": int(v.get("share_count", 0))}
