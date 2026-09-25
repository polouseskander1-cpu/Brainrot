"""The posting queue: every finished reel is posted to each connected platform, spaced out over time."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from ..media import StopRequested
from . import facebook, instagram, tiktok, youtube
from .http import UploadError
from .post import PostInfo

log = logging.getLogger("brainrot")

PLATFORMS = {m.KEY: m for m in (youtube, tiktok, instagram, facebook)}
BACKOFF_MINUTES = [10, 30, 120, 360, 720]
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
REMIND_EVERY = 6 * 3600


def platform_name(key: str) -> str:
    return PLATFORMS[key].NAME


class UploadQueue:
    def __init__(self, cfg: SimpleNamespace, state, credentials, save: Callable[[], None]):
        self.cfg = cfg
        self.state = state
        self.credentials = credentials
        self.save = save
        self._reminded: dict[str, float] = {}

    @property
    def items(self) -> dict:
        return self.state.data.setdefault("uploads", {})

    @property
    def blocked(self) -> dict:
        return self.state.data.setdefault("upload_blocked", {})

    @property
    def last_post(self) -> dict:
        return self.state.data.setdefault("last_post", {})

    def active_platforms(self) -> list[str]:
        """Switched on in config.yaml and connected."""
        return [key for key in PLATFORMS if getattr(self.cfg.upload, key, False) and self.credentials.get(key)]

    def add(self, video: Path, post: PostInfo) -> list[str]:
        added = []
        for key in self.active_platforms():
            self.items[f"{key}|{video}"] = {
                "platform": key,
                "video": str(video),
                "post": post.to_dict(),
                "status": "pending",
                "attempts": 0,
                "next_try": 0,
                "added": time.time(),
            }
            added.append(platform_name(key))
        return added

    def pending(self, key: str | None = None) -> list[dict]:
        return sorted(
            (item for item in self.items.values() if item["status"] == "pending" and (key is None or item["platform"] == key)),
            key=lambda item: item["added"],
        )

    def run_due(self, should_stop: Callable[[], bool] = lambda: False) -> int:
        """Post at most one reel per platform, if that platform is due. Returns how many were posted."""
        posted = 0
        now = time.time()
        self.credentials.reload_if_changed()
        for key in self.active_platforms():
            if should_stop():
                break
            if key in self.blocked:
                if now - self._reminded.get(key, 0) > REMIND_EVERY:
                    log.warning("%s: %s. Open Brainrot Bot > Connect accounts to log in again.", platform_name(key), self.blocked[key])
                    self._reminded[key] = now
                continue
            spacing = self.cfg.upload.hours_between_posts * 3600
            if now - float(self.last_post.get(key, 0)) < spacing:
                continue
            item = next((i for i in self.pending(key) if i["next_try"] <= now), None)
            if item is None:
                continue
            if self._post(key, item, should_stop):
                posted += 1
        return posted

    def _post(self, key: str, item: dict, should_stop: Callable[[], bool]) -> bool:
        video = Path(item["video"])
        name = platform_name(key)
        if not video.exists():
            item.update(status="failed", error="the reel file was deleted or moved")
            self.save()
            return False
        post = PostInfo.from_dict(item["post"])
        log.info("Posting %s to %s...", video.name, name)
        try:
            result = PLATFORMS[key].upload(video, post, self.cfg, self.credentials, should_stop)
        except StopRequested:
            raise
        except UploadError as exc:
            self._failed(key, item, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - unexpected answers: treat as temporary
            self._failed(key, item, UploadError(f"unexpected problem: {exc}"))
            return False
        item.update(status="done", result=result, posted=time.time())
        item.pop("error", None)
        self.last_post[key] = time.time()
        self.save()
        log.info("%s: %s", name, result)
        return True

    def _failed(self, key: str, item: dict, exc: UploadError) -> None:
        name = platform_name(key)
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["error"] = str(exc)
        if exc.relogin:
            self.blocked[key] = str(exc)
            log.error("%s: %s. Open Brainrot Bot > Connect accounts to log in again. Reels wait until then.", name, exc)
        elif exc.retry and item["attempts"] < MAX_ATTEMPTS:
            minutes = exc.wait_hours * 60 if exc.wait_hours else BACKOFF_MINUTES[item["attempts"] - 1]
            item["next_try"] = time.time() + minutes * 60
            log.warning("%s: couldn't post %s (%s). Trying again in %d min.", name, Path(item["video"]).name, exc, minutes)
        else:
            item["status"] = "failed"
            log.error("%s: giving up on %s: %s", name, Path(item["video"]).name, exc)
        self.save()

    def reconnected(self, key: str) -> None:
        """Called after the user logs in again: unblock and retry right away."""
        self.blocked.pop(key, None)
        for item in self.items.values():
            if item["platform"] == key and item["status"] == "pending":
                item["next_try"] = 0
        self.save()

    def summary(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for item in self.items.values():
            counts.setdefault(item["platform"], {}).setdefault(item["status"], 0)
            counts[item["platform"]][item["status"]] += 1
        return counts
