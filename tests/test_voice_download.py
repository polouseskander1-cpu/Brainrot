"""The real voice: downloads Piper and the voice (about 85 MB) and speaks a line. It needs the internet, so it
only runs when BRAINROT_NETWORK_TESTS=1 (the CI sets it); the other voiceover tests use a stand-in voice."""

import os
import wave

import pytest

from brainrot_bot.media import find_tools
from brainrot_bot.voice import Voice, piper_build

pytestmark = pytest.mark.skipif(os.environ.get("BRAINROT_NETWORK_TESTS") != "1" or not piper_build(),
                                reason="downloads the voice; set BRAINROT_NETWORK_TESTS=1")


def test_the_real_voice_downloads_and_speaks(tmp_path):
    voice = Voice("norman", 1.1, find_tools(), tmp_path)
    out = voice.speak("Would you move to Mars? Tell me in the comments.", "en", tmp_path / "line.wav")
    with wave.open(str(out)) as handle:
        seconds = handle.getnframes() / handle.getframerate()
    assert 1.5 < seconds < 6
    assert voice.piper_ready() and voice.engine_for("en") == "piper"  # the natural voice, not the fallback
    again = voice.speak("Second line.", "en", tmp_path / "again.wav")  # no second download
    assert again.stat().st_size > 1000 and not list(tmp_path.glob("**/*.part"))
