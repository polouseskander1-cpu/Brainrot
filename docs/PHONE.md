# Your phone: Telegram, Discord and the phone dashboard

Brainrot Bot can send every new reel to your phone, so you can watch it and decide with one tap
whether it gets posted. You can also send it video links from anywhere and ask how the posts are doing.
There are three ways, and you can use any of them together:

| | What you need | Best for |
|---|---|---|
| **Telegram** | The free Telegram app | Approving reels with buttons, sending links from anywhere |
| **Discord** | A Discord server of your own | Same, with reactions (✅ 🚀 ❌) |
| **Phone dashboard** | A phone on the same Wi-Fi as the computer | A small web page: everything waiting, the stats, a box for links |

Connect Telegram or Discord during the setup, or later with the menu option
**Phone (Telegram / Discord)**. The bot tokens are stored in `credentials.dat` (encrypted on Windows),
never in `config.yaml`.

---

## Telegram

1. In Telegram, open **@BotFather** (the official one, with the blue check mark) and send `/newbot`.
2. Choose a name (anything), then a username that ends in `bot`, for example `my_reels_bot`.
3. BotFather answers with a token like `123456789:AAH...`. Paste it into Brainrot Bot.
4. The app shows a link (`https://t.me/your_bot`). Open it on your phone and press **Start**. The bot
   remembers your chat, answers *"Connected!"*, and from then on only talks to you.

Every new reel arrives as a short preview video (or its cover picture) with its title, the clip folder
and where it will be posted:

- **✅ Post**: post it at the next posting time.
- **🚀 Now**: post it right away.
- **⏭ Skip**: never post it (the file stays in your Reels folder).

Without approval mode (below), reels are posted by themselves and the message only has
**⏭ Don't post it**.

**Send a link** (YouTube, TikTok, Instagram, X...) and the bot asks which clip folder (podcast /
influencer) it belongs to, with a button for each folder and **➕ New folder**. The video is downloaded
within a minute and becomes reels like any clip.

**Commands** (also in the bot's menu button):

| Command | What it does |
|---|---|
| `/status` | What the bot is doing, what's waiting |
| `/stats` | Views and likes per platform, the best podcasts and gameplay |
| `/dashboard` | The phone dashboard link |
| `/pause` / `/resume` | Stop posting for now / post again (reels are still made) |
| `/help` | What the bot can do |

## Discord

1. Open [discord.com/developers/applications](https://discord.com/developers/applications) and press
   **New Application**. Give it a name.
2. Open **Bot**, press **Reset Token** and copy the token.
3. On the same page, switch on **Message Content Intent**. Without it the bot can't read the links you send.
4. Open **OAuth2 > URL Generator**. Tick the scope **bot**, then the permissions **View Channels**,
   **Send Messages**, **Attach Files**, **Add Reactions** and **Read Message History**. Open the
   generated link and add the bot to your server.
5. In Discord: **Settings > Advanced > Developer Mode** on. Then right-click the channel the bot should
   use and choose **Copy Channel ID**. A private channel is best.
6. Paste the token and the channel ID into Brainrot Bot. It also asks for **your user ID**
   (right-click your name > **Copy User ID**): with it, only your reactions and messages count. Press
   Enter to skip if you're alone on the server.

Every new reel is posted in the channel with the bot's reactions under it. Click one:

- **✅**: post it at the next posting time.
- **🚀**: post it right away.
- **❌**: don't post it.

**Send a link** in the channel to make reels from it. Put the folder name after the link to choose the
clip folder, for example `https://youtu.be/abc123 Diary Of A CEO`. Without a name it goes to the
`From phone` folder (`phone.link_folder` in `config.yaml`).

**Commands**: write `status`, `stats`, `dashboard`, `pause`, `resume` or `help` in the channel.

Discord bots can send files up to 10 MB, so reels longer than about 45 seconds arrive as their cover
picture instead of a preview video.

## Approval mode

With `phone.approval: true` (the setup asks *"Wait for your OK on the phone before posting each reel?"*),
nothing is posted until you tap **Post** or **Now**. Reels you don't answer simply wait: the menu counts
them as *waiting*, and the phone dashboard lists them so you can still approve them there.

Without approval mode, reels are posted at the usual posting times and the phone message lets you stop
one before it goes out.

Other settings in the `phone:` section of `config.yaml`:

| Setting | Default | What it does |
|---|---|---|
| `approval` | `false` | Reels are only posted after your OK |
| `preview` | `true` | Send a small preview video (otherwise only the cover picture) |
| `notify_posted` | `true` | A message with the link when a post goes live |
| `notify_errors` | `true` | A message when something needs you: a clip failed, a login ran out |
| `link_folder` | `From phone` | Where Discord links without a folder name go |

## The phone dashboard

A small web page served by the bot itself. It shows what's waiting for your OK (with the video),
what was posted and how it did, the best podcasts and gameplay, and a box to paste video links.

1. In the app, choose **Phone dashboard (scan a QR code)**.
2. Scan the QR code with your phone's camera. The phone must be on the **same Wi-Fi** as the computer.
3. Bookmark the page or add it to your home screen.

The link contains a secret key, so other people on your Wi-Fi can't open it without the link. Keep the
link to yourself. It only works at home: the bot is not reachable from the internet, and it shouldn't be.

**It doesn't open?**

- The first time, Windows asks whether Brainrot Bot may use the network. Allow it on **private networks**.
  If you missed it: Windows Security > Firewall & network protection > Allow an app through firewall.
- Your Wi-Fi must be set as a *private* network on the computer, and some guest or office networks
  block devices from seeing each other.
- Another program already uses port 8770: change `dashboard.port` in `config.yaml`.
- To switch the dashboard off: `dashboard.enabled: false`. To allow only this computer: `dashboard.lan: false`.

On a server, see [SERVER.md](SERVER.md#the-phone-dashboard).
