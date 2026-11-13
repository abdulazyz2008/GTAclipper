"""Autopilot, background jobs, the MCP server and Instagram posting."""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import make_chat, near, needs_ffmpeg
from test_review_publish import add_clip

from clipper import autopilot, cli, config, jobs, mcp_server, pipeline, publish, store
from clipper import chat as chatmod

# ---- background jobs -------------------------------------------------------


def test_job_runs_and_reports(workspace):
    job = jobs.start(["list"], "list")
    assert job["state"] == "running" and job["args"] == ["list"]
    done = jobs.wait(job["id"], timeout=30)
    assert done["state"] == "finished" and "no clips yet" in done["log"]
    assert jobs.list_jobs()[0]["id"] == job["id"]


def test_failed_job_shows_why(workspace):
    done = jobs.wait(jobs.start(["render", "999"], "render")["id"], timeout=30)
    assert done["state"] == "failed" and "no clip with id 999" in done["log"]
    assert jobs.status("nope") is None and jobs.stop("nope") is None


def test_job_can_be_stopped_gracefully(workspace):
    job = jobs.start(["review", "--port", "0"], "review")
    deadline = time.time() + 20
    while "review page:" not in jobs.log_tail(job["id"]) and time.time() < deadline:
        time.sleep(0.2)
    assert jobs.running("review")["id"] == job["id"]
    jobs.stop(job["id"])
    assert jobs.wait(job["id"], timeout=20)["state"] in ("finished", "stopped")
    assert jobs.running("review") is None


# ---- autopilot -------------------------------------------------------------


@needs_ffmpeg
def test_autopilot_watches_several_streamers_at_once(vod, cfg, con):
    logs = []
    ap = autopilot.Autopilot(
        cfg, sources={"example": str(vod), "nocam": str(vod)},
        chats={"example": chatmod.ReplayChat(make_chat()), "nocam": chatmod.ReplayChat(make_chat())},
        simulate=True, speed=10, use_whisper=False, use_llm=False, once=True, log=logs.append)
    ids = ap.run()
    rows = [store.get_clip(con, i) for i in ids]
    by = {name: sorted(r["start"] for r in rows if r["streamer"] == name)
          for name in ("example", "nocam")}
    for name, starts in by.items():
        assert len(starts) == 2, (name, logs)
        assert near(starts[0], 44, 6) and near(starts[1], 104, 6)
    assert all(r["status"] == "rendered" and Path(r["file"]).exists() for r in rows)
    assert any(line.startswith("[example]") for line in logs)
    status = autopilot.read_status()
    assert status["streamers"]["nocam"]["streams"] == 1
    assert sorted(status["streamers"]["example"]["clips"] + status["streamers"]["nocam"]["clips"]) == ids


@needs_ffmpeg
def test_autopilot_offline_channel_is_not_an_error(cfg, con, tmp_path):
    ap = autopilot.Autopilot(cfg, streamers=["example"], sources={"example": str(tmp_path / "x.mp4")},
                             simulate=True, once=True, log=lambda *_: None)
    assert ap.run() == []
    st = autopilot.read_status()["streamers"]["example"]
    assert st["error"] == "" and st["streams"] == 0
    assert not list((config.paths()["live"]).glob("*"))          # nothing left behind


def test_autopilot_needs_a_channel(cfg):
    with pytest.raises(config.ConfigError) as e:
        autopilot.Autopilot(cfg)
    assert "channel" in str(e.value)
    with pytest.raises(config.ConfigError):
        autopilot.Autopilot(cfg, streamers=["ghost"])


# ---- MCP server ------------------------------------------------------------


def rpc(method, params=None, mid=1):
    return mcp_server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def tool(name, **args):
    res = rpc("tools/call", {"name": name, "arguments": args})["result"]
    return res["isError"], res["content"]


