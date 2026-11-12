"""Step 4 - cut the clip and make it vertical (1080x1920) with ffmpeg.

Two layouts:
  * facecam known  -> webcam on top (1080x608), gameplay below (1080x1312)
  * facecam absent -> gameplay centred over a blurred copy of itself
Hook text and word-timed captions are burned in from an .ass subtitle file.
"""
import subprocess
from functools import lru_cache
from pathlib import Path

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,76,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,8,60,60,{hook_mv},1
Style: Cap,Arial,70,&H0000FFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,{cap_mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "/").replace("\n", " ").strip()


def build_ass(hook, words, dur, has_cam, per_line=3):
    out = ASS_HEAD.format(hook_mv=570 if has_cam else 250, cap_mv=300 if has_cam else 330)
    if hook:
        out += f"Dialogue: 1,{_ts(0)},{_ts(dur)},Hook,,0,0,0,,{_clean(hook)}\n"
    for i in range(0, len(words), per_line):
        chunk = words[i: i + per_line]
        s = chunk[0]["s"]
        e = words[i + per_line]["s"] if i + per_line < len(words) else chunk[-1]["e"] + 0.3
        e = min(max(e, s + 0.2), dur)
        if s >= dur:
            break
        text = _clean(" ".join(w["w"] for w in chunk)).upper()
        out += f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{text}\n"
    return out


@lru_cache(maxsize=1)
def has_ass_filter():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return any(line.split()[1:2] == ["ass"] for line in out.splitlines() if line.strip())


def render(vod_path, start, end, out_path, hook="", words=None, facecam=None):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    subs = ""
    if (hook or words) and has_ass_filter():
        ass = out_path.with_suffix(".ass")
        ass.write_text(build_ass(hook, words or [], dur, bool(facecam)), encoding="utf-8")
        subs = f",ass={ass.name}"
    elif hook or words:
        print("  this ffmpeg build has no libass; rendering without text")

    if facecam:
        f = {k: int(facecam[k]) for k in ("x", "y", "w", "h")}
        graph = (
            "[0:v]split=2[a][b];"
            f"[a]crop={f['w']}:{f['h']}:{f['x']}:{f['y']},"
            "scale=1080:608:force_original_aspect_ratio=increase,crop=1080:608[cam];"
            "[b]crop=trunc(ih*1080/1312/2)*2:ih,scale=1080:1312[game];"
            f"[cam][game]vstack,setsar=1{subs}[v]")
    else:
        graph = (
            "[0:v]split=2[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "boxblur=20:2[bg];"
            "[b]crop=trunc(ih*1.2/2)*2:ih,scale=1080:900[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{subs}[v]")

    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(dur),
           "-i", str(Path(vod_path).resolve()),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
           "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out_path.name]
    # run inside the output folder so the ass= path needs no escaping
    subprocess.run(cmd, check=True, cwd=out_path.parent)
    return out_path
