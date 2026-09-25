"""Graphics card rendering, cloud folders, updates and the tray icon."""

import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from brainrot_bot import cloud, gpu, updater
from brainrot_bot.config import DEFAULTS
from brainrot_bot.media import Tools
from test_uploads import FakeApi, response


def video_cfg(**overrides):
    values = dict(DEFAULTS["video"])
    values.update(overrides)
    return SimpleNamespace(**values)


# ------------------------------------------------------------------ graphics card


def test_auto_codec_picks_the_first_encoder_that_works(monkeypatch):
    tools = Tools("ffmpeg", "ffprobe", encoders={"libx264", "h264_nvenc", "h264_qsv"})
    monkeypatch.setattr(gpu, "candidates", lambda: ["h264_nvenc", "h264_qsv", "h264_amf"])
    tried = []
    monkeypatch.setattr(gpu, "works", lambda tools, codec, video: tried.append(codec) or codec == "h264_qsv")
    assert gpu.pick_codec(tools, video_cfg()) == "h264_qsv"
    assert tried == ["h264_nvenc", "h264_qsv"]  # h264_amf isn't in this ffmpeg
    assert gpu.pick_codec(tools, video_cfg(codec="libx265")) == "libx265"  # a fixed choice is kept
    monkeypatch.setattr(gpu, "works", lambda tools, codec, video: False)
    assert gpu.pick_codec(tools, video_cfg()) == "libx264"


def test_intel_encoder_gets_its_pixel_format():
    from test_editing import full_cfg, make_job
    from brainrot_bot.render import build_command

    cfg = full_cfg()
    cfg.video.resolved_codec = "h264_qsv"
    args = build_command(make_job(), cfg)
    assert args[args.index("-pix_fmt") + 1] == "nv12" and args[args.index("-c:v") + 1] == "h264_qsv"
    cfg.video.resolved_codec = ""
    cfg.video.codec = "auto"  # not detected: the CPU
    args = build_command(make_job(), cfg)
    assert args[args.index("-c:v") + 1] == "libx264" and args[args.index("-pix_fmt") + 1] == "yuv420p"


def test_real_detection_falls_back_to_the_cpu_here():
    from brainrot_bot.media import MediaError, find_tools

    try:
        tools = find_tools()
    except MediaError:
        pytest.skip("needs ffmpeg")
    codec = gpu.pick_codec(tools, video_cfg())
    assert codec == "libx264" or gpu.works(tools, codec, video_cfg())


# ------------------------------------------------------------------ cloud folders


def test_synced_folders_are_found(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "My Drive").mkdir(parents=True)
    dropbox = tmp_path / "Dropbox (Personal)"
    dropbox.mkdir()
    (home / ".dropbox").mkdir()
    (home / ".dropbox" / "info.json").write_text(json.dumps({"personal": {"path": str(dropbox)}}))
    (tmp_path / "OneDrive").mkdir()
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nothing"))
    found = cloud.synced_folders(home)
    assert found == {"Google Drive": home / "My Drive", "Dropbox": dropbox, "OneDrive": tmp_path / "OneDrive"}
    monkeypatch.delenv("OneDrive")
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    assert cloud.synced_folders(tmp_path / "empty") == {}


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a fake rclone")
def test_rclone_sync_pulls_clips_and_pushes_reels(tmp_path, monkeypatch):
    log = tmp_path / "calls.txt"
    fake = tmp_path / "bin" / "rclone"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    cfg = SimpleNamespace(cloud=SimpleNamespace(remote="gdrive:Brainrot/", every_minutes=5),
                          paths=SimpleNamespace(clips=tmp_path / "c", gameplay=tmp_path / "g", output=tmp_path / "o"))
    sync = cloud.CloudSync(cfg)
    assert sync.run()
    calls = log.read_text().splitlines()
    assert [c.split()[:3] for c in calls] == [["copy", "gdrive:Brainrot/Clips", str(tmp_path / "c")],
                                              ["copy", "gdrive:Brainrot/Gameplay", str(tmp_path / "g")],
                                              ["copy", str(tmp_path / "o"), "gdrive:Brainrot/Reels"]]
    assert all("--ignore-existing" in c for c in calls)
    assert sync.run() and len(log.read_text().splitlines()) == 3  # not again until every_minutes passed


