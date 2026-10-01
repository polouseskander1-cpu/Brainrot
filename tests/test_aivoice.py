"""AI voices for the voiceover (Google Gemini, ElevenLabs), against fake servers that answer like the real APIs."""

import base64
import io
import json
import math
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from brainrot_bot import aivoice
from brainrot_bot.commentary import Commentator
from brainrot_bot.config import DEFAULTS
from brainrot_bot.credentials import Credentials
from brainrot_bot.media import probe
from brainrot_bot.transcribe import Word
from brainrot_bot.voice import Voice
from test_bot_integration import BASE_CONFIG, SPEECH, TOOLS, FakeTranscriber, connect_fake_ai, ffmpeg, make_workspace, new_bot

class Ears(FakeTranscriber):
    """The clip's words; the voiceover isn't recognized (its captions then follow an estimate)."""

    def transcribe(self, wav, should_stop=None, quiet=False):
        return [] if quiet else super().transcribe(wav, should_stop)


needs_ffmpeg = pytest.mark.skipif(TOOLS is None or "ass" not in TOOLS.filters, reason="needs ffmpeg built with libass")
GOOD_KEY = "test-key-123"


def tone_wav(seconds=1.0, rate=24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True)
                                    for i in range(int(seconds * rate))))
    return buffer.getvalue()


