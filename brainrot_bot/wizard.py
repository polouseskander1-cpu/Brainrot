"""The setup page shown the first time the app starts (and from the menu under Settings)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

from . import service, ui
from .ai import CREDENTIAL_KEY as AI_KEY
from .ai import KEY_PAGE, check_key
from .config import FROZEN, SERVER, load_config, update_config_file
from .credentials import Credentials
from .cloud import synced_folders
from .links import add_link
from .phone import DISCORD, TELEGRAM, Discord, PhoneError, Telegram
from .uploads import PLATFORMS, UploadError, platform_name
from .uploads import facebook, instagram, pinterest, tiktok, x, youtube
from .uploads.accounts import account_id, set_folder_account, split_account, valid_name

log = logging.getLogger("brainrot")

GUIDE = "https://github.com/polouseskander1-cpu/Brainrot/blob/HEAD/docs/PLATFORMS.md"
TOTAL_STEPS = 6


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


def connect_youtube(cfg: SimpleNamespace, creds: Credentials, account: str = youtube.KEY) -> bool:
    _instructions([
        "YouTube needs a free Google key for a 'Desktop app' (about 5 minutes, only once):",
        "  1. console.cloud.google.com > create a project",
        "  2. APIs & Services > Library > enable 'YouTube Data API v3'",
        "  3. Google Auth Platform > set up the consent screen (External), then under",
        "     Audience press 'Publish app' (in 'Testing' the login stops working after 7 days)",
        "  4. Clients > Create client > type 'Desktop app' > Download JSON",
        ui.yellow("Heads up: until your Google project passes YouTube's API audit, YouTube LOCKS every"),
        ui.yellow("API upload as private and you can't make it public. The audit is free but can take weeks:"),
        ui.yellow("  support.google.com/youtube/contact/yt_api_form   (until then, skip YouTube and upload"),
        ui.yellow("  the reels from the Reels folder yourself)"),
    ])
    raw = ui.ask("Path to the downloaded JSON file (drag it here), or 'skip'")
    if _skip(raw):
        return False
    client_id, secret = youtube.read_client_file(ui.clean_path(raw))
    values = youtube.connect(client_id, secret, printer=ui.say)
    creds.set(account, values)
    ui.say(ui.green(f"  Connected to YouTube channel: {values.get('account')}"))
    return True


def connect_tiktok(cfg: SimpleNamespace, creds: Credentials, account: str = tiktok.KEY) -> bool:
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
    stats = ui.ask_yes_no("Also read the views and likes of posts? (needs the 'Display API' product with video.list)", default=False)
    values = tiktok.connect(key, secret, mode, port, printer=ui.say, stats=stats)
    creds.set(account, values)
    update_config_file(cfg.config_path, {"upload": {"tiktok_mode": mode}})
    ui.say(ui.green(f"  Connected to TikTok as: {values.get('account')}"))
    return True


def connect_instagram(cfg: SimpleNamespace, creds: Credentials, account: str = instagram.KEY) -> bool:
    _instructions([
        "Instagram Reels needs a Professional (Business or Creator) Instagram account:",
        "  1. developers.facebook.com/apps > Create app > type 'Business'",
        "  2. Add the 'Instagram' product > 'API setup with Instagram login'",
        "  3. Add your Instagram account and press 'Generate token', then copy the token",
        "     (allow 'instagram_business_manage_insights' too if you want views in the stats)",
        "The bot renews this token by itself every week, so it doesn't run out.",
    ])
    token = ui.ask("Access token (or 'skip')")
    if _skip(token):
        return False
    values = instagram.connect(token, cfg.upload.meta_api_version)
    creds.set(account, values)
    ui.say(ui.green(f"  Connected to Instagram: {values.get('account')}"))
    return True


def connect_facebook(cfg: SimpleNamespace, creds: Credentials, account: str = facebook.KEY) -> bool:
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
    creds.set(account, values)
    ui.say(ui.green(f"  Connected to Facebook Page: {values.get('account')}"))
    return True


def connect_x(cfg: SimpleNamespace, creds: Credentials, account: str = x.KEY) -> bool:
    port = cfg.upload.x_redirect_port
    _instructions([
        "X needs your own X developer app. X charges per use: about $0.015 per post, no monthly fee.",
        "  1. console.x.com > sign up for the API (pay-per-use) and add a little credit",
        "  2. Create an app > User authentication settings > OAuth 2.0,",
        "     type 'Web App, Automated App or Bot', permissions 'Read and write'",
        f"  3. Callback URI:  {x.redirect_uri(port)}",
        "  4. Keys and tokens > OAuth 2.0 Client ID and Client Secret",
    ])
    client_id = ui.ask("OAuth 2.0 Client ID (or 'skip')")
    if _skip(client_id):
        return False
    secret = ui.ask("Client Secret (Enter if your app has none)")
    values = x.connect(client_id.strip(), secret.strip(), port, printer=ui.say)
    creds.set(account, values)
    ui.say(ui.green(f"  Connected to X as: {values.get('account')}"))
    return True


def connect_pinterest(cfg: SimpleNamespace, creds: Credentials, account: str = pinterest.KEY) -> bool:
    port = cfg.upload.pinterest_redirect_port
    _instructions([
        "Pinterest needs your own Pinterest developer app (free):",
        "  1. developers.pinterest.com > My apps > Connect app (needs a Pinterest business account)",
        f"  2. Add the redirect URI:  {pinterest.redirect_uri(port)}",
        "  3. Copy the App ID and App secret key",
        ui.yellow("New apps get 'trial access' first; ask Pinterest for standard access so pins go public."),
    ])
    app_id = ui.ask("App ID (or 'skip')")
    if _skip(app_id):
        return False
    secret = ui.ask_required("App secret key")
    values = pinterest.connect(app_id.strip(), secret.strip(), port, printer=ui.say)
    boards = pinterest.list_boards(values["access_token"])
    ui.say("Which board should the pins go to?")
    choice = ui.choose([b.get("name", b["id"]) for b in boards] + ["Make a new board..."], default=1)
    if choice <= len(boards):
        board = boards[choice - 1]
    else:
        board = pinterest.create_board(values["access_token"], ui.ask_required("Name of the new board"))
    values.update(board_id=str(board.get("id", "")), board=board.get("name", ""))
    creds.set(account, values)
    ui.say(ui.green(f"  Connected to Pinterest: {values.get('account')}, board '{values.get('board')}'"))
    return True


CONNECTORS = {"youtube": connect_youtube, "tiktok": connect_tiktok, "instagram": connect_instagram, "facebook": connect_facebook,
              "x": connect_x, "pinterest": connect_pinterest}


def add_account(cfg: SimpleNamespace, creds: Credentials) -> str | None:
    """Connect one more account of a platform (e.g. a second YouTube channel for one podcast), give it a
    name, and pick the clip folders that post to it. Returns the new account id."""
    keys = list(PLATFORMS)
    ui.say("Which platform?")
    platform = keys[ui.choose([PLATFORMS[k].NAME for k in keys], default=1) - 1]
    while True:
        name = ui.ask("A short name for this account (e.g. gaming, podcast2)").strip().lower()
        if valid_name(name) and name not in ("main", "default", "off"):
            break
        ui.say(ui.red("  Use letters, numbers, - or _ (and not main/off)."))
    account = account_id(platform, name)
    try:
        if not CONNECTORS[platform](cfg, creds, account):
            return None
    except UploadError as exc:
        ui.say(ui.red(f"  Couldn't connect: {exc}"))
        return None
    root = cfg.paths.clips
    folders = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_", "~"))) if root.is_dir() else []
    if folders:
        ui.say(f"Which clip folders should post to {platform_name(account)}? Type their numbers, e.g. 1 3 (Enter = none yet)")
        for number, folder in enumerate(folders, 1):
            ui.say(f"  {ui.yellow(str(number))}) {folder}")
        picked = ui.ask("Folders", "")
        for token in picked.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(folders):
                set_folder_account(root / folders[int(token) - 1], platform, name)
                ui.say(f"  {folders[int(token) - 1]} -> {platform_name(account)}")
    ui.say(ui.dim(f"  (Any clip folder can use it: put '{platform} = {name}' in an accounts.txt file inside the folder.)"))
    return account


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


# ---------------------------------------------------------------- AI and links


def connect_ai(cfg: SimpleNamespace, creds: Credentials) -> bool:
    """Ask for an Anthropic API key. Returns True if one is connected."""
    saved = creds.get(AI_KEY)
    ui.say("Claude (AI) can pick the best moments of long videos, write the hook, title, caption and")
    ui.say("hashtags of every reel, and translate captions. Without it the bot uses built-in rules.")
    ui.say(ui.dim("It uses your own Anthropic API key: you pay Anthropic per use, usually a few cents per reel."))
    if saved:
        choice = ui.choose(["Keep the connected key", "Use a different key", "Disconnect the AI"], default=1)
        if choice == 1:
            return True
        if choice == 3:
            creds.remove(AI_KEY)
            ui.say("  AI disconnected.")
            return False
    elif not ui.ask_yes_no("Connect Claude (AI)?", default=False):
        return False
    _instructions([
        f"  1. Open {KEY_PAGE} and sign in (or sign up)",
        "  2. Add a little credit under Billing",
        "  3. API keys > Create key, then copy it",
    ])
    while True:
        key = ui.ask("Paste the key (starts with sk-ant-), or 'skip'")
        if _skip(key):
            return bool(saved)
        problem = check_key(key.strip(), cfg.ai.model)
        if not problem:
            creds.set(AI_KEY, {"api_key": key.strip(), "account": "Anthropic API"})
            ui.say(ui.green("  AI connected."))
            return True
        ui.say(ui.red(f"  That didn't work: {problem}"))
        if not ui.ask_yes_no("  Try again?", default=True):
            return bool(saved)


def connect_telegram(cfg: SimpleNamespace, creds: Credentials) -> bool:
    _instructions([
        "Telegram (free): the bot sends you each new reel with Post / Now / Skip buttons.",
        "  1. In Telegram, open @BotFather and send /newbot",
        "  2. Choose a name, then a username that ends in 'bot'",
        "  3. BotFather answers with a token like 123456789:AAH... - copy it",
    ])
    while True:
        token = ui.ask("Bot token (or 'skip')").strip()
        if _skip(token):
            return False
        try:
            username = Telegram.check_token(token)
            break
        except (PhoneError, UploadError) as exc:
            ui.say(ui.red(f"  That didn't work: {exc}"))
    ui.say(f"Now open  https://t.me/{username}  on your phone, press Start (or send any message).")
    ui.say("Waiting up to 2 minutes...")
    try:
        chat = Telegram(token, "").wait_for_chat(120)
    except (PhoneError, UploadError) as exc:
        ui.say(ui.red(f"  Telegram problem: {exc}"))
        return False
    if not chat:
        ui.say(ui.red("  No message arrived. Try again from the menu (Phone)."))
        return False
    bot = Telegram(token, chat)
    creds.set(TELEGRAM, {"token": token, "chat_id": chat, "account": "@" + username})
    try:
        bot.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in (
            ("status", "What the bot is doing"), ("stats", "Views, likes, best podcasts"),
            ("pause", "Stop posting for now"), ("resume", "Post again"), ("help", "What I can do"))]})
        bot.send_text("Connected! New reels will show up here. Send me a video link any time to make reels from it.")
    except (PhoneError, UploadError):
        pass
    ui.say(ui.green(f"  Telegram connected (@{username})."))
    return True


def connect_discord(cfg: SimpleNamespace, creds: Credentials) -> bool:
    _instructions([
        "Discord: the bot posts each new reel in a channel; react to approve it.",
        "  1. discord.com/developers/applications > New Application > Bot > Reset Token, copy the token",
        "  2. Same page: switch on 'Message Content Intent' (so it can read the links you send)",
        "  3. OAuth2 > URL Generator: scope 'bot'; permissions View Channels, Send Messages, Attach Files,",
        "     Add Reactions, Read Message History. Open the link and add the bot to your server",
        "  4. Discord settings > Advanced > Developer Mode on; right-click your channel > Copy Channel ID",
    ])
    token = ui.ask("Bot token (or 'skip')").strip()
    if _skip(token):
        return False
    channel = ui.ask_required("Channel ID").strip()
    owner = ui.ask("Your user ID, so only you can approve (right-click your name > Copy User ID; Enter to skip)").strip()
    try:
        bot_name, channel_name = Discord.check(token, channel)
        Discord(token, channel, owner).send_text("Connected! New reels will show up here. Send a video link any time to make reels from it.")
    except (PhoneError, UploadError) as exc:
        ui.say(ui.red(f"  That didn't work: {exc}"))
        return False
    creds.set(DISCORD, {"token": token, "channel_id": channel, "owner_id": owner, "account": f"#{channel_name}"})
    ui.say(ui.green(f"  Discord connected ({bot_name} in #{channel_name})."))
    return True


def setup_phone(cfg: SimpleNamespace, creds: Credentials, ask_first: bool = True) -> dict:
    """Returns the phone settings to save."""
    ui.say("Get every new reel on your phone, approve it with one tap, and send video links from anywhere.")
    if ask_first and not (creds.get(TELEGRAM) or creds.get(DISCORD)):
        if not ui.ask_yes_no("Connect your phone (Telegram or Discord)?", default=False):
            return {}
    settings = {"telegram": cfg.phone.telegram and bool(creds.get(TELEGRAM)), "discord": cfg.phone.discord and bool(creds.get(DISCORD))}
    for key, name, connect in ((TELEGRAM, "Telegram", connect_telegram), (DISCORD, "Discord", connect_discord)):
        saved = creds.get(key)
        ui.say()
        ui.say(ui.bold(f"> {name}"))
        if saved:
            choice = ui.choose([f"Keep ({saved.get('account', 'connected')})", "Connect again", "Turn off"], default=1)
            if choice == 1:
                settings[key] = True
                continue
            if choice == 3:
                creds.remove(key)
                settings[key] = False
                continue
        elif not ui.ask_yes_no(f"Use {name}?", default=key == TELEGRAM):
            settings[key] = False
            continue
        settings[key] = connect(cfg, creds) or bool(creds.get(key))
    if settings["telegram"] or settings["discord"]:
        settings["approval"] = ui.ask_yes_no("Wait for your OK on the phone before posting each reel?", default=cfg.phone.approval)
    return settings


def choose_look(cfg: SimpleNamespace, config_path: Path) -> None:
    """Menu: caption style, layout and the editing features, saved in config.yaml."""
    from .styles import DESCRIPTIONS, PRESETS

    while True:
        cfg = load_config(config_path)
        e = cfg.edit
        on = lambda value: ui.green("on") if value else ui.dim("off")  # noqa: E731
        ui.banner("look & features")
        items = [
            ("Caption style", f"{cfg.captions.style} - {DESCRIPTIONS.get(cfg.captions.style, '')}"),
            ("Layout", {"split": "clip on top, gameplay below", "floating": "gameplay full screen, clip as a card",
                        "fullscreen": "only the clip, following the face", "side": "side by side (landscape)"}[cfg.video.layout]),
            ("Cut pauses", on(e.cut_silences)),
            ("Zoom in on key moments", on(e.zoom)),
            ("Emojis over the captions", on(e.emojis)),
            ("Sound effects", on(e.sfx)),
            ("Hide swear words", on(e.censor) + (f" ({e.censor_mode})" if e.censor else "")),
            ("Hook title at the start", on(cfg.hook.enabled)),
            ("Cover picture", on(e.thumbnail)),
            ("Extra reels with translated captions", ", ".join(cfg.captions.translate_to) or ui.dim("none")),
            ("Render with", "graphics card if possible (auto)" if cfg.video.codec == "auto" else cfg.video.codec),
        ]
        for number, (name, value) in enumerate(items, 1):
            ui.say(f"  {ui.yellow(str(number))}) {name}: {value}")
        ui.say(f"  {ui.yellow('0')}) Back")
        choice = ui.ask("\nChange", "0")
        toggles = {"3": ("edit", "cut_silences"), "4": ("edit", "zoom"), "5": ("edit", "emojis"), "6": ("edit", "sfx"),
                   "8": ("hook", "enabled"), "9": ("edit", "thumbnail")}
        if choice == "0" or not choice:
            return
        if choice == "1":
            names = list(PRESETS)
            picked = names[ui.choose([f"{n} - {DESCRIPTIONS[n]}" for n in names], default=names.index(cfg.captions.style) + 1) - 1]
            update_config_file(config_path, {"captions": {"style": picked}})
        elif choice == "2":
            layouts = ["split", "floating", "fullscreen", "side"]
            picked = layouts[ui.choose([
                "Split: clip on top, gameplay below (the classic)", "Floating: gameplay full screen, the clip as a card on top",
                "Full screen: only the clip, cropped to follow the speaker's face",
                "Side by side: clip left, gameplay right (landscape 16:9 video)"], default=layouts.index(cfg.video.layout) + 1) - 1]
            changes = {"layout": picked}
            if picked == "side" and cfg.video.height > cfg.video.width:
                changes.update(width=1920, height=1080)
            elif picked != "side" and cfg.video.width > cfg.video.height:
                changes.update(width=1080, height=1920)
            update_config_file(config_path, {"video": changes})
        elif choice in toggles:
            section, key = toggles[choice]
            update_config_file(config_path, {section: {key: not getattr(getattr(cfg, section), key)}})
        elif choice == "7":
            if e.censor:
                mode = ui.choose(["Bleep", "Mute", "Turn off"], default=1)
                update_config_file(config_path, {"edit": {"censor": mode != 3, "censor_mode": "bleep" if mode == 1 else "mute"}})
            else:
                update_config_file(config_path, {"edit": {"censor": True}})
        elif choice == "10":
            ui.say("Language codes separated by spaces, e.g.  es ar fr  (Enter = none). Needs the AI helper.")
            codes = ui.ask("Languages", " ".join(cfg.captions.translate_to))
            update_config_file(config_path, {"captions": {"translate_to": codes.replace(",", " ").split()}})
        elif choice == "11":
            picked = ui.choose(["Graphics card if it works, otherwise the processor (auto)", "Always the processor (libx264)"],
                               default=1 if cfg.video.codec == "auto" else 2)
            update_config_file(config_path, {"video": {"codec": "auto" if picked == 1 else "libx264"}})


def add_video_link(cfg: SimpleNamespace) -> None:
    """Menu: paste a link, pick the folder (podcast / influencer) it belongs to."""
    ui.banner("add a video link")
    ui.say("Paste a link to a video (YouTube, TikTok, Instagram, X...). A playlist or channel link downloads")
    ui.say(f"the newest {cfg.links.playlist_limit} videos. Long videos (podcasts) become several reels of their best moments.")
    url = ui.ask("Link (or Enter to go back)")
    if not url.lower().startswith(("http://", "https://")):
        if url:
            ui.say(ui.red("  That doesn't look like a link (it should start with https://)."))
            ui.pause()
        return
    root = cfg.paths.clips
    root.mkdir(parents=True, exist_ok=True)
    folders = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_", "~")))
    ui.say("Which folder (podcast / influencer) is it for?")
    choice = ui.choose(folders + ["A new folder..."], default=1)
    if choice <= len(folders):
        folder = root / folders[choice - 1]
    else:
        name = ui.ask_required("Name of the new folder (e.g. the podcast's name)").strip().strip("/\\")
        folder = root / "".join(ch for ch in name if ch not in '<>:"/\\|?*')
    add_link(folder, url)
    ui.say(ui.green(f"  Added to {folder.name}/links.txt. The bot downloads it within a minute (while it's running)."))
    ui.pause()


# ---------------------------------------------------------------- the whole setup


def run_setup(config_path: Path, only_platforms: bool = False) -> SimpleNamespace:
    cfg = load_config(config_path)
    creds = Credentials(cfg.paths.credentials)

    if only_platforms:
        ui.banner("connect accounts")
        enabled = setup_platforms(cfg, creds)
        extra = [a for a in creds.data if ":" in a and split_account(a)[0] in PLATFORMS]
        if extra:
            ui.say()
            ui.say("Extra accounts: " + ", ".join(platform_name(a) for a in sorted(extra)))
        while ui.ask_yes_no("Add another account (a different channel/profile for some clip folders)?", default=False):
            account = add_account(cfg, creds)
            if account:
                enabled[split_account(account)[0]] = True
        update_config_file(config_path, {"upload": enabled})
        return load_config(config_path)

    ui.banner("setup")
    ui.say("Welcome! This takes about a minute. Press Enter to keep the answer in [brackets].")

    total = TOTAL_STEPS - (2 if SERVER else 0)
    numbers = iter(range(1, total + 1))
    delay = int(cfg.app.autostart_delay)
    if SERVER:  # Docker keeps it running and restarts it; there's no window or login to start with
        run_mode, autostart = "background", False
    else:
        ui.step(next(numbers), total, "How should the bot run?")
        run_mode = "background" if ui.choose([
            "24/7 in the background - keeps making reels after you close this window (recommended)",
            "Only while this window is open - closing the window stops it",
        ], default=1 if cfg.app.run_mode == "background" else 2) == 1 else "window"

        ui.step(next(numbers), total, "Start with your computer?")
        ui.say(f"The bot can start by itself about {delay} seconds after you log in, so it never misses a clip.")
        autostart = ui.ask_yes_no("Start automatically when you log in?", default=cfg.app.autostart or not cfg.app.setup_done)

    ui.step(next(numbers), total, "Auto-posting (optional)")
    enabled = setup_platforms(cfg, creds)

    ui.step(next(numbers), total, "AI helper (optional)")
    ai_on = connect_ai(cfg, creds)

    ui.step(next(numbers), total, "Your phone (optional)")
    phone = setup_phone(cfg, creds)

    ui.step(next(numbers), total, "Your folders")
    suggested = suggested_folders(cfg)
    clouds = synced_folders() if not cfg.app.setup_done and not SERVER else {}
    if clouds:
        ui.say("You have " + " and ".join(clouds) + " on this computer. Keeping the folders there lets you add clips")
        ui.say("from your phone's app and watch the finished reels on it.")
        names = list(clouds)
        choice = ui.choose([f"Yes, in {name} ({clouds[name] / 'Brainrot Bot'})" for name in names] + ["No, only on this computer"],
                           default=len(names) + 1)
        if choice <= len(names):
            base = clouds[names[choice - 1]] / "Brainrot Bot"
            suggested = {"gameplay": base / "Gameplay", "clips": base / "Clips", "output": base / "Reels", "music": base / "Music"}
    gameplay = ui.ask_path("Folder for your GAMEPLAY videos", suggested["gameplay"])
    clips = ui.ask_path("Folder for your CLIPS (one subfolder per podcast / influencer)", suggested["clips"])
    output = ui.ask_path("Folder where finished REELS are saved", suggested["output"])
    # The optional music folder sits next to the gameplay folder, wherever that was put.
    music = suggested["music"] if gameplay == Path(suggested["gameplay"]).resolve() else gameplay.parent / "Music"
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
        **({"ai": {"enabled": True}} if ai_on else {}),
        **({"phone": phone} if phone else {}),
    })
    if not SERVER:
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
    ui.say("  AI: " + ("Claude picks moments and writes hooks, captions and hashtags" if ai_on else "off (built-in rules)"))
    phones = [name for key, name in (("telegram", "Telegram"), ("discord", "Discord")) if phone.get(key)]
    if phones:
        ui.say("  Phone: " + " and ".join(phones) + (" (each reel waits for your OK)" if phone.get("approval") else ""))
    ui.say("  Runs: " + ("24/7 in the background" if run_mode == "background" else "while the window is open")
           + (f", starts {delay}s after you log in" if autostart else ""))
    ui.say()
    if SERVER:
        ui.say("  On the server these are inside the data folder. Start the bot with:  docker compose up -d")
    elif ui.ask_yes_no("Open the gameplay and clips folders now?", default=True):
        ui.open_folder(gameplay)
        ui.open_folder(clips)
    if os.name != "nt" and run_mode == "window" and autostart:
        ui.say(ui.dim("(On Mac/Linux, starting at login always runs in the background.)"))
    return load_config(config_path)
