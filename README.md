# gta-clipper

Turns a stream VOD into ranked, vertical, captioned clips, with a human
approve/reject step before anything is published.

    VOD + chat -> detect spikes -> transcribe -> LLM rank -> ffmpeg 9:16 -> review -> publish

Only clip streamers whose campaign or written permission covers reuse.

## Setup (laptop, PC, or a cloud machine such as a GitHub Codespace)

Needs Python 3.10+ and ffmpeg (with libass, which standard builds include).

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-...        # optional, enables LLM ranking + hooks
    python tests/smoke_test.py             # offline self-test, should print PASSED

An iPad alone cannot run this; it needs a machine that can run Python and ffmpeg.

## Daily use

1. Add the streamer to `config.yaml` (name, credit line, facecam rectangle).
   To find the facecam rectangle, take a screenshot of the stream at full
   resolution and read the webcam's x, y, width, height in any image editor.
2. Process a VOD:

       python -m clipper run "https://www.twitch.tv/videos/123456789" --streamer example

   A local file works too: `python -m clipper run footage.mp4 --streamer example`
   Flags: `--no-llm` (rank by spikes only), `--no-whisper` (no captions),
   `--chat chat.json` (your own chat log: `[{"t": 12.5, "text": "..."}]`).
3. Review: `python -m clipper review` opens a page where you watch each clip,
   fix the hook/title, and Approve or Reject. Changing the hook re-renders.
4. Publish:

       python -m clipper publish --dry-run   # see what would go out
       python -m clipper publish             # upload approved clips to YouTube
       python -m clipper export              # clips + captions for TikTok / Instagram

## YouTube setup (one time)

1. Google Cloud Console -> new project -> enable "YouTube Data API v3".
2. Credentials -> OAuth client ID -> Desktop app -> download as `client_secret.json`
   into this folder.
3. First `publish` opens a browser to sign in; the token is cached in `token.json`.

Check this before relying on it: Google has historically locked uploads from
unverified API projects to private until the project passes a compliance audit.
That is why `privacy` defaults to `private` - flip clips to public in YouTube
Studio, or upload the exported files by hand.

## What each step does

| Step | File | Notes |
|---|---|---|
| Ingest | `clipper/ingest.py` | yt-dlp for video, chat-downloader for chat replay. Kick has no chat replay here, so it falls back to audio only. |
| Detect | `clipper/detect.py` | Chat messages and audio loudness per 5 s bucket, robust z-scores, blended 60/40. Chat is shifted back 8 s because it reacts late. |
| Transcribe | `clipper/transcribe.py` | faster-whisper on candidate windows only, with word timings for captions. |
| Rank | `clipper/rank.py` | Claude scores each candidate 0-100 and writes hook + title. Told to avoid story spoilers and invented claims. |
| Edit | `clipper/edit.py` | Facecam on top + gameplay below, or blurred background if no facecam. Hook and captions burned in. |
| Review | `clipper/review_app.py` | Streamlit page. |
| Publish | `clipper/publish.py` | YouTube upload with a daily cap; export folder for the rest. |

## Known limits (v1)

- Works on finished VODs, not live streams in real time.
- Not yet run against real Twitch/Kick/YouTube downloads; the download and chat
  libraries change often, so expect to update `yt-dlp` and possibly patch `ingest.py`.
- Whisper, the Claude ranking call, the review page and the real YouTube upload
  were not exercised in the build environment; detection, ranking fallback,
  rendering, export and dry-run publish were.
- A 3-hour 1080p VOD is several GB. Set `ingest.max_height: 720` if disk is tight.
- Tuning lives in `config.yaml` (`detect.*`). If you get too few clips, lower
  `min_score`; if clips start too late, raise `pre_roll`.
