# Brainrot Reel Bot

Drop podcast or educational clips (or just paste a YouTube / TikTok link) and get back vertical
reels: the clip on top, random gameplay underneath that lasts exactly as long as the clip, and big
yellow word-by-word captions. It runs 24/7, finds the best moments of long episodes, edits them
(cut pauses, zooms on the speaker, emojis, sound effects), writes the hook and caption, makes a version
of every reel for each platform (1 minute or longer for TikTok, under a minute for YouTube Shorts...),
and can post them to TikTok, Instagram, Facebook, YouTube, X and Pinterest, with a tap-to-approve on your phone.

```
┌────────────────────────┐
│                        │
│     your clip          │  top third, shown whole (never cropped), zooms in on the speaker
│                        │
├████████████░░░░░░░░░░░░┤  progress bar that fills up as the reel plays
│  ┌──────────────────┐  │
│  │ hook title       │  │  first 4 seconds (written for you, or from title.txt)
│  └──────────────────┘  │
│          💰            │  emoji when a word matches
│    YELLOW CAPTIONS     │  white outline, soft drop shadow, key words in green
│                        │
│    random gameplay     │  bottom two thirds, ends together with the clip
│                        │
└────────────────────────┘
        1080 x 1920 (9:16)
```

## Download (Windows)

