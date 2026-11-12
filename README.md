# gta-clipper

Turns a livestream or VOD into short vertical clips, ready to review and post.

    stream / VOD + chat
      -> find moments (chat acceleration, chat rate, hype messages, audio)
      -> transcribe -> judge (Claude) -> edit to 9:16 with hook + captions
      -> you approve or reject -> publish

It works on finished VODs and video files, and it can watch a stream while it
is live and cut clips as the moments happen. It is built for GTA 6 streams but
nothing depends on the game: set `game:` in `config.yaml`.

Nothing is published until a person has approved it on the review page.

**Only clip streamers whose campaign or written permission covers reuse.**
Every streamer has to be listed in `config.yaml` before the tool will touch
their footage, and every clip carries their credit.

## Install

Needs Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) (a standard
build; it must include libx264 and libass).

    git clone https://github.com/abdulazyz2008/GTAclipper
    cd GTAclipper
    pip install -e ".[all]"        # or: pip install -r requirements.txt
    clipper init                   # writes config.yaml, data/ and the Claude skill
    clipper doctor                 # checks everything and renders a test clip

`clipper doctor` lists each part as `ok`, `off` (optional, with what you lose
and how to add it) or `MISSING`. The optional parts are captions
(`faster-whisper`), live recording (`streamlink`), automatic ranking through
the Claude API (`anthropic` + a key) and YouTube upload.

A phone or tablet alone cannot run this; it needs a computer or a cloud
machine such as a GitHub Codespace (a `.devcontainer` is included).

## Use it with Claude (the skill)

The repository ships a Claude skill, `stream-clipper`, in
`.claude/skills/stream-clipper/`. Open the folder in Claude Code (or a Claude
cloud session on this repository) and ask in plain words:

> Clip last night's stream: https://www.twitch.tv/videos/123456789 (streamer: example)

> Watch twitch.tv/example and clip the best moments while it's live.

Claude runs `clipper` for you. Its part is the judgement: it reads what the
streamer said and what chat said for each detected moment, looks at frames
from the footage, scores the moments, writes the hook and title, and adjusts
the cut points. No API key is needed for this. You then approve or reject
each clip, and Claude publishes only what you approved and only when you ask.

`clipper init` copies the skill into any other workspace folder. To have it
in every project, copy that folder to `~/.claude/skills/`. The repository is
also a Claude Code plugin (`.claude-plugin/plugin.json`).

## Use it by hand

1. Add the streamer to `config.yaml`: a key, the credit line, their channel
   and, if they have a webcam on screen, its rectangle (`facecam`) measured on
   a full-resolution screenshot. Without a facecam, clips use a blurred
   background layout.
2. Make clips from a VOD or a file:

       clipper run "https://www.twitch.tv/videos/123456789" --streamer example
       clipper run footage.mp4 --streamer example --chat chat.json

   or from a stream that is live:

       clipper watch --streamer example --wait 30

3. Review: `clipper review` prints an address. On that page you watch each
   clip, fix the hook, title or cut points, and approve or reject
   (keys: `A`, `R`, `J`/`K`).
4. Publish:

       clipper publish --dry-run     # see what would go out
       clipper publish               # upload approved clips to YouTube
       clipper export                # clip files + captions for TikTok / Instagram

All commands: `clipper --help`. The ones Claude uses to judge clips itself are
`scan`, `candidates --json`, `frames`, `judge` and `render`.

## How moments are found

Everything is measured in 5-second steps, and each step is compared with the
previous ten minutes of the same stream, so a quiet stream and a huge one are
judged on their own normal.

| Signal | What it measures |
|---|---|
| Chat rate | How many different people are typing. One person counts once per step, so a spammer or bot cannot fake a spike; commands and known bots are ignored. |
| Chat acceleration | How fast that rate is climbing. The jump marks the instant something happened better than the peak does, so it also sets where the clip starts. |
| Hype | The share of reaction messages: LUL, KEKW, W, "NO WAY", "CLIP IT", bursts of capitals. Add your community's emotes under `detect.hype_words`. |
| Audio | Loudness: shouting, explosions, laughter. |

