# Brainrot Reel Bot

Drop a podcast or educational clip into a folder and get back a vertical reel: the clip on top, random
gameplay underneath that lasts exactly as long as the clip, and big yellow word-by-word captions.
It runs in the background 24/7 and picks up new clips on its own.

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

## What it does

- **Watches `clips/`** for new subfolders and new clips (every 10 s). A file is only used once it has stopped
  changing, so it never grabs a half-copied video.
- **Picks random gameplay** from `gameplay/`: a random recording, starting at a random point, cut to exactly
  the clip's length. Recordings take turns, so they all get used. If none is long enough, pieces are chained.
- **Writes captions automatically** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) speech
  recognition (offline, free, many languages): 1-3 words at a time, yellow with a white stroke and a light
  drop shadow, popping in as each phrase is spoken.
- **Extra edits for retention:** a title box at the start, a progress bar on the seam, even and
  loud-enough voice (a gentle compressor, then normalization to -14 LUFS, the level TikTok, YouTube and
  Instagram aim for), optional background music, optional word highlighting, optional
  "Part 1/2/3" splitting of long clips.
- **Never re-does work:** finished clips are remembered in `bot_state.json`, so restarts are safe. A clip that
  keeps failing is retried a few times, then skipped. One bad file never stops the bot.

## Setup (once)

