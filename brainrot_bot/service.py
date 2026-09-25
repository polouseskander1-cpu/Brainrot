"""Running the bot in the background, stopping it, and starting it when you log in."""

from __future__ import annotations

import logging
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import APP_DIR, DEFAULT_CONFIG_PATH, FROZEN, LOCK_FILE, PID_FILE, STOP_FILE

log = logging.getLogger("brainrot")

APP_EXE = "BrainrotBot.exe"
BACKGROUND_EXE = "BrainrotBot-background.exe"  # same app, but never opens a window
AUTOSTART_NAME = "BrainrotBot"
MAC_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.brainrotbot.autostart.plist"
LINUX_AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "brainrot-bot.desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


# ---------------------------------------------------------------- one bot at a time


class InstanceLock:
    """Held by whichever process is running the bot. The OS drops it automatically if that process dies."""

    def __init__(self, path: Path = LOCK_FILE):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._file.close()
        self._file = None


def is_running() -> bool:
    probe = InstanceLock()
    if probe.acquire():
        probe.release()
        return False
    return True


def write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")


def running_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- commands


def _python(windowless: bool) -> str:
    python = Path(sys.executable)
    if windowless and os.name == "nt":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(python)


def app_command(extra: list[str], *, windowless: bool, config: Path | None = None) -> list[str]:
    """How to start this app again: the .exe when packaged, or python brainrot.py."""
    if config is not None and Path(config).resolve() != DEFAULT_CONFIG_PATH.resolve():
        extra = [*extra, "--config", str(Path(config).resolve())]
    if FROZEN:
        exe = APP_DIR / (BACKGROUND_EXE if windowless else APP_EXE)
        return [str(exe if exe.exists() else Path(sys.executable)), *extra]
    return [_python(windowless), str(APP_DIR / "brainrot.py"), *extra]


def _spawn(cmd: list[str], *, new_console: bool = False) -> None:
    kwargs: dict = {"cwd": str(APP_DIR), "close_fds": True}
    if not new_console:
        kwargs.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        flags = CREATE_NEW_CONSOLE if new_console else DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:  # leave this window's job, so closing the window doesn't take the bot with it
            subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            return
        except OSError:
            pass
        subprocess.Popen(cmd, creationflags=flags, **kwargs)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)


def start_background(config: Path | None = None, wait: float = 20) -> bool:
    """Start the bot as a background process that keeps running after this window closes."""
    if is_running():
        return True
    STOP_FILE.unlink(missing_ok=True)
    _spawn(app_command(["--background"], windowless=True, config=config))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(0.3)
    return False


def open_window(config: Path | None = None) -> None:
    """Open the app in a new console window (Windows)."""
    _spawn(app_command([], windowless=False, config=config), new_console=True)


def stop_background(timeout: float = 60) -> bool:
    """Ask the running bot to stop (it finishes cleanly), force it if it doesn't listen."""
    if not is_running():
        return True
    STOP_FILE.touch()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not is_running():
                return True
            time.sleep(0.5)
        pid = running_pid()
        if pid:
            _kill_tree(pid)
            time.sleep(1)
        return not is_running()
    finally:
        STOP_FILE.unlink(missing_ok=True)


def _kill_tree(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def kill_children_on_exit() -> None:
    """Windows: if the bot process dies (even forcibly), its ffmpeg helpers die with it."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        info = ExtendedLimits()
        kill_on_close, breakaway_ok = 0x2000, 0x0800
        info.BasicLimitInformation.LimitFlags = kill_on_close | breakaway_ok
        extended_limit_information = 9
        kernel32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info))
        kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
        globals()["_job_handle"] = job  # must stay open for the life of the process
    except Exception as exc:  # noqa: BLE001 - nice to have only
        log.debug("Couldn't set up child cleanup: %s", exc)


# ---------------------------------------------------------------- start at login


def _desktop_quote(arg: str) -> str:
    return '"' + "".join("\\" + ch if ch in '"`$\\' else ch for ch in arg) + '"'


def autostart_enabled() -> bool:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return MAC_AGENT.exists()
    return LINUX_AUTOSTART.exists()


def set_autostart(enabled: bool, config: Path | None = None) -> None:
    """Start the app (windowless) when you log in; it waits app.autostart_delay seconds, then runs."""
    cmd = app_command(["--autostart"], windowless=True, config=config)
    if os.name == "nt":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        if enabled:
            MAC_AGENT.parent.mkdir(parents=True, exist_ok=True)
            MAC_AGENT.write_bytes(plistlib.dumps({
                "Label": "com.brainrotbot.autostart",
                "ProgramArguments": cmd,
                "WorkingDirectory": str(APP_DIR),
                "RunAtLoad": True,
            }))
        else:
            MAC_AGENT.unlink(missing_ok=True)
    else:
        if enabled:
            LINUX_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            LINUX_AUTOSTART.write_text(
                "[Desktop Entry]\nType=Application\nName=Brainrot Bot\nComment=Turns clips into reels\n"
                f"Exec={' '.join(_desktop_quote(a) for a in cmd)}\nPath={APP_DIR}\nTerminal=false\nNoDisplay=true\n"
                "X-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        else:
            LINUX_AUTOSTART.unlink(missing_ok=True)
