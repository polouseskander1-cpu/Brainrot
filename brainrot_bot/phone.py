"""Your phone as a remote control, through Telegram and/or Discord.

- Every new reel is sent to you as a small preview. With phone.approval on, it's posted only after
  you tap Post (at the next posting time) or Now; Skip drops it. Without approval you can still stop
  a reel from being posted.
- Send the bot a video link and it's downloaded into the clip folder you pick.
- /status and /stats (Telegram) or "status" / "stats" (Discord) tell you what's going on.
- You also hear when a post goes live and when something needs you (a failed clip, an expired login).

The chat runs in its own thread, so taps are answered even while a reel is rendering; what they
change (the posting queue, links) is applied by the bot between its steps.
"""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import quote

from .media import Tools, popen_kwargs
from .report import stats_lines
from .uploads import http
from .uploads.http import UploadError

log = logging.getLogger("brainrot")

TELEGRAM = "telegram"
DISCORD = "discord"
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
TELEGRAM_LIMIT = 49 * 1024 * 1024  # bots can send files up to 50 MB
DISCORD_LIMIT = 9 * 1024 * 1024  # 10 MB for bots in a normal server
DISCORD_EMOJI = {"✅": "approve", "\U0001F680": "now", "❌": "skip"}  # check, rocket, cross


class PhoneError(Exception):
    pass


@dataclass
class Action:
    """Something you asked for from the phone, done by the bot between its steps."""

    kind: str  # approve | now | skip | link | pause | resume
    video: str = ""
    url: str = ""
    folder: str = ""


def make_preview(tools: Tools, video: Path, out: Path) -> Path | None:
    """A small copy of the reel (at most 960 px, about 1.5 Mbit/s) that phones load quickly."""
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
           "-vf", "scale=w='if(gt(iw,ih),960,-2)':h='if(gt(iw,ih),-2,960)'", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "30", "-maxrate", "1500k", "-bufsize", "3000k", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=600, check=True, **popen_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Couldn't make the phone preview: %s", exc)
        return None
    return out if out.exists() else None


def _describe(title: str, folder: str, duration: float, targets: list[str], waiting: bool) -> str:
    lines = [f"New reel: {title}"]
    if folder:
        lines.append(f"From: {folder} - {duration:.0f}s")
    if targets:
        lines.append(("Will be posted on: " if not waiting else "Post on: ") + ", ".join(targets))
        lines.append("Waiting for your OK." if waiting else "")
    else:
        lines.append("Saved (no accounts to post to).")
    return "\n".join(line for line in lines if line)


# ------------------------------------------------------------------ Telegram


