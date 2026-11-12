"""The review page: watch each clip, fix its hook / title / cut points, then
approve or reject it. Nothing is ever published without an approval here.

It is a small local web server from the standard library (no extra packages).
It listens on this computer only, and every request must carry the random key
from the address printed at start-up, so another website open in the same
browser cannot read or change your clips.
"""
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ingest, media, pipeline, store

PAGE = Path(__file__).resolve().parent / "assets" / "review.html"
HOOK_MAX, TITLE_MAX = 90, 100
_render_lock = threading.Lock()


def public(clip):
    """The fields the page needs for one clip."""
    f = Path(clip["file"]) if clip["file"] else None
    has_file = bool(f and f.exists())
    return {
        "id": clip["id"], "streamer": clip["streamer"], "status": clip["status"],
        "title": clip["title"], "hook": clip["hook"], "reason": clip["reason"],
        "llm_score": clip["llm_score"], "score": clip["score"],
        "chat_score": clip["chat_score"], "accel_score": clip["accel_score"],
        "audio_score": clip["audio_score"], "start": clip["start"], "end": clip["end"],
        "transcript": clip["transcript"], "chat_sample": clip["chat_sample"],
        "youtube_id": clip["youtube_id"], "has_file": has_file,
        "has_thumb": bool(clip["thumb"] and Path(clip["thumb"]).exists()),
        "version": int(f.stat().st_mtime) if has_file else 0,
    }


def change_clip(con, clip_id, body, cfg):
    """Apply an edit from the page. Returns the updated clip; raises ValueError
    with a message for the reviewer when the edit is not acceptable."""
    clip = store.get_clip(con, clip_id)
    if not clip:
        raise ValueError("that clip no longer exists")
    if clip["status"] == "uploaded":
        raise ValueError("this clip is already uploaded and cannot be changed")
    if not isinstance(body, dict) or set(body) - {"status", "hook", "title", "start", "end"}:
        raise ValueError("unknown field in request")
    fields = {}
    if "hook" in body:
        hook = " ".join(str(body["hook"]).split())
        if len(hook) > HOOK_MAX:
            raise ValueError(f"the hook is too long ({len(hook)} characters, limit {HOOK_MAX})")
        fields["hook"] = hook
    if "title" in body:
        title = " ".join(str(body["title"]).split())
        if not title:
            raise ValueError("the title cannot be empty")
        if len(title) > TITLE_MAX:
            raise ValueError(f"the title is too long ({len(title)} characters, limit {TITLE_MAX})")
        if re.search(r"[<>]", title):
            raise ValueError("the title cannot contain < or >")
        fields["title"] = title
    status = body.get("status")
    if status is not None and status not in ("rendered", "approved", "rejected"):
        raise ValueError("status must be rendered, approved or rejected")
    start, end = clip["start"], clip["end"]
    for key in ("start", "end"):
        if key in body:
            v = body[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
                raise ValueError(f"{key} must be a time in seconds")
            if key == "start":
                start = float(v)
            else:
                end = float(v)
    retrim = abs(start - clip["start"]) > 0.004 or abs(end - clip["end"]) > 0.004
    if retrim and not 5 <= end - start <= 180:
        raise ValueError("a clip must be between 5 and 180 seconds long")
    rerender = retrim or ("hook" in fields and fields["hook"] != clip["hook"])
    if status == "approved" and not rerender and not (clip["file"] and Path(clip["file"]).exists()):
        raise ValueError("this clip has no video file yet; render it first")
    if fields:
        store.update_clip(con, clip_id, **fields)
    if rerender:
        with _render_lock:
            try:
                if retrim:
                    pipeline.retrim(con, clip_id, start, end, cfg, log=lambda *_: None)
                else:
                    pipeline.render_clip(con, clip_id, cfg, log=lambda *_: None)
            except (pipeline.PipelineError, media.MediaError, ingest.IngestError) as e:
                # put the old values back so the text matches the video on disk
                store.update_clip(con, clip_id, hook=clip["hook"], start=clip["start"],
                                  end=clip["end"], words=clip["words"],
                                  transcript=clip["transcript"])
                raise ValueError(f"could not re-render: {e}") from None
    if status:
        store.update_clip(con, clip_id, status=status)
    return store.get_clip(con, clip_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "clipper-review"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # -- helpers -------------------------------------------------------------

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorised(self, query):
        given = self.headers.get("X-Clipper-Key") or (query.get("k") or [""])[0]
        return secrets.compare_digest(given.encode(), self.server.key.encode())

    def _file(self, path, ctype):
        """Serve a file with Range support (needed for seeking in the video)."""
        try:
            size = path.stat().st_size
        except OSError:
            return self._send(404, {"error": "file not found"})
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        partial = False
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    buf = f.read(min(1 << 16, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- routes --------------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            if not self._authorised(query):
                return self._send(403, "Open the full address printed by `clipper review` "
                                       "(it ends in ?k=...).", "text/plain; charset=utf-8")
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", {
                "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; "
                                           "script-src 'unsafe-inline'; media-src 'self'; "
                                           "img-src 'self'; frame-ancestors 'none'"})
        if not self._authorised(query):
            return self._send(403, {"error": "missing or wrong key"})
        con = store.connect()
        try:
            if url.path == "/api/state":
                clips = store.fetch_clips(con, "status!='candidate'")
                return self._send(200, {"counts": store.counts(con),
                                        "clips": [public(c) for c in clips]})
            m = re.fullmatch(r"/(media|thumb)/(\d+)\.(mp4|jpg)", url.path)
            if m:
                clip = store.get_clip(con, int(m.group(2)))
                key = "file" if m.group(1) == "media" else "thumb"
                if not clip or not clip[key]:
                    return self._send(404, {"error": "not found"})
                return self._file(Path(clip[key]),
                                  "video/mp4" if key == "file" else "image/jpeg")
        finally:
            con.close()
        self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        url = urlparse(self.path)
        # the key must come in a header: a plain cross-site form post cannot set one
        self.close_connection = True      # replies below may leave the request body unread
        if not secrets.compare_digest((self.headers.get("X-Clipper-Key") or "").encode(),
                                      self.server.key.encode()):
            return self._send(403, {"error": "missing or wrong key"})
        m = re.fullmatch(r"/api/clip/(\d+)", url.path)
        if not m:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 65536:
                raise ValueError
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400, {"error": "request body must be JSON"})
        con = store.connect()
        try:
            clip = change_clip(con, int(m.group(1)), body, self.server.cfg)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        finally:
            con.close()
        self._send(200, public(clip))


def make_server(cfg, host=None, port=None, key=None):
    host = host or cfg["review"]["host"]
    port = cfg["review"]["port"] if port is None else port
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.cfg = cfg
    srv.key = key or secrets.token_urlsafe(18)
    return srv


def address(srv):
    host, port = srv.server_address[:2]
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"http://{shown}:{port}/?k={srv.key}"


def serve(cfg, host=None, port=None, log=print):
    try:
        srv = make_server(cfg, host, port)
    except OSError as e:
        raise SystemExit(f"could not start the review page on port "
                         f"{port or cfg['review']['port']}: {e}. Try --port with another number.")
    if srv.server_address[0] not in ("127.0.0.1", "::1", "localhost"):
        log("warning: the review page is reachable from other computers on your network; "
            "anyone with the address below can approve clips")
    log(f"review page: {address(srv)}")
    log("press Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
