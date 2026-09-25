"""X and Pinterest, stats, several accounts per platform, posting times and per-platform texts."""

import datetime as dt
import json
import re
import time
from types import SimpleNamespace

import pytest

from brainrot_bot.config import DEFAULTS, load_config
from brainrot_bot.credentials import Credentials
from brainrot_bot.state import State
from brainrot_bot.uploads import PostInfo, UploadError, UploadQueue, facebook, instagram, pinterest, tiktok, x, youtube
from brainrot_bot.uploads import queue as queue_module
from brainrot_bot.uploads.accounts import account_id, folder_choices, set_folder_account, split_account
from brainrot_bot.uploads.post import weighted_length
from test_uploads import FakeApi, response, tiktok_ok

POST = PostInfo("Why the Roman Empire fell", "#history #rome #fyp", 42.0, "The real reason nobody tells you")


def cfg(tmp_path=None, **upload):
    values = dict(DEFAULTS["upload"])
    values.update(post_times=[])
    values.update(upload)
    c = SimpleNamespace(upload=SimpleNamespace(**values))
    if tmp_path is not None:
        c.paths = SimpleNamespace(clips=tmp_path / "clips")
    return c


@pytest.fixture
def store(tmp_path):
    return Credentials(tmp_path / "credentials", encrypt=False)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "reel.mp4"
    path.write_bytes(b"v" * (9 * 1024 * 1024 + 5))  # 3 pieces for X
    return path


# ------------------------------------------------------------------ per-platform texts


def test_texts_follow_each_platforms_rules():
    post = PostInfo("Title here", "#a #b #c #d #e #f #g", 30, "Caption here")
    assert post.render("instagram") == "Caption here\n\n#a #b #c #d #e"  # Instagram: 5 hashtags at most
    assert post.render("x") == "Caption here #a #b"
    assert post.render("tiktok").endswith("#g")
    custom = {"tiktok": "{title} | {caption}\\n{hashtags}"}
    assert post.render("tiktok", custom) == "Title here | Caption here\n#a #b #c #d #e #f #g"
    long = PostInfo("t", "#one #two", 30, "ä" * 50 + "表" * 200)
    text = long.render("x")
    assert weighted_length(text) <= 280 and text.endswith("... #one #two")