Chat reacts late, so it is shifted back 8 seconds (`detect.chat_lag`). A clip
opens 12 seconds before the moment (`pre_roll`), runs 35 seconds (`clip_len`)
or longer while chat is still reacting (up to `max_len`), and its start and
end are nudged onto pauses in the audio so it does not cut a word in half.

Scores only ever look backwards in time. That is why a live stream and its
VOD produce the same clips.

## What the edit looks like

- 1080x1920, up to 60 fps, H.264 + AAC, ready for Shorts, TikTok and Reels.
- Layouts: facecam on top with gameplay below (`split`), gameplay over a
  blurred copy of itself (`blur`), or gameplay filling the frame (`crop`).
- The hook as a title card from the first frame, word-by-word captions with
  the spoken word highlighted, and the streamer's credit. Text stays clear of
  the areas the apps cover with their own buttons.
- Audio levelled to -14 LUFS so every clip plays at the same volume.
- Slurs in captions are always masked; `edit.censor: true` also masks strong
  language.

Colours, sizes, font and layout are under `edit:` in `config.yaml`. The
caption font (Poppins ExtraBold) is bundled under the SIL Open Font License.

## Downloads

For a VOD link the default (`ingest.mode: smart`) downloads only the audio
track for analysis and then just the few seconds of video each chosen clip
needs, instead of the whole multi-gigabyte VOD. If a site cannot do that, the
whole VOD is downloaded. Set `ingest.max_height: 720` to save more space.

Chat replays are fetched with `chat-downloader`. When a site has none, the
tool says so and uses audio alone; you can pass your own log with `--chat`
(this tool's format, chat-downloader's, or TwitchDownloader's JSON).

## Live

`clipper watch` records the stream in short pieces (only the last
`live.keep_min` minutes stay on disk), reads Twitch chat through Twitch's
public chat gateway without any login, and cuts a clip as soon as a moment
clears `live.min_score` and is fully recorded. Clips appear in review while
the stream is still going. Other sites are recorded and judged on audio only.

## YouTube setup (one time)

1. Google Cloud Console -> new project -> enable "YouTube Data API v3".
2. Credentials -> OAuth client ID -> Desktop app -> download the file as
   `client_secret.json` into your workspace folder.
3. The first `clipper publish` opens a browser to sign in; the token is saved
   as `token.json`. Both files are in `.gitignore` - never commit them.

Uploads default to `private` (`youtube.privacy`). Google has historically
kept uploads from unverified API projects private until the project passes
its audit, so check that before relying on public uploads; `clipper export`
plus a manual upload always works. TikTok and Instagram have no upload here:
their posting APIs need an approved app, so `export` prepares the files and
captions for you to post.

## What has and has not been tested

The test suite (`pip install -e ".[dev]"`, then `pytest`) runs 150+ checks
offline on synthetic footage: detection, the editor in every layout, the full
VOD pipeline, partial downloads against a local web server, the live watcher
on a simulated stream, the Twitch chat reader against a local chat server,
the review page, and publishing against a stand-in for the YouTube API.

Not yet exercised against the real services, because the build machine had
no access to them:

- real Twitch / YouTube / Kick downloads and live recording. These go through
  `yt-dlp` and `streamlink`, which change often - `pip install -U yt-dlp
  streamlink` is the first thing to try when a download fails;
- speech-to-text with a real Whisper model (captions were tested with
  stand-in words);
- a real Claude API call and a real YouTube upload.

Expect to tune `detect.min_score` on your first real streams: lower it if you
get too few clips, raise it if you get weak ones.

## Layout of the code

| File | Job |
|---|---|
| `clipper/detect.py` | chat rate / acceleration / hype / audio scoring, moment picking |
| `clipper/ingest.py` | VOD, audio and section downloads; chat replay |
| `clipper/chat.py` | chat log formats; live Twitch chat reader |
| `clipper/live.py` | live recorder and watcher |
| `clipper/transcribe.py` | speech-to-text with word timings |
| `clipper/rank.py` | judgement rules; Claude API ranking; detector-score fallback |
| `clipper/edit.py` | layouts, hook, captions, audio levelling, encoding |
| `clipper/pipeline.py` | scan -> judge -> render, shared by everything |
| `clipper/review.py` | the review page |
| `clipper/publish.py` | YouTube upload and export |
| `clipper/skill/SKILL.md` | the Claude skill |
