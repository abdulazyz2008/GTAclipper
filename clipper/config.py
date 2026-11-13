"""Workspace paths and validated settings.

A workspace is the folder you run `clipper` from (or $CLIPPER_HOME). It holds
`config.yaml` and a `data/` folder. Every setting has a default here, so a
config file only needs the values you want to change. Unknown keys and
out-of-range values are rejected with a readable message instead of being
silently ignored.
"""
import copy
import difflib
import os
import re
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised for a missing or invalid setting. The message is shown to the user."""


def home():
    return Path(os.environ.get("CLIPPER_HOME") or Path.cwd()).resolve()


def data_dir():
    return home() / "data"


def paths():
    d = data_dir()
    return {
        "data": d,
        "vods": d / "vods",
        "clips": d / "clips",
        "export": d / "export",
        "live": d / "live",
        "frames": d / "frames",
        "jobs": d / "jobs",
        "db": d / "clips.db",
        "config": home() / "config.yaml",
    }


def ensure_dirs():
    p = paths()
    for k in ("data", "vods", "clips", "export", "live", "frames", "jobs"):
        p[k].mkdir(parents=True, exist_ok=True)
    return p


def safe_name(text, fallback="clip"):
    """Reduce any string to something safe as a file name and inside an ffmpeg filter."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_")
    return s[:80] or fallback


# ---- defaults --------------------------------------------------------------

DEFAULTS = {
    "game": "GTA 6",
    "streamers": {},
    "ingest": {
        "mode": "smart",        # smart = audio first, then only the clip sections | full = whole VOD
        "max_height": 1080,
        "section_pad": 8,       # extra seconds fetched around each clip (room to re-trim)
    },
    "detect": {
        "bucket_sec": 5,
        "chat_lag": 8,
        "baseline_min": 10,     # minutes of history each moment is compared against
        "chat_weight": 0.6,
        "audio_weight": 0.4,
        "rate_weight": 0.45,    # inside the chat score: how busy chat is
        "accel_weight": 0.35,   # how fast chat is speeding up
        "hype_weight": 0.20,    # share of reaction messages (LUL, W, NO WAY...)
        "min_msgs": 4,          # ignore chat spikes smaller than this per bucket
        "hype_words": [],       # extra reaction words / emote names for this community
        "ignore_users": ["nightbot", "streamelements", "streamlabs", "moobot",
                         "fossabot", "wizebot", "soundalerts"],
        "clip_len": 35,
        "max_len": 55,
        "pre_roll": 12,
        "min_gap": 60,
        "top_n": 30,
        "min_score": 1.5,
    },
    "whisper": {
        "model": "small",
        "language": None,
        "device": "auto",
    },
    "rank": {
        "model": "claude-haiku-5-5",
        "render_top": 12,
    },
    "edit": {
        "layout": "auto",           # auto | split | blur | crop
        "fps": 60,                  # upper limit; slower sources keep their own rate
        "cam_height": 608,          # split layout: height of the facecam panel
        "blur_fg_height": 900,      # blur layout: height of the gameplay panel (608 = whole frame)
        "focus_x": 0.5,             # 0 = keep the left of the gameplay, 1 = the right
        "font": "Poppins ExtraBold",
        "hook_style": "box",        # box | outline
        "hook_seconds": 0,          # 0 = keep the hook on screen for the whole clip
        "caption_words": 3,
        "hook_size": 92,
        "caption_size": 108,
        "caption_highlight": "#FFE600",
        "censor": False,
        "show_credit": True,
        "loudness": -14,            # LUFS target; null = leave the audio level alone
        "crf": 19,
        "preset": "fast",
        "audio_bitrate": "192k",
    },
    "live": {
        "segment_sec": 20,
        "poll_sec": 10,
        "keep_min": 30,             # minutes of raw recording kept on disk
        "min_score": 2.5,           # live clips are cut the moment they clear this
        "max_per_hour": 8,
        "chat_offset": 0,           # extra seconds to shift live chat (positive = chat is late)
        "quality": "best",
    },
    "autopilot": {
        "check_min": 2,             # how often an offline channel is checked
    },
    "review": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "youtube": {
        "privacy": "private",
        "category_id": "20",
        "max_per_day": 10,
        "made_for_kids": False,
    },
    "instagram": {
        "user_id": "",              # Instagram professional account id; empty = not set up
        "api_version": "v26.0",
        "max_per_day": 10,
        "share_to_feed": True,
    },
}

