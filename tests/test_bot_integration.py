"""Renders real (tiny) reels with ffmpeg. The speech model is replaced by a stub so no download is needed."""

import subprocess

import pytest

from brainrot_bot.bot import Bot
from brainrot_bot.config import WORK_DIR, load_config
from brainrot_bot.media import MediaError, find_tools, probe
from brainrot_bot.transcribe import Word


def _tools():
    try:
        return find_tools()
    except MediaError:
        return None


TOOLS = _tools()
pytestmark = pytest.mark.skipif(TOOLS is None or "ass" not in TOOLS.filters, reason="needs ffmpeg built with libass")

# Pause cutting is switched off here so the reel is exactly as long as the clip; it has its own test below.
BASE_CONFIG = (
    "watch:\n  settle_seconds: 0\n  max_attempts: 1\nvideo:\n  width: 360\n  height: 640\n  preset: ultrafast\n"
    "captions:\n  font_size: 40\nedit:\n  cut_silences: false\n"
)


def ffmpeg(*args):
    subprocess.run([TOOLS.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_clip(path, seconds, size="320x180", silent=None):
    """A test picture with a tone; silent=(a, b) makes the tone stop between a and b seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tone = "sine=f=300" + (f",volume=volume=0:enable='between(t,{silent[0]},{silent[1]})'" if silent else "")
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s={size}:r=25", "-f", "lavfi", "-i", tone,
           "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path))


class FakeTranscriber:
    def __init__(self, words, language="en"):
        self.words = words
        self.calls = 0
        self.last_language = language

    def load(self):
        pass

    def transcribe(self, wav, should_stop=None):
        assert wav.exists()
        self.calls += 1
        return list(self.words)


SPEECH = [Word("Hello", 0.2, 0.6), Word("there,", 0.7, 1.1), Word("friend.", 1.3, 1.8), Word("Listen", 3.0, 3.3), Word("up", 3.4, 3.8)]


@pytest.fixture
def workspace(tmp_path):
    make_clip(tmp_path / "clips" / "show" / "ep1.mp4", 4.3)
    (tmp_path / "clips" / "show" / "title.txt").write_text("Test title", encoding="utf-8")
    (tmp_path / "gameplay").mkdir()
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x180:r=30", "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / "gameplay" / "run.mp4"))
    (tmp_path / "config.yaml").write_text(BASE_CONFIG, encoding="utf-8")
    return tmp_path


def new_bot(workspace, words=SPEECH):
    bot = Bot(load_config(workspace / "config.yaml"), TOOLS)
    bot.transcriber = FakeTranscriber(words)
    return bot


def test_new_clip_becomes_a_reel_exactly_once(workspace):
    jobs_before = set(WORK_DIR.glob("*")) if WORK_DIR.exists() else set()
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    assert bot.transcriber.calls == 1

    reel = workspace / "output" / "show" / "ep1.mp4"
    assert reel.exists()
    assert "Hello there, friend." in reel.with_suffix(".srt").read_text(encoding="utf-8")
    clip_info, reel_info = probe(TOOLS, workspace / "clips" / "show" / "ep1.mp4"), probe(TOOLS, reel)
    assert (reel_info.width, reel_info.height) == (360, 640)
    assert abs(reel_info.duration - clip_info.duration) < 0.05  # clip and gameplay end together
    assert reel_info.has_audio
    assert reel.with_suffix(".jpg").exists()  # cover picture

    again = new_bot(workspace)
    assert again.run_once() == 0  # remembered across restarts
    assert again.transcriber.calls == 0
    assert again.state.usage == {"run.mp4": 1}
    assert (set(WORK_DIR.glob("*")) if WORK_DIR.exists() else set()) <= jobs_before  # temp files cleaned up


def test_pauses_are_cut_and_captions_follow(workspace):
    # Silence between "friend." (ends 1.8s) and "Listen" (starts 3.0s): that pause is cut.
    make_clip(workspace / "clips" / "show" / "ep1.mp4", 4.3, silent=(1.85, 2.95))
    (workspace / "config.yaml").write_text(BASE_CONFIG.replace("cut_silences: false", "cut_silences: true"), encoding="utf-8")
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    reel = workspace / "output" / "show" / "ep1.mp4"
    # Kept: 0.08-1.92 (rounded to frames: 2/30-58/30) and 2.88-4.04 (86/30-121/30) = 3.033s
    assert abs(probe(TOOLS, reel).duration - 3.033) < 0.06
    srt = reel.with_suffix(".srt").read_text(encoding="utf-8")
    assert "00:00:02,00" in srt  # "Listen" moved from 3.0s to 2.0s
    assert "00:00:03,0" not in srt.split("Listen")[0]


def test_loud_pauses_are_kept(workspace):
    # The tone keeps playing between the words (like laughter), so nothing in the middle is cut.
    (workspace / "config.yaml").write_text(BASE_CONFIG.replace("cut_silences: false", "cut_silences: true"), encoding="utf-8")
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    # Only the quiet-before-the-first-word and after-the-last-word trims: 2/30 .. 121/30
    assert abs(probe(TOOLS, workspace / "output" / "show" / "ep1.mp4").duration - 3.967) < 0.06


def decode_audio(path):
    """The reel's sound as mono 48 kHz floats."""
    import numpy as np

    raw = subprocess.run([TOOLS.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000",
                          "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4")


def band_level(audio, start, end, freq):
    """How strong one frequency is between start and end seconds."""
    import numpy as np

    part = audio[int(start * 48000):int(end * 48000)]
    spectrum = np.abs(np.fft.rfft(part * np.hanning(len(part)))) / len(part)
    freqs = np.fft.rfftfreq(len(part), 1 / 48000)
    return float(spectrum[(freqs > freq - 15) & (freqs < freq + 15)].max())


def test_swear_word_is_bleeped_and_music_ducks_under_the_voice(workspace):
    # Voice: a 300 Hz tone that stops from 3.0s to 4.5s. Music: a 150 Hz tone.
    make_clip(workspace / "clips" / "show" / "ep1.mp4", 6, silent=(3.0, 4.5))
    (workspace / "music").mkdir()
    ffmpeg("-f", "lavfi", "-i", "sine=f=150", "-t", "20", "-c:a", "aac", str(workspace / "music" / "tone.m4a"))
    (workspace / "config.yaml").write_text(
        BASE_CONFIG + "  censor: true\n  zoom: false\n  emojis: false\naudio:\n  music_volume: 0.3\n", encoding="utf-8"
    )
    words = [Word("This", 0.3, 0.7), Word("fucking", 1.0, 1.4), Word("works.", 1.6, 2.2), Word("Again", 4.6, 5.0)]
    bot = new_bot(workspace, words)
    assert bot.run_once() == 1
    reel = workspace / "output" / "show" / "ep1.mp4"
    audio = decode_audio(reel)
    assert abs(len(audio) / 48000 - 6) < 0.1

    # While "fucking" is said: the voice is gone and a 1 kHz bleep plays instead.
    assert band_level(audio, 1.05, 1.35, 1000) > 20 * band_level(audio, 1.05, 1.35, 300)
    assert band_level(audio, 0.3, 0.9, 300) > 20 * band_level(audio, 0.3, 0.9, 1000)
    # The music is much quieter under the voice than in the pause.
    assert band_level(audio, 3.6, 4.4, 150) > 2 * band_level(audio, 1.6, 2.4, 150)
    assert "This f****** works." in reel.with_suffix(".srt").read_text(encoding="utf-8")


def test_long_clip_is_split_into_parts(workspace):
    make_clip(workspace / "clips" / "long" / "talk.mp4", 23)
    (workspace / "clips" / "show" / "ep1.mp4").unlink()
    (workspace / "config.yaml").write_text(BASE_CONFIG + "parts:\n  max_seconds: 10\n", encoding="utf-8")
    words = [Word("word." if i % 8 == 7 else "word", i * 0.5, i * 0.5 + 0.3) for i in range(44)]
    bot = new_bot(workspace, words)
    assert bot.run_once() == 1
    reels = sorted((workspace / "output" / "long").glob("*.mp4"))
    assert [r.name for r in reels] == ["talk_part1.mp4", "talk_part2.mp4", "talk_part3.mp4"]
    durations = [probe(TOOLS, r).duration for r in reels]
    assert all(d <= 10.05 for d in durations)
    assert abs(sum(durations) - 23) < 0.2


def test_waits_for_gameplay_without_failing(workspace):
    (workspace / "gameplay" / "run.mp4").unlink()
    bot = new_bot(workspace)
    assert bot.run_once() == 0
    assert bot.state.clips == {}  # not counted as a failure


def test_broken_clip_is_marked_failed(workspace):
    (workspace / "clips" / "show" / "broken.mp4").write_bytes(b"this is not a video" * 100)
    bot = new_bot(workspace)
    assert bot.run_once() == 1  # the good clip
    entry = bot.state.clips["show/broken.mp4"]
    assert entry["status"] == "failed" and entry["attempts"] == 1
    assert not (workspace / "output" / "show" / "broken.mp4").exists()


# ------------------------------------------------------------------ long videos, AI, translations, duplicates, links

LONG_TALK_CONFIG = "moments:\n  min_source_minutes: 1\n  count: 2\n  min_seconds: 8\n  max_seconds: 15\n"


def long_talk():
    """About 70 seconds of talk: filler, a strong bit, filler, another strong bit, filler."""
    words, t = [], 0.5
    filler = ["so", "yeah", "we", "were", "talking", "about", "stuff."]
    script = (filler * 3
              + "Why do most people never get rich? They spend every dollar they earn. The secret is to invest ten percent forever!".split()
              + filler * 3
              + "What is the biggest mistake? Never quit your job before you make a million dollars!".split()
              + filler * 2)
    for token in script:
        words.append(Word(token, t, t + 0.3))
        t += 0.4 + (0.5 if token.endswith((".", "?", "!")) else 0)
    return words


def test_long_video_becomes_reels_of_its_best_moments(workspace):
    (workspace / "clips" / "show" / "ep1.mp4").unlink()
    make_clip(workspace / "clips" / "show" / "episode.mp4", 70)
    (workspace / "config.yaml").write_text(BASE_CONFIG + LONG_TALK_CONFIG, encoding="utf-8")
    bot = new_bot(workspace, long_talk())
    assert bot.run_once() == 1
    reels = sorted((workspace / "output" / "show").glob("*.mp4"))
    assert [r.name for r in reels] == ["episode_moment1.mp4", "episode_moment2.mp4"]
    durations = [probe(TOOLS, r).duration for r in reels]
    assert all(7.5 <= d <= 16 for d in durations)
    srt = (workspace / "output" / "show" / "episode_moment1.srt").read_text(encoding="utf-8")
    assert "Why do most people never get rich?" in srt  # the best moment is the strong bit


def connect_fake_ai(workspace):
    from brainrot_bot.credentials import Credentials

    Credentials(workspace / "credentials").set("anthropic", {"api_key": "sk-ant-test"})


def test_ai_picks_the_moments_and_writes_hooks_and_captions(workspace, fake_claude):
    (workspace / "clips" / "show" / "ep1.mp4").unlink()
    (workspace / "clips" / "show" / "title.txt").unlink()
    make_clip(workspace / "clips" / "show" / "episode.mp4", 70)
    (workspace / "config.yaml").write_text(BASE_CONFIG + LONG_TALK_CONFIG, encoding="utf-8")
    connect_fake_ai(workspace)
    words = long_talk()
    fake_claude.moments = [{"start_sentence": 5, "end_sentence": 7, "hook": "Nobody tells you this", "score": 9, "why": "money"}]
    bot = new_bot(workspace, words)
    from brainrot_bot.gameplay import scan_library
    from brainrot_bot.media import VIDEO_EXTS

    gameplay = scan_library(workspace / "gameplay", VIDEO_EXTS, TOOLS, {})
    rendered = bot.make_reels(workspace / "clips" / "show" / "episode.mp4", gameplay)
    assert [r.suffix for r in rendered] == ["_moment1"]
    post = rendered[0].post
    assert post.title == "The day he lost it all" and post.description == "Would you have made the same choice?"
    assert post.hashtags.split() == ["#Show", "#money", "#story", "#fyp", "#viral", "#podcast"]  # specific tags first
    systems = [r["body"]["system"] for r in fake_claude.requests]
    assert systems[0].startswith("You are an experienced short-form video editor")
    assert "[5] " in fake_claude.requests[0]["body"]["messages"][0]["content"]
    assert len(fake_claude.requests) == 2  # moments + copy


def test_translated_reel(workspace, fake_claude):
    (workspace / "config.yaml").write_text(BASE_CONFIG.replace("captions:\n", "captions:\n  translate_to: [es, en]\n"), encoding="utf-8")
    connect_fake_ai(workspace)
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    out = workspace / "output" / "show"
    assert sorted(p.name for p in out.glob("*.mp4")) == ["ep1.mp4", "ep1_es.mp4"]  # English is the spoken language
    assert "ES: Hello there, friend." in (out / "ep1_es.srt").read_text(encoding="utf-8")
    assert abs(probe(TOOLS, out / "ep1_es.mp4").duration - probe(TOOLS, out / "ep1.mp4").duration) < 0.05
    assert (out / "ep1_es.jpg").exists()


def test_duplicates_are_skipped(workspace):
    import shutil

    # 15 seconds of sound that gets louder and quieter (like speech), so it has a recognizable "shape".
    clip = workspace / "clips" / "show" / "ep1.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25", "-f", "lavfi",
           "-i", "sine=f=300,volume=volume='0.1+0.4*abs(sin(2*PI*t*0.37))*abs(sin(2*PI*t*1.1))':eval=frame",
           "-t", "15", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(clip))
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    # An exact copy in another folder, and a re-encoded copy (different file, same sound).
    (workspace / "clips" / "other").mkdir()
    shutil.copyfile(clip, workspace / "clips" / "other" / "copy.mp4")
    ffmpeg("-i", str(clip), "-vf", "scale=256:144", "-c:v", "libx264", "-crf", "30",
           "-preset", "ultrafast", "-c:a", "aac", "-b:a", "96k", str(workspace / "clips" / "other" / "reencoded.mp4"))
    make_clip(workspace / "clips" / "other" / "new.mp4", 15, silent=(1.0, 2.0))  # different sound: not a duplicate
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    clips = bot.state.clips
    assert clips["other/copy.mp4"]["status"] == "duplicate" and clips["other/copy.mp4"]["same_as"] == "show/ep1.mp4"
    assert clips["other/reencoded.mp4"]["status"] == "duplicate"
    assert clips["other/new.mp4"]["status"] == "done"
    assert sorted(p.name for p in (workspace / "output" / "other").glob("*.mp4")) == ["new.mp4"]
    assert new_bot(workspace).run_once() == 0  # duplicates are remembered, not checked again


def test_links_are_downloaded_and_made_into_reels(workspace, monkeypatch):
    import shutil

    from brainrot_bot.links import add_link

    source = workspace / "source.mp4"
    shutil.move(workspace / "clips" / "show" / "ep1.mp4", source)

    def fake_download(url, folder, tools, cfg, should_stop):
        target = folder / "Great Episode [abc123].mp4"
        shutil.copyfile(source, target)
        return [target]

    monkeypatch.setattr("brainrot_bot.links.download", fake_download)
    add_link(workspace / "clips" / "show", "https://www.youtube.com/watch?v=abc123")
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    assert (workspace / "output" / "show" / "Great Episode [abc123].mp4").exists()
    assert new_bot(workspace).run_once() == 0  # the link is not downloaded again


def test_finished_reels_are_posted_with_their_details(workspace, monkeypatch):
    from brainrot_bot.credentials import Credentials
    from brainrot_bot.uploads import Posted
    from brainrot_bot.uploads import queue as queue_module

    posted = []

    class FakeYouTube:
        KEY, NAME = "youtube", "YouTube Shorts"

        @staticmethod
        def upload(video, post, cfg, store, should_stop, account="youtube"):
            posted.append((video.name, account, post.render("youtube", cfg.upload.templates)))
            return Posted("https://youtube.com/shorts/abc", "https://youtube.com/shorts/abc", "abc")

    monkeypatch.setitem(queue_module.PLATFORMS, "youtube", FakeYouTube)
    Credentials(workspace / "credentials").set("youtube", {"refresh_token": "r"})
    Credentials(workspace / "credentials").set("youtube:other", {"refresh_token": "r"})
    (workspace / "config.yaml").write_text(BASE_CONFIG + "upload:\n  youtube: true\n  post_times: []\n  hours_between_posts: 0\n"
                                           "  hashtags: '#fyp'\n  templates:\n    youtube: '{caption} | {hashtags}'\n", encoding="utf-8")
    (workspace / "clips" / "show" / "hashtags.txt").write_text("#showtag\n", encoding="utf-8")
    bot = new_bot(workspace)
    assert bot.run_once() == 1
    assert posted == [("ep1.mp4", "youtube", "Test title | #showtag #Show #fyp")]
    (item,) = bot.state.data["uploads"].values()
    assert item["folder"] == "show" and item["clip"] == "show/ep1.mp4" and item["gameplay"] == ["run.mp4"]
    assert item["post_id"] == "abc" and item["status"] == "done"

    # This folder's reels go to the other channel from now on.
    (workspace / "clips" / "show" / "accounts.txt").write_text("youtube = other\n", encoding="utf-8")
    make_clip(workspace / "clips" / "show" / "ep2.mp4", 3, silent=(1, 2))
    assert new_bot(workspace).run_once() == 1
    assert posted[-1][:2] == ("ep2.mp4", "youtube:other")
