"""--selftest: checks the parts that can only be tested on the real machine (used by the Windows build)."""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

from . import __version__, service
from .config import FROZEN, update_config_file
from .credentials import Credentials
from .media import find_tools


def _check(name: str, func) -> bool:
    try:
        detail = func()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        traceback.print_exc()
        return False
    print(f"ok    {name}" + (f" ({detail})" if detail else ""))
    return True


def _tools():
    tools = find_tools()
    assert "ass" in tools.filters, "ffmpeg has no libass (captions)"
    assert "libx264" in tools.encoders, "ffmpeg has no libx264"
    return tools.ffmpeg


def _speech_engine():
    import ctranslate2
    import faster_whisper  # noqa: F401

    types = ctranslate2.get_supported_compute_types("cpu")
    assert types, "no CPU compute types"
    assert Path(faster_whisper.__file__).parent.joinpath("assets").is_dir(), "voice-activity model missing"
    return ", ".join(sorted(types))


def _editing():
    import numpy as np

    from .analysis import EMOJI_WORDS, emoji_path
    from .faces import FaceFinder
    from .sfx import SoundEvent, write_track

    missing = [code for code in EMOJI_WORDS if not emoji_path(code).exists()]
    assert not missing, f"emoji pictures missing: {missing}"
    finder = FaceFinder()
    assert finder.detect(np.zeros((240, 320, 3), dtype=np.uint8)) == [], "face found in an empty picture"
    with tempfile.TemporaryDirectory() as tmp:
        track = write_track([SoundEvent("whoosh", 0.1), SoundEvent("boom", 0.5), SoundEvent("bleep", 1.0, 0.3)], 2.0, Path(tmp) / "sfx.wav")
        assert track.stat().st_size > 300_000, "sound effects track too small"
    return f"{len(EMOJI_WORDS)} emojis, face model, sound effects"


def _credentials():
    with tempfile.TemporaryDirectory() as tmp:
        store = Credentials(Path(tmp) / "credentials")
        store.set("demo", {"token": "secret-value-123"})
        raw = store.path.read_bytes()
        if os.name == "nt":
            assert b"secret-value-123" not in raw, "credentials were not encrypted"
        again = Credentials(Path(tmp) / "credentials")
        assert again.get("demo") == {"token": "secret-value-123"}
        return store.path.suffix


def _autostart():
    before = service.autostart_enabled()
    try:
        service.set_autostart(True)
        assert service.autostart_enabled(), "entry not created"
        if os.name == "nt":
            value = service.windows_run_value() or ""
            assert "--autostart" in value, value
            if FROZEN:
                assert service.BACKGROUND_EXE in value, value
        service.set_autostart(False)
        assert not service.autostart_enabled(), "entry not removed"
    finally:
        service.set_autostart(before)
    return "restored to " + ("on" if before else "off")


def _background():
    if service.is_running():
        return "skipped: the bot is already running"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        config = base / "config.yaml"
        update_config_file(config, {
            "folders": {"clips": str(base / "clips"), "gameplay": str(base / "gameplay"), "music": str(base / "music"), "output": str(base / "out")},
            "captions": {"enabled": False},
            "watch": {"poll_seconds": 1},
            "app": {"setup_done": True},
        })
        assert service.start_background(config, wait=60), "background bot didn't start"
        pid = service.running_pid()
        assert service.stop_background(timeout=60), "background bot didn't stop"
        assert not service.is_running()
        return f"pid {pid}"


def _job_object():
    service.kill_children_on_exit()
    return "set" if os.name == "nt" else "not needed here"


def run() -> int:
    print(f"Brainrot bot {__version__} self-test ({'packaged app' if FROZEN else 'source'}, {sys.platform})")
    results = [
        _check("ffmpeg with captions + x264", _tools),
        _check("speech-to-text engine", _speech_engine),
        _check("editing (emojis, faces, sounds)", _editing),
        _check("credential storage", _credentials),
        _check("start at login", _autostart),
        _check("background start/stop", _background),
        _check("child process cleanup", _job_object),
    ]
    print("SELFTEST PASSED" if all(results) else "SELFTEST FAILED")
    return 0 if all(results) else 1