STREAMER_DEFAULTS = {
    "credit": "",
    "hashtags": "#shorts",
    "channel": "",
    "facecam": None,
    "frame": {"w": 1920, "h": 1080},
    "layout": None,
}

# (type, min, max) or (type, choices) per leaf. None in a tuple means "no limit".
_RULES = {
    "ingest.mode": ("choice", ["smart", "full"]),
    "ingest.max_height": ("int", 144, 4320),
    "ingest.section_pad": ("num", 0, 60),
    "detect.bucket_sec": ("num", 1, 30),
    "detect.chat_lag": ("num", 0, 60),
    "detect.baseline_min": ("num", 1, 120),
    "detect.chat_weight": ("num", 0, 1),
    "detect.audio_weight": ("num", 0, 1),
    "detect.rate_weight": ("num", 0, 1),
    "detect.accel_weight": ("num", 0, 1),
    "detect.hype_weight": ("num", 0, 1),
    "detect.min_msgs": ("num", 0, 1000),
    "detect.hype_words": ("strlist",),
    "detect.ignore_users": ("strlist",),
    "detect.clip_len": ("num", 10, 180),
    "detect.max_len": ("num", 10, 180),
    "detect.pre_roll": ("num", 0, 120),
    "detect.min_gap": ("num", 0, 3600),
    "detect.top_n": ("int", 1, 500),
    "detect.min_score": ("num", 0, 100),
    "whisper.model": ("str",),
    "whisper.language": ("optstr",),
    "whisper.device": ("choice", ["auto", "cpu", "cuda"]),
    "rank.model": ("str",),
    "rank.render_top": ("int", 1, 500),
    "edit.layout": ("choice", ["auto", "split", "blur", "crop"]),
    "edit.fps": ("num", 15, 120),
    "edit.cam_height": ("int", 200, 1200),
    "edit.blur_fg_height": ("int", 400, 1600),
    "edit.focus_x": ("num", 0, 1),
    "edit.font": ("str",),
    "edit.hook_style": ("choice", ["box", "outline"]),
    "edit.hook_seconds": ("num", 0, 180),
    "edit.caption_words": ("int", 1, 6),
    "edit.hook_size": ("int", 30, 160),
    "edit.caption_size": ("int", 30, 200),
    "edit.caption_highlight": ("color",),
    "edit.censor": ("bool",),
    "edit.show_credit": ("bool",),
    "edit.loudness": ("optnum", -30, -5),
    "edit.crf": ("int", 0, 40),
    "edit.preset": ("choice", ["ultrafast", "superfast", "veryfast", "faster", "fast",
                               "medium", "slow", "slower", "veryslow"]),
    "edit.audio_bitrate": ("str",),
    "live.segment_sec": ("num", 4, 300),
    "live.poll_sec": ("num", 1, 300),
    "live.keep_min": ("num", 3, 1440),
    "live.min_score": ("num", 0, 100),
    "live.max_per_hour": ("int", 1, 1000),
    "live.chat_offset": ("num", -60, 60),
    "live.quality": ("str",),
    "review.host": ("str",),
    "review.port": ("int", 1, 65535),
    "youtube.privacy": ("choice", ["private", "unlisted", "public"]),
    "youtube.category_id": ("str",),
    "youtube.max_per_day": ("int", 1, 100),
    "youtube.made_for_kids": ("bool",),
    "instagram.user_id": ("idstr",),
    "instagram.api_version": ("str",),
    "instagram.max_per_day": ("int", 1, 100),
    "instagram.share_to_feed": ("bool",),
    "autopilot.check_min": ("num", 0.05, 120),
    "game": ("str",),
}


