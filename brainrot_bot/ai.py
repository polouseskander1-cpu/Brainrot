"""Optional help from Claude (Anthropic API): picking the best moments of long videos, writing the
on-screen hook, the post title / caption / hashtags, and translating captions.

It is only used when an API key is connected (menu > AI). Without one, or if a request fails, the bot
falls back to its built-in rules, so reels never wait for the AI.
"""

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace

log = logging.getLogger("brainrot")

CREDENTIAL_KEY = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
KEY_PAGE = "https://platform.claude.com/settings/keys"

MOMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sentence": {"type": "integer"},
                    "end_sentence": {"type": "integer"},
                    "hook": {"type": "string"},
                    "score": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["start_sentence", "end_sentence", "hook", "score", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hook", "title", "caption", "hashtags"],
    "additionalProperties": False,
}

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["n", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}

MOMENTS_SYSTEM = (
    "You are an experienced short-form video editor. You find the moments of long podcasts, interviews and "
    "talks that work as stand-alone vertical clips on TikTok, Instagram Reels and YouTube Shorts."
)

MOMENTS_PROMPT = """Here is the transcript of a {length} video{source}, split into numbered sentences with their start times.

<transcript>
{transcript}
</transcript>

Pick up to {count} moments that would make the best clips. Each moment:
- is one continuous run of sentences, from start_sentence to end_sentence (both included), about {min_s}-{max_s} seconds long (use the start times to judge the length);
- makes sense on its own to someone who hasn't seen the rest of the video: no unexplained "he", "it" or "that" at the start, no "as I said before";
- grabs attention in the first 3 seconds (a bold claim, a question, a number, the start of a story) and ends on a complete thought or a punchline;
- does not overlap another moment.

Prefer surprising, funny, emotional, useful or controversial moments. Skip intros, outros, sponsor reads and small talk. A few strong moments are better than filling the list with weak ones.

For each moment also write:
- hook: a short title shown on screen during the first seconds, in the language of the video. At most 8 words. It should make people want to see the payoff without giving it away. No hashtags, emojis or quotation marks.
- score: 1-10, how well you expect it to do.
- why: a few words on why it works."""

COPY_SYSTEM = "You write the text for short vertical videos: the on-screen hook, the post title, the caption and the hashtags."

COPY_PROMPT = """Transcript of a {length:.0f}-second clip{source}:

<transcript>
{transcript}
</transcript>

Write, in the language of the transcript:
- hook: shown on screen during the first seconds. At most 8 words. Make people stay to see the payoff without giving it away. No hashtags, emojis or quotation marks.
- title: a YouTube Shorts title, at most 80 characters, no hashtags.
- caption: one or two short sentences for TikTok, Instagram and Facebook. It can end with a question that invites comments. No hashtags.
- hashtags: 3 to 6 hashtags about this clip's topic, without the # sign and without spaces.

Stay true to what is actually said in the clip."""

TRANSLATE_SYSTEM = "You translate subtitles for short vertical videos."

TRANSLATE_PROMPT = """Translate each numbered subtitle line into {language}. Keep the meaning and the tone, and keep it short and natural to read on screen. Keep names as they are.
Return exactly one translation for every line number, in the same order.

<lines>
{lines}
</lines>"""


