"""Setup wizard, config editing, the one-bot-at-a-time lock and start-at-login entries."""

import builtins
import plistlib
import sys
import types
from pathlib import Path

import pytest

from brainrot_bot import service, wizard
from brainrot_bot.config import load_config, update_config_file
from brainrot_bot.credentials import Credentials

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(REPO_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def typed(monkeypatch, *answers):
    """Pretend the user types these answers, one per question."""
    remaining = list(answers)
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        if not remaining:
            raise AssertionError(f"unexpected question: {prompt}")
        return remaining.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return asked


# ---------------------------------------------------------------- config.yaml editing


def test_update_config_keeps_comments_and_other_settings(config):
    before = config.read_text(encoding="utf-8")
    update_config_file(config, {"app": {"run_mode": "window", "autostart": True}, "folders": {"clips": r"C:\Users\Me\Videos\Clips"}})
    after = config.read_text(encoding="utf-8")
    assert "  run_mode: window  # background = keeps working" in after
    assert r"  clips: 'C:\Users\Me\Videos\Clips'" in after
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 3 and len(before.splitlines()) == len(after.splitlines())
    cfg = load_config(config)
    assert cfg.app.run_mode == "window" and cfg.app.autostart is True


def test_update_config_adds_missing_keys_and_sections(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("captions:\n  max_words: 2   # few words\n\nvideo:\n  fps: 60\n", encoding="utf-8")
    update_config_file(path, {"captions": {"font_size": 90}, "upload": {"tiktok": True, "hashtags": "#a #b"}})
    text = path.read_text(encoding="utf-8")
    assert "  max_words: 2   # few words\n  font_size: 90\n\nvideo:" in text
    assert text.endswith("upload:\n  tiktok: true\n  hashtags: '#a #b'\n")
    cfg = load_config(path)
    assert cfg.captions.font_size == 90 and cfg.video.fps == 60 and cfg.upload.hashtags == "#a #b"


# ---------------------------------------------------------------- the setup wizard


def test_first_setup_writes_everything(config, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "set_autostart", lambda enabled, config=None: calls.append(enabled))
    monkeypatch.setattr(wizard.ui, "open_folder", lambda path: calls.append(("open", path)))
    gameplay, clips, reels = tmp_path / "My Gameplay", tmp_path / "Clips", tmp_path / "Reels"
    typed(monkeypatch,
          "2",            # run only while the window is open
          "",             # start at login: default yes on first setup
          "n", "n", "n", "n", "n", "n",  # skip all six platforms
          "n",            # no AI
          "n",            # no phone
          f'"{gameplay}"', str(clips), str(reels),  # quotes from "Copy as path" are fine
          "y")            # open the folders
    cfg = wizard.run_setup(config)
    assert cfg.app.run_mode == "window" and cfg.app.autostart and cfg.app.setup_done
    assert cfg.paths.gameplay == gameplay and cfg.paths.clips == clips and cfg.paths.output == reels
    assert gameplay.is_dir() and clips.is_dir() and reels.is_dir()
    assert cfg.paths.music == tmp_path / "Music"  # the optional music folder follows the gameplay folder
    assert "  gameplay: 'My Gameplay'" in config.read_text(encoding="utf-8")  # inside the app folder: saved relative
    assert calls == [True, ("open", gameplay), ("open", clips)]
    assert not any(getattr(cfg.upload, p) for p in ("youtube", "tiktok", "instagram", "facebook"))


def test_connecting_a_platform_and_retrying_after_an_error(config, monkeypatch):
    monkeypatch.setattr(service, "set_autostart", lambda enabled, config=None: None)
    attempts = []

    def fake_instagram(cfg, creds):
        attempts.append(1)
        if len(attempts) == 1:
            raise wizard.UploadError("token is wrong", retry=False)
        creds.set("instagram", {"access_token": "t", "user_id": "1", "account": "@me"})
        return True

    monkeypatch.setitem(wizard.CONNECTORS, "instagram", fake_instagram)
    # skip YouTube, TikTok; connect Instagram, fail, retry; skip Facebook, X, Pinterest; no extra accounts
    typed(monkeypatch, "n", "n", "y", "y", "", "n", "n", "n")
    cfg = wizard.run_setup(config, only_platforms=True)
    assert cfg.upload.instagram and not cfg.upload.youtube
    assert Credentials(cfg.paths.credentials).get("instagram")["account"] == "@me"
    assert len(attempts) == 2

    # Next time the connected account is offered to keep.
    typed(monkeypatch, "n", "n", "1", "n", "n", "n", "n")
    cfg = wizard.run_setup(config, only_platforms=True)
    assert cfg.upload.instagram


def test_adding_a_second_account_for_some_folders(config, monkeypatch):
    cfg = load_config(config)
    for name in ("Gaming Clips", "Podcast"):
        (cfg.paths.clips / name).mkdir(parents=True)
    connected = []

    def fake_youtube(cfg, creds, account="youtube"):
        connected.append(account)
        creds.set(account, {"refresh_token": "r", "account": "Gaming Channel"})
        return True

    monkeypatch.setitem(wizard.CONNECTORS, "youtube", fake_youtube)
    typed(monkeypatch, "n", "n", "n", "n", "n", "n",  # the six platforms: nothing changes
          "y", "1", "main", "gaming", "1",  # another account: YouTube, 'main' is refused, 'gaming', for folder 1
          "n")
    cfg = wizard.run_setup(config, only_platforms=True)
    assert connected == ["youtube:gaming"]
    assert cfg.upload.youtube
    assert (cfg.paths.clips / "Gaming Clips" / "accounts.txt").read_text().splitlines()[-1] == "youtube = gaming"
    assert not (cfg.paths.clips / "Podcast" / "accounts.txt").exists()


def test_skip_word_skips_a_platform(config, monkeypatch):
    creds = Credentials(load_config(config).paths.credentials)
    typed(monkeypatch, "skip")
    assert wizard.connect_instagram(load_config(config), creds) is False
    assert creds.get("instagram") == {}


# ---------------------------------------------------------------- one bot at a time


def test_instance_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "bot.lock"
    first, second = service.InstanceLock(lock_path), service.InstanceLock(lock_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_is_running_follows_the_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(service.InstanceLock.__init__, "__defaults__", (tmp_path / "bot.lock",))
    assert not service.is_running()
    holder = service.InstanceLock()
    assert holder.acquire()
    assert service.is_running()
    holder.release()
    assert not service.is_running()


# ---------------------------------------------------------------- start at login


def test_autostart_linux_desktop_file(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "login_system", lambda: "linux")
    monkeypatch.setattr(service, "LINUX_AUTOSTART", tmp_path / "autostart" / "brainrot-bot.desktop")
    service.set_autostart(True, config=tmp_path / "my config.yaml")
    text = service.LINUX_AUTOSTART.read_text(encoding="utf-8")
    assert '"--autostart" "--config"' in text
    assert service._desktop_quote(str(tmp_path / "my config.yaml")) in text  # backslashes escaped, as the format requires
    assert service.autostart_enabled()
    service.set_autostart(False)
    assert not service.autostart_enabled()


def test_autostart_macos_launch_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "login_system", lambda: "mac")
    monkeypatch.setattr(service, "MAC_AGENT", tmp_path / "LaunchAgents" / "agent.plist")
    service.set_autostart(True)
    agent = plistlib.loads(service.MAC_AGENT.read_bytes())
    assert agent["RunAtLoad"] is True and agent["ProgramArguments"][-1] == "--autostart"
    service.set_autostart(False)
    assert not service.MAC_AGENT.exists()


def test_autostart_windows_registry(monkeypatch, tmp_path):
    values = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    def delete(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU", REG_SZ=1,
        CreateKey=lambda root, path: Key(), OpenKey=lambda root, path: Key(),
        SetValueEx=lambda key, name, reserved, kind, value: values.__setitem__(name, value),
        QueryValueEx=query, DeleteValue=delete,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert service.windows_run_value() is None
    service.set_windows_run_value('"C:\\Apps\\BrainrotBot-background.exe" --autostart')
    assert service.windows_run_value() == '"C:\\Apps\\BrainrotBot-background.exe" --autostart'
    service.set_windows_run_value(None)
    service.set_windows_run_value(None)  # removing twice is fine
    assert values == {}


def test_packaged_app_starts_the_windowless_exe_at_login(monkeypatch, tmp_path):
    (tmp_path / "BrainrotBot.exe").write_bytes(b"")
    (tmp_path / "BrainrotBot-background.exe").write_bytes(b"")
    monkeypatch.setattr(service, "FROZEN", True)
    monkeypatch.setattr(service, "APP_DIR", tmp_path)
    assert service.app_command(["--autostart"], windowless=True) == [str(tmp_path / "BrainrotBot-background.exe"), "--autostart"]
    assert service.app_command([], windowless=False, config=tmp_path / "other.yaml") == [
        str(tmp_path / "BrainrotBot.exe"), "--config", str((tmp_path / "other.yaml").resolve())]


def test_connecting_the_ai_checks_the_key(config, monkeypatch, fake_claude):
    cfg = load_config(config)
    creds = Credentials(cfg.paths.credentials)
    fake_claude.status = 401
    typed(monkeypatch, "y", "sk-ant-wrong", "y", "skip")  # a refused key, try again, give up
    assert not wizard.connect_ai(cfg, creds) and not creds.get("anthropic")
    fake_claude.status = 200
    typed(monkeypatch, "y", "  sk-ant-good  ")
    assert wizard.connect_ai(cfg, creds)
    assert creds.get("anthropic")["api_key"] == "sk-ant-good"
    typed(monkeypatch, "3")  # disconnect
    assert not wizard.connect_ai(cfg, creds) and not creds.get("anthropic")


def test_adding_a_link_from_the_menu(config, monkeypatch):
    cfg = load_config(config)
    (cfg.paths.clips / "Podcast A").mkdir(parents=True)
    monkeypatch.setattr(wizard.ui, "pause", lambda *a: None)
    typed(monkeypatch, "https://youtu.be/abc", "1")
    wizard.add_video_link(cfg)
    assert (cfg.paths.clips / "Podcast A" / "links.txt").read_text() == "https://youtu.be/abc\n"
    typed(monkeypatch, "https://youtu.be/xyz", "2", "New: Show?")
    wizard.add_video_link(cfg)
    assert (cfg.paths.clips / "New Show" / "links.txt").read_text() == "https://youtu.be/xyz\n"
