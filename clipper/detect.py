"""Step 2 - find the exciting moments.

Two signals, both bucketed on the same timeline:
  * chat messages per bucket (shifted back, because chat reacts late)
  * audio loudness (RMS) per bucket - shouting, explosions, laughter
Each is turned into a robust z-score so a quiet stream and a loud stream
are judged on their own baseline, then blended.
"""
import subprocess

import numpy as np

SR = 8000


def audio_rms(path, bucket):
    n = int(SR * bucket) * 2  # bytes per bucket (16-bit mono)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", str(SR), "-f", "s16le", "-"],
        stdout=subprocess.PIPE)
    out = []
    while True:
        buf = proc.stdout.read(n)
        if not buf:
            break
        a = np.frombuffer(buf[: len(buf) // 2 * 2], dtype=np.int16).astype(np.float32)
        out.append(float(np.sqrt(np.mean(a * a))) if len(a) else 0.0)
    proc.wait()
    return np.array(out, dtype=np.float32)


def robust_z(x):
    if len(x) == 0:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    if mad < 1e-6:
        mad = float(np.std(x)) or 1.0
    return np.clip((x - med) / mad, 0, None)


def smooth(x):
    return np.convolve(x, [0.25, 0.5, 0.25], mode="same") if len(x) > 2 else x


def find_moments(vod_path, chat, dur, cfg):
    b = cfg["bucket_sec"]
    audio = audio_rms(vod_path, b)
    nb = len(audio)
    if nb == 0:
        raise RuntimeError("no audio track found in the VOD")
    audio_z = smooth(robust_z(audio))

    chat_z = np.zeros(nb, dtype=np.float32)
    if chat:
        idx = ((np.array([m["t"] for m in chat]) - cfg["chat_lag"]) / b).astype(int)
        idx = idx[(idx >= 0) & (idx < nb)]
        counts = np.bincount(idx, minlength=nb)[:nb].astype(np.float32)
        chat_z = smooth(robust_z(counts))
        wc, wa = cfg["chat_weight"], cfg["audio_weight"]
    else:
        wc, wa = 0.0, 1.0
    score = wc * chat_z + wa * audio_z

    picked = []
    # ignore weak bumps: must clear min_score AND be at least 15% of the best moment
    floor = max(cfg["min_score"], 0.15 * float(score.max()))
    for i in np.argsort(-score):
        if score[i] <= floor or len(picked) >= cfg["top_n"]:
            break
        t = float(i * b)
        if any(abs(t - p["peak"]) < cfg["min_gap"] for p in picked):
            continue
        start = max(0.0, t - cfg["pre_roll"])
        end = min(dur, start + cfg["clip_len"])
        if end - start < 10:
            continue
        sample = [m["text"] for m in chat
                  if start <= m["t"] <= end + cfg["chat_lag"] and m["text"]][:15]
        picked.append({
            "peak": t, "start": round(start, 2), "end": round(end, 2),
            "score": round(float(score[i]), 3),
            "chat_score": round(float(chat_z[i]), 3),
            "audio_score": round(float(audio_z[i]), 3),
            "chat_sample": sample,
        })
    return picked
