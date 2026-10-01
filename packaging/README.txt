BRAINROT BOT - quick start (Windows)
=====================================

1. Unzip this whole folder somewhere you keep programs, for example
   Documents\BrainrotBot. Keep all the files together.

2. Double-click BrainrotBot.exe.
   Windows may say "Windows protected your PC" because the app isn't signed:
   click "More info" and then "Run anyway".

3. Answer the setup questions (anything optional can be skipped and added later):
   - run 24/7 in the background, or only while the window is open
   - start automatically when you log in (about 15 seconds after login)
   - auto-posting: TikTok, Instagram, Facebook, YouTube, X and Pinterest (connect or skip each one)
   - AI helper: an Anthropic API key lets Claude pick the best moments and write hooks and captions
   - your phone: Telegram or Discord, to approve each reel with one tap
   - your folders (it offers Google Drive / Dropbox / OneDrive folders if you have them)

4. At the end the app shows two folders:
   - GAMEPLAY: paste your gameplay recordings there
   - CLIPS: make one subfolder per podcast / influencer / topic and paste the clips inside
   Every clip becomes reels in the REELS folder, one version per platform: REELS\TikTok (1 minute
   or longer), REELS\YouTube (under a minute), REELS\Instagram and REELS\Facebook (up to 90 s).
   Long videos (whole episodes) are cut into their best moments: about 6 reels from a 2-hour episode.
   You can also add a YouTube / TikTok link from the menu or from your phone.

5. Commentary voiceover (so reposted clips count as original): the clip pauses for a line before it
   and a take after it. The AI helper writes the take, or use your own: a recording saved next to the
   clip as <clip>.outro.mp3, or your words in <clip>.commentary.txt. It's read by an AI voice that
   sounds like a real person (Google Gemini: free for about 50 reels a day; or ElevenLabs), or by a
   free voice on your PC. Menu > Look & features > Commentary voiceover > Voice.

6. Stories and AI videos: send your bot an idea on Telegram (/idea a lighthouse that blinks with
   nobody inside) and get back a 1-minute narrated reel, over gameplay (free) or as a full AI video
   (each scene animated by Google's Veo in a claymation, anime or 3D cartoon look; about $9 a story,
   it asks first and keeps to your daily budget). Menu > Stories & AI videos. Guide: docs/STORIES.md

Double-click BrainrotBot.exe again at any time for the menu: what the bot is doing, the folders,
add a video link, the phone dashboard (scan its QR code on the same Wi-Fi), stats, look & features,
connect accounts, and start / stop. While it runs in the background, its icon sits next to the clock.

The first clip takes a few extra minutes: the speech-recognition model (about 500 MB) is
downloaded once into the "models" folder next to the app.

The app checks for new versions once a day and offers them in the menu. Updating keeps your
settings, logins, clips and reels.

Look & feel (caption colors, font size, words per caption, ...): menu > Look & features, or open
config.yaml next to BrainrotBot.exe with Notepad after the first start. Every setting is explained there.

Full guide and help: https://github.com/polouseskander1-cpu/Brainrot
  Connecting each platform: docs/PLATFORMS.md    Phone: docs/PHONE.md    Stories: docs/STORIES.md
