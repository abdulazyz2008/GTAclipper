"""The steps of a run, as functions the CLI, the live watcher and the review
page all share:

  scan      source -> candidate rows (ingest, detect, tidy cut points, transcribe)
  judge     candidates -> score / hook / title (API or detector score)
  render    candidate -> finished vertical clip waiting for review
"""
from pathlib import Path

from . import detect, edit, ingest, media, rank, store, transcribe
from .config import ensure_dirs, streamer_cfg


class PipelineError(Exception):
    pass


def add_candidate(con, cand, meta, cfg, use_whisper=True, log=print):
    """Tidy one detected moment and store it. `meta` carries vod_id, source,
    streamer, audio_path, audio_offset, vod_path, media_start, lo, hi."""
    start, end = detect.snap_boundaries(
        meta["audio_path"], cand["start"], cand["end"], meta["lo"], meta["hi"],
        offset=meta.get("audio_offset", 0.0))
    text, words = "", []
    if use_whisper:
        text, rel = transcribe.transcribe_window(
            meta["audio_path"], start, end, cfg["whisper"],
            offset=meta.get("audio_offset", 0.0), log=log)
        words = [{"w": w["w"], "s": round(w["s"] + start, 2), "e": round(w["e"] + start, 2)}
                 for w in rel]
    return store.insert_clip(con, {
        "vod_id": meta["vod_id"], "source": meta.get("source", ""),
        "vod_path": str(meta.get("vod_path") or ""), "media_start": meta.get("media_start", 0.0),
        "audio_path": str(meta["audio_path"]), "streamer": meta["streamer"],
        "peak": cand["peak"], "start": start, "end": end, "score": cand["score"],
        "chat_score": cand["chat_score"], "accel_score": cand["accel_score"],
        "hype_score": cand["hype_score"], "audio_score": cand["audio_score"],
        "transcript": text, "words": words, "chat_sample": cand["chat_sample"],
        "status": "candidate",
    })


def scan(con, source, streamer, cfg, chat_file=None, use_whisper=True, log=print):
    """Find the moments in a VOD or video file. Returns the new clip ids, best first."""
    streamer_cfg(cfg, streamer)
    media.require_tools()
    log("1/3 ingest")
    info = ingest.describe(source)
    if info["is_live"]:
        raise PipelineError("that stream is live right now - use `clipper watch` for live "
                            "streams, or wait for the VOD")
    vod_id = info["id"]
    chat = ingest.get_chat(source, vod_id, chat_file, log=log)

    video = audio = None
    if info["path"]:
        video = audio = info["path"]
    else:
        if cfg["ingest"]["mode"] == "smart":
            try:
                audio = ingest.get_audio(source, vod_id)
            except (ingest.IngestError, media.MediaError) as e:
                log(f"  audio-only download not possible ({e}); downloading the full VOD")
        if audio is None:
            video = audio = ingest.get_full(source, vod_id, cfg["ingest"]["max_height"])
    ainfo = media.probe(audio)
    if video is None and ainfo["has_video"]:
        video = audio          # the site only offers one file: it is already the whole VOD
    if not ainfo["has_audio"]:
        raise PipelineError(f"{Path(audio).name} has no audio track; nothing to analyse")
    dur = ainfo["duration"] or info["duration"] or 0
    log(f"  {info['title'] or vod_id}: {dur / 60:.1f} min, {len(chat)} chat messages"
        + ("" if video else " (audio only so far; video is fetched per clip)"))

    log("2/3 detect")
    d = cfg["detect"]
    rms = media.envelope(audio, d["bucket_sec"])
    if len(rms) == 0:
        raise PipelineError("could not read any audio from the source")
    dur = dur or len(rms) * d["bucket_sec"]
    # moments this VOD already produced clips for are left alone
    kept = store.fetch_clips(con, "vod_id=? AND streamer=? AND status!='candidate'",
                             (vod_id, streamer))
    con.execute("DELETE FROM clips WHERE vod_id=? AND streamer=? AND status='candidate'",
                (vod_id, streamer))
    con.commit()
    cands = detect.find_moments(chat, rms, dur, d, taken=[c["peak"] for c in kept])
    log(f"  {len(cands)} candidate moments" + ("" if chat else " (audio only - no chat)"))
    if not cands:
        return []

    log("3/3 transcribe" if use_whisper else "3/3 store (captions off)")
    meta = {"vod_id": vod_id, "source": info["url"] or str(info["path"]), "streamer": streamer,
            "audio_path": audio, "audio_offset": 0.0, "vod_path": video or "",
            "media_start": 0.0, "lo": 0.0, "hi": dur}
    return [add_candidate(con, c, meta, cfg, use_whisper, log) for c in cands]


