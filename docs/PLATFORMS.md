# Connecting TikTok, Instagram, Facebook, YouTube, X and Pinterest

Brainrot Bot can post every finished reel for you. Each platform needs a one-time setup on its
developer website, because the platforms only let apps post through their official APIs. Everything
below is free, except X, which charges a small amount per post.

Open Brainrot Bot and choose **Connect accounts / auto-posting** in the menu (or answer the questions
in the first setup), then follow the steps for the platforms you want. You can skip any of them;
skipped platforms are simply not posted to.

Your keys and logins are saved in `credentials.dat` next to the app. On Windows that file is encrypted
with your Windows account, so it's useless if copied to another computer. They are never put in
`config.yaml`.

On a server without a browser, the logins work slightly differently: see
[SERVER.md](SERVER.md#logging-in-to-the-platforms).

**What to expect, in short**

| Platform | Works right away? | The catch |
|---|---|---|
| TikTok | Yes, as **drafts**: each reel lands in your TikTok inbox and you tap *Post* | Posting directly (no tap) needs TikTok to audit your app. Only reels of 1 minute or longer are made for TikTok |
| Instagram Reels | Yes | Needs a Professional (Business or Creator) account. Max 100 posts a day |
| Facebook Reels | Yes | Posts to a Facebook **Page**. Reels must be 3 to 90 seconds. Max 30 a day |
| YouTube Shorts | Uploads work, but they are **locked as private** | Google must audit your project before uploads can be public |
| X (Twitter) | Yes | Pay-per-use API: about $0.015 per post. Videos up to 140 seconds |
| Pinterest | Yes, with **trial access** | Pinterest must approve your app (standard access) before pins go public |
| Snapchat Spotlight | **No** | Snapchat has no public API for posting to Spotlight. Upload those from the Snapchat app |

## A version for each platform

Each platform gets its own version of every reel, cut to the length that platform takes and pays for,
in its own folder: `Reels\TikTok\<podcast>\`, `Reels\YouTube\<podcast>\`, `Reels\Instagram\<podcast>\`,
`Reels\Facebook\<podcast>\`. Upload from those folders yourself, or let the bot post each one to its platform.

| Platform | Length (after pauses are cut) | Why |
|---|---|---|
| TikTok | 61-90 seconds | The Creator Rewards Program only pays for videos of **1 minute or longer** |
| YouTube Shorts | up to 59 seconds | Shorts can run 3 minutes, but a Short over 1 minute with a copyright claim can't earn |
| Instagram | up to 90 seconds (the long version when there is one) | Instagram shows reels up to 3 minutes to new viewers; the same version as Facebook keeps it simple |
| Facebook | up to 90 seconds (the long version when there is one) | Facebook only takes reels of 3-90 seconds from apps |

Usually that's two renders: a long version (TikTok, Instagram, Facebook) and a shorter one for YouTube,
taken from the same moment, so the viral part is in both. A clip under a minute gets no TikTok version,
because TikTok wouldn't pay for it; the log says so. Change the lengths, or switch this off, under
`versions:` in `config.yaml`, or in the menu under **Look & features**.

## Getting paid: what each platform requires

The bot makes every version the right length and format, which is the part it can control. Whether a
platform actually pays you also depends on your account, and on originality rules no editing tool can
guarantee:

| Platform | Program | You also need |
|---|---|---|
| TikTok | [Creator Rewards Program](https://www.tiktok.com/legal/page/global/tiktok-creator-rewards-program-eea/en) | 10,000 followers and 100,000 views in the last 30 days, 18+, a personal (not Business) account, a country where it runs. Each video needs 1,000 qualified views, and has to be original or "add new ideas to preexisting content" |
| YouTube | [Partner Program, Shorts revenue](https://support.google.com/youtube/answer/12504220) | 1,000 subscribers and 10 million Shorts views in 90 days (or 4,000 watch hours). Reuploads of other creators' content and compilations without original content added can't earn |
| Facebook | [Content Monetization](https://creators.facebook.com/introducing-facebook-content-monetization) | Facebook's eligibility (followers and minutes watched). Reposting other creators' content, even with captions, borders or speed changes added, counts as [unoriginal](https://about.fb.com/news/2026/03/rewarding-original-creators-on-facebook/): less reach, and pages that mostly post it lose monetization |
| Instagram | No pay-per-view program | Bonuses are invite-only; gifts and subscriptions need followers; Meta's originality rules apply here too |

**About clips of other people's podcasts.** All three companies say that reposting someone else's
video with light edits (captions, gameplay underneath, a new speed) isn't original content. Channels
built only on that are the ones most often refused or demonetized, and a podcast whose owner uses
YouTube's Content ID can claim your Short (the money then goes to them). What helps:

- clip podcasts you own, or that allow clipping (many podcasters are happy to be clipped; ask);
- add something of your own: your commentary or reaction, on screen or as a voice-over;
- credit the source in the caption.

## When and what gets posted

- **Posting times.** By default (`upload.post_times: auto`) each account posts at 12:00, 17:00 and 20:00.
  Once an account has about 8 posts with views, the bot switches to that account's own best hours.
  Set a list like `["12:00", "18:30"]` to post only then, or `[]` to post as soon as possible.
- **Spacing.** At least 3 hours between two posts on the same account (`upload.hours_between_posts`).
  Posting 20 reels at once hurts reach and can trip the platforms' spam limits.
- **The text.** Each post gets a title, a caption and hashtags. With the AI helper, Claude writes them
  from what is said in the clip. Without it, the bot uses the clip's strongest line. A
  `<clip name>.txt` file next to a clip, or a `title.txt` in its folder, sets the title yourself.
- **Hashtags.** `upload.hashtags` in `config.yaml` goes on every post, and a `hashtags.txt` in a clip
  folder adds hashtags for that podcast or influencer. Instagram keeps at most 5 hashtags and X 2, as
  those platforms recommend. X posts are also cut to 280 characters.
- **Your own layout per platform.** `upload.templates` in `config.yaml` sets the text of each post, with
  `{title}`, `{caption}` and `{hashtags}` filled in. For example `instagram: "{caption}\n.\n.\n{hashtags}"`.
- **Approval on your phone.** With `phone.approval: true`, a reel is only posted after you tap *Post* in
  Telegram or Discord. See [PHONE.md](PHONE.md).

---

## TikTok

1. Go to [developers.tiktok.com](https://developers.tiktok.com/), log in, and open **Manage apps > Connect an app**.
2. Fill in the app details. The description can be *"Posts my own videos to my TikTok account"*.
3. Add the products **Login Kit** and **Content Posting API**. For views and likes in the stats, also
   add **Display API**.
4. In **Login Kit**, choose the **Desktop** platform and add this redirect URI exactly:
   `http://127.0.0.1:8765/callback/`
5. In the scopes list, make sure **user.info.basic** and **video.upload** are included. Add
   **video.publish** only if you want direct posting (see below), and **video.list** for the stats.
6. Either **submit the app for review**, or, to start right away, open the app's **Sandbox**, add
   your own TikTok account under *Target users*, and use the sandbox keys.
7. Copy the **Client key** and **Client secret** into Brainrot Bot, and answer *yes* to *"Also read the
   views and likes"* if you added the Display API. A browser window opens: log in to TikTok and allow access.

**Drafts vs direct posting.** In *draft* mode (the default), every reel is sent to your TikTok inbox.
Open TikTok, tap the notification, and post. You can add a trending sound first, which helps
reach. *Direct* mode posts on its own, but TikTok only allows that for apps that passed its audit.
Unaudited apps can only post directly to **private** accounts. TikTok also allows at most 5 drafts
waiting in your inbox per 24 hours.

## Instagram Reels

Your Instagram account must be **Professional** (Business or Creator). You can switch for free in the
Instagram app: Settings > Account type and tools.

1. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps) and **Create app**. Pick
   type **Business**.
