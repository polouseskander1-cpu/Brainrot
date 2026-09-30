"""The commentary voiceover: your own words or recording, or the AI's take, said while the clip pauses."""

import hashlib
import io
import math
import os
import tarfile
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from brainrot_bot import voice as voice_module
from brainrot_bot.captions import build_ass, group_words, prepare_words, time_groups
from brainrot_bot.commentary import LEAD, TAIL, Commentator, Line, align_words, own_commentary, parse_text, spoken
from brainrot_bot.config import DEFAULTS, ConfigError, load_config
from brainrot_bot.editor import add_voiceover, plan_edit
from brainrot_bot.media import probe
from brainrot_bot.render import Insert, RenderJob, build_command, compute_layout
from brainrot_bot.transcribe import Word
from brainrot_bot.versions import moment_range
from brainrot_bot.voice import Voice, VoiceError, download, piper_build, valid_voice, voice_language
from test_bot_integration import (
    BASE_CONFIG, SPEECH, TOOLS, FakeTranscriber, connect_fake_ai, ffmpeg, make_clip, make_workspace, new_bot, steady_talk,
)

needs_ffmpeg = pytest.mark.skipif(TOOLS is None or "ass" not in TOOLS.filters, reason="needs ffmpeg built with libass")
VOICE_ON = BASE_CONFIG.replace("commentary:\n  enabled: false\n", "")


@pytest.fixture
def workspace(tmp_path):
    return make_workspace(tmp_path)


def settings():
    return SimpleNamespace(**{k: SimpleNamespace(**v) for k, v in DEFAULTS.items()})


class FakeVoice:
    """Says each word as 0.3 s of tone: no download, no real voice."""

    def __init__(self):
        self.said = []

    def speak(self, text, language, out):
        self.said.append(text)
        ffmpeg("-f", "lavfi", "-i", f"sine=f=440:d={0.3 * len(text.split()):.2f}", "-ar", "22050", "-ac", "1", str(out))
        return out

    def describe(self, language=""):
        return "a test voice"


class Ears(FakeTranscriber):
    """The clip's words as usual; the voiceover isn't recognized (its captions then follow an estimate)."""

    def __init__(self, words):
        super().__init__(words)
        self.voice_calls = 0

    def transcribe(self, wav, should_stop=None, quiet=False):
        if quiet:
            self.voice_calls += 1
            return []
        return super().transcribe(wav, should_stop)


def voiced_bot(workspace, words=SPEECH):
    bot = new_bot(workspace, words)
    bot.transcriber = Ears(words)
    bot.commentator.voice = FakeVoice()
    return bot


def pause(seconds, fps=30):
    return math.ceil((LEAD + seconds + TAIL) * fps - 1e-6) / fps


