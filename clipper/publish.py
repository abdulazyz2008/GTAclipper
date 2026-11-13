"""Publish approved clips.

Only clips a person approved in review are ever uploaded, and each platform
has its own daily limit.

YouTube    the official Data API with your own Google project
           (README, "YouTube setup").
Instagram  Reels through Meta's Graph API with your own app and token
           (README, "Instagram setup").
Others     `export` writes each approved clip plus a ready-to-paste caption
           into data/export/ for TikTok or a scheduler.
"""
import datetime as dt
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import store
from .config import ensure_dirs, home, safe_name

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class PublishError(Exception):
    pass


GRAPH_URL = "https://graph.facebook.com"
UPLOAD_URL = "https://rupload.facebook.com"
PLATFORMS = {
    "youtube": {"id": "youtube_id", "at": "uploaded_at"},
    "instagram": {"id": "instagram_id", "at": "instagram_at"},
}


def caption(clip, cfg, platform="youtube"):
    """Return (title, description, tags) for a clip."""
    s = cfg["streamers"].get(clip["streamer"], {})
    credit = s.get("credit") or clip["streamer"]
    hashtags = s.get("hashtags") or "#shorts"
    if platform == "youtube" and not re.search(r"(?i)#shorts\b", hashtags):
        hashtags += " #shorts"
    if platform == "instagram":
        hashtags = re.sub(r"(?i)#shorts\b", "#reels", hashtags)
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


def queue(con, cfg, only=None, platform="youtube"):
    """Approved clips not yet on `platform`, best first, within today's limit.
    Returns (clips to send now, how many are waiting, how many today's limit allows)."""
    col = PLATFORMS[platform]
    today = dt.date.today().isoformat()
    done = len(store.fetch_clips(con, f"{col['at']} LIKE ?", (today + "%",)))
    left = max(cfg[platform]["max_per_day"] - done, 0)
    clips = store.fetch_clips(con, f"status IN ('approved','uploaded') AND {col['id']}=''")
    if only:
        clips = [c for c in clips if c["id"] in only]
    return clips[:left], len(clips), left


def _ready(con, cfg, only, platform, dry_run, log):
    """Shared checks before an upload run. Returns the clips to upload now."""
    name = "YouTube" if platform == "youtube" else "Instagram"
    todo, waiting, left = queue(con, cfg, only, platform)
    if not waiting:
        log(f"nothing to upload to {name}: no approved clips. "
            "Approve some in `clipper review` first.")
        return []
    if not todo:
        log(f"{name}: daily limit reached ({cfg[platform]['max_per_day']} per day); "
            f"{waiting} approved clip(s) wait until tomorrow")
        return []
    missing = [c for c in todo if not c["file"] or not Path(c["file"]).exists()]
    for c in missing:
        log(f"#{c['id']} skipped: its video file is missing ({c['file'] or 'never rendered'})")
    todo = [c for c in todo if c not in missing]
    if dry_run:
        for c in todo:
            title, _, _ = caption(c, cfg, platform)
            how = f" as {cfg['youtube']['privacy']}" if platform == "youtube" else ""
            log(f"[dry-run] would upload #{c['id']} to {name}{how}: {title}")
        if waiting > len(todo) + len(missing):
            log(f"[dry-run] {waiting - len(todo) - len(missing)} more wait for tomorrow's limit")
        return []
    return todo


def publish_youtube(con, cfg, dry_run=False, only=None, service=None, log=print):
    """Upload approved clips. Returns the list of (clip_id, youtube_id) uploaded."""
    todo = _ready(con, cfg, only, "youtube", dry_run, log)
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


# ---- Instagram Reels -------------------------------------------------------


def instagram_token():
    """The access token: $INSTAGRAM_TOKEN, or instagram_token.txt in the workspace."""
    tok = os.environ.get("INSTAGRAM_TOKEN", "").strip()
    f = home() / "instagram_token.txt"
    if not tok and f.exists():
        tok = f.read_text(encoding="utf-8").strip()
    return tok


def instagram_configured(cfg):
    return bool(cfg["instagram"]["user_id"] and instagram_token())


def _graph(method, url, token, fields=None, data=None, headers=None, timeout=300):
    """One Graph API call. Returns the decoded JSON; raises PublishError with
    Meta's own error message. The token travels in a header, never in the URL."""
    h = {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    if fields is not None and method == "GET":
        url += "?" + urllib.parse.urlencode(fields)
    elif fields is not None:
        data = urllib.parse.urlencode(fields).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = raw.decode("utf-8", "replace")[:200]
        raise PublishError(f"Instagram said: {msg} (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise PublishError(f"could not reach Instagram: {e}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise PublishError("Instagram sent an unreadable reply") from None


def _instagram_one(clip, cfg, token, wait=5.0, timeout=600):
    """Upload one clip as a Reel: container -> bytes -> wait -> publish. Returns the media id."""
    ig = cfg["instagram"]
    base = f"{GRAPH_URL}/{ig['api_version']}"
    _, text, _ = caption(clip, cfg, "instagram")
    made = _graph("POST", f"{base}/{ig['user_id']}/media", token, fields={
        "media_type": "REELS", "upload_type": "resumable", "caption": text,
        "share_to_feed": "true" if ig["share_to_feed"] else "false"})
    container = str(made.get("id") or "")
    if not re.fullmatch(r"[0-9]+", container):
        raise PublishError("Instagram did not return an upload id")
    # always our own upload address: the token must never follow a link from a reply
    size = os.path.getsize(clip["file"])
    with open(clip["file"], "rb") as f:
        sent = _graph("POST", f"{UPLOAD_URL}/ig-api-upload/{ig['api_version']}/{container}",
                      token, data=f, headers={"Authorization": f"OAuth {token}", "offset": "0",
                                              "file_size": str(size), "Content-Length": str(size),
                                              "Content-Type": "application/octet-stream"})
    if sent.get("success") is False:
        raise PublishError("Instagram refused the video file")
    deadline = time.time() + timeout
    while True:
        st = _graph("GET", f"{base}/{container}", token, fields={"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram could not process the video: {st.get('status') or code}")
        if time.time() > deadline:
            raise PublishError("Instagram is still processing the video; try again later")
        time.sleep(wait)
    done = _graph("POST", f"{base}/{ig['user_id']}/media_publish", token,
                  fields={"creation_id": container})
    if not done.get("id"):
        raise PublishError("Instagram did not confirm the post")
    return str(done["id"])


def publish_instagram(con, cfg, dry_run=False, only=None, log=print, wait=5.0):
    """Post approved clips as Reels. Returns the list of (clip_id, instagram_id)."""
    todo = _ready(con, cfg, only, "instagram", dry_run, log)
    if not todo:
        return []
    token = instagram_token()
    if not cfg["instagram"]["user_id"] or not token:
        raise PublishError("Instagram is not set up: put your account id in config.yaml "
                           "(instagram.user_id) and your token in instagram_token.txt - "
                           "see README, 'Instagram setup'")
    out = []
    for c in todo:
        try:
            media_id = _instagram_one(c, cfg, token, wait=wait)
        except PublishError as e:
            log(f"#{c['id']} failed to post: {e}")
            if "HTTP 401" in str(e) or "HTTP 403" in str(e) or "access token" in str(e).lower():
                log("stopping: the Instagram token was not accepted")
                break
            continue
        store.update_clip(con, c["id"], status="uploaded", instagram_id=media_id,
                          instagram_at=store.now())
        out.append((c["id"], media_id))
        log(f"posted #{c['id']} to Instagram (media id {media_id})")
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
