import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import needs_ffmpeg

from clipper import pipeline, publish, review, store


def add_clip(con, workspace, status="rendered", **extra):
    """A clip row with small stand-in files (no real rendering needed)."""
    cid = store.insert_clip(con, dict({"vod_id": "v", "streamer": "example", "start": 10.0,
                                       "end": 40.0, "peak": 20.0, "title": "A title",
                                       "hook": "A hook", "llm_score": 50, "status": status},
                                      **extra))
    clips = workspace / "data" / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    f = clips / f"v_{cid:04d}.mp4"
    f.write_bytes(bytes(range(256)) * 8)
    t = f.with_suffix(".jpg")
    t.write_bytes(b"\xff\xd8jpeg")
    store.update_clip(con, cid, file=str(f), thumb=str(t))
    return cid


@pytest.fixture
def server(cfg):
    srv = review.make_server(cfg, port=0, key="secret-key")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


KEY = {"X-Clipper-Key": "secret-key"}


def test_everything_needs_the_key(server, con, workspace):
    cid = add_clip(con, workspace)
    assert call(server + "/")[0] == 403
    assert call(server + "/?k=wrong")[0] == 403
    assert call(server + "/api/state")[0] == 403
    assert call(server + f"/media/{cid}.mp4")[0] == 403
    assert call(server + f"/api/clip/{cid}", {"status": "approved"})[0] == 403
    # a key in the address is not enough to change anything: it must be a header
    assert call(server + f"/api/clip/{cid}?k=secret-key", {"status": "approved"})[0] == 403
    assert store.get_clip(con, cid)["status"] == "rendered"


def test_page_and_state(server, con, workspace):
    add_clip(con, workspace)
    add_clip(con, workspace, status="candidate")           # not shown: not rendered yet
    status, page, headers = call(server + "/?k=secret-key")
    assert status == 200 and b"Clip review" in page
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, raw, _ = call(server + "/api/state", headers=KEY)
    state = json.loads(raw)
    assert status == 200 and len(state["clips"]) == 1
    assert state["counts"]["rendered"] == 1 and state["counts"]["candidate"] == 1
    clip = state["clips"][0]
    assert clip["has_file"] and clip["has_thumb"] and "file" not in clip   # no disk paths leak


def test_video_range_requests(server, con, workspace):
    cid = add_clip(con, workspace)
    url = server + f"/media/{cid}.mp4?k=secret-key"
    status, body, headers = call(url)
    assert status == 200 and len(body) == 2048 and headers["Accept-Ranges"] == "bytes"
    status, body, headers = call(url, headers={"Range": "bytes=10-19"})
    assert status == 206 and body == bytes(range(10, 20))
    assert headers["Content-Range"] == "bytes 10-19/2048"
    status, body, _ = call(url, headers={"Range": "bytes=-4"})
    assert status == 206 and len(body) == 4
    assert call(url, headers={"Range": "bytes=5000-"})[0] == 416
    assert call(server + f"/thumb/{cid}.jpg?k=secret-key")[0] == 200
    assert call(server + "/media/999.mp4?k=secret-key")[0] == 404
    assert call(server + "/media/../config.yaml?k=secret-key")[0] == 404


def test_approve_reject_and_edit_title(server, con, workspace):
    cid = add_clip(con, workspace)
    url = server + f"/api/clip/{cid}"
    status, raw, _ = call(url, {"status": "approved", "title": "  Better   title "}, KEY)
    assert status == 200 and json.loads(raw)["status"] == "approved"
    assert store.get_clip(con, cid)["title"] == "Better title"
    assert call(url, {"status": "rejected"}, KEY)[0] == 200
    assert store.get_clip(con, cid)["status"] == "rejected"
    assert call(url, {"status": "rendered"}, KEY)[0] == 200


@pytest.mark.parametrize("body, fragment", [
    ({"status": "uploaded"}, "status must be"),
    ({"title": ""}, "cannot be empty"),
    ({"title": "x" * 101}, "too long"),
    ({"title": "a <b> c"}, "< or >"),
    ({"hook": "x" * 91}, "too long"),
    ({"start": -1}, "time in seconds"),
    ({"start": "soon"}, "time in seconds"),
    ({"start": 10, "end": 12}, "between 5 and 180"),
    ({"file": "/etc/passwd"}, "unknown field"),
    ({"status": "approved", "extra": 1}, "unknown field"),
])
def test_bad_edits_are_refused(server, con, workspace, body, fragment):
    cid = add_clip(con, workspace)
    before = store.get_clip(con, cid)
    status, raw, _ = call(server + f"/api/clip/{cid}", body, KEY)
    assert status == 400 and fragment in json.loads(raw)["error"]
    assert store.get_clip(con, cid) == before


def test_bad_requests(server, con, workspace):
    cid = add_clip(con, workspace)
    req = urllib.request.Request(server + f"/api/clip/{cid}", data=b"not json",
                                 headers=dict(KEY, **{"Content-Type": "application/json"}))
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
    assert call(server + "/api/clip/999", {"status": "approved"}, KEY)[0] == 400
    assert call(server + "/api/other", {"x": 1}, KEY)[0] == 404


def test_uploaded_clip_cannot_be_changed(con, workspace, cfg):
    cid = add_clip(con, workspace, status="uploaded")
    with pytest.raises(ValueError) as e:
        review.change_clip(con, cid, {"title": "new"}, cfg)
    assert "already uploaded" in str(e.value)


