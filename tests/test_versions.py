"""A version of every reel per platform: TikTok 1 minute or longer, YouTube Shorts under a minute..."""

from types import SimpleNamespace

import pytest

from brainrot_bot.config import DEFAULTS, ConfigError, load_config
from brainrot_bot.credentials import Credentials
from brainrot_bot.state import State
from brainrot_bot.uploads import queue as queue_module
from brainrot_bot.uploads.post import PostInfo
from brainrot_bot.uploads.queue import UploadQueue
from brainrot_bot.versions import moment_range, parse_range, plan_cuts, ranges

TARGETS = {"tiktok": (61.0, 90.0), "youtube": (3.0, 59.0), "instagram": (3.0, 90.0), "facebook": (3.0, 90.0)}


def no_pauses(a, b):
    return b - a


def search(a, b, lo, hi):
    """Stand-in for the best-moment search: one window starting 2 s in, as long as allowed."""
    end = min(b, a + 2 + hi)
    return [(a + 2, end)] if end - (a + 2) >= lo else []


def cuts_for(length, edited=no_pauses):
    cuts, left_out = plan_cuts(0.0, length, [], TARGETS, edited, search)
    return [(c.start, c.end, round(c.length, 1), sorted(c.platforms)) for c in cuts], left_out


def test_a_short_clip_is_not_made_for_tiktok():
    cuts, left_out = cuts_for(45)
    assert cuts == [(0.0, 45, 45, ["facebook", "instagram", "youtube"])]
    assert left_out == {"tiktok": 45}  # TikTok only pays for 1 minute or longer


def test_a_75_second_clip_gets_a_shorter_youtube_version_of_the_same_moment():
    cuts, left_out = cuts_for(75)
    assert cuts[0] == (0.0, 75, 75, ["facebook", "instagram", "tiktok"])
    start, end, length, platforms = cuts[1]
    assert platforms == ["youtube"] and length <= 59 and 0 <= start and end <= 75
    assert left_out == {}


def test_a_long_clip_gets_a_tiktok_length_part_and_a_youtube_part_inside_it():
    cuts, left_out = cuts_for(170)
    (a1, b1, len1, p1), (a2, b2, len2, p2) = cuts
    assert p1 == ["facebook", "instagram", "tiktok"] and 61 <= len1 <= 90
    assert p2 == ["youtube"] and len2 <= 59 and a1 <= a2 and b2 <= b1  # the same moment, shorter


def test_lengths_are_checked_after_pauses_are_cut():
    # 66 s of clip, but 10% is pauses that get cut: 59.4 s - not enough for TikTok, too long for a Short.
    cuts, left_out = cuts_for(66, edited=lambda a, b: (b - a) * 0.9)
    assert left_out == {"tiktok": pytest.approx(59.4)}
    assert cuts[0][3] == ["facebook", "instagram"] and cuts[1][3] == ["youtube"] and cuts[1][2] <= 59


def test_length_ranges_in_the_settings(tmp_path):
    assert parse_range("x", "61-90") == (61.0, 90.0) and parse_range("x", " 3 – 59 ") == (3.0, 59.0)
    assert parse_range("x", "off") is None and parse_range("x", "") is None
    for bad in ("90", "50-52", "0-30", "61-9000", "sixty-ninety"):
        with pytest.raises(ValueError):
            parse_range("x", bad)
    path = tmp_path / "config.yaml"
    path.write_text("versions:\n  tiktok: 90\n")
    with pytest.raises(ConfigError, match="versions.tiktok"):
        load_config(path)
    path.write_text("versions:\n  youtube: off\n  x: 20-140\n")
    cfg = load_config(path)
    assert set(ranges(cfg)) == {"tiktok", "instagram", "facebook", "x"}
    path.write_text("versions:\n  enabled: false\n")
    assert ranges(load_config(path)) == {}


def test_moments_are_long_enough_for_the_tiktok_version():
    cfg = SimpleNamespace(moments=SimpleNamespace(**DEFAULTS["moments"]), versions=SimpleNamespace(**DEFAULTS["versions"]))
    low, high = moment_range(cfg)
    assert 64 < low < 70 and high == 90  # room for the pauses that get cut
    cfg.versions.enabled = False
    assert moment_range(cfg) == (20, 60)


# ------------------------------------------------------------------ posting each platform's own version


class Recorder:
    def __init__(self, key):
        self.KEY, self.NAME = key, key.title()
        self.calls = []

    def upload(self, video, post, cfg, store, should_stop, account=None):
        self.calls.append((video.name, round(post.duration)))
        return queue_module.Posted("ok", "", "id")


