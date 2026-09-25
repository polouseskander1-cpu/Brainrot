"""Uploaders are tested against a scripted fake of each platform's HTTP API (no network)."""

import re
import threading
import time
import urllib.request
from email.message import Message
from types import SimpleNamespace

import pytest

from brainrot_bot.config import DEFAULTS
from brainrot_bot.credentials import Credentials
from brainrot_bot.uploads import PostInfo, UploadError, UploadQueue, http, instagram, facebook, oauth, tiktok, youtube
from brainrot_bot.uploads import queue as queue_module
from brainrot_bot.state import State


def response(status=200, body=b"{}", **headers):
    message = Message()
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    if isinstance(body, dict):
        import json

        body = json.dumps(body).encode()
    return http.Response(status, message, body)


class FakeApi:
    """Answers requests with scripted responses, matched by (method, regex on the URL), in order."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.routes = []
        monkeypatch.setattr(http, "request", self.request)
        monkeypatch.setattr(time, "sleep", lambda s: None)

    def on(self, method, pattern, *responses):
        self.routes.append([method, re.compile(pattern), list(responses)])
        return self

    def request(self, method, url, *, params=None, headers=None, json_body=None, form=None, data=None, timeout=60):
        body = data.read() if hasattr(data, "read") else data
        self.calls.append(SimpleNamespace(method=method, url=url, params=params or {}, headers=headers or {}, json=json_body, form=form or {}, data=body))
        for route in self.routes:
            if route[0] == method and route[1].search(url):
                if not route[2]:
                    raise AssertionError(f"no more scripted answers for {method} {url}")
                answer = route[2][0] if len(route[2]) == 1 else route[2].pop(0)
                return answer(self.calls[-1]) if callable(answer) else answer
        raise AssertionError(f"unexpected request {method} {url}")

    def find(self, method, pattern):
        return [c for c in self.calls if c.method == method and re.search(pattern, c.url)]


def cfg(**upload):
    values = dict(DEFAULTS["upload"])
    values.update(upload)
    return SimpleNamespace(upload=SimpleNamespace(**values))


@pytest.fixture
def store(tmp_path):
    return Credentials(tmp_path / "credentials", encrypt=False)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "reel.mp4"
    path.write_bytes(bytes(range(256)) * 2400)  # 614,400 bytes
    return path


POST = PostInfo("Why the Roman Empire fell", "#fyp #history", 42.0)


# ---------------------------------------------------------------- post text


def test_post_texts():
    assert POST.caption == "Why the Roman Empire fell\n\n#fyp #history"
    assert POST.youtube_title == "Why the Roman Empire fell #shorts"
    long = PostInfo("x" * 150 + " <b>", "", 200)
    assert len(long.youtube_title) == 100 and "<" not in long.youtube_title and "#shorts" not in long.youtube_title
    assert PostInfo.from_dict(POST.to_dict()) == POST


def test_pkce():
    verifier, challenge = oauth.pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    verifier, challenge = oauth.pkce_pair(hex_challenge=True)
    assert re.fullmatch(r"[0-9a-f]{64}", challenge)


def test_browser_login_catches_the_redirect():
    seen = {}

    def build(redirect_uri, state):
        seen.update(redirect=redirect_uri, state=state)
        return "https://example.com/auth"

    def browser():
        while "redirect" not in seen:
            time.sleep(0.05)
        urllib.request.urlopen(f"{seen['redirect']}?code=abc123&state={seen['state']}", timeout=5).read()

    threading.Thread(target=browser, daemon=True).start()
    params, redirect = oauth.browser_login(build, path="/callback/", open_browser=False, printer=lambda s: None, timeout=10)
    assert params["code"] == "abc123" and redirect.endswith("/callback/")


def test_browser_login_refused():
    seen = {}

    def build(redirect_uri, state):
        seen.update(redirect=redirect_uri, state=state)
        return "https://example.com/auth"

    def browser():
        while "redirect" not in seen:
            time.sleep(0.05)
        urllib.request.urlopen(f"{seen['redirect']}?error=access_denied&state={seen['state']}", timeout=5).read()

    threading.Thread(target=browser, daemon=True).start()
    with pytest.raises(UploadError, match="access_denied"):
        oauth.browser_login(build, open_browser=False, printer=lambda s: None, timeout=10)


# ---------------------------------------------------------------- YouTube


def test_youtube_resumable_upload_in_chunks(monkeypatch, store, video):
    monkeypatch.setattr(youtube, "CHUNK", 256 * 1024)
    store.set("youtube", {"client_id": "id", "client_secret": "s", "refresh_token": "r", "access_token": "old", "expires_at": 0})
    size = video.stat().st_size

    def chunk_answer(call):
        start, end = map(int, re.match(r"bytes (\d+)-(\d+)/", call.headers["Content-Range"]).groups())
        if end + 1 < size:
            return response(308, Range=f"bytes=0-{end}")
        return response(200, {"id": "vid123"})

    api = (FakeApi(monkeypatch)
           .on("POST", "oauth2.googleapis.com/token", response(200, {"access_token": "fresh", "expires_in": 3600}))
           .on("POST", "upload/youtube/v3/videos", response(200, Location="https://upload.example/session"))
           .on("PUT", "upload.example/session", chunk_answer))
    result = youtube.upload(video, POST, cfg(youtube_privacy="unlisted"), store)
    assert result == "https://youtube.com/shorts/vid123"
    init = api.find("POST", "upload/youtube")[0]
    assert init.params == {"uploadType": "resumable", "part": "snippet,status"}
    assert init.headers["X-Upload-Content-Length"] == str(size) and init.headers["Authorization"] == "Bearer fresh"
    assert init.json["snippet"]["title"] == "Why the Roman Empire fell #shorts"
    assert init.json["status"]["privacyStatus"] == "unlisted"
    ranges = [c.headers["Content-Range"] for c in api.find("PUT", "session")]
    assert ranges == ["bytes 0-262143/614400", "bytes 262144-524287/614400", "bytes 524288-614399/614400"]
    assert b"".join(c.data for c in api.find("PUT", "session")) == video.read_bytes()
    assert store.get("youtube")["access_token"] == "fresh"


def test_youtube_errors(monkeypatch, store, video):
    store.set("youtube", {"refresh_token": "r", "access_token": "tok", "expires_at": time.time() + 3600})
    quota = response(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    FakeApi(monkeypatch).on("POST", "upload/youtube", quota)
    with pytest.raises(UploadError) as err:
        youtube.upload(video, POST, cfg(), store)
    assert err.value.retry and err.value.wait_hours == 12

    FakeApi(monkeypatch).on("POST", "upload/youtube", response(401, {"error": {"message": "bad token"}}))
    with pytest.raises(UploadError) as err:
        youtube.upload(video, POST, cfg(), store)
    assert err.value.relogin and not err.value.retry

    store.update("youtube", expires_at=0)
    FakeApi(monkeypatch).on("POST", "oauth2.googleapis.com/token", response(400, {"error": "invalid_grant"}))
    with pytest.raises(UploadError) as err:
        youtube.upload(video, POST, cfg(), store)
    assert err.value.relogin


def test_youtube_client_file(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text('{"installed": {"client_id": "abc.apps.googleusercontent.com", "client_secret": "xyz"}}')
    assert youtube.read_client_file(path) == ("abc.apps.googleusercontent.com", "xyz")
    path.write_text('{"something": {}}')
    with pytest.raises(UploadError):
        youtube.read_client_file(path)


# ---------------------------------------------------------------- TikTok

MB = 1024 * 1024


def test_tiktok_chunk_rules():
    assert tiktok.chunk_plan(3 * MB) == (3 * MB, 1)
    assert tiktok.chunk_plan(64 * MB) == (64 * MB, 1)
    chunk, count = tiktok.chunk_plan(100 * MB)
    assert (chunk, count) == (32 * MB, 3)
    assert 100 * MB - chunk * (count - 1) <= 128 * MB  # last chunk holds the remainder


def tiktok_ok(data):
    return response(200, {"data": data, "error": {"code": "ok", "message": ""}})


def test_tiktok_draft_upload(monkeypatch, store, video):
    store.set("tiktok", {"client_key": "k", "client_secret": "s", "access_token": "tok", "expires_at": time.time() + 3600})
    api = (FakeApi(monkeypatch)
           .on("POST", "inbox/video/init", tiktok_ok({"publish_id": "p1", "upload_url": "https://upload.tiktok.example/u"}))
           .on("PUT", "upload.tiktok.example", response(201))
           .on("POST", "status/fetch", tiktok_ok({"status": "PROCESSING_UPLOAD"}), tiktok_ok({"status": "SEND_TO_USER_INBOX"})))
    result = tiktok.upload(video, POST, cfg(tiktok_mode="draft"), store)
    assert "inbox" in result
    init = api.find("POST", "inbox/video/init")[0]
    assert init.json == {"source_info": {"source": "FILE_UPLOAD", "video_size": 614400, "chunk_size": 614400, "total_chunk_count": 1}}
    put = api.find("PUT", "upload.tiktok")[0]
    assert put.headers["Content-Range"] == "bytes 0-614399/614400" and put.headers["Content-Type"] == "video/mp4"


def test_tiktok_direct_post_checks_creator_and_explains_audit(monkeypatch, store, video):
    store.set("tiktok", {"access_token": "tok", "expires_at": time.time() + 3600})
    unaudited = response(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}})
    api = (FakeApi(monkeypatch)
           .on("POST", "creator_info/query", tiktok_ok({"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "max_video_post_duration_sec": 600}))
           .on("POST", "post/publish/video/init", unaudited))
    with pytest.raises(UploadError, match="tiktok_mode to draft") as err:
        tiktok.upload(video, POST, cfg(tiktok_mode="direct"), store)
    assert not err.value.retry
    body = api.find("POST", "post/publish/video/init")[0].json
    assert body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE" and body["post_info"]["title"] == POST.caption


def test_tiktok_refreshes_expired_token(monkeypatch, store, video):
    store.set("tiktok", {"client_key": "k", "client_secret": "s", "access_token": "old", "expires_at": 0,
                         "refresh_token": "r1", "refresh_expires_at": time.time() + 1000})
    api = (FakeApi(monkeypatch)
           .on("POST", "oauth/token", response(200, {"access_token": "new", "expires_in": 86400, "refresh_token": "r2", "refresh_expires_in": 31536000, "open_id": "o"}))
           .on("POST", "inbox/video/init", tiktok_ok({"publish_id": "p1", "upload_url": "https://up.example/u"}))
           .on("PUT", "up.example", response(201))
           .on("POST", "status/fetch", tiktok_ok({"status": "SEND_TO_USER_INBOX"})))
    tiktok.upload(video, POST, cfg(), store)
    assert api.find("POST", "oauth/token")[0].form["grant_type"] == "refresh_token"
    assert store.get("tiktok")["refresh_token"] == "r2"
    assert api.find("POST", "inbox/video/init")[0].headers["Authorization"] == "Bearer new"


# ---------------------------------------------------------------- Instagram / Facebook


def test_instagram_reel_upload(monkeypatch, store, video):
    store.set("instagram", {"access_token": "tok", "user_id": "178", "refreshed_at": time.time(), "expires_at": time.time() + 86400})
    api = (FakeApi(monkeypatch)
           .on("POST", r"graph.instagram.com/v25.0/178/media$", response(200, {"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"}))
           .on("POST", "rupload.facebook.com", response(200, {"success": True}))
           .on("GET", r"/c1$", response(200, {"status_code": "IN_PROGRESS"}), response(200, {"status_code": "FINISHED"}))
           .on("POST", "media_publish", response(200, {"id": "m9"}))
           .on("GET", r"/m9$", response(200, {"permalink": "https://www.instagram.com/reel/abc/"})))
    assert instagram.upload(video, POST, cfg(), store) == "https://www.instagram.com/reel/abc/"
    container = api.find("POST", "178/media$")[0].form
    assert container["media_type"] == "REELS" and container["upload_type"] == "resumable" and container["caption"] == POST.caption
    upload = api.find("POST", "rupload")[0]
    assert upload.headers == {"Authorization": "OAuth tok", "offset": "0", "file_size": "614400", "Content-Length": "614400"}
    assert upload.data == video.read_bytes()
    assert api.find("POST", "media_publish")[0].form["creation_id"] == "c1"


def test_instagram_weekly_token_refresh_and_expiry(monkeypatch, store, video):
    store.set("instagram", {"access_token": "old", "user_id": "1", "refreshed_at": time.time() - 8 * 86400, "expires_at": time.time() + 86400})
    FakeApi(monkeypatch).on("GET", "refresh_access_token", response(200, {"access_token": "new", "expires_in": 5184000}))
    assert instagram.access_token(store) == "new"
    assert store.get("instagram")["expires_at"] > time.time() + 59 * 86400

    store.update("instagram", expires_at=time.time() - 1)
    with pytest.raises(UploadError) as err:
        instagram.access_token(store)
    assert err.value.relogin


def test_instagram_error_mapping():
    assert instagram.graph_error(response(400, {"error": {"code": 190, "message": "expired"}}), "Instagram").relogin
    limited = instagram.graph_error(response(400, {"error": {"code": 4, "message": "limit"}}), "Instagram")
    assert limited.retry and limited.wait_hours == 6
    assert not instagram.graph_error(response(400, {"error": {"code": 36003, "message": "aspect ratio"}}), "Instagram").retry


def test_facebook_reel_upload_and_length_limit(monkeypatch, store, video):
    store.set("facebook", {"page_id": "55", "page_token": "ptok"})
    with pytest.raises(UploadError, match="3-90 seconds"):
        facebook.upload(video, PostInfo("t", "", 120), cfg(), store)
    api = (FakeApi(monkeypatch)
           .on("POST", "55/video_reels", response(200, {"video_id": "v7", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/v7"}), response(200, {"success": True}))
           .on("POST", "rupload.facebook.com", response(200, {"success": True})))
    assert facebook.upload(video, POST, cfg(), store) == "https://www.facebook.com/reel/v7"
    start, finish = api.find("POST", "video_reels")
    assert start.form["upload_phase"] == "start"
    assert finish.form == {"upload_phase": "finish", "video_id": "v7", "video_state": "PUBLISHED", "description": POST.caption, "access_token": "ptok"}


# ---------------------------------------------------------------- the queue


class FakePlatform:
    KEY = "youtube"
    NAME = "YouTube Shorts"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.uploaded = []

    def upload(self, video, post, cfg, store, should_stop):
        self.uploaded.append(video)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def queue_setup(tmp_path, monkeypatch, store):
    state = State(tmp_path / "state.json")
    store.set("youtube", {"refresh_token": "r"})
    config = cfg(youtube=True, tiktok=True, hours_between_posts=3)
    q = UploadQueue(config, state, store, state.save)
    return q, state, config


def make_reels(tmp_path, count):
    paths = []
    for i in range(count):
        p = tmp_path / f"reel{i}.mp4"
        p.write_bytes(b"x")
        paths.append(p)
    return paths


def test_queue_posts_to_connected_platforms_spaced_out(tmp_path, monkeypatch, queue_setup):
    q, state, config = queue_setup
    fake = FakePlatform(["https://youtube.com/shorts/a", "https://youtube.com/shorts/b"])
    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", fake)
    reels = make_reels(tmp_path, 2)
    for reel in reels:
        assert q.add(reel, POST) == ["YouTube Shorts"]  # TikTok is switched on but not connected
    assert q.run_due() == 1
    assert q.run_due() == 0  # 3 hours between posts
    state.data["last_post"]["youtube"] -= 3 * 3600 + 1
    assert q.run_due() == 1
    assert fake.uploaded == reels  # oldest first
    assert q.summary() == {"youtube": {"done": 2}}


def test_queue_retries_then_gives_up(tmp_path, monkeypatch, queue_setup):
    q, state, config = queue_setup
    config.upload.hours_between_posts = 0
    fake = FakePlatform([UploadError("network problem")] * 10)
    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", fake)
    (reel,) = make_reels(tmp_path, 1)
    q.add(reel, POST)
    for _ in range(queue_module.MAX_ATTEMPTS):
        for item in q.items.values():
            item["next_try"] = 0
        q.run_due()
    item = next(iter(q.items.values()))
    assert item["status"] == "failed" and item["attempts"] == queue_module.MAX_ATTEMPTS
    assert len(fake.uploaded) == queue_module.MAX_ATTEMPTS


def test_queue_pauses_platform_when_login_expires(tmp_path, monkeypatch, queue_setup):
    q, state, config = queue_setup
    fake = FakePlatform([UploadError("login expired", relogin=True), "ok"])
    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", fake)
    (reel,) = make_reels(tmp_path, 1)
    q.add(reel, POST)
    assert q.run_due() == 0
    assert "youtube" in q.blocked and q.pending("youtube")
    assert q.run_due() == 0 and len(fake.uploaded) == 1  # waits for a new login
    q.reconnected("youtube")
    assert q.run_due() == 1


def test_queue_skips_deleted_reels(tmp_path, monkeypatch, queue_setup):
    q, state, config = queue_setup
    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", FakePlatform([]))
    (reel,) = make_reels(tmp_path, 1)
    q.add(reel, POST)
    reel.unlink()
    assert q.run_due() == 0
    assert next(iter(q.items.values()))["status"] == "failed"


def test_real_network_errors_become_upload_errors(monkeypatch):
    with pytest.raises(UploadError, match="network problem"):
        http.request("GET", "http://127.0.0.1:9/nothing-listens-here", timeout=2)
