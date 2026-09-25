"""bot_state.json: which clips are done or failed, and how often each gameplay recording was used."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("brainrot")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "version": 1,
            "clips": {},
            "gameplay_usage": {},
            "gameplay_cache": {},
            "music_cache": {},
            "uploads": {},
            "upload_blocked": {},
            "last_post": {},
            "links": {},
            "fingerprints": [],
            "update": {},
        }
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                backup = path.with_name(path.stem + ".broken.json")
                shutil.copyfile(path, backup)
                log.warning("%s was unreadable (%s); starting fresh. Old copy saved as %s", path.name, exc, backup.name)
            else:
                for key, value in loaded.items():
                    if key in self.data and isinstance(value, type(self.data[key])):
                        self.data[key] = value

    @property
    def clips(self) -> dict:
        return self.data["clips"]

    @property
    def usage(self) -> dict:
        return self.data["gameplay_usage"]

    @property
    def gameplay_cache(self) -> dict:
        return self.data["gameplay_cache"]

    @property
    def music_cache(self) -> dict:
        return self.data["music_cache"]

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # Windows: file briefly locked by an editor or antivirus
                if attempt == 4:
                    raise
                time.sleep(0.5)

    def retry_failed(self) -> int:
        count = 0
        for entry in self.clips.values():
            if entry.get("status") in ("failed", "retry"):
                entry["status"] = "retry"
                entry["attempts"] = 0
                entry["next_try"] = 0
                count += 1
        return count
