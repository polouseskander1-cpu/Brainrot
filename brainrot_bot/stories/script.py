"""The story script: the AI turns an idea into narration split into shots, with characters and a look for
each shot, plus the hook, title, caption and hashtags."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

WORDS_PER_SECOND = 2.5  # narration read at a brisk storytelling pace
# How fast each voice reads a story (words a second at speed 1), so the story is written to the right length:
# the free Piper voices read faster than the AI voices, which pause like a storyteller.
PACE = {"piper": 3.1, "system": 2.7, "gemini": 2.5, "elevenlabs": 2.6}
MAX_CHARACTERS = 3

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "voice": {"type": "string"},
        "mood": {"type": "string"},
        "setting": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "look": {"type": "string"}},
                "required": ["name", "look"],
                "additionalProperties": False,
            },
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "narration": {"type": "string"},
                    "visual": {"type": "string"},
                    "camera": {"type": "string"},
                    "sound": {"type": "string"},
                    "characters": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["narration", "visual", "camera", "sound", "characters"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "hook", "caption", "hashtags", "voice", "mood", "setting", "characters", "scenes"],
    "additionalProperties": False,
}

STORY_SYSTEM = (
    "You are the head writer of a faceless short-video channel that tells stories in about a minute: mysterious "
    "encounters, eerie coincidences, wise little stories and parables, twists and life lessons. You write for the ear "
    "and for the eye: every line makes the viewer need the next one, and every scene is one shot an animator can show."
)

STORY_PROMPT = """Write a story for a vertical short video (TikTok, YouTube Shorts, Instagram Reels).

<idea>
{idea}
</idea>
{source_rule}
Language of everything you write: {language}.

The narration, {words_lo}-{words_hi} words in total (about {seconds} seconds read aloud):
- Opens with a hook in its first sentence (at most 12 words): a strange detail, a high stake or a question. Never "Once upon a time" or "Have you ever wondered".
- Is told in the first person or a close third person, with everyday words, short sentences and concrete details people can picture.
- Raises a question in every scene that the next scene answers, so the tension keeps rising.
- Ends on a twist, a reveal or a line that lands: the lesson is shown, not preached.{question_rule}
- Is plain spoken words only: no hashtags, emojis, stage directions, quotation marks, brackets or headings.

Split it into {scenes_lo}-{scenes_hi} scenes. Each scene is one shot of an animated short film:
- narration: the words said over this shot, 8-20 words (one or two sentences).
- visual: what we see in this single shot, in one or two sentences: who is there, what they do, where, the mood and the light. One simple action a 4-8 second shot can show, no cuts. Describe the characters by name; never show text, signs with writing, screens, letters or numbers.
- camera: one framing and camera move, e.g. "close-up, slow push-in", "wide shot, static", "low angle, slow tilt up".
- sound: the sound of the place in a few words, e.g. "wind and distant waves". Never music or speech.
- characters: the names (from your characters list) of everyone visible in this shot.

characters: at most {max_characters} recurring characters. "look" is a precise description repeated in every shot so they look the same each time: age, build, face, hair, clothes with their colors, and one distinctive detail. Characters are adults, animals or creatures, never children. No real people, celebrities or brands, and nothing from existing films, games or books.
setting: the world of the story in one sentence: the place, the era, the time of day and the colors.

Also write:
- title: a title for YouTube Shorts, at most 70 characters, no hashtags.
- hook: shown on screen in the first seconds, at most 7 words. It makes people stay without giving away the ending.
- caption: one or two short sentences for TikTok, Instagram and Facebook. It can end with a question that invites comments. No hashtags.
- hashtags: 3 to 6 hashtags about the story, without the # sign and without spaces.
- voice: one sentence directing the narrator: the mood and the pace, e.g. "Hushed and tense, slowing down before the reveal."
- mood: one or two words, e.g. "eerie" or "heartwarming"."""

SOURCE_RULES = {
    "reddit": (
        "\nThe idea is a post someone wrote on Reddit (r/{subreddit}). Take only its core idea: write a new, original "
        "story in your own words, with new names, new details and your own scenes. Don't reuse its sentences, phrases or "
        "distinctive details, and don't mention Reddit or the post.\n"
    ),
    "ai": "\nThe idea is your own; make the story original.\n",
    "you": "\nThe idea is from the channel's owner: keep what they ask for (names, facts, the ending) and fill in the rest.\n",
}

IDEAS_SCHEMA = {
    "type": "object",
    "properties": {"ideas": {"type": "array", "items": {"type": "string"}}},
    "required": ["ideas"],
    "additionalProperties": False,
}

IDEAS_SYSTEM = "You come up with ideas for one-minute stories for a faceless short-video channel."

IDEAS_PROMPT = """The channel tells stories about: {topics}

