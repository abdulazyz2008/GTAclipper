"""Find the moments worth clipping.

Everything is bucketed on one timeline (default 5 s). Per bucket:

  chat rate          how many different people are typing
  chat acceleration  how fast that rate is climbing - the jump itself, which
                     marks the instant something happened better than the peak
  hype share         reaction messages (LUL, KEKW, W, NO WAY, "CLIP IT"...)
  audio loudness     shouting, explosions, laughter

Each signal is scored against a rolling baseline of the minutes *before* it,
never the future. That makes a quiet stream and a loud one comparable, copes
with chat growing over a long stream, and means a live stream and its VOD get
the same scores.
"""
import re
from collections import Counter

import numpy as np

from . import media

Z_CAP = 10.0
WARMUP = 12          # buckets of history needed before anything is scored

# reactions that mean "something just happened" wherever they appear
_HYPE_STRONG = (
    r"lul\w*|kekw?\w*|omegalul\w*|lmf?ao+|lo+l+|rofl|pog\w*|wtf+|omf?g+|holy|"
    r"no ?way+|clip(?: it| that|ped)|sheesh+|ayo+|bruh+|oof+|monka\w*|pepelaugh|icant|"
    r"xd+|(?:ha){2,}h?|(?:ja){2,}|k{3,}|let'?s go+|w+|l+"
)
# everyday words that only count as a reaction when they are the whole message
_HYPE_WEAK = (
    r"what+|huh+|nah+|bro+|dead|insane|crazy|gg|ez|rip|yo+|clip|wow+|damn+|no+|"
    r"oh my god|omg bro|he did not|she did not"
)
_HYPE_SYMBOLS = r"[!?]{2,}|[\U0001F480\U0001F602\U0001F923\U0001F525\U0001F631\U0001F62D]"


def hype_pattern(extra=()):
    """Compile the reaction matcher; `extra` adds community emotes / words."""
    strong = _HYPE_STRONG
    extra = [re.escape(w.strip()) for w in extra if w and w.strip()]
    if extra:
        strong += "|" + "|".join(extra)
    # the apostrophes keep "I'll" and "we'll" from reading as the reaction "L"
    return (re.compile(rf"(?i)(?<![\w'\u2019])(?:{strong})(?![\w'\u2019])|{_HYPE_SYMBOLS}"),
            re.compile(rf"(?i)\W*(?:{_HYPE_WEAK})(?:\W+(?:{_HYPE_WEAK}))*\W*"))


def is_hype(text, pattern):
    """A reaction message: a hype word / emote, a bare exclamation like "what"
    or "bro", or a short burst of capitals."""
    strong, weak = pattern
    if strong.search(text) or weak.fullmatch(text):
        return True
    letters = [c for c in text if c.isalpha()]
    return 3 <= len(letters) <= 24 and all(c.isupper() for c in letters)