def test_mcp_handshake_and_tool_list(workspace):
    init = rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "clipper"
    assert rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] in \
        mcp_server.PROTOCOLS
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc("ping")["result"] == {}
    assert rpc("nope")["error"]["code"] == -32601
    tools = rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) >= 15
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"
        assert set(t["inputSchema"]["required"]) <= set(t["inputSchema"]["properties"])


def test_mcp_status_and_unknown_tool(workspace):
    err, content = tool("status")
    info = json.loads(content[0]["text"])
    assert not err and set(info["streamers"]) == {"example", "nocam"}
    assert info["clips"]["rendered"] == 0 and info["autopilot"] is None
    assert tool("does_not_exist")[0] is True


def test_mcp_add_streamer_needs_permission_and_keeps_config_valid(workspace):
    err, content = tool("add_streamer", key="kai", credit="@kai on Twitch",
                        channel="https://www.twitch.tv/kai")
    assert err and "permission" in content[0]["text"]
    assert "kai" not in config.load_config()["streamers"]

    err, _ = tool("add_streamer", key="kai", credit="@kai on Twitch",
                  channel="https://www.twitch.tv/kai", permission_confirmed=True)
    assert not err
    cfg = config.load_config()
    assert cfg["streamers"]["kai"]["channel"] == "https://www.twitch.tv/kai"
    assert "example" in cfg["streamers"] and cfg["detect"]["clip_len"] == 14     # rest untouched
    assert (workspace / "config.yaml.bak").exists()

    before = (workspace / "config.yaml").read_text()
    err, content = tool("add_streamer", key="bad", permission_confirmed=True,
                        facecam={"x": 0, "y": 0, "w": 9000, "h": 100})
    assert err and "does not fit" in content[0]["text"]
    assert (workspace / "config.yaml").read_text() == before
    assert tool("add_streamer", key="bad name", permission_confirmed=True)[0]


def test_mcp_approval_and_publish_need_the_users_word(workspace):
    con = store.connect()
    cid = add_clip(con, workspace)
    err, content = tool("set_clip_status", clip_ids=[cid], status="approved")
    assert err and "user" in content[0]["text"]
    assert store.get_clip(con, cid)["status"] == "rendered"
    err, _ = tool("set_clip_status", clip_ids=[cid], status="approved", decided_by_user=True)
    assert not err and store.get_clip(con, cid)["status"] == "approved"

    err, content = tool("publish")                               # dry run by default
    assert not err and f"would upload #{cid}" in content[0]["text"]
    err, content = tool("publish", dry_run=False)
    assert err and "confirmed_by_user" in content[0]["text"]
    assert store.get_clip(con, cid)["status"] == "approved" and not jobs.list_jobs()
    assert tool("set_clip_status", clip_ids="3", status="approved", decided_by_user=True)[0]
    con.close()


@needs_ffmpeg
def test_mcp_judging_flow(vod, workspace):
    con = store.connect()
    ids = pipeline.scan(con, str(vod), "example", config.load_config(), use_whisper=False,
                        log=lambda *_: None)
    err, content = tool("clips_to_judge")
    data = json.loads(content[0]["text"])
    assert not err and data["waiting"] == 2 and data["clips"][0]["chat_said"]

    err, content = tool("clip_frames", clip_id=ids[0])
    assert not err and content[0]["type"] == "image" and content[0]["mimeType"] == "image/jpeg"
    assert len(content[0]["data"]) > 1000 and "stream seconds" in content[1]["text"]

    err, content = tool("judge_clips", judgements=[
        {"id": ids[0], "score": 500, "hook": "x", "title": "t"}])
    assert err and "Nothing was changed" in content[0]["text"]
    err, _ = tool("judge_clips", judgements=[
        {"id": ids[0], "score": 88, "hook": "Chat lost it", "title": "A real title", "reason": "r"},
        {"id": ids[1], "score": 10, "hook": "", "title": "Raid", "reason": "raid"}])
    assert not err and store.get_clip(con, ids[0])["judge"] == "claude"
    assert json.loads(tool("clips_to_judge")[1][0]["text"])["waiting"] == 0

    err, content = tool("render_clips", clip_ids=[ids[0]])
    job = json.loads(content[0]["text"])
    assert not err and jobs.wait(job["job_id"], timeout=120)["state"] == "finished"
    assert store.get_clip(con, ids[0])["status"] == "rendered"
    listed = json.loads(tool("list_clips", status="rendered")[1][0]["text"])
    assert [c["id"] for c in listed] == [ids[0]] and listed[0]["hook"] == "Chat lost it"
    con.close()


