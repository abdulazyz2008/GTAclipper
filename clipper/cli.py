"""Command line:  clipper <command>   (or: python -m clipper <command>)"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, config, store

SKILL_SRC = Path(__file__).resolve().parent / "skill" / "SKILL.md"
SKILL_NAME = "stream-clipper"


def _clock(t):
    t = int(t)
    return f"{t // 3600}h{t % 3600 // 60:02d}m{t % 60:02d}s" if t >= 3600 else f"{t // 60}m{t % 60:02d}s"


def _ids(con, values):
    """Turn id arguments into clip rows, with a clear error for a bad one."""
    out = []
    for v in values:
        clip = store.get_clip(con, v)
        if not clip:
            sys.exit(f"error: no clip with id {v}")
        out.append(clip)
    return out


def _mcp_entry():
    """How an MCP client should start this program for the current workspace."""
    return {"command": sys.executable,
            "args": ["-m", "clipper", "--home", str(config.home()), "mcp"]}


# ---- commands --------------------------------------------------------------


def cmd_init(a):
    made = config.init_workspace(force=a.force)
    dst = config.home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    if SKILL_SRC.exists() and (a.force or not dst.exists()):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL_SRC, dst)
        made.append(str(dst))
    mcp_file = config.home() / ".mcp.json"
    if a.force or not mcp_file.exists():
        mcp_file.write_text(json.dumps({"mcpServers": {"clipper": _mcp_entry()}}, indent=2),
                            encoding="utf-8")
        made.append(str(mcp_file))
    for m in made:
        print("created", m)
    if not made:
        print("already set up (use --force to overwrite config.yaml and the skill)")
    print("next: edit config.yaml (add your streamer), then run `clipper doctor`")


def cmd_doctor(a):
    from . import media, transcribe
    ok = True

    def line(good, name, detail="", required=True):
        nonlocal ok
        mark = "ok     " if good else ("MISSING" if required else "off    ")
        print(f"  {mark} {name}" + (f" - {detail}" if detail else ""))
        if required and not good:
            ok = False

    print(f"clipper {__version__}, workspace {config.home()}")
    py_ok = sys.version_info >= (3, 10)
    line(py_ok, f"Python {sys.version.split()[0]}", "" if py_ok else "3.10 or newer needed")
    have = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    line(have, "ffmpeg + ffprobe", "" if have else "install from https://ffmpeg.org/download.html")
    if have:
        line(media.has_encoder("libx264"), "ffmpeg H.264 encoder (libx264)")
        line(media.has_filter("ass"), "ffmpeg text rendering (libass)",
             "" if media.has_filter("ass") else "clips will have no hook or captions",
             required=False)
    try:
        cfg = config.load_config()
        names = ", ".join(cfg["streamers"]) or "none - add one under 'streamers:'"
        exists = config.paths()["config"].exists()
        line(exists, "config.yaml", f"streamers: {names}" if exists else "run `clipper init`",
             required=False)
    except config.ConfigError as e:
        cfg = None
        line(False, "config.yaml", str(e))

    def mod(name):
        return importlib.util.find_spec(name) is not None

    line(mod("yt_dlp"), "yt-dlp (downloads)", "" if mod("yt_dlp") else "pip install yt-dlp")
    line(mod("chat_downloader"), "chat-downloader (VOD chat replay)",
         "" if mod("chat_downloader") else "pip install chat-downloader; without it, pass --chat",
         required=False)
    line(transcribe.available(), "faster-whisper (captions)",
         "" if transcribe.available() else "pip install faster-whisper; without it clips have no captions",
         required=False)
    line(mod("streamlink"), "streamlink (live recording)",
         "" if mod("streamlink") else "pip install streamlink; yt-dlp is used instead", required=False)
    from .rank import api_configured
    line(mod("anthropic") and api_configured(), "Claude API (automatic ranking in `clipper run`)",
         "" if mod("anthropic") and api_configured()
         else "not set up; inside Claude the skill judges clips itself", required=False)
    yt = mod("googleapiclient") and (config.home() / "client_secret.json").exists()
    line(yt, "YouTube upload", "" if yt else "see README 'YouTube setup'; `clipper export` works without it",
         required=False)

    if have and cfg is not None and not a.quick:
        print("  rendering a 6-second test clip...")
        try:
            with tempfile.TemporaryDirectory() as td:
                from . import edit
                src, out = Path(td) / "src.mp4", Path(td) / "out.mp4"
                media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                           "testsrc2=size=1280x720:rate=30:duration=6", "-f", "lavfi", "-i",
                           "sine=frequency=440:duration=6", "-c:v", "libx264", "-preset",
                           "ultrafast", "-c:a", "aac", str(src)])
                words = [{"w": w, "s": 0.5 + i * 0.4, "e": 0.85 + i * 0.4}
                         for i, w in enumerate("this is a caption test".split())]
                edit.render(src, 0, 6, out, cfg["edit"], "Test hook", words, None)
                info = media.probe(out)
                good = (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
                line(good, "test render", f"{info['width']}x{info['height']}, {info['duration']:.1f}s")
        except media.MediaError as e:
            line(False, "test render", str(e))
    print("all required parts are working" if ok else "fix the MISSING items above")
    sys.exit(0 if ok else 1)


def cmd_scan(a):
    from . import pipeline
    cfg = config.load_config()
    con = store.connect()
    ids = pipeline.scan(con, a.source, a.streamer, cfg, chat_file=a.chat,
                        use_whisper=not a.no_whisper)
    if not ids:
        print("no moments cleared detect.min_score - lower it in config.yaml to get more")
        return
    print(f"{len(ids)} candidates stored: ids {ids[0]}..{ids[-1]}")
    print("next: `clipper candidates --json`, judge them, then `clipper render`")


def cmd_run(a):
    from . import pipeline
    cfg = config.load_config()
    con = store.connect()
    ids = pipeline.scan(con, a.source, a.streamer, cfg, chat_file=a.chat,
                        use_whisper=not a.no_whisper)
    if not ids:
        sys.exit("no moments cleared detect.min_score - lower it in config.yaml to get more")
    print("rank")
    pipeline.judge(con, ids, a.streamer, cfg, use_llm=not a.no_llm)
    print("render")
    done = pipeline.render_top(con, ids, cfg, limit=a.top)
    print(f"done: {len(done)} clip(s) waiting for review. next: `clipper review`")


def _candidate_view(c):
    return {
        "id": c["id"], "streamer": c["streamer"], "vod_id": c["vod_id"], "status": c["status"],
        "at": _clock(c["start"]), "start": c["start"], "end": c["end"],
        "seconds": round(c["end"] - c["start"], 1),
        "score": c["score"], "chat_score": c["chat_score"], "accel_score": c["accel_score"],
        "hype_score": c["hype_score"], "audio_score": c["audio_score"],
        "streamer_said": c["transcript"], "chat_said": c["chat_sample"],
        "judged_by": c["judge"], "judge_score": c["llm_score"], "hook": c["hook"],
        "title": c["title"],
    }


def cmd_candidates(a):
    con = store.connect()
    where, params = "status=?", [a.status]
    if a.vod:
        where += " AND vod_id=?"
        params.append(a.vod)
    if a.unjudged:
        where += " AND judge IN ('', 'heuristic')"
    clips = store.fetch_clips(con, where, params, order="score DESC, id")
    if a.json:
        print(json.dumps([_candidate_view(c) for c in clips], ensure_ascii=False, indent=1))
        return
    if not clips:
        print(f"no clips with status '{a.status}'")
    for c in clips:
        print(f"#{c['id']:<4} {_clock(c['start']):>9} {c['end'] - c['start']:4.0f}s  "
              f"score {c['score']:5.2f} (chat {c['chat_score']:.1f} accel {c['accel_score']:.1f} "
              f"audio {c['audio_score']:.1f})  {c['transcript'][:60]}")


def cmd_frames(a):
    from . import pipeline
    cfg = config.load_config()
    con = store.connect()
    for clip in _ids(con, a.ids):
        path, times = pipeline.frames(con, clip["id"], cfg)
        print(f"#{clip['id']}: {path}")
        print("  frames left-to-right, top-to-bottom, at stream seconds: "
              + ", ".join(f"{t:g}" for t in times))


def cmd_judge(a):
    from . import pipeline
    cfg = config.load_config()
    try:
        text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
        items = json.loads(text)
    except OSError as e:
        sys.exit(f"error: cannot read {a.file}: {e}")
    except ValueError as e:
        sys.exit(f"error: {a.file} is not valid JSON: {e}")
    con = store.connect()
    problems = pipeline.apply_judgements(con, items, cfg)
    if problems:
        print("nothing was changed. Fix these and run again:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    n = len(items.get("clips", items)) if isinstance(items, dict) else len(items)
    print(f"{n} judgement(s) saved. next: `clipper render`")


def cmd_render(a):
    from . import ingest, media, pipeline
    cfg = config.load_config()
    con = store.connect()
    if a.ids:
        done = 0
        for clip in _ids(con, a.ids):
            try:
                row = pipeline.render_clip(con, clip["id"], cfg)
            except (pipeline.PipelineError, media.MediaError, ingest.IngestError) as e:
                print(f"  #{clip['id']} could not be rendered: {e}")
                continue
            done += 1
            print(f"  #{row['id']} -> {row['file']}")
        print(f"{done} clip(s) rendered")
        if done < len(a.ids):
            sys.exit(1)
        return
    ids = [c["id"] for c in store.fetch_clips(con, "status='candidate'")]
    if not ids:
        print("no candidates to render (run `clipper scan` first, or pass clip ids)")
        return
    done = pipeline.render_top(con, ids, cfg, limit=a.top)
    print(f"{len(done)} clip(s) rendered and waiting for review. next: `clipper review`")


def cmd_watch(a):
    from . import chat as chatmod
    from . import live
    cfg = config.load_config()
    con = store.connect()
    source = a.source
    if not source:
        source = config.streamer_cfg(cfg, a.streamer)["channel"]
        if not source:
            sys.exit(f"error: give a channel URL, or set streamers.{a.streamer}.channel in config.yaml")
    chat_source = None
    if a.simulate:
        if not Path(source).exists():
            sys.exit(f"error: --simulate needs a local video file, not {source}")
        side = Path(source).with_suffix(".chat.json")
        chat_file = a.chat or (side if side.exists() else None)
        chat_source = chatmod.ReplayChat(chatmod.load_chat_file(chat_file) if chat_file else [])
    elif a.chat:
        sys.exit("error: --chat only works together with --simulate")
    ids = live.wait_and_watch(
        con, source, a.streamer, cfg, wait_minutes=a.wait, chat_source=chat_source,
        simulate=a.simulate, speed=a.speed, use_whisper=not a.no_whisper,
        use_llm=not a.no_llm, max_minutes=a.minutes)
    if ids:
        print("clips:", ", ".join(f"#{i}" for i in ids), "- next: `clipper review`")


def cmd_status(a):
    from . import live
    con = store.connect()
    from . import autopilot
    info = {"clips": store.counts(con), "watcher": live.latest_status(),
            "autopilot": autopilot.read_status()}
    if a.json:
        print(json.dumps(info, indent=1))
        return
    print("clips: " + ", ".join(f"{k} {v}" for k, v in info["clips"].items()))
    if info["autopilot"]:
        print("autopilot (running):")
        for name, s in info["autopilot"]["streamers"].items():
            print(f"  {name:<14} {s['state']:<10} {len(s['clips'])} clip(s)"
                  + (f"  problem: {s['error']}" if s["error"] else ""))
    w = info["watcher"]
    if w:
        print(f"last watch: {w['streamer']} {w['state']}, {w['recorded_min']} min recorded, "
              f"{w['chat_messages']} chat messages, {len(w['clips'])} clip(s), updated {w['updated']}")
        if w.get("chat_error"):
            print("  chat problem:", w["chat_error"])


def cmd_review(a):
    from . import review
    review.serve(config.load_config(), host=a.host, port=a.port)


def cmd_list(a):
    con = store.connect()
    where, params = ("status=?", [a.status]) if a.status else ("1=1", [])
    clips = store.fetch_clips(con, where, params)
    if a.json:
        print(json.dumps([_candidate_view(c) | {"file": c["file"], "youtube_id": c["youtube_id"]}
                          for c in clips], ensure_ascii=False, indent=1))
        return
    if not clips:
        print("no clips yet")
    for c in clips:
        print(f"#{c['id']:<4} {c['status']:<9} {c['llm_score']:5.1f} {c['streamer']:<12} "
              f"{_clock(c['start']):>9}  {c['title']}")


def _set_status(a, status):
    con = store.connect()
    for clip in _ids(con, a.ids):
        if clip["status"] == "uploaded":
            print(f"#{clip['id']} is already uploaded; left as is")
            continue
        if status == "approved" and not (clip["file"] and Path(clip["file"]).exists()):
            print(f"#{clip['id']} has no rendered video yet; run `clipper render {clip['id']}` first")
            continue
        store.update_clip(con, clip["id"], status=status)
        print(f"#{clip['id']} {status}")


def cmd_approve(a):
    _set_status(a, "approved")


def cmd_reject(a):
    _set_status(a, "rejected")


def cmd_publish(a):
    from . import publish
    cfg, con, only = config.load_config(), store.connect(), set(a.ids) or None
    if a.to in ("all", "youtube"):
        try:
            publish.publish_youtube(con, cfg, dry_run=a.dry_run, only=only)
        except publish.PublishError as e:
            if a.to == "youtube":
                raise
            print(f"YouTube skipped: {e}")
    if a.to == "instagram" or (a.to == "all" and publish.instagram_configured(cfg)):
        publish.publish_instagram(con, cfg, dry_run=a.dry_run, only=only)


def cmd_autopilot(a):
    from .autopilot import Autopilot
    Autopilot(config.load_config(), streamers=a.streamers, use_whisper=not a.no_whisper,
              use_llm=not a.no_llm, max_minutes=a.minutes, once=a.once).run()


def cmd_mcp(_):
    from . import mcp_server
    mcp_server.serve()


def cmd_mcp_config(_):
    entry = _mcp_entry()
    print("Claude Desktop: Settings -> Developer -> Edit Config, and add this inside "
          "\"mcpServers\":\n")
    print(json.dumps({"clipper": entry}, indent=2))
    print("\nClaude Code, from any folder:\n")
    print("  claude mcp add clipper -- " + " ".join(
        f'"{x}"' if " " in x else x for x in [entry["command"], *entry["args"]]))
    print("\nRestart Claude afterwards. Then ask it, for example: \"start the clipper "
          "autopilot\".")


def cmd_jobs(a):
    from . import jobs
    if a.stop:
        j = jobs.stop(a.stop)
        print(f"asked {a.stop} to stop" if j else f"no job {a.stop}")
        return
    rows = jobs.list_jobs(15)
    if not rows:
        print("no background jobs yet")
    for j in rows:
        print(f"{j['id']}  {j['state']:<9} clipper {' '.join(j['args'])}")


def cmd_export(_):
    from . import publish
    publish.export(store.connect(), config.load_config())


# ---- parser ----------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="clipper",
        description="Turn livestreams and VODs into reviewed, vertical, captioned clips.")
    p.add_argument("--version", action="version", version=f"clipper {__version__}")
    p.add_argument("--home", help="workspace folder (default: current folder or $CLIPPER_HOME)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    def source_args(s, source_required=True):
        if source_required:
            s.add_argument("source", help="VOD URL or local video file")
        s.add_argument("--streamer", required=True, help="streamer key from config.yaml")
        s.add_argument("--chat", help="chat log file (this tool's, chat-downloader's or "
                                      "TwitchDownloader's JSON)")
        s.add_argument("--no-whisper", action="store_true", help="skip captions")

    s = add("init", cmd_init, "create config.yaml, the data folders and the Claude skill here")
    s.add_argument("--force", action="store_true", help="overwrite existing files")
    s = add("doctor", cmd_doctor, "check that everything needed is installed and working")
    s.add_argument("--quick", action="store_true", help="skip the test render")

    s = add("run", cmd_run, "VOD or file -> ranked, rendered clips waiting for review")
    source_args(s)
    s.add_argument("--no-llm", action="store_true", help="rank by detector score only")
    s.add_argument("--top", type=int, help="how many clips to render (default rank.render_top)")

    s = add("scan", cmd_scan, "find and transcribe the moments, without ranking or rendering")
    source_args(s)

    s = add("candidates", cmd_candidates, "show found moments (for judging)")
    s.add_argument("--status", default="candidate", choices=store.STATUSES)
    s.add_argument("--vod", help="only this VOD id")
    s.add_argument("--unjudged", action="store_true", help="only clips no model has judged yet")
    s.add_argument("--json", action="store_true")

    s = add("frames", cmd_frames, "save a sheet of frames from a clip's source video")
    s.add_argument("ids", nargs="+", type=int)

    s = add("judge", cmd_judge, "apply scores / hooks / titles from a JSON file ('-' = stdin)")
    s.add_argument("file")

    s = add("render", cmd_render, "render the best candidates, or the given clip ids")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--top", type=int, help="how many candidates to render")

    s = add("watch", cmd_watch, "record a live stream and cut clips as moments happen")
    s.add_argument("source", nargs="?", help="channel URL (default: the streamer's 'channel')")
    s.add_argument("--streamer", required=True, help="streamer key from config.yaml")
    s.add_argument("--minutes", type=float, help="stop after this many minutes")
    s.add_argument("--wait", type=float, default=0,
                   help="if the stream is offline, keep checking for this many minutes")
    s.add_argument("--no-llm", action="store_true", help="rank by detector score only")
    s.add_argument("--no-whisper", action="store_true", help="skip captions")
    s.add_argument("--simulate", action="store_true",
                   help="play a local video file as if it were live (for testing)")
    s.add_argument("--speed", type=float, default=1.0, help="playback speed for --simulate")
    s.add_argument("--chat", help="chat log to replay with --simulate")

    s = add("status", cmd_status, "clip counts and the state of the latest live watch")
    s.add_argument("--json", action="store_true")

    s = add("review", cmd_review, "open the approve / reject page")
    s.add_argument("--port", type=int)
    s.add_argument("--host", help="address to listen on (default 127.0.0.1)")

    s = add("list", cmd_list, "print clips")
    s.add_argument("--status", choices=store.STATUSES)
    s.add_argument("--json", action="store_true")

    s = add("approve", cmd_approve, "approve clips by id (same as the review page button)")
    s.add_argument("ids", nargs="+", type=int)
    s = add("reject", cmd_reject, "reject clips by id")
    s.add_argument("ids", nargs="+", type=int)

    s = add("autopilot", cmd_autopilot,
            "watch every streamer with a channel; clip whoever goes live")
    s.add_argument("--streamers", nargs="+", help="only these streamer keys")
    s.add_argument("--minutes", type=float, help="stop after this many minutes")
    s.add_argument("--once", action="store_true", help="check each streamer once, then stop")
    s.add_argument("--no-llm", action="store_true", help="rank by detector score only")
    s.add_argument("--no-whisper", action="store_true", help="skip captions")

    add("mcp", cmd_mcp, "run as an MCP server so Claude can use clipper as tools")
    add("mcp-config", cmd_mcp_config, "show how to connect Claude Desktop / Claude Code")
    s = add("jobs", cmd_jobs, "list background jobs started through Claude")
    s.add_argument("--stop", metavar="JOB_ID", help="ask a job to stop")

    s = add("publish", cmd_publish, "upload approved clips to YouTube and Instagram")
    s.add_argument("--to", choices=["all", "youtube", "instagram"], default="all",
                   help="where to post (default: every platform that is set up)")
    s.add_argument("ids", nargs="*", type=int, help="only these clip ids")
    s.add_argument("--dry-run", action="store_true", help="show what would be uploaded")
    add("export", cmd_export, "copy approved clips + captions for TikTok / Instagram")
    return p


def main(argv=None):
    from . import ingest, live, media, pipeline, publish
    a = build_parser().parse_args(argv)
    if a.home:
        os.environ["CLIPPER_HOME"] = str(Path(a.home).expanduser().resolve())
    for stream in (sys.stdout, sys.stderr):      # chat and titles are full of emoji
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        a.fn(a)
    except (config.ConfigError, pipeline.PipelineError, ingest.IngestError, media.MediaError,
            live.LiveError, publish.PublishError) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