def chat_series(chat, nb, bucket, lag, cfg):
    """Per-bucket chat rate and hype count, shifted back by `lag` seconds.

    What is counted is how many different people typed, not how many
    messages there were, so one spammer or bot cannot fake a spike. Commands
    and known bots are dropped. Logs without user names count every message.
    """
    rate = np.zeros(nb, dtype=np.float32)
    hype = np.zeros(nb, dtype=np.float32)
    pattern = hype_pattern(cfg.get("hype_words", ()))
    ignore = {u.lower() for u in cfg.get("ignore_users", ())}
    seen = set()
    for m in chat:
        text = (m.get("text") or "").strip()
        user = (m.get("user") or "").lower()
        if not text or text.startswith("!") or (user and user in ignore):
            continue
        i = int((m["t"] - lag) // bucket)
        if not 0 <= i < nb:
            continue
        if user:
            if (i, user) in seen:
                continue
            seen.add((i, user))
        rate[i] += 1.0
        if is_hype(text, pattern):
            hype[i] += 1.0
    return rate, hype


def smooth(x):
    if len(x) < 3:
        return np.asarray(x, dtype=np.float32)
    return np.convolve(x, [0.25, 0.5, 0.25], mode="same").astype(np.float32)


def trailing_z(x, win, poisson=True, floor=1.0):
    """Score each bucket against the median / spread of the `win` buckets before it.

    Returns (z, scale). `poisson` adds a counting-noise floor to the spread so
    that going from 0 to 2 messages in a dead chat is not an "infinite" spike.
    The first WARMUP buckets are judged against themselves (there is no history).
    """
    x = np.asarray(x, dtype=np.float32)
    n = len(x)
    z = np.zeros(n, dtype=np.float32)
    scale = np.ones(n, dtype=np.float32)
    if n < WARMUP:
        return z, scale
    for i in range(n):
        ref = x[max(0, i - win): i] if i >= WARMUP else x[:WARMUP]
        med = float(np.median(ref))
        mad = float(np.median(np.abs(ref - med))) * 1.4826
        s = max(mad, floor, np.sqrt(max(med, 1.0)) if poisson else 0.0)
        scale[i] = s
        z[i] = (x[i] - med) / s
    return np.clip(z, 0, Z_CAP), scale


def score_timeline(chat, audio_rms, cfg):
    """Return a dict of per-bucket arrays: total, chat, rate, accel, hype, audio."""
    b = cfg["bucket_sec"]
    nb = len(audio_rms)
    win = max(WARMUP, int(cfg["baseline_min"] * 60 / b))
    zero = np.zeros(nb, dtype=np.float32)

    if nb:
        audio_z, _ = trailing_z(smooth(media.to_db(audio_rms)), win, poisson=False, floor=2.0)
    else:
        audio_z = zero

    rate, hype = chat_series(chat, nb, b, cfg["chat_lag"], cfg) if chat else (zero, zero)
    has_chat = bool(nb) and float(rate.sum()) > 0
    if has_chat:
        rate_z, scale = trailing_z(rate, win)
        rs = smooth(rate)
        accel = np.diff(rs, prepend=rs[:1]) / scale
        accel_z = np.clip(accel, 0, Z_CAP)
        # the jump comes a bucket or two before the peak: credit it to the peak
        near = accel_z.copy()
        for k in (1, 2):
            near[k:] = np.maximum(near[k:], accel_z[:-k])
        hype_z, _ = trailing_z(hype, win)
        wr, wa, wh = cfg["rate_weight"], cfg["accel_weight"], cfg["hype_weight"]
        chat_z = (wr * rate_z + wa * near + wh * hype_z) / (wr + wa + wh)
        # a "spike" of a couple of messages is noise, however quiet chat was
        busy = rate.copy()
        busy[1:] = np.maximum(busy[1:], rate[:-1])
        busy[:-1] = np.maximum(busy[:-1], rate[1:])
        chat_z = smooth(np.where(busy >= cfg["min_msgs"], chat_z, 0.0))
        wc, wau = cfg["chat_weight"], cfg["audio_weight"]
        total = (wc * chat_z + wau * audio_z) / (wc + wau)
    else:
        rate_z = near = hype_z = chat_z = zero
        total = audio_z
    return {"total": total.astype(np.float32), "chat": chat_z, "rate": rate_z, "accel": near,
            "hype": hype_z, "audio": audio_z, "raw_rate": rate, "has_chat": has_chat}


def chat_onset(chat, peak_t, bucket, lag):
    """Second at which chat started to take off before `peak_t` (lag already removed)."""
    lo, hi = peak_t - 20.0, peak_t + bucket
    ts = np.array([m["t"] - lag for m in chat if lo <= m["t"] - lag < hi], dtype=np.float32)
    if len(ts) < 4:
        return None
    hist = np.bincount((ts - lo).astype(int), minlength=int(hi - lo))[: int(hi - lo)]
    sm = np.convolve(hist, np.ones(3) / 3.0, mode="same")
    rise = sm[2:] - sm[:-2]                 # change over a 2 s span, centred
    if rise.max() <= 0:
        return None
    return float(lo + 1 + int(np.argmax(rise)))


def chat_sample(chat, start, end, limit=15):
    """The most repeated messages in a window, most common first, as 'text xN'."""
    c = Counter()
    shown = {}
    for m in chat:
        if start <= m["t"] <= end:
            text = " ".join((m.get("text") or "").split())[:80]
            if text and not text.startswith("!"):
                key = text.lower()
                c[key] += 1
                shown.setdefault(key, text)
    return [shown[k] + (f" x{n}" if n > 1 else "") for k, n in c.most_common(limit)]


def find_moments(chat, audio_rms, dur, cfg, min_score=None, top_n=None,
                 relative_floor=True, ready_until=None, taken=()):
    """Pick the best moments. Returns a list of candidate dicts, best first.

    `ready_until` (live mode) only returns moments whose whole clip is already
    recorded; `taken` is a list of peak times that were already clipped.
    """
    b = cfg["bucket_sec"]
    s = score_timeline(chat, audio_rms, cfg)
    total = s["total"]
    nb = len(total)
    if nb == 0:
        return []
    min_score = cfg["min_score"] if min_score is None else min_score
    top_n = cfg["top_n"] if top_n is None else top_n
    floor = max(min_score, 0.15 * float(total.max())) if relative_floor else min_score
    sm = smooth(total)

    picked = []
    used = list(taken)
    for i in np.argsort(-total, kind="stable"):
        if total[i] < floor or total[i] <= 0 or len(picked) >= top_n:
            break
        if (i > 0 and total[i - 1] > total[i]) or (i + 1 < nb and total[i + 1] > total[i]):
            continue                         # not a local peak
        peak = float(i * b)
        if any(abs(peak - p) < cfg["min_gap"] for p in used):
            continue

        onset = chat_onset(chat, peak + b, b, cfg["chat_lag"]) if s["chat"][i] > 0 else None
        event = min(onset, peak) if onset is not None else peak
        start = max(0.0, event - cfg["pre_roll"])

        # run until the reaction dies down, between clip_len and max_len
        j = i
        while j + 1 < nb and sm[j + 1] > 0.35 * sm[i] and (j + 2) * b < start + cfg["max_len"]:
            j += 1
        end = (j + 1) * b + 2.0
        end = max(end, start + cfg["clip_len"])
        end = min(end, start + cfg["max_len"], dur)
        if end - start < min(12.0, cfg["clip_len"]):
            continue
        if ready_until is not None and (end > ready_until or i + 2 >= nb):
            continue                         # still happening; pick it up next time
        used.append(peak)
        picked.append({
            "peak": peak,
            "start": round(float(start), 2), "end": round(float(end), 2),
            "score": round(float(total[i]), 3),
            "chat_score": round(float(s["chat"][i]), 3),
            "accel_score": round(float(s["accel"][i]), 3),
            "hype_score": round(float(s["hype"][i]), 3),
            "audio_score": round(float(s["audio"][i]), 3),
            "chat_sample": chat_sample(chat, start + cfg["chat_lag"], end + cfg["chat_lag"]),
        })
    return picked


def snap_boundaries(audio_path, start, end, lo, hi, offset=0.0):
    """Nudge a clip's start and end onto pauses in the audio so it does not
    open or close in the middle of a word.

    `lo` / `hi` are the stream times available; `offset` is the stream time at
    which `audio_path` begins. Returns (start, end), unchanged if there is no
    clear pause nearby.
    """
    hop = 0.05
    a0 = max(lo, start - 2.5)
    a1 = min(hi, end + 2.5)
    try:
        env = media.to_db(media.envelope(audio_path, hop, a0 - offset, a1 - a0))
    except media.MediaError:
        return start, end
    if len(env) < 40:
        return start, end
    k = np.ones(6) / 6.0                      # 0.3 s average
    env = np.convolve(env, k, mode="same")
    typical = float(np.median(env))

    def snap(t, back, fwd):
        i0 = max(0, int((t - back - a0) / hop))
        i1 = min(len(env), int((t + fwd - a0) / hop) + 1)
        if i1 - i0 < 4:
            return t
        idx = np.arange(i0, i1)
        times = a0 + idx * hop
        cost = env[i0:i1] + 2.0 * np.abs(times - t)      # prefer quiet, and close to t
        best = int(np.argmin(cost))
        if env[i0 + best] > typical - 6.0:               # no real pause here
            return t
        return float(times[best])

    new_start = max(lo, snap(start, 2.0, 1.5))
    new_end = min(hi, snap(end, 1.5, 2.0) + 0.15)
    if new_end - new_start < 8:
        return start, end
    return round(new_start, 2), round(new_end, 2)