class FakeVoices:
    """Enough of Google's Interactions API and ElevenLabs' API for the voiceover."""

    def __init__(self):
        self.requests: list[dict] = []
        self.fail: dict | None = None  # {"status": 429, "body": {...}} = every request fails like this
        self.bare_pcm = False  # answer like the Gemini TTS models before 3.8 (no WAV header)
        self.mp3 = b""

    def answer(self, method: str, path: str, headers: dict, body: dict) -> tuple[int, str, bytes]:
        self.requests.append({"method": method, "path": path, "headers": headers, "body": body})
        if self.fail:
            return self.fail["status"], "application/json", json.dumps(self.fail["body"]).encode()
        key = headers.get("x-goog-api-key") or headers.get("xi-api-key")
        if key != GOOD_KEY:
            if "x-goog-api-key" in headers:
                return 400, "application/json", json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                                      "status": "INVALID_ARGUMENT"}}).encode()
            return 401, "application/json", json.dumps({"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}).encode()
        if path.startswith("/v1beta/models/"):
            return 200, "application/json", json.dumps({"name": "models/" + path.rsplit("/", 1)[1]}).encode()
        if path == "/v1beta/interactions":
            audio = tone_wav(0.25 * len(body["input"][0]["content"][0]["text"].split()))
            if self.bare_pcm:
                audio = audio[44:]
            answer = {"id": "int_1", "status": "completed", "steps": [
                {"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav",
                                                      "data": base64.b64encode(audio).decode()}]}]}
            return 200, "application/json", json.dumps(answer).encode()
        if path.startswith("/v2/voices"):
            voices = [{"voice_id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "labels": {"gender": "male", "accent": "british"}},
                      {"voice_id": "Xb7hH8MSUJpSbSDYk0k2", "name": "Alice", "labels": {"gender": "female"}}]
            return 200, "application/json", json.dumps({"voices": voices, "has_more": False}).encode()
        if path.startswith("/v1/text-to-speech/"):
            return 200, "audio/mpeg", self.mp3
        return 404, "application/json", b'{"error": {"code": 404, "message": "not found"}}'


@pytest.fixture
def voices_api(monkeypatch, tmp_path):
    fake = FakeVoices()
    if TOOLS is not None:
        ffmpeg("-f", "lavfi", "-i", "sine=f=330:d=1.5", "-c:a", "libmp3lame", "-b:a", "64k", str(tmp_path / "say.mp3"))
        fake.mp3 = (tmp_path / "say.mp3").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            status, kind, data = fake.answer(method, self.path.split("?")[0], {k.lower(): v for k, v in self.headers.items()}, body)
            fake.requests[-1]["query"] = self.path.partition("?")[2]
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("BRAINROT_GEMINI_BASE_URL", url + "/v1beta")
    monkeypatch.setenv("BRAINROT_ELEVENLABS_BASE_URL", url)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield fake
    server.shutdown()
    server.server_close()


def store(folder, **keys):
    folder.mkdir(parents=True, exist_ok=True)
    creds = Credentials(folder / "credentials", encrypt=False)
    for provider, key in keys.items():
        creds.set(provider, {"api_key": key})
    return creds


def settings(**changes):
    return SimpleNamespace(**dict(DEFAULTS["commentary"], **changes))


# ------------------------------------------------------------------ the two APIs


def test_gemini_says_it_with_the_voice_and_style(voices_api, tmp_path):
    out = aivoice.gemini_speak(GOOD_KEY, "Charon", "Would you move to Mars?", tmp_path / "line.raw", style="calm")
    assert out.suffix == ".wav"
    with wave.open(str(out)) as handle:
        assert (handle.getframerate(), handle.getnchannels()) == (24000, 1) and handle.getnframes() > 20000
    sent = voices_api.requests[-1]
    assert sent["path"] == "/v1beta/interactions" and sent["headers"]["x-goog-api-key"] == GOOD_KEY
    body = sent["body"]
    assert body["model"] == "gemini-3.8-flash-tts" and body["generation_config"] == {"speech_config": [{"voice": "Charon"}]}
    assert body["response_format"] == {"type": "audio"}
    content = body["input"][0]["content"][0]
    assert content["text"] == "Would you move to Mars?" and content["annotations"] == [{"type": "speech_metadata", "style": "calm"}]
    voices_api.bare_pcm = True  # the models before 3.8 send bare PCM: it's wrapped into a WAV file
    out = aivoice.gemini_speak(GOOD_KEY, "Puck", "Older model.", tmp_path / "old.wav")
    with wave.open(str(out)) as handle:
        assert handle.getframerate() == 24000 and handle.getnframes() > 10000


def test_gemini_problems_are_explained(voices_api, tmp_path):
    with pytest.raises(aivoice.AIVoiceError, match="refused the key") as refused:
        aivoice.gemini_speak("wrong", "Puck", "Hi there.", tmp_path / "a.wav")
    assert refused.value.wait >= 6 * 3600
    voices_api.fail = {"status": 429, "body": {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                         "message": "Quota exceeded for metric: generate_requests_per_model_per_day"}}}
    with pytest.raises(aivoice.AIVoiceError, match="limit is used up") as limit:
        aivoice.gemini_speak(GOOD_KEY, "Puck", "Hi there.", tmp_path / "a.wav")
    assert limit.value.wait == 3600
    voices_api.fail = None
    assert aivoice.check_gemini_key(GOOD_KEY) == ""
    assert "refused the key" in aivoice.check_gemini_key("wrong")


