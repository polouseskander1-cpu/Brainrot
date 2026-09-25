"""Telegram and Discord: previews with buttons, approvals, links and commands (fake APIs, no network)."""

import json
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from brainrot_bot.config import DEFAULTS
from brainrot_bot.credentials import Credentials
from brainrot_bot.phone import Action, Discord, Phone, Telegram, clean_folder_name, make_preview
from test_uploads import FakeApi, response


def phone_cfg(**phone):
    values = dict(DEFAULTS["phone"])
    values.update(phone)
    return SimpleNamespace(phone=SimpleNamespace(**values))


def tg_ok(result):
    return response(200, {"ok": True, "result": result})


def jresp(status, value):
    """A JSON answer of any kind (Discord answers lists too)."""
    return response(status, json.dumps(value).encode())


@pytest.fixture
def files(tmp_path):
    preview = tmp_path / "preview.mp4"
    preview.write_bytes(b"mp4" * 100)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpg" * 10)
    return preview, cover


def make_phone(tmp_path, creds=None, **settings):
    creds = creds or Credentials(tmp_path / "credentials", encrypt=False)
    return Phone(phone_cfg(**settings), creds, None, tmp_path / "phone_state.json", tmp_path / "work")


# ------------------------------------------------------------------ Telegram


def test_telegram_sends_the_preview_with_buttons(monkeypatch, files):
    preview, cover = files
    api = FakeApi(monkeypatch).on("POST", "bot123:abc/sendVideo", tg_ok({"message_id": 77}))
    bot = Telegram("123:abc", "42")
    assert bot.send_reel("ref1", "New reel: <b>x</b>", preview, cover, waiting=True, has_targets=True) == "77"
    body = api.find("POST", "sendVideo")[0].data
    assert b'name="chat_id"\r\n\r\n42' in body and b"New reel: &lt;b&gt;x&lt;/b&gt;" in body
    markup = json.loads(re.search(rb'name="reply_markup"\r\n\r\n(.*?)\r\n', body).group(1))
    assert [b["callback_data"] for b in markup["inline_keyboard"][0]] == ["a|ref1", "n|ref1", "s|ref1"]
    assert b'name="video"; filename="preview.mp4"' in body


def test_telegram_falls_back_to_the_cover_and_to_text(monkeypatch, files):
    preview, cover = files
    api = (FakeApi(monkeypatch).on("POST", "sendPhoto", tg_ok({"message_id": 1})).on("POST", "sendMessage", tg_ok({"message_id": 2})))
    bot = Telegram("t", "42")
    bot.send_reel("r", "text", None, cover, waiting=False, has_targets=True)
    markup = json.loads(re.search(rb'name="reply_markup"\r\n\r\n(.*?)\r\n', api.find("POST", "sendPhoto")[0].data).group(1))
    assert markup["inline_keyboard"][0][0]["callback_data"] == "s|r"  # only "don't post it" without approval
    bot.send_reel("r", "text", None, None, waiting=False, has_targets=False)
    assert api.find("POST", "sendMessage")[0].json["text"] == "text"