class Telegram:
    name = "Telegram"
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.awaiting_folder: str | None = None  # link id waiting for a typed folder name

    def call(self, method: str, params: dict | None = None, files: dict | None = None, timeout: float = 60):
        url = f"{self.API}/bot{self.token}/{method}"
        if files:
            fields = {k: v if isinstance(v, str) else json.dumps(v) for k, v in (params or {}).items()}
            body, content_type = http.multipart(fields, files)
            resp = http.request("POST", url, headers={"Content-Type": content_type}, data=body, timeout=timeout)
        else:
            resp = http.request("POST", url, json_body=params or {}, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            raise PhoneError(f"Telegram: {data.get('description') or resp.status}")
        return data.get("result")

    @staticmethod
    def check_token(token: str) -> str:
        """The bot's @username, or raises PhoneError."""
        me = Telegram(token, "").call("getMe")
        return me.get("username", "")

    def wait_for_chat(self, seconds: float = 120) -> str:
        """The chat of the first person who writes to the bot (you, during setup)."""
        deadline = time.monotonic() + seconds
        offset = 0
        while time.monotonic() < deadline:
            for update in self.call("getUpdates", {"offset": offset, "timeout": 10}, timeout=20) or []:
                offset = update["update_id"] + 1
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id"):
                    self.call("getUpdates", {"offset": offset, "timeout": 0})  # mark it as read
                    return str(chat["id"])
        return ""

    def buttons(self, ref: str, waiting: bool) -> dict:
        if waiting:
            row = [{"text": "✅ Post", "callback_data": f"a|{ref}"}, {"text": "\U0001F680 Now", "callback_data": f"n|{ref}"},
                   {"text": "⏭ Skip", "callback_data": f"s|{ref}"}]
        else:
            row = [{"text": "⏭ Don't post it", "callback_data": f"s|{ref}"}]
        return {"inline_keyboard": [row]}

    def send_reel(self, ref: str, text: str, preview: Path | None, cover: Path | None, waiting: bool, has_targets: bool) -> str:
        params = {"chat_id": self.chat_id, "caption": html.escape(text)[:1000], "parse_mode": "HTML"}
        if has_targets:
            params["reply_markup"] = self.buttons(ref, waiting)
        if preview is not None and preview.stat().st_size <= TELEGRAM_LIMIT:
            params["supports_streaming"] = "true"
            result = self.call("sendVideo", params, {"video": (preview.name, preview.read_bytes(), "video/mp4")}, timeout=300)
        elif cover is not None:
            result = self.call("sendPhoto", params, {"photo": (cover.name, cover.read_bytes(), "image/jpeg")}, timeout=120)
        else:
            params = {k: v for k, v in params.items() if k != "caption"}
            params["text"] = html.escape(text)
            result = self.call("sendMessage", params)
        return str((result or {}).get("message_id", ""))

    def send_text(self, text: str) -> None:
        self.call("sendMessage", {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})

    def poll(self, phone: "Phone") -> None:
        updates = self.call("getUpdates", {"offset": phone.memory.get("telegram_offset", 0), "timeout": 8,
                                           "allowed_updates": ["message", "callback_query"]}, timeout=20) or []
        for update in updates:
            phone.remember(telegram_offset=update["update_id"] + 1)
            try:
                if "callback_query" in update:
                    self._tap(phone, update["callback_query"])
                elif "message" in update:
                    self._message(phone, update["message"])
            except (PhoneError, UploadError) as exc:
                log.debug("Telegram: %s", exc)

    def _tap(self, phone: "Phone", query: dict) -> None:
        message = query.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != self.chat_id:
            return  # not you
        kind, _, rest = str(query.get("data", "")).partition("|")
        answer = ""
        if kind in ("a", "n", "s"):
            video = phone.video_for(rest)
            if video:
                action = {"a": "approve", "n": "now", "s": "skip"}[kind]
                phone.actions.put(Action(action, video=video))
                answer = {"approve": "Will be posted at the next posting time", "now": "Posting now",
                          "skip": "Won't be posted"}[action]
                self.call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message.get("message_id"),
                                                     "reply_markup": {"inline_keyboard": [[{"text": answer, "callback_data": "x|done"}]]}})
            else:
                answer = "This reel is no longer waiting"
        elif kind in ("f", "fn"):
            link_id, _, index = rest.partition("|")
            link = phone.pending_links.get(link_id)
            if link is None:
                answer = "That link is gone, send it again"
            elif kind == "fn":
                self.awaiting_folder = link_id
                answer = "Type the new folder's name"
                self.send_text("Type the name of the new folder:")
            else:
                folders = link["folders"]
                folder = folders[int(index)] if index.isdigit() and int(index) < len(folders) else ""
                phone.pending_links.pop(link_id, None)
                phone.actions.put(Action("link", url=link["url"], folder=folder))
                answer = f"Added to {folder or 'the clips folder'}"
                self.send_text(f"✅ {answer}. It's downloaded within a minute.")
        self.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": answer[:190]})

    def _message(self, phone: "Phone", message: dict) -> None:
        if str((message.get("chat") or {}).get("id")) != self.chat_id:
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text:
            return
        if self.awaiting_folder and not text.startswith("/") and not URL_RE.search(text):
            link = phone.pending_links.pop(self.awaiting_folder, None)
            self.awaiting_folder = None
            if link:
                folder = clean_folder_name(text)
                phone.actions.put(Action("link", url=link["url"], folder=folder))
                self.send_text(f"✅ Added to {folder}. It's downloaded within a minute.")
            return
        reply = phone.command(text)
        if reply:
            self.send_text(reply)
            return
        url = URL_RE.search(text)
        if url:
            link_id, folders = phone.new_link(url.group(0))
            keyboard = [[{"text": name[:40], "callback_data": f"f|{link_id}|{i}"}] for i, name in enumerate(folders[:20])]
            keyboard.append([{"text": "➕ New folder", "callback_data": f"fn|{link_id}"}])
            self.call("sendMessage", {"chat_id": self.chat_id, "text": "Which folder (podcast / influencer) is it for?",
                                      "reply_markup": {"inline_keyboard": keyboard}})
        else:
            self.send_text(HELP)


