"""Console look and feel for the setup page and the menu."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

WIDTH = 66
_color: bool | None = None


def _enable_color() -> bool:
    out = sys.stdout
    if out is None or not hasattr(out, "isatty") or not out.isatty() or os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:  # noqa: BLE001
            return False
    return True


def _paint(text: str, code: str) -> str:
    global _color
    if _color is None:
        _color = _enable_color()
    return f"\033[{code}m{text}\033[0m" if _color else text


def yellow(text: str) -> str:
    return _paint(text, "93")


def green(text: str) -> str:
    return _paint(text, "92")


def red(text: str) -> str:
    return _paint(text, "91")


def cyan(text: str) -> str:
    return _paint(text, "96")


def bold(text: str) -> str:
    return _paint(text, "1")


def dim(text: str) -> str:
    return _paint(text, "2")


def say(text: str = "") -> None:
    print(text, flush=True)


def clear() -> None:
    if sys.stdout is not None and sys.stdout.isatty():
        if os.name == "nt":
            os.system("cls")
        else:
            print("\033[2J\033[H", end="", flush=True)


def banner(subtitle: str = "") -> None:
    clear()
    say(yellow("=" * WIDTH))
    say(bold(yellow("  BRAINROT BOT")) + (dim("  -  " + subtitle) if subtitle else ""))
    say(yellow("=" * WIDTH))


def step(number: int, total: int, title: str) -> None:
    say()
    say(yellow(f"STEP {number} of {total}") + "   " + bold(title))
    say(dim("-" * WIDTH))


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{hint}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_required(prompt: str) -> str:
    while True:
        answer = ask(prompt)
        if answer:
            return answer
        say(red("  This can't be empty (type 'skip' to skip)."))


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{prompt} ({hint})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        say(red("  Please type y or n."))


def choose(options: list[str], default: int = 1, prompt: str = "Your choice") -> int:
    for number, text in enumerate(options, 1):
        say(f"  {yellow(str(number))}) {text}")
    while True:
        answer = ask(prompt, str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say(red(f"  Please type a number from 1 to {len(options)}."))


def clean_path(text: str) -> Path:
    """Accepts paths pasted with quotes (Windows 'Copy as path') or dragged into the window."""
    text = text.strip().strip('"').strip("'").strip()
    return Path(os.path.expandvars(text)).expanduser()


def ask_path(prompt: str, default: Path) -> Path:
    while True:
        path = clean_path(ask(prompt, str(default)))
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()
        except OSError as exc:
            say(red(f"  Can't use that folder ({exc}). Try another one."))


def pause(message: str = "Press Enter to continue...") -> None:
    try:
        input(dim(message))
    except EOFError:
        pass


def open_folder(path: Path) -> None:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 - opens Explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        say(f"  Open this folder yourself: {path}")