def top_frames(reel, times, height=150):
    """The clip area (the top of the reel, above the progress bar) at each time, in gray."""
    import subprocess

    import numpy as np

    out = []
    for t in times:
        raw = subprocess.run([TOOLS.ffmpeg, "-v", "error", "-ss", f"{t}", "-i", str(reel), "-frames:v", "1",
                              "-vf", f"crop=360:{height}:0:0", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout
        out.append(np.frombuffer(raw, dtype=np.uint8).astype(float))
    return out


def change(a, b):
    """How different two pictures are (0 = the same; the encoder alone makes a little difference)."""
    return float(abs(a - b).mean())


# ------------------------------------------------------------------ your own commentary


def test_your_own_words_and_recordings_are_found(tmp_path):
    clip = tmp_path / "My Clip.mp4"
    clip.write_bytes(b"x")
    (tmp_path / "My Clip.outro.MP3").write_bytes(b"x")
    (tmp_path / "my clip.intro.wav").write_bytes(b"x")
    (tmp_path / "Other.outro.mp3").write_bytes(b"x")
    (tmp_path / "My Clip.commentary.txt").write_text("# notes for me\nintro: Wait for it.\nStill before.\n\noutro: He's wrong.\n",
                                                     encoding="utf-8")
    recordings, texts = own_commentary(clip)
    assert {k: v.name for k, v in recordings.items()} == {"intro": "my clip.intro.wav", "outro": "My Clip.outro.MP3"}
    assert texts == {"intro": "Wait for it. Still before.", "outro": "He's wrong."}
    assert parse_text("Just my take.\nSecond line.") == {"outro": "Just my take. Second line."}  # no label: after the clip
    assert parse_text("before - Watch this guy.\nafter: Crazy, right?") == {"intro": "Watch this guy.", "outro": "Crazy, right?"}


def test_text_is_tidied_for_the_voice():
    assert spoken('He said "wow" 🤯 #crazy [laughs] it is (pause) *sighs* TRUE.', 30) == "He said wow it is TRUE."
    assert spoken("One two three. Four five six. Seven eight nine ten eleven.", 7) == "One two three. Four five six."
    assert spoken("a b c d e f g h", 4) == "a b c d."


def test_captions_show_the_written_words_at_the_heard_times():
    heard = [Word("Space", 1.0, 1.3), Word("X", 1.3, 1.5), Word("missed", 1.6, 2.0), Word("every", 2.0, 2.3),
             Word("deadline.", 2.3, 2.9)]
    words = align_words("SpaceX missed every deadline.", heard, 3.2)
    assert [w.text for w in words] == ["SpaceX", "missed", "every", "deadline."]
    assert (words[1].start, words[3].end) == (1.6, 2.9)
    assert 1.0 <= words[0].start < words[0].end <= 1.6
    guessed = align_words("Nothing was heard here.", [], 2.0)
    assert len(guessed) == 4 and 0 <= guessed[0].start and guessed[-1].end <= 2.0
    assert all(a.end <= b.start + 1e-9 for a, b in zip(guessed, guessed[1:]))


# ------------------------------------------------------------------ the edit


def test_the_clip_pauses_for_the_voiceover_and_everything_moves_later():
    cfg = settings()
    layout = compute_layout(cfg.video, 1920, 1080)
    words = [Word(t, 0.5 + i * 0.5, 0.9 + i * 0.5) for i, t in enumerate("This is a million dollar secret!".split())]
    plan = plan_edit(cfg, layout, 0.0, 4.0, words)
    clip_length, first_word, zooms = plan.duration, plan.words[0].start, [(z.start, z.end) for z in plan.zooms]
    intro = Line("intro", "Why?", Path("i.wav"), 1.0, [Word("Why?", 0.0, 0.8)])
    outro = Line("outro", "Think about it. Agree?", Path("o.wav"), 2.0,
                 [Word("Think", 0.0, 0.3), Word("about", 0.35, 0.6), Word("it.", 0.65, 0.9), Word("Agree?", 1.2, 1.9)])
    add_voiceover(cfg, plan, [outro, intro], 30, LEAD, TAIL)

    before, after = pause(1.0), pause(2.0)
    assert plan.duration == pytest.approx(before + clip_length + after)
    assert [(i.before, i.length, i.audio.name) for i in plan.inserts] == [(0, before, "i.wav"), (len(plan.intervals), after, "o.wav")]
    assert plan.words[0].start == pytest.approx(first_word + before)
    assert zooms and [(z.start, z.end) for z in plan.zooms] == [pytest.approx((a + before, b + before)) for a, b in zooms]
    voice = [c for c in plan.chunks if c.voice]
    assert voice[0].start == pytest.approx(LEAD) and voice[0].end <= before + 1e-9
    assert all(c.start >= before + clip_length for c in voice[1:]) and voice[-1].end <= plan.duration + 1e-9
    assert [text for text, *_ in plan.voice_lines] == ["Why?", "Think about it. Agree?"]
    assert [w.text for w in plan.voice_words][:2] == ["Why?", "Think"]
    whooshes = sorted(s.at for s in plan.sounds if s.kind == "whoosh")
    assert pytest.approx(before - 0.3) in whooshes and pytest.approx(before + clip_length - 0.15) in whooshes


def test_voiceover_captions_have_their_own_color():
    cfg = settings()
    chunks = time_groups(group_words(prepare_words([Word("Clip", 0.0, 0.4)], True, True), 3, 18))
    voice = time_groups(group_words(prepare_words([Word("Take", 1.0, 1.4)], True, True), 3, 18))
    for chunk in voice:
        chunk.voice = True
    ass = build_ass(chunks + voice, width=1080, height=1920, duration=2.0, captions=cfg.captions, voice_color="#7FDBFF")
    assert "Style: Voice,Montserrat Black,100,&H00FFDB7F," in ass
    assert ",Caption,,0,0,0,,{\\an5\\pos(540,960)" in ass and ",Voice,,0,0,0,,{\\an5\\pos(540,960)" in ass
    assert "Style: Voice" not in build_ass(chunks, width=1080, height=1920, duration=2.0, captions=cfg.captions)


def test_the_render_holds_the_picture_during_each_pause(tmp_path):
    cfg = settings()
    layout = compute_layout(cfg.video, 1920, 1080)
    job = RenderJob(clip=tmp_path / "c.mp4", clip_start=0.0, duration=5.0, clip_has_audio=False, layout=layout, segments=[],
                    output=tmp_path / "o.mp4", intervals=[(1.0, 3.0), (4.0, 6.0)],
                    inserts=[Insert(0, 1.5, tmp_path / "v1.wav", LEAD), Insert(2, 2.0, tmp_path / "v2.wav", LEAD)])
    assert job.total() == pytest.approx(7.5)
    assert job.order() == [("pause", 0), ("part", 0), ("part", 1), ("pause", 1)]
    assert job.source_time(0.5) == 1.0 and job.source_time(2.0) == pytest.approx(1.5)  # a still, then the clip
    assert job.source_time(4.0) == pytest.approx(4.5) and job.source_time(7.0) == pytest.approx(6.0 - 1 / 30)
    args = build_command(job, cfg)
    graph = args[args.index("-filter_complex") + 1]
    assert "tpad=stop_mode=clone:stop=44" in graph and "tpad=stop_mode=clone:stop=59" in graph  # 45 and 60 frames
    assert "concat=n=4:v=1:a=1" in graph and "anullsrc" in graph  # a silent clip gets silence between the voice
    assert str(tmp_path / "v1.wav") in args and "loudnorm" in graph
    assert args[args.index("-t", args.index("-filter_complex")) + 1] == "7.500"


# ------------------------------------------------------------------ writing it


def test_the_ai_writes_a_take_that_adds_something(fake_claude, tmp_path):
    from brainrot_bot.ai import AI
    from brainrot_bot.credentials import Credentials

    cfg = settings()
    cfg.commentary.intro, cfg.commentary.persona = False, "a skeptical engineer who loves space"
    store = Credentials(tmp_path / "credentials")
    store.set("anthropic", {"api_key": "sk-ant-test"})
    fake_claude.commentary = {"intro": "ignored", "outro": "He is half right. 🤯 Rockets are cheap now, [laughs] Mars is not. Agree?"}
    voice = FakeVoice()
    commentator = Commentator(cfg, TOOLS, AI(cfg, store), voice)
    texts = commentator._ai_texts([Word("Mars", 0.0, 0.4), Word("by", 0.5, 0.6), Word("2040.", 0.7, 1.2)], "en", ' called "Ep"')
    assert texts == {"outro": "He is half right. Rockets are cheap now, Mars is not. Agree?"}
    prompt = fake_claude.requests[-1]["body"]["messages"][0]["content"]
    assert "Mars by 2040." in prompt and "- intro: leave it empty." in prompt and "a skeptical engineer" in prompt
    assert "your voice after it" in prompt and "Don't repeat or summarize" in prompt and "Write, in English:" in prompt
    commentator._ai_texts([Word("Hola.", 0.0, 0.4)], "", "")  # the language wasn't detected
    assert "Write, in the language of the transcript:" in fake_claude.requests[-1]["body"]["messages"][0]["content"]


@needs_ffmpeg
def test_a_long_take_is_trimmed_to_the_limit(tmp_path):
    cfg = settings()
    cfg.commentary.max_seconds = 2.5  # the test voice takes 0.3 s a word
    long_take = "First point here. Second point here. Third point here. Would you do it?"
    ai = SimpleNamespace(available=True, write_commentary=lambda *a: {"intro": "Watch this now.", "outro": long_take})
    commentator = Commentator(cfg, TOOLS, ai, FakeVoice())
    voiceover = commentator.make(tmp_path / "c.mp4", [Word("Hi.", 0.0, 0.5)], "en", "", tmp_path, "1")
    assert voiceover.source == "the AI"
    assert voiceover.line("outro").text == "First point here. Would you do it?"  # the middle went first
    assert voiceover.line("intro") is None  # then the intro, to stay within the limit
    assert sum(x.length for x in voiceover.lines) <= 2.5


def test_voice_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("commentary:\n  voice: Kristin\n  speed: 1.3\n  caption_color: '00ff00'\n")
    c = load_config(path).commentary
    assert (c.voice, c.speed, c.caption_color, c.intro, c.outro) == ("kristin", 1.3, "#00FF00", True, True)
    for bad in ("voice: robot", "voice: de-thorsten", "speed: 5", "caption_color: blue", "max_seconds: 1"):
        path.write_text(f"commentary:\n  {bad}\n")
        with pytest.raises(ConfigError, match="commentary"):
            load_config(path)
    path.write_text("commentary:\n  voice: de_DE-thorsten-medium\n")
    assert load_config(path).commentary.voice == "de_DE-thorsten-medium"


def test_moments_leave_room_for_the_voiceover():
    cfg = settings()
    assert moment_range(cfg, 14.0) == (pytest.approx((61 - 14) * 1.08), 76)
    assert moment_range(cfg) == (pytest.approx(61 * 1.08), 90)


# ------------------------------------------------------------------ the voice


def test_which_voice_runs_where():
    assert piper_build("windows", "AMD64") == "windows-amd64" and piper_build("windows", "ARM64") == "windows-amd64"
    assert piper_build("linux", "x86_64") == "linux-x86_64" and piper_build("linux", "aarch64") == "linux-aarch64"
    assert piper_build("darwin", "arm64") == "" and piper_build("windows", "x86") == ""  # the Mac build is broken
    assert all(valid_voice(v) for v in ("norman", "kristin", "system", "en_GB-alan-medium", "en_US-libritts_r-medium"))
    assert not any(valid_voice(v) for v in ("robot", "en-alan-medium", "en_GB-alan-best", "../evil"))
    assert (voice_language("john"), voice_language("de_DE-thorsten-medium"), voice_language("system")) == ("en", "de", "")


def test_downloads_are_checked(tmp_path):
    src = tmp_path / "file.bin"
    src.write_bytes(b"voice" * 1000)
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    dest = download(src.as_uri(), tmp_path / "got" / "file.bin", good)
    assert dest.read_bytes() == src.read_bytes()
    with pytest.raises(VoiceError, match="damaged"):
        download(src.as_uri(), tmp_path / "bad" / "file.bin", "0" * 64)
    assert not list((tmp_path / "bad").iterdir())  # no half file left behind


def test_an_archive_cant_write_outside_its_folder(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        data = b"x"
        info = tarfile.TarInfo("../outside.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    (tmp_path / "into").mkdir()
    with pytest.raises(VoiceError):
        voice_module._extract(archive, tmp_path / "into")
    assert not (tmp_path / "outside.txt").exists()


def test_the_computers_voice_steps_in(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(voice_module, "piper_build", lambda *a: "linux-x86_64")
    voice = Voice("norman", 1.1, TOOLS, tmp_path)
    spoken_by = []

    def system(text, language, out):
        spoken_by.append("system")
        out.write_bytes(b"x" * 2000)
        return out

    monkeypatch.setattr(voice, "_system", system)
    monkeypatch.setattr(voice, "_install_piper", lambda: (_ for _ in ()).throw(OSError("no internet")))
    assert voice.engine_for("en") == "piper" and voice.engine_for("") == "piper"  # unknown language: the chosen voice
    voice.speak("Hello there.", "en", tmp_path / "a.wav")
    assert spoken_by == ["system"] and "Couldn't download the voice" in caplog.text
    assert voice.engine_for("en") == "system"  # the download is tried again an hour later, not for every line
    assert voice.engine_for("es") == "system"  # the built-in voices are English

    broken = Voice("norman", 1.0, TOOLS, tmp_path)
    monkeypatch.setattr(broken, "_system", system)
    monkeypatch.setattr(broken, "_install_piper", lambda: tmp_path / "piper")
    monkeypatch.setattr(broken, "_install_voice", lambda: tmp_path / "v.onnx")
    monkeypatch.setattr(broken, "_piper", lambda *a: (_ for _ in ()).throw(VoiceError("Piper: missing DLL")))
    broken.speak("Hi.", "en", tmp_path / "b.wav")
    assert broken.engine_for("en") == "system" and "couldn't start" in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows' own voices")
def test_windows_own_voice_steps_in_for_real(tmp_path):
    """The fallback when the natural voice can't run: Windows' built-in voice (System.Speech)."""
    voice = Voice("system", 1.1, TOOLS, tmp_path)
    out = voice.speak("Would you move to Mars? Tell me in the comments.", "en", tmp_path / "windows.wav")
    with wave.open(str(out)) as handle:
        assert handle.getnframes() / handle.getframerate() > 1.0


# ------------------------------------------------------------------ whole reels


@needs_ffmpeg
def test_ai_voiceover_pauses_the_clip_around_its_take(workspace, fake_claude):
    (workspace / "config.yaml").write_text(VOICE_ON, encoding="utf-8")
    connect_fake_ai(workspace)
    bot = voiced_bot(workspace)
    from brainrot_bot.gameplay import scan_library
    from brainrot_bot.media import VIDEO_EXTS

    clip = workspace / "clips" / "show" / "ep1.mp4"
    rendered = bot.make_reels(clip, scan_library(workspace / "gameplay", VIDEO_EXTS, TOOLS, {}))
    reel = rendered[0].video
    before, after = pause(0.3 * 5), pause(0.3 * 11)  # "Here is why this matters." / "Honestly, I think he ... same?"
    assert abs(probe(TOOLS, reel).duration - (probe(TOOLS, clip).duration + before + after)) < 0.12
    srt = reel.with_suffix(".srt").read_text(encoding="utf-8")
    assert srt.index("Here is why this matters.") < srt.index("Hello there, friend.") < srt.index("Would you do the same?")
    assert rendered[0].note == ("Before the clip: Here is why this matters.\n"
                                "After the clip: Honestly, I think he is right. Would you do the same?")
    prompt = next(r for r in fake_claude.requests if r.get("body", {}).get("system", "").startswith("You are the voice"))
    assert "Hello there, friend. Listen up" in prompt["body"]["messages"][0]["content"]
    # The clip area holds still during the intro, then plays (the test picture changes every frame).
    still_a, still_b, play_a, play_b = top_frames(reel, [0.3, 1.5, before + 1.0, before + 2.0])
    assert change(still_a, still_b) < 1.0 and change(play_a, play_b) > 5.0


@needs_ffmpeg
def test_your_own_recording_and_words_with_a_silent_clip(workspace):
    clip = workspace / "clips" / "show" / "ep1.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25", "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-an", str(clip))
    ffmpeg("-f", "lavfi", "-i", "sine=f=500:d=1.2", "-c:a", "aac", str(workspace / "clips" / "show" / "ep1.outro.m4a"))
    (workspace / "clips" / "show" / "ep1.commentary.txt").write_text("intro: Wait for it.\n", encoding="utf-8")
    (workspace / "config.yaml").write_text(VOICE_ON, encoding="utf-8")
    bot = voiced_bot(workspace, [])
    assert bot.run_once() == 1  # the recording next to the clip isn't mistaken for a clip
    reel = workspace / "output" / "show" / "ep1.mp4"
    expected = 4.0 + pause(0.3 * 3) + pause(1.2)
    info = probe(TOOLS, reel)
    assert abs(info.duration - expected) < 0.12 and info.has_audio
    assert bot.commentator.voice.said == ["Wait for it."]  # the voice read your words; your recording was played as is
    assert sorted(p.name for p in (workspace / "output" / "show").glob("*.mp4")) == ["ep1.mp4"]


@needs_ffmpeg
def test_the_voiceover_counts_towards_tiktoks_minute(workspace, fake_claude):
    """A 56-second clip is too short for TikTok's Creator Rewards, but with 6 seconds of voiceover it's over a minute."""
    (workspace / "clips" / "show" / "ep1.mp4").unlink()
    make_clip(workspace / "clips" / "show" / "talk.mp4", 56)
    (workspace / "config.yaml").write_text(VOICE_ON.replace("versions:\n  enabled: false\n", ""), encoding="utf-8")
    connect_fake_ai(workspace)
    bot = voiced_bot(workspace, steady_talk(56))
    assert bot.run_once() == 1
    out = workspace / "output"
    tiktok, youtube = probe(TOOLS, out / "TikTok" / "show" / "talk.mp4").duration, probe(TOOLS, out / "YouTube" / "show" / "talk.mp4").duration
    assert 61 <= tiktok <= 90 and youtube <= 59
    srt = (out / "YouTube" / "show" / "talk.srt").read_text(encoding="utf-8")
    assert "Here is why this matters." in srt and "Would you do the same?" in srt  # every version has the voiceover
    assert bot.commentator.voice.said.count("Here is why this matters.") == 1  # written and spoken once per reel


@needs_ffmpeg
def test_translated_reels_translate_the_voiceover_captions_too(workspace, fake_claude):
    (workspace / "config.yaml").write_text(VOICE_ON.replace("captions:\n", "captions:\n  translate_to: [es]\n"), encoding="utf-8")
    connect_fake_ai(workspace)
    bot = voiced_bot(workspace)
    assert bot.run_once() == 1
    out = workspace / "output" / "show"
    srt = (out / "ep1_es.srt").read_text(encoding="utf-8")
    assert srt.index("ES: Here is why this matters.") < srt.index("ES: Hello there, friend.") < srt.index("ES: Honestly")
    assert abs(probe(TOOLS, out / "ep1_es.mp4").duration - probe(TOOLS, out / "ep1.mp4").duration) < 0.05
    assert bot.commentator.voice.said == ["Here is why this matters.", "Honestly, I think he is right. Would you do the same?"]
