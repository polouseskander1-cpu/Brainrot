"""More than one account per platform, and which clip folder posts where.

Every connected account has an id: the platform ("youtube") for the first one, or platform:name
("youtube:gaming") for more. A clip folder can say which accounts its reels go to with an
accounts.txt file:

    youtube = gaming       # this folder posts to the YouTube account called "gaming"
    tiktok = main
    instagram = off        # never post this folder on Instagram

Folders without one (and platforms not listed) use the first account of each platform.
"""

from __future__ import annotations

import re
from pathlib import Path

ACCOUNTS_FILE = "accounts.txt"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def account_id(platform: str, name: str = "") -> str:
    name = (name or "").strip().lower()
    return platform if not name or name in ("main", "default") else f"{platform}:{name}"


def split_account(account: str) -> tuple[str, str]:
    """'youtube:gaming' -> ('youtube', 'gaming'), 'youtube' -> ('youtube', 'main')."""
    platform, _, name = account.partition(":")
    return platform, name or "main"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name.strip().lower()))


def read_accounts_file(path: Path) -> dict[str, str]:
    """{platform: account name or 'off'}"""
    choices: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return choices
    for line in lines:
        line = line.split("#")[0].strip()
        if "=" not in line and ":" not in line:
            continue
        key, _, value = line.replace(":", "=", 1).partition("=")
        key, value = key.strip().lower(), value.strip().lower()
        if key and value:
            choices[key] = value
    return choices


def folder_choices(folder: Path, clips_root: Path) -> dict[str, str]:
    """The choices for a clip folder: accounts.txt files from the clips folder down to it (the nearest wins)."""
    folder, root = Path(folder), Path(clips_root)
    try:
        rel = folder.relative_to(root)
    except ValueError:
        return read_accounts_file(folder / ACCOUNTS_FILE)
    chosen = dict(read_accounts_file(root / ACCOUNTS_FILE))
    current = root
    for part in rel.parts:
        current = current / part
        chosen.update(read_accounts_file(current / ACCOUNTS_FILE))
    return chosen


def set_folder_account(folder: Path, platform: str, name: str) -> None:
    """Write (or change) one line of folder/accounts.txt."""
    path = Path(folder) / ACCOUNTS_FILE
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if path.exists() else [
        "# Which account each platform posts this folder's reels to (main = the first account, off = don't post)."
    ]
    for i, line in enumerate(lines):
        key = line.split("#")[0].replace(":", "=", 1).partition("=")[0].strip().lower()
        if key == platform:
            lines[i] = f"{platform} = {name}"
            break
    else:
        lines.append(f"{platform} = {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