def test_telegram_taps_links_and_commands(monkeypatch, tmp_path):
    phone = make_phone(tmp_path)
    phone.remember(refs={"ref1": "/reels/a.mp4"})
    phone.folders_provider = lambda: ["Podcast A", "Podcast B"]
    phone.status_provider = lambda: "all good"
    updates = [
        {"update_id": 10, "callback_query": {"id": "q1", "data": "a|ref1", "message": {"message_id": 5, "chat": {"id": 42}}}},
        {"update_id": 11, "callback_query": {"id": "q2", "data": "s|ref1", "message": {"message_id": 5, "chat": {"id": 999}}}},  # a stranger
        {"update_id": 12, "message": {"chat": {"id": 42}, "text": "/status"}},
        {"update_id": 13, "message": {"chat": {"id": 42}, "text": "look https://youtu.be/abc please"}},
    ]
    api = (FakeApi(monkeypatch)
           .on("POST", "getUpdates", tg_ok(updates))
           .on("POST", "editMessageReplyMarkup", tg_ok(True))
           .on("POST", "answerCallbackQuery", tg_ok(True))
           .on("POST", "sendMessage", tg_ok({"message_id": 9})))
    bot = Telegram("t", "42")
    bot.poll(phone)
    assert phone.memory["telegram_offset"] == 14
    assert phone.actions.get_nowait() == Action("approve", video="/reels/a.mp4")
    assert phone.actions.empty()  # the stranger's tap was ignored
    messages = [c.json for c in api.find("POST", "sendMessage")]
    assert messages[0]["text"] == "all good"
    keyboard = messages[1]["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in keyboard] == ["Podcast A", "Podcast B", "➕ New folder"]
    link_id = keyboard[1][0]["callback_data"].split("|")[1]

    # Pick "Podcast B" for the link.
    FakeApi(monkeypatch).on("POST", "getUpdates", tg_ok([
        {"update_id": 14, "callback_query": {"id": "q3", "data": f"f|{link_id}|1", "message": {"message_id": 6, "chat": {"id": 42}}}}]))\
        .on("POST", "answerCallbackQuery", tg_ok(True)).on("POST", "sendMessage", tg_ok({}))
    bot.poll(phone)
    assert phone.actions.get_nowait() == Action("link", url="https://youtu.be/abc", folder="Podcast B")


def test_telegram_new_folder_by_name(monkeypatch, tmp_path):
    phone = make_phone(tmp_path)
    phone.folders_provider = lambda: []
    link_id, _ = phone.new_link("https://tiktok.com/@a/video/1")
    FakeApi(monkeypatch).on("POST", "getUpdates", tg_ok([
        {"update_id": 1, "callback_query": {"id": "q", "data": f"fn|{link_id}", "message": {"message_id": 6, "chat": {"id": 42}}}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "My: New/Show?"}}]))\
        .on("POST", "answerCallbackQuery", tg_ok(True)).on("POST", "sendMessage", tg_ok({}))
    Telegram("t", "42").poll(phone)
    assert phone.actions.get_nowait() == Action("link", url="https://tiktok.com/@a/video/1", folder="My NewShow")


def test_telegram_setup_finds_your_chat(monkeypatch):
    api = (FakeApi(monkeypatch)
           .on("POST", "getMe", tg_ok({"username": "my_reels_bot"}))
           .on("POST", "getUpdates", tg_ok([]), tg_ok([{"update_id": 3, "message": {"chat": {"id": 555}, "text": "hi"}}]), tg_ok([])))
    assert Telegram.check_token("t") == "my_reels_bot"
    assert Telegram("t", "").wait_for_chat(30) == "555"
    assert api.find("POST", "getUpdates")[-1].json == {"offset": 4, "timeout": 0}


# ------------------------------------------------------------------ Discord


def test_discord_posts_the_preview_and_reactions(monkeypatch, files):
    preview, cover = files
    api = (FakeApi(monkeypatch)
           .on("POST", r"/channels/c1/messages$", response(200, {"id": "m1"}))
           .on("PUT", r"/reactions/", response(204)))
    bot = Discord("tok", "c1")
    assert bot.send_reel("r", "New reel", preview, cover, waiting=True, has_targets=True) == "m1"
    post = api.find("POST", "/messages")[0]
    assert post.headers["Authorization"] == "Bot tok"
    payload = json.loads(re.search(rb'name="payload_json"\r\n\r\n(.*?)\r\n', post.data).group(1))
    assert "React ✅" in payload["content"] and payload["attachments"] == [{"id": 0, "filename": "preview.mp4"}]
    assert [unquote(c.url.split("/reactions/")[1].split("/")[0]) for c in api.find("PUT", "reactions")] == ["✅", "\U0001F680", "❌"]


def test_discord_reactions_links_and_commands(monkeypatch, tmp_path):
    phone = make_phone(tmp_path)
    phone.remember(refs={"r": "/reels/b.mp4"}, discord_pending={"m1": "r"}, discord_after="100")
    phone.status_provider = lambda: "busy"
    me = response(200, {"id": "bot1", "username": "reels"})
    no_one = jresp(200, [])
    api = (FakeApi(monkeypatch)
           .on("GET", r"/users/@me$", me)
           .on("GET", r"reactions/%E2%9C%85$", jresp(200, [{"id": "bot1", "bot": True}]))  # only the bot's own
           .on("GET", r"reactions/%F0%9F%9A%80$", jresp(200, [{"id": "u7"}]))  # you tapped the rocket
           .on("GET", r"reactions/%E2%9D%8C$", no_one)
           .on("GET", r"/channels/c1/messages$", jresp(200, [
               {"id": "102", "author": {"id": "u7"}, "content": "status"},
               {"id": "101", "author": {"id": "u7"}, "content": "https://youtu.be/xyz Joe Show"},
               {"id": "103", "author": {"id": "u9", "bot": True}, "content": "https://spam.example"}]))
           .on("POST", r"/channels/c1/messages$", response(200, {"id": "reply"})))
    bot = Discord("tok", "c1")
    bot.poll(phone)
    assert phone.actions.get_nowait() == Action("now", video="/reels/b.mp4")
    assert phone.actions.get_nowait() == Action("link", url="https://youtu.be/xyz", folder="Joe Show")
    assert phone.actions.empty()
    assert phone.memory["discord_pending"] == {} and phone.memory["discord_after"] == "103"
    replies = [c.json["content"] for c in api.find("POST", "/messages")]
    assert replies[0].startswith("b.mp4: \U0001F680") and replies[2] == "busy"
    assert api.find("GET", "/channels/c1/messages$")[0].params == {"after": "100", "limit": 20}


