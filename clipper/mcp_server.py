"""Connect Claude to the clipper (Model Context Protocol server).

`clipper mcp` speaks MCP over stdin/stdout, so Claude Desktop, Claude Code or
any other MCP client can run the whole workflow as tools: add a streamer,
start the autopilot, read the moments, look at frames, hand back verdicts,
render, open the review page, publish.

The protocol is small (JSON lines: initialize, tools/list, tools/call), so it
is implemented here directly instead of depending on an SDK whose interface
changes between versions.

Rules built into the tools, not left to the model's goodwill:
  * approving a clip and publishing are separate tools whose descriptions say
    they need the user's explicit word;
  * a real publish refuses to run unless `confirmed_by_user` is true;
  * a streamer cannot be added without `permission_confirmed`.
"""
import base64
import json
import re
import sys
import time

import yaml

from . import __version__, config, jobs, store

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """Shown to the model as a failed tool call."""


# ---- helpers ---------------------------------------------------------------


def _text(obj):
    if not isinstance(obj, str):
        obj = json.dumps(obj, ensure_ascii=False, indent=1)
    return [{"type": "text", "text": obj}]


def _clip_view(c):
    from .cli import _candidate_view
    v = _candidate_view(c)
    v["has_video"] = bool(c["file"])
    v["youtube_id"], v["instagram_id"] = c["youtube_id"], c["instagram_id"]
    return v


def _job_view(j):
    return {"job_id": j["id"], "what": " ".join(["clipper", *j["args"]]), "state": j["state"],
            "started": j["started"], "log": j["log"]}


def _ids(a, key="clip_ids", required=True):
    ids = a.get(key)
    if ids is None and not required:
        return None
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool)
                                                       for i in ids):
        raise ToolError(f"{key} must be a list of clip numbers, e.g. [3, 4]")
    return ids


def _streamer(a, cfg):
    name = a.get("streamer")
    if not isinstance(name, str) or name not in cfg["streamers"]:
        known = ", ".join(cfg["streamers"]) or "none yet - use add_streamer"
        raise ToolError(f"unknown streamer {name!r}. Known: {known}")
    return name


# ---- tools -----------------------------------------------------------------


def t_status(a):
    from . import autopilot, live
    con = store.connect()
    try:
        cfg = config.load_config()
        return _text({
            "workspace": str(config.home()),
            "streamers": {n: {"channel": s["channel"], "credit": s["credit"],
                              "has_facecam": bool(s["facecam"])}
                          for n, s in cfg["streamers"].items()},
            "clips": store.counts(con),
            "autopilot": autopilot.read_status(),
            "last_live_watch": live.latest_status(),
            "jobs": [_job_view(j) for j in jobs.list_jobs(5)],
        })
    finally:
        con.close()


def t_add_streamer(a):
    key = a.get("key", "")
    if not isinstance(key, str) or config.safe_name(key, "") != key or not key:
        raise ToolError("key may only use letters, digits, '-' and '_' (e.g. \"kai\")")
    if a.get("permission_confirmed") is not True:
        raise ToolError("Not added. Ask the user to confirm they have this streamer's "
                        "permission (or a campaign that allows reuse), then call again with "
                        "permission_confirmed: true.")
    path = config.paths()["config"]
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entry = {"credit": str(a.get("credit") or key)}
    for k in ("channel", "hashtags"):
        if a.get(k):
            entry[k] = str(a[k])
    if a.get("facecam"):
        entry["facecam"] = a["facecam"]
    if a.get("frame"):
        entry["frame"] = a["frame"]
    new = dict(raw)
    new["streamers"] = dict(raw.get("streamers") or {})
    replaced = key in new["streamers"]
    new["streamers"][key] = entry
    try:
        config.validate(new)                     # never write a config that would not load
    except config.ConfigError as e:
        raise ToolError(str(e)) from None
    if path.exists():
        path.with_suffix(".yaml.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(yaml.safe_dump(new, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return _text(f"{'updated' if replaced else 'added'} streamer '{key}' in {path} "
                 "(the previous file is saved as config.yaml.bak)")