Come up with {count} fresh, original story ideas for it. Each idea is two or three sentences: the situation, what makes it gripping and how it ends (a twist, a reveal or a lesson). Each one different from the others in place, characters and feeling.{avoid}"""


@dataclass
class Scene:
    narration: str
    visual: str
    camera: str = ""
    sound: str = ""
    characters: list[str] = field(default_factory=list)


@dataclass
class Character:
    name: str
    look: str


@dataclass
class Script:
    title: str
    hook: str
    caption: str
    hashtags: list[str]
    voice: str
    mood: str
    setting: str
    characters: list[Character]
    scenes: list[Scene]

    @property
    def narration(self) -> str:
        return " ".join(s.narration for s in self.scenes)

    @property
    def words(self) -> int:
        return len(self.narration.split())

    def seconds(self, words_per_second: float = WORDS_PER_SECOND) -> float:
        """About how long the narration takes to read."""
        return self.words / words_per_second

    def cast(self, names: list[str]) -> list[Character]:
        """The characters of a scene, as they are described (names the AI made up on the spot are ignored)."""
        wanted = {n.strip().lower() for n in names}
        return [c for c in self.characters if c.name.strip().lower() in wanted]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Script":
        return Script(
            title=str(data.get("title", "")), hook=str(data.get("hook", "")), caption=str(data.get("caption", "")),
            hashtags=[str(t) for t in data.get("hashtags") or []], voice=str(data.get("voice", "")),
            mood=str(data.get("mood", "")), setting=str(data.get("setting", "")),
            characters=[Character(str(c.get("name", "")), str(c.get("look", ""))) for c in data.get("characters") or []
                        if isinstance(c, dict)],
            scenes=[Scene(str(s.get("narration", "")), str(s.get("visual", "")), str(s.get("camera", "")),
                          str(s.get("sound", "")), [str(n) for n in s.get("characters") or []])
                    for s in data.get("scenes") or [] if isinstance(s, dict)],
        )


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def _plain(text: str, limit: int = 1000) -> str:
    """Spoken words only: no emojis, hashtags, brackets, stage directions or quotation marks."""
    text = _EMOJI_RE.sub("", str(text or ""))
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", "", text)
    text = re.sub(r"[\[\]{}()*_\"“”«»<>|~^]", "", text)
    return " ".join(text.split())[:limit]


def clean(data: dict) -> Script | None:
    """The AI's answer made safe to use, or None if it isn't a usable story."""
    script = Script.from_dict(data)
    script.title = " ".join(script.title.split())[:100]
    script.hook = " ".join(_plain(script.hook, 80).split()[:9])
    script.caption = script.caption.strip()[:1000]
    script.hashtags = [re.sub(r"[^\w]", "", t.lstrip("#")) for t in script.hashtags if re.sub(r"[^\w]", "", t.lstrip("#"))][:8]
    script.voice = " ".join(script.voice.split())[:300]
    script.mood = " ".join(script.mood.split())[:40]
    script.setting = " ".join(script.setting.split())[:600]
    script.characters = [Character(" ".join(c.name.split())[:40], " ".join(c.look.split())[:600])
                         for c in script.characters if c.name.strip() and c.look.strip()][:MAX_CHARACTERS]
    scenes = []
    for s in script.scenes:
        s.narration = _plain(s.narration, 400)
        s.visual = " ".join(s.visual.split())[:800]
        s.camera = " ".join(s.camera.split())[:120]
        s.sound = " ".join(s.sound.split())[:160]
        if s.narration:
            scenes.append(s)
    script.scenes = scenes[:24]
    if len(script.scenes) < 2 or script.words < 40:
        return None
    return script


def pace(engine: str, speed: float = 1.0) -> float:
    """Words a second the narrator reads (engine: piper, system, gemini or elevenlabs)."""
    return PACE.get(engine, WORDS_PER_SECOND) * (speed if engine != "gemini" else 1.0)


def word_range(seconds: float, words_per_second: float = WORDS_PER_SECOND) -> tuple[int, int]:
    words = seconds * words_per_second
    return round(words * 0.92), round(words * 1.05)


def scene_range(seconds: float) -> tuple[int, int]:
    """About one shot every 6-7 seconds (Veo makes shots of 8 seconds at most)."""
    return max(3, round(seconds / 7.5)), max(4, round(seconds / 5.5))


def story_prompt(idea: str, *, source: str, subreddit: str = "", language: str = "English", seconds: float = 75,
                 question: bool = True, words_per_second: float = WORDS_PER_SECOND) -> str:
    words_lo, words_hi = word_range(seconds, words_per_second)
    scenes_lo, scenes_hi = scene_range(seconds)
    question_rule = (" Then one short question to the viewer that invites comments, e.g. \"Would you have opened the "
                     "door?\"") if question else ""
    return STORY_PROMPT.format(
        idea=idea.strip()[:12000], source_rule=SOURCE_RULES.get(source, SOURCE_RULES["you"]).format(subreddit=subreddit),
        language=language, words_lo=words_lo, words_hi=words_hi, seconds=round(seconds), scenes_lo=scenes_lo,
        scenes_hi=scenes_hi, max_characters=MAX_CHARACTERS, question_rule=question_rule,
    )


def ideas_prompt(topics: str, count: int, avoid: list[str]) -> str:
    recent = [t for t in avoid if t][-30:]
    avoid_text = ("\n\nThe channel already told these stories, so don't repeat them or anything close:\n"
                  + "\n".join(f"- {t}" for t in recent)) if recent else ""
    return IDEAS_PROMPT.format(topics=topics.strip() or "mysterious stories and wise little stories", count=count,
                               avoid=avoid_text)
