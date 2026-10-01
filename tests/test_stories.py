"""Stories: ideas become scripts, scripts become reels over gameplay or full AI videos. Google (pictures and
shots), Reddit, Telegram and Claude are fakes that answer like the real APIs; the videos are real renders."""

import base64
import json
import math
import re
import wave
from types import SimpleNamespace

import pytest

from brainrot_bot.bot import Bot
from brainrot_bot.config import DEFAULTS, ConfigError, load_config
from brainrot_bot.credentials import Credentials
from brainrot_bot.media import probe
from brainrot_bot.phone import Action, Telegram
from brainrot_bot.stories import google, reddit, styles
from brainrot_bot.stories.menu import drop_in_inbox
from brainrot_bot.stories.narrate import scene_cuts
from brainrot_bot.stories.script import Script, clean, scene_range, story_prompt, word_range
from brainrot_bot.stories.studio import estimate, plain_script
from brainrot_bot.transcribe import Word
from conftest import STORY
from test_bot_integration import BASE_CONFIG, TOOLS, connect_fake_ai, ffmpeg, make_workspace
from test_phone import make_phone, tg_ok
from test_uploads import FakeApi, response

needs_ffmpeg = pytest.mark.skipif(TOOLS is None or "ass" not in TOOLS.filters, reason="needs ffmpeg built with libass")
KEY = "google-test-key"
STORY_CONFIG = BASE_CONFIG + "stories:\n  seconds: 20\n"


def stories_cfg(**changes):
    return SimpleNamespace(stories=SimpleNamespace(**dict(DEFAULTS["stories"], **changes)))


# ------------------------------------------------------------------ the script


def test_the_prompt_asks_for_a_story_that_fits_the_length_and_the_source():
    prompt = story_prompt("A lighthouse that blinks", source="reddit", subreddit="LetsNotMeet", seconds=75)
    assert word_range(75) == (172, 197) and scene_range(75) == (10, 14)
    assert "172-197 words" in prompt and "10-14 scenes" in prompt
    assert "r/LetsNotMeet" in prompt and "new, original story in your own words" in prompt
    assert "Would you have opened the door?" in prompt  # the closing question to the viewer
    assert "never children" in prompt and "No real people" in prompt
    mine = story_prompt("My idea", source="you", question=False, language="Spanish")
    assert "Reddit" not in mine and "Would you" not in mine and "Language of everything you write: Spanish" in mine


def test_the_ais_script_is_cleaned_up():
    data = json.loads(json.dumps(STORY))
    data["scenes"][0]["narration"] = "Every night at nine 🌙 the old lighthouse #creepy blinked [pause] twice."
    data["characters"] += [{"name": "Extra", "look": "x"}, {"name": "Fourth", "look": "y"}]
    data["hook"] = "A hook that is far too long to fit on the screen at all"
    script = clean(data)
    assert script.scenes[0].narration == "Every night at nine the old lighthouse blinked twice."
    assert [c.name for c in script.characters] == ["Mara", "Tom", "Extra"]  # 3 at most
    assert script.hashtags == ["mystery", "story", "lighthouse"]
    assert len(script.hook.split()) <= 9
    assert [c.name for c in script.cast(["mara", "Nobody"])] == ["Mara"]  # unknown names are ignored
    data["scenes"] = data["scenes"][:1]
    assert clean(data) is None  # not a story


def test_scenes_are_cut_in_the_pauses_between_them():
    script = Script.from_dict({"scenes": [{"narration": "One two three."}, {"narration": "Four five."}, {"narration": "Six."}]})
    words = [Word("One", 0.1, 0.4), Word("two", 0.5, 0.8), Word("three.", 0.9, 1.4), Word("Four", 2.0, 2.3),
             Word("five.", 2.4, 2.9), Word("Six.", 3.1, 3.3)]
    # In the middle of each pause ("three." ends at 1.4, "Four" starts at 2.0), and the last picture holds 0.8 s.
    assert scene_cuts(script, words, 3.5) == pytest.approx([0.0, 1.7, 3.0, 4.3])
    words[3:] = [Word("Four", 1.5, 1.6), Word("five.", 1.6, 1.7), Word("Six.", 1.8, 2.0)]
    cuts = scene_cuts(script, words, 2.1)
    assert cuts[2] - cuts[1] >= 1.0 and cuts[3] - cuts[2] >= 1.0  # every scene gets at least a second
    assert scene_cuts(script, [], 6.0)[1] == pytest.approx(3.0, abs=0.04)  # no timings: by the number of words