def test_cannot_approve_a_clip_without_video(con, workspace, cfg):
    cid = store.insert_clip(con, {"vod_id": "v", "streamer": "example", "start": 1, "end": 20,
                                  "status": "rendered", "title": "t"})
    with pytest.raises(ValueError) as e:
        review.change_clip(con, cid, {"status": "approved"}, cfg)
    assert "no video file" in str(e.value)


@needs_ffmpeg
def test_changing_hook_or_trim_rerenders(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    row = pipeline.render_clip(con, ids[0], cfg, log=lambda *_: None)
    ass = Path(row["file"]).with_suffix(".ass")
    out = review.change_clip(con, row["id"], {"hook": "Brand new hook", "status": "approved"}, cfg)
    assert out["status"] == "approved" and "Brand new hook" in ass.read_text()
    out = review.change_clip(con, row["id"], {"end": row["end"] - 4}, cfg)
    assert out["end"] == row["end"] - 4 and out["status"] == "approved"


@needs_ffmpeg
def test_failed_rerender_puts_the_old_values_back(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    row = pipeline.render_clip(con, ids[0], cfg, log=lambda *_: None)
    with pytest.raises(ValueError) as e:        # far beyond the end of the 150 s source
        review.change_clip(con, row["id"], {"start": 900, "end": 930, "hook": "x"}, cfg)
    assert "could not re-render" in str(e.value)
    after = store.get_clip(con, row["id"])
    assert (after["start"], after["end"], after["hook"]) == (row["start"], row["end"], row["hook"])


# ---- publishing ------------------------------------------------------------


def test_caption_and_body(cfg, con, workspace):
    cid = add_clip(con, workspace, title="  He <really> did   that ")
    clip = store.get_clip(con, cid)
    title, desc, tags = publish.caption(clip, cfg)
    assert title == "He really did that"
    assert "Clipped from @example on Twitch" in desc and "#shorts" in desc.lower()
    assert "shorts" in tags
    b = publish.body(clip, cfg)
    assert b["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    assert b["snippet"]["categoryId"] == "20" and len(b["snippet"]["title"]) <= 100


def test_dry_run_changes_nothing(cfg, con, workspace):
    cid = add_clip(con, workspace, status="approved")
    add_clip(con, workspace, status="rendered")
    logs = []
    assert publish.publish_youtube(con, cfg, dry_run=True, log=logs.append) == []
    assert len(logs) == 1 and f"#{cid}" in logs[0] and "private" in logs[0]
    assert store.get_clip(con, cid)["status"] == "approved"


def test_only_approved_clips_are_uploaded(cfg, con, workspace):
    add_clip(con, workspace, status="rendered")
    add_clip(con, workspace, status="rejected")
    logs = []
    assert publish.publish_youtube(con, cfg, service=object(), log=logs.append) == []
    assert "no approved clips" in logs[0]


class FakeYouTube:
    """Records what would be sent to the YouTube API."""

    def __init__(self):
        self.sent = []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.sent.append(body)
        n = len(self.sent)
        return SimpleNamespace(next_chunk=lambda num_retries=0: (None, {"id": f"yt{n}"}))


def test_upload_marks_clips_and_respects_daily_limit(cfg, con, workspace):
    pytest.importorskip("googleapiclient")
    cfg["youtube"]["max_per_day"] = 2
    ids = [add_clip(con, workspace, status="approved", llm_score=90 - i) for i in range(3)]
    yt = FakeYouTube()
    done = publish.publish_youtube(con, cfg, service=yt, log=lambda *_: None)
    assert [c for c, _ in done] == ids[:2]                    # best first, two per day
    assert store.get_clip(con, ids[0])["youtube_id"] == "yt1"
    assert store.get_clip(con, ids[0])["status"] == "uploaded"
    assert store.get_clip(con, ids[2])["status"] == "approved"
    logs = []
    assert publish.publish_youtube(con, cfg, service=yt, log=logs.append) == []
    assert "daily limit" in logs[0] and len(yt.sent) == 2


def test_missing_video_file_is_skipped(cfg, con, workspace):
    cid = add_clip(con, workspace, status="approved")
    Path(store.get_clip(con, cid)["file"]).unlink()
    logs = []
    assert publish.publish_youtube(con, cfg, dry_run=True, log=logs.append) == []
    assert "missing" in logs[0]


def test_export_copies_approved_clips_with_captions(cfg, con, workspace):
    cid = add_clip(con, workspace, status="approved")
    add_clip(con, workspace, status="rendered")
    assert publish.export(con, cfg, log=lambda *_: None) == 1
    out = workspace / "data" / "export"
    assert (out / f"{cid:04d}_example.mp4").exists()
    assert "Clipped from" in (out / f"{cid:04d}_example.txt").read_text()


def test_youtube_setup_missing_is_a_clear_error(cfg, con, workspace):
    pytest.importorskip("googleapiclient")
    pytest.importorskip("google_auth_oauthlib")
    add_clip(con, workspace, status="approved")
    with pytest.raises(publish.PublishError) as e:
        publish.publish_youtube(con, cfg, log=lambda *_: None)
    assert "client_secret.json" in str(e.value)
