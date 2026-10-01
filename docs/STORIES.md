# Stories and AI videos

Send the bot an idea and it comes back as a 1-minute narrated reel, made one of two ways:

- **AI video**: every scene is an animated shot made by Google's **Veo 3.1**, from a picture drawn by
  **Nano Banana 2**, in one of three looks (claymation, anime, 3D cartoon). The characters look the same
  in every scene. It costs money, so it waits for your OK and never goes over your daily budget.
- **Over gameplay**: the narration over full-screen gameplay from your gameplay folder, with big captions.
  It's free.

Either way the reel gets the hook, captions, music, progress bar and cover picture like every other reel,
goes into `Reels\<Platform>\Stories\` and is posted like the rest (with a preview on your phone).

## How a story is made

1. **The idea.** From your phone (`/idea a lighthouse that blinks with nobody inside` on Telegram,
   `idea ...` on Discord), the menu (*Stories & AI videos* > *Add a story idea*), a `.txt` file dropped
   into the `stories\inbox` folder, Reddit, or the AI itself (see below).
   Have a whole story already? `/story <your story>` reads it as you wrote it.
2. **The script.** The AI helper (Claude, menu > *AI helper*) writes the story: a hook in the first
   sentence, about 75 seconds of narration with rising tension and a twist or a lesson at the end, split
   into about 11 scenes. For each scene it writes what we see, the camera, and the sound of the place.
   It also describes each character precisely (age, face, hair, clothes and colors) so they can be drawn
   the same every time, plus the title, the on-screen hook, the caption and hashtags.
3. **Your OK.** The script comes to your phone with buttons (below). A story that would become an AI
   video waits for your tap; one that's made over gameplay (when that's the default, or there's no Google
   key yet) is made right away (`stories.approval`). The story is written to the length your narrator
   reads it in, and a narration just short of a minute is read a little slower, so stories last over the
   minute TikTok pays for.
4. **The narration** is read in one go (so it flows like one telling), timed word by word, and each scene
   is cut in the pause before its first word.
5. **The AI video**: the characters are drawn first. Each scene's picture is then drawn with those
   character pictures as references, and Veo animates it into a 4, 6 or 8 second shot (whatever covers
   that scene's narration) with the sound of the place (wind, rain, footsteps) quietly under the voice.
   Four shots are made at a time; a story takes about 15-30 minutes.
6. **The reel**: the shots cut to the narration, captions low on the screen (under the faces), the hook,
   music from your music folder, then the cover picture. The phone gets a preview as usual.

Nothing is paid for twice: every picture, shot and the narration is saved in the story's own folder, so
if the bot stops or a request fails halfway, it carries on where it was.

## On your phone

**Telegram**

- `/idea <your idea>`: the AI writes a story from it. `/idea` alone asks for it.
- `/story <your story>`: your own story, read as you wrote it (the AI only splits it into scenes).
- `/stories`: what's waiting for you, what's being made, and what AI videos cost today.

The script arrives with buttons:

```
📝 The Lighthouse That Blinked Twice
From your idea · about 75s · 11 scenes · eerie
Hook: Nobody lives there. It still blinks.

Every night at nine, the old lighthouse blinked twice...

🎬 AI video: about $9.30 (Veo 3.1 Fast; $0.00 of $10.00 used today)
🎮 Over gameplay: free

