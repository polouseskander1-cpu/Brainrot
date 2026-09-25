# Brainrot Reel Bot

Drop podcast or educational clips into a folder and get back vertical reels: the clip on top,
random gameplay underneath that lasts exactly as long as the clip, and big yellow word-by-word
captions. It runs in the background 24/7, and it can post every reel to TikTok, Instagram,
Facebook and YouTube for you.

```
┌────────────────────────┐
│                        │
│     your clip          │  top third, shown whole (never cropped)
│                        │
├████████████░░░░░░░░░░░░┤  progress bar that fills up as the reel plays
│  ┌──────────────────┐  │
│  │ optional title   │  │  first 4 seconds
│  └──────────────────┘  │
│                        │
│    YELLOW CAPTIONS     │  white outline, soft drop shadow, pop-in animation
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

On Mac or Linux, [run it from the source code](#run-from-the-source-code-mac-linux-windows) instead.

## The setup page

The first time, the app asks four questions in its window:

1. **How should the bot run?** *24/7 in the background* keeps working after you close the window.
   *Only while this window is open* stops when you close it.
2. **Start with your computer?** If yes, the bot starts by itself about 15 seconds after you log in.
3. **Auto-posting.** For each of YouTube Shorts, TikTok, Instagram Reels and Facebook Reels: connect
   it or skip it. You can connect more later.
   → [How to get the keys for each platform](docs/PLATFORMS.md)
4. **Your folders.** Press Enter to use the suggested ones, in `Videos\Brainrot Bot`.

At the end it shows the two folders you need:

- **Gameplay**: paste your gameplay recordings here (Minecraft, parkour, Subway Surfers...).
- **Clips**: make **one subfolder per podcast / influencer / topic** and paste the clips inside.
  Every video in those subfolders becomes a reel in the **Reels** folder, with the same subfolder name.

After that, double-clicking the app shows a small menu: what the bot is doing, how many reels it
made, what is waiting to be posted, and options to open the folders, watch the live activity,
connect accounts, change settings, or stop and start the bot.

The very first clip takes a few extra minutes, because the speech-recognition model (about 500 MB)
is downloaded once. After that, a 1-minute clip takes about 1-2 minutes on a normal PC.

## What it does to every clip

- **Gameplay picked at random**: a random recording, starting at a random point, cut to exactly
  the clip's length. Recordings take turns so they all get used. Short recordings are chained if needed.
- **Captions written automatically** by offline speech recognition (free, many languages): 1-3 words
  at a time, yellow with a white stroke and a light drop shadow, popping in as each phrase is said.
- **Title box** for the first 4 seconds. Put a `.txt` file next to a clip with the same name
  (`clip1.mp4` → `clip1.txt`), or a `title.txt` in the folder for every clip in it. A strong hook keeps
  people watching.
- **Progress bar** on the seam, **even voice volume** (compressed and normalized to -14 LUFS, the
  level TikTok, YouTube and Instagram aim for), optional **background music** from the `Music`
  folder, optional **word highlighting**, optional **Part 1/2/3** splitting of long clips at pauses.
- **Never loses work.** Half-copied files are never used. Finished clips are remembered, so
  restarts are safe. Stopping in the middle of a reel just finishes it next time. A bad file is
  retried a few times, then skipped. One bad file never stops the bot.

## Auto-posting

Every finished reel goes to each connected platform, spaced out (every 3 hours per platform by
default) so the platforms don't treat you as spam. Failed posts are retried. If a login expires,
the menu shows *needs login* and the reels wait until you reconnect.

| Platform | What you get |
|---|---|
| TikTok | Reels arrive in your TikTok inbox; tap *Post* (direct posting needs an app audited by TikTok) |
| Instagram Reels | Posted automatically (Professional account needed) |
| Facebook Reels | Posted to your Page automatically (reels of 3-90 seconds) |
| YouTube Shorts | Uploaded, but **locked as private** until Google audits your project |

Step-by-step setup for each one: **[docs/PLATFORMS.md](docs/PLATFORMS.md)**.

## Change the look (`config.yaml`)

`config.yaml` sits next to `BrainrotBot.exe` (it appears after the first start). Open it with Notepad;
every setting is explained inside. After changing it, choose *Stop the bot* then *Start the bot* in
the menu. The most useful settings:

| Setting | Default | What it does |
|---|---|---|
| `captions.color` / `stroke_color` | yellow / white | Caption colors. |
| `captions.max_words` | `3` | Words on screen at once. `1` gives the fast one-word-at-a-time style. |
| `captions.font_size` / `position` | `100` / `0.5` | Caption size and height (0 = top, 1 = bottom). |
| `captions.highlight_color` | off | For example `"#00FF66"` lights up each word as it's spoken. |
| `captions.model` | `small` | Speech model. `base` is faster, `medium` or `turbo` are more accurate. |
| `captions.language` | `auto` | Force a language (`en`, `ar`, `fr`...) if detection guesses wrong. |
| `video.width` / `video.height` | `1080` / `1920` | Output size. Swap them for a 16:9 landscape video. |
| `video.top_ratio` | `0.3333` | Share of the screen for the clip (the rest is gameplay). |
| `hook.duration` | `4` | Seconds the title box stays (0 = the whole video). |
| `parts.max_seconds` | `0` (off) | For example `90`: longer clips are cut at pauses into Part 1/2/3 reels. |
| `gameplay.skip_start` | `0` | Skip the first seconds of each recording (loading screens). |
| `upload.hours_between_posts` | `3` | Space between posts on each platform. |
| `upload.hashtags` | `#fyp #viral #podcast` | Added to every post (plus `hashtags.txt` in a clip folder). |
| `video.codec` | `libx264` | `h264_nvenc` (NVIDIA), `h264_qsv` (Intel) or `h264_videotoolbox` (Mac) render much faster. |

