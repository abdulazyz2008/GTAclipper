from pathlib import Path

import pytest
from conftest import near, needs_ffmpeg

from clipper import media, pipeline, store, transcribe

pytestmark = needs_ffmpeg


def fake_whisper(monkeypatch):
    """Stand in for speech-to-text: three words, one second apart, from the clip start."""
    def fake(audio_path, start, end, cfg, offset=0.0, log=print):
        words = [{"w": w, "s": 1.0 + i, "e": 1.5 + i} for i, w in enumerate(["no", "way", "bro"])]
        return "no way bro", words
    monkeypatch.setattr(transcribe, "transcribe_window", fake)
    monkeypatch.setattr(transcribe, "available", lambda: True)


def test_scan_judge_render(vod, cfg, con, monkeypatch):
    fake_whisper(monkeypatch)
    logs = []
    ids = pipeline.scan(con, str(vod), "example", cfg, log=logs.append)
    assert len(ids) == 2
    clips = [store.get_clip(con, i) for i in ids]
    starts = sorted(c["start"] for c in clips)
    assert near(starts[0], 44, 6) and near(starts[1], 104, 6)
    for c in clips:
        assert c["status"] == "candidate" and c["transcript"] == "no way bro"
        assert c["words"][0]["s"] == pytest.approx(c["start"] + 1.0)     # stored in stream time
        assert c["accel_score"] > 0 and c["chat_sample"]

    pipeline.judge(con, ids, "example", cfg, use_llm=True, log=logs.append)   # no key -> heuristic
    assert all(store.get_clip(con, i)["judge"] == "heuristic" for i in ids)
    assert any("no Claude API key" in str(line) for line in logs)

    done = pipeline.render_top(con, ids, cfg, limit=1, log=logs.append)
    assert len(done) == 1
    row = done[0]
    assert row["status"] == "rendered" and Path(row["file"]).exists() and Path(row["thumb"]).exists()
    info = media.probe(row["file"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert near(info["duration"], row["end"] - row["start"], 0.3)
    assert ",Cap," in Path(row["file"]).with_suffix(".ass").read_text()
    other = store.get_clip(con, [i for i in ids if i != row["id"]][0])
    assert other["status"] == "candidate" and not other["file"]


def test_rescan_keeps_reviewed_clips_and_replaces_candidates(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    store.update_clip(con, ids[0], status="approved")
    again = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    assert len(again) == 1                              # the approved moment is not found twice
    rows = store.fetch_clips(con)
    assert len(rows) == 2 and sorted(r["status"] for r in rows) == ["approved", "candidate"]


def test_unknown_streamer_and_missing_file(vod, cfg, con):
    from clipper import config, ingest
    with pytest.raises(config.ConfigError):
        pipeline.scan(con, str(vod), "nobody", cfg)
    with pytest.raises(ingest.IngestError):
        pipeline.scan(con, "does-not-exist.mp4", "example", cfg)


def test_apply_judgements_is_all_or_nothing(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    good = {"id": ids[0], "score": 90, "hook": "A true hook", "title": "A fine title", "reason": "r"}
    problems = pipeline.apply_judgements(con, [
        good,
        {"id": ids[1], "score": 150, "hook": "one two three four five six seven eight nine",
         "title": "BAD <TITLE>", "reason": ""},
        {"id": 9999, "score": 1, "title": "x"},
        {"score": 1},
    ])
    text = "\n".join(problems)
    assert "score must be" in text and "hook must be" in text and "< or >" in text
    assert "no such clip" in text and 'needs a whole-number "id"' in text
    assert store.get_clip(con, ids[0])["judge"] == ""           # the valid one was not applied either

    assert pipeline.apply_judgements(con, [good]) == []
    c = store.get_clip(con, ids[0])
    assert (c["llm_score"], c["hook"], c["title"], c["judge"]) == (90, "A true hook", "A fine title", "claude")


def test_judgement_can_move_the_cut_points(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    c = store.get_clip(con, ids[0])
    j = {"id": c["id"], "score": 80, "hook": "h", "title": "t", "start": c["start"] + 2,
         "end": c["end"] - 1}
    assert pipeline.apply_judgements(con, [j], cfg) == []
    after = store.get_clip(con, c["id"])
    assert after["start"] == c["start"] + 2 and after["end"] == c["end"] - 1
    bad = dict(j, start=10, end=12)
    assert "5 to 180 seconds" in pipeline.apply_judgements(con, [bad], cfg)[0]
    assert pipeline.apply_judgements(con, "nonsense")[0].startswith("the file must hold")


def test_judge_does_not_overwrite_claudes_verdict(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    pipeline.apply_judgements(con, [{"id": ids[0], "score": 5, "hook": "", "title": "Mine"}])
    pipeline.judge(con, ids, "example", cfg, use_llm=False, log=lambda *_: None)
    assert store.get_clip(con, ids[0])["title"] == "Mine"
    assert store.get_clip(con, ids[1])["judge"] == "heuristic"


def test_retrim_rerenders_and_refreshes_captions(vod, cfg, con, monkeypatch):
    fake_whisper(monkeypatch)
    ids = pipeline.scan(con, str(vod), "example", cfg, log=lambda *_: None)
    row = pipeline.render_clip(con, ids[0], cfg, log=lambda *_: None)
    new = pipeline.retrim(con, row["id"], row["start"] + 3, row["end"], cfg, log=lambda *_: None)
    assert new["start"] == row["start"] + 3
    assert near(media.probe(new["file"])["duration"], new["end"] - new["start"], 0.3)
    assert new["words"][0]["s"] == pytest.approx(new["start"] + 1.0)
    with pytest.raises(pipeline.PipelineError):
        pipeline.retrim(con, row["id"], 10, 11, cfg)


def test_render_missing_source_is_a_clear_error(cfg, con):
    cid = store.insert_clip(con, {"vod_id": "x", "streamer": "example", "start": 1, "end": 20,
                                  "vod_path": "/nowhere/gone.mp4", "source": "/nowhere/gone.mp4"})
    with pytest.raises(pipeline.PipelineError) as e:
        pipeline.render_clip(con, cid, cfg)
    assert "missing" in str(e.value)
    assert pipeline.render_top(con, [cid], cfg, log=lambda *_: None) == []


def test_uploaded_clips_are_locked(vod, cfg, con):
    cid = store.insert_clip(con, {"vod_id": "x", "streamer": "example", "start": 1, "end": 20,
                                  "status": "uploaded"})
    with pytest.raises(pipeline.PipelineError):
        pipeline.render_clip(con, cid, cfg)
    assert "already uploaded" in pipeline.apply_judgements(
        con, [{"id": cid, "score": 1, "hook": "", "title": "t"}])[0]


def test_frames_sheet(vod, cfg, con):
    ids = pipeline.scan(con, str(vod), "example", cfg, use_whisper=False, log=lambda *_: None)
    path, times = pipeline.frames(con, ids[0], cfg)
    c = store.get_clip(con, ids[0])
    assert path.exists() and len(times) == 10
    assert c["start"] < times[0] < times[-1] < c["end"]


def test_old_database_is_upgraded(workspace):
    import sqlite3
    db = store.ensure_dirs()["db"]
    old = sqlite3.connect(db)
    old.execute("""CREATE TABLE clips(id INTEGER PRIMARY KEY AUTOINCREMENT, vod_id TEXT,
        vod_path TEXT, streamer TEXT, start REAL, end REAL, score REAL, chat_score REAL,
        audio_score REAL, transcript TEXT DEFAULT '', words TEXT DEFAULT '[]',
        chat_sample TEXT DEFAULT '[]', llm_score REAL DEFAULT 0, hook TEXT DEFAULT '',
        title TEXT DEFAULT '', reason TEXT DEFAULT '', file TEXT DEFAULT '',
        status TEXT DEFAULT 'candidate', youtube_id TEXT DEFAULT '', uploaded_at TEXT DEFAULT '')""")
    old.execute("INSERT INTO clips(vod_id, streamer, start, end, title) VALUES('v','s',1,2,'old')")
    old.commit()
    old.close()
    con = store.connect()
    row = store.fetch_clips(con)[0]
    assert row["title"] == "old" and row["media_start"] == 0 and row["accel_score"] == 0
    con.close()