1. Open the [latest release](https://github.com/polouseskander1-cpu/Brainrot/releases/latest) and
   download **BrainrotBot-windows.zip**. Nothing else to install: ffmpeg is included.
2. Unzip the whole folder somewhere permanent, for example `Documents\BrainrotBot`.
3. Double-click **BrainrotBot.exe**. Windows may say *"Windows protected your PC"* because the app
   isn't signed. Click **More info > Run anyway**.

On Mac or Linux, [run it from the source code](#run-from-the-source-code-mac-linux-windows). On a
server or a Raspberry Pi, use [Docker](docs/SERVER.md).

## The setup page

The first time, the app asks a few questions in its window (everything optional can be skipped and
added later from the menu):

1. **How should the bot run?** *24/7 in the background* keeps working after you close the window
   (with an icon next to the clock). *Only while this window is open* stops when you close it.
2. **Start with your computer?** If yes, it starts by itself about 15 seconds after you log in.
3. **Auto-posting.** YouTube Shorts, TikTok, Instagram Reels, Facebook Reels, X and Pinterest: connect
   or skip each one. → [How to connect each platform](docs/PLATFORMS.md)
4. **AI helper (optional).** An Anthropic API key lets Claude pick the best moments of long videos
   and write hooks, titles, captions and hashtags. Without it the bot uses its own rules.
5. **Your phone (optional).** Telegram or Discord: every new reel comes to your phone with *Post* /
   *Skip* buttons. → [Phone guide](docs/PHONE.md)
6. **Your folders.** If you use Google Drive, Dropbox or OneDrive, it offers to put the folders there,
   so you can add clips from your phone's app and watch the reels on it.

At the end it shows the two folders you need:

- **Gameplay**: paste your gameplay recordings here (Minecraft, parkour, Subway Surfers...).
- **Clips**: make **one subfolder per podcast / influencer / topic** and paste the clips inside.
  Every video in those subfolders becomes reels in the **Reels** folder, one version per platform:
  `Reels\TikTok\<podcast>\`, `Reels\YouTube\<podcast>\`, `Reels\Instagram\<podcast>\` and
  `Reels\Facebook\<podcast>\`.

After that, double-clicking the app shows a small menu: what the bot is doing, what is waiting to be
posted, and options to open the folders, add a video link, open the phone dashboard, see the stats,
change the look, connect accounts, or stop and start the bot.

The very first clip takes a few extra minutes, because the speech-recognition model (about 500 MB)
is downloaded once. After that, a 1-minute clip takes about 1-2 minutes on a normal PC.

## What it does

**Getting the videos**
- **Folders**: any video dropped into a clip subfolder is picked up (half-copied files are never used).
- **Links**: *Add a video link* in the menu, a link sent to your phone bot, or a `links.txt` file in a
  clip folder (one link per line). YouTube, TikTok, Instagram, X and more; the video is downloaded
  into that folder. A playlist or channel link takes the newest 5.
- **Long videos** (whole podcast episodes, 5 minutes or longer): instead of one huge reel, the bot finds
  the **best moments** and makes reels of each: about one per 20 minutes, so **6 from a 2-hour episode**
  (set your own number in the menu under **Look & features**). Each moment is 1-1.5 minutes, long enough
  for TikTok, and the YouTube version is the strongest part of it. The bot looks for hot takes and
  controversial opinions, mind-blowing facts and sci-fi talk (space, aliens, AI, the future), stories,
  strong openings (a question, a bold claim, a number) and a payoff at the end, and skips intros, ads
  and small talk. With the AI helper, Claude reads the whole transcript and picks them.
- **Duplicates are skipped**: the same video renamed, copied, re-downloaded or re-encoded (same sound
  or same words) is noticed and not made or posted twice.

**Editing**
- **Gameplay picked at random**, cut to exactly the reel's length. Recordings take turns.
- **Captions** from offline speech recognition (free, many languages): yellow, white stroke, soft drop
  shadow, popping in as each phrase is said. **Key words** (money, never, numbers...) turn green.
- **Pauses cut**: silences longer than half a second are removed (laughter and reactions are kept).
- **Zooms**: quick punch-ins on the strongest moments, aimed at the speaker's face.
- **Emojis** over the captions (money → 💰, fire → 🔥...) and **sound effects** (whoosh on zooms,
  a "boom" on punchlines, a pop for emojis), all made by the bot itself.
- **Swear words** hidden if you want: a bleep (or silence) and `F******` in the captions.
- **Music** from the Music folder gets quieter while someone talks.
- **Hook title** for the first seconds, a **progress bar**, even voice loudness (-14 LUFS), and a
  **cover picture** (`.jpg`) with the title for every reel.
- **Caption styles**: classic (the look above), Hormozi, MrBeast, minimal, karaoke.
- **Layouts**: split (clip on top), floating card on full-screen gameplay, full screen following the
  speaker's face, or side by side (landscape).
- **Translated captions**: extra reels with the captions in other languages (needs the AI helper).

Change any of this in the menu under **Look & features**, or in `config.yaml`.

## Posting

Every finished reel goes to each connected account, spaced out and at good times of day, then its
views and likes are checked a few times so you can see what works. **Each platform gets its own
version**, cut to what it takes and pays for:

| Version | Length | Why |
|---|---|---|
| TikTok | 61-90 s | TikTok's Creator Rewards only pay for videos of 1 minute or longer |
| YouTube Shorts | up to 59 s | A Short over 1 minute with a copyright claim can't earn |
| Instagram, Facebook | up to 90 s | Facebook takes reels of 3-90 s from apps; Instagram gets the same version |

The right length is only part of getting paid: each program also has follower and view thresholds, and
all of them require original content, which reposted clips of other people's podcasts often don't
count as. → [What each platform requires](docs/PLATFORMS.md#getting-paid-what-each-platform-requires)

| Platform | What you get |
|---|---|
| TikTok | Reels arrive in your TikTok inbox; tap *Post* (direct posting needs an app audited by TikTok) |
| Instagram Reels | Posted automatically (Professional account needed) |
| Facebook Reels | Posted to your Page automatically (reels of 3-90 seconds) |
| YouTube Shorts | Uploaded, but **locked as private** until Google audits your project |
| X (Twitter) | Posted automatically (X charges about $0.015 per post) |
| Pinterest | Video Pins on the board you pick (new apps start with limited "trial" access) |
| Snapchat Spotlight | Not possible: Snapchat has no public API for posting |

- **Posting times**: by default 12:00, 17:00 and 20:00, then each account's own best hours once there
  are enough views to learn from (`upload.post_times`).
- **Several accounts**: connect a second YouTube channel or TikTok profile in the menu, then put an
  `accounts.txt` in a clip folder: `youtube = gaming` posts that folder to the "gaming" channel,
  `instagram = off` keeps it off Instagram.
- **Text per platform**: title, caption and hashtags written by the AI helper (or taken from
  `title.txt` / the file name), fitted to each platform (Instagram keeps 5 hashtags, X 2 and 280
  characters). Change the layout of each post in `upload.templates`. A `hashtags.txt` in a clip folder
  adds hashtags for that podcast.
- **Stats**: menu > *Stats* shows views and likes per platform and which podcasts and gameplay videos
  get the most views.

Step-by-step setup for each platform: **[docs/PLATFORMS.md](docs/PLATFORMS.md)**.

## Your phone

- **Telegram or Discord**: each new reel arrives as a small preview. Tap *Post*, *Now* or *Skip*
  (turn on `phone.approval` so nothing is posted without your OK). Send the bot a link to make reels
  from it, `/stats` for the numbers. → [docs/PHONE.md](docs/PHONE.md)
- **Phone dashboard**: menu > *Phone dashboard* shows a QR code. Scan it with a phone on the same Wi-Fi
  to see what's waiting, watch it, approve it, add links and see the stats.

## More

- **Graphics card**: rendering uses an NVIDIA, Intel or AMD graphics card when it works (checked with a
  one-second test), otherwise the processor (`video.codec: auto`).
- **Updates**: the app checks GitHub once a day and offers new versions in the menu
  (`app.auto_update: auto` installs them by itself). Your settings, logins, clips and reels stay.
- **Server mode**: run it on a Linux server, NAS or Raspberry Pi with Docker, and sync folders with
  Google Drive / Dropbox through rclone. → [docs/SERVER.md](docs/SERVER.md)

## Settings (`config.yaml`)

`config.yaml` sits next to `BrainrotBot.exe` (it appears after the first start). Open it with Notepad;
every setting is explained inside. Most can also be changed in the menu. After changing the file,
choose *Stop the bot* then *Start the bot*. Some useful ones:

| Setting | Default | What it does |
|---|---|---|
| `captions.style` | `classic` | `hormozi`, `mrbeast`, `minimal` or `karaoke` replace the look settings. |
| `captions.color` / `stroke_color` | yellow / white | Caption colors (classic style). |
| `captions.max_words` | `3` | Words on screen at once. `1` gives the fast one-word style. |
| `captions.model` | `small` | Speech model. `base` is faster, `medium` or `turbo` are more accurate. |
| `captions.language` | `auto` | Force a language (`en`, `ar`, `fr`...) if detection guesses wrong. |
| `captions.translate_to` | `[]` | For example `[es, ar]`: also make each reel in these languages. |
| `video.layout` | `split` | `floating`, `fullscreen` or `side`. |
| `edit.cut_silences` / `zoom` / `emojis` / `sfx` | on | The editing features. |
| `edit.censor` | off | Hide swear words (`censor_mode: bleep` or `mute`). |
| `moments.min_source_minutes` | `5` | Videos at least this long are cut into their best moments. |
| `moments.count` | `0` | Reels per long video. `0` = about one per 20 minutes (6 from 2 hours). |
| `versions.tiktok` / `youtube` / ... | `61-90` / `3-59` / ... | Length of each platform's version, in seconds. `versions.enabled: false` = one version in `Reels\<podcast>\`. |
| `upload.post_times` | `auto` | `["12:00", "18:30"]` = only then, `[]` = any time. |
| `upload.hours_between_posts` | `3` | At least this long between posts on the same account. |
| `phone.approval` | off | Reels wait for your OK on the phone. |
| `ai.model` | `claude-opus-5` | The Claude model the AI helper uses. |

## Run from the source code (Mac, Linux, Windows)

1. Install **Python 3.10+** and **ffmpeg**:
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg-full`. The plain `ffmpeg` formula can't draw captions; the bot finds
     `ffmpeg-full` by itself.
   - Linux: `sudo apt install ffmpeg`
   - For YouTube links, also install [Deno](https://deno.com) (yt-dlp needs it).
2. In this folder: `pip install -r requirements.txt`
3. `python brainrot.py`. You get the same setup page and menu as the Windows app.

Other commands:

```
python brainrot.py --setup                # run the setup again
python brainrot.py --run                  # just run the bot in this window with a live log (no menu)
python brainrot.py --once                 # download links, process what's waiting, post what's due, then exit
python brainrot.py --clip my.mp4          # make a reel from one video right now (never posted)
python brainrot.py --clip my.mp4 --preview 15   # only the first 15 seconds, to test the look quickly
python brainrot.py --check                # check the setup
python brainrot.py --dashboard            # the phone dashboard link and QR code
python brainrot.py --status / --stop      # is the background bot running? / stop it
python brainrot.py --retry-failed         # try clips that failed again
```

The Windows app accepts the same options: `BrainrotBot.exe --clip my.mp4`.

## Troubleshooting

- **What is it doing?** Menu option *Watch live activity*, or open `logs/bot.log`.
- **A clip failed.** The log says why. It is retried after 1 and 2 minutes, then skipped. After fixing
  the file, save it again (or copy it back in), or use the menu's *Try failed clips again*.
- **A link doesn't download.** The log says why. YouTube needs Deno (included in the Windows app);
  Instagram and private videos need your browser's cookies saved as `cookies.txt` next to `config.yaml`.
- **The phone can't open the dashboard**: the phone must be on the same Wi-Fi, and Windows must allow
  Brainrot Bot on private networks (it asks the first time).
- **Auto-start stopped working after moving the app folder:** run the setup again (menu > Settings).
- **Antivirus or SmartScreen complains:** the app isn't code-signed. It's built from this repository by
  GitHub Actions; see `.github/workflows/windows-app.yml`.
- **Too slow:** use `captions.model: base`, and record gameplay at 1080p instead of 4K. A graphics card
  is used automatically when it works.
- **Wrong words in captions:** set `captions.language`, or use a bigger model (`medium`).

## For developers

```
pip install -r requirements.txt pytest
python -m pytest
```

The code lives in `brainrot_bot/`:

- `app.py` (menu), `wizard.py` (setup page), `dashboard.py` (phone web page), `tray.py`
- `bot.py` (watcher and job flow), `links.py` (yt-dlp), `dedupe.py`, `cloud.py`
- `moments.py` (best moments), `copywriter.py` (hooks and captions), `ai.py` (Claude), `translate.py`
- `analysis.py`, `editor.py`, `faces.py`, `sfx.py`, `styles.py`, `thumbnail.py` (the edit)
- `render.py` (ffmpeg filter graph), `captions.py` (`.ass` styling), `gameplay.py`, `transcribe.py`
- `uploads/` (platform APIs, accounts, posting queue, stats), `phone.py` (Telegram / Discord)
- `service.py` (background mode, start at login), `updater.py`, `gpu.py`, `credentials.py`, `config.py`

`.github/workflows/windows-app.yml` runs the tests on Linux, builds and tests the Docker image, then
builds the Windows app with PyInstaller (`packaging/brainrot.spec`), bundles ffmpeg and Deno, tests the
finished `.exe` end to end on Windows, and publishes `BrainrotBot-windows.zip` as a release whenever the
version in `brainrot_bot/__init__.py` changes on the default branch.
