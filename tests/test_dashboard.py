"""The phone dashboard: locked without the key, shows what's waiting, takes approvals and links."""

import json
import queue
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from brainrot_bot.config import DEFAULTS
from brainrot_bot.credentials import Credentials
from brainrot_bot.dashboard import Dashboard, dashboard_data, get_token, qr_text
from brainrot_bot.phone import Action


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, method="GET", body=None, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def running(tmp_path):
    output = tmp_path / "output"
    (output / "Podcast A").mkdir(parents=True)
    reel = output / "Podcast A" / "ep1.mp4"
    reel.write_bytes(bytes(range(256)) * 40)
    reel.with_suffix(".jpg").write_bytes(b"\xff\xd8jpeg")
    state = {
        "uploads": {
            f"youtube|{reel}": {"platform": "youtube", "account": "youtube", "video": str(reel), "status": "waiting",
                                "folder": "Podcast A", "post": {"title": "Big <b>title</b>"}, "added": 1},
            f"tiktok|{reel}": {"platform": "tiktok", "account": "tiktok", "video": str(reel), "status": "waiting",
                               "folder": "Podcast A", "post": {"title": "Big <b>title</b>"}, "added": 1},
            "youtube|old": {"platform": "youtube", "account": "youtube", "video": str(output / "old.mp4"), "status": "done",
                            "folder": "Podcast A", "post": {"title": "Old"}, "posted": time.time() - 100, "stats": {"views": 1500, "likes": 20},
                            "url": "https://youtube.com/shorts/x", "gameplay": ["mc.mp4"]},
        },
        "clips": {"Podcast A/ep1.mp4": {"status": "done", "outputs": [str(reel)]}},
        "links": {},
    }
    cfg = SimpleNamespace(dashboard=SimpleNamespace(enabled=True, port=0, lan=False), phone=SimpleNamespace(**DEFAULTS["phone"]),
                          paths=SimpleNamespace(output=output))
    creds = Credentials(tmp_path / "credentials", encrypt=False)
    inbox = queue.Queue()
    board = Dashboard(cfg, creds, inbox, lambda: state, lambda: "watching for new clips", lambda: ["Podcast A", "Podcast B"])
    assert board.start()
    base = f"http://127.0.0.1:{board.server.server_address[1]}"
    yield SimpleNamespace(base=base, token=get_token(creds), inbox=inbox, reel=reel, state=state)
    board.stop()


def login(running):
    status, headers, _ = fetch(f"{running.base}/?key={running.token}")
    assert status == 302 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    return {"Cookie": cookie}


def test_locked_without_the_key(running):
    assert fetch(f"{running.base}/")[0] == 403
    assert fetch(f"{running.base}/api/state")[0] == 403
    assert fetch(f"{running.base}/?key=wrong")[0] == 403
    assert fetch(f"{running.base}/api/action", "POST", {"kind": "pause"})[0] == 403


def test_page_state_and_media(running):
    cookie = login(running)
    status, _, page = fetch(f"{running.base}/", headers=cookie)
    assert status == 200 and b"Waiting for your OK" in page
    status, _, raw = fetch(f"{running.base}/api/state", headers=cookie)
    data = json.loads(raw)
    (waiting,) = data["waiting"]
    assert waiting["accounts"] == ["YouTube Shorts", "TikTok"] and waiting["title"] == "Big <b>title</b>"
    assert waiting["cover"] == "/media?f=Podcast%20A/ep1.jpg"
    assert data["summary"]["best_gameplay"][0]["name"] == "mc.mp4" and data["folders"] == ["Podcast A", "Podcast B"]
    status, headers, jpg = fetch(running.base + waiting["cover"], headers=cookie)
    assert status == 200 and jpg == b"\xff\xd8jpeg" and headers["Content-Type"] == "image/jpeg"
    status, headers, part = fetch(running.base + waiting["play"], headers={**cookie, "Range": "bytes=100-199"})
    assert status == 206 and part == (bytes(range(256)) * 40)[100:200]
    assert headers["Content-Range"] == f"bytes 100-199/{256 * 40}"
    assert fetch(f"{running.base}/media?f=../credentials", headers=cookie)[0] == 403  # only the reels folder


def test_actions_need_the_page_header_and_go_to_the_bot(running):
    cookie = login(running)
    url = f"{running.base}/api/action"
    assert fetch(url, "POST", {"kind": "approve", "video": str(running.reel)}, cookie)[0] == 403  # no X-Brainrot header
    headers = {**cookie, "X-Brainrot": "1"}
    assert fetch(url, "POST", {"kind": "approve", "video": str(running.reel)}, headers)[0] == 200
    assert running.inbox.get_nowait() == Action("approve", video=str(running.reel))
    assert fetch(url, "POST", {"kind": "skip", "video": "/not/waiting.mp4"}, headers)[0] == 404
    status, _, raw = fetch(url, "POST", {"kind": "link", "url": "see https://youtu.be/abc", "folder": "Podcast: B"}, headers)
    assert status == 200 and json.loads(raw)["folder"] == "Podcast B"
    assert running.inbox.get_nowait() == Action("link", url="https://youtu.be/abc", folder="Podcast B")
    assert fetch(url, "POST", {"kind": "link", "url": "nope"}, headers)[0] == 400
    assert fetch(url, "POST", {"kind": "pause"}, headers)[0] == 200 and running.inbox.get_nowait() == Action("pause")


def test_qr_code_for_the_menu():
    pytest.importorskip("segno")
    text = qr_text("http://192.168.1.20:8770/?key=abc")
    assert len(text.splitlines()) > 10 and "█" in text or "▀" in text or "▄" in text


def test_dashboard_data_without_files(tmp_path):
    data = dashboard_data({"uploads": {"x": {"platform": "youtube", "video": "/elsewhere/a.mp4", "status": "waiting"}}}, "ok", tmp_path, [])
    assert data["waiting"][0]["cover"] == "" and data["status"] == "ok"