def t_scan_vod(a):
    cfg = config.load_config()
    name = _streamer(a, cfg)
    src = a.get("source")
    if not isinstance(src, str) or not src.strip():
        raise ToolError("source must be a VOD link or the path of a video file")
    args = ["scan", src.strip(), "--streamer", name]
    if a.get("chat_file"):
        args += ["--chat", str(a["chat_file"])]
    return _text(_job_view(jobs.start(args, "scan")))


def t_watch_live(a):
    cfg = config.load_config()
    name = _streamer(a, cfg)
    if not cfg["streamers"][name]["channel"]:
        raise ToolError(f"streamer '{name}' has no channel link; add one with add_streamer")
    args = ["watch", "--streamer", name, "--wait", str(float(a.get("wait_minutes", 30)))]
    if a.get("minutes"):
        args += ["--minutes", str(float(a["minutes"]))]
    return _text(_job_view(jobs.start(args, "watch")))


def t_start_autopilot(a):
    if jobs.running("autopilot"):
        return _text({"already_running": _job_view(jobs.running("autopilot"))})
    cfg = config.load_config()
    args = ["autopilot"]
    names = a.get("streamers")
    if names:
        if not isinstance(names, list) or any(n not in cfg["streamers"] for n in names):
            raise ToolError("streamers must be a list of keys from config.yaml")
        args += ["--streamers", *names]
    if a.get("minutes"):
        args += ["--minutes", str(float(a["minutes"]))]
    job = jobs.start(args, "autopilot")
    time.sleep(1.5)
    return _text(_job_view(jobs.status(job["id"])))


def t_job_status(a):
    if a.get("job_id"):
        j = jobs.status(str(a["job_id"]), tail=30)
        if not j:
            raise ToolError("no such job")
        return _text(_job_view(j))
    return _text([_job_view(j) for j in jobs.list_jobs(10)])


def t_stop_job(a):
    j = jobs.stop(str(a.get("job_id", "")))
    if not j:
        raise ToolError("no such job")
    return _text(_job_view(j))


def t_clips_to_judge(a):
    con = store.connect()
    try:
        rows = store.fetch_clips(
            con, "status IN ('candidate','rendered') AND judge IN ('', 'heuristic')",
            order="score DESC, id")
        limit = a.get("limit") or 30
        return _text({"waiting": len(rows), "clips": [_clip_view(c) for c in rows[:limit]]})
    finally:
        con.close()


def t_clip_frames(a):
    from . import pipeline
    cid = a.get("clip_id")
    if not isinstance(cid, int):
        raise ToolError("clip_id must be a clip number")
    con = store.connect()
    try:
        path, times = pipeline.frames(con, cid, config.load_config(), log=lambda *_: None)
    finally:
        con.close()
    return [{"type": "image", "mimeType": "image/jpeg",
             "data": base64.b64encode(path.read_bytes()).decode()},
            {"type": "text", "text": "Ten frames, left to right then top to bottom, at stream "
                                     "seconds: " + ", ".join(f"{t:g}" for t in times)}]


def t_judge_clips(a):
    from . import pipeline
    items = a.get("judgements")
    con = store.connect()
    try:
        problems = pipeline.apply_judgements(con, items, config.load_config(),
                                             log=lambda *_: None)
    finally:
        con.close()
    if problems:
        raise ToolError("Nothing was changed. Fix these and send the whole list again:\n- "
                        + "\n- ".join(problems))
    return _text(f"{len(items)} judgement(s) saved. Call render_clips for the ones worth "
                 "rendering (also for clips that were already rendered, to apply the new hook).")


def t_render_clips(a):
    ids = _ids(a, required=False)
    args = ["render", *map(str, ids)] if ids else ["render"]
    if not ids and a.get("top"):
        args += ["--top", str(int(a["top"]))]
    return _text(_job_view(jobs.start(args, "render")))


