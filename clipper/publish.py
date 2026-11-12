"""Step 6 - publish approved clips.

YouTube: uploaded through the Data API (needs client_secret.json, see README).
TikTok / Instagram: `export` writes each clip plus a ready caption .txt into
data/export/ so you can post by hand or through a scheduler.
"""
import datetime as dt
import shutil
from pathlib import Path

from .common import EXPORT, ROOT, db, fetch_clips, update_clip

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _caption(clip, cfg):
    s = cfg["streamers"].get(clip["streamer"], {})
    credit = s.get("credit", clip["streamer"])
    tags = s.get("hashtags", "#shorts")
    return clip["title"], f"{clip['title']}\n\nClipped from {credit}\n{tags}"


def _youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token = ROOT / "token.json"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = ROOT / "client_secret.json"
            if not secret.exists():
                raise SystemExit("client_secret.json missing - see README, 'YouTube setup'")
            creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES) \
                .run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def publish_youtube(cfg, dry_run=False):
    con = db()
    today = dt.date.today().isoformat()
    done = len(fetch_clips(con, "status='uploaded' AND uploaded_at LIKE ?", (today + "%",)))
    left = cfg["youtube"]["max_per_day"] - done
    queue = fetch_clips(con, "status='approved'")[: max(left, 0)]
    if not queue:
        print("nothing to upload (no approved clips, or daily cap reached)")
        return
    yt = None if dry_run else _youtube()
    if not dry_run:
        from googleapiclient.http import MediaFileUpload
    for c in queue:
        title, desc = _caption(c, cfg)
        if dry_run:
            print(f"[dry-run] would upload #{c['id']}: {title}")
            continue
        body = {
            "snippet": {"title": title[:100], "description": desc,
                        "categoryId": cfg["youtube"]["category_id"]},
            "status": {"privacyStatus": cfg["youtube"]["privacy"],
                       "selfDeclaredMadeForKids": False},
        }
        req = yt.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(c["file"], chunksize=-1, resumable=True))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        update_clip(con, c["id"], status="uploaded", youtube_id=resp["id"],
                    uploaded_at=dt.datetime.now().isoformat(timespec="seconds"))
        print(f"uploaded #{c['id']} -> https://youtu.be/{resp['id']}")


def export(cfg):
    con = db()
    n = 0
    for c in fetch_clips(con, "status IN ('approved','uploaded')"):
        src = Path(c["file"])
        if not src.exists():
            continue
        EXPORT.mkdir(parents=True, exist_ok=True)
        dst = EXPORT / f"{c['id']:04d}_{c['streamer']}.mp4"
        shutil.copy2(src, dst)
        _, desc = _caption(c, cfg)
        dst.with_suffix(".txt").write_text(desc, encoding="utf-8")
        n += 1
    print(f"exported {n} clip(s) to {EXPORT}")
