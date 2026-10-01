"""Story ideas from Reddit, through Reddit's official API with your own Reddit app.

Since November 2025 Reddit approves every app before it may read anything (the Responsible Builder Policy),
and using Reddit posts commercially needs Reddit's written approval. So this only runs with the app ID and
secret of an app Reddit approved for you. It signs in as the app itself ("application-only"), so it never
needs your Reddit password, and reads the top posts of the subreddits you pick.

A post is never read out as it is: it's an idea. The AI writes a new story in its own words from it (posts
belong to the people who wrote them). r/nosleep and others that forbid adaptations are always skipped.
"""

from __future__ import annotations

import base64
import logging
import re
import sys
import time
from dataclasses import dataclass

from .. import __version__
from ..uploads import http

log = logging.getLogger("brainrot")

CREDENTIAL_KEY = "reddit"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_URL = "https://oauth.reddit.com"
APPS_PAGE = "https://www.reddit.com/prefs/apps"
POLICY_PAGE = "https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy"
# Subreddits whose rules forbid retelling their stories without the author's permission.
NEVER = {"nosleep", "shortscarystories"}
SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{1,20}$")


class RedditError(Exception):
    def __init__(self, message: str, wait: float = 3600.0):
        super().__init__(message)
        self.wait = wait


@dataclass
class Post:
    id: str
    subreddit: str
    title: str
    text: str
    score: int
    url: str

    @property
    def idea(self) -> str:
        return f"{self.title.strip()}\n\n{self.text.strip()}".strip()


def clean_subreddit(name: str) -> str:
    """'r/Stoicism', '/r/stoicism/' or 'https://reddit.com/r/Stoicism' -> 'Stoicism' ('' if it isn't one)."""
    name = (name or "").strip().rstrip("/")
    name = name.rsplit("/r/", 1)[-1] if "/r/" in name else name
    name = name[2:] if name.lower().startswith("r/") else name
    return name if SUBREDDIT_RE.match(name) else ""


def user_agent(username: str) -> str:
    """The User-Agent Reddit asks for: <platform>:<app ID>:<version> (by /u/<username>)."""
    platform = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    who = f" (by /u/{username})" if username else ""
    return f"{platform}:brainrot-bot:{__version__}{who}"


def _base(kind: str) -> str:
    import os

    override = os.environ.get("BRAINROT_REDDIT_BASE_URL")  # the tests' fake Reddit
    if override:
        return override.rstrip("/") + ("/api/v1/access_token" if kind == "token" else "")
    return TOKEN_URL if kind == "token" else API_URL


class Reddit:
    def __init__(self, client_id: str, client_secret: str, username: str = ""):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.username = username.strip().removeprefix("u/").removeprefix("/u/")
        self._token = ""
        self._expires = 0.0

    def _headers(self) -> dict:
        return {"User-Agent": user_agent(self.username)}

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            resp = http.request("POST", _base("token"), headers={**self._headers(), "Authorization": f"Basic {basic}"},
                                form={"grant_type": "client_credentials"}, timeout=30)
        except http.UploadError as exc:
            raise RedditError(f"couldn't reach Reddit ({exc})", wait=900) from exc
        data = resp.json()
        if resp.status in (401, 403) or data.get("error") in ("invalid_grant", "unauthorized_client", 401):
            raise RedditError("Reddit refused the app ID or secret (or hasn't approved the app yet). Check them in the "
                              f"menu (Stories > Reddit) and at {APPS_PAGE}", wait=24 * 3600)
        if not resp.ok or not data.get("access_token"):
            raise RedditError(f"Reddit didn't sign in the app (error {resp.status}: {resp.text(150)})", wait=3600)
        self._token = str(data["access_token"])
        self._expires = time.time() + float(data.get("expires_in") or 3600)
        return self._token

    def top(self, subreddit: str, period: str = "week", limit: int = 25) -> list[Post]:
        """The top text posts of a subreddit this week (no NSFW, no pinned posts, nothing removed)."""
        name = clean_subreddit(subreddit)
        if not name or name.lower() in NEVER:
            return []
        try:
            resp = http.request("GET", f"{_base('api')}/r/{name}/top",
                                params={"t": period, "limit": limit, "raw_json": 1},
                                headers={**self._headers(), "Authorization": f"bearer {self.token()}"}, timeout=30)
        except http.UploadError as exc:
            raise RedditError(f"couldn't reach Reddit ({exc})", wait=900) from exc
        if resp.status == 429:
            raise RedditError("Reddit asks to slow down", wait=1800)
        if resp.status in (403, 404):
            log.warning("Reddit: r/%s can't be read (private, banned or doesn't exist); skipping it.", name)
            return []
        if resp.status == 401:
            self._token = ""
            raise RedditError("Reddit signed the app out; trying again later", wait=600)
        if not resp.ok:
            raise RedditError(f"Reddit answered with error {resp.status}", wait=1800)
        posts = []
        for child in (resp.json().get("data") or {}).get("children") or []:
            d = child.get("data") if isinstance(child, dict) else None
            if not isinstance(d, dict) or d.get("over_18") or d.get("stickied") or d.get("spoiler"):
                continue
            text = str(d.get("selftext") or "")
            if text.strip() in ("[removed]", "[deleted]") or d.get("removed_by_category"):
                continue
            if d.get("is_video") or (not d.get("is_self", True) and not text):
                continue
            posts.append(Post(str(d.get("id") or ""), str(d.get("subreddit") or name), str(d.get("title") or ""),
                              text, int(d.get("score") or 0), "https://www.reddit.com" + str(d.get("permalink") or "")))
        return [p for p in posts if p.id and p.title]


def pick(posts: list[Post], used: set[str], min_score: int, count: int) -> list[Post]:
    """The best new posts that can make a 1-minute story: most upvoted first, long enough to have a story or
    an idea in them, not too long to retell."""
    good = [p for p in posts if p.id not in used and p.score >= min_score and 60 <= len(p.idea) <= 12000]
    good.sort(key=lambda p: -p.score)
    chosen, seen = [], set()
    for post in good:
        if post.id in seen:
            continue
        seen.add(post.id)
        chosen.append(post)
        if len(chosen) >= count:
            break
    return chosen


def check(client_id: str, client_secret: str, username: str = "") -> str:
    """'' if Reddit signs the app in, otherwise what's wrong."""
    try:
        Reddit(client_id, client_secret, username).token()
    except RedditError as exc:
        return str(exc)
    return ""