def t_list_clips(a):
    status = a.get("status")
    if status is not None and status not in store.STATUSES:
        raise ToolError("status must be one of: " + ", ".join(store.STATUSES))
    con = store.connect()
    try:
        rows = store.fetch_clips(con, "status=?", (status,)) if status else store.fetch_clips(con)
        return _text([{"id": c["id"], "status": c["status"], "streamer": c["streamer"],
                       "score": c["llm_score"], "title": c["title"], "hook": c["hook"],
                       "seconds": round(c["end"] - c["start"], 1), "file": c["file"],
                       "youtube_id": c["youtube_id"], "instagram_id": c["instagram_id"]}
                      for c in rows[: a.get("limit") or 50]])
    finally:
        con.close()


def t_review_page(a):
    job = jobs.running("review") or jobs.start(["review"], "review")
    for _ in range(40):
        m = re.search(r"review page: (\S+)", jobs.log_tail(job["id"], 20))
        if m:
            return _text(f"Review page: {m.group(1)}\nGive this address to the user. It only "
                         "works on this computer. Stop it later with stop_job "
                         f"(job_id {job['id']}).")
        if jobs.status(job["id"])["state"] not in ("running",):
            break
        time.sleep(0.25)
    raise ToolError("the review page did not start: " + jobs.log_tail(job["id"], 5))


def t_set_clip_status(a):
    from pathlib import Path
    ids = _ids(a)
    status = a.get("status")
    if status not in ("approved", "rejected", "rendered"):
        raise ToolError("status must be approved, rejected or rendered (= back to review)")
    if a.get("decided_by_user") is not True:
        raise ToolError("Not changed. Approving and rejecting are the user's decisions: only "
                        "call this for clips the user named, with decided_by_user: true.")
    con = store.connect()
    out = []
    try:
        for i in ids:
            c = store.get_clip(con, i)
            if not c:
                out.append(f"#{i}: no such clip")
            elif c["status"] == "uploaded":
                out.append(f"#{i}: already uploaded, left as is")
            elif status == "approved" and not (c["file"] and Path(c["file"]).exists()):
                out.append(f"#{i}: has no rendered video yet - render it first")
            else:
                store.update_clip(con, i, status=status)
                out.append(f"#{i}: {status}")
    finally:
        con.close()
    return _text("\n".join(out))


def t_publish(a):
    from . import publish
    to = a.get("to", "all")
    if to not in ("all", "youtube", "instagram"):
        raise ToolError("to must be all, youtube or instagram")
    ids = _ids(a, required=False)
    if a.get("dry_run", True):
        cfg = config.load_config()
        con = store.connect()
        lines = []
        try:
            if to in ("all", "youtube"):
                publish.publish_youtube(con, cfg, dry_run=True, only=set(ids) if ids else None,
                                        log=lines.append)
            if to == "instagram" or (to == "all" and publish.instagram_configured(cfg)):
                publish.publish_instagram(con, cfg, dry_run=True,
                                          only=set(ids) if ids else None, log=lines.append)
        finally:
            con.close()
        return _text("\n".join(lines) + "\n(dry run: nothing was uploaded)")
    if a.get("confirmed_by_user") is not True:
        raise ToolError("Not published. Show the user the dry run first; publish only after "
                        "they say to, with confirmed_by_user: true.")
    args = ["publish", "--to", to, *map(str, ids or [])]
    return _text(_job_view(jobs.start(args, "publish")))


def t_export_clips(a):
    from . import publish
    con = store.connect()
    lines = []
    try:
        publish.export(con, config.load_config(), log=lines.append)
    finally:
        con.close()
    return _text("\n".join(lines))


def t_doctor(a):
    job = jobs.wait(jobs.start(["doctor", "--quick"], "doctor")["id"], timeout=60)
    return _text(jobs.log_tail(job["id"], 40))


def _schema(props=None, required=()):
    return {"type": "object", "properties": props or {}, "required": list(required),
            "additionalProperties": False}


