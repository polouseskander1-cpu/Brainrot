"""The posting queue: every finished reel is posted to each connected account, spaced out over time
and at the best times of day, then its views and likes are checked now and then."""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from ..media import StopRequested
from . import facebook, instagram, pinterest, tiktok, x, youtube
from .accounts import account_id, folder_choices, split_account
from .http import UploadError
from .post import Posted, PostInfo

log = logging.getLogger("brainrot")

PLATFORMS = {m.KEY: m for m in (youtube, tiktok, instagram, facebook, x, pinterest)}
BACKOFF_MINUTES = [10, 30, 120, 360, 720]
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
REMIND_EVERY = 6 * 3600
# When to read a post's views and likes again (hours after posting).
STATS_AGES = [2, 24, 72, 168, 720]
STATS_AGES_PAID = {"x": [24, 168]}  # X charges for every read
DEFAULT_TIMES = ["12:00", "17:00", "20:00"]  # before there are enough stats to learn from
LEARN_AFTER = 8  # posts with stats needed before the best hours are learned


def platform_name(key: str) -> str:
    platform, name = split_account(key)
    base = PLATFORMS[platform].NAME
    return base if name == "main" else f"{base} ({name})"


def parse_times(values) -> list[tuple[int, int]]:
    slots = []
    for value in values or []:
        try:
            hours, minutes = str(value).strip().split(":")
            slot = (int(hours), int(minutes))
        except ValueError:
            continue
        if 0 <= slot[0] < 24 and 0 <= slot[1] < 60:
            slots.append(slot)
    return sorted(set(slots))


def next_slot(after: float, slots: list[tuple[int, int]]) -> float:
    """The first posting time (local clock) after the moment `after`."""
    start = dt.datetime.fromtimestamp(after)
    for day in range(0, 3):
        date = (start + dt.timedelta(days=day)).date()
        for hours, minutes in slots:
            moment = dt.datetime.combine(date, dt.time(hours, minutes))
            if moment.timestamp() > after:
                return moment.timestamp()
    return after + 86400


