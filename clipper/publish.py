"""Publish approved clips.

YouTube   uploaded through the official Data API with your own Google
          project (see README, "YouTube setup"). Only clips a person approved
          in review are ever uploaded, at most `youtube.max_per_day` a day.
Others    `export` writes each approved clip plus a ready-to-paste caption
          into data/export/ for TikTok, Instagram or a scheduler.
"""
import datetime as dt
import os
import re
import shutil
from pathlib import Path

from . import store
from .config import ensure_dirs, home, safe_name

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class PublishError(Exception):
    pass


def caption(clip, cfg):
    """Return (title, description, tags) for a clip."""
    s = cfg["streamers"].get(clip["streamer"], {})
    credit = s.get("credit") or clip["streamer"]
    hashtags = s.get("hashtags") or "#shorts"
    if not re.search(r"(?i)#shorts\b", hashtags):
        hashtags += " #shorts"
    title = re.sub(r"[<>]", "", clip["title"] or f"{clip['streamer']} {cfg['game']} moment")
    title = " ".join(title.split())[:100]
    desc = f"{title}\n\nClipped from {credit}\n{hashtags}"
    tags = [t.lstrip("#") for t in hashtags.split() if t.startswith("#") and len(t) > 1]
    return title, desc, tags


def body(clip, cfg):
    title, desc, tags = caption(clip, cfg)
    y = cfg["youtube"]
    return {
        "snippet": {"title": title, "description": desc, "tags": tags,
                    "categoryId": str(y["category_id"])},
        "status": {"privacyStatus": y["privacy"],
                   "selfDeclaredMadeForKids": bool(y["made_for_kids"])},
    }


def _youtube():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise PublishError("YouTube upload needs extra packages. Run: "
                           "pip install google-api-python-client google-auth-oauthlib") from None
    token = home() / "token.json"
    creds = None
    if token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except ValueError:
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            secret = home() / "client_secret.json"
            if not secret.exists():
                raise PublishError(f"{secret} is missing - see README, 'YouTube setup'")
            creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES) \
                .run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(token, 0o600)
        except OSError:
            pass
    return build("youtube", "v3", credentials=creds)


def queue(con, cfg, only=None):
    """Approved clips that may go out now, best first, within today's limit."""
    today = dt.date.today().isoformat()
    done = len(store.fetch_clips(con, "status='uploaded' AND uploaded_at LIKE ?", (today + "%",)))
    left = max(cfg["youtube"]["max_per_day"] - done, 0)
    clips = store.fetch_clips(con, "status='approved'")
    if only:
        clips = [c for c in clips if c["id"] in only]
    return clips[:left], len(clips), left


def publish_youtube(con, cfg, dry_run=False, only=None, service=None, log=print):
    """Upload approved clips. Returns the list of (clip_id, youtube_id) uploaded."""
    todo, waiting, left = queue(con, cfg, only)
    if not waiting:
        log("nothing to upload: no approved clips. Approve some in `clipper review` first.")
        return []
    if not todo:
        log(f"daily limit reached ({cfg['youtube']['max_per_day']} per day); "
            f"{waiting} approved clip(s) wait until tomorrow")
        return []
    missing = [c for c in todo if not c["file"] or not Path(c["file"]).exists()]
    for c in missing:
        log(f"#{c['id']} skipped: its video file is missing ({c['file'] or 'never rendered'})")
    todo = [c for c in todo if c not in missing]
    if dry_run:
        for c in todo:
            title, _, _ = caption(c, cfg)
            log(f"[dry-run] would upload #{c['id']} as {cfg['youtube']['privacy']}: {title}")
        if waiting > len(todo) + len(missing):
            log(f"[dry-run] {waiting - len(todo) - len(missing)} more wait for tomorrow's limit")
        return []
    if not todo:
        return []
    yt = service or _youtube()
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    out = []
    for c in todo:
        try:
            req = yt.videos().insert(
                part="snippet,status", body=body(c, cfg),
                media_body=MediaFileUpload(c["file"], chunksize=8 * 1024 * 1024, resumable=True))
            resp = None
            while resp is None:
                _, resp = req.next_chunk(num_retries=3)
        except HttpError as e:
            reason = str(e)
            if "quotaExceeded" in reason or "uploadLimitExceeded" in reason:
                log("YouTube's daily upload quota is used up; the rest stay approved for later")
                break
            log(f"#{c['id']} failed to upload: {reason[:200]}")
            continue
        store.update_clip(con, c["id"], status="uploaded", youtube_id=resp["id"],
                          uploaded_at=store.now())
        out.append((c["id"], resp["id"]))
        log(f"uploaded #{c['id']} -> https://youtu.be/{resp['id']}")
    return out


def export(con, cfg, log=print):
    """Copy approved clips and their captions to data/export/. Returns the count."""
    folder = ensure_dirs()["export"]
    n = 0
    for c in store.fetch_clips(con, "status IN ('approved','uploaded')"):
        src = Path(c["file"]) if c["file"] else None
        if not src or not src.exists():
            log(f"#{c['id']} skipped: its video file is missing")
            continue
        dst = folder / f"{c['id']:04d}_{safe_name(c['streamer'])}.mp4"
        shutil.copy2(src, dst)
        _, desc, _ = caption(c, cfg)
        dst.with_suffix(".txt").write_text(desc, encoding="utf-8")
        n += 1
    log(f"exported {n} clip(s) to {folder}")
    return n