def test_discord_waits_when_asked_to_slow_down(monkeypatch):
    FakeApi(monkeypatch).on("GET", "/users/@me", response(429, {"retry_after": 0.5}), response(200, {"id": "1", "username": "b"}))
    slept = []
    monkeypatch.setattr("brainrot_bot.phone.time.sleep", slept.append)
    assert Discord("t", "c").call("GET", "/users/@me")["username"] == "b"
    assert slept == [0.5]


# ------------------------------------------------------------------ the hub


def test_phone_connects_only_what_is_switched_on(tmp_path):
    creds = Credentials(tmp_path / "credentials", encrypt=False)
    creds.set("telegram", {"token": "t", "chat_id": "1"})
    creds.set("discord", {"token": "d", "channel_id": "c"})
    assert make_phone(tmp_path, creds).channels == []
    phone = make_phone(tmp_path, creds, telegram=True, approval=True)
    assert [c.name for c in phone.channels] == ["Telegram"] and phone.approval
    assert [c.name for c in make_phone(tmp_path, creds, telegram=True, discord=True).channels] == ["Telegram", "Discord"]


def test_phone_outbox_problems_and_commands(tmp_path, files):
    preview, cover = files
    creds = Credentials(tmp_path / "credentials", encrypt=False)
    creds.set("telegram", {"token": "t", "chat_id": "1"})
    phone = make_phone(tmp_path, creds, telegram=True, preview=False)

    class Recorder:
        name = "fake"

        def __init__(self):
            self.sent = []

        def send_reel(self, *args):
            self.sent.append(("reel",) + args)
            return "m"

        def send_text(self, text):
            self.sent.append(("text", text))

    fake = Recorder()
    phone.channels = [fake]
    phone.reel_ready(Path("/reels/c.mp4"), cover, "Big title", "Podcast A", 31.4, ["YouTube Shorts"], waiting=True)
    phone.problem("YouTube login expired", "relogin:youtube")
    phone.problem("YouTube login expired", "relogin:youtube")  # not twice in a row
    phone._send_waiting()
    (reel, ref, text, got_preview, got_cover, waiting, has_targets), problem = fake.sent
    assert phone.video_for(ref) == "/reels/c.mp4" and got_preview is None and got_cover == cover
    assert "Big title" in text and "Podcast A - 31s" in text and "Post on: YouTube Shorts" in text and waiting
    assert problem == ("text", "⚠️ YouTube login expired")
    assert json.loads((tmp_path / "phone_state.json").read_text())["refs"][ref] == "/reels/c.mp4"  # survives restarts

    assert phone.command("/pause") and phone.actions.get_nowait() == Action("pause")
    assert phone.command("Stats@my_bot") == "No stats yet."
    assert phone.command("hello") == ""
    done = []
    phone.actions.put(Action("skip", video="x"))
    phone.drain(done.append)
    assert done == [Action("skip", video="x")]


def test_folder_names_from_the_phone():
    assert clean_folder_name('  Joe: "The" Show?  ') == "Joe The Show"
    assert clean_folder_name("...") == "From phone"


def test_small_preview_for_phones(tmp_path):
    from brainrot_bot.media import MediaError, find_tools, probe

    try:
        tools = find_tools()
    except MediaError:
        pytest.skip("needs ffmpeg")
    video = tmp_path / "reel.mp4"
    subprocess.run([tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30",
                    "-f", "lavfi", "-i", "sine", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(video)], check=True)
    preview = make_preview(tools, video, tmp_path / "small.mp4")
    info = probe(tools, preview)
    assert (info.width, info.height) == (540, 960) and info.has_audio
    assert preview.stat().st_size < video.stat().st_size
