import json
import logging

import pytest

from brainrot_bot.config import ConfigError, load_config
from brainrot_bot.state import State


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_config_loads_with_defaults():
    cfg = load_config()
    assert cfg.video.width == 1080 and cfg.video.height == 1920
    assert cfg.captions.color == "#FFD400" and cfg.captions.stroke_color == "#FFFFFF"


def test_overrides_and_relative_folders(tmp_path):
    cfg = load_config(write(tmp_path, "folders:\n  clips: my clips\ncaptions:\n  max_words: 1\n  color: 00ff00\nvideo:\n  width: 721\n"))
    assert cfg.paths.clips == (tmp_path / "my clips").resolve()
    assert cfg.captions.max_words == 1 and cfg.captions.color == "#00FF00"
    assert cfg.video.width == 720  # rounded down to an even number
    assert cfg.paths.state_file == tmp_path / "bot_state.json"


@pytest.mark.parametrize(
    "text",
    [
        "captions:\n  color: yellow\n",
        "video:\n  top_ratio: 2\n",
        "video:\n  bitrate: 12 Mbps\n",
        "captions:\n  animation: spin\n",
        "parts:\n  max_seconds: 5\n",
        "captions: 3\n",
        "- just a list\n",
    ],
)
def test_bad_values_are_explained(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_unknown_setting_only_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="brainrot"):
        cfg = load_config(write(tmp_path, "captions:\n  colour: '#FFFFFF'\n"))
    assert cfg.captions.color == "#FFD400"
    assert "captions.colour" in caplog.text


def test_state_roundtrip_and_retry_failed(tmp_path):
    path = tmp_path / "bot_state.json"
    state = State(path)
    state.clips["a/b.mp4"] = {"sig": [1, 2], "status": "failed", "attempts": 3}
    state.usage["g.mp4"] = 2
    state.save()
    again = State(path)
    assert again.clips["a/b.mp4"]["status"] == "failed" and again.usage == {"g.mp4": 2}
    assert again.retry_failed() == 1
    assert again.clips["a/b.mp4"]["attempts"] == 0 and again.clips["a/b.mp4"]["status"] == "retry"


def test_broken_state_file_is_kept_aside(tmp_path):
    path = tmp_path / "bot_state.json"
    path.write_text("{not json", encoding="utf-8")
    state = State(path)
    assert state.clips == {}
    assert (tmp_path / "bot_state.broken.json").exists()
    state.save()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
