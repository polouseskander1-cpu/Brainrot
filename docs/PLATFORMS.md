# Connecting YouTube, TikTok, Instagram and Facebook

Brainrot Bot can post every finished reel for you. Each platform needs a one-time setup on its
developer website, because the big platforms only let apps post through their official APIs.
Everything below is free.

Open Brainrot Bot, choose **5) Connect accounts / auto-posting**, and follow the steps for the
platforms you want. You can skip any of them; skipped platforms are simply not posted to.

Your keys and logins are saved in `credentials.dat` next to the app. On Windows that file is encrypted
with your Windows account, so it's useless if copied to another computer. They are never put in
`config.yaml`.

**What to expect, in short**

| Platform | Works right away? | The catch |
|---|---|---|
| TikTok | Yes, as **drafts**: each reel lands in your TikTok inbox and you tap *Post* | Posting directly (no tap) needs TikTok to audit your app |
| Instagram Reels | Yes | Needs a Professional (Business or Creator) account. Max 100 posts a day |
| Facebook Reels | Yes | Posts to a Facebook **Page**. Reels must be 3 to 90 seconds. Max 30 a day |
| YouTube Shorts | Uploads work, but they are **locked as private** | Google must audit your project before uploads can be public |

The bot spaces posts out (every 3 hours per platform by default, `upload.hours_between_posts` in
`config.yaml`). Posting 20 reels at once hurts reach and can trip the platforms' spam limits.

Post text: the clip's title (from `clip.txt` / `title.txt`, or the file name) plus the hashtags from
`upload.hashtags` in `config.yaml`. Put a `hashtags.txt` in a clip folder to add hashtags just for that
podcast or influencer.

---

## TikTok

1. Go to [developers.tiktok.com](https://developers.tiktok.com/), log in, and open **Manage apps > Connect an app**.
2. Fill in the app details. The description can be *"Posts my own videos to my TikTok account"*.
3. Add the products **Login Kit** and **Content Posting API**.
4. In **Login Kit**, choose the **Desktop** platform and add this redirect URI exactly:
   `http://127.0.0.1:8765/callback/`
5. In the scopes list, make sure **user.info.basic** and **video.upload** are included. Add
   **video.publish** only if you want direct posting (see below).
6. Either **submit the app for review**, or, to start right away, open the app's **Sandbox**, add
   your own TikTok account under *Target users*, and use the sandbox keys.
7. Copy the **Client key** and **Client secret** into Brainrot Bot. A browser window opens: log in to
   TikTok and allow access.

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
3. Under *Generate access tokens*, **add your Instagram account** and log in when asked.
4. Press **Generate token** and copy it.
5. Paste the token into Brainrot Bot. It checks the token and shows your @username.

The token lasts 60 days. The bot renews it by itself every week, so as long as the bot runs now and
then it never expires. If it does expire, connect Instagram again.

## Facebook Reels (Pages)

Reels go to a Facebook **Page** you manage, not to a personal profile. Facebook only accepts reels
of **3 to 90 seconds**. Longer reels are skipped for Facebook (turn on `parts.max_seconds: 90` to cut
long clips into parts).

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

---

## When something goes wrong

- The bot's window and `logs/bot.log` say exactly what the platform answered.
- **"needs login"** in the menu: the platform ended the login (password change, expired token,
  access removed). Choose *Connect accounts* and log in again. Reels waiting for that platform are
  posted after that; nothing is lost.
- A platform limit was reached (daily cap, too many drafts): the bot waits and tries again later
  by itself.