[ 🎬 AI video · Clay · ~$9.30 ]
[ Clay ✓ ] [ Anime ] [ 3D ]
[ 🎮 Over gameplay · free ]
[ 🔁 Write again ] [ 🗑 Drop ]
```

Tap a look to switch it, then *AI video*. *Write again* asks the AI for a different version.

**Discord**: `idea <your idea>`, `story <your story>` and `stories`. React 🎬 for an AI video (in the
look set in the menu), 🎮 for over gameplay, 🗑 to drop it.

**No phone?** The scripts wait in the menu: *Stories & AI videos* > *Stories waiting for your OK*.

## The three looks

Each look is an engineered prompt used for every picture and every shot of a story, so all its scenes
look like one film. They describe the look in detail instead of naming a studio (Google's models refuse
or water down "in the style of <studio>", and a described look is yours to use).

| Look (`stories.style`) | What it asks for |
|---|---|
| **Claymation** (`claymation`) | Handmade stop-motion: matte plasticine with fingerprints and tool marks, chunky rounded characters with big eyes, felt and knitted clothes, a miniature tabletop set, warm studio light, a macro-lens blur behind. Moves in slightly stepped, handmade frames. |
| **Anime** (`anime`) | A modern anime feature: clean line art and cel shading, expressive eyes, lush painted backgrounds and skies, golden-hour or moonlight with rim light, bloom and drifting particles. Hair and clothes move in the wind; the camera moves slowly. |
| **3D cartoon** (`cartoon3d`) | A polished 3D animated movie: appealing characters with exaggerated proportions, soft skin and detailed fabric and hair, warm and cool lights, depth of field, vibrant colors. Bouncy, expressive animation. |

Every picture also gets the composition (vertical, faces in the upper two thirds because the captions
go low) and "no text anywhere" (AI models draw text badly). Every shot asks for no music and no talking
(the narration is the only voice), and Veo is told what to avoid (text, watermarks, morphing, distorted
faces, and the other looks).

## What an AI video costs

For a 75-second story (about 12 scenes: about 84 seconds of shots and 14 pictures), at Google's prices
of October 2026:

| Video model (`stories.video_model`) | Per second | A 75-second story |
|---|---|---|
| `veo-3.1-lite-generate-preview` (Lite) | $0.05 | about **$5** |
| `veo-3.1-fast-generate-preview` (Fast, the default) | $0.10 (1080p: $0.12) | about **$9** |
| `veo-3.1-generate-preview` (the best) | $0.40 | about **$35** |
| `none`: pictures only, moved by a slow camera | - | about **$1** |

- Pictures (Nano Banana 2, `gemini-3.1-flash-image`): $0.067 each. Already included above.
- The script (Claude): a few cents. The narration with a Google Gemini voice: free for about 100 stories
  a day, then about 2 cents; the free voices on your PC cost nothing.
- **A shot Google blocks** (its safety filters) **isn't charged**; that scene becomes its picture moved
  by a slow camera, and the story still gets made.
- **Daily budget** (`stories.daily_budget`, $10 by default; menu > *Daily budget*): before an AI video is
  started, its cost is checked against what's left of today's budget. If it doesn't fit, it waits for the
  next day (and your phone is told). A story that has started is always finished.
- The phone shows each story's price before you tap, and `/stories` shows what today has cost.
- Tip: also set a budget alert in [Google Cloud billing](https://console.cloud.google.com/billing), on top
  of the bot's budget.

## The Google AI key

Pictures and shots need a key from Google AI Studio **with billing turned on** (neither has a free tier).
The same key also works for the Gemini voice.

1. Open [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and sign in with a Google account.
2. *Create API key*, and copy it.
3. *Set up billing* for the key's project (a card in Google Cloud billing).
4. In the bot: menu > *Stories & AI videos* > *Google AI key*, and paste it. The bot checks it (for free)
   and offers a Gemini voice for the stories' narrator.

The key is kept in the bot's encrypted credentials file, never in `config.yaml`. On a server you can also
set `GEMINI_API_KEY`.

## Story ideas from Reddit

The bot can read the top posts of the week from subreddits you pick, and the AI writes a **new** story
from the best one's idea, with new names, details and scenes (never the post itself: posts belong to the
people who wrote them). The caption says "Inspired by a story on r/...".

**Reddit's rules**: since November 2025 Reddit approves every app before it may read anything (its
[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)),
and using Reddit content commercially (videos you earn money from) needs Reddit's **written approval**.
So this only runs with your own app, after Reddit approved it:

1. Open [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) > *create another app* > type **script**
   (redirect uri: `http://localhost:8080`).
