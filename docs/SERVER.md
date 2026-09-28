# Running Brainrot Bot on a server

Instead of a Windows PC, the bot can run on any Linux machine that stays on: a home server, a NAS, a
Raspberry Pi 4 or 5 (64-bit), or a small cloud server. It runs in Docker, restarts by itself, and keeps
everything (settings, logins, clips, gameplay, reels, the speech model) in one `data` folder.

There's no window and no tray icon on a server. You control it with a few commands, your phone
([Telegram or Discord](PHONE.md)) and the phone dashboard.

## What you need

- Docker with the compose plugin ([docs.docker.com/engine/install](https://docs.docker.com/engine/install/)).
- About 2 GB of free memory and 3 GB of disk for the program and the speech model, plus room for videos.
- A 64-bit system (x86-64 or ARM64). On a Raspberry Pi use the 64-bit Raspberry Pi OS.

Rendering is done by the processor. A 1-minute reel takes a few minutes on a normal server and longer on
a Raspberry Pi; there, set `captions.model: base` in `data/config.yaml` to make speech recognition faster.

## Set it up

```
git clone https://github.com/polouseskander1-cpu/Brainrot.git
cd Brainrot
```

Open `docker-compose.yml` and change two lines:

- `TZ`: your time zone, for example `America/New_York`, so posts go out at the right times of day.
- `BRAINROT_PUBLIC_URL`: remove the `#` and put the server's address on your home network, for example
  `http://192.168.1.50:8770`. This is only used to show you the right phone dashboard link.

Then create the `data` folder yourself, run the setup once, and start the bot:

```
mkdir data
docker compose run --rm brainrot --setup
docker compose up -d
```

Creating `data` yourself matters: the bot then runs as **your** user, so every file it makes (settings,
reels, folders) is yours and you can add, edit and delete files without `sudo`. If Docker creates the
folder instead, it belongs to root and so does everything in it; fix that any time with
`sudo chown -R $USER: data` followed by `docker compose restart`.

The setup asks the same questions as the Windows app, except how to run: on a server the bot always
runs 24/7 and Docker starts it again after a reboot. Press Enter at the folder questions to keep the
suggested folders. Building the image the first time takes a few minutes.

## Adding videos

The folders are inside `data` next to `docker-compose.yml`:

| Folder | What goes there |
|---|---|
| `data/gameplay` | Your gameplay recordings |
| `data/clips/<podcast name>/` | The clips, one subfolder per podcast / influencer / topic |
| `data/output/<podcast name>/` | The finished reels and their cover pictures |
| `data/music` | Optional background music |

Copy files in with whatever you like (`scp`, a network share, Syncthing...), send a video link to the
phone bot, or put links in a `links.txt` file inside a clip folder. To use Google Drive or Dropbox
folders instead, see [Cloud folders](#cloud-folders-google-drive-dropbox) below.

After editing `data/config.yaml` (with any text editor), run `docker compose restart`.

## Logging in to the platforms

Logins that need a browser (YouTube, TikTok, X, Pinterest) work differently on a server, because it has
no browser of its own:

1. The setup prints a long link. Open it on your phone or computer and log in as usual.
2. At the end, the browser tries to open an address like `http://127.0.0.1:8765/callback/?code=...` and
   shows an error page. That's expected: that address means "this computer", and the bot isn't there.
3. Copy the **whole address** from the address bar and paste it into the setup.

For YouTube, first copy the JSON file you download from Google into the `data` folder, then type its
path as the container sees it, for example `/data/client_secret.json`, when the setup asks for it.

Instagram and Facebook use pasted tokens anyway, so nothing changes for them. The setup steps for each
platform are in [PLATFORMS.md](PLATFORMS.md). To connect accounts later, stop the bot, run the setup
again, and start it:

```
docker compose down
docker compose run --rm brainrot --setup
docker compose up -d
```

## Everyday commands

Run these in the folder with `docker-compose.yml`:

```
docker compose logs -f --tail 50                     # what the bot is doing (Ctrl+C to stop watching)
docker compose run --rm brainrot --dashboard         # the phone dashboard link and QR code
docker compose run --rm brainrot --check             # check the setup
docker compose run --rm brainrot --clip "/data/clips/My Podcast/clip.mp4" --preview 15   # test the look (lands in data/output)
docker compose restart                               # after changing data/config.yaml
docker compose down                                  # stop the bot
docker compose up -d                                 # start it again
```

The full log is also in `data/logs/bot.log`.

**Updating to a new version:**

```
git pull
docker compose up -d --build
```

The bot tells you in the log (and on your phone, when it's connected) when a new version is out.

## The phone dashboard

The container shares port 8770 with your home network. Get the link with
`docker compose run --rm brainrot --dashboard` and scan the QR code with your phone. The link contains a
secret key; keep it to yourself.

The dashboard is plain HTTP and meant for your **home network only**. Don't forward port 8770 on your
router. On a cloud server, either block port 8770 in the firewall and use Telegram or Discord only, or
reach it privately, for example with [Tailscale](https://tailscale.com) on the server and your phone
(then use the server's Tailscale address in `BRAINROT_PUBLIC_URL`). To turn it off, set
`dashboard.enabled: false` in `data/config.yaml` and remove the `ports:` lines from `docker-compose.yml`.

## Cloud folders (Google Drive, Dropbox)

The bot can keep its folders in sync with a cloud drive through [rclone](https://rclone.org), which is
included in the image. Then you can add clips from your phone's Drive or Dropbox app and watch the reels
there.

1. Connect your drive (the login is saved in `data/rclone.conf`):

   ```
   docker compose run --rm --entrypoint rclone brainrot config
   ```

   Choose `n` (new remote), name it `gdrive`, pick **Google Drive** (or Dropbox, OneDrive...) and keep the
   defaults. When rclone asks *"Use web browser to automatically authenticate?"*, answer `n` and follow
   its instructions: it asks you to run an `rclone authorize` command on a computer with a browser and
   paste the answer back.

2. In the drive, create a folder `Brainrot` with the subfolders `Clips` and `Gameplay`.

3. In `data/config.yaml`, set:

   ```yaml
   cloud:
     remote: "gdrive:Brainrot"
     every_minutes: 5
   ```

4. `docker compose restart`

Every 5 minutes the bot copies new files from `Brainrot/Clips` and `Brainrot/Gameplay` down, and the
finished reels up to `Brainrot/Reels`. Files are only added, never overwritten or deleted, on either side.
The same works on a Mac or Linux computer without Docker when rclone is installed. On Windows and Mac,
simply keep the folders inside the Google Drive or Dropbox folder: the setup offers that.

## Without Docker

On a Linux machine with Python 3.10+, ffmpeg and Deno installed (see the main README), you can also run
the bot directly. Set `BRAINROT_SERVER=1` so logins use the copy-the-address method:

```
BRAINROT_SERVER=1 python brainrot.py --setup
BRAINROT_SERVER=1 python brainrot.py --run
```

To keep it running, use a systemd service or `tmux`.
