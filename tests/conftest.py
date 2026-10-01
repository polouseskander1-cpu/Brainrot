"""A fake Anthropic API (enough of it for the bot's AI features), so the tests run the real SDK code
without a key or internet."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brainrot_bot import ai as ai_module
from brainrot_bot.stories import script as story_module

# A short story as the AI writes it (4 scenes, 2 characters).
STORY = {
    "title": "The Lighthouse That Blinked Twice",
    "hook": "Nobody lives there. It still blinks.",
    "caption": "Would you have climbed the stairs?",
    "hashtags": ["mystery", "#story", "light house"],
    "voice": "Hushed and tense, slowing down before the reveal.",
    "mood": "eerie",
    "setting": "A rocky northern coast at night in the 1950s, cold blue light and warm amber lamps.",
    "characters": [
        {"name": "Mara", "look": "a 60-year-old keeper with a grey braid and a yellow raincoat"},
        {"name": "Tom", "look": "a tall fisherman with a red beard and a green knitted hat"},
    ],
    "scenes": [
        {"narration": "Every night at nine, the old lighthouse blinked twice.", "visual": "Mara watches the lighthouse from her porch.",
         "camera": "wide shot, static", "sound": "waves and wind", "characters": ["Mara"]},
        {"narration": "Nobody had lived there for forty years, and the door was welded shut after the storm.",
         "visual": "The empty tower in thick fog, its lamp flashing.", "camera": "slow push-in", "sound": "foghorn far away",
         "characters": []},
        {"narration": "Tom laughed at her. Ghosts don't pay for electricity, he said.", "visual": "Tom laughs at the bar.",
         "camera": "close-up", "sound": "a busy harbor bar", "characters": ["Tom", "Mara"]},
        {"narration": "So Mara cut the lock, climbed the stairs and found a note in her own handwriting. Would you have gone up?",
         "visual": "Mara holds a yellowed note at the top of a spiral staircase.", "camera": "low angle, slow tilt up",
         "sound": "creaking metal stairs", "characters": ["Mara", "Nobody"]},
    ],
}


class FakeClaude:
    def __init__(self):
        self.requests: list[dict] = []
        self.moments: list[dict] | None = None  # what "pick moments" answers (None = the first sentences)
        self.copy = {"hook": "He lost everything in one day", "title": "The day he lost it all",
                     "caption": "Would you have made the same choice?", "hashtags": ["money", "story"]}
        self.commentary = {"intro": "Here is why this matters.", "outro": "Honestly, I think he is right. Would you do the same?"}
        self.prefix = "ES:"
        self.story = dict(STORY)
        self.ideas = ["A lighthouse that blinks twice every night, although nobody has lived there for forty years."]
        self.status = 200  # e.g. 401 to test a refused key
        self.stop_reason = "end_turn"
        self.url = ""

    def answer(self, body: dict) -> dict:
        system = body.get("system")
        prompt = body["messages"][0]["content"]
        if system == ai_module.MOMENTS_SYSTEM:
            if self.moments is not None:
                return {"moments": self.moments}
            numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt, re.M)]
            return {"moments": [{"start_sentence": numbers[0], "end_sentence": numbers[-1], "hook": "Watch till the end",
                                 "score": 8, "why": "test"}]}
        if system == ai_module.COPY_SYSTEM:
            return self.copy
        if system == ai_module.COMMENTARY_SYSTEM:
            return self.commentary
        if system == story_module.STORY_SYSTEM:
            return self.story
        if system == story_module.IDEAS_SYSTEM:
            return {"ideas": self.ideas}
        if system == ai_module.TRANSLATE_SYSTEM:
            lines = re.findall(r"^\[(\d+)\] (.*)$", prompt.split("<lines>")[1], re.M)
            return {"lines": [{"n": int(n), "text": f"{self.prefix} {text}"} for n, text in lines]}
        raise AssertionError(f"unexpected system prompt: {system!r}")


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@pytest.fixture
def fake_claude(monkeypatch):
    fake = FakeClaude()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self) -> bool:
            if fake.status == 200:
                return False
            kinds = {401: "authentication_error", 404: "not_found_error", 429: "rate_limit_error", 400: "invalid_request_error"}
            self._json(fake.status, {"type": "error", "error": {"type": kinds.get(fake.status, "api_error"), "message": "fake error"}})
            return True

        def do_GET(self):  # noqa: N802 - models.retrieve (key check)
            fake.requests.append({"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
            if self._error():
                return
            model = self.path.rstrip("/").split("/")[-1]
            self._json(200, {"type": "model", "id": model, "display_name": model, "created_at": "2026-01-01T00:00:00Z"})

        def do_POST(self):  # noqa: N802 - messages (streamed)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake.requests.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": body})
            if self._error():
                return
            text = json.dumps(fake.answer(body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            message = {"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                       "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}
            self.wfile.write(_sse("message_start", {"type": "message_start", "message": message}))
            self.wfile.write(_sse("content_block_start", {"type": "content_block_start", "index": 0,
                                                          "content_block": {"type": "text", "text": ""}}))
            for i in range(0, len(text), 40):
                self.wfile.write(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                              "delta": {"type": "text_delta", "text": text[i:i + 40]}}))
            self.wfile.write(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
            self.wfile.write(_sse("message_delta", {"type": "message_delta",
                                                    "delta": {"stop_reason": fake.stop_reason, "stop_sequence": None},
                                                    "usage": {"output_tokens": 50}}))
            self.wfile.write(_sse("message_stop", {"type": "message_stop"}))
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("BRAINROT_ANTHROPIC_BASE_URL", fake.url)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield fake
    server.shutdown()
    server.server_close()
