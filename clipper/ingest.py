"""Step 1 - get the VOD and its chat replay.

Accepts a Twitch / Kick / YouTube VOD URL, or a local video file (use this
when a streamer or campaign hands you the footage directly).
"""
import json
from pathlib import Path

from .common import VODS


def get_vod(source, max_height=1080):
    """Return (vod_id, local_path)."""
    p = Path(source)
    if p.exists():
        return p.stem, p.resolve()

    from yt_dlp import YoutubeDL
    VODS.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "outtmpl": str(VODS / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source, download=True)
    vod_id = str(info["id"])
    path = VODS / f"{vod_id}.mp4"
    if not path.exists():
        found = sorted(VODS.glob(f"{vod_id}.*"))
        if not found:
            raise RuntimeError("download finished but no file was found")
        path = found[0]
    return vod_id, path


def get_chat(source, vod_id, chat_file=None):
    """Return a list of {"t": seconds_into_vod, "text": str}. Empty if unavailable."""
    cache = VODS / f"{vod_id}.chat.json"
    if chat_file:
        return json.loads(Path(chat_file).read_text(encoding="utf-8"))
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    if Path(source).exists():
        return []
    msgs = []
    try:
        from chat_downloader import ChatDownloader
        for m in ChatDownloader().get_chat(source):
            t = m.get("time_in_seconds")
            if t is not None and t >= 0:
                msgs.append({"t": float(t), "text": str(m.get("message") or "")})
    except Exception as e:  # Kick, deleted replays, site changes...
        print(f"  chat replay unavailable ({type(e).__name__}: {e}); using audio only")
        return []
    VODS.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
    return msgs