def test_cost_of_an_ai_video():
    script = clean(json.loads(json.dumps(STORY)))
    fast = estimate(stories_cfg(), script, [3.0, 5.0, 7.5, 9.0])  # shots of 4, 6, 8 and 8 seconds
    assert (fast.pictures, fast.video_seconds) == (6, 26)
    assert fast.total == round(6 * 0.067 + 26 * 0.10, 2)
    assert estimate(stories_cfg(video_model="veo-3.1-lite-generate-preview"), script, [3.0, 5.0, 7.5, 9.0]).total == round(6 * 0.067 + 26 * 0.05, 2)
    assert estimate(stories_cfg(video_model="none"), script, [3.0, 5.0, 7.5, 9.0]).total == round(6 * 0.067, 2)
    assert google.shot_seconds(4.0) == 4 and google.shot_seconds(4.3) == 6 and google.shot_seconds(12) == 8
    assert google.shot_seconds(3.0, "1080p") == 8  # Veo makes 1080p shots of 8 seconds only
    assert estimate(stories_cfg(resolution="1080p"), script, [3.0, 5.0, 7.5, 9.0]).total == round(6 * 0.067 + 32 * 0.12, 2)


def test_your_own_story_without_the_ai():
    script = plain_script("I found a key in my soup. It opened nothing. Then, years later, my daughter's music box clicked open.")
    assert [x.narration for x in script.scenes] == [  # sentences share a scene up to 20 words
        "I found a key in my soup. It opened nothing. Then, years later, my daughter's music box clicked open."]
    assert script.hook == "I found a key in my soup." and not any(x.visual for x in script.scenes)
    assert len(plain_script("One two three four five six seven eight nine ten. " * 5).scenes) == 3


def test_every_look_has_its_engineered_prompts():
    assert set(styles.STYLES) == {"claymation", "anime", "cartoon3d"}
    for style in styles.STYLES.values():
        picture = style.picture_prompt("Mara climbs the stairs.", "A rocky coast.", "Reference picture 1: Mara.", "low angle")
        assert "Mara climbs the stairs." in picture and style.look in picture and "no text anywhere" in picture
        assert "9:16" in picture
        shot = style.video_prompt("Mara climbs the stairs.", "slow tilt up", "creaking stairs")
        assert "No music, no speech" in shot and style.motion in shot
        assert "text" in style.avoid and "subtitles" in style.avoid
        for name in ("Pixar", "Disney", "Ghibli", "Aardman", "Laika", "Shinkai"):  # a look, never a studio's name
            assert name.lower() not in (style.look + style.motion).lower()