# ------------------------------------------------------------------ Discord


class Discord:
    name = "Discord"
    API = "https://discord.com/api/v10"

    def __init__(self, token: str, channel_id: str, owner_id: str = ""):
        self.token = token
        self.channel_id = str(channel_id)
        self.owner_id = str(owner_id or "")
        self.bot_id = ""
        self._next_poll = 0.0

    def call(self, method: str, path: str, *, json_body=None, files: dict | None = None, params: dict | None = None, timeout: float = 60):
        headers = {"Authorization": f"Bot {self.token}"}
        for _ in range(3):
            if files:
                body, content_type = http.multipart({"payload_json": json.dumps(json_body or {})}, files)
                resp = http.request(method, self.API + path, headers={**headers, "Content-Type": content_type}, data=body, timeout=timeout)
            else:
                resp = http.request(method, self.API + path, headers=headers, json_body=json_body, params=params, timeout=timeout)
            if resp.status == 429:
                time.sleep(min(5.0, float(resp.json().get("retry_after", 1))))
                continue
            if resp.status == 204:
                return {}
            data = resp.json()
            if not resp.ok:
                raise PhoneError(f"Discord ({resp.status}): {data.get('message') or resp.text()}")
            return data.get("data") if set(data) == {"data"} else data
        raise PhoneError("Discord kept asking to slow down")

    @staticmethod
    def check(token: str, channel_id: str) -> tuple[str, str]:
        """(bot name, channel name), or raises PhoneError."""
        d = Discord(token, channel_id)
        me = d.call("GET", "/users/@me")
        channel = d.call("GET", f"/channels/{channel_id}")
        return me.get("username", "bot"), channel.get("name", channel_id)

    def _me(self) -> str:
        if not self.bot_id:
            self.bot_id = str(self.call("GET", "/users/@me").get("id", ""))
        return self.bot_id

    def send_reel(self, ref: str, text: str, preview: Path | None, cover: Path | None, waiting: bool, has_targets: bool) -> str:
        how = ("React ✅ to post (at the next posting time), \U0001F680 to post now, ❌ to skip." if waiting
               else "React ❌ to stop it from being posted.")
        content = text + ("\n" + how if has_targets else "")
        attach = preview if preview is not None and preview.stat().st_size <= DISCORD_LIMIT else cover
        body = {"content": content[:1900]}
        if attach is not None:
            body["attachments"] = [{"id": 0, "filename": attach.name}]
            kind = "video/mp4" if attach.suffix == ".mp4" else "image/jpeg"
            message = self.call("POST", f"/channels/{self.channel_id}/messages", json_body=body,
                                files={"files[0]": (attach.name, attach.read_bytes(), kind)}, timeout=300)
        else:
            message = self.call("POST", f"/channels/{self.channel_id}/messages", json_body=body)
        message_id = str(message.get("id", ""))
        if has_targets:
            for emoji, action in DISCORD_EMOJI.items():
                if waiting or action == "skip":
                    self.call("PUT", f"/channels/{self.channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me")
                    time.sleep(0.3)
        return message_id

    def send_text(self, text: str) -> None:
        self.call("POST", f"/channels/{self.channel_id}/messages", json_body={"content": text[:1900]})

    def _counts(self, user: dict) -> bool:
        if user.get("bot") or str(user.get("id")) == self._me():
            return False
        return not self.owner_id or str(user.get("id")) == self.owner_id

    def poll(self, phone: "Phone") -> None:
        if time.monotonic() < self._next_poll:
            return
        self._next_poll = time.monotonic() + 10
        # Reactions on reels that are waiting.
        pending = dict(phone.memory.get("discord_pending", {}))
        for message_id, ref in pending.items():
            for emoji, action in DISCORD_EMOJI.items():
                users = self.call("GET", f"/channels/{self.channel_id}/messages/{message_id}/reactions/{quote(emoji)}") or []
                if any(self._counts(u) for u in users):
                    video = phone.video_for(ref)
                    if video:
                        phone.actions.put(Action(action, video=video))
                        label = {"approve": "✅ will be posted at the next posting time", "now": "\U0001F680 posting now",
                                 "skip": "❌ won't be posted"}[action]
                        self.send_text(f"{Path(video).name}: {label}")
                    phone.forget_discord(message_id)
                    break
        # New messages: commands and links.
        after = phone.memory.get("discord_after")
        if not after:
            latest = self.call("GET", f"/channels/{self.channel_id}/messages", params={"limit": 1}) or []
            phone.remember(discord_after=str(latest[0]["id"]) if latest else "0")
            return
        messages = self.call("GET", f"/channels/{self.channel_id}/messages", params={"after": after, "limit": 20}) or []
        for message in sorted(messages, key=lambda m: int(m["id"])):
            phone.remember(discord_after=str(message["id"]))
            if not self._counts(message.get("author") or {}):
                continue
            text = (message.get("content") or "").strip()
            reply = phone.command(text)
            if reply:
                self.send_text(reply)
                continue
            url = URL_RE.search(text)
            if url:
                rest = (text[: url.start()] + " " + text[url.end():]).replace("|", " ").strip()
                folder = clean_folder_name(rest) if rest else phone.default_folder
                phone.actions.put(Action("link", url=url.group(0), folder=folder))
                self.send_text(f"✅ Added to {folder}. Tip: put the folder name after the link to choose another one.")


