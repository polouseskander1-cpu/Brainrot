"""Finding moments, hooks/captions/hashtags, translations, duplicates, links and the AI client."""

import json
import threading
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from brainrot_bot import ai as ai_module
from brainrot_bot.ai import AI, check_key
from brainrot_bot.analysis import Loudness
from brainrot_bot.config import DEFAULTS
from brainrot_bot.copywriter import best_line, built_in_copy, clean_sentence, hashtag, merge_hashtags, write_copy
from brainrot_bot.credentials import Credentials
from brainrot_bot.dedupe import Fingerprints, similarity, sound_bits, sound_match, text_signature
from brainrot_bot.links import LinkQueue, add_link, explain, read_links
from brainrot_bot.moments import auto_count, find_moments, moments_from_picks, split_sentences, transcript_for_ai
from brainrot_bot.state import State
from brainrot_bot.transcribe import Word
from brainrot_bot.translate import caption_settings, translate_reel, translated_words


def cfg(**sections):
    c = SimpleNamespace(**{k: SimpleNamespace(**v) for k, v in DEFAULTS.items()})
    for section, values in sections.items():
        for key, value in values.items():
            setattr(getattr(c, section), key, value)
    return c


def talk(text: str, start: float = 0.0, pace: float = 0.4) -> list[Word]:
    words, t = [], start
    for token in text.split():
        words.append(Word(token, t, t + pace * 0.8))
        t += pace + (0.6 if token.endswith((".", "?", "!")) else 0)
    return words


FILLER = "and um you know we were just talking about the weather and stuff like that honestly. "
GOOD = ("Why do most people never get rich? Because they spend every dollar the moment they earn it. "
        "The secret is simple: pay yourself first, invest ten percent, and never touch it. "
        "I did that for ten years and now I have a million dollars. ")


# ------------------------------------------------------------------ moments


def test_sentences_split_on_punctuation_and_pauses():
    words = talk("Hello there. How are you? Fine") + [Word("later", 10, 10.3)]
    assert [s.text for s in split_sentences(words)] == ["Hello there.", "How are you?", "Fine", "later"]


def test_long_rambling_sentence_is_split():
    words = [Word("word," if i % 10 == 9 else "word", i * 0.5, i * 0.5 + 0.4) for i in range(80)]  # 40 s, no full stop
    sentences = split_sentences(words)
    assert len(sentences) >= 2 and all(s.end - s.start <= 20 for s in sentences)


def test_find_moments_prefers_the_strong_part():
    words = talk(FILLER * 12)
    t = words[-1].end + 1
    words += talk(GOOD, t)
    t2 = words[-1].end + 1
    words += talk(FILLER * 12, t2)
    moments = find_moments(words, words[-1].end + 1, count=1, min_seconds=15, max_seconds=40)
    (best,) = moments
    assert best.start >= t - 0.5 and best.end <= t2 + 1
    assert 15 <= best.duration <= 41
    assert best.sentences[0].text.startswith("Why do most people")


def test_find_moments_never_overlap_and_respect_limits():
    words = talk((GOOD + FILLER) * 20)
    moments = find_moments(words, words[-1].end, count=5, min_seconds=10, max_seconds=30)
    assert 1 <= len(moments) <= 5
    for m in moments:
        assert 9.5 <= m.duration <= 31
    ordered = sorted(moments, key=lambda m: m.start)
    for a, b in zip(ordered, ordered[1:]):
        assert a.end <= b.start


def test_auto_count():
    assert auto_count(3600, 0, 12) == 8 and auto_count(3 * 3600, 0, 12) == 12 and auto_count(200, 0, 12) == 1
    assert auto_count(3600, 3, 12) == 3


def test_ai_picks_are_checked_and_repaired():
    words = talk((GOOD + FILLER) * 6)
    sentences = split_sentences(words)
    last = len(sentences) - 1
    picks = [
        {"start_sentence": 0, "end_sentence": last, "hook": "Too long", "score": 9, "why": ""},  # trimmed
        {"start_sentence": 2, "end_sentence": 2, "hook": "Overlaps", "score": 8, "why": ""},  # overlaps the first
        {"start_sentence": 999, "end_sentence": 1000, "hook": "Bad", "score": 7, "why": ""},  # ignored
        {"start_sentence": last - 1, "end_sentence": last, "hook": "  Short   one ", "score": 6, "why": ""},  # extended
    ]
    moments = moments_from_picks(picks, sentences, words[-1].end, count=5, min_seconds=10, max_seconds=25)
    assert [m.hook for m in moments] == ["Too long", "Short one"]
    assert all(9.5 <= m.duration <= 31 for m in moments)
    assert moments[0].end <= moments[1].start