def test_each_platform_posts_its_own_version(tmp_path, monkeypatch):
    store = Credentials(tmp_path / "credentials", encrypt=False)
    for account in ("tiktok", "youtube", "instagram", "x"):
        store.set(account, {"token": account})
    upload = dict(DEFAULTS["upload"], post_times=[], hours_between_posts=0, tiktok=True, youtube=True, instagram=True, x=True)
    cfg = SimpleNamespace(upload=SimpleNamespace(**upload), versions=SimpleNamespace(**DEFAULTS["versions"]),
                          paths=SimpleNamespace(clips=tmp_path / "clips"))
    state = State(tmp_path / "state.json")
    q = UploadQueue(cfg, state, store, state.save)
    fakes = {key: Recorder(key) for key in ("tiktok", "youtube", "instagram", "x")}
    for key, fake in fakes.items():
        monkeypatch.setitem(queue_module.PLATFORMS, key, fake)
    files = {}
    for name in ("TikTok", "YouTube", "Instagram"):
        files[name] = tmp_path / name / "ep_moment1.mp4"
        files[name].parent.mkdir()
        files[name].write_bytes(b"x")
    post = PostInfo("Title", "#tag", 75)
    versions = {"tiktok": (str(files["TikTok"]), 75.0, ["run.mp4"]), "youtube": (str(files["YouTube"]), 48.0, ["jump.mp4"]),
                "instagram": (str(files["Instagram"]), 75.0, ["run.mp4"])}
    assert q.add(files["TikTok"], post, None, {"folder": "show"}, versions=versions) == ["Youtube", "Tiktok", "Instagram", "X"]
    assert q.items[f"youtube|{files['YouTube']}"]["gameplay"] == ["jump.mp4"]
    assert q.run_due() == 4
    assert fakes["tiktok"].calls == [("ep_moment1.mp4", 75)] and fakes["youtube"].calls == [("ep_moment1.mp4", 48)]
    assert fakes["x"].calls == [("ep_moment1.mp4", 75)]  # no version of its own: the longest one
    assert {item["video"] for item in q.items.values()} == {str(files["TikTok"]), str(files["YouTube"]), str(files["Instagram"])}

    # A reel with no TikTok version (under a minute) isn't posted there; one OK approves every version.
    q.approval_needed = lambda: True
    short = {"youtube": (str(files["YouTube"]), 40.0, []), "instagram": (str(files["Instagram"]), 40.0, [])}
    assert q.add(tmp_path / "short.mp4", post, None, None, versions=short) == ["Youtube", "Instagram", "X"]
    assert q.approve(str(tmp_path / "short.mp4"), now=True) == 3
    assert q.skip(str(tmp_path / "short.mp4")) == 3


def test_moments_are_stretched_until_the_tiktok_version_is_a_minute():
    from brainrot_bot.bot import Bot
    from brainrot_bot.editor import edited_length
    from brainrot_bot.moments import Moment
    from brainrot_bot.transcribe import Word

    # A slow talker: 2.5 s of speech, then a 2.5 s pause, over and over. Pauses get cut to a few tenths.
    words, t = [], 0.0
    while t < 300:
        for k in range(5):
            words.append(Word("word." if k == 4 else "word", t + k * 0.5, t + k * 0.5 + 0.4))
        t += 5.0
    cfg = SimpleNamespace(**{k: SimpleNamespace(**v) for k, v in DEFAULTS.items()})
    ctx = SimpleNamespace(words=words, loudness=None, layout=SimpleNamespace(fps=30))
    edited = lambda m: edited_length(cfg, 30, m.start, m.end, words)  # noqa: E731
    first, second = Moment(0.0, 70.0, 5.0), Moment(200.0, 270.0, 4.0)
    assert edited(first) < 61  # 70 s of this, once the pauses are cut, isn't a minute
    bot = SimpleNamespace(cfg=cfg)
    bot._stretch = lambda *args: Bot._stretch(bot, *args)
    Bot._stretch_moments(bot, ctx, [first, second], 300.0)
    assert 61 <= edited(first) <= 90 and first.end < 199  # long enough now, and it stops before the next one
    blocked = [Moment(0.0, 70.0, 5.0), Moment(80.0, 150.0, 4.0)]
    Bot._stretch_moments(bot, ctx, blocked, 300.0)
    assert blocked[0].end < 79  # never runs into the next moment (that one just gets no TikTok version)
    # A 12-second voiceover counts towards the minute: the moment itself needs only 49 s.
    voiced = Moment(0.0, 40.0, 5.0)
    Bot._stretch_moments(bot, ctx, [voiced], 300.0, 12.0)
    assert 49 <= edited(voiced) < 55
