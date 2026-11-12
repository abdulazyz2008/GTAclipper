"""Command line entry point:  python -m clipper <run|review|publish|export|list>"""
import argparse
import json
import subprocess
import sys

from .common import CLIPS, ROOT, db, duration, fetch_clips, insert_clip, load_config


def cmd_run(a):
    from . import detect, edit, ingest, rank, transcribe
    cfg = load_config()
    if a.streamer not in cfg["streamers"]:
        sys.exit(f"streamer '{a.streamer}' is not in config.yaml")
    scfg = cfg["streamers"][a.streamer]
    con = db()

    print("1/5 ingest")
    vod_id, vod = ingest.get_vod(a.source, cfg["ingest"]["max_height"])
    chat = ingest.get_chat(a.source, vod_id, a.chat)
    dur = duration(vod)
    print(f"  {vod.name}: {dur / 60:.1f} min, {len(chat)} chat messages")

    print("2/5 detect")
    cands = detect.find_moments(vod, chat, dur, cfg["detect"])
    print(f"  {len(cands)} candidate moments")
    if not cands:
        sys.exit("no moments above min_score - lower detect.min_score in config.yaml")

    print("3/5 transcribe")
    for c in cands:
        if a.no_whisper:
            c["transcript"], c["words"] = "", []
        else:
            c["transcript"], c["words"] = transcribe.transcribe_window(
                vod, c["start"], c["end"], cfg["whisper"])

    print("4/5 rank")
    cands = rank.rank(cands, a.streamer, cfg["game"], cfg["rank"], use_llm=not a.no_llm)
    cands.sort(key=lambda c: -c["llm_score"])

    print("5/5 render")
    keep = cfg["rank"]["render_top"]
    for n, c in enumerate(cands):
        row = {
            "vod_id": vod_id, "vod_path": str(vod), "streamer": a.streamer,
            "start": c["start"], "end": c["end"], "score": c["score"],
            "chat_score": c["chat_score"], "audio_score": c["audio_score"],
            "transcript": c["transcript"], "words": c["words"],
            "chat_sample": c["chat_sample"], "llm_score": c["llm_score"],
            "hook": c["hook"], "title": c["title"], "reason": c["reason"],
        }
        cid = insert_clip(con, row)
        if n < keep:
            out = CLIPS / f"{vod_id}_{cid:04d}.mp4"
            edit.render(vod, c["start"], c["end"], out, c["hook"], c["words"],
                        scfg.get("facecam"))
            con.execute("UPDATE clips SET file=?, status='rendered' WHERE id=?", (str(out), cid))
            con.commit()
            print(f"  #{cid} {c['llm_score']:5.1f}  {c['start']:7.1f}s  {c['title']}")
    print("done. next:  python -m clipper review")


def cmd_review(_):
    subprocess.run([sys.executable, "-m", "streamlit", "run",
                    str(ROOT / "clipper" / "review_app.py")])


def cmd_publish(a):
    from . import publish
    publish.publish_youtube(load_config(), dry_run=a.dry_run)


def cmd_export(_):
    from . import publish
    publish.export(load_config())


def cmd_list(_):
    for c in fetch_clips(db()):
        print(f"#{c['id']:<4} {c['status']:<9} {c['llm_score']:5.1f} "
              f"{c['streamer']:<12} {c['start']:7.1f}s  {c['title']}")


def main():
    p = argparse.ArgumentParser(prog="clipper")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="VOD -> ranked, rendered clips")
    r.add_argument("source", help="VOD URL or local video file")
    r.add_argument("--streamer", required=True, help="key from config.yaml")
    r.add_argument("--chat", help="chat JSON file: [{\"t\": seconds, \"text\": \"...\"}]")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-whisper", action="store_true")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("review", help="open the approve/reject screen").set_defaults(fn=cmd_review)
    pb = sub.add_parser("publish", help="upload approved clips to YouTube")
    pb.add_argument("--dry-run", action="store_true")
    pb.set_defaults(fn=cmd_publish)
    sub.add_parser("export", help="copy approved clips + captions for TikTok/IG").set_defaults(fn=cmd_export)
    sub.add_parser("list", help="print all clips").set_defaults(fn=cmd_list)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
