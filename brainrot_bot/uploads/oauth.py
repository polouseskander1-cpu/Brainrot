"""Log in through the browser: the platform sends you back to a tiny web server on this computer."""

from __future__ import annotations

import base64
import hashlib
import secrets
import select
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .http import UploadError

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Brainrot Bot</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}}div{{text-align:center}}h1{{color:{color}}}</style></head>
<body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


def pkce_pair(hex_challenge: bool = False) -> tuple[str, str]:
    """(code_verifier, code_challenge). TikTok wants the challenge hex-encoded, everyone else base64url."""
    verifier = secrets.token_urlsafe(64)[:86]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    if hex_challenge:
        return verifier, digest.hex()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def browser_login(
    build_url: Callable[[str, str], str],
    *,
    port: int = 0,
    path: str = "/",
    timeout: float = 300,
    printer: Callable[[str], None] = print,
    open_browser: bool = True,
    host: str = "127.0.0.1",
) -> tuple[dict[str, str], str]:
    """Opens build_url(redirect_uri, state) in the browser and waits for the platform to send the
    browser back. Returns (query parameters, redirect_uri). host "localhost" also listens on IPv6,
    because browsers may send localhost there."""
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            good = "code" in params and params.get("state") == state
            received.update(params or {"error": "empty_response"})
            page = PAGE.format(
                color="#FFD400" if good else "#ff5555",
                title="Connected!" if good else "Login failed",
                text="You can close this tab and go back to the Brainrot Bot window." if good
                else "Go back to the Brainrot Bot window to see what happened.",
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the console quiet
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise UploadError(f"couldn't listen on port {port} for the login ({exc}). Close other programs using it.", retry=False) from exc
    servers = [server]
    if host == "localhost":
        try:
            servers.append(_IPv6Server(("::1", server.server_address[1]), Handler))
        except OSError:
            pass  # no IPv6 here: browsers then use 127.0.0.1
    try:
        redirect_uri = f"http://{host}:{server.server_address[1]}{path}"
        url = build_url(redirect_uri, state)
        printer("A browser window will open. Log in and allow access.")
        printer("If it doesn't open, copy this link into your browser:")
        printer(url)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            ready, _, _ = select.select(servers, [], [], 1.0)
            for ready_server in ready:
                ready_server.handle_request()
    finally:
        for each in servers:
            each.server_close()

    if not received:
        raise UploadError("the login took too long (5 minutes). Try again.", retry=False)
    if "error" in received:
        detail = received.get("error_description") or received["error"]
        raise UploadError(f"login was cancelled or refused: {detail}", retry=False)
    if received.get("state") != state:
        raise UploadError("login failed (the answer didn't match this request). Try again.", retry=False)
    if not received.get("code"):
        raise UploadError("login failed (no code came back). Try again.", retry=False)
    return received, redirect_uri


class _IPv6Server(HTTPServer):
    address_family = socket.AF_INET6