class UploadQueue:
    def __init__(self, cfg: SimpleNamespace, state, credentials, save: Callable[[], None]):
        self.cfg = cfg
        self.state = state
        self.credentials = credentials
        self.save = save
        self._reminded: dict[str, float] = {}
        self._warned: set[str] = set()
        self.approval_needed: Callable[[], bool] = lambda: False  # set by the phone approval feature
        self.on_waiting: Callable[[str, dict], None] = lambda key, item: None  # called for items waiting for approval
        self.on_posted: Callable[[dict], None] = lambda item: None
        self.on_problem: Callable[[str, str], None] = lambda text, key: None  # e.g. a login that ran out

    @property
    def items(self) -> dict:
        return self.state.data.setdefault("uploads", {})

    @property
    def blocked(self) -> dict:
        return self.state.data.setdefault("upload_blocked", {})

    @property
    def last_post(self) -> dict:
        return self.state.data.setdefault("last_post", {})

    # ------------------------------------------------------------------ where posts go

    def connected_accounts(self) -> list[str]:
        """Every connected account of the platforms switched on in config.yaml."""
        found = []
        for account in sorted(self.credentials.data):
            platform, _ = split_account(account)
            if platform in PLATFORMS and getattr(self.cfg.upload, platform, False) and self.credentials.get(account):
                found.append(account)
        return found

    def active_platforms(self) -> list[str]:
        return [a for a in self.connected_accounts() if ":" not in a]

    def accounts_for(self, folder: Path | None) -> list[str]:
        """The accounts a reel from this clip folder is posted to (see accounts.txt)."""
        choices = folder_choices(folder, self.cfg.paths.clips) if folder is not None and hasattr(self.cfg, "paths") else {}
        targets = []
        for platform in PLATFORMS:
            if not getattr(self.cfg.upload, platform, False):
                continue
            choice = choices.get(platform, "")
            if choice in ("off", "none", "no", "false"):
                continue
            account = account_id(platform, choice)
            if self.credentials.get(account):
                targets.append(account)
            elif account != platform and account not in self._warned:
                log.warning("accounts.txt in %s asks for %s, which isn't connected (menu > Connect accounts).",
                            folder, platform_name(account))
                self._warned.add(account)
        return targets

    def add(self, video: Path, post: PostInfo, folder: Path | None = None, details: dict | None = None) -> list[str]:
        """Queue a reel for every account it should go to. Returns their names."""
        added = []
        waiting = self.approval_needed()
        for account in self.accounts_for(folder):
            key = f"{account}|{video}"
            self.items[key] = {
                "platform": split_account(account)[0],
                "account": account,
                "video": str(video),
                "post": post.to_dict(),
                "status": "waiting" if waiting else "pending",
                "attempts": 0,
                "next_try": 0,
                "added": time.time(),
                **(details or {}),
            }
            added.append(platform_name(account))
            if waiting:
                self.on_waiting(key, self.items[key])
        return added

    # ------------------------------------------------------------------ approval (phone)

    def approve(self, video: str, now: bool = False) -> int:
        """OK from the phone: post at the next posting time, or right away (now)."""
        count = 0
        for item in self.items.values():
            if item["video"] == video and item["status"] in ("waiting", "pending"):
                item["status"] = "pending"
                item["now"] = now or item.get("now", False)
                count += 1
        self.save()
        return count

    @property
    def paused(self) -> bool:
        return bool(self.state.data.get("posting_paused"))

    def pause(self, paused: bool = True) -> None:
        self.state.data["posting_paused"] = paused
        self.save()

    def skip(self, video: str) -> int:
        count = 0
        for item in self.items.values():
            if item["video"] == video and item["status"] in ("waiting", "pending"):
                item["status"] = "skipped"
                count += 1
        self.save()
        return count

    # ------------------------------------------------------------------ posting

    def pending(self, account: str | None = None) -> list[dict]:
        return sorted(
            (item for item in self.items.values()
             if item["status"] == "pending" and (account is None or item.get("account", item["platform"]) == account)),
            key=lambda item: item["added"],
        )

    def slots(self, account: str) -> list[tuple[int, int]]:
        """Posting times for an account: config (post_times), or learned from its best hours."""
        setting = self.cfg.upload.post_times
        if isinstance(setting, list):
            return parse_times(setting)
        learned = self.best_hours(account)
        return learned or parse_times(DEFAULT_TIMES)

    def is_due(self, account: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        last = float(self.last_post.get(account, 0))
        if now - last < self.cfg.upload.hours_between_posts * 3600:
            return False
        slots = self.slots(account)
        return not slots or last == 0 or next_slot(last, slots) <= now

    def run_due(self, should_stop: Callable[[], bool] = lambda: False) -> int:
        """Post at most one reel per account, if that account is due. Returns how many were posted."""
        posted = 0
        now = time.time()
        if self.paused:
            return 0
        self.credentials.reload_if_changed()
        accounts = {item.get("account", item["platform"]) for item in self.items.values() if item["status"] == "pending"}
        for account in sorted(accounts):
            if should_stop():
                break
            if not self.credentials.get(account) or not getattr(self.cfg.upload, split_account(account)[0], False):
                continue
            if account in self.blocked:
                if now - self._reminded.get(account, 0) > REMIND_EVERY:
                    log.warning("%s: %s. Open Brainrot Bot > Connect accounts to log in again.", platform_name(account), self.blocked[account])
                    self._reminded[account] = now
                continue
            ready = [i for i in self.pending(account) if i["next_try"] <= now]
            # "Now" from the phone skips the wait; everything else waits for the account's next posting time.
            item = next((i for i in ready if i.get("now")), None) or (ready[0] if ready and self.is_due(account, now) else None)
            if item is None:
                continue
            if self._post(account, item, should_stop):
                posted += 1
        return posted

    def _post(self, account: str, item: dict, should_stop: Callable[[], bool]) -> bool:
        video = Path(item["video"])
        name = platform_name(account)
        if not video.exists():
            item.update(status="failed", error="the reel file was deleted or moved")
            self.save()
            return False
        post = PostInfo.from_dict(item["post"])
        module = PLATFORMS[split_account(account)[0]]
        log.info("Posting %s to %s...", video.name, name)
        try:
            result = module.upload(video, post, self.cfg, self.credentials, should_stop, account=account)
        except StopRequested:
            raise
        except UploadError as exc:
            self._failed(account, item, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - unexpected answers: treat as temporary
            self._failed(account, item, UploadError(f"unexpected problem: {exc}"))
            return False
        if not isinstance(result, Posted):
            result = Posted(str(result))
        item.update(status="done", result=result.message, url=result.url, post_id=result.post_id, posted=time.time())
        item.pop("error", None)
        self.last_post[account] = time.time()
        self.save()
        log.info("%s: %s", name, result.message)
        try:
            self.on_posted(item)
        except Exception:  # noqa: BLE001 - a notification problem must not undo a post
            log.exception("Couldn't send the 'posted' notification")
        return True

    def _failed(self, account: str, item: dict, exc: UploadError) -> None:
        name = platform_name(account)
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["error"] = str(exc)
        if exc.relogin:
            self.blocked[account] = str(exc)
            log.error("%s: %s. Open Brainrot Bot > Connect accounts to log in again. Reels wait until then.", name, exc)
            self.on_problem(f"{name}: {exc}. Log in again in the app (Connect accounts); reels wait until then.", f"relogin:{account}")
        elif exc.retry and item["attempts"] < MAX_ATTEMPTS:
            minutes = exc.wait_hours * 60 if exc.wait_hours else BACKOFF_MINUTES[item["attempts"] - 1]
            item["next_try"] = time.time() + minutes * 60
            log.warning("%s: couldn't post %s (%s). Trying again in %d min.", name, Path(item["video"]).name, exc, minutes)
        else:
            item["status"] = "failed"
            log.error("%s: giving up on %s: %s", name, Path(item["video"]).name, exc)
        self.save()

    def reconnected(self, account: str) -> None:
        """Called after the user logs in again: unblock and retry right away."""
        self.blocked.pop(account, None)
        for item in self.items.values():
            if item.get("account", item["platform"]) == account and item["status"] == "pending":
                item["next_try"] = 0
        self.save()

    # ------------------------------------------------------------------ views and likes

    def refresh_stats(self, should_stop: Callable[[], bool] = lambda: False, limit: int = 10) -> int:
        """Read the numbers of posts that are due for a check. Returns how many were updated."""
        now = time.time()
        done = 0
        for item in sorted(self.items.values(), key=lambda i: i.get("posted", 0)):
            if done >= limit or should_stop():
                break
            if item["status"] != "done" or not item.get("post_id"):
                continue
            account = item.get("account", item["platform"])
            module = PLATFORMS.get(item["platform"])
            if module is None or not hasattr(module, "stats") or account in self.blocked or not self.credentials.get(account):
                continue
            ages = STATS_AGES_PAID.get(item["platform"], STATS_AGES)
            count = int(item.get("stats_count", 0))
            if count >= len(ages) or now - float(item.get("posted", now)) < ages[count] * 3600:
                continue
            try:
                numbers = module.stats(item["post_id"], self.cfg, self.credentials, account)
            except StopRequested:
                raise
            except UploadError as exc:
                log.debug("Couldn't read the stats of %s on %s: %s", Path(item["video"]).name, platform_name(account), exc)
                numbers = None
            except Exception as exc:  # noqa: BLE001
                log.debug("Couldn't read stats: %s", exc)
                numbers = None
            item["stats_count"] = count + 1
            if numbers:
                item["stats"] = numbers
                item["stats_at"] = now
                done += 1
        if done:
            self.save()
        return done

    def best_hours(self, account: str, count: int = 3) -> list[tuple[int, int]]:
        """The hours of the day this account's posts got the most views (once there's enough data)."""
        by_hour: dict[int, list[int]] = {}
        for item in self.items.values():
            if item.get("account", item["platform"]) == account and item.get("stats") and item.get("posted"):
                hour = dt.datetime.fromtimestamp(float(item["posted"])).hour
                by_hour.setdefault(hour, []).append(int(item["stats"].get("views", 0)))
        if sum(len(v) for v in by_hour.values()) < LEARN_AFTER:
            return []
        ranked = sorted(by_hour, key=lambda h: -sum(by_hour[h]) / len(by_hour[h]))
        return sorted((h, 0) for h in ranked[:count])

    def summary(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for item in self.items.values():
            key = item.get("account", item["platform"])
            counts.setdefault(key, {}).setdefault(item["status"], 0)
            counts[key][item["status"]] += 1
        return counts


def best_by(items: dict, field: str) -> list[tuple[str, int, float]]:
    """(name, posts, average views) for each clip folder ('folder') or gameplay video ('gameplay'), best first."""
    groups: dict[str, list[int]] = {}
    for item in items.values():
        numbers = item.get("stats")
        if not numbers:
            continue
        names = item.get(field) or []
        for name in names if isinstance(names, list) else [names]:
            groups.setdefault(str(name), []).append(int(numbers.get("views", 0)))
    ranked = [(name, len(views), sum(views) / len(views)) for name, views in groups.items()]
    return sorted(ranked, key=lambda r: -r[2])


def totals(items: dict) -> dict[str, dict[str, int]]:
    """Views, likes... added up per platform."""
    result: dict[str, dict[str, int]] = {}
    for item in items.values():
        numbers = item.get("stats") or {}
        platform = result.setdefault(item["platform"], {"posts": 0})
        if item["status"] == "done":
            platform["posts"] += 1
        for name, value in numbers.items():
            platform[name] = platform.get(name, 0) + int(value)
    return result