def judge(con, ids, streamer, cfg, use_llm=True, log=print):
    """Score candidates that have no judgement yet (API if configured, else detector score)."""
    clips = [c for c in (store.get_clip(con, i) for i in ids) if c and not c["judge"]]
    if not clips:
        return
    for c in clips:
        c["judge"] = ""
    rank.rank(clips, streamer, cfg["game"], cfg["rank"], use_llm=use_llm, log=log)
    for c in clips:
        store.update_clip(con, c["id"], llm_score=c["llm_score"], hook=c["hook"],
                          title=c["title"], reason=c["reason"], judge=c["judge"])


def refresh_captions(con, clip, cfg, log=print):
    """Re-run speech-to-text for a clip whose cut points moved (only if it had captions)."""
    audio = clip["audio_path"]
    if not (clip["words"] and audio and Path(audio).exists() and transcribe.available()):
        return
    offset = clip["media_start"] if audio == clip["vod_path"] else 0.0
    text, rel = transcribe.transcribe_window(audio, clip["start"], clip["end"], cfg["whisper"],
                                             offset=offset, log=log)
    if rel:
        store.update_clip(con, clip["id"], transcript=text, words=[
            {"w": w["w"], "s": round(w["s"] + clip["start"], 2),
             "e": round(w["e"] + clip["start"], 2)} for w in rel])


def apply_judgements(con, items, cfg=None, log=print):
    """Apply judgements written by Claude (the skill path). All-or-nothing:
    returns a list of problems, empty when everything was applied."""
    if isinstance(items, dict):
        items = items.get("clips", items)
    if not isinstance(items, list) or not items:
        return ["the file must hold a JSON list of judgements, e.g. "
                '[{"id": 3, "score": 80, "hook": "...", "title": "...", "reason": "..."}]']
    problems, updates, seen = [], [], set()
    for n, d in enumerate(items):
        if not isinstance(d, dict) or not isinstance(d.get("id"), int):
            problems.append(f"entry {n}: needs a whole-number \"id\"")
            continue
        clip = store.get_clip(con, d["id"])
        if not clip:
            problems.append(f"clip {d['id']}: no such clip")
            continue
        if d["id"] in seen:
            problems.append(f"clip {d['id']}: listed twice")
            continue
        seen.add(d["id"])
        if clip["status"] == "uploaded":
            problems.append(f"clip {d['id']}: already uploaded, cannot be changed")
            continue
        fields, errs = rank.check_judgement(d)
        problems += [f"clip {d['id']}: {e}" for e in errs]
        for key in ("start", "end"):
            if key in d and d[key] is not None:
                if not isinstance(d[key], (int, float)) or isinstance(d[key], bool) or d[key] < 0:
                    problems.append(f"clip {d['id']}: {key} must be a time in seconds")
                else:
                    fields[key] = round(float(d[key]), 2)
        s, e = fields.get("start", clip["start"]), fields.get("end", clip["end"])
        if not 5 <= e - s <= 180:
            problems.append(f"clip {d['id']}: the clip must be 5 to 180 seconds long "
                            f"(start {s}, end {e})")
        fields["judge"] = "claude"
        updates.append((d["id"], fields))
    if problems:
        return problems
    for clip_id, fields in updates:
        before = store.get_clip(con, clip_id)
        store.update_clip(con, clip_id, **fields)
        after = store.get_clip(con, clip_id)
        if cfg and (before["start"], before["end"]) != (after["start"], after["end"]):
            refresh_captions(con, after, cfg, log)
    return []


# ---- video for a clip ------------------------------------------------------


def _covers(clip):
    p = clip["vod_path"]
    if not p or not Path(p).exists():
        return False
    try:
        dur = media.probe(p)["duration"]
    except media.MediaError:
        return False
    return clip["media_start"] <= clip["start"] + 0.05 and \
        clip["media_start"] + dur >= clip["end"] - 0.5