def test_mcp_review_page_and_stop(workspace):
    err, content = tool("review_page")
    assert not err and "http://127.0.0.1:" in content[0]["text"] and "?k=" in content[0]["text"]
    job = jobs.running("review")
    assert tool("review_page")[1][0]["text"] == content[0]["text"]      # reuses the running page
    assert not tool("stop_job", job_id=job["id"])[0]
    assert jobs.wait(job["id"], timeout=20)["state"] in ("finished", "stopped")


def test_mcp_over_real_stdio(workspace):
    """Start `clipper mcp` as a client would and talk to it over its pipes."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "export_clips", "arguments": {}}}]
    p = subprocess.run([sys.executable, "-m", "clipper", "--home", str(workspace), "mcp"],
                       input="\n".join(json.dumps(m) for m in msgs) + "\nnot json\n",
                       capture_output=True, text=True, timeout=60)
    replies = [json.loads(line) for line in p.stdout.splitlines()]     # stdout is protocol only
    assert [r["id"] for r in replies] == [1, 2, 3, None]
    assert "exported 0 clip(s)" in replies[2]["result"]["content"][0]["text"]
    assert replies[3]["error"]["code"] == -32700


def test_mcp_config_and_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path))
    cli.main(["init"])
    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["clipper"]
    assert entry["command"] == sys.executable and entry["args"][-1] == "mcp"
    assert str(tmp_path) in entry["args"]
    capsys.readouterr()
    cli.main(["mcp-config"])
    out = capsys.readouterr().out
    assert "claude mcp add clipper" in out and '"clipper"' in out


# ---- Instagram -------------------------------------------------------------


class FakeMeta:
    """A stand-in for Meta's Graph API + upload host, recording what it was sent."""

    def __init__(self, fail_with=None, polls=1):
        self.calls, self.uploaded, self.fail_with, self.polls = [], None, fail_with, polls
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.calls.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, b""))
                outer.polls -= 1
                self._reply(200, {"status_code": "IN_PROGRESS" if outer.polls > 0 else "FINISHED"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.calls.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if outer.fail_with:
                    return self._reply(outer.fail_with[0], {"error": {"message": outer.fail_with[1]}})
                if "/ig-api-upload/" in self.path:
                    outer.uploaded = body
                    return self._reply(200, {"success": True})
                if self.path.endswith("/media_publish"):
                    return self._reply(200, {"id": "17900000000000001"})
                self._reply(200, {"id": "555", "uri": "https://evil.example/steal-the-token"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def meta(monkeypatch, workspace):
    fake = FakeMeta(polls=2)
    monkeypatch.setattr(publish, "GRAPH_URL", fake.url)
    monkeypatch.setattr(publish, "UPLOAD_URL", fake.url)
    monkeypatch.setenv("INSTAGRAM_TOKEN", "tok-123")
    yield fake
    fake.close()


def ig_cfg():
    cfg = config.load_config()
    cfg["instagram"]["user_id"] = "17841400000000000"
    return cfg


def test_instagram_reel_upload_flow(meta, con, workspace):
    cid = add_clip(con, workspace, status="approved", title="He did that")
    logs = []
    done = publish.publish_instagram(con, ig_cfg(), log=logs.append, wait=0.01)
    assert done == [(cid, "17900000000000001")], logs
    clip = store.get_clip(con, cid)
    assert clip["status"] == "uploaded" and clip["instagram_id"] == "17900000000000001"
    assert clip["instagram_at"] and clip["youtube_id"] == ""

    paths = [(m, p.split("?")[0]) for m, p, _, _ in meta.calls]
    assert paths == [("POST", "/v26.0/17841400000000000/media"),
                     ("POST", "/ig-api-upload/v26.0/555"),          # our address, not the reply's
                     ("GET", "/v26.0/555"), ("GET", "/v26.0/555"),
                     ("POST", "/v26.0/17841400000000000/media_publish")]
    create, upload = meta.calls[0], meta.calls[1]
    assert b"media_type=REELS" in create[3] and b"upload_type=resumable" in create[3]
    assert b"Clipped+from" in create[3] and b"%23reels" in create[3].lower()
    assert create[2]["authorization"] == "Bearer tok-123"
    assert meta.uploaded == Path(clip["file"]).read_bytes()
    assert upload[2]["authorization"] == "OAuth tok-123" and upload[2]["offset"] == "0"
    assert upload[2]["file_size"] == str(len(meta.uploaded))
    assert all("tok-123" not in p for _, p, _, _ in meta.calls)     # token never in an address
    assert b"creation_id=555" in meta.calls[-1][3]


def test_instagram_and_youtube_queues_are_independent(meta, con, workspace):
    cid = add_clip(con, workspace, status="uploaded", youtube_id="yt1", uploaded_at=store.now())
    assert publish.queue(con, ig_cfg(), platform="youtube")[1] == 0     # already on YouTube
    done = publish.publish_instagram(con, ig_cfg(), log=lambda *_: None, wait=0.01)
    assert [c for c, _ in done] == [cid]
    assert publish.publish_instagram(con, ig_cfg(), log=lambda *_: None) == []   # not twice


def test_instagram_errors_are_reported_and_clip_stays_approved(meta, con, workspace):
    meta.fail_with = (400, "The video file is too long")
    cid = add_clip(con, workspace, status="approved")
    logs = []
    assert publish.publish_instagram(con, ig_cfg(), log=logs.append, wait=0.01) == []
    assert "too long" in logs[0] and store.get_clip(con, cid)["status"] == "approved"

    meta.fail_with = (401, "Invalid OAuth access token")
    add_clip(con, workspace, status="approved")
    logs = []
    publish.publish_instagram(con, ig_cfg(), log=logs.append, wait=0.01)
    assert logs[-1].startswith("stopping") and len(logs) == 2          # did not try the second clip


def test_instagram_not_set_up(con, workspace, monkeypatch):
    monkeypatch.delenv("INSTAGRAM_TOKEN", raising=False)
    add_clip(con, workspace, status="approved")
    cfg = config.load_config()
    assert not publish.instagram_configured(cfg)
    with pytest.raises(publish.PublishError) as e:
        publish.publish_instagram(con, cfg, log=lambda *_: None)
    assert "Instagram setup" in str(e.value)
    logs = []
    assert publish.publish_instagram(con, cfg, dry_run=True, log=logs.append) == []
    assert "would upload" in logs[0] and "Instagram" in logs[0]
    (workspace / "instagram_token.txt").write_text(" filetoken \n")
    assert publish.instagram_token() == "filetoken"


def test_publish_command_covers_configured_platforms(meta, workspace, capsys):
    con = store.connect()
    cid = add_clip(con, workspace, status="approved")
    (workspace / "config.yaml").write_text(
        (workspace / "config.yaml").read_text() + "\ninstagram:\n  user_id: \"17841400000000000\"\n")
    cli.main(["publish", "--dry-run"])
    out = capsys.readouterr().out
    assert f"would upload #{cid} to YouTube" in out and f"would upload #{cid} to Instagram" in out
    cli.main(["publish", "--to", "instagram", "--dry-run"])
    assert "YouTube" not in capsys.readouterr().out
    con.close()
