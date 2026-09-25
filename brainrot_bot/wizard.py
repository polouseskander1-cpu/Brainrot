"""The setup page shown the first time the app starts (and from the menu under Settings)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

from . import service, ui
from .config import FROZEN, load_config, update_config_file
from .credentials import Credentials
from .uploads import PLATFORMS, UploadError, platform_name
from .uploads import facebook, instagram, tiktok, youtube

log = logging.getLogger("brainrot")

GUIDE = "https://github.com/polouseskander1-cpu/Brainrot/blob/HEAD/docs/PLATFORMS.md"
TOTAL_STEPS = 4


def suggested_folders(cfg: SimpleNamespace) -> dict[str, Path]:
    """The .exe keeps your videos in Videos/Brainrot Bot; running from the source code uses the repo folders."""
    if FROZEN and not cfg.app.setup_done:
        videos = Path.home() / "Videos"
        base = (videos if videos.is_dir() else Path.home()) / "Brainrot Bot"
        return {"gameplay": base / "Gameplay", "clips": base / "Clips", "output": base / "Reels", "music": base / "Music"}
    return {"gameplay": cfg.paths.gameplay, "clips": cfg.paths.clips, "output": cfg.paths.output, "music": cfg.paths.music}


def _config_value(path: Path, base: Path) -> str:
    """Folders inside the app folder are saved relative, so the whole folder can be moved."""
    try:
        return path.relative_to(base).as_posix() or "."
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- connecting platforms


def _instructions(lines: list[str]) -> None:
    for line in lines:
        ui.say("  " + line)
    ui.say(ui.dim(f"  Step-by-step guide with pictures: {GUIDE}"))
    ui.say()


def _skip(text: str) -> bool:
    return text.strip().lower() in ("", "skip", "s")


def connect_youtube(cfg: SimpleNamespace, creds: Credentials) -> bool:
    _instructions([
        "YouTube needs a free Google key for a 'Desktop app' (about 5 minutes, only once):",
        "  1. console.cloud.google.com > create a project",
        "  2. APIs & Services > Library > enable 'YouTube Data API v3'",
        "  3. Google Auth Platform > set up the consent screen (External), then under",
        "     Audience press 'Publish app' (in 'Testing' the login stops working after 7 days)",
        "  4. Clients > Create client > type 'Desktop app' > Download JSON",
        ui.yellow("Note: until Google approves an audit of your project, YouTube keeps API uploads PRIVATE."),
        "You can make them public yourself in YouTube Studio.",
    ])
    raw = ui.ask("Path to the downloaded JSON file (drag it here), or 'skip'")
    if _skip(raw):
        return False
    client_id, secret = youtube.read_client_file(ui.clean_path(raw))
    values = youtube.connect(client_id, secret, printer=ui.say)
    creds.set(youtube.KEY, values)
    ui.say(ui.green(f"  Connected to YouTube channel: {values.get('account')}"))
    return True


def connect_tiktok(cfg: SimpleNamespace, creds: Credentials) -> bool:
    port = cfg.upload.tiktok_redirect_port
    _instructions([
        "TikTok needs your own (free) TikTok developer app:",
        "  1. developers.tiktok.com > Manage apps > Connect an app",
        "  2. Add the products 'Login Kit' and 'Content Posting API'",
        f"  3. Login Kit > Desktop > redirect URI:  {tiktok.redirect_uri(port)}",
        "  4. Scopes: user.info.basic and video.upload (and video.publish for direct posting)",
        "  5. Submit the app for review, or use Sandbox and add your TikTok account as a target user",
    ])
    ui.say("How should videos go to TikTok?")
    mode = "draft" if ui.choose([
        "Send to my TikTok inbox, I tap Post (recommended: works right away, you can add a trending sound)",
        "Post directly (only for apps that passed TikTok's audit, otherwise TikTok refuses public posts)",
    ], default=1 if cfg.upload.tiktok_mode == "draft" else 2) == 1 else "direct"
    key = ui.ask("Client key (or 'skip')")
    if _skip(key):
        return False
    secret = ui.ask_required("Client secret")
    values = tiktok.connect(key, secret, mode, port, printer=ui.say)
    creds.set(tiktok.KEY, values)
    update_config_file(cfg.config_path, {"upload": {"tiktok_mode": mode}})
    ui.say(ui.green(f"  Connected to TikTok as: {values.get('account')}"))
    return True


def connect_instagram(cfg: SimpleNamespace, creds: Credentials) -> bool:
    _instructions([
        "Instagram Reels needs a Professional (Business or Creator) Instagram account:",
        "  1. developers.facebook.com/apps > Create app > type 'Business'",
        "  2. Add the 'Instagram' product > 'API setup with Instagram login'",
        "  3. Add your Instagram account and press 'Generate token', then copy the token",
        "The bot renews this token by itself every week, so it doesn't run out.",
    ])
    token = ui.ask("Access token (or 'skip')")
    if _skip(token):
        return False
    values = instagram.connect(token, cfg.upload.meta_api_version)
    creds.set(instagram.KEY, values)
    ui.say(ui.green(f"  Connected to Instagram: {values.get('account')}"))
    return True


def connect_facebook(cfg: SimpleNamespace, creds: Credentials) -> bool:
    _instructions([
        "Facebook Reels posts to a Facebook Page you manage (reels of 3-90 seconds):",
        "  1. developers.facebook.com/apps > your 'Business' app > App settings > Basic:",
        "     copy the App ID and App secret",
        "  2. developers.facebook.com/tools/explorer > pick your app > add the permissions",
        "     pages_show_list, pages_read_engagement, pages_manage_posts > Generate Access Token",
    ])
    app_id = ui.ask("App ID (or 'skip')")
    if _skip(app_id):
        return False
    secret = ui.ask_required("App secret")
    user_token = ui.ask_required("Access token from the Graph API Explorer")
    version = cfg.upload.meta_api_version
    long_token = facebook.long_lived_user_token(app_id, secret, user_token, version)
    pages = facebook.list_pages(long_token, version)
    if not pages:
        raise UploadError("this Facebook account doesn't manage any Page (or the permissions weren't granted)", retry=False)
    ui.say("Which Page should the reels go to?")
    page = pages[ui.choose([p.get("name", p["id"]) for p in pages]) - 1]
    values = facebook.page_credentials(page)
    creds.set(facebook.KEY, values)
    ui.say(ui.green(f"  Connected to Facebook Page: {values.get('account')}"))
    return True


CONNECTORS = {"youtube": connect_youtube, "tiktok": connect_tiktok, "instagram": connect_instagram, "facebook": connect_facebook}


def setup_platforms(cfg: SimpleNamespace, creds: Credentials) -> dict[str, bool]:
    """Ask about each platform: connect, keep, or skip. Returns which ones are switched on."""
    ui.say("The bot can post every finished reel for you. Skip anything you don't want;")
    ui.say("you can connect more later from the menu.")
    enabled: dict[str, bool] = {}
    for key in PLATFORMS:
        name = platform_name(key)
        saved = creds.get(key)
        ui.say()
        ui.say(ui.bold(f"> {name}"))
        if saved:
            choice = ui.choose([
                f"Keep posting (connected: {saved.get('account', 'yes')})" if getattr(cfg.upload, key) else f"Turn posting on (connected: {saved.get('account', 'yes')})",
                "Log in again / use a different account",
                "Turn off",
            ], default=1)
            if choice == 1:
                enabled[key] = True
                continue
            if choice == 3:
                enabled[key] = False
                continue
        elif not ui.ask_yes_no(f"Connect {name}?", default=False):
            enabled[key] = False
            continue
        while True:
            try:
                enabled[key] = CONNECTORS[key](cfg, creds)
                break
            except UploadError as exc:
                ui.say(ui.red(f"  Couldn't connect {name}: {exc}"))
                if not ui.ask_yes_no("  Try again?", default=True):
                    enabled[key] = bool(creds.get(key)) and getattr(cfg.upload, key)
                    break
    return enabled


# ---------------------------------------------------------------- the whole setup


def run_setup(config_path: Path, only_platforms: bool = False) -> SimpleNamespace:
    cfg = load_config(config_path)
    creds = Credentials(cfg.paths.credentials)

    if only_platforms:
        ui.banner("connect accounts")
        enabled = setup_platforms(cfg, creds)
        update_config_file(config_path, {"upload": enabled})
        return load_config(config_path)

    ui.banner("setup")
    ui.say("Welcome! This takes about a minute. Press Enter to keep the answer in [brackets].")

    ui.step(1, TOTAL_STEPS, "How should the bot run?")
    run_mode = "background" if ui.choose([
        "24/7 in the background - keeps making reels after you close this window (recommended)",
        "Only while this window is open - closing the window stops it",
    ], default=1 if cfg.app.run_mode == "background" else 2) == 1 else "window"

    ui.step(2, TOTAL_STEPS, "Start with your computer?")
    delay = int(cfg.app.autostart_delay)
    ui.say(f"The bot can start by itself about {delay} seconds after you log in, so it never misses a clip.")
    autostart = ui.ask_yes_no("Start automatically when you log in?", default=cfg.app.autostart or not cfg.app.setup_done)

    ui.step(3, TOTAL_STEPS, "Auto-posting (optional)")
    enabled = setup_platforms(cfg, creds)

    ui.step(4, TOTAL_STEPS, "Your folders")
    suggested = suggested_folders(cfg)
    gameplay = ui.ask_path("Folder for your GAMEPLAY videos", suggested["gameplay"])
    clips = ui.ask_path("Folder for your CLIPS (one subfolder per podcast / influencer)", suggested["clips"])
    output = ui.ask_path("Folder where finished REELS are saved", suggested["output"])
    music = suggested["music"]
    music.mkdir(parents=True, exist_ok=True)

    base = config_path.resolve().parent
    update_config_file(config_path, {
        "folders": {
            "gameplay": _config_value(gameplay, base),
            "clips": _config_value(clips, base),
            "output": _config_value(output, base),
            "music": _config_value(music, base),
        },
        "app": {"run_mode": run_mode, "autostart": autostart, "setup_done": True},
        "upload": enabled,
    })
    try:
        service.set_autostart(autostart, config=config_path)
    except OSError as exc:
        ui.say(ui.red(f"Couldn't change the start-at-login setting: {exc}"))

    ui.banner("ready")
    ui.say()
    ui.say(ui.green(ui.bold("  ALL SET!")))
    ui.say()
    ui.say("  1) Paste your GAMEPLAY videos here:")
    ui.say("     " + ui.yellow(str(gameplay)))
    ui.say()
    ui.say("  2) Paste your CLIPS here, in one subfolder per podcast / influencer / topic:")
    ui.say("     " + ui.yellow(str(clips)))
    ui.say(ui.dim(f"     example: {clips / 'My Podcast Ep 12' / 'clip1.mp4'}"))
    ui.say()
    ui.say("  Every video in those subfolders becomes a reel here:")
    ui.say("     " + ui.cyan(str(output)))
    posting = [platform_name(k) for k, on in enabled.items() if on]
    ui.say()
    ui.say("  Posting to: " + (", ".join(posting) if posting else "nowhere yet (reels are just saved)"))
    ui.say("  Runs: " + ("24/7 in the background" if run_mode == "background" else "while the window is open")
           + (f", starts {delay}s after you log in" if autostart else ""))
    ui.say()
    if ui.ask_yes_no("Open the gameplay and clips folders now?", default=True):
        ui.open_folder(gameplay)
        ui.open_folder(clips)
    if os.name != "nt" and run_mode == "window" and autostart:
        ui.say(ui.dim("(On Mac/Linux, starting at login always runs in the background.)"))
    return load_config(config_path)