@needs_ffmpeg
def test_elevenlabs_says_it_with_the_chosen_voice(voices_api, tmp_path):
    out = aivoice.elevenlabs_speak(GOOD_KEY, "Xb7hH8MSUJpSbSDYk0k2", "Would you move to Mars?", tmp_path / "line.wav", speed=1.5)
    assert out.suffix == ".mp3" and probe(TOOLS, out).duration > 1.0
    sent = voices_api.requests[-1]
    assert sent["path"] == "/v1/text-to-speech/Xb7hH8MSUJpSbSDYk0k2" and sent["query"] == "output_format=mp3_44100_128"
    assert sent["headers"]["xi-api-key"] == GOOD_KEY
    assert sent["body"] == {"text": "Would you move to Mars?", "model_id": "eleven_v4", "voice_settings": {"speed": 1.2}}
    assert aivoice.elevenlabs_voices(GOOD_KEY) == ("", [
        {"id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "about": "male, british"},
        {"id": "Xb7hH8MSUJpSbSDYk0k2", "name": "Alice", "about": "female"}])
    problem, voices = aivoice.elevenlabs_voices("wrong")
    assert "refused the key" in problem and voices == []
    # Out of credits comes back as 401, like a wrong key: it must read as a limit, not a refused key.
    voices_api.fail = {"status": 401, "body": {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}}
    with pytest.raises(aivoice.AIVoiceError, match="limit is used up"):
        aivoice.elevenlabs_speak(GOOD_KEY, "Xb7hH8MSUJpSbSDYk0k2", "Hi.", tmp_path / "b.wav")


def test_voice_settings_are_read():
    assert aivoice.parse("gemini:Charon") == ("gemini", "Charon")
    assert aivoice.parse("elevenlabs") == ("elevenlabs", "JBFqnCBsd6RMkjVDRZzb")
    assert aivoice.parse("norman") is None and aivoice.parse("system") is None
    assert aivoice.valid("gemini:Puck") and aivoice.valid("elevenlabs:Xb7hH8MSUJpSbSDYk0k2")
    assert not aivoice.valid("gemini:two words") and not aivoice.valid("elevenlabs:short")


# ------------------------------------------------------------------ the voice picks the AI voice, or the free one


def test_the_ai_voice_reads_when_connected_and_the_free_voice_steps_in(voices_api, tmp_path, monkeypatch, caplog):
    local = []

    def free_voice(text, language, out):
        local.append(text)
        out.write_bytes(tone_wav(0.5))
        return out

    no_key = Voice("gemini:Puck", 1.1, TOOLS, tmp_path, credentials=store(tmp_path / "a"), settings=settings())
    monkeypatch.setattr(no_key, "_system", free_voice)
    monkeypatch.setattr(no_key, "_local_engine", lambda language: "system")
    assert no_key.engine_for("en") == "system"  # no key connected: the free voice
    no_key.speak("No key here.", "en", tmp_path / "x.wav")
    assert local == ["No key here."] and no_key.last_engine == "system"

    voice = Voice("gemini:Puck", 1.1, TOOLS, tmp_path, credentials=store(tmp_path / "b", gemini=GOOD_KEY), settings=settings())
    monkeypatch.setattr(voice, "_system", free_voice)
    monkeypatch.setattr(voice, "_local_engine", lambda language: "system")
    assert voice.engine_for("es") == "gemini"  # it speaks every language
    assert voice.describe("en") == "Puck (upbeat, male), Google Gemini AI voice"
    out = voice.speak("Say it nicely.", "en", tmp_path / "y.wav")
    assert out.suffix == ".wav" and voice.last_engine == "gemini"
    assert voices_api.requests[-1]["body"]["input"][0]["content"][0]["annotations"][0]["style"] == aivoice.STYLE

    voices_api.fail = {"status": 429, "body": {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}}
    voice.speak("Limit reached.", "en", tmp_path / "z.wav")
    assert local[-1] == "Limit reached." and voice.last_engine == "system"
    assert "The AI voice didn't work" in caplog.text and "tried again in 60 minutes" in caplog.text
    asked = len(voices_api.requests)
    voices_api.fail = None
    voice.speak("Still resting.", "en", tmp_path / "w.wav")
    assert len(voices_api.requests) == asked and voice.last_engine == "system"  # not asked again within the hour


def test_a_reel_never_mixes_two_voices(voices_api, tmp_path, monkeypatch):
    """The AI voice reads the line before the clip, then hits its limit: the free voice reads both lines."""
    if TOOLS is None:
        pytest.skip("needs ffmpeg")
    cfg = SimpleNamespace(commentary=settings(voice="gemini:Puck", max_seconds=60), captions=SimpleNamespace(enabled=False))
    voice = Voice("gemini:Puck", 1.1, TOOLS, tmp_path, credentials=store(tmp_path, gemini=GOOD_KEY), settings=cfg.commentary)
    said_by_pc = []

    def free_voice(text, language, out):
        said_by_pc.append(text)
        out.write_bytes(tone_wav(0.3 * len(text.split()), 22050))
        return out

    monkeypatch.setattr(voice, "_system", free_voice)
    monkeypatch.setattr(voice, "_local_engine", lambda language: "system")
    real_cloud = voice._cloud
    calls = []

    def cloud(provider, text, out):
        calls.append(text)
        if len(calls) > 1:  # the second line hits the daily limit
            voices_api.fail = {"status": 429, "body": {"error": {"code": 429, "message": "Quota exceeded"}}}
        return real_cloud(provider, text, out)

    monkeypatch.setattr(voice, "_cloud", cloud)
    ai = SimpleNamespace(available=True, write_commentary=lambda *a: {"intro": "Watch this one.", "outro": "Would you do it?"})
    voiceover = Commentator(cfg, TOOLS, ai, voice).make(tmp_path / "c.mp4", [Word("Hi.", 0.0, 0.5)], "en", "", tmp_path, "1")
    assert [x.engine for x in voiceover.lines] == ["system", "system"]
    assert said_by_pc == ["Would you do it?", "Watch this one."]  # the intro was read again by the same voice


# ------------------------------------------------------------------ the menu


def test_menu_connects_google_and_picks_a_voice(voices_api, tmp_path, monkeypatch):
    from brainrot_bot.wizard import choose_voice
    from test_app import typed

    creds = Credentials(tmp_path / "credentials", encrypt=False)
    cfg = SimpleNamespace(commentary=settings(), paths=SimpleNamespace(credentials=tmp_path / "credentials"))
    typed(monkeypatch, "1", "wrong-key", "y", GOOD_KEY, "2")  # Google; a wrong key, again; the right one; Charon
    assert choose_voice(cfg, creds) == "gemini:Charon"
    assert creds.get("gemini")["api_key"] == GOOD_KEY
    typed(monkeypatch, "1", "", "3")  # Google again: keep the connected key, pick Fenrir
    assert choose_voice(cfg, creds) == "gemini:Fenrir"


@needs_ffmpeg
def test_menu_connects_elevenlabs_and_lists_your_voices(voices_api, tmp_path, monkeypatch):
    from brainrot_bot.wizard import choose_voice
    from test_app import typed

    creds = Credentials(tmp_path / "credentials", encrypt=False)
    cfg = SimpleNamespace(commentary=settings(), paths=SimpleNamespace(credentials=tmp_path / "credentials"))
    typed(monkeypatch, "2", GOOD_KEY, "2")  # ElevenLabs; the key; Alice
    assert choose_voice(cfg, creds) == "elevenlabs:Xb7hH8MSUJpSbSDYk0k2"
    typed(monkeypatch, "3")  # Norman, the free voice on this PC
    assert choose_voice(cfg, creds) == "norman"


# ------------------------------------------------------------------ a whole reel


@needs_ffmpeg
def test_a_reel_with_the_google_voice(voices_api, tmp_path, fake_claude):
    workspace = make_workspace(tmp_path)
    (workspace / "config.yaml").write_text(BASE_CONFIG.replace("commentary:\n  enabled: false\n",
                                                               "commentary:\n  voice: gemini:Kore\n"), encoding="utf-8")
    connect_fake_ai(workspace)
    Credentials(workspace / "credentials").set("gemini", {"api_key": GOOD_KEY})
    bot = new_bot(workspace)
    bot.transcriber = Ears(SPEECH)
    assert bot.run_once() == 1
    reel = workspace / "output" / "show" / "ep1.mp4"
    # "Here is why this matters." (5 words) and "Honestly, ... same?" (11 words): 0.25 s a word from the fake API
    assert probe(TOOLS, reel).duration > probe(TOOLS, workspace / "clips" / "show" / "ep1.mp4").duration + 4
    sent = [r for r in voices_api.requests if r["path"] == "/v1beta/interactions"]
    assert [r["body"]["input"][0]["content"][0]["text"] for r in sent] == [
        "Here is why this matters.", "Honestly, I think he is right. Would you do the same?"]
    assert {r["body"]["generation_config"]["speech_config"][0]["voice"] for r in sent} == {"Kore"}
    assert "Here is why this matters." in reel.with_suffix(".srt").read_text(encoding="utf-8")
