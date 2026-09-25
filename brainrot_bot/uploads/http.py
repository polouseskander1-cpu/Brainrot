"""Tiny HTTP helper on top of the standard library (keeps the .exe small, no extra packages)."""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urlencode

from .. import __version__

USER_AGENT = f"BrainrotBot/{__version__}"


class UploadError(Exception):
    """Posting failed.

    retry:   worth trying again later (network trouble, server busy, daily limit reached)
    relogin: the saved login stopped working; the account must be connected again
    wait_hours: how long to wait before the next try, when the platform says so (limits)
    """

    def __init__(self, message: str, *, retry: bool = True, relogin: bool = False, wait_hours: float | None = None):
        super().__init__(message)
        self.retry = retry and not relogin
        self.relogin = relogin
        self.wait_hours = wait_hours


@dataclass
class Response:
    status: int
    headers: Message
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> dict:
        try:
            data = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {"data": data}

    def text(self, limit: int = 300) -> str:
        return self.body.decode("utf-8", errors="replace")[:limit]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Upload APIs use 3xx codes (e.g. YouTube's 308) as normal answers, never follow them."""

    def redirect_request(self, *args, **kwargs):
        return None


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:  # also trust certifi's list: python.org builds on macOS have no system certificates
        import certifi

        context.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return context


_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context()))
    return _opener


def request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: dict | None = None,
    form: dict | None = None,
    data=None,
    timeout: float = 60,
) -> Response:
    """Send a request and return the response, whatever its status. Network failures raise UploadError."""
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    all_headers = {"User-Agent": USER_AGENT}
    all_headers.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif form is not None:
        data = urlencode(form).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is None and method in ("POST", "PUT"):
        data = b""
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with _get_opener().open(req, timeout=timeout) as resp:
            return Response(resp.status, resp.headers, resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return Response(exc.code, exc.headers or Message(), body)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise UploadError(f"network problem ({reason})") from exc
