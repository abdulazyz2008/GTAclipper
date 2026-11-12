"""Downloads and live watching, exercised offline.

A small local web server stands in for a VOD site, and the live watcher is
fed a video file played back faster than real time.
"""
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import make_chat, near, needs_ffmpeg

from clipper import chat as chatmod
from clipper import ingest, live, media, pipeline, store

pytestmark = needs_ffmpeg


@pytest.fixture
def site(vod):
    """Serve the test video over HTTP with Range support, like a real host."""
    root = vod.parent

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            p = root / Path(self.path.split("?")[0]).name
            if not p.is_file():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            size = p.stat().st_size
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
            if m:
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            self.send_response(206 if m else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if m:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with p.open("rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_HEAD = do_GET

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/{vod.name}"
    srv.shutdown()
    srv.server_close()


yt_dlp = pytest.importorskip("yt_dlp")


def test_describe_file_and_url(vod, site, workspace):
    info = ingest.describe(str(vod))
    assert info["id"] == "teststream" and info["path"] == vod.resolve() and not info["is_live"]
    info = ingest.describe(site)
    assert info["id"] == "teststream" and info["path"] is None and info["url"] == site
    with pytest.raises(ingest.IngestError):
        ingest.describe("no-such-file.mp4")
    with pytest.raises(ingest.IngestError):
        ingest.describe("http://127.0.0.1:9/missing.mp4")


def test_section_download_and_alignment(vod, site, workspace, tmp_path):
    sec = ingest.get_section(site, "teststream", 41.3, 68.0, 1080)
    info = media.probe(sec)
    assert info["has_video"] and near(info["duration"], 26.7, 1.5)
    assert ingest.get_section(site, "teststream", 41.3, 68.0, 1080) == sec     # cached
    assert near(ingest.align_section(sec, vod, 41.3), 41.3, 0.2)


def test_alignment_recovers_a_section_that_starts_early(vod, tmp_path):
    early = tmp_path / "early.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-ss", "37.9", "-t", "30", "-i", str(vod),
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(early)])
    assert near(ingest.align_section(early, vod, 41.3), 37.9, 0.15)


def test_alignment_falls_back_when_audio_is_silent(tmp_path, vod):
    silent = tmp_path / "silent.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
               "testsrc2=size=320x180:rate=30:duration=10", "-f", "lavfi", "-i",
               "anullsrc=r=44100:cl=mono", "-shortest", "-c:v", "libx264", "-preset",
               "ultrafast", "-c:a", "aac", str(silent)])
    assert ingest.align_section(silent, vod, 20.0) == 20.0


def test_a_clip_with_no_video_yet_fetches_only_its_section(vod, site, cfg, con):
    """The 'smart' path: the clip row knows the URL and the audio; the video
    section is downloaded when the clip is rendered."""
    cid = store.insert_clip(con, {"vod_id": "teststream", "source": site, "streamer": "nocam",
                                  "audio_path": str(vod), "start": 46.0, "end": 60.0, "peak": 50.0,
                                  "title": "t"})
    row = pipeline.render_clip(con, cid, cfg, log=lambda *_: None)
    assert row["status"] == "rendered" and Path(row["vod_path"]).name.startswith("teststream.s")
    assert near(row["media_start"], 38.0, 0.3)                       # 46 - section_pad
    assert media.probe(row["vod_path"])["duration"] < 40             # not the whole VOD
    assert near(media.probe(row["file"])["duration"], 14, 0.3)


def test_run_from_url_end_to_end(site, cfg, con, vod):
    ids = pipeline.scan(con, site, "nocam", cfg, chat_file=str(vod.with_suffix(".chat.json")),
                        use_whisper=False, log=lambda *_: None)
    assert len(ids) == 2
    pipeline.judge(con, ids, "nocam", cfg, use_llm=False, log=lambda *_: None)
    done = pipeline.render_top(con, ids, cfg, limit=1, log=lambda *_: None)
    assert len(done) == 1 and Path(done[0]["file"]).exists()


def test_chat_file_and_sidecar(vod, workspace, tmp_path):
    assert len(ingest.get_chat(str(vod), "teststream")) > 100          # teststream.chat.json beside it
    with pytest.raises(ingest.IngestError):
        ingest.get_chat(str(vod), "teststream", chat_file=str(tmp_path / "nope.json"))


def test_live_watch_on_a_simulated_stream(vod, cfg, con):
    """Play the test video as a live stream at 10x. The watcher must cut the
    same two moments the VOD path finds, while the 'stream' is still running."""
    logs = []
    w = live.Watcher(con, str(vod), "example", cfg, chat_source=chatmod.ReplayChat(make_chat()),
                     simulate=True, speed=10, use_whisper=False, use_llm=False, log=logs.append)
    ids = w.run()
    assert len(ids) == 2, logs
    rows = [store.get_clip(con, i) for i in ids]
    starts = sorted(r["start"] for r in rows)
    assert near(starts[0], 44, 6) and near(starts[1], 104, 6)
    for r in rows:
        assert r["status"] == "rendered" and Path(r["file"]).exists()
        assert near(media.probe(r["file"])["duration"], r["end"] - r["start"], 0.4)
        assert r["media_start"] <= r["start"]
    status = live.latest_status()
    assert status["state"] == "finished" and status["clips"] == ids
    assert near(status["recorded_min"], 2.5, 0.2)
    assert not list(w.folder.glob("seg_*.ts"))                # raw recording is cleaned up
    assert len(list(w.folder.glob("moment_*.ts"))) == 2       # each clip keeps its own footage


def test_live_and_vod_agree(vod, cfg, con):
    vod_ids = pipeline.scan(con, str(vod), "nocam", cfg, use_whisper=False, log=lambda *_: None)
    vod_peaks = sorted(store.get_clip(con, i)["peak"] for i in vod_ids)
    w = live.Watcher(con, str(vod), "example", cfg, chat_source=chatmod.ReplayChat(make_chat()),
                     simulate=True, speed=10, use_whisper=False, use_llm=False,
                     log=lambda *_: None)
    live_peaks = sorted(store.get_clip(con, i)["peak"] for i in w.run())
    assert live_peaks == vod_peaks


def test_live_offline_stream_is_reported(cfg, con, tmp_path):
    w = live.Watcher(con, str(tmp_path / "missing.mp4"), "example", cfg, simulate=True,
                     log=lambda *_: None)
    with pytest.raises(live.LiveError) as e:
        w.run()
    assert "not live" in str(e.value)


def test_live_time_limit(vod, cfg, con):
    w = live.Watcher(con, str(vod), "example", cfg, chat_source=chatmod.ReplayChat([]),
                     simulate=True, speed=10, use_whisper=False, use_llm=False,
                     max_minutes=0.5, log=lambda *_: None)
    w.run()
    assert live.latest_status()["recorded_min"] < 1.5


def test_source_command_picks_a_recorder(cfg):
    cmd = live.source_command("https://www.twitch.tv/example", cfg)
    assert "https://www.twitch.tv/example" in cmd
    assert cmd[2] in ("streamlink", "yt_dlp")