def _unknown(key, known, where):
    hint = difflib.get_close_matches(key, list(known), n=1)
    extra = f" Did you mean '{hint[0]}'?" if hint else ""
    return ConfigError(f"config.yaml: unknown setting '{key}' in {where}.{extra}")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(path, v):
    rule = _RULES[path]
    kind = rule[0]
    bad = None
    if kind == "choice":
        if v not in rule[1]:
            bad = "one of: " + ", ".join(map(str, rule[1]))
    elif kind in ("int", "num", "optnum"):
        if v is None and kind == "optnum":
            return v
        if not _is_num(v) or (kind == "int" and int(v) != v):
            bad = "a whole number" if kind == "int" else "a number"
        elif not rule[1] <= v <= rule[2]:
            bad = f"between {rule[1]} and {rule[2]}"
        elif kind == "int":
            v = int(v)
    elif kind == "str":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str) or not v.strip():
            bad = "a non-empty text value"
    elif kind == "optstr":
        if v is not None and not isinstance(v, str):
            bad = "text or null"
    elif kind == "idstr":
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str) or not re.fullmatch(r"[0-9]*", v):
            bad = "a number in quotes (or \"\" to leave it off)"
    elif kind == "bool":
        if not isinstance(v, bool):
            bad = "true or false"
    elif kind == "strlist":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            bad = "a list of words"
    elif kind == "color":
        if not isinstance(v, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            bad = "a colour like \"#FFE600\""
    if bad:
        raise ConfigError(f"config.yaml: '{path}' must be {bad} (got {v!r}).")
    return v


def _streamer(name, raw):
    if not isinstance(raw, dict):
        raise ConfigError(f"config.yaml: streamer '{name}' must be a block of settings.")
    for k in raw:
        if k not in STREAMER_DEFAULTS:
            raise _unknown(k, STREAMER_DEFAULTS, f"streamers.{name}")
    s = copy.deepcopy(STREAMER_DEFAULTS)
    s.update(raw)
    for k in ("credit", "hashtags", "channel"):
        if not isinstance(s[k], str):
            raise ConfigError(f"config.yaml: streamers.{name}.{k} must be text.")
    if not s["credit"]:
        s["credit"] = name
    if s["layout"] is not None and s["layout"] not in ("auto", "split", "blur", "crop"):
        raise ConfigError(f"config.yaml: streamers.{name}.layout must be auto, split, blur or crop.")
    fr = s["frame"]
    if (not isinstance(fr, dict) or set(fr) != {"w", "h"}
            or not all(_is_num(fr[k]) and fr[k] >= 16 for k in fr)):
        raise ConfigError(f"config.yaml: streamers.{name}.frame must look like {{w: 1920, h: 1080}}.")
    cam = s["facecam"]
    if cam is not None:
        if (not isinstance(cam, dict) or set(cam) != {"x", "y", "w", "h"}
                or not all(_is_num(cam[k]) for k in cam)):
            raise ConfigError(
                f"config.yaml: streamers.{name}.facecam must look like {{x: 0, y: 0, w: 480, h: 270}}.")
        if cam["w"] < 16 or cam["h"] < 16 or cam["x"] < 0 or cam["y"] < 0:
            raise ConfigError(f"config.yaml: streamers.{name}.facecam has a negative or tiny size.")
        if cam["x"] + cam["w"] > fr["w"] or cam["y"] + cam["h"] > fr["h"]:
            raise ConfigError(
                f"config.yaml: streamers.{name}.facecam does not fit inside the "
                f"{fr['w']}x{fr['h']} frame. Set streamers.{name}.frame to the stream's "
                "real resolution if it is not 1920x1080.")
    return s


def validate(raw):
    """Merge `raw` over the defaults and return a checked, complete config."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a set of 'key: value' settings.")
    cfg = copy.deepcopy(DEFAULTS)
    for key, val in raw.items():
        if key not in DEFAULTS:
            raise _unknown(key, DEFAULTS, "the top level")
        if key == "streamers":
            if val is None:
                val = {}
            if not isinstance(val, dict):
                raise ConfigError("config.yaml: 'streamers' must be a block of named streamers.")
            cfg["streamers"] = {str(n): _streamer(str(n), s or {}) for n, s in val.items()}
            for n in cfg["streamers"]:
                if safe_name(n, "") != n:
                    raise ConfigError(
                        f"config.yaml: streamer key '{n}' may only use letters, digits, '-' and '_'.")
        elif isinstance(DEFAULTS[key], dict):
            if val is None:
                continue
            if not isinstance(val, dict):
                raise ConfigError(f"config.yaml: '{key}' must be a block of settings.")
            for k, v in val.items():
                if k not in DEFAULTS[key]:
                    raise _unknown(k, DEFAULTS[key], f"'{key}'")
                cfg[key][k] = _check(f"{key}.{k}", v)
        else:
            cfg[key] = _check(key, val)
    d = cfg["detect"]
    if d["chat_weight"] + d["audio_weight"] <= 0:
        raise ConfigError("config.yaml: detect.chat_weight and detect.audio_weight cannot both be 0.")
    if d["rate_weight"] + d["accel_weight"] + d["hype_weight"] <= 0:
        raise ConfigError("config.yaml: the three detect.*_weight chat settings cannot all be 0.")
    if d["max_len"] < d["clip_len"]:
        raise ConfigError("config.yaml: detect.max_len must be at least detect.clip_len.")
    if d["pre_roll"] >= d["clip_len"]:
        raise ConfigError("config.yaml: detect.pre_roll must be shorter than detect.clip_len.")
    return cfg


def load_config(path=None):
    p = Path(path) if path else paths()["config"]
    if not p.exists():
        if path:
            raise ConfigError(f"config file not found: {p}")
        return validate({})
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{p.name} is not valid YAML: {e}") from None
    return validate(raw)


def streamer_cfg(cfg, name):
    if name not in cfg["streamers"]:
        known = ", ".join(cfg["streamers"]) or "none yet"
        raise ConfigError(
            f"streamer '{name}' is not in config.yaml (known: {known}). Add it under "
            "'streamers:' - only clip people whose permission covers reuse.")
    return cfg["streamers"][name]


TEMPLATE = """# ---- gta-clipper settings ----
# Only the values you want to change need to be here; everything else uses
# the built-in default. `clipper doctor` checks this file.
game: "GTA 6"

# One entry per streamer you have a campaign / written permission for.
#   credit   - shown on the clip and in the description
#   channel  - their live channel, used by `clipper watch`
#   facecam  - pixel rectangle of the webcam inside the stream frame.
#              Leave it out and clips use the blurred-background layout.
#   frame    - the stream resolution the facecam numbers were measured on
streamers:
  example:
    credit: "@example on Twitch"
    channel: "https://www.twitch.tv/example"
    facecam: {x: 0, y: 0, w: 480, h: 270}
    frame: {w: 1920, h: 1080}
    hashtags: "#gta6 #gtavi #gaming #shorts"

ingest:
  mode: smart             # smart = download audio + only the clip sections; full = whole VOD
  max_height: 1080

detect:
  chat_weight: 0.6        # chat vs. audio in the final score
  audio_weight: 0.4
  clip_len: 35            # normal clip length in seconds
  max_len: 55             # a clip may run this long while chat is still going
  pre_roll: 12            # seconds kept before the moment (the setup)
  min_score: 1.5          # lower = more clips, higher = only the big moments
  top_n: 30               # candidates kept per VOD
  hype_words: []          # extra emotes / words that mean "this is a moment"

whisper:
  model: small            # tiny / base / small / medium
  language: null          # e.g. "en"; null = detect automatically

rank:
  model: claude-haiku-5-5 # only used with ANTHROPIC_API_KEY (not needed inside Claude)
  render_top: 12          # how many ranked clips get rendered for review

edit:
  layout: auto            # auto / split (facecam on top) / blur / crop
  fps: 60
  hook_style: box         # box (white label) or outline
  caption_highlight: "#FFE600"
  censor: false           # mask strong language in captions

live:
  min_score: 2.5          # live clips are cut as soon as a moment clears this
  max_per_hour: 8

youtube:
  privacy: private        # private / unlisted / public
  max_per_day: 10

instagram:
  user_id: ""             # your Instagram professional account id (README, "Instagram setup")
  max_per_day: 10
"""


def init_workspace(force=False):
    """Create config.yaml and the data folders. Returns the list of things created."""
    p = ensure_dirs()
    made = []
    if force or not p["config"].exists():
        p["config"].write_text(TEMPLATE, encoding="utf-8")
        made.append(str(p["config"]))
    return made