def test_cloud_remote_setting(tmp_path):
    from brainrot_bot.config import ConfigError, load_config

    path = tmp_path / "config.yaml"
    path.write_text("cloud:\n  remote: gdrive:Brainrot\n")
    assert load_config(path).cloud.remote == "gdrive:Brainrot"
    path.write_text("cloud:\n  remote: just-a-folder\n")
    with pytest.raises(ConfigError, match="rclone remote"):
        load_config(path)


# ------------------------------------------------------------------ updates


def release_json(version="9.9.9", digest=None, size=None, data=b""):
    return {"tag_name": f"v{version}", "draft": False, "prerelease": False, "body": "New stuff", "html_url": "https://github.com/x/y/releases/v9",
            "assets": [{"name": "BrainrotBot-windows.zip", "browser_download_url": "https://example.com/app.zip",
                        "size": len(data) if size is None else size, "digest": digest}]}


def test_versions():
    assert updater.newer("1.2.0", "1.1.9") and updater.newer("v1.10.0", "1.9.0")
    assert not updater.newer("1.1.0", "1.1.0") and not updater.newer("0.9", "1.0")


def test_latest_release_and_daily_check(monkeypatch):
    api = FakeApi(monkeypatch).on("GET", "releases/latest", response(200, release_json("9.9.9", "sha256:ab")))
    state = {}
    found = updater.available_update(state)
    assert found.version == "9.9.9" and found.digest == "sha256:ab" and found.url.endswith("app.zip")
    assert updater.available_update(state).version == "9.9.9" and len(api.calls) == 1  # remembered for a day
    FakeApi(monkeypatch).on("GET", "releases/latest", response(200, release_json("0.0.1")))
    assert updater.available_update({}) is None  # older than this one
    FakeApi(monkeypatch).on("GET", "releases/latest", response(404, {}))
    assert updater.latest_release() is None


def make_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BrainrotBot/BrainrotBot.exe", b"exe")
        archive.writestr("BrainrotBot/_internal/base_library.zip", b"lib")
    return buffer.getvalue()


def test_download_checks_the_file_and_unpacks(tmp_path, monkeypatch):
    data = make_zip()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=60: Resp(data))
    good = updater.Release("9.9.9", "https://example.com/app.zip", len(data), "sha256:" + hashlib.sha256(data).hexdigest(), "", "")
    zip_path = updater.download(good, tmp_path)
    app = updater.unpack(zip_path, tmp_path)
    assert (app / "BrainrotBot.exe").read_bytes() == b"exe" and (app / "_internal").is_dir()
    bad = updater.Release("9.9.9", "https://example.com/app.zip", len(data), "sha256:" + "0" * 64, "", "")
    with pytest.raises(RuntimeError, match="checksum"):
        updater.download(bad, tmp_path)
    short = updater.Release("9.9.9", "https://example.com/app.zip", len(data) + 5, "", "", "")
    with pytest.raises(RuntimeError, match="incomplete"):
        updater.download(short, tmp_path)


def test_unpack_refuses_files_outside_the_folder(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../outside.txt", b"x")
    with pytest.raises(RuntimeError, match="unexpected"):
        updater.unpack(evil, tmp_path / "u")


def test_install_script_keeps_the_old_version_if_copying_fails():
    script = updater.install_script(Path(r"C:\A\.update\new\BrainrotBot"), Path(r"C:\A"), 4242, [r"C:\A\BrainrotBot.exe"])
    assert 'tasklist /FI "PID eq 4242"' in script
    assert 'move "_internal" "_internal.old"' in script and 'move "_internal.old" "_internal"' in script
    assert 'robocopy "C:\\A\\.update\\new\\BrainrotBot" "C:\\A" /E' in script
    assert script.strip().splitlines()[-2] == 'start "" C:\\A\\BrainrotBot.exe'
    assert "config.yaml" not in script  # settings are never touched


# ------------------------------------------------------------------ tray icon


def test_tray_icon_picture():
    pytest.importorskip("PIL")
    from brainrot_bot.tray import icon_image, start_tray

    image = icon_image(64)
    assert image.size == (64, 64) and image.getpixel((32, 4))[:3] == (255, 212, 0)
    cfg = SimpleNamespace(app=SimpleNamespace(tray=False))
    assert start_tray(cfg, dashboard_url=str, open_app=int, open_folder=print, is_paused=bool, toggle_pause=int, stop=int) is None
    if not sys.platform.startswith("win"):
        cfg.app.tray = True
        assert start_tray(cfg, dashboard_url=str, open_app=int, open_folder=print, is_paused=bool, toggle_pause=int, stop=int) is None
