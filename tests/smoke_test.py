"""Offline end-to-end check with synthetic footage (no network, no API keys).

Makes a 3-minute fake "stream" whose audio gets loud at 60s and 130s, fake
chat that spikes ~8s after each, then runs detect -> rank -> render for both
layouts and asserts the two moments were found and the outputs are 1080x1920.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from clipper import detect, edit, rank  # noqa: E402
from clipper.common import duration, load_config  # noqa: E402

tmp = ROOT / "data" / "test"
tmp.mkdir(parents=True, exist_ok=True)
vod = tmp / "fake_vod.mp4"
subprocess.run([
    "ffmpeg", "-v", "error", "-y",
    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=180",
    "-f", "lavfi", "-i", "sine=frequency=330:duration=180",
    "-af", "volume='if(between(t,60,65)+between(t,130,135),1.0,0.03)':eval=frame",
    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(vod)], check=True)

chat = [{"t": float(t), "text": "hi"} for t in range(0, 180, 4)]
for peak in (68, 138):
    chat += [{"t": peak + i * 0.1, "text": "LMAOOO"} for i in range(60)]

cfg = load_config()
dur = duration(vod)
cands = detect.find_moments(vod, chat, dur, cfg["detect"])
peaks = sorted(c["peak"] for c in cands[:2])
print("peaks found at", peaks)
assert len(cands) >= 2 and abs(peaks[0] - 60) <= 10 and abs(peaks[1] - 130) <= 10, cands

audio_only = detect.find_moments(vod, [], dur, cfg["detect"])
assert len(audio_only) >= 2, "audio-only detection failed"

cands = rank.rank(cands, "example", cfg["game"], cfg["rank"], use_llm=False)
c = cands[0]
words = [{"w": w, "s": 1 + i * 0.4, "e": 1.35 + i * 0.4}
         for i, w in enumerate("no way he just did that oh my god".split())]
for name, cam in (("cam", {"x": 0, "y": 0, "w": 480, "h": 270}), ("nocam", None)):
    out = edit.render(vod, c["start"], c["end"], tmp / f"out_{name}.mp4",
                      "He did NOT expect this", words, cam)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(out)],
        capture_output=True, text=True, check=True)
    s = json.loads(probe.stdout)["streams"][0]
    d = duration(out)
    print(name, s, f"{d:.1f}s")
    assert (s["width"], s["height"]) == (1080, 1920) and abs(d - (c["end"] - c["start"])) < 1.5
print("SMOKE TEST PASSED")