def test_post_times_setting(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("upload:\n  post_times: [12:00, \"18:30\", 7:05]\n", encoding="utf-8")
    assert load_config(path).upload.post_times == ["12:00", "18:30", "07:05"]
    path.write_text("upload:\n  post_times: auto\n", encoding="utf-8")
    assert load_config(path).upload.post_times == "auto"
    path.write_text("upload:\n  post_times: []\n", encoding="utf-8")
    assert load_config(path).upload.post_times == []
    path.write_text("upload:\n  templates:\n    x: '{caption}'\n", encoding="utf-8")
    templates = load_config(path).upload.templates
    assert templates["x"] == "{caption}" and templates["tiktok"] == DEFAULTS["upload"]["templates"]["tiktok"]


# ------------------------------------------------------------------ X


def test_x_upload_in_pieces_then_post(monkeypatch, store, video):
    store.set("x", {"client_id": "cid", "client_secret": "sec", "access_token": "tok", "expires_at": time.time() + 3600,
                    "refresh_token": "r", "username": "me"})
    api = (FakeApi(monkeypatch)
           .on("POST", r"/2/media/upload/initialize$", response(200, {"data": {"id": "m1"}}))
           .on("POST", r"/2/media/upload/m1/append$", response(204))
           .on("POST", r"/2/media/upload/m1/finalize$", response(200, {"data": {"id": "m1", "processing_info": {"state": "pending", "check_after_secs": 1}}}))
           .on("GET", r"/2/media/upload$", response(200, {"data": {"processing_info": {"state": "in_progress"}}}),
               response(200, {"data": {"processing_info": {"state": "succeeded"}}}))
           .on("POST", r"/2/tweets$", response(201, {"data": {"id": "t9", "text": "x"}})))
    result = x.upload(video, POST, cfg(), store)
    assert (result.url, result.post_id) == ("https://x.com/me/status/t9", "t9")
    init = api.find("POST", "initialize")[0]
    assert init.json == {"media_type": "video/mp4", "total_bytes": 9 * 1024 * 1024 + 5, "media_category": "tweet_video"}
    appends = api.find("POST", "append")
    assert len(appends) == 3
    assert [re.search(rb'name="segment_index"\r\n\r\n(\d+)', a.data).group(1) for a in appends] == [b"0", b"1", b"2"]
    assert appends[0].headers["Content-Type"].startswith("multipart/form-data; boundary=")
    tweet = api.find("POST", "tweets")[0].json
    assert tweet == {"text": "The real reason nobody tells you #history #rome", "media": {"media_ids": ["m1"]}}


def test_x_refresh_uses_basic_auth_and_errors(monkeypatch, store, video):
    store.set("x", {"client_id": "cid", "client_secret": "sec", "access_token": "old", "expires_at": 0, "refresh_token": "r1"})
    api = FakeApi(monkeypatch).on("POST", "oauth2/token", response(200, {"access_token": "new", "expires_in": 7200, "refresh_token": "r2"}))
    assert x.access_token(store) == "new"
    call = api.find("POST", "oauth2/token")[0]
    assert call.headers["Authorization"].startswith("Basic ") and call.form == {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "cid"}
    assert store.get("x")["refresh_token"] == "r2"
    with pytest.raises(UploadError, match="140 seconds"):
        x.upload(video, PostInfo("t", "", 200), cfg(), store)
    FakeApi(monkeypatch).on("POST", "initialize", response(402, {"title": "CreditsDepleted", "detail": "no credits"}))
    with pytest.raises(UploadError, match="credits") as err:
        x.upload(video, POST, cfg(), store)
    assert err.value.wait_hours == 24


def test_x_stats(monkeypatch, store):
    store.set("x", {"access_token": "tok", "expires_at": time.time() + 3600})
    FakeApi(monkeypatch).on("GET", "/2/tweets/t9", response(200, {"data": {"public_metrics": {
        "impression_count": 5000, "like_count": 300, "reply_count": 12, "retweet_count": 20, "quote_count": 2}}}))
    assert x.stats("t9", cfg(), store) == {"views": 5000, "likes": 300, "comments": 12, "shares": 22}


# ------------------------------------------------------------------ Pinterest


def test_pinterest_video_pin(monkeypatch, store, video):
    store.set("pinterest", {"app_id": "a", "app_secret": "s", "access_token": "tok", "expires_at": time.time() + 20 * 86400,
                            "refresh_token": "r", "refresh_expires_at": time.time() + 50 * 86400, "board_id": "b7"})
    api = (FakeApi(monkeypatch)
           .on("POST", r"api.pinterest.com/v5/media$", response(201, {"media_id": "123", "media_type": "video",
               "upload_url": "https://pinterest-media-upload.s3.example/", "upload_parameters": {"key": "k1", "policy": "p"}}))
           .on("POST", r"s3.example", response(204))
           .on("GET", r"/v5/media/123$", response(200, {"status": "processing"}), response(200, {"status": "succeeded"}))
           .on("POST", r"/v5/pins$", response(201, {"id": "p55"})))
    result = pinterest.upload(video, POST, cfg(), store)
    assert (result.url, result.post_id) == ("https://www.pinterest.com/pin/p55/", "p55")
    s3 = api.find("POST", "s3.example")[0]
    assert b'name="key"\r\n\r\nk1' in s3.data and b'name="file"; filename="reel.mp4"' in s3.data
    assert "Authorization" not in s3.headers
    pin = api.find("POST", "/v5/pins")[0].json
    assert pin["board_id"] == "b7" and pin["title"] == "Why the Roman Empire fell"
    assert pin["media_source"] == {"source_type": "video_id", "media_id": "123", "cover_image_key_frame_time": 1}


def test_pinterest_needs_a_board_and_refreshes(monkeypatch, store, video):
    store.set("pinterest", {"access_token": "tok", "expires_at": time.time() + 3600})
    with pytest.raises(UploadError, match="board"):
        pinterest.upload(video, POST, cfg(), store)
    store.set("pinterest", {"app_id": "a", "app_secret": "s", "access_token": "tok", "expires_at": time.time() + 3 * 86400,
                            "refresh_token": "r", "refresh_expires_at": time.time() + 50 * 86400})
    api = FakeApi(monkeypatch).on("POST", "oauth/token", response(200, {"access_token": "new", "expires_in": 2592000,
                                                                      "refresh_token": "r2", "refresh_token_expires_in": 5184000}))
    assert pinterest.access_token(store) == "new"  # a week before it runs out
    assert api.find("POST", "oauth/token")[0].headers["Authorization"].startswith("Basic ")
    store.set("pinterest", {"refresh_token": "r", "refresh_expires_at": time.time() - 1})
    with pytest.raises(UploadError) as err:
        pinterest.access_token(store)
    assert err.value.relogin


def test_pinterest_stats(monkeypatch, store):
    store.set("pinterest", {"access_token": "tok", "expires_at": time.time() + 20 * 86400})
    FakeApi(monkeypatch).on("GET", "/v5/pins/p55/analytics", response(200, {"all": {
        "summary_metrics": {"IMPRESSION": 900, "SAVE": 30, "VIDEO_MRC_VIEW": 400},
        "lifetime_metrics": {"TOTAL_REACTIONS": 25, "TOTAL_COMMENTS": 3}}}))
    assert pinterest.stats("p55", cfg(), store) == {"views": 400, "likes": 25, "comments": 3, "saves": 30}


# ------------------------------------------------------------------ stats of the other platforms


def test_other_platform_stats(monkeypatch, store):
    now = time.time()
    store.set("youtube", {"access_token": "y", "expires_at": now + 3600})
    store.set("tiktok", {"access_token": "t", "expires_at": now + 3600})
    store.set("instagram", {"access_token": "i", "refreshed_at": now, "expires_at": now + 86400})
    store.set("facebook", {"page_token": "f"})
    api = (FakeApi(monkeypatch)
           .on("GET", "youtube/v3/videos", response(200, {"items": [{"statistics": {"viewCount": "1200", "likeCount": "80", "commentCount": "4"}}]}))
           .on("POST", "video/query", tiktok_ok({"videos": [{"view_count": 9000, "like_count": 700, "comment_count": 20, "share_count": 15}]}))
           .on("GET", r"graph.instagram.com/v25.0/m9$", response(200, {"like_count": 50, "comments_count": 5}))
           .on("GET", r"m9/insights", response(200, {"data": [{"name": "views", "values": [{"value": 3000}]}, {"name": "shares", "values": [{"value": 9}]}]}))
           .on("GET", r"graph.facebook.com/v25.0/v7$", response(200, {"likes": {"summary": {"total_count": 11}}, "comments": {"summary": {"total_count": 2}}}))
           .on("GET", r"v7/video_insights", response(400, {"error": {"message": "needs read_insights"}})))
    assert youtube.stats("vid", cfg(), store) == {"views": 1200, "likes": 80, "comments": 4}
    assert tiktok.stats("tt1", cfg(), store) == {"views": 9000, "likes": 700, "comments": 20, "shares": 15}
    assert api.find("POST", "video/query")[0].json == {"filters": {"video_ids": ["tt1"]}}
    assert instagram.stats("m9", cfg(), store) == {"likes": 50, "comments": 5, "views": 3000, "shares": 9}
    assert facebook.stats("v7", cfg(), store) == {"likes": 11, "comments": 2}  # views need an extra permission


# ------------------------------------------------------------------ several accounts


def test_account_ids_and_folder_files(tmp_path):
    assert account_id("youtube") == "youtube" and account_id("youtube", "Main") == "youtube"
    assert account_id("tiktok", "Gaming") == "tiktok:gaming" and split_account("tiktok:gaming") == ("tiktok", "gaming")
    clips = tmp_path / "clips"
    (clips / "Podcast A" / "Season 2").mkdir(parents=True)
    set_folder_account(clips, "instagram", "off")
    set_folder_account(clips / "Podcast A", "youtube", "podcasts")
    set_folder_account(clips / "Podcast A", "youtube", "clips")  # changed, not added twice
    (clips / "Podcast A" / "Season 2" / "accounts.txt").write_text("tiktok: second  # comment\n")
    assert folder_choices(clips / "Podcast A" / "Season 2", clips) == {"instagram": "off", "youtube": "clips", "tiktok": "second"}
    assert (clips / "Podcast A" / "accounts.txt").read_text().count("youtube") == 1


def queue_for(tmp_path, store, **upload):
    state = State(tmp_path / "state.json")
    c = cfg(tmp_path, youtube=True, tiktok=True, instagram=True, **upload)
    return UploadQueue(c, state, store, state.save), state


class Recorder:
    def __init__(self, key):
        self.KEY, self.NAME = key, key.title()
        self.calls = []

    def upload(self, video, post, cfg, store, should_stop, account=None):
        self.calls.append(account)
        return queue_module.Posted(f"posted to {account}", "", f"id-{len(self.calls)}")


def test_each_folder_posts_to_its_own_accounts(tmp_path, store, monkeypatch):
    for account in ("youtube", "youtube:gaming", "tiktok", "instagram"):
        store.set(account, {"token": account})
    q, state = queue_for(tmp_path, store, hours_between_posts=0)
    clips = tmp_path / "clips"
    (clips / "Gaming").mkdir(parents=True)
    (clips / "Talk").mkdir(parents=True)
    (clips / "Gaming" / "accounts.txt").write_text("youtube = gaming\ninstagram = off\n")
    fakes = {key: Recorder(key) for key in ("youtube", "tiktok", "instagram")}
    for key, fake in fakes.items():
        monkeypatch.setitem(queue_module.PLATFORMS, key, fake)
    reel1, reel2 = tmp_path / "a.mp4", tmp_path / "b.mp4"
    reel1.write_bytes(b"x")
    reel2.write_bytes(b"x")
    assert q.add(reel1, POST, clips / "Gaming") == ["Youtube (gaming)", "Tiktok"]
    assert q.add(reel2, POST, clips / "Talk") == ["Youtube", "Tiktok", "Instagram"]
    posted = 0
    for _ in range(3):
        posted += q.run_due()
    assert posted == 5
    assert sorted(fakes["youtube"].calls) == ["youtube", "youtube:gaming"]
    assert fakes["instagram"].calls == ["instagram"]
    assert q.items[f"youtube:gaming|{reel1}"]["post_id"]


def test_missing_named_account_is_not_replaced_by_the_main_one(tmp_path, store):
    store.set("youtube", {"token": "t"})
    q, state = queue_for(tmp_path, store)
    (tmp_path / "clips" / "Gaming").mkdir(parents=True)
    (tmp_path / "clips" / "Gaming" / "accounts.txt").write_text("youtube = gaming\n")
    assert q.accounts_for(tmp_path / "clips" / "Gaming") == []


def test_spacing_is_per_account(tmp_path, store, monkeypatch):
    for account in ("youtube", "youtube:second"):
        store.set(account, {"token": account})
    q, state = queue_for(tmp_path, store, hours_between_posts=3)
    fake = Recorder("youtube")
    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", fake)
    (tmp_path / "clips" / "B").mkdir(parents=True)
    (tmp_path / "clips" / "B" / "accounts.txt").write_text("youtube = second\n")
    for i, folder in enumerate(("A", "B", "A")):
        reel = tmp_path / f"r{i}.mp4"
        reel.write_bytes(b"x")
        q.add(reel, POST, tmp_path / "clips" / folder)
    assert q.run_due() == 2  # one per account
    assert q.run_due() == 0  # both wait 3 hours now


# ------------------------------------------------------------------ posting times


def at(day, hour, minute=0):
    return dt.datetime(2026, 9, day, hour, minute).timestamp()


def test_next_slot():
    slots = queue_module.parse_times(["18:30", "12:00", "bad", "25:00"])
    assert slots == [(12, 0), (18, 30)]
    assert queue_module.next_slot(at(10, 9), slots) == at(10, 12)
    assert queue_module.next_slot(at(10, 12), slots) == at(10, 18, 30)
    assert queue_module.next_slot(at(10, 19), slots) == at(11, 12)


def test_posting_waits_for_the_next_slot(tmp_path, store, monkeypatch):
    store.set("youtube", {"token": "t"})
    q, state = queue_for(tmp_path, store, hours_between_posts=1, post_times=["12:00", "18:00"])
    monkeypatch.setattr(queue_module.time, "time", lambda: at(10, 13))
    state.data["last_post"]["youtube"] = at(10, 12, 5)  # posted in the 12:00 slot
    assert not q.is_due("youtube", at(10, 13)) and not q.is_due("youtube", at(10, 17, 59))
    assert q.is_due("youtube", at(10, 18)) and q.is_due("youtube", at(11, 9))  # missed slot: catch up once
    state.data["last_post"]["youtube"] = 0
    assert q.is_due("youtube", at(10, 3))  # never posted: the first one goes right away


def test_auto_times_learn_the_best_hours(tmp_path, store):
    store.set("youtube", {"token": "t"})
    q, state = queue_for(tmp_path, store, post_times="auto")
    assert q.slots("youtube") == [(12, 0), (17, 0), (20, 0)]  # not enough posts yet
    for i, (hour, views) in enumerate([(9, 100), (9, 120), (13, 900), (13, 1100), (21, 5000), (21, 4000), (7, 10), (7, 20)]):
        q.items[f"youtube|{i}"] = {"platform": "youtube", "account": "youtube", "video": f"{i}", "status": "done",
                                   "posted": at(3 + i, hour), "stats": {"views": views}, "added": 0}
    assert q.slots("youtube") == [(9, 0), (13, 0), (21, 0)]


# ------------------------------------------------------------------ stats in the queue


def test_stats_are_read_at_growing_intervals(tmp_path, store, monkeypatch):
    store.set("youtube", {"token": "t"})
    store.set("x", {"token": "t"})
    q, state = queue_for(tmp_path, store, x=True)
    reads = []

    class Stats:
        KEY, NAME = "youtube", "YouTube"

        @staticmethod
        def stats(post_id, cfg, store, account):
            reads.append(post_id)
            return {"views": 10 * len(reads), "likes": 1}

    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", Stats)
    monkeypatch.setitem(queue_module.PLATFORMS, "x", Stats)
    now = time.time()
    q.items["youtube|a"] = {"platform": "youtube", "account": "youtube", "video": "a", "status": "done", "post_id": "Y", "posted": now - 3 * 3600}
    q.items["x|a"] = {"platform": "x", "account": "x", "video": "a", "status": "done", "post_id": "X", "posted": now - 3 * 3600}
    assert q.refresh_stats() == 1 and reads == ["Y"]  # X waits a day (every read costs money)
    assert q.refresh_stats() == 0  # next check after 24 hours
    q.items["youtube|a"]["posted"] -= 30 * 3600
    q.items["x|a"]["posted"] -= 30 * 3600
    assert q.refresh_stats() == 2 and q.items["youtube|a"]["stats"]["views"] == 20


def test_best_folders_and_gameplay():
    items = {
        "1": {"platform": "youtube", "status": "done", "folder": "Podcast A", "gameplay": ["parkour.mp4"], "stats": {"views": 1000}},
        "2": {"platform": "tiktok", "status": "done", "folder": "Podcast A", "gameplay": ["minecraft.mp4"], "stats": {"views": 3000, "likes": 50}},
        "3": {"platform": "tiktok", "status": "done", "folder": "Podcast B", "gameplay": ["minecraft.mp4"], "stats": {"views": 500}},
        "4": {"platform": "tiktok", "status": "pending", "folder": "Podcast B"},
    }
    assert queue_module.best_by(items, "folder") == [("Podcast A", 2, 2000.0), ("Podcast B", 1, 500.0)]
    assert queue_module.best_by(items, "gameplay")[0] == ("minecraft.mp4", 2, 1750.0)
    assert queue_module.totals(items)["tiktok"] == {"posts": 2, "views": 3500, "likes": 50}


def test_platform_names():
    assert queue_module.platform_name("youtube") == "YouTube Shorts"
    assert queue_module.platform_name("x:news") == "X (Twitter) (news)"
    assert set(queue_module.PLATFORMS) == {"youtube", "tiktok", "instagram", "facebook", "x", "pinterest"}
    assert json.dumps(DEFAULTS["upload"]["templates"])  # plain data, saved in config.yaml
