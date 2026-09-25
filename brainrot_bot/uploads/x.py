"""X (Twitter) via the X API v2 with your own X developer app (OAuth 2.0 login).

X charges per use: about $0.015 per post (no monthly fee). Videos up to 140 seconds work for every
account. Reading the views and likes later also costs a little per read, so it's done only twice.
"""

from __future__ import annotations

import base64
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

KEY = "x"
NAME = "X (Twitter)"
AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API = "https://api.x.com/2"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
CALLBACK_PATH = "/callback/"
CHUNK = 4 * 1024 * 1024  # X accepts at most 5 MB per piece
MAX_SECONDS = 140


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def _error(resp: http.Response, what: str = "X") -> UploadError:
    data = resp.json()
    detail = data.get("detail") or data.get("error_description") or data.get("title") or data.get("error") or resp.text()
    if isinstance(data.get("errors"), list) and data["errors"]:
        detail = data["errors"][0].get("message") or detail
    if resp.status == 401 or data.get("error") in ("invalid_grant", "invalid_request"):
        return UploadError(f"{what} login is no longer valid ({detail})", relogin=True)
    if resp.status == 402 or "credits" in str(detail).lower():
        return UploadError(f"{what}: no API credits left on your X developer account ({detail})", wait_hours=24)
    if resp.status == 429:
        reset = resp.headers.get("x-rate-limit-reset")
        wait = max(0.25, (float(reset) - time.time()) / 3600) if reset and reset.isdigit() else 1.0
        return UploadError(f"{what} rate limit reached", wait_hours=min(wait, 24))
    if resp.status == 403:
        return UploadError(f"{what} refused: {detail}", retry=False)
    return UploadError(f"{what} said ({resp.status}): {detail}", retry=resp.status >= 500)


def _checked(resp: http.Response) -> dict:
    if not resp.ok:
        raise _error(resp)
    return resp.json()


def _token_request(form: dict, client_id: str, client_secret: str) -> dict:
    headers = {}
    if client_secret:  # confidential client ("Web App, Automated App or Bot")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    resp = http.request("POST", TOKEN_URL, form={**form, "client_id": client_id}, headers=headers)
    data = resp.json()
    if not resp.ok or not data.get("access_token"):
        raise _error(resp, "X login")
    now = time.time()
    return {
        "access_token": data["access_token"],
        "expires_at": now + float(data.get("expires_in", 7200)),
        "refresh_token": data.get("refresh_token", ""),
    }


def connect(client_id: str, client_secret: str, port: int, printer: Callable[[str], None] = print) -> dict:
    verifier, challenge = pkce_pair()

    def build(redirect: str, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    params, redirect = browser_login(build, port=port, path=CALLBACK_PATH, printer=printer)
    creds = {"client_id": client_id, "client_secret": client_secret}
    creds.update(_token_request({"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect,
                                 "code_verifier": verifier}, client_id, client_secret))
    me = _checked(http.request("GET", f"{API}/users/me", headers={"Authorization": f"Bearer {creds['access_token']}"}))
    username = (me.get("data") or {}).get("username", "")
    creds["username"] = username
    creds["account"] = f"@{username}" if username else "your account"
    return creds


def access_token(store, account: str = KEY) -> str:
    creds = store.get(account)
    if creds.get("access_token") and float(creds.get("expires_at", 0)) > time.time() + 120:
        return creds["access_token"]
    if not creds.get("refresh_token"):
        raise UploadError("X login expired", relogin=True)
    fields = _token_request({"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
                            creds.get("client_id", ""), creds.get("client_secret", ""))
    if not fields["refresh_token"]:
        fields["refresh_token"] = creds["refresh_token"]
    store.update(account, **fields)
    return fields["access_token"]


def upload(video: Path, post: PostInfo, cfg: SimpleNamespace, store, should_stop: Callable[[], bool] = lambda: False,
           account: str = KEY) -> Posted:
    if post.duration > MAX_SECONDS:
        raise UploadError(f"X allows videos up to {MAX_SECONDS} seconds for most accounts (this one is {post.duration:.0f}s)", retry=False)
    token = access_token(store, account)
    auth = {"Authorization": f"Bearer {token}"}
    size = video.stat().st_size
    init = _checked(http.request("POST", f"{API}/media/upload/initialize", headers=auth, json_body={
        "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"}))
    media_id = str((init.get("data") or {}).get("id") or "")
    if not media_id:
        raise UploadError("X didn't start the upload")
    with open(video, "rb") as handle:
        index = 0
        while True:
            if should_stop():
                raise StopRequested()
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            body, content_type = http.multipart({"segment_index": str(index)}, {"media": ("chunk.mp4", chunk, "application/octet-stream")})
            _checked(http.request("POST", f"{API}/media/upload/{media_id}/append", headers={**auth, "Content-Type": content_type},
                                  data=body, timeout=300))
            index += 1
    info = (_checked(http.request("POST", f"{API}/media/upload/{media_id}/finalize", headers=auth)).get("data") or {}).get("processing_info")
    for _ in range(120):  # X processes videos before they can be posted
        if not info or info.get("state") == "succeeded":
            break
        if info.get("state") == "failed":
            raise UploadError(f"X couldn't process the video: {(info.get('error') or {}).get('message', 'unknown reason')}", retry=False)
        if should_stop():
            raise StopRequested()
        time.sleep(min(30, max(1, int(info.get("check_after_secs", 5)))))
        status = _checked(http.request("GET", f"{API}/media/upload", params={"command": "STATUS", "media_id": media_id}, headers=auth))
        info = (status.get("data") or {}).get("processing_info")
    else:
        raise UploadError("X took too long to process the video")

    text = post.render("x", getattr(cfg.upload, "templates", None))
    created = _checked(http.request("POST", f"{API}/tweets", headers=auth, json_body={"text": text, "media": {"media_ids": [media_id]}}))
    tweet_id = str((created.get("data") or {}).get("id") or "")
    username = store.get(account).get("username", "")
    url = f"https://x.com/{username or 'i/web'}/status/{tweet_id}" if tweet_id else ""
    return Posted(url or "posted on X", url, tweet_id)


def stats(post_id: str, cfg: SimpleNamespace, store, account: str = KEY) -> dict:
    token = access_token(store, account)
    data = _checked(http.request("GET", f"{API}/tweets/{post_id}", params={"tweet.fields": "public_metrics"},
                                 headers={"Authorization": f"Bearer {token}"}))
    metrics = (data.get("data") or {}).get("public_metrics") or {}
    return {"views": int(metrics.get("impression_count", 0)), "likes": int(metrics.get("like_count", 0)),
            "comments": int(metrics.get("reply_count", 0)), "shares": int(metrics.get("retweet_count", 0)) + int(metrics.get("quote_count", 0))}
