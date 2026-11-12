"""Shared fixtures. Everything runs offline on synthetic footage:

A fake 150-second "stream" whose audio gets loud at 50 s and 110 s, with a
fake chat that erupts a few seconds after each, plus one spammer at 80 s who
must not count as a moment.
"""
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipper import config, store  # noqa: E402

MOMENTS = (50, 110)
DUR = 150

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg is not installed")


def make_chat(moments=MOMENTS, dur=DUR, seed=7):
    rnd = random.Random(seed)
    users = [f"user{i}" for i in range(200)]
    chat, t = [], 0.0
    while t < dur:
        chat.append({"t": round(t, 2), "user": rnd.choice(users),
                     "text": rnd.choice(["hi", "what game is this", "nice", "ok", "hello",
                                         "!discord", "first time here"])})
        t += rnd.expovariate(0.6)
    for m in moments:
        for _ in range(90):
            chat.append({"t": round(m + 6 + rnd.random() * 12, 2), "user": rnd.choice(users),
                         "text": rnd.choice(["LMAOOO", "NO WAY", "KEKW", "CLIP IT", "OMEGALUL",
                                             "W", "HOLY", "???"])})
    for i in range(80):
        chat.append({"t": round(80 + i * 0.1, 2), "user": "spambot9000", "text": "BUY FOLLOWERS"})
    chat.sort(key=lambda m: m["t"])
    return chat


@pytest.fixture(scope="session")
def chat():
    return make_chat()


@pytest.fixture(scope="session")
def vod(tmp_path_factory):
    """Synthetic stream video (640x360, 30 fps) with a chat log saved beside it."""
    folder = tmp_path_factory.mktemp("footage")
    path = folder / "teststream.mp4"
    loud = "+".join(f"between(t,{m},{m + 6})" for m in MOMENTS)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={DUR}",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={DUR}:amplitude=0.6",
        "-af", f"volume='if({loud},1.0,0.04)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac",
        "-movflags", "+faststart", str(path)], check=True)
    path.with_suffix(".chat.json").write_text(json.dumps(make_chat()), encoding="utf-8")
    return path


TEST_CONFIG = """
streamers:
  example:
    credit: "@example on Twitch"
    facecam: {x: 0, y: 0, w: 480, h: 270}
    frame: {w: 1920, h: 1080}
  nocam:
    credit: "@nocam on Kick"
detect:
  clip_len: 14
  max_len: 18
  pre_roll: 5
  min_gap: 30
edit:
  fps: 30
  preset: ultrafast
live:
  poll_sec: 1
  segment_sec: 4
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fresh workspace folder with a small config; the current workspace for the test."""
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    (tmp_path / "config.yaml").write_text(TEST_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture
def cfg(workspace):
    return config.load_config()


@pytest.fixture
def con(workspace):
    c = store.connect()
    yield c
    c.close()


def near(value, target, tol):
    return abs(value - target) <= tol