_IDS = {"type": "array", "items": {"type": "integer"}, "description": "clip numbers"}
_STREAMER = {"type": "string", "description": "streamer key from config.yaml"}
_BOX = {"type": "object", "properties": {k: {"type": "number"} for k in "xywh"},
        "required": ["x", "y", "w", "h"]}

TOOLS = [
    ("status", t_status, "Where things stand: the streamers that are set up, how many clips "
     "are in each state, what the autopilot and recent background jobs are doing. Start here.",
     _schema()),
    ("doctor", t_doctor, "Check that ffmpeg and the optional parts (captions, live recording, "
     "YouTube, Instagram) are installed, with the fix for anything missing.", _schema()),
    ("add_streamer", t_add_streamer, "Add or update a streamer in config.yaml. Only streamers "
     "listed there can be clipped. Requires the user's confirmation that they have permission "
     "to reuse this streamer's footage.",
     _schema({"key": {"type": "string", "description": "short name: letters, digits, - or _"},
              "credit": {"type": "string", "description": "shown on clips, e.g. '@name on Twitch'"},
              "channel": {"type": "string", "description": "live channel link"},
              "hashtags": {"type": "string"},
              "facecam": dict(_BOX, description="webcam rectangle in the stream frame, in pixels; "
                                                "leave out if unknown (never guess)"),
              "frame": {"type": "object", "description": "stream resolution the facecam was "
                        "measured on, e.g. {\"w\": 1920, \"h\": 1080}"},
              "permission_confirmed": {"type": "boolean"}},
             ["key", "permission_confirmed"])),
    ("start_autopilot", t_start_autopilot, "Start watching every streamer that has a channel "
     "link (or only the ones listed). Runs in the background: records whoever goes live, cuts "
     "clips as moments happen, and queues them for review. Check on it with status.",
     _schema({"streamers": {"type": "array", "items": {"type": "string"}},
              "minutes": {"type": "number", "description": "stop after this long"}})),
    ("watch_live", t_watch_live, "Record one streamer's live stream in the background and cut "
     "clips as moments happen.",
     _schema({"streamer": _STREAMER, "minutes": {"type": "number"},
              "wait_minutes": {"type": "number", "description": "keep checking this long if "
                               "they are offline (default 30)"}}, ["streamer"])),
    ("scan_vod", t_scan_vod, "Find the moments in a finished stream (VOD link or video file) "
     "in the background. When the job has finished, read them with clips_to_judge.",
     _schema({"source": {"type": "string"}, "streamer": _STREAMER,
              "chat_file": {"type": "string", "description": "optional chat log file"}},
             ["source", "streamer"])),
    ("job_status", t_job_status, "State and recent output of a background job, or of the "
     "latest jobs when no job_id is given.", _schema({"job_id": {"type": "string"}})),
    ("stop_job", t_stop_job, "Ask a background job (autopilot, watch, review page...) to "
     "finish up and stop.", _schema({"job_id": {"type": "string"}}, ["job_id"])),
    ("clips_to_judge", t_clips_to_judge, "The moments no model has judged yet, with what the "
     "streamer said, what chat said and how strongly chat and audio spiked. Judge them, then "
     "call judge_clips.", _schema({"limit": {"type": "integer"}})),
    ("clip_frames", t_clip_frames, "Ten frames from a clip's footage as one image, to see what "
     "actually happens before scoring it or writing its hook.",
     _schema({"clip_id": {"type": "integer"}}, ["clip_id"])),
    ("judge_clips", t_judge_clips, "Save your verdicts. Each: id, score 0-100, hook (at most 8 "
     "words / 60 characters, must describe what really happens - never invent events), title "
     "(at most 95 characters, sentence case, no < >, no story spoilers), reason (one sentence), "
     "and optionally start / end in stream seconds to move the cut. Score raids, giveaways, "
     "menus and anything unclear low. All-or-nothing: if one is invalid nothing is saved.",
     _schema({"judgements": {"type": "array", "items": {
         "type": "object",
         "properties": {"id": {"type": "integer"}, "score": {"type": "number"},
                        "hook": {"type": "string"}, "title": {"type": "string"},
                        "reason": {"type": "string"}, "start": {"type": "number"},
                        "end": {"type": "number"}},
         "required": ["id", "score", "hook", "title"]}}}, ["judgements"])),
    ("render_clips", t_render_clips, "Render clips in the background: the given clip numbers "
     "(also re-renders, to apply a new hook or cut), or with no numbers the best unrendered "
     "candidates.", _schema({"clip_ids": _IDS, "top": {"type": "integer"}})),
    ("list_clips", t_list_clips, "List clips, optionally by status (candidate, rendered = "
     "waiting for review, approved, rejected, uploaded), with their video file paths.",
     _schema({"status": {"type": "string"}, "limit": {"type": "integer"}})),
    ("review_page", t_review_page, "Start the review page and return its address, where the "
     "user watches each clip, edits hook / title / cut, and approves or rejects.", _schema()),
    ("set_clip_status", t_set_clip_status, "Record the user's decision on clips: approved, "
     "rejected, or rendered (back to review). Only for clips the user explicitly decided on in "
     "this conversation - never approve on your own judgement.",
     _schema({"clip_ids": _IDS, "status": {"type": "string"},
              "decided_by_user": {"type": "boolean"}}, ["clip_ids", "status", "decided_by_user"])),
    ("publish", t_publish, "Upload approved clips to YouTube Shorts and / or Instagram Reels. "
     "dry_run (the default) only lists what would go out. A real upload needs dry_run: false "
     "and confirmed_by_user: true, and only after the user has seen the dry run and asked to "
     "publish.",
     _schema({"to": {"type": "string", "description": "all (default), youtube or instagram"},
              "clip_ids": _IDS, "dry_run": {"type": "boolean"},
              "confirmed_by_user": {"type": "boolean"}})),
    ("export_clips", t_export_clips, "Copy approved clips and ready-to-paste captions into "
     "data/export/ for posting by hand (TikTok, or anything else).", _schema()),
]
_BY_NAME = {name: fn for name, fn, _, _ in TOOLS}