2. Ask Reddit for API access for that app (its form is linked from that page) and wait for the OK.
3. Menu > *Stories & AI videos* > *Reddit*: paste the app ID (under the app's name), the secret and your
   Reddit username, then the subreddits.

The bot signs in as the app itself, so it never needs your Reddit password. Every 6 hours it takes the one
best new post (`stories.reddit_per_check`) with at least 300 upvotes, skipping NSFW, pinned and removed
posts. It stops looking while 3 stories are already waiting for you, and never reads subreddits whose
rules forbid retelling their stories (r/nosleep, r/shortscarystories).

Subreddits that suit 1-minute stories:

- Mysterious: `Glitch_in_the_Matrix`, `LetsNotMeet`, `Paranormal`, `HighStrangeness`
- Wise and useful: `GetMotivated`, `Stoicism`, `LifeProTips`, `YouShouldKnow`
- Stories: `MaliciousCompliance`, `tifu`, `pettyrevenge`, `WritingPrompts` (the prompts are the ideas)

## Ideas from the AI

With `stories.ai_ideas: true` (menu > *Ideas from the AI*), when no idea is waiting the AI thinks of a
new one in your `stories.topics` (by default: mysterious encounters and eerie coincidences, wise little
stories with a lesson, small acts of kindness with a twist). It avoids the stories already told, and
makes at most `stories.per_day` stories a day.

## Labels and the platforms' rules

- **TikTok**: every picture and shot of an AI video is made by AI, so AI videos are posted with TikTok's
  "AI-generated" label (in direct posting; in draft mode, turn on *AI-generated content* when you post).
- **YouTube** asks for its "altered or synthetic" label only for [realistic content](https://support.google.com/youtube/answer/14328491)
  ("clearly unrealistic content, such as animation" doesn't need it), and all three looks are animation.
- **Facebook and Instagram** add their "AI info" label themselves when they detect AI content.
- **Original content**: the stories are written new for you, which is what the platforms pay for. Still,
  YouTube's [inauthentic content rule](https://support.google.com/youtube/answer/1311392) names "content
  made with generic or unoriginal templates giving the impression of mass production": keep the topics
  varied, read the scripts, and write some ideas yourself.
- **Length**: stories are about 75 seconds (`stories.seconds`), over the minute TikTok's Creator Rewards
  need. Every platform gets the same story (a story can't be cut shorter for YouTube); YouTube Shorts take
  up to 3 minutes, and Facebook up to 90 seconds, so stories are kept under 88 seconds.

## Where everything is

- `stories\` (next to your clips folder): one folder per story, `<id> <title>`, with `script.json`, the
  narration (`narration.wav`), the character pictures, and each scene's picture (`scene_01.png`) and shot
  (`scene_01.mp4`). Keep what you like; delete old ones whenever you want.
- `stories\inbox\`: drop `.txt` files here and each becomes an idea (the menu hands its ideas over here).
- `stories\accounts.txt` (optional): which accounts stories are posted to, like in a clips folder, e.g.
  `tiktok = stories` to post them on a second TikTok account.
- The reels: `Reels\<Platform>\Stories\<title>_<id>.mp4` (with `.srt` and `.jpg`).

## When something goes wrong

- **"Google needs billing turned on"**: pictures and video have no free tier; set up billing (above).
- **"today's budget for AI videos is used up"**: the story is made tomorrow, or raise the budget in the
  menu (the bot checks again when it restarts).
- **A scene is a still picture slowly zooming**: Google blocked that shot (not charged) or took over 20
  minutes for it.
- **A character looks a little different in one scene**: the reference pictures keep them close, but
  it's AI. *Write again*, or try another look.
- **"Stories are written by the AI helper"**: connect Claude in the menu (*AI helper*). Without it only
  `/story` (your own words) works, over gameplay.
- Anything else: the log (menu > *Watch live activity*) says what happened, and failing stories are
  tried 3 times, then your phone is told why.
