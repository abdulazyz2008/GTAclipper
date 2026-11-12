"""Get the footage and the chat.

A source is a local video file or a VOD URL (Twitch, YouTube, Kick - anything
yt-dlp understands).

For URLs there are two modes (config `ingest.mode`):

  smart  download only the small audio track for analysis, then fetch just
         the few seconds of video each chosen clip needs. A 3-hour VOD costs
         a few hundred MB instead of many GB.
  full   download the whole VOD first.

If the smart path is not possible for a site, it falls back to full.
"""
import json
from pathlib import Path

import numpy as np

from . import chat as chatmod
from . import media
from .config import ensure_dirs, safe_name


class IngestError(Exception):
    pass


def is_url(source):
    return "://" in str(source) and not Path(str(source)).exists()


def _ydl(opts):
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise IngestError("yt-dlp is not installed. Run: pip install -U yt-dlp") from None
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "retries": 5,
            "fragment_retries": 5, "concurrent_fragment_downloads": 4}
    base.update(opts)
    return YoutubeDL(base)


def _wrap(e):
    msg = str(e).replace("ERROR: ", "").strip()
    return IngestError(f"download failed: {msg}. If the site changed recently, "
                       "run: pip install -U yt-dlp")


def describe(source):
    """Return {"id", "title", "duration", "is_live", "url"} for a source."""
    p = Path(str(source))
    if p.exists():
        if not p.is_file():
            raise IngestError(f"{p} is a folder, not a video file")
        return {"id": safe_name(p.stem, "video"), "title": p.stem, "duration": None,
                "is_live": False, "url": "", "path": p.resolve()}
    if not is_url(source):
        raise IngestError(f"'{source}' is neither an existing file nor a URL")
    try:
        with _ydl({"skip_download": True}) as y:
            info = y.extract_info(str(source), download=False)
    except IngestError:
        raise
    except Exception as e:
        raise _wrap(e) from None
    if info.get("_type") == "playlist":
        raise IngestError("that URL is a list of videos; pass one VOD at a time")
    return {"id": safe_name(info.get("id") or "vod", "vod"), "title": info.get("title") or "",
            "duration": info.get("duration"), "is_live": bool(info.get("is_live")),
            "url": str(source), "path": None}


def _find(folder, stem):
    # exactly "<stem>.<ext>": the VOD "123" must not match "123.audio.m4a"
    hits = [p for p in sorted(folder.glob(stem + ".*"))
            if "." not in p.name[len(stem) + 1:]
            and p.suffix.lower() not in (".part", ".ytdl", ".json", ".txt", ".jsonl")]
    return hits[0] if hits else None


def _download(url, fmt, stem, extra=None):
    folder = ensure_dirs()["vods"]
    done = _find(folder, stem)
    if done:
        return done
    opts = {"format": fmt, "outtmpl": str(folder / (stem + ".%(ext)s"))}
    opts.update(extra or {})
    try:
        with _ydl(opts) as y:
            y.extract_info(url, download=True)
    except IngestError:
        raise
    except Exception as e:
        raise _wrap(e) from None
    out = _find(folder, stem)
    if not out:
        raise IngestError("the download finished but no file was written")
    return out


def get_full(url, vod_id, max_height):
    fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    return _download(url, fmt, vod_id, {"merge_output_format": "mp4"})


def get_audio(url, vod_id):
    """Audio-only track (or the smallest stream that has audio)."""
    path = _download(url, "ba/wa/worst", f"{vod_id}.audio")
    if not media.probe(path)["has_audio"]:
        raise IngestError("the audio download has no audio track")
    return path


def get_section(url, vod_id, start, end, max_height):
    """Download just [start, end] of the video. Returns the file; where exactly it
    begins on the stream timeline is settled afterwards by `align_section`."""
    try:
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise IngestError("yt-dlp is not installed. Run: pip install -U yt-dlp") from None
    stem = f"{vod_id}.s{int(start * 10):08d}-{int(end * 10):08d}"
    fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    path = _download(url, fmt, stem, {
        "merge_output_format": "mp4",
        "download_ranges": download_range_func(None, [(float(start), float(end))]),
    })
    # a failed partial download can leave an empty shell behind: never keep or reuse it
    try:
        info = media.probe(path)
    except media.MediaError:
        info = {"has_video": False, "duration": 0}
    if not info["has_video"] or info["duration"] < min(5.0, (end - start) / 2):
        path.unlink(missing_ok=True)
        raise IngestError("the site did not return that part of the video")
    return path


def align_section(section_path, audio_path, expected_start, search=12.0):
    """Find the stream time at which a downloaded section really begins.

    Section downloads start on a keyframe, which can be several seconds away
    from what was asked for. The section's loudness curve is slid along the
    full audio track; the best match gives the true start. Falls back to
    `expected_start` when the match is not convincing.
    """
    hop = 0.1
    try:
        sec = media.to_db(media.envelope(section_path, hop))
        lo = max(0.0, expected_start - search)
        ref = media.to_db(media.envelope(audio_path, hop, lo, len(sec) * hop + 2 * search))
    except media.MediaError:
        return expected_start
    n = len(sec)
    if n < 30 or len(ref) < n:
        return expected_start
    a = sec - sec.mean()
    na = float(np.sqrt((a * a).sum()))
    if na < 1e-3:
        return expected_start                     # silent section: nothing to match
    best, best_c = None, -2.0
    for k in range(0, len(ref) - n + 1):
        w = ref[k: k + n]
        w = w - w.mean()
        nw = float(np.sqrt((w * w).sum()))
        if nw < 1e-3:
            continue
        c = float((a * w).sum()) / (na * nw)
        if c > best_c:
            best, best_c = k, c
    if best is None or best_c < 0.8:
        return expected_start
    return round(lo + best * hop, 2)


# ---- chat ------------------------------------------------------------------


def get_chat(source, vod_id, chat_file=None, log=print):
    """Chat replay for a VOD as a message list. Empty if there is none."""
    if chat_file:
        try:
            return chatmod.load_chat_file(chat_file)
        except (OSError, ValueError) as e:
            raise IngestError(f"could not read chat file {chat_file}: {e}") from None
    cache = ensure_dirs()["vods"] / f"{vod_id}.chat.json"
    if cache.exists():
        try:
            return chatmod.normalise(json.loads(cache.read_text(encoding="utf-8")))
        except ValueError:
            cache.unlink()
    if not is_url(source):
        side = Path(str(source)).with_suffix(".chat.json")
        if side.exists():
            return chatmod.load_chat_file(side)
        return []
    msgs = []
    try:
        from chat_downloader import ChatDownloader
        for m in ChatDownloader().get_chat(str(source)):
            t = m.get("time_in_seconds")
            text = m.get("message")
            if t is not None and t >= 0 and text:
                msgs.append({"t": float(t), "text": str(text),
                             "user": str((m.get("author") or {}).get("name") or "")})
    except Exception as e:      # no replay on this site, deleted replay, site change...
        log(f"  chat replay unavailable ({type(e).__name__}: {str(e)[:120]}).")
        log("  Detection will use audio only. To add chat, export it yourself "
            "(e.g. with TwitchDownloader) and pass --chat chat.json")
        return []
    msgs.sort(key=lambda m: m["t"])
    cache.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
    return msgs