2. Add the **Instagram** product and open **API setup with Instagram login**.
3. Under *Generate access tokens*, **add your Instagram account** and log in when asked. Allow
   **instagram_business_manage_insights** too if you want views, shares and saves in the stats (likes and
   comments work without it).
4. Press **Generate token** and copy it.
5. Paste the token into Brainrot Bot. It checks the token and shows your @username.

The token lasts 60 days. The bot renews it by itself every week, so as long as the bot runs now and
then it never expires. If it does expire, connect Instagram again.

## Facebook Reels (Pages)

Reels go to a Facebook **Page** you manage, not to a personal profile. Facebook only accepts reels
of **3 to 90 seconds** from apps, so the Facebook version is never longer than 90 seconds.

1. In [developers.facebook.com/apps](https://developers.facebook.com/apps), open your **Business** app
   (you can reuse the Instagram one). Under **App settings > Basic**, copy the **App ID** and **App secret**.
2. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/) and pick your app at
   the top right.
3. Under *Permissions*, add **pages_show_list**, **pages_read_engagement** and **pages_manage_posts**.
4. Press **Generate Access Token**, allow access, and copy the token.
5. Paste the App ID, App secret and token into Brainrot Bot, then pick your Page from the list.

The bot turns this into a Page login that doesn't expire.

## YouTube Shorts

**Read this first.** YouTube locks every upload made through the API as **private** until your Google
Cloud project passes YouTube's API compliance audit. Locked videos **can't be made public**, not even
in YouTube Studio. The [audit](https://support.google.com/youtube/contact/yt_api_form) is free, but it
asks for a privacy policy and terms of service and can take weeks, with no guarantee. Until you have
it, the simplest path is to skip YouTube in the bot and upload the reels from your Reels folder yourself.

If you want to set it up (for example while you wait for the audit):

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) with the Google account that owns
   your channel, and create a project.
2. **APIs & Services > Library**: search **YouTube Data API v3** and press **Enable**.
3. **Google Auth Platform** (OAuth consent screen): press *Get started*, enter an app name and your
   email, and choose **External**.
