"""Numbers for people: what was posted, how it did, which podcasts and gameplay work best.
Used by the menu, the phone dashboard and the Telegram / Discord bot."""

from __future__ import annotations

import datetime as dt

from .uploads.queue import PLATFORMS, best_by, platform_name, totals


def short_number(value: float) -> str:
    value = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:.0f}"


def stats_summary(state: dict) -> dict:
    """Everything the screens show, as plain data."""
    items = state.get("uploads", {})
    clips = state.get("clips", {})
    per_platform = []
    for platform, numbers in sorted(totals(items).items()):
        if platform in PLATFORMS:
            per_platform.append({"platform": platform, "name": PLATFORMS[platform].NAME, **numbers})
    recent = sorted((i for i in items.values() if i.get("status") == "done"), key=lambda i: -float(i.get("posted", 0)))[:15]
    waiting = sorted((i for i in items.values() if i.get("status") in ("pending", "waiting")), key=lambda i: i.get("added", 0))
    return {
        "reels_made": sum(len(e.get("outputs", [])) for e in clips.values() if e.get("status") == "done"),
        "clips_done": sum(1 for e in clips.values() if e.get("status") == "done"),
        "clips_failed": sum(1 for e in clips.values() if e.get("status") == "failed"),
        "duplicates": sum(1 for e in clips.values() if e.get("status") == "duplicate"),
        "platforms": per_platform,
        "best_folders": [{"name": n, "posts": c, "avg_views": v} for n, c, v in best_by(items, "folder")[:10]],
        "best_gameplay": [{"name": n, "posts": c, "avg_views": v} for n, c, v in best_by(items, "gameplay")[:10]],
        "recent": [{
            "video": i.get("video", ""), "account": platform_name(i.get("account", i["platform"])) if i["platform"] in PLATFORMS else i["platform"],
            "url": i.get("url", ""), "posted": i.get("posted", 0), "stats": i.get("stats") or {},
            "title": (i.get("post") or {}).get("title", ""),
        } for i in recent],
        "waiting": [{
            "video": i.get("video", ""), "account": platform_name(i.get("account", i["platform"])) if i["platform"] in PLATFORMS else i["platform"],
            "status": i.get("status"), "title": (i.get("post") or {}).get("title", ""),
        } for i in waiting],
    }


def stats_lines(state: dict) -> list[str]:
    """The stats as text (menu and chat bots)."""
    s = stats_summary(state)
    lines = [f"Reels made: {s['reels_made']} from {s['clips_done']} clips"
             + (f" ({s['duplicates']} duplicates skipped)" if s["duplicates"] else "")]
    if not s["platforms"]:
        lines.append("Nothing posted yet.")
    for p in s["platforms"]:
        numbers = ", ".join(f"{short_number(p[k])} {k}" for k in ("views", "likes", "comments", "shares", "saves") if p.get(k))
        lines.append(f"{p['name']}: {p['posts']} posts" + (f", {numbers}" if numbers else ""))
    if s["best_folders"]:
        lines.append("")
        lines.append("Best clip folders (average views per post):")
        lines += [f"  {f['name'] or '(clips folder)'}: {short_number(f['avg_views'])} ({f['posts']} posts)" for f in s["best_folders"][:5]]
    if s["best_gameplay"]:
        lines.append("Best gameplay:")
        lines += [f"  {g['name']}: {short_number(g['avg_views'])} ({g['posts']} posts)" for g in s["best_gameplay"][:5]]
    if s["waiting"]:
        lines.append("")
        lines.append(f"Waiting to be posted: {len(s['waiting'])}")
    return lines


def when(timestamp: float) -> str:
    if not timestamp:
        return ""
    moment = dt.datetime.fromtimestamp(float(timestamp))
    today = dt.date.today()
    if moment.date() == today:
        return moment.strftime("today %H:%M")
    if moment.date() == today - dt.timedelta(days=1):
        return moment.strftime("yesterday %H:%M")
    return moment.strftime("%d %b %H:%M")