def test_transcript_for_ai():
    text = transcript_for_ai(split_sentences([Word("Hi.", 3725.2, 3725.5), Word("Yes", 3727, 3727.3)]))
    assert text.splitlines() == ["[0] 01:02:05 Hi.", "[1] 01:02:07 Yes"]


# ------------------------------------------------------------------ hooks, captions, hashtags


def test_clean_sentence_and_hashtag():
    assert clean_sentence("so, the biggest mistake is this.") == "The biggest mistake is this"
    assert hashtag("Diary of a CEO!") == "DiaryOfACEO"
    assert merge_hashtags("#fyp #viral", ["Money", "fyp", "#Viral"], "#podcast") == "#fyp #viral #Money #podcast"


def test_built_in_copy():
    words = talk(FILLER + GOOD)
    assert best_line(words) == "Why do most people never get rich?"
    copy = built_in_copy(words, "", "Money Talks")
    assert copy.hook == "Why do most people never get rich?" and copy.title == copy.hook
    assert copy.hashtags[:2] == ["MoneyTalks", "money"]
    assert not copy.by_ai
    assert best_line(talk(FILLER)) == ""  # nothing catchy: no hook rather than a bad one


def test_write_copy_without_ai_uses_the_rules():
    copy = write_copy(None, talk(GOOD), "My title", "", 30)
    assert copy.title == "My title" and copy.hook and not copy.by_ai


# ------------------------------------------------------------------ translations


def test_translated_words_keep_the_sentence_timing():
    words = translated_words([(1.0, 3.0), (4.0, 5.0)], ["uno dos tres", "cuatro"], "es")
    assert [w.text for w in words] == ["uno", "dos", "tres", "cuatro"]
    assert words[0].start == 1.0 and words[2].end <= 3.0 and words[3].start == 4.0
    chinese = translated_words([(0.0, 2.0)], ["这是一个非常长的中文句子没有空格"], "zh")
    assert [len(w.text) for w in chinese] == [8, 8]


def test_caption_settings_per_language():
    style = SimpleNamespace(**DEFAULTS["captions"])
    style.animation = "karaoke"
    arabic = caption_settings(style, "ar")
    assert arabic.font == "Cairo Black" and arabic.keyword_color == "" and arabic.animation == "pop"
    assert caption_settings(style, "es").font == style.font
    assert caption_settings(style, "ja").max_words == 1


# ------------------------------------------------------------------ duplicates


def test_text_signature_similarity():
    a = talk((GOOD + FILLER) * 2)
    b = [Word(w.text.upper(), w.start + 100, w.end + 100) for w in a]  # same words, other timing and case
    c = talk(FILLER * 3 + "completely different words about cooking pasta at home tonight with friends.")
    assert similarity(text_signature(a), text_signature(b)) == 1.0
    assert similarity(text_signature(a), text_signature(c)) < 0.3
    assert text_signature(talk("too short")) == []


def test_sound_bits():
    rng = np.random.default_rng(1)
    levels = list(np.abs(rng.normal(0.1, 0.05, 600)))  # 60 s
    bits = sound_bits(levels)
    assert len(bits) == 119
    noisy = sound_bits([x * 1.5 + 0.0005 for x in levels])  # louder copy: same ups and downs
    assert sound_match(bits, noisy) > 0.95
    other = sound_bits(list(np.abs(rng.normal(0.1, 0.05, 600))))
    assert sound_match(bits, other) < 0.7
    assert sound_bits([0.0] * 600) == "" and sound_bits(levels[:50]) == ""