The caption font is Montserrat Black (included, free license). To use another font, set
`captions.font` to any font installed on your computer.

## Run from the source code (Mac, Linux, Windows)

1. Install **Python 3.9+** and **ffmpeg**:
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg-full`. The plain `ffmpeg` formula can't draw captions; the bot finds
     `ffmpeg-full` by itself.
   - Linux: `sudo apt install ffmpeg`
2. In this folder: `pip install -r requirements.txt`
3. `python brainrot.py`. You get the same setup page and menu as the Windows app.

Other commands:

```
python brainrot.py --setup                # run the setup again
python brainrot.py --run                  # just run the bot in this window with a live log (no menu)
python brainrot.py --once                 # process what's waiting, post what's due, then exit
python brainrot.py --clip my.mp4          # make a reel from one video right now (never posted)
python brainrot.py --clip my.mp4 --preview 15   # only the first 15 seconds, to test the look quickly
python brainrot.py --check                # check the setup
python brainrot.py --status / --stop      # is the background bot running? / stop it
python brainrot.py --retry-failed         # try clips that failed again
```

The Windows app accepts the same options: `BrainrotBot.exe --clip my.mp4`.

On Linux servers without a desktop, use a systemd user service running `python3 brainrot.py --run`
(`Restart=on-failure`, `SuccessExitStatus=130`) instead of the start-at-login option.

## Troubleshooting

- **What is it doing?** Menu option *Watch live activity*, or open `logs/bot.log`.
- **A clip failed.** The log says why. It is retried after 1 and 2 minutes, then skipped. After fixing
  the file, save it again (or copy it back in) and it is picked up again, or use the menu's
  *Try failed clips again*.
- **Auto-start stopped working after moving the app folder:** run the setup again (menu > Settings),
  so Windows learns the new location.
- **Antivirus or SmartScreen complains:** the app isn't code-signed. It's built from this repository by
  GitHub Actions; see the workflow in `.github/workflows/windows-app.yml`.
- **"ffmpeg can't draw captions"** (source install only): your ffmpeg was built without libass. Use the
  install commands above.
- **Too slow:** use `captions.model: base`, a hardware `video.codec` (see table), and record gameplay
  at 1080p instead of 4K. With an NVIDIA GPU, speech recognition uses it automatically when CUDA works.
- **Wrong words in captions:** set `captions.language`, or use a bigger model (`medium`).
- **Arabic or another script:** captions work in any language. If the letters look like a different
  font, set `captions.font` to a font that has that script (for example `Arial` or `Tahoma` on Windows).

## For developers

```
pip install -r requirements.txt pytest
python -m pytest
```

The code lives in `brainrot_bot/`:

- `app.py` (menu) and `wizard.py` (setup page)
- `bot.py` (watcher and job flow)
- `render.py` (ffmpeg filter graph)
- `captions.py` (grouping and `.ass` styling)
- `gameplay.py` (random footage picking)
- `transcribe.py` (faster-whisper)
- `uploads/` (platform APIs and posting queue)
- `service.py` (background mode, start at login)
- `credentials.py` (encrypted keys)
- `config.py` (settings)

The Windows app is built by `.github/workflows/windows-app.yml`. It builds with PyInstaller
(`packaging/brainrot.spec`), bundles ffmpeg, runs the tests and an end-to-end test of the finished
`.exe` on Windows, and publishes `BrainrotBot-windows.zip` as a release whenever the version in
`brainrot_bot/__init__.py` changes on the default branch (or a tag like `v1.2.0` is pushed).