def ensure_video(con, clip, cfg, log=print):
    """Make sure the video for a clip is on disk; fetch just its section if needed."""
    if _covers(clip):
        return clip
    src = clip["source"]
    if not src or not ingest.is_url(src):
        raise PipelineError(
            f"clip {clip['id']}: its source video is missing or does not cover "
            f"{clip['start']:.0f}-{clip['end']:.0f}s ({clip['vod_path'] or 'no file'})")
    pad = cfg["ingest"]["section_pad"]
    want_start = max(0.0, clip["start"] - pad)
    log(f"  fetching video for clip {clip['id']} ({clip['start']:.0f}-{clip['end']:.0f}s)")
    try:
        path = ingest.get_section(src, clip["vod_id"], want_start, clip["end"] + pad,
                                  cfg["ingest"]["max_height"])
        begin = want_start
        if clip["audio_path"] and Path(clip["audio_path"]).exists():
            begin = ingest.align_section(path, clip["audio_path"], want_start)
    except (ingest.IngestError, media.MediaError) as e:
        log(f"  partial download not possible ({e}); downloading the full VOD instead")
        path = ingest.get_full(src, clip["vod_id"], cfg["ingest"]["max_height"])
        begin = 0.0
    store.update_clip(con, clip["id"], vod_path=str(path), media_start=begin)
    clip = store.get_clip(con, clip["id"])
    if not _covers(clip):
        # the section came back shorter than asked (keyframes); fit the clip inside it
        dur = media.probe(path)["duration"]
        s = max(clip["start"], begin)
        e = min(clip["end"], begin + dur)
        if e - s < 5:
            raise PipelineError(f"clip {clip['id']}: the downloaded section does not contain the clip")
        store.update_clip(con, clip["id"], start=round(s, 2), end=round(e, 2))
        clip = store.get_clip(con, clip["id"])
    return clip


def render_clip(con, clip_id, cfg, log=print):
    """Render one clip and mark it ready for review. Returns the clip row."""
    clip = store.get_clip(con, clip_id)
    if not clip:
        raise PipelineError(f"no clip with id {clip_id}")
    if clip["status"] == "uploaded":
        raise PipelineError(f"clip {clip_id} is already uploaded")
    streamer = cfg["streamers"].get(clip["streamer"])
    clip = ensure_video(con, clip, cfg, log)
    out = ensure_dirs()["clips"] / f"{clip['vod_id']}_{clip['id']:04d}.mp4"
    edit.render(clip["vod_path"], clip["start"], clip["end"], out, cfg["edit"],
                hook=clip["hook"], words=clip["words"], streamer=streamer,
                media_start=clip["media_start"], log=log)
    thumb = out.with_suffix(".jpg")
    try:
        at = min(max(clip["peak"] - clip["start"], 0.5), clip["end"] - clip["start"] - 0.5)
        edit.thumbnail(out, at, thumb)
    except media.MediaError:
        thumb = ""
    status = "rendered" if clip["status"] == "candidate" else clip["status"]
    store.update_clip(con, clip_id, file=str(out), thumb=str(thumb), status=status)
    return store.get_clip(con, clip_id)


def render_top(con, ids, cfg, limit=None, log=print):
    """Render the best `limit` candidates from `ids`. Returns the rendered rows."""
    clips = [c for c in (store.get_clip(con, i) for i in ids) if c and c["status"] == "candidate"]
    clips.sort(key=lambda c: (-c["llm_score"], -c["score"]))
    limit = cfg["rank"]["render_top"] if limit is None else limit
    done = []
    for c in clips[:limit]:
        try:
            row = render_clip(con, c["id"], cfg, log)
        except (PipelineError, media.MediaError, ingest.IngestError) as e:
            log(f"  #{c['id']} could not be rendered: {e}")
            continue
        done.append(row)
        log(f"  #{row['id']} {row['llm_score']:5.1f}  {row['start']:7.1f}s  {row['title']}")
    return done


def retrim(con, clip_id, start, end, cfg, log=print):
    """Change a clip's cut points, refresh its captions, and render it again."""
    clip = store.get_clip(con, clip_id)
    if not clip:
        raise PipelineError(f"no clip with id {clip_id}")
    if not 5 <= end - start <= 180:
        raise PipelineError("a clip must be 5 to 180 seconds long")
    if start < 0:
        raise PipelineError("start cannot be negative")
    store.update_clip(con, clip_id, start=round(start, 2), end=round(end, 2))
    refresh_captions(con, store.get_clip(con, clip_id), cfg, log)
    return render_clip(con, clip_id, cfg, log)


def frames(con, clip_id, cfg, log=print):
    """Write a contact sheet of the clip's source frames. Returns (path, times)."""
    clip = store.get_clip(con, clip_id)
    if not clip:
        raise PipelineError(f"no clip with id {clip_id}")
    clip = ensure_video(con, clip, cfg, log)
    out = ensure_dirs()["frames"] / f"clip_{clip_id:04d}.jpg"
    times = media.frame_sheet(clip["vod_path"], clip["start"] - clip["media_start"],
                              clip["end"] - clip["start"], out)
    return out, [round(t + clip["media_start"], 1) for t in times]