1. **Python 3.9 or newer.** Windows: get it from [python.org](https://www.python.org/downloads/) and tick
   *"Add python.exe to PATH"* during install.
2. **ffmpeg** (does the video editing):
   - Windows: `winget install Gyan.FFmpeg`, then open a **new** terminal window.
   - macOS: `brew install ffmpeg-full`. The plain `ffmpeg` formula can't draw captions. The bot finds
     `ffmpeg-full` by itself.
   - Linux: `sudo apt install ffmpeg` (or your distro's package).
3. Download this folder (green *Code* button → *Download ZIP*, or `git clone`), open a terminal inside it and run:
   ```
   pip install -r requirements.txt
   python brainrot.py --check
   ```
   `--check` tells you if anything is missing.

## Use it

1. Put some gameplay recordings in **`gameplay/`**.
2. Start the bot: double-click **`run_bot.bat`** (Windows), run **`./run_bot.sh`** (macOS/Linux), or type
   `python brainrot.py`.
3. Make a folder inside **`clips/`**, for example `clips/my_podcast_ep12/`, and drop your clips into it.
4. Finished reels appear in **`output/my_podcast_ep12/`**, one reel per clip, each with a `.srt` subtitle
   file next to it.

The very first clip takes longer because the speech model is downloaded once (about 500 MB).
After that, a 1-minute clip typically takes 1-2 minutes on a normal PC.

**Optional title:** put a text file next to a clip with the same name (`clip1.mp4` → `clip1.txt`), or a
`title.txt` in the folder to use the same title for every clip in it. The text is shown in a white box for
the first 4 seconds. A strong hook ("He lost $2M in one day...") keeps people watching.

**Optional music:** drop some tracks in `music/`. One is picked at random and mixed quietly under the voice.

## Run it 24/7

The bot uses low CPU priority so your PC stays usable. It only works while the computer is awake, so
set your power settings to never sleep.

**Windows:** press `Win + R`, type `shell:startup`, press Enter, and put a *shortcut* to `run_bot.bat` in
the folder that opens. In the shortcut's properties you can set *Run: Minimized*. The bot then starts when
you log in and restarts itself if it ever crashes.

**macOS:** in Terminal, inside this folder, run `cp run_bot.sh run_bot.command && chmod +x run_bot.command`.
Then add `run_bot.command` under *System Settings → General → Login Items*.

**Linux (systemd):** create `~/.config/systemd/user/brainrot.service`:

```ini
[Unit]
Description=Brainrot reel bot

[Service]
WorkingDirectory=/path/to/Brainrot
ExecStart=/usr/bin/python3 brainrot.py
Restart=on-failure
SuccessExitStatus=130

[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now brainrot` and `loginctl enable-linger $USER` (keeps it running
while you're logged out). Follow along with `journalctl --user -u brainrot -f`.

## Change the look (`config.yaml`)

Every setting is explained in `config.yaml`. Restart the bot after changing it. The most useful ones:

| Setting | Default | What it does |
|---|---|---|
| `video.width` / `video.height` | `1080` / `1920` | Output size. Swap them for a 16:9 landscape video. |
| `video.top_ratio` | `0.3333` | Share of the screen for the clip (the rest is gameplay). |
| `captions.color` / `stroke_color` | yellow / white | Caption colors. |
| `captions.max_words` | `3` | Words on screen at once. `1` gives the fast one-word-at-a-time style. |
| `captions.font_size` / `position` | `100` / `0.5` | Caption size and height (0 = top, 1 = bottom). |
| `captions.highlight_color` | off | For example `"#00FF66"` lights up each word as it's spoken. |
| `captions.model` | `small` | Speech model. `base` is faster, `medium` or `turbo` are more accurate. |
| `captions.language` | `auto` | Force a language (`en`, `ar`, `fr`...) if detection guesses wrong. |
| `hook.duration` | `4` | Seconds the title box stays (0 = the whole video). |
| `parts.max_seconds` | `0` (off) | For example `90`: longer clips are cut at pauses into Part 1/2/3 reels. |
| `gameplay.skip_start` | `0` | Skip the first seconds of each recording (loading screens). |
| `gameplay.mirror` | `false` | Randomly mirror gameplay so reused footage looks fresh. |
| `video.codec` | `libx264` | `h264_nvenc` (NVIDIA), `h264_qsv` (Intel) or `h264_videotoolbox` (Mac) render much faster. |

The caption font is Montserrat Black (included in `fonts/`, free license). To use another font, set
`captions.font` to any font installed on your computer, or drop a `.ttf` file into `fonts/` and use its name.

## Handy commands

```
python brainrot.py                        # watch for new clips forever (normal mode)
python brainrot.py --once                 # process what's waiting, then exit
python brainrot.py --clip my.mp4          # make a reel from one video right now
python brainrot.py --clip my.mp4 --preview 15   # only the first 15 seconds, to test the look quickly
python brainrot.py --check                # check the setup
python brainrot.py --retry-failed         # try clips that failed again
```

## Troubleshooting

- **What is it doing?** Everything is printed in the window and saved in `logs/bot.log`.
- **A clip failed.** The log says why. It is retried after 1 and 2 minutes, then skipped. After fixing
  the file, save it again (or copy it back in) and it is picked up again. Or run `--retry-failed`.
- **"ffmpeg can't draw captions"**: your ffmpeg was built without libass. Use the install commands above.
  On macOS that means `ffmpeg-full`, not `ffmpeg`.
- **Too slow:** use `captions.model: base`, a hardware `video.codec` (see table), and record gameplay at
  1080p instead of 4K. With an NVIDIA GPU, speech recognition uses it automatically when CUDA works.
- **Wrong words in captions:** set `captions.language`, or use a bigger model (`medium`).
- **Arabic or another script:** captions work in any language. If the letters look like a different font,
  set `captions.font` to a font that has that script (for example `Arial` or `Tahoma` on Windows).
- **Start over for a clip:** stop the bot, delete its entry from `bot_state.json` (or the whole file), and
  start again.

## For developers

```
pip install pytest
python -m pytest
```

The code lives in `brainrot_bot/`: `bot.py` (watcher and job flow), `render.py` (ffmpeg filter graph),
`captions.py` (grouping and `.ass` styling), `gameplay.py` (random footage picking), `transcribe.py`
(faster-whisper), `parts.py` (splitting), `media.py` (ffmpeg/ffprobe helpers), `config.py` (settings).
