---
name: stream-clipper
description: Turn a livestream or VOD (Twitch, YouTube, Kick, or a local video file) into short vertical clips with the `clipper` tool - it finds the best moments from chat acceleration and audio spikes, you judge them and write the hooks and titles, it edits them into captioned 9:16 clips, the user reviews, and approved clips are published. Use this whenever the user wants to clip a stream, find the best or funniest moments of a stream or VOD, make Shorts / TikToks / Reels from stream footage, watch a live channel and clip it as it happens, or review, approve, export or upload clips - even if they never say "clipper".
---

# Stream clipper

`clipper` does the mechanical work: downloading, measuring chat and audio,
cutting, captioning, rendering, uploading. You are the editor. Your part is
the judgement a script cannot make: deciding which moments are actually good,
writing a hook that is true and makes people stop scrolling, and catching
clips that would embarrass the user before they see them.

Two things stay with the user, always: **approving** clips and deciding to
**publish**. Clips are public and carry someone else's face, so nothing goes
out on your judgement alone.

## Before you start

Run commands from the workspace folder (the one holding `config.yaml`).
Use `clipper ...`; if that is not found, `python -m clipper ...` from the
repository works the same.

1. `clipper doctor` - shows what is installed and what is missing, with the
   fix for each. If there is no `config.yaml`, run `clipper init` first.
   If `clipper` itself is missing: `pip install -e .` in the repository
   (or `pip install -r requirements.txt`).
2. The streamer must have an entry under `streamers:` in `config.yaml`.
   If they do not, ask the user for: a short key (letters/digits), the credit
   line to show (for example `@name on Twitch`), their channel URL, and
   whether the user has permission to reuse this streamer's footage. Add the
   entry yourself once you have that. A `facecam` box is optional; without
   it clips use the blurred-background layout. Do not invent a facecam box -
   to measure one, run `clipper frames <id>` on any clip and read the webcam's
   position off the frame, or ask the user.

## A finished stream (VOD URL or video file)

```bash
clipper scan "<url or file>" --streamer <key>      # find + transcribe moments
clipper candidates --json                           # read them
clipper frames <id> [<id> ...]                      # look at the ones worth a look
clipper judge data/judgements.json                  # hand back your verdicts
clipper render                                      # render the best ones
```

`scan` prints how many moments it found. Each candidate from
`candidates --json` has:

- `streamer_said` - speech-to-text of the window (may contain mistakes, may be empty)
- `chat_said` - the messages chat repeated most, with counts
- `chat_score`, `accel_score`, `hype_score`, `audio_score` - how far above the
  stream's own normal each signal went (0 = normal, 10 = maximum)
- `start`, `end` - seconds into the stream

For a long VOD the scan takes a while (it downloads audio and runs
speech-to-text); run it in the background and wait for it.

### Judging

Read every candidate. For the ones that look promising from the text - and
any where the text is empty or unclear - run `clipper frames` and Read the
image it prints. The sheet shows ten frames across the clip, left to right
then top to bottom; the command prints the stream time of each.

Give each candidate a score from 0 to 100 for how well it works as a
standalone 30-50 second clip for someone who never saw the stream:

- High: a clear setup and payoff, a strong genuine reaction, something funny,
  shocking or skilful that the frames and words both show.
- Low: menus, loading screens, the streamer away from the desk, moments that
  need earlier context, and spikes caused by something off-stream - raids,
  gifted subs, giveaways, an ad break, chat arguing among themselves. Those
  produce big chat numbers and terrible clips. `chat_said` usually gives it
  away.
- If you cannot tell what happened, score it low and say so. A missed clip
  costs nothing; a confusing one costs the user a review.

Then write, for each one worth rendering:

- `hook` - the on-screen title: at most 8 words and 60 characters. It must
  describe what actually happens. Never invent an event, a name or an outcome
  that is not in the transcript, chat or frames: a misleading hook gets the
  clip reported and the streamer blamed.
- `title` - at most 95 characters, normal sentence case, no `<` or `>`, and
  no story spoilers (endings, deaths, plot twists).
- `reason` - one sentence the user will read in review.
- `start` / `end` (optional) - move the cut points, in stream seconds, when
  the frames or transcript show the clip opens too late, drags on, or cuts a
  sentence. Leave them out to keep the detected ones.

Write the verdicts as a JSON list and apply them:

```json
[
  {"id": 3, "score": 86, "hook": "He did not see the cop behind him",
   "title": "The worst possible time to check the map",
   "reason": "Clean setup and a loud, real reaction; chat is all LMAO."},
  {"id": 4, "score": 12, "hook": "", "title": "Raid welcome",
   "reason": "Chat spike is a raid, nothing happens in game."}
]
```

`clipper judge <file>` checks everything and changes nothing unless the whole
file is valid; if it lists problems, fix them and run it again. Then
`clipper render` renders the top candidates (`--top N` to change how many;
`clipper render <id> ...` for specific ones, also to re-render after a change).

## A stream that is live now

```bash
clipper watch --streamer <key> --wait 30      # records, clips moments as they happen
clipper status                                # how it is going
```

Run `watch` in the background; it keeps going until the stream ends (or
`--minutes N`). `--wait 30` keeps checking for 30 minutes if the channel is
not live yet. Live chat is read for Twitch; other sites fall back to audio.

Live clips are rendered straight away so they are never lost, but with a
plain title and no hook. When the user wants them finished, judge them the
same way as above - `clipper candidates --status rendered --unjudged --json`,
then `clipper judge`, then `clipper render <ids>` to apply the new hooks.

## Review and publishing

Rendered clips wait in review. The user decides:

- `clipper review` prints an address for a page where they watch each clip,
  edit the hook, title and cut points, and approve or reject. Run it in the
  background and give them the address.
- If they cannot open that page (a remote or cloud session), show them the
  clips instead - the files are in `data/clips/`, `clipper list` has the
  titles - and record their decision with `clipper approve <id> ...` or
  `clipper reject <id> ...`. Use these only for clips the user has explicitly
  approved or rejected in this conversation, by number.

Publishing, only when the user asks for it:

```bash
clipper publish --dry-run     # show exactly what would go out
clipper publish               # upload approved clips to YouTube
clipper export                # or: files + captions for TikTok / Instagram
```

Show the dry-run result first. YouTube upload needs the user's own
`client_secret.json` (README, "YouTube setup") and opens a browser sign-in
the first time; if that is not set up, use `export` and tell them where the
files are. Uploads default to private.

## When something goes wrong

| What you see | What to do |
|---|---|
| `chat replay unavailable` | Detection continues on audio alone. For better results ask the user for a chat export (TwitchDownloader's JSON works) and pass `--chat file.json`. |
| `no moments cleared detect.min_score` | Quiet stream. Lower `detect.min_score` in `config.yaml` (try 1.0) and scan again. |
| `captions are off` | `pip install faster-whisper`, then scan again. Clips still render without captions. |
| `download failed` | `pip install -U yt-dlp` and retry; sites change often. A local file always works. |
| `the stream is not live` | Use `--wait <minutes>`, or check the channel URL in `config.yaml`. |
| a clip opens late or ends early | Change `start` / `end` in a judgement, or the user trims it on the review page. |

## Reporting back

Say what the commands actually printed: how many moments were found, which
clips you rendered and why, which you skipped and why, and where things
stand (waiting for review, approved, uploaded). If a step failed or was
skipped - no chat, no captions, a download error - say so plainly rather
than describing the run as complete. Never say a clip is published unless
`clipper publish` printed its upload line.