def test_fingerprints(tmp_path):
    state = State(tmp_path / "state.json")
    fp = Fingerprints(state, 0.7)
    sig = text_signature(talk((GOOD + FILLER) * 2))
    fp.remember("show/a.mp4", quick="abc", sound="10" * 30, signature=sig)
    fp.remember("show/a.mp4#_moment1", signature=sig)
    assert fp.same_file("show/b.mp4", "abc") == "show/a.mp4"
    assert fp.same_file("show/a.mp4", "abc") is None  # itself (the file was changed and is made again)
    assert fp.same_sound("other/c.mp4", "10" * 30) == "show/a.mp4"
    assert fp.same_words("other/c.mp4", sig) in ("show/a.mp4", "show/a.mp4#_moment1")
    assert fp.same_words("show/a.mp4", sig) is None
    state.save()
    again = Fingerprints(State(tmp_path / "state.json"))
    assert again.same_file("x.mp4", "abc") == "show/a.mp4"  # remembered across restarts
    again.forget("show/a.mp4")
    assert again.entries == []


# ------------------------------------------------------------------ links


def test_read_and_add_links(tmp_path):
    folder = tmp_path / "Podcast"
    add_link(folder, "https://www.youtube.com/watch?v=abc")
    (folder / "links.txt").write_text((folder / "links.txt").read_text() + "# a comment\n\nnot a link\n"
                                      "https://youtu.be/xyz  # the good one\n<https://www.tiktok.com/@a/video/1>\n")
    assert read_links(folder / "links.txt") == ["https://www.youtube.com/watch?v=abc", "https://youtu.be/xyz",
                                                 "https://www.tiktok.com/@a/video/1"]


def test_error_explanations():
    assert "Deno" in explain("Signature solving failed: JavaScript runtime", "https://youtu.be/x", False)
    assert "cookies.txt" in explain("This content requires login", "https://instagram.com/p/x", True)
    assert "isn't supported" in explain("Unsupported URL: https://example.com", "https://example.com", True)


def link_cfg(tmp_path):
    c = cfg()
    c.paths = SimpleNamespace(clips=tmp_path / "clips")
    c.config_path = tmp_path / "config.yaml"
    return c


def test_link_queue_downloads_each_link_once(tmp_path, monkeypatch):
    c = link_cfg(tmp_path)
    add_link(c.paths.clips / "Show", "https://example.com/video1")
    add_link(c.paths.clips / "Show", "https://example.com/broken")
    calls = []

    def fake_download(url, folder, tools, cfg, should_stop):
        calls.append(url)
        if "broken" in url:
            from brainrot_bot.links import LinkError

            raise LinkError("Video unavailable")
        (folder / "video1 [abc].mp4").write_bytes(b"x")
        return [folder / "video1 [abc].mp4"]

    monkeypatch.setattr("brainrot_bot.links.download", fake_download)
    state = State(tmp_path / "state.json")
    queue = LinkQueue(c, state, None, state.save)
    assert queue.run(lambda: False) == 1
    assert queue.run(lambda: False) == 0  # done once; the broken one waits before trying again
    assert calls == ["https://example.com/video1", "https://example.com/broken"]
    items = {i["url"]: i for i in state.data["links"].values()}
    assert items["https://example.com/video1"]["status"] == "done"
    assert items["https://example.com/broken"]["status"] == "pending" and items["https://example.com/broken"]["attempts"] == 1


