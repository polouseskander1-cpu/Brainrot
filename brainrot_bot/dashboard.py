"""A small web page for your phone (same Wi-Fi): what's waiting for your OK, what was posted and how it
did, the best podcasts and gameplay, and a box to paste video links. Open it by scanning the QR code
in the menu (Phone dashboard). A secret key in the link keeps other people on the network out.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import queue
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .phone import URL_RE, Action, clean_folder_name
from .report import stats_summary
from .uploads.queue import PLATFORMS, platform_name

log = logging.getLogger("brainrot")

CREDENTIAL_KEY = "dashboard"
COOKIE = "brainrot"


def get_token(credentials) -> str:
    token = credentials.get(CREDENTIAL_KEY).get("token", "")
    if not token:
        token = secrets.token_urlsafe(18)
        credentials.set(CREDENTIAL_KEY, {"token": token})
    return token


def local_ip() -> str:
    """This computer's address on the home network (nothing is sent anywhere to find it)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def dashboard_url(cfg: SimpleNamespace, credentials) -> str:
    public = os.environ.get("BRAINROT_PUBLIC_URL", "").rstrip("/")  # a server's own address (Docker)
    if public:
        return f"{public}/?key={get_token(credentials)}"
    host = local_ip() if cfg.dashboard.lan else "127.0.0.1"
    return f"http://{host}:{cfg.dashboard.port}/?key={get_token(credentials)}"


def qr_text(url: str) -> str:
    """The link as a QR code drawn with characters, for the menu."""
    try:
        import io

        import segno
    except ImportError:
        return ""
    buffer = io.StringIO()
    segno.make(url, error="l").terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue()


def dashboard_data(state: dict, status: str, output: Path, folders: list[str]) -> dict:
    summary = stats_summary(state)

    def media(video: str, suffix: str) -> str:
        try:
            rel = Path(video).with_suffix(suffix).resolve().relative_to(output.resolve()).as_posix()
        except (ValueError, OSError):
            return ""
        return "/media?f=" + quote(rel) if (output / rel).exists() else ""

    waiting = {}
    for item in state.get("uploads", {}).values():
        if item.get("status") != "waiting":
            continue
        entry = waiting.setdefault(item["video"], {
            "video": item["video"], "name": Path(item["video"]).name, "title": (item.get("post") or {}).get("title", ""),
            "folder": item.get("folder", ""), "cover": media(item["video"], ".jpg"), "play": media(item["video"], ".mp4"),
            "accounts": []})
        account = item.get("account", item["platform"])
        entry["accounts"].append(platform_name(account) if item["platform"] in PLATFORMS else account)
    for entry in summary["recent"]:
        entry["cover"] = media(entry["video"], ".jpg")
    return {
        "version": __version__,
        "status": status,
        "paused": bool(state.get("posting_paused")),
        "waiting": list(waiting.values()),
        "summary": summary,
        "folders": folders,
        "links": [i for i in state.get("links", {}).values() if i.get("status") == "pending"],
    }