def test_story_settings_are_checked(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("stories:\n  reddit: [r/Stoicism, 'https://www.reddit.com/r/tifu/', nosleep, Stoicism]\n  style: anime\n",
                    encoding="utf-8")
    cfg = load_config(path)
    assert cfg.stories.reddit == ["Stoicism", "tifu"] and cfg.stories.style == "anime"  # r/nosleep forbids retelling
    assert cfg.paths.stories == tmp_path / "stories"
    for bad in ("  style: watercolor\n", "  approval: maybe\n", "  seconds: 200\n", "  reddit: ['not a sub!']\n",
                "  video_model: 'veo 3'\n"):
        path.write_text("stories:\n" + bad, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)


# ------------------------------------------------------------------ Reddit


def reddit_listing(*posts):
    return {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


def post(pid, score, **extra):
    data = {"id": pid, "subreddit": "Glitch_in_the_Matrix", "title": f"Post {pid}: the clock stopped at 3:33",
            "selftext": "It happened again last night. " * 5, "score": score, "permalink": f"/r/x/comments/{pid}/",
            "is_self": True}
    data.update(extra)
    return data


def test_reddit_reads_top_posts_as_the_app_itself(monkeypatch):
    api = (FakeApi(monkeypatch)
           .on("POST", r"reddit\.com/api/v1/access_token", response(200, {"access_token": "tok", "token_type": "bearer",
                                                                               "expires_in": 86400, "scope": "*"}))
           .on("GET", r"oauth\.reddit\.com/r/Glitch_in_the_Matrix/top", response(200, reddit_listing(
               post("a1", 5000), post("a2", 9000, over_18=True), post("a3", 7000, stickied=True),
               post("a4", 8000, selftext="[removed]"), post("a5", 100), post("a6", 6000)))))
    client = reddit.Reddit("app-id", "app-secret", "u/storyfan")
    posts = client.top("r/Glitch_in_the_Matrix")
    assert [p.id for p in posts] == ["a1", "a5", "a6"]  # no NSFW, pinned or removed posts
    token = api.find("POST", "access_token")[0]
    assert token.form == {"grant_type": "client_credentials"}  # never your password
    assert token.headers["Authorization"] == "Basic " + base64.b64encode(b"app-id:app-secret").decode()
    listing = api.find("GET", "/top")[0]
    assert listing.headers["Authorization"] == "bearer tok" and listing.params["t"] == "week"
    assert re.fullmatch(r"(windows|macos|linux):brainrot-bot:[\d.]+ \(by /u/storyfan\)", listing.headers["User-Agent"])
    assert client.top("nosleep") == []  # never asked for
    assert len(api.find("GET", "/top")) == 1
    best = reddit.pick(posts, used={"a6"}, min_score=300, count=2)
    assert [p.id for p in best] == ["a1"]  # a6 was used before, a5 has too few upvotes


def test_reddit_refusing_the_app_is_explained(monkeypatch):
    FakeApi(monkeypatch).on("POST", "access_token", response(401, {"message": "Unauthorized", "error": 401}))
    assert "refused the app ID or secret" in reddit.check("id", "wrong")


# ------------------------------------------------------------------ Google: pictures and shots


def fake_png(tmp_path):
    path = tmp_path / "fake.png"
    if TOOLS is not None:
        ffmpeg("-f", "lavfi", "-i", "color=c=0x3060c0:s=360x640", "-frames:v", "1", str(path))
    else:
        path.write_bytes(b"\x89PNG" + b"0" * 2000)
    return path.read_bytes()


def fake_shot(tmp_path):
    path = tmp_path / "shot.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=360x640:r=24", "-f", "lavfi", "-i", "anoisesrc=a=0.3", "-t", "8",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path))
    return path.read_bytes()