def test_real_download_of_a_direct_video_link(tmp_path):
    """yt-dlp itself, on a video served from this computer (no internet needed)."""
    pytest.importorskip("yt_dlp")
    from brainrot_bot.links import download
    from brainrot_bot.media import MediaError, find_tools

    try:
        tools = find_tools()
    except MediaError:
        pytest.skip("needs ffmpeg")
    import subprocess

    served = tmp_path / "www"
    served.mkdir()
    subprocess.run([tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25",
                    "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", str(served / "talk.mp4")], check=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(served)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = link_cfg(tmp_path)
        folder = c.paths.clips / "Show"
        files = download(f"http://127.0.0.1:{server.server_address[1]}/talk.mp4", folder, tools, c)
    finally:
        server.shutdown()
    assert len(files) == 1 and files[0].parent == folder and files[0].suffix == ".mp4"
    assert files[0].stat().st_size == (served / "talk.mp4").stat().st_size
    assert not (folder / ".downloading").exists()


# ------------------------------------------------------------------ the AI client (fake Anthropic server)


def ai_with_key(tmp_path, **ai_settings):
    c = cfg(ai=ai_settings)
    creds = Credentials(tmp_path / "credentials")
    creds.set("anthropic", {"api_key": "sk-ant-test"})
    return AI(c, creds)


def test_ai_off_without_key(tmp_path, fake_claude):
    c = cfg()
    assert not AI(c, Credentials(tmp_path / "credentials")).available
    assert fake_claude.requests == []


def test_ai_request_shape_and_answers(tmp_path, fake_claude):
    ai = ai_with_key(tmp_path)
    copy = ai.write_copy("some transcript", 30, "")
    assert copy["hook"] == "He lost everything in one day" and copy["hashtags"] == ["money", "story"]
    request = fake_claude.requests[-1]
    body = request["body"]
    assert request["path"].startswith("/v1/messages")
    assert body["model"] == "claude-opus-5" and body["stream"] is True
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in request["headers"].get("anthropic-beta", "")
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert request["headers"]["x-api-key"] == "sk-ant-test"

    assert ai.translate(["Hello", "World"], "Spanish") == ["ES: Hello", "ES: World"]
    fake_claude.moments = [{"start_sentence": 1, "end_sentence": 3, "hook": "h", "score": 9, "why": "w"}]
    assert ai.pick_moments("[0] 00:00:00 a", "10-minute", "", 3, 20, 60) == fake_claude.moments
    assert fake_claude.requests[-1]["body"]["output_config"]["effort"] == "high"


def test_ai_haiku_gets_no_thinking_or_fallbacks(tmp_path, fake_claude):
    ai = ai_with_key(tmp_path, model="claude-haiku-4-5")
    assert ai.write_copy("t", 30, "")
    body = fake_claude.requests[-1]["body"]
    assert "thinking" not in body and "fallbacks" not in body and "effort" not in body["output_config"]


def test_ai_problems_fall_back_quietly(tmp_path, fake_claude):
    ai = ai_with_key(tmp_path)
    fake_claude.stop_reason = "refusal"
    assert ai.write_copy("t", 30, "") is None and ai.available  # just this once
    fake_claude.stop_reason = "end_turn"
    fake_claude.status = 429
    assert ai.write_copy("t", 30, "") is None and not ai.available  # paused for a while
    ai.resume()
    fake_claude.status = 401
    assert ai.write_copy("t", 30, "") is None and not ai.available  # bad key: paused until reconnected


def test_ai_copy_and_translation_through_the_helpers(tmp_path, fake_claude):
    ai = ai_with_key(tmp_path)
    copy = write_copy(ai, talk(GOOD), "", "Money Talks", 30)
    assert copy.by_ai and copy.hook == "He lost everything in one day" and copy.hashtags[0] == "MoneyTalks"
    words, extras = translate_reel(ai, talk("Hello there. How are you?"), {"hook": "A hook", "title": ""}, "es")
    assert extras == {"hook": "ES: A hook"}
    assert " ".join(w.text for w in words) == "ES: Hello there. ES: How are you?"


def test_check_key(fake_claude):
    assert check_key("sk-ant-good", "claude-opus-5") == ""
    fake_claude.status = 401
    assert "refused" in check_key("sk-ant-bad", "claude-opus-5")
    fake_claude.status = 404
    assert "wasn't found" in check_key("sk-ant-good", "claude-nope")


def test_ai_schemas_are_strict():
    for schema in (ai_module.MOMENTS_SCHEMA, ai_module.COPY_SCHEMA, ai_module.TRANSLATE_SCHEMA):
        text = json.dumps(schema)
        assert '"additionalProperties": false' in text
        assert schema["required"]


def test_loudness_reads_long_files_in_pieces(tmp_path):
    rate = 16000
    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        for minute in range(3):
            level = 0.05 * (minute + 1)
            t = np.arange(rate * 60) / rate
            handle.writeframes((level * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes())
    loud = Loudness.from_wav(path)
    assert len(loud.levels) == 1800
    assert loud.level(10, 11) == pytest.approx(0.05 / np.sqrt(2), rel=0.02)
    assert loud.level(150, 151) == pytest.approx(0.15 / np.sqrt(2), rel=0.02)


def test_state_keeps_links_and_fingerprints(tmp_path):
    state = State(tmp_path / "s.json")
    state.data["links"]["x"] = {"url": "u", "status": "done"}
    state.data["fingerprints"].append({"key": "k"})
    state.save()
    loaded = json.loads(Path(tmp_path / "s.json").read_text())
    assert loaded["links"]["x"]["status"] == "done"
    assert State(tmp_path / "s.json").data["fingerprints"] == [{"key": "k"}]