class AI:
    """One Claude client for the whole bot. `available` is False when no key is connected."""

    def __init__(self, cfg: SimpleNamespace, credentials):
        self.cfg = cfg.ai
        self.credentials = credentials
        self._client = None
        self._client_key = ""
        self._paused_until = 0.0
        self._warned = False

    # ------------------------------------------------------------------ setup

    def api_key(self) -> str:
        stored = self.credentials.get(CREDENTIAL_KEY) or {}
        return str(stored.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()

    @property
    def available(self) -> bool:
        if not self.cfg.enabled or not self.api_key() or time.time() < self._paused_until:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            if not self._warned:
                log.warning("The AI features need the 'anthropic' package (Python 3.10+): pip install -r requirements.txt")
                self._warned = True
            return False
        return True

    def _get_client(self):
        import anthropic

        key = self.api_key()
        if self._client is None or key != self._client_key:
            base_url = os.environ.get("BRAINROT_ANTHROPIC_BASE_URL") or None  # used by the tests
            self._client = anthropic.Anthropic(api_key=key, base_url=base_url, max_retries=3, timeout=600)
            self._client_key = key
        return self._client

    def _request_options(self, effort: str) -> dict:
        model = self.cfg.model
        options: dict = {}
        if model.startswith(("claude-opus-5", "claude-fable-5")):
            # If the model declines a request, Anthropic re-runs it on a fallback model instead of refusing.
            options.update(betas=[FALLBACK_BETA], fallbacks="default")
        if not model.startswith("claude-haiku"):
            options.update(thinking={"type": "adaptive"}, output_config={"effort": effort})
        return options

    # ------------------------------------------------------------------ one request

    def ask(self, system: str, prompt: str, schema: dict, *, effort: str = "medium", max_tokens: int = 16000,
            what: str = "AI request") -> dict | None:
        """Send one request and return the JSON answer, or None if it didn't work (the caller then falls back)."""
        if not self.available:
            return None
        import anthropic

        options = self._request_options(effort)
        output_config = dict(options.pop("output_config", {}))
        output_config["format"] = {"type": "json_schema", "schema": schema}
        started = time.monotonic()
        try:
            client = self._get_client()
            with client.beta.messages.stream(
                model=self.cfg.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,
                **options,
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError:
            self._pause("the API key was refused. Connect a new one in the menu (AI)", hours=24)
            return None
        except anthropic.PermissionDeniedError as exc:
            self._pause(f"the API key isn't allowed to do this ({_short(exc)})", hours=6)
            return None
        except anthropic.NotFoundError:
            self._pause(f"the model '{self.cfg.model}' wasn't found. Check ai.model in config.yaml", hours=6)
            return None
        except anthropic.RateLimitError:
            log.warning("%s: too many requests or no credit left on the Anthropic account; using the built-in rules this time.", what)
            self._paused_until = time.time() + 15 * 60
            return None
        except anthropic.BadRequestError as exc:
            log.warning("%s was refused: %s. Using the built-in rules instead.", what, _short(exc))
            return None
        except anthropic.APIStatusError as exc:
            log.warning("%s failed (Anthropic error %s); using the built-in rules this time.", what, exc.status_code)
            return None
        except anthropic.APIConnectionError:
            log.warning("%s: couldn't reach Anthropic (internet down?); using the built-in rules this time.", what)
            return None

        if message.stop_reason == "refusal":
            log.warning("%s: the AI declined this one; using the built-in rules instead.", what)
            return None
        if message.stop_reason == "max_tokens":
            log.warning("%s: the answer was cut off; using the built-in rules instead.", what)
            return None
        text = next((block.text for block in message.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("%s: unexpected answer; using the built-in rules instead.", what)
            return None
        usage = getattr(message, "usage", None)
        log.info("%s done in %.0fs (%s tokens in, %s out).", what, time.monotonic() - started,
                 getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"))
        return data if isinstance(data, dict) else None

    def _pause(self, reason: str, hours: float) -> None:
        log.error("AI turned off for now: %s.", reason)
        self._paused_until = time.time() + hours * 3600

    def resume(self) -> None:
        """After the key was changed in the menu."""
        self._paused_until = 0.0

    # ------------------------------------------------------------------ the jobs

    def pick_moments(self, transcript: str, length: str, source: str, count: int, min_s: int, max_s: int) -> list[dict] | None:
        data = self.ask(
            MOMENTS_SYSTEM,
            MOMENTS_PROMPT.format(length=length, source=source, transcript=transcript, count=count, min_s=min_s, max_s=max_s),
            MOMENTS_SCHEMA,
            effort="high",
            max_tokens=32000,
            what="Picking the best moments with AI",
        )
        moments = data.get("moments") if data else None
        return moments if isinstance(moments, list) else None

    def write_copy(self, transcript: str, length: float, source: str) -> dict | None:
        data = self.ask(
            COPY_SYSTEM,
            COPY_PROMPT.format(transcript=transcript, length=length, source=source),
            COPY_SCHEMA,
            effort="medium",
            what="Writing the hook and caption with AI",
        )
        if not data:
            return None
        return {
            "hook": " ".join(str(data.get("hook", "")).split())[:80],
            "title": " ".join(str(data.get("title", "")).split())[:100],
            "caption": str(data.get("caption", "")).strip()[:1000],
            "hashtags": [str(tag) for tag in data.get("hashtags", []) if str(tag).strip()][:8],
        }

    def translate(self, lines: list[str], language: str) -> list[str] | None:
        numbered = "\n".join(f"[{n}] {text}" for n, text in enumerate(lines))
        data = self.ask(
            TRANSLATE_SYSTEM,
            TRANSLATE_PROMPT.format(language=language, lines=numbered),
            TRANSLATE_SCHEMA,
            effort="medium",
            what=f"Translating the captions into {language}",
        )
        if not data or not isinstance(data.get("lines"), list):
            return None
        out = list(lines)
        found = 0
        for item in data["lines"]:
            try:
                n, text = int(item["n"]), " ".join(str(item["text"]).split())
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= n < len(out) and text:
                out[n] = text
                found += 1
        if found < len(lines) * 0.8:
            log.warning("The translation into %s was incomplete; skipping it.", language)
            return None
        return out


def check_key(key: str, model: str) -> str:
    """'' if the key works (and can use the model), otherwise what's wrong. Costs nothing."""
    try:
        import anthropic
    except ImportError:
        return "the 'anthropic' package is missing (Python 3.10+ needed): pip install -r requirements.txt"
    base_url = os.environ.get("BRAINROT_ANTHROPIC_BASE_URL") or None
    try:
        anthropic.Anthropic(api_key=key, base_url=base_url, max_retries=1, timeout=30).models.retrieve(model)
    except anthropic.AuthenticationError:
        return "this key was refused (copy it again, it starts with sk-ant-)"
    except anthropic.PermissionDeniedError:
        return "this key isn't allowed to use the API"
    except anthropic.NotFoundError:
        return f"the key works, but the model '{model}' wasn't found (check ai.model in config.yaml)"
    except anthropic.APIConnectionError:
        return "couldn't reach Anthropic (check the internet connection)"
    except anthropic.APIStatusError as exc:
        return f"Anthropic answered with error {exc.status_code}"
    return ""


def _short(exc: Exception) -> str:
    message = getattr(exc, "message", "") or str(exc)
    return " ".join(str(message).split())[:200]
