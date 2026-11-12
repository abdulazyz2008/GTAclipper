"""The clip database: one SQLite file in the workspace.

A clip moves through these states:
  candidate -> rendered -> approved | rejected -> uploaded
The live watcher, the review page and the CLI can all have it open at once,
so it runs in WAL mode with a busy timeout.
"""
import datetime as dt
import json
import sqlite3

from .config import ensure_dirs

STATUSES = ("candidate", "rendered", "approved", "rejected", "uploaded")

# name -> SQL type and default. Order is the column order of a fresh database.
COLUMNS = {
    "vod_id": "TEXT DEFAULT ''",
    "source": "TEXT DEFAULT ''",        # URL or file the clip came from
    "vod_path": "TEXT DEFAULT ''",      # video file the clip is cut from ('' = not fetched yet)
    "media_start": "REAL DEFAULT 0",    # stream time at which vod_path begins
    "audio_path": "TEXT DEFAULT ''",    # audio on the full stream timeline (analysis, captions)
    "streamer": "TEXT DEFAULT ''",
    "peak": "REAL DEFAULT 0",
    "start": "REAL DEFAULT 0",
    "end": "REAL DEFAULT 0",
    "score": "REAL DEFAULT 0",
    "chat_score": "REAL DEFAULT 0",
    "accel_score": "REAL DEFAULT 0",
    "hype_score": "REAL DEFAULT 0",
    "audio_score": "REAL DEFAULT 0",
    "transcript": "TEXT DEFAULT ''",
    "words": "TEXT DEFAULT '[]'",
    "chat_sample": "TEXT DEFAULT '[]'",
    "llm_score": "REAL DEFAULT 0",
    "judge": "TEXT DEFAULT ''",         # '', 'heuristic', 'api' or 'claude'
    "hook": "TEXT DEFAULT ''",
    "title": "TEXT DEFAULT ''",
    "reason": "TEXT DEFAULT ''",
    "file": "TEXT DEFAULT ''",
    "thumb": "TEXT DEFAULT ''",
    "status": "TEXT DEFAULT 'candidate'",
    "youtube_id": "TEXT DEFAULT ''",
    "uploaded_at": "TEXT DEFAULT ''",
    "created_at": "TEXT DEFAULT ''",
}
JSON_COLUMNS = ("words", "chat_sample")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(path=None):
    """Open (and create or upgrade) the database."""
    db_path = path or ensure_dirs()["db"]
    con = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    cols = ", ".join(f'"{k}" {v}' for k, v in COLUMNS.items())
    con.execute(f"CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
    have = {r["name"] for r in con.execute("PRAGMA table_info(clips)")}
    for k, v in COLUMNS.items():        # upgrade databases made by older versions
        if k not in have:
            con.execute(f'ALTER TABLE clips ADD COLUMN "{k}" {v}')
    con.execute("CREATE INDEX IF NOT EXISTS clips_status ON clips(status)")
    con.commit()
    return con


def _encode(fields):
    out = {}
    for k, v in fields.items():
        if k not in COLUMNS:
            raise KeyError(f"unknown clip field: {k}")
        if k in JSON_COLUMNS and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        out[k] = v
    if "status" in out and out["status"] not in STATUSES:
        raise ValueError(f"unknown status: {out['status']}")
    return out


def insert_clip(con, fields):
    row = _encode(fields)
    row.setdefault("created_at", now())
    cols = ",".join(f'"{k}"' for k in row)
    cur = con.execute(f"INSERT INTO clips({cols}) VALUES({','.join('?' * len(row))})",
                      list(row.values()))
    con.commit()
    return cur.lastrowid


def update_clip(con, clip_id, **fields):
    row = _encode(fields)
    if not row:
        return
    sets = ",".join(f'"{k}"=?' for k in row)
    con.execute(f"UPDATE clips SET {sets} WHERE id=?", [*row.values(), clip_id])
    con.commit()


def _decode(r):
    d = dict(r)
    for k in JSON_COLUMNS:
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (TypeError, ValueError):
            d[k] = []
    return d


def fetch_clips(con, where="1=1", params=(), order="llm_score DESC, score DESC, id"):
    """`where` and `order` are fixed strings from this codebase, never user input."""
    return [_decode(r) for r in
            con.execute(f"SELECT * FROM clips WHERE {where} ORDER BY {order}", params)]


def get_clip(con, clip_id):
    r = con.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    return _decode(r) if r else None


def counts(con):
    out = {s: 0 for s in STATUSES}
    for r in con.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status"):
        out[r["status"]] = r["n"]
    return out
