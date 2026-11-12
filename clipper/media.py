"""Thin, checked wrappers around ffmpeg / ffprobe."""
import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

SR = 8000  # sample rate used for all loudness analysis


class MediaError(Exception):
    """ffmpeg or ffprobe failed. The message carries the tool's own error text."""


def require_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise MediaError(
            f"{' and '.join(missing)} not found. Install ffmpeg (https://ffmpeg.org/download.html) "
            "and make sure it is on your PATH.")


def run(cmd, cwd=None):
    """Run an ffmpeg command; raise MediaError with the tail of stderr on failure."""
    try:
        p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True)
    except FileNotFoundError:
        raise MediaError(f"{cmd[0]} not found. Install ffmpeg and put it on your PATH.") from None
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise MediaError(f"{cmd[0]} failed: " + " | ".join(tail))
    return p


@lru_cache(maxsize=8)
def _filters():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return {line.split()[1] for line in out.splitlines() if len(line.split()) > 2}


def has_filter(name):
    try:
        return name in _filters()
    except FileNotFoundError:
        return False


@lru_cache(maxsize=8)
def has_encoder(name):
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines())


def probe(path):
    """Return duration, video size / frame rate and whether audio and video exist."""
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration",
             "-of", "json", str(path)])
    data = json.loads(p.stdout or b"{}")
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
            "has_video": False, "has_audio": False}
    try:
        info["duration"] = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        pass
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
        elif s.get("codec_type") == "video" and not info["has_video"]:
            info["has_video"] = True
            info["width"], info["height"] = int(s.get("width") or 0), int(s.get("height") or 0)
            for key in ("avg_frame_rate", "r_frame_rate"):
                num, _, den = (s.get(key) or "0/1").partition("/")
                try:
                    fps = float(num) / float(den or 1)
                except (ValueError, ZeroDivisionError):
                    fps = 0.0
                if 1 <= fps <= 240:
                    info["fps"] = fps
                    break
        if not info["duration"]:
            try:
                info["duration"] = float(s.get("duration") or 0)
            except ValueError:
                pass
    return info


def duration(path):
    return probe(path)["duration"]


def envelope(path, hop, start=None, length=None):
    """Loudness (RMS, linear) of the audio in `hop`-second steps as a float32 array.

    Decodes audio only, at 8 kHz mono, so a 3-hour VOD takes a minute or two.
    `start` / `length` restrict the range (fast seek).
    """
    n = max(1, int(round(SR * hop)))
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    if length is not None:
        cmd += ["-t", f"{max(0.0, length):.3f}"]
    cmd += ["-i", str(path), "-vn", "-sn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise MediaError("ffmpeg not found. Install ffmpeg and put it on your PATH.") from None
    out = []
    block = n * 2 * 256           # 256 hops per read
    rest = b""
    while True:
        buf = proc.stdout.read(block)
        if not buf:
            break
        buf = rest + buf
        usable = len(buf) // (n * 2) * (n * 2)
        rest = buf[usable:]
        if usable:
            a = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32).reshape(-1, n)
            out.append(np.sqrt(np.mean(a * a, axis=1)))
    proc.wait()
    if len(rest) >= n:            # count a final partial hop if it is at least half full
        a = np.frombuffer(rest[: len(rest) // 2 * 2], dtype=np.int16).astype(np.float32)
        out.append(np.array([float(np.sqrt(np.mean(a * a)))], dtype=np.float32))
    if not out:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(out).astype(np.float32)


def to_db(x):
    """Linear RMS -> decibels, with a floor so silence does not become -inf."""
    return 20.0 * np.log10(np.maximum(np.asarray(x, dtype=np.float32), 1.0))


def frame_sheet(path, start, length, out_path, count=10, cols=5, width=384):
    """Write one image holding `count` evenly spaced frames; returns their times."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = -(-count // cols)
    step = length / count
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, start + step / 2):.3f}",
         "-t", f"{length:.3f}", "-i", str(path),
         "-vf", f"fps=1/{step:.4f},scale={width}:-2,tile={cols}x{rows}:padding=4",
         "-frames:v", "1", "-q:v", "4", str(out_path)])
    return [round(start + step * (i + 0.5), 1) for i in range(count)]
