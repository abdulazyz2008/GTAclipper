"""Config, paths and the small SQLite store shared by every step."""
import json
import sqlite3
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
VODS = DATA / "vods"
CLIPS = DATA / "clips"
EXPORT = DATA / "export"
DB_PATH = DATA / "clips.db"

SCHEMA = """CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vod_id TEXT, vod_path TEXT, streamer TEXT,
  start REAL, end REAL,
  score REAL, chat_score REAL, audio_score REAL,
  transcript TEXT DEFAULT '', words TEXT DEFAULT '[]', chat_sample TEXT DEFAULT '[]',
  llm_score REAL DEFAULT 0, hook TEXT DEFAULT '', title TEXT DEFAULT '', reason TEXT DEFAULT '',
  file TEXT DEFAULT '', status TEXT DEFAULT 'candidate',
  youtube_id TEXT DEFAULT '', uploaded_at TEXT DEFAULT '')"""


def load_config(path=None):
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def db():
    for d in (DATA, VODS, CLIPS, EXPORT):
        d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def insert_clip(con, row):
    row = dict(row)
    for k in ("words", "chat_sample"):
        if k in row and not isinstance(row[k], str):
            row[k] = json.dumps(row[k], ensure_ascii=False)
    cols = ",".join(row)
    cur = con.execute(
        f"INSERT INTO clips({cols}) VALUES({','.join('?' * len(row))})", list(row.values())
    )
    con.commit()
    return cur.lastrowid


def update_clip(con, clip_id, **fields):
    sets = ",".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE clips SET {sets} WHERE id=?", [*fields.values(), clip_id])
    con.commit()


def fetch_clips(con, where="1=1", params=()):
    return [dict(r) for r in con.execute(
        f"SELECT * FROM clips WHERE {where} ORDER BY llm_score DESC, score DESC", params)]


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)
