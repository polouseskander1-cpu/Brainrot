"""Pinterest video Pins via the Pinterest API v5 (your own Pinterest developer app).

New Pinterest apps start with "trial" access; Pinterest reviews the app before it can post for real.
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
from .oauth import browser_login
from .post import Posted, PostInfo

KEY = "pinterest"
NAME = "Pinterest"
AUTH_URL = "https://www.pinterest.com/oauth/"
API = "https://api.pinterest.com/v5"
SCOPES = "boards:read,boards:write,pins:read,pins:write,user_accounts:read"
CALLBACK_PATH = "/callback/"


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def _error(resp: http.Response, what: str = "Pinterest") -> UploadError:
    data = resp.json()
    message = data.get("message") or data.get("error_description") or data.get("error") or resp.text()
    if resp.status == 401 or data.get("error") == "invalid_grant":
        return UploadError(f"{what} login is no longer valid ({message})", relogin=True)
    if resp.status == 429:
        return UploadError(f"{what} rate limit reached", wait_hours=1)
    if resp.status == 403:
        return UploadError(f"{what} refused: {message} (new apps need Pinterest's approval for full access)", retry=False)
    return UploadError(f"{what} said ({resp.status}): {message}", retry=resp.status >= 500)


def _checked(resp: http.Response) -> dict:
    if not resp.ok:
        raise _error(resp)
    return resp.json()


def _token_request(form: dict, app_id: str, secret: str) -> dict:
    basic = base64.b64encode(f"{app_id}:{secret}".encode()).decode()
    resp = http.request("POST", f"{API}/oauth/token", form=form, headers={"Authorization": f"Basic {basic}"})
    data = resp.json()
    if not resp.ok or not data.get("access_token"):
        raise _error(resp, "Pinterest login")
    now = time.time()
    return {
        "access_token": data["access_token"],
        "expires_at": now + float(data.get("expires_in", 30 * 86400)),
        "refresh_token": data.get("refresh_token", ""),
        "refresh_expires_at": now + float(data.get("refresh_token_expires_in", 60 * 86400)),
    }


def connect(app_id: str, secret: str, port: int, printer: Callable[[str], None] = print) -> dict:
    def build(redirect: str, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": app_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES, "state": state})

    params, redirect = browser_login(build, port=port, path=CALLBACK_PATH, printer=printer, host="localhost")
    creds = {"app_id": app_id, "app_secret": secret}
    creds.update(_token_request({"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect,
                                 "continuous_refresh": "true"}, app_id, secret))
    auth = {"Authorization": f"Bearer {creds['access_token']}"}
    try:
        user = _checked(http.request("GET", f"{API}/user_account", headers=auth))
        creds["account"] = "@" + str(user.get("username", "")) if user.get("username") else "your account"
    except UploadError:
        creds["account"] = "your account"
    return creds


def list_boards(access: str) -> list[dict]:
    data = _checked(http.request("GET", f"{API}/boards", params={"page_size": 100}, headers={"Authorization": f"Bearer {access}"}))
    return [b for b in data.get("items", []) if b.get("id")]


def create_board(access: str, name: str) -> dict:
    return _checked(http.request("POST", f"{API}/boards", headers={"Authorization": f"Bearer {access}"},
                                 json_body={"name": name, "privacy": "PUBLIC"}))


def access_token(store, account: str = KEY) -> str:
    creds = store.get(account)
    now = time.time()
    if creds.get("access_token") and float(creds.get("expires_at", 0)) > now + 3600:
        # Refresh a week before the 30-day token runs out; the refresh token then renews too.
        if float(creds.get("expires_at", 0)) - now > 7 * 86400:
            return creds["access_token"]
    if not creds.get("refresh_token") or float(creds.get("refresh_expires_at", 0)) < now:
        raise UploadError("Pinterest login expired (logins last 60 days without use)", relogin=True)
    fields = _token_request({"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
                            creds.get("app_id", ""), creds.get("app_secret", ""))
    if not fields["refresh_token"]:
        fields["refresh_token"] = creds["refresh_token"]
        fields["refresh_expires_at"] = creds.get("refresh_expires_at", 0)
    store.update(account, **fields)
    return fields["access_token"]


def upload(video: Path, post: PostInfo, cfg: SimpleNamespace, store, should_stop: Callable[[], bool] = lambda: False,
           account: str = KEY) -> Posted:
    creds = store.get(account)
    board = creds.get("board_id", "")
    if not board:
        raise UploadError("no Pinterest board chosen. Connect Pinterest again in the menu to pick one.", retry=False)
    token = access_token(store, account)
    auth = {"Authorization": f"Bearer {token}"}
    registered = _checked(http.request("POST", f"{API}/media", headers=auth, json_body={"media_type": "video"}))
    media_id, upload_url = str(registered.get("media_id", "")), registered.get("upload_url", "")
    if not media_id or not upload_url:
        raise UploadError("Pinterest didn't start the upload")
    fields = {str(k): str(v) for k, v in (registered.get("upload_parameters") or {}).items()}
    body, content_type = http.multipart(fields, {"file": (video.name, video.read_bytes(), "video/mp4")})
    sent = http.request("POST", upload_url, headers={"Content-Type": content_type}, data=body, timeout=1800)
    if sent.status not in (200, 201, 204):
        raise UploadError(f"Pinterest upload failed ({sent.status})", retry=sent.status >= 500)
    for _ in range(120):
        if should_stop():
            raise StopRequested()
        status = _checked(http.request("GET", f"{API}/media/{media_id}", headers=auth)).get("status")
        if status == "succeeded":
            break
        if status == "failed":
            raise UploadError("Pinterest couldn't process the video", retry=False)
        time.sleep(5)
    else:
        raise UploadError("Pinterest took too long to process the video")

    templates = getattr(cfg.upload, "templates", None)
    pin = _checked(http.request("POST", f"{API}/pins", headers=auth, json_body={
        "board_id": board,
        "title": post.render("pinterest_title", templates),
        "description": post.render("pinterest", templates),
        "media_source": {"source_type": "video_id", "media_id": media_id, "cover_image_key_frame_time": 1},
    }))
    pin_id = str(pin.get("id", ""))
    url = f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else ""
    return Posted(url or "posted on Pinterest", url, pin_id)


def stats(post_id: str, cfg: SimpleNamespace, store, account: str = KEY) -> dict:
    token = access_token(store, account)
    today = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 89 * 86400))
    data = _checked(http.request("GET", f"{API}/pins/{post_id}/analytics", headers={"Authorization": f"Bearer {token}"}, params={
        "start_date": start, "end_date": today, "metric_types": "IMPRESSION,SAVE,PIN_CLICK,VIDEO_MRC_VIEW,TOTAL_REACTIONS,TOTAL_COMMENTS"}))
    block = data.get("all") or next((v for v in data.values() if isinstance(v, dict)), {})
    totals = {**(block.get("summary_metrics") or {}), **(block.get("lifetime_metrics") or {})}
    return {"views": int(totals.get("VIDEO_MRC_VIEW") or totals.get("IMPRESSION") or 0), "likes": int(totals.get("TOTAL_REACTIONS") or 0),
            "comments": int(totals.get("TOTAL_COMMENTS") or 0), "saves": int(totals.get("SAVE") or 0)}