class FakeGoogle:
    """Nano Banana 2 (Interactions API) and Veo 3.1 (long-running operations), answering like Google."""

    def __init__(self, monkeypatch, png: bytes, mp4: bytes = b""):
        self.png, self.mp4 = png, mp4
        self.ops: dict[str, dict] = {}
        self.fail_shots = None  # an error answer for every new shot
        self.api = (FakeApi(monkeypatch)
                    .on("POST", r"generativelanguage\.googleapis\.com/v1beta/interactions$", self.picture)
                    .on("GET", r"generativelanguage\.googleapis\.com/v1beta/models/[^/:]+$", response(200, {"name": "models/x"}))
                    .on("POST", r":predictLongRunning$", self.start)
                    .on("GET", r"/v1beta/models/[^/]+/operations/", self.poll)
                    .on("GET", r"/v1beta/files/", self.file)
                    .on("GET", r"^https://storage\.example/", lambda call: response(200, self.mp4)))

    def picture(self, call):
        assert call.headers["x-goog-api-key"] == KEY
        text = call.json["input"][0]["text"]
        if "BLOCKED" in text:
            return response(200, {"id": "i", "status": "completed", "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": "I can't draw that."}]}]})
        return response(200, {"id": "i", "status": "completed", "steps": [
            {"type": "thought", "content": [{"type": "text", "text": "thinking"}]},
            {"type": "model_output", "content": [{"type": "image", "mime_type": "image/png",
                                                  "data": base64.b64encode(self.png).decode()}]}]})

    def start(self, call):
        if self.fail_shots is not None:
            return self.fail_shots
        name = f"models/veo-3.1-fast-generate-preview/operations/op{len(self.ops) + 1}"
        self.ops[name] = {"polls": 0, "prompt": call.json["instances"][0]["prompt"]}
        return response(200, {"name": name})

    def poll(self, call):
        name = call.url.split("/v1beta/", 1)[1]
        op = self.ops[name]
        op["polls"] += 1
        if op["polls"] < 2:
            return response(200, {"name": name, "done": False})
        if "UNSAFE" in op["prompt"]:
            return response(200, {"name": name, "done": True, "response": {"generateVideoResponse": {
                "raiMediaFilteredCount": 1, "raiMediaFilteredReasons": ["Unsafe content"]}}})
        uri = f"https://generativelanguage.googleapis.com/v1beta/files/{name.rsplit('/', 1)[1]}:download?alt=media"
        return response(200, {"name": name, "done": True, "response": {
            "@type": "type.googleapis.com/google.ai.generativelanguage.v1beta.PredictLongRunningResponse",
            "generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}}})

    def file(self, call):
        assert call.headers["x-goog-api-key"] == KEY
        return response(302, b"", Location=f"https://storage.example/{call.url.rsplit('/', 1)[1]}")

    def shots(self):
        return self.api.find("POST", "predictLongRunning")

    def pictures(self):
        return self.api.find("POST", "/interactions")


def test_a_picture_with_reference_pictures(monkeypatch, tmp_path):
    fake = FakeGoogle(monkeypatch, fake_png(tmp_path))
    ref = tmp_path / "mara.png"
    ref.write_bytes(fake.png)
    out = google.make_picture(KEY, "Mara on the stairs", tmp_path / "scene_01", references=[ref])
    assert out == tmp_path / "scene_01.png" and out.read_bytes() == fake.png
    body = fake.pictures()[0].json
    assert body["model"] == "gemini-3.1-flash-image"
    assert body["response_format"] == {"type": "image", "aspect_ratio": "9:16", "image_size": "1K"}
    assert body["input"][1] == {"type": "image", "mime_type": "image/png", "data": base64.b64encode(fake.png).decode()}
    with pytest.raises(google.GoogleError, match="I can't draw that") as refused:
        google.make_picture(KEY, "BLOCKED", tmp_path / "scene_02")
    assert refused.value.blocked


def test_a_shot_is_started_collected_and_downloaded_without_leaking_the_key(monkeypatch, tmp_path):
    fake = FakeGoogle(monkeypatch, fake_png(tmp_path), b"m" * 5000)
    frame = tmp_path / "frame.png"
    frame.write_bytes(fake.png)
    name = google.start_shot(KEY, "Mara climbs. Camera: tilt up.", frame, seconds=6, negative="text, music")
    body = fake.shots()[0].json
    assert body["instances"][0]["image"] == {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(fake.png).decode()}}
    assert body["parameters"] == {"aspectRatio": "9:16", "resolution": "720p", "durationSeconds": 6,
                                  "personGeneration": "allow_adult", "negativePrompt": "text, music"}
    assert not google.check_shot(KEY, name).done
    shot = google.check_shot(KEY, name)
    assert shot.done and shot.uri.endswith(":download?alt=media")
    out = google.download_shot(KEY, shot.uri, tmp_path / "scene_01.mp4")
    assert out.read_bytes() == fake.mp4
    storage = fake.api.find("GET", "storage.example")[0]
    assert "x-goog-api-key" not in storage.headers  # the key only ever goes to Google's API


def test_google_problems_are_explained(monkeypatch, tmp_path):
    FakeApi(monkeypatch).on("POST", "interactions", response(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 0"}}))
    with pytest.raises(google.GoogleError, match="billing turned on") as no_billing:
        google.make_picture(KEY, "x", tmp_path / "a")
    assert no_billing.value.wait == 24 * 3600
    FakeApi(monkeypatch).on("POST", "predictLongRunning", response(400, {"error": {"code": 400,
        "message": "The prompt could not be submitted. It violates our Responsible AI practices."}}))
    with pytest.raises(google.GoogleError) as unsafe:
        google.start_shot(KEY, "x", None)
    assert unsafe.value.blocked


# ------------------------------------------------------------------ whole stories


class FakeVoice:
    """Reads text as a tone, 0.3 s a word."""

    def __init__(self):
        self.last_engine = "piper"
        self.settings = SimpleNamespace(style="")
        self.said = []

    def describe(self, language=""):
        return "a test voice"

    def engine_for(self, language):
        return "piper"

    def speak(self, text, language, out, local=False):
        self.said.append(text)
        rate = 24000
        with wave.open(str(out), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"".join(int(6000 * math.sin(i / 9)).to_bytes(2, "little", signed=True)
                                        for i in range(int(0.3 * len(text.split()) * rate))))
        return out


def story_bot(workspace, extra=""):
    (workspace / "config.yaml").write_text(STORY_CONFIG + extra, encoding="utf-8")
    bot = Bot(load_config(workspace / "config.yaml"), TOOLS)
    bot.transcriber = None  # the captions follow the words' estimated times
    bot.studio.voice = FakeVoice()
    bot.pause = lambda seconds: None
    return bot


@pytest.fixture
def story_workspace(tmp_path, fake_claude):
    workspace = make_workspace(tmp_path)
    connect_fake_ai(workspace)
    return workspace


@needs_ffmpeg
def test_an_idea_becomes_a_story_over_gameplay(story_workspace, fake_claude):
    bot = story_bot(story_workspace, "  make: gameplay\n")
    story_id = bot.studio.add_idea("A lighthouse that blinks with nobody inside")
    assert bot.studio.run()  # written; over gameplay is free, so it's made without asking (approval: ai_only)
    entry = bot.state.data["stories"][story_id]
    assert entry["status"] == "approved" and entry["make"] == "gameplay" and entry["title"] == STORY["title"]
    prompt = next(r["body"]["messages"][0]["content"] for r in fake_claude.requests if r.get("body"))
    assert "A lighthouse that blinks with nobody inside" in prompt
    assert bot.studio.run()
    assert entry["status"] == "done" and entry["cost"] == 0
    reel = story_workspace / "output" / "Stories" / f"The_Lighthouse_That_Blinked_Twice_{story_id}.mp4"
    info = probe(TOOLS, reel)
    words = sum(len(s["narration"].split()) for s in STORY["scenes"])
    assert (info.width, info.height) == (360, 640) and info.has_audio
    assert abs(info.duration - (0.3 * words + 0.8)) < 0.6  # the narration and the last picture's hold
    srt = reel.with_suffix(".srt").read_text(encoding="utf-8")
    assert "Every night at nine" in srt and "own handwriting" in srt
    assert reel.with_suffix(".jpg").exists()
    assert bot.studio.voice.said == [" ".join(s["narration"] for s in STORY["scenes"])]  # read in one go
    folder = story_workspace / "stories" / f"{story_id} The_Lighthouse_That_Blinked_Twice"
    assert (folder / "script.json").exists() and (folder / "narration.wav").exists()
    assert bot.state.usage == {"run.mp4": 1}


@needs_ffmpeg
def test_an_ai_video_waits_for_your_ok_then_is_drawn_and_filmed(story_workspace, fake_claude, monkeypatch):
    fake = FakeGoogle(monkeypatch, fake_png(story_workspace), fake_shot(story_workspace))
    Credentials(story_workspace / "credentials").set("gemini", {"api_key": KEY})
    fake_claude.story["scenes"][2] = dict(fake_claude.story["scenes"][2], visual="UNSAFE: Tom laughs at the bar.")
    bot = story_bot(story_workspace, "  style: anime\n")
    story_id = bot.studio.add_idea("A lighthouse that blinks with nobody inside")
    bot.studio.run()
    entry = bot.state.data["stories"][story_id]
    assert entry["status"] == "ready"  # an AI video costs money: it waits for your OK
    assert not bot.studio.run() and not fake.pictures()
    assert "Making the AI video (Claymation)" in bot.studio.choose(story_id, "ai", "claymation")
    assert bot.studio.run()
    assert entry["status"] == "done", entry.get("error")

    pictures = fake.pictures()
    assert len(pictures) == 2 + 4  # Mara and Tom, then the 4 scenes
    sheets = [p.json["input"][0]["text"] for p in pictures[:2]]  # drawn at the same time, in any order
    sheet = next(text for text in sheets if "character design reference picture of Mara" in text)
    assert styles.STYLES["claymation"].look in sheet and "grey braid" in sheet
    scene3 = next(p.json for p in pictures if "Tom laughs" in p.json["input"][0]["text"])
    assert len(scene3["input"]) == 3  # the prompt, and Tom's and Mara's reference pictures
    assert "Reference picture 1: Mara" in scene3["input"][0]["text"] and "Reference picture 2: Tom" in scene3["input"][0]["text"]

    shots = fake.shots()
    assert len(shots) == 4
    assert all(s.json["parameters"]["aspectRatio"] == "9:16" and s.json["parameters"]["personGeneration"] == "allow_adult"
               for s in shots)
    assert all(s.json["parameters"]["durationSeconds"] in (4, 6, 8) for s in shots)
    assert styles.STYLES["claymation"].motion in shots[0].json["instances"][0]["prompt"]

    lengths = json.loads((story_workspace / "stories" / entry["folder"] / "narration.json").read_text())
    seconds = sum(google.shot_seconds(min(8.0, b - a)) for a, b in zip(lengths["cuts"], lengths["cuts"][1:]))
    blocked = google.shot_seconds(min(8.0, lengths["cuts"][3] - lengths["cuts"][2]))  # the unsafe shot isn't charged
    assert entry["cost"] == pytest.approx(6 * 0.067 + (seconds - blocked) * 0.10, abs=0.001)
    assert bot.studio.spent_today() == pytest.approx(entry["cost"])
    folder = story_workspace / "stories" / entry["folder"]
    assert sorted(p.name for p in folder.glob("scene_*.mp4")) == ["scene_01.mp4", "scene_02.mp4", "scene_04.mp4"]

    reel = story_workspace / "output" / "Stories" / f"The_Lighthouse_That_Blinked_Twice_{story_id}.mp4"
    info = probe(TOOLS, reel)
    assert (info.width, info.height) == (360, 640) and abs(info.duration - lengths["cuts"][-1]) < 0.15


@needs_ffmpeg
def test_nothing_is_paid_twice_after_a_failure(story_workspace, fake_claude, monkeypatch):
    fake = FakeGoogle(monkeypatch, fake_png(story_workspace), fake_shot(story_workspace))
    fake.fail_shots = response(429, {"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota)."}})
    fake_claude.story["scenes"][1] = dict(fake_claude.story["scenes"][1], visual="BLOCKED: the empty tower in the fog.")
    Credentials(story_workspace / "credentials").set("gemini", {"api_key": KEY})
    bot = story_bot(story_workspace, "  approval: none\n")
    story_id = bot.studio.add_idea("An idea")
    bot.studio.run()
    entry = bot.state.data["stories"][story_id]
    assert entry["make"] == "ai" and entry["status"] == "approved"
    assert not bot.studio.run()  # the pictures are drawn, then Google's limit stops the shots
    assert entry["status"] == "making" and entry["attempts"] == 1 and "limit" in entry["error"]
    drawn = len(fake.pictures())
    assert drawn == 7  # 2 characters, 4 scenes, and the place instead of the scene Google wouldn't draw
    assert "An establishing shot of the place" in fake.pictures()[-1].json["input"][0]["text"] or any(
        "An establishing shot of the place" in c.json["input"][0]["text"] for c in fake.pictures())
    assert entry["cost"] == pytest.approx(6 * 0.067)  # a refused picture isn't paid for
    assert not bot.studio.run()  # Google said its limit is used up: AI videos wait an hour
    fake.fail_shots = None
    entry["next_try"] = 0
    bot.studio._paused_until.clear()  # an hour later
    assert bot.studio.run()
    assert entry["status"] == "done" and len(fake.pictures()) == drawn  # the pictures weren't drawn again


@needs_ffmpeg
def test_the_daily_budget_is_never_passed(story_workspace, fake_claude, monkeypatch):
    fake = FakeGoogle(monkeypatch, fake_png(story_workspace), fake_shot(story_workspace))
    Credentials(story_workspace / "credentials").set("gemini", {"api_key": KEY})
    bot = story_bot(story_workspace, "  approval: none\n  daily_budget: 2\n")
    bot.state.data["story_spend"] = {__import__("datetime").date.today().isoformat(): 1.5}
    story_id = bot.studio.add_idea("An idea")
    bot.studio.run()
    assert not bot.studio.run()
    entry = bot.state.data["stories"][story_id]
    assert entry["status"] == "making" and "budget" in entry["error"] and entry["next_try"] > __import__("time").time() + 500
    assert not fake.pictures() and not fake.shots()  # nothing was spent


# ------------------------------------------------------------------ the phone and the menu


def test_telegram_ideas_and_story_buttons(monkeypatch, tmp_path):
    creds = Credentials(tmp_path / "credentials", encrypt=False)
    creds.set("telegram", {"token": "t", "chat_id": "42"})
    phone = make_phone(tmp_path, creds, telegram=True)
    phone.stories_provider = lambda: "Stories: 1 waiting"
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/idea a lighthouse that blinks"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/story"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "I found a key in my soup."}},
        {"update_id": 4, "message": {"chat": {"id": 42}, "text": "/stories"}},
    ]
    api = FakeApi(monkeypatch).on("POST", "getUpdates", tg_ok(updates)).on("POST", "sendMessage", tg_ok({"message_id": 5}))
    bot = Telegram("t", "42")
    bot.poll(phone)
    assert phone.actions.get_nowait() == Action("idea", text="a lighthouse that blinks")
    assert phone.actions.get_nowait() == Action("idea", text="I found a key in my soup.", choice="words")
    texts = [c.json["text"] for c in api.find("POST", "sendMessage")]
    assert texts[1].startswith("Send me your story") and texts[-1] == "Stories: 1 waiting"

    # The script arrives with its buttons: the AI video in the chosen look, the looks, gameplay, again, drop.
    phone.story_ready("abc123", "📝 The Lighthouse", {k: 8.4 for k in styles.STYLES}, "claymation", True)
    job = phone.outbox.get_nowait()
    api = FakeApi(monkeypatch).on("POST", "sendMessage", tg_ok({"message_id": 9}))
    bot.send_story(*job[1:])
    keyboard = api.find("POST", "sendMessage")[0].json["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0] == {"text": "🎬 AI video · Clay · ~$8.40", "callback_data": "sa|abc123"}
    assert [b["text"] for b in keyboard[1]] == ["Clay ✓", "Anime", "3D"]
    assert keyboard[2][0]["callback_data"] == "sg|abc123"

    taps = [{"update_id": 5, "callback_query": {"id": "q1", "data": "ss|abc123|anime", "message": {"message_id": 9, "chat": {"id": 42}}}},
            {"update_id": 6, "callback_query": {"id": "q2", "data": "sa|abc123", "message": {"message_id": 9, "chat": {"id": 42}}}}]
    api = (FakeApi(monkeypatch).on("POST", "getUpdates", tg_ok(taps)).on("POST", "editMessageReplyMarkup", tg_ok(True))
           .on("POST", "answerCallbackQuery", tg_ok(True)))
    bot.poll(phone)
    edits = [c.json["reply_markup"]["inline_keyboard"] for c in api.find("POST", "editMessageReplyMarkup")]
    assert [b["text"] for b in edits[0][1]] == ["Clay", "Anime ✓", "3D"] and "Anime" in edits[0][0][0]["text"]
    assert edits[1] == [[{"text": "🎬 AI video (Anime)", "callback_data": "x|done"}]]
    assert phone.actions.get_nowait() == Action("story", story="abc123", choice="ai", style="anime")
    assert phone.story_memory("abc123") is None  # answered


@needs_ffmpeg
def test_ideas_and_choices_from_the_menu(story_workspace, fake_claude):
    bot = story_bot(story_workspace, "  approval: all\n")  # every script waits for your OK
    drop_in_inbox(story_workspace / "stories", {"idea": "A clock that runs backwards", "words": False})
    (story_workspace / "stories" / "inbox" / "mine.txt").write_text("My own idea from a file", encoding="utf-8")
    bot.studio.run()
    ideas = sorted(e["idea"] for e in bot.state.data["stories"].values())
    assert ideas == ["A clock that runs backwards", "My own idea from a file"]
    assert not list((story_workspace / "stories" / "inbox").iterdir())
    story_id, entry = next((k, e) for k, e in bot.state.data["stories"].items() if e["status"] == "ready")
    drop_in_inbox(story_workspace / "stories", {"story": story_id, "choice": "drop"})
    bot.studio.run()
    assert entry["status"] == "dropped"


def test_discord_story_reactions(monkeypatch, tmp_path):
    from brainrot_bot.phone import Discord
    from test_phone import jresp

    api = (FakeApi(monkeypatch).on("POST", r"/channels/c1/messages$", response(200, {"id": "m5"}))
           .on("PUT", r"/reactions/", response(204)))
    bot = Discord("tok", "c1")
    assert bot.send_story("abc123", "📝 The Lighthouse", "anime", 9.3, True) == "m5"
    assert "React 🎬 for an AI video (Anime, about $9.30)" in api.find("POST", "/messages")[0].json["content"]
    assert [c.url.split("/reactions/")[1].split("/")[0] for c in api.find("PUT", "reactions")] == [
        "%F0%9F%8E%AC", "%F0%9F%8E%AE", "%F0%9F%97%91"]

    phone = make_phone(tmp_path)
    phone.remember(stories={"abc123": {"style": "anime", "cost": 9.3, "can_ai": True}}, discord_stories={"m5": "abc123"},
                   discord_after="100")
    (FakeApi(monkeypatch)
     .on("GET", r"/users/@me$", response(200, {"id": "bot1"}))
     .on("GET", r"reactions/%F0%9F%8E%AC$", jresp(200, [{"id": "u7"}]))  # you tapped the clapper
     .on("GET", r"reactions/", jresp(200, []))
     .on("GET", r"/channels/c1/messages$", jresp(200, [{"id": "101", "author": {"id": "u7"}, "content": "idea a cursed clock"}]))
     .on("POST", r"/channels/c1/messages$", response(200, {"id": "reply"})))
    bot.poll(phone)
    assert phone.actions.get_nowait() == Action("story", story="abc123", choice="ai", style="anime")
    assert phone.actions.get_nowait() == Action("idea", text="a cursed clock")
    assert phone.memory["discord_stories"] == {} and phone.story_memory("abc123") is None


def test_story_files_move_to_another_drive(tmp_path, monkeypatch):
    """The work folder and the stories folder can be on different drives (Windows can't rename across them)."""
    import os

    from brainrot_bot.stories import studio

    src, dest = tmp_path / "narration.wav", tmp_path / "stories" / "narration.wav"
    src.write_bytes(b"RIFF" * 100)
    dest.parent.mkdir()
    real, calls = os.replace, []

    def replace(a, b):
        calls.append(a)
        if len(calls) == 1:
            raise OSError(17, "The system cannot move the file to a different disk drive")
        real(a, b)

    monkeypatch.setattr(studio.os, "replace", replace)
    studio._move(src, dest)
    assert dest.read_bytes() == b"RIFF" * 100 and not src.exists() and not list(dest.parent.glob("*.partial"))