# ------------------------------------------------------------------ the hub


HELP = ("Send me a video link to make reels from it.\n"
        "/status - what the bot is doing\n/stats - views, likes, best podcasts\n/dashboard - the phone dashboard link\n"
        "/pause - stop posting for now\n/resume - post again")


def clean_folder_name(text: str) -> str:
    name = "".join(ch for ch in " ".join(text.split()) if ch not in '<>:"/\\|?*').strip(". ")
    return name[:60] or "From phone"


class Phone:
    def __init__(self, cfg: SimpleNamespace, credentials, tools: Tools, memory_path: Path, work_dir: Path,
                 actions: "queue.Queue[Action] | None" = None):
        self.cfg = cfg
        self.credentials = credentials
        self.tools = tools
        self.memory_path = memory_path
        self.work_dir = work_dir
        self.actions: queue.Queue[Action] = actions if actions is not None else queue.Queue()
        self.outbox: queue.Queue = queue.Queue()
        self.pending_links: dict[str, dict] = {}
        self.status_provider: Callable[[], str] = lambda: "The bot is running."
        self.stats_provider: Callable[[], str] = lambda: "No stats yet."
        self.folders_provider: Callable[[], list[str]] = lambda: []
        self.dashboard_provider: Callable[[], str] = lambda: ""
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_problem: dict[str, float] = {}
        self.memory = self._load()
        self.channels = self._connect()

    # ------------------------------------------------------------------ setup

    def _connect(self) -> list:
        channels = []
        phone = self.cfg.phone
        tg = self.credentials.get(TELEGRAM)
        if phone.telegram and tg.get("token") and tg.get("chat_id"):
            channels.append(Telegram(tg["token"], tg["chat_id"]))
        dc = self.credentials.get(DISCORD)
        if phone.discord and dc.get("token") and dc.get("channel_id"):
            channels.append(Discord(dc["token"], dc["channel_id"], dc.get("owner_id", "")))
        return channels

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    @property
    def approval(self) -> bool:
        return self.enabled and self.cfg.phone.approval

    @property
    def default_folder(self) -> str:
        return self.cfg.phone.link_folder

    def _load(self) -> dict:
        try:
            data = json.loads(self.memory_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def remember(self, **values) -> None:
        with self._lock:
            self.memory.update(values)
            self._save()

    def _save(self) -> None:
        tmp = self.memory_path.with_name(self.memory_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.memory, indent=1), encoding="utf-8")
            os.replace(tmp, self.memory_path)
        except OSError as exc:
            log.debug("Couldn't save the phone memory: %s", exc)

    def video_for(self, ref: str) -> str:
        with self._lock:
            return self.memory.get("refs", {}).get(ref, "")

    def forget_discord(self, message_id: str) -> None:
        with self._lock:
            self.memory.setdefault("discord_pending", {}).pop(message_id, None)
            self._save()

    def new_link(self, url: str) -> tuple[str, list[str]]:
        link_id = secrets.token_hex(3)
        folders = self.folders_provider()
        self.pending_links[link_id] = {"url": url, "folders": folders}
        return link_id, folders

    def command(self, text: str) -> str:
        """The answer to a command, or '' if the text isn't one."""
        word = text.strip().lower().lstrip("/!").split("@")[0].split(" ")[0] if text else ""
        if word in ("start", "help"):
            return HELP
        if word == "status":
            return self.status_provider()
        if word == "stats":
            return self.stats_provider()
        if word == "dashboard":
            link = self.dashboard_provider()
            return f"Open on the same Wi-Fi as the computer: {link}" if link else "The dashboard is switched off (dashboard.enabled)."
        if word in ("pause", "resume"):
            self.actions.put(Action(word))
            return "Posting paused. Send /resume to post again." if word == "pause" else "Posting again."
        return ""

    # ------------------------------------------------------------------ what the bot tells you (main thread)

    def reel_ready(self, video: Path, cover: Path | None, title: str, folder: str, duration: float, targets: list[str], waiting: bool) -> None:
        if not self.enabled:
            return
        ref = secrets.token_hex(4)
        with self._lock:
            refs = self.memory.setdefault("refs", {})
            refs[ref] = str(video)
            if len(refs) > 500:
                for old in list(refs)[: len(refs) - 500]:
                    refs.pop(old, None)
            self._save()
        self.outbox.put(("reel", ref, video, cover, _describe(title, folder, duration, targets, waiting), waiting, bool(targets)))

    def posted(self, item: dict) -> None:
        if self.enabled and self.cfg.phone.notify_posted:
            from .uploads.queue import platform_name

            where = platform_name(item.get("account", item["platform"]))
            self.outbox.put(("text", f"Posted {Path(item['video']).name} on {where}: {item.get('url') or item.get('result', '')}"))

    def problem(self, text: str, key: str = "") -> None:
        """Tell you about a problem, at most once every 6 hours for the same thing."""
        if not (self.enabled and self.cfg.phone.notify_errors):
            return
        key = key or text
        if time.time() - self._last_problem.get(key, 0) < 6 * 3600:
            return
        self._last_problem[key] = time.time()
        self.outbox.put(("text", "⚠️ " + text))

    def drain(self, handle: Callable[[Action], None]) -> None:
        """Apply what you asked for from the phone (called by the bot between its steps)."""
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                return
            try:
                handle(action)
            except Exception:  # noqa: BLE001
                log.exception("Couldn't do what the phone asked (%s)", action.kind)

    # ------------------------------------------------------------------ the chat thread

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="phone", daemon=True)
            self._thread.start()
            log.info("Phone connected: %s", ", ".join(c.name for c in self.channels))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self._send_waiting()
                for channel in self.channels:
                    if self._stop.is_set():
                        break
                    channel.poll(self)
                failures = 0
                if not any(isinstance(c, Telegram) for c in self.channels):
                    self._stop.wait(2)  # Telegram's long polling waits by itself
            except (PhoneError, UploadError) as exc:
                failures += 1
                log.debug("Phone: %s", exc)
                if failures in (5, 50):
                    log.warning("Can't reach the phone app (%s). Will keep trying.", exc)
                self._stop.wait(min(300, 5 * failures))
            except Exception:  # noqa: BLE001 - the chat must never take the bot down
                log.exception("Phone chat problem")
                self._stop.wait(30)

    def _send_waiting(self) -> None:
        while True:
            try:
                job = self.outbox.get_nowait()
            except queue.Empty:
                return
            try:
                if job[0] == "reel":
                    self._send_reel(*job[1:])
                else:
                    for channel in self.channels:
                        channel.send_text(job[1])
            except (PhoneError, UploadError, OSError) as exc:
                log.warning("Couldn't send to the phone: %s", exc)

    def _send_reel(self, ref: str, video: Path, cover: Path | None, text: str, waiting: bool, has_targets: bool) -> None:
        preview = None
        if self.cfg.phone.preview and Path(video).exists():
            self.work_dir.mkdir(parents=True, exist_ok=True)
            preview = make_preview(self.tools, Path(video), self.work_dir / f"preview_{ref}.mp4")
        cover = cover if cover is not None and Path(cover).exists() else None
        try:
            for channel in self.channels:
                message_id = channel.send_reel(ref, text, preview, cover, waiting, has_targets)
                if isinstance(channel, Discord) and has_targets and message_id:
                    with self._lock:
                        self.memory.setdefault("discord_pending", {})[message_id] = ref
                        self._save()
        finally:
            if preview is not None:
                preview.unlink(missing_ok=True)
