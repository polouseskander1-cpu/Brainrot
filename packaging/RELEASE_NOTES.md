**Download `BrainrotBot-windows.zip`**, unzip it, and double-click `BrainrotBot.exe`.
Windows may warn that the app isn't signed: click "More info" > "Run anyway".

**Already on 1.2?** Open the app and choose **Update** in the menu. **Coming from 1.1?** Stop the bot
(menu > Stop the bot), then unzip this download into the same place as before and choose to replace the
files. Either way, your settings, logins, clips and reels stay.

## What's new in 1.3

**A version of every reel for each platform**, each in its own folder and posted to its own platform:

| Folder | Length | Why |
|---|---|---|
| `Reels\TikTok\` | 61-90 seconds | TikTok's Creator Rewards only pay for videos of 1 minute or longer |
| `Reels\YouTube\` | up to 59 seconds | A Short over 1 minute with a copyright claim can't earn |
| `Reels\Instagram\`, `Reels\Facebook\` | up to 90 seconds | Facebook takes reels of 3-90 seconds from apps |

Lengths are checked after pauses are cut. A clip under a minute gets no TikTok version (the log says
why). Change the lengths or switch this off in `config.yaml` (`versions:`) or in the menu.

**Long podcast episodes**
- About one reel per 20 minutes: **6 from a 2-hour episode**. Pick your own number in the menu under
  Look & features.
- Each moment is 1-1.5 minutes (long enough for TikTok); the YouTube version is its strongest part.
- The bot now looks for hot takes and controversial opinions, mind-blowing facts and sci-fi talk (space,
  aliens, AI, the future), and so does the AI helper.
- Listening (speech recognition) uses more of the processor on bigger PCs, so long episodes finish sooner.

**Getting paid:** the right length is only part of it. See
[what each platform requires](https://github.com/polouseskander1-cpu/Brainrot/blob/HEAD/docs/PLATFORMS.md#getting-paid-what-each-platform-requires):
follower and view thresholds, and originality rules that reposted clips often don't meet.

Guides: [connecting the platforms](https://github.com/polouseskander1-cpu/Brainrot/blob/HEAD/docs/PLATFORMS.md) ·
[your phone](https://github.com/polouseskander1-cpu/Brainrot/blob/HEAD/docs/PHONE.md) ·
[servers](https://github.com/polouseskander1-cpu/Brainrot/blob/HEAD/docs/SERVER.md)