class Dashboard:
    def __init__(self, cfg: SimpleNamespace, credentials, actions: "queue.Queue[Action]", read_state: Callable[[], dict],
                 status: Callable[[], str], folders: Callable[[], list[str]]):
        self.cfg = cfg
        self.token = get_token(credentials)
        self.actions = actions
        self.read_state = read_state
        self.status = status
        self.folders = folders
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self.cfg.dashboard.enabled or self.server is not None:
            return False
        host = "0.0.0.0" if self.cfg.dashboard.lan else "127.0.0.1"
        try:
            self.server = ThreadingHTTPServer((host, self.cfg.dashboard.port), self._handler())
        except OSError as exc:
            log.warning("The phone dashboard couldn't start on port %s (%s). Change dashboard.port in config.yaml.",
                        self.cfg.dashboard.port, exc)
            return False
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True)
        self.thread.start()
        if os.environ.get("BRAINROT_SERVER") == "1":
            log.info("Phone dashboard on port %s (the link: docker compose run --rm brainrot --dashboard)", self.cfg.dashboard.port)
        else:
            log.info("Phone dashboard: http://%s:%s (open it from the menu > Phone dashboard)",
                     local_ip() if self.cfg.dashboard.lan else "127.0.0.1", self.cfg.dashboard.port)
        return True

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def handle_action(self, body: dict) -> tuple[int, dict]:
        kind = str(body.get("kind", ""))
        if kind in ("approve", "now", "skip"):
            video = str(body.get("video", ""))
            waiting = {i.get("video") for i in self.read_state().get("uploads", {}).values() if i.get("status") in ("waiting", "pending")}
            if video not in waiting:
                return 404, {"error": "that reel isn't waiting anymore"}
            self.actions.put(Action(kind, video=video))
            return 200, {"ok": True}
        if kind == "link":
            match = URL_RE.search(str(body.get("url", "")))
            if not match:
                return 400, {"error": "that doesn't look like a link"}
            folder = clean_folder_name(str(body.get("folder") or self.cfg.phone.link_folder))
            self.actions.put(Action("link", url=match.group(0), folder=folder))
            return 200, {"ok": True, "folder": folder}
        if kind in ("pause", "resume"):
            self.actions.put(Action(kind))
            return 200, {"ok": True}
        return 400, {"error": "unknown action"}

    def _handler(self):
        dashboard = self
        output = self.cfg.paths.output

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _authorized(self) -> bool:
                cookies = dict(part.strip().split("=", 1) for part in (self.headers.get("Cookie") or "").split(";") if "=" in part)
                return hmac.compare_digest(cookies.get(COOKIE, ""), dashboard.token)

            def _send(self, status: int, body: bytes, kind: str = "text/html; charset=utf-8", headers: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, data: dict) -> None:
                self._send(status, json.dumps(data).encode("utf-8"), "application/json")

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                query = parse_qs(url.query)
                key = (query.get("key") or [""])[0]
                if url.path == "/" and key:
                    if hmac.compare_digest(key, dashboard.token):
                        cookie = f"{COOKIE}={dashboard.token}; Max-Age=31536000; HttpOnly; SameSite=Strict; Path=/"
                        self._send(302, b"", headers={"Location": "/", "Set-Cookie": cookie})
                    else:
                        self._send(403, LOCKED.encode("utf-8"))
                    return
                if not self._authorized():
                    self._send(403, LOCKED.encode("utf-8"))
                    return
                if url.path == "/":
                    self._send(200, PAGE.encode("utf-8"))
                elif url.path == "/api/state":
                    data = dashboard_data(dashboard.read_state(), dashboard.status(), output, dashboard.folders())
                    self._json(200, data)
                elif url.path == "/media":
                    self._media((query.get("f") or [""])[0])
                else:
                    self._send(404, b"not found", "text/plain")

            do_HEAD = do_GET

            def do_POST(self):  # noqa: N802
                if urlparse(self.path).path != "/api/action":
                    self._send(404, b"not found", "text/plain")
                    return
                # The page always sends this header; other websites can't (that would need our permission).
                if not self._authorized() or self.headers.get("X-Brainrot") != "1":
                    self._json(403, {"error": "not allowed"})
                    return
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 65536)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._json(400, {"error": "bad request"})
                    return
                status, answer = dashboard.handle_action(body if isinstance(body, dict) else {})
                self._json(status, answer)

            def _media(self, rel: str) -> None:
                try:
                    path = (output / rel).resolve()
                    path.relative_to(output.resolve())  # only files from the reels folder
                except (ValueError, OSError):
                    self._send(403, b"not allowed", "text/plain")
                    return
                if path.suffix.lower() not in (".jpg", ".mp4") or not path.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                size = path.stat().st_size
                start, end = 0, size - 1
                ranged = self.headers.get("Range", "")
                if ranged.startswith("bytes="):  # phones stream videos in pieces
                    first, _, last = ranged[6:].split(",")[0].partition("-")
                    try:
                        start = int(first) if first else max(0, size - int(last))
                        end = min(size - 1, int(last)) if first and last else size - 1
                    except ValueError:
                        start, end = 0, size - 1
                    if start >= size:
                        self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                        return
                with open(path, "rb") as handle:
                    handle.seek(start)
                    data = handle.read(min(end - start + 1, 16 * 1024 * 1024))
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                headers = {"Accept-Ranges": "bytes"}
                if ranged:
                    headers["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/{size}"
                self._send(206 if ranged else 200, data, kind, headers)

        return Handler


LOCKED = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Brainrot Bot</title>
<body style="font-family:system-ui;background:#111;color:#eee;padding:2em;text-align:center">
<h2 style="color:#FFD400">Brainrot Bot</h2><p>Open this page with the link or QR code from the app<br>(menu &gt; Phone dashboard).</p>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brainrot Bot</title>
<style>
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--text:#eef0f4;--dim:#9aa3b2;--yellow:#FFD400;--green:#39d98a;--red:#ff5c5c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;overflow-x:hidden}
header{position:sticky;top:0;background:rgba(15,17,21,.95);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;align-items:center;gap:10px;z-index:2}
h1{font-size:18px;margin:0;color:var(--yellow);flex:1}h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:22px 0 8px}
main{max-width:720px;margin:0 auto;padding:0 16px 40px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:8px 0}
.status{color:var(--dim);font-size:13px;white-space:pre-line;overflow-wrap:anywhere}.row{display:flex;gap:12px;align-items:flex-start}
.row>div{min-width:0;flex:1}.title,.meta,td{overflow-wrap:anywhere}header button{flex:none}
.cover{width:84px;height:150px;object-fit:cover;border-radius:8px;background:#000;flex:none}
.title{font-weight:600}.meta{color:var(--dim);font-size:13px}
button,select,input{font:inherit;border-radius:9px;border:1px solid var(--line);background:#232833;color:var(--text);padding:9px 12px}
button{cursor:pointer;font-weight:600}.post{background:var(--green);color:#06140c;border:0}.now{background:var(--yellow);color:#1b1600;border:0}.skip{background:transparent;color:var(--red)}
.btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}input{width:100%}form .btns{margin-top:8px}
table{width:100%;border-collapse:collapse;table-layout:fixed}td{padding:6px 4px;border-bottom:1px solid var(--line)}td.n{text-align:right;color:var(--yellow);width:120px}
.empty{color:var(--dim);font-size:14px}a{color:var(--yellow)}#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#222;border:1px solid var(--line);padding:10px 14px;border-radius:10px;display:none}
video{width:100%;max-height:70vh;border-radius:10px;background:#000;margin-top:8px}
</style></head><body>
<header><h1>Brainrot Bot</h1><button id="pause" onclick="togglePause()">Pause posting</button></header>
<main>
<div class="card"><div id="status" class="status">Loading...</div></div>
<h2>Waiting for your OK</h2><div id="waiting"></div>
<h2>Add a video link</h2>
<form class="card" onsubmit="addLink(event)"><input id="url" type="url" placeholder="https://youtube.com/..." required>
<div class="btns"><select id="folder"></select><button class="now" type="submit">Add</button></div></form>
<h2>Numbers</h2><div id="numbers" class="card"></div>
<h2>Recent posts</h2><div id="recent"></div>
<h2>Best clip folders</h2><div id="folders" class="card"></div>
<h2>Best gameplay</h2><div id="gameplay" class="card"></div>
</main><div id="toast"></div>
<script>
let paused=false,waitingList=[];
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num=v=>{v=Number(v||0);return v>=1e6?(v/1e6).toFixed(1)+"M":v>=1e3?(v/1e3).toFixed(1)+"K":String(Math.round(v))};
function toast(t){const e=document.getElementById("toast");e.textContent=t;e.style.display="block";setTimeout(()=>e.style.display="none",2500)}
async function act(body){const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-Brainrot":"1"},body:JSON.stringify(body)});
 const j=await r.json().catch(()=>({}));if(!r.ok){toast(j.error||"Something went wrong");return false}return true}
async function choose(i,kind){const video=waitingList[i].video;if(await act({kind,video})){toast(kind=="skip"?"Won't be posted":kind=="now"?"Posting now":"Will be posted at the next posting time");setTimeout(load,1500)}}
function play(el,i){const src=waitingList[i].play;if(!src)return;const v=document.createElement("video");v.src=src;v.controls=true;v.autoplay=true;v.playsInline=true;el.replaceWith(v)}
async function addLink(e){e.preventDefault();const url=document.getElementById("url").value,folder=document.getElementById("folder").value;
 if(await act({kind:"link",url,folder})){toast("Added - downloading soon");document.getElementById("url").value="";setTimeout(load,1500)}}
async function togglePause(){if(await act({kind:paused?"resume":"pause"})){toast(paused?"Posting again":"Posting paused");setTimeout(load,1200)}}
function table(rows,empty){return rows.length?"<table>"+rows.map(r=>`<tr><td>${esc(r.name||"(clips folder)")}<div class=meta>${r.posts} posts</div></td><td class=n>${num(r.avg_views)} avg views</td></tr>`).join("")+"</table>":`<div class=empty>${empty}</div>`}
async function load(){let d;try{d=await (await fetch("/api/state")).json()}catch(e){document.getElementById("status").textContent="Can't reach the bot. Is it running?";return}
 paused=d.paused;document.getElementById("pause").textContent=paused?"Resume posting":"Pause posting";
 document.getElementById("status").textContent=d.status+(d.links.length?`\nLinks downloading: ${d.links.length}`:"");
 waitingList=d.waiting;
 document.getElementById("waiting").innerHTML=d.waiting.length?d.waiting.map((w,i)=>`<div class=card><div class=row>
  ${w.cover?`<img class=cover src="${esc(w.cover)}" onclick="play(this,${i})" alt="">`:""}<div><div class=title>${esc(w.title||w.name)}</div>
  <div class=meta>${esc(w.folder)} - ${esc(w.accounts.join(", "))}</div><div class=meta>Tap the picture to watch</div></div></div>
  <div class=btns><button class=post onclick="choose(${i},'approve')">Post</button>
  <button class=now onclick="choose(${i},'now')">Post now</button>
  <button class=skip onclick="choose(${i},'skip')">Skip</button></div></div>`).join(""):`<div class="card empty">Nothing is waiting.</div>`;
 const sel=document.getElementById("folder"),keep=sel.value;sel.innerHTML=d.folders.concat(["From phone"]).filter((v,i,a)=>a.indexOf(v)==i).map(f=>`<option>${esc(f)}</option>`).join("");if(keep)sel.value=keep;
 const s=d.summary;document.getElementById("numbers").innerHTML=`<div>${s.reels_made} reels made from ${s.clips_done} clips${s.duplicates?`, ${s.duplicates} duplicates skipped`:""}</div>`+
  (s.platforms.length?"<table>"+s.platforms.map(p=>`<tr><td>${esc(p.name)}<div class=meta>${p.posts} posts</div></td><td class=n>${num(p.views)} views<br>${num(p.likes)} likes</td></tr>`).join("")+"</table>":`<div class=empty>Nothing posted yet.</div>`);
 document.getElementById("recent").innerHTML=s.recent.length?s.recent.map(r=>`<div class=card><div class=row>${r.cover?`<img class=cover style="width:54px;height:96px" src="${esc(r.cover)}" alt="">`:""}
  <div><div class=title>${esc(r.title)}</div><div class=meta>${esc(r.account)}${/^https:\/\//.test(r.url||"")?` - <a href="${esc(r.url)}" target=_blank rel=noopener>open</a>`:""}</div>
  <div class=meta>${num(r.stats.views)} views - ${num(r.stats.likes)} likes - ${num(r.stats.comments)} comments</div></div></div></div>`).join(""):`<div class="card empty">No posts yet.</div>`;
 document.getElementById("folders").innerHTML=table(s.best_folders,"Needs a few posts with views first.");
 document.getElementById("gameplay").innerHTML=table(s.best_gameplay,"Needs a few posts with views first.");}
load();setInterval(load,15000);
</script></body></html>"""