4. **Audience**: press **Publish app**. If you leave it in *Testing*, Google ends the login after
   7 days and you would have to reconnect every week.
5. **Clients > Create client**, type **Desktop app**, then **Download JSON**.
6. In Brainrot Bot, drag the downloaded JSON file into the window and press Enter. A browser opens:
   choose your account. Google warns *"Google hasn't verified this app"*, because it's your own new
   app. Click **Advanced > Go to (your app name)** and allow access.

YouTube treats vertical videos up to 3 minutes as Shorts. The bot adds `#shorts` to the title.

## X (Twitter)

X's API is **pay-per-use**: there is no monthly fee, you buy a little credit and each post costs about
$0.015 (check the current price in the X developer console). Reading a post's views and likes later
costs a little too, so the bot does it only twice per post (after a day and after a week). Videos can
be up to 140 seconds.

1. Go to [console.x.com](https://console.x.com/), sign in, sign up for API access (pay-per-use) and
   add some credit.
2. **Create an app**. In its **User authentication settings**, choose **OAuth 2.0**, app type
   **Web App, Automated App or Bot**, and permissions **Read and write**.
3. Add this **Callback URI** exactly: `http://127.0.0.1:8766/callback/`. The *Website URL* can be any
   page of yours, for example your X profile.
4. Under **Keys and tokens**, copy the **OAuth 2.0 Client ID** and **Client Secret**.
5. Paste them into Brainrot Bot. A browser opens: log in to X with the account the reels should go to,
   and press **Authorize app**.

The bot renews the login by itself. If X ever ends it, the menu shows *needs login*: connect X again.

## Pinterest

Reels become **video Pins** on a board you choose. You need a (free) Pinterest **business** account;
you can convert a personal one in Pinterest's settings.

1. Go to [developers.pinterest.com](https://developers.pinterest.com/), open **My apps** and
   **Connect app**. Fill in the short form about your app.
2. In the app's settings, add this **redirect URI** exactly: `http://localhost:8767/callback/`
3. Copy the **App ID** and **App secret key** into Brainrot Bot. A browser opens: log in to Pinterest
   and allow access.
4. Pick the board for the pins, or let the bot make a new one.

**Trial access.** New Pinterest apps start with *trial access*, which is limited while Pinterest reviews
the app. Request **standard access** on your app's page so your pins can go public. The login renews
itself (it lasts 60 days after the last use), so it only runs out if the bot is off for two months.

---

## Several accounts per platform

You can connect more than one account per platform, for example a second YouTube channel for one
podcast, or a separate TikTok profile for gaming clips.

1. In the menu, choose **Connect accounts / auto-posting**, and at the end answer *yes* to
   *"Add another account"*.
2. Pick the platform, give the account a short name (like `gaming`), and log in with that account.
3. Pick the clip folders that should post to it.

This writes an `accounts.txt` in each folder you picked. You can also write it yourself:

```
youtube = gaming       # this folder posts to the YouTube account called "gaming"
tiktok = main          # the first TikTok account you connected
instagram = off        # never post this folder on Instagram
```

Folders without an `accounts.txt`, and platforms not listed in it, use the first account of each
platform.

## Views and likes (stats)

After posting, the bot checks each post's views and likes a few times (after 2 hours, 1 day, 3 days,
1 week and 1 month) and shows the totals in the menu under **Stats**, per platform and per podcast and
gameplay video, so you can see what works. The best posting hours are learned from these numbers.
Turn it off with `upload.stats: false`.

| Platform | What's read | Needs |
|---|---|---|
| TikTok | Views, likes, comments, shares | The Display API product and **video.list** (optional when connecting) |
| Instagram | Likes, comments; views, shares, saves | **instagram_business_manage_insights** for the views |
| Facebook | Views, likes, comments | Nothing extra |
| YouTube | Views, likes, comments | Nothing extra |
| X | Views, likes, reposts, replies | Nothing extra (costs a little per read) |
| Pinterest | Views, saves, reactions, comments | Nothing extra |

## Snapchat

Snapchat doesn't offer a public API for posting to Spotlight or Stories from other apps, so the bot
can't post there. The reels in your Reels folder are ready for Spotlight as they are: upload them from
the Snapchat app.

---

## When something goes wrong

- The bot's window and `logs/bot.log` say exactly what the platform answered.
- **"needs login"** in the menu: the platform ended the login (password change, expired token,
  access removed). Choose *Connect accounts* and log in again. Reels waiting for that platform are
  posted after that; nothing is lost. With Telegram or Discord connected, you also get a message.
- A platform limit was reached (daily cap, too many drafts): the bot waits and tries again later
  by itself.
- **The browser says it can't reach 127.0.0.1 or localhost** at the end of a login: check that the
  redirect / callback URI in your developer app matches the one above exactly, including the `/` at
  the end. If another program uses that port, change `tiktok_redirect_port`, `x_redirect_port` or
  `pinterest_redirect_port` in `config.yaml` and the URI in your app to match.