INSTRUCTIONS = (
    "Clipper turns livestreams and VODs into short vertical clips. You are the editor: read "
    "each detected moment, look at its frames, score it honestly and write a hook that is "
    "true. The user approves clips and decides when to publish - never do either on your own. "
    "Typical flow: status -> (add_streamer) -> start_autopilot or scan_vod -> clips_to_judge "
    "-> clip_frames -> judge_clips -> render_clips -> review_page -> publish (dry run first). "
    "Report what the tools actually returned, including failures.")


# ---- protocol --------------------------------------------------------------


def call_tool(name, arguments):
    """Run one tool. Returns an MCP tool result (never raises)."""
    fn = _BY_NAME.get(name)
    if fn is None:
        return {"content": _text(f"unknown tool: {name}"), "isError": True}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return {"content": fn(arguments), "isError": False}
    except ToolError as e:
        return {"content": _text(str(e)), "isError": True}
    except Exception as e:      # config, download, ffmpeg errors: say what went wrong
        return {"content": _text(f"{type(e).__name__}: {e}"), "isError": True}


def handle(msg):
    """Answer one JSON-RPC message. Returns the reply dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "bad request"}}
    mid, method = msg.get("id"), msg.get("method")
    if method is None or mid is None:
        return None                              # a notification or a reply: nothing to send

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    params = msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "clipper", "version": __version__},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d, "inputSchema": s}
                             for n, _, d, s in TOOLS]})
    if method == "tools/call":
        return ok(call_tool(params.get("name"), params.get("arguments")))
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601,
                                                   "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None):
    """Read JSON lines from stdin, answer on stdout, until stdin closes."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    sys.stdout = sys.stderr          # nothing but protocol messages may reach real stdout
    for line in iter(stdin.readline, b""):
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
