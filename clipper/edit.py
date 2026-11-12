"""Cut a clip and finish it as a 1080x1920 short.

Layouts
  split  facecam panel on top, gameplay filling the rest (needs a facecam box)
  blur   gameplay centred over a blurred copy of itself
  crop   gameplay cropped to fill the whole frame

On top of the picture: the hook as a title card, word-by-word captions with
the spoken word highlighted, and a small credit for the streamer. Text stays
out of the top and bottom strips that TikTok / Shorts / Reels cover with
their own buttons. Audio is levelled to a common loudness so clips from a
quiet and a loud stream play at the same volume.
"""
import os
import re
import shutil
from pathlib import Path

from . import media
from .transcribe import clean_words

W, H = 1080, 1920
SAFE_TOP, SAFE_BOTTOM = 210, 1560        # keep text between these lines
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

_SLURS = re.compile(r"(?i)\b(n+i+g+[aeg]+\w*|f+a+g+\w*|r+e+t+a+r+d+\w*)")
_SWEARS = re.compile(r"(?i)\b(f+u+c+k+\w*|s+h+i+t+\w*|b+i+t+c+h+\w*|c+u+n+t+\w*|"
                     r"a+s+s+h+o+l+e+\w*|d+i+c+k+\w*|p+u+s+s+y+\w*)")


def even(x):
    return max(2, int(round(x)) // 2 * 2)


def ass_colour(hex_colour, alpha=0):
    """'#RRGGBB' -> ASS '&HAABBGGRR'."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_time(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(s):
    """Make arbitrary text safe inside an ASS event (no override codes, one line)."""
    s = str(s).replace("\\", "/").replace("{", "(").replace("}", ")")
    return " ".join(s.split())


def _mask(m):
    w = m.group(0)
    return w[0] + "*" * max(1, len(w) - 2) + (w[-1] if len(w) > 2 else "")


def censor(text, swears=False):
    """Slurs are always masked; strong language only when asked."""
    text = _SLURS.sub(_mask, text)
    return _SWEARS.sub(_mask, text) if swears else text


def wrap_hook(text, max_chars=20, max_lines=3):
    """Split the hook into balanced lines. Returns (lines, shrink) where shrink
    is True if the text was too long for the normal size."""
    words = text.split()
    if not words:
        return [], False
    total = len(" ".join(words))
    shrink = total > max_chars * max_lines
    if shrink:
        max_chars, max_lines = 28, 4
    best = None
    for n in range(1, max_lines + 1):
        target = total / n
        lines, cur = [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            # close the line once it has reached its share, unless it is the last one
            if cur and len(lines) < n - 1 and len(trial) > target + 2:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        widest = max(len(x) for x in lines)
        if best is None or widest < best[0]:
            best = (widest, lines)
        if widest <= max_chars:
            return lines, shrink
    return best[1], shrink


def caption_token(word, swears=False):
    tok = censor(ass_text(word), swears)
    tok = tok.strip(" .,;:\"()[]<>-—…")
    return tok.upper()


def caption_groups(words, max_words=3, max_chars=14):
    """Group words into short on-screen lines, breaking at pauses and sentence ends."""
    groups, cur = [], []
    for w in words:
        tok = w["w"].strip()
        if not tok:
            continue
        if cur:
            gap = w["s"] - cur[-1]["e"]
            length = len(" ".join(x["w"].strip() for x in cur)) + 1 + len(tok)
            ended = cur[-1]["w"].strip()[-1:] in ".?!"
            if len(cur) >= max_words or length > max_chars or gap > 0.6 or ended:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def choose_layout(cfg_edit, streamer):
    layout = (streamer or {}).get("layout") or cfg_edit["layout"]
    cam = (streamer or {}).get("facecam")
    if layout == "auto":
        layout = "split" if cam else "blur"
    if layout == "split" and not cam:
        layout = "blur"
    return layout


def geometry(layout, cfg_edit):
    """Where the text goes for each layout (y positions in the 1080x1920 frame)."""
    if layout == "split":
        seam = even(cfg_edit["cam_height"])
        return {"hook_y": seam, "cap_y": 1330, "credit_y": 1540, "game_top": seam, "game_bottom": H}
    if layout == "blur":
        fg = even(min(cfg_edit["blur_fg_height"], 1500))
        top = (H - fg) // 2
        hook_y = max(SAFE_TOP + 110, (SAFE_TOP + top) // 2 + 20)
        cap_y = min(SAFE_BOTTOM - 70, top + fg + 75)
        return {"hook_y": hook_y, "cap_y": cap_y, "credit_y": top + fg - 18,
                "game_top": top, "game_bottom": top + fg}
    return {"hook_y": SAFE_TOP + 120, "cap_y": 1330, "credit_y": 1540, "game_top": 0, "game_bottom": H}


def build_ass(hook, words, length, layout, cfg_edit, credit=""):
    """Return the .ass subtitle text for one clip. `words` are clip-relative."""
    g = geometry(layout, cfg_edit)
    font = ass_text(cfg_edit["font"]).replace(",", " ")
    cap_size = int(cfg_edit["caption_size"])
    hi = ass_colour(cfg_edit["caption_highlight"])
    lines, shrink = wrap_hook(censor(ass_text(hook), cfg_edit["censor"]))
    hook_size = int(cfg_edit["hook_size"] * (0.78 if shrink else 1.0))
    if cfg_edit["hook_style"] == "box":
        hook_style = (f"Style: Hook,{font},{hook_size},&H00141414,&H000000FF,&H00FFFFFF,&H78000000,"
                      "0,0,0,0,100,100,0,0,3,16,3,5,50,50,0,1")
    else:
        hook_style = (f"Style: Hook,{font},{hook_size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,"
                      "0,0,0,0,100,100,0,0,1,7,3,5,50,50,0,1")
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        hook_style,
        f"Style: Cap,{font},{cap_size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,"
        "0,0,0,0,100,100,0,0,1,7,3,5,40,40,0,1",
        f"Style: Credit,{font},{int(cfg_edit['hook_size'] * 0.45)},&H28FFFFFF,&H000000FF,&H50000000,&H96000000,"
        "0,0,0,0,100,100,0,0,1,3,0,1,40,40,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if lines:
        hook_end = length if not cfg_edit["hook_seconds"] else min(length, cfg_edit["hook_seconds"])
        # no fade-in: the hook must be readable on the very first frame (the preview)
        fade = "" if hook_end >= length else r"\fad(0,220)"
        pop = r"\fscx88\fscy88\t(0,140,\fscx100\fscy100)"
        out.append(f"Dialogue: 2,{ass_time(0)},{ass_time(hook_end)},Hook,,0,0,0,,"
                   f"{{\\an5\\pos({W // 2},{g['hook_y']}){fade}{pop}}}" + r"\N".join(lines))
    if credit and cfg_edit["show_credit"]:
        out.append(f"Dialogue: 0,{ass_time(0)},{ass_time(length)},Credit,,0,0,0,,"
                   f"{{\\an1\\pos(36,{g['credit_y']})}}{ass_text(credit)[:60]}")

    groups = caption_groups(clean_words(words or [], length), cfg_edit["caption_words"])
    pos = f"\\an5\\pos({W // 2},{g['cap_y']})"
    for gi, grp in enumerate(groups):
        toks = [caption_token(w["w"], cfg_edit["censor"]) for w in grp]
        if not any(toks):
            continue
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else length
        g_end = min(grp[-1]["e"] + 0.35, nxt, length)
        if nxt - g_end < 0.3:
            g_end = min(nxt, length)                     # no flicker between close lines
        for j, w in enumerate(grp):
            s = w["s"]
            e = grp[j + 1]["s"] if j + 1 < len(grp) else g_end
            if e - s < 0.02 or s >= length:
                continue
            parts = [f"{{\\1c{hi}&}}{t}{{\\1c&HFFFFFF&}}" if k == j else t
                     for k, t in enumerate(toks) if t]
            pop = r"\fscx86\fscy86\t(0,90,\fscx100\fscy100)" if j == 0 else ""
            out.append(f"Dialogue: 1,{ass_time(s)},{ass_time(min(e, length))},Cap,,0,0,0,,"
                       f"{{{pos}{pop}}}" + "  ".join(parts))
    return "\n".join(out) + "\n"


def scaled_cam(cam, frame, w, h):
    """Facecam box (measured on `frame`) mapped to the real w x h picture."""
    sx, sy = w / frame["w"], h / frame["h"]
    cw = min(even(cam["w"] * sx), even(w))
    ch = min(even(cam["h"] * sy), even(h))
    cx = min(max(0, int(cam["x"] * sx)), w - cw)
    cy = min(max(0, int(cam["y"] * sy)), h - ch)
    return cw, ch, cx, cy


def video_graph(layout, w, h, fps, cfg_edit, streamer):
    """ffmpeg filter graph producing [v0] (1080x1920, before text) from [0:v]."""
    fx = cfg_edit["focus_x"]
    head = f"[0:v]fps={fps:.3f},setsar=1"

    def fill(tw, th):
        """Crop the source to the target shape (keeping focus_x), then scale."""
        cw = min(even(h * tw / th), even(w))
        ch = min(even(cw * th / tw), even(h))
        cx = int((w - cw) * fx)
        cy = (h - ch) // 2
        return (f"crop={cw}:{ch}:{cx}:{cy},scale={tw}:{th}:force_original_aspect_ratio=increase:"
                f"flags=lanczos,crop={tw}:{th}")

    if layout == "split":
        cam_h = even(cfg_edit["cam_height"])
        cw, ch, cx, cy = scaled_cam(streamer["facecam"], streamer["frame"], w, h)
        return (f"{head},split=2[a][b];"
                f"[a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:force_original_aspect_ratio=increase:"
                f"flags=lanczos,crop={W}:{cam_h}[cam];"
                f"[b]{fill(W, H - cam_h)}[game];"
                "[cam][game]vstack,setsar=1[v0]")
    if layout == "blur":
        fg = even(min(cfg_edit["blur_fg_height"], 1500))
        return (f"{head},split=2[a][b];"
                "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
                f"scale={W}:{H}:flags=bilinear,eq=brightness=-0.10:saturation=1.1[bg];"
                f"[b]{fill(W, fg)}[fg];"
                "[bg][fg]overlay=0:(H-h)/2,setsar=1[v0]")
    return f"{head},{fill(W, H)},setsar=1[v0]"


def _fonts_into(folder):
    """Copy the bundled fonts next to the clips so the filter can use a relative path."""
    dst = Path(folder) / "fonts"
    dst.mkdir(parents=True, exist_ok=True)
    for f in FONT_DIR.glob("*.ttf"):
        if not (dst / f.name).exists():
            shutil.copy2(f, dst / f.name)
    return dst


def render(media_path, start, end, out_path, cfg_edit, hook="", words=None,
           streamer=None, media_start=0.0, log=print):
    """Render [start, end] (stream time) of `media_path` to `out_path`.

    `words` are in stream time, so trimming a clip keeps its captions in sync.
    Returns the output path.
    """
    media.require_tools()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    info = media.probe(media_path)
    if not info["has_video"]:
        raise media.MediaError(f"{media_path} has no video track")
    length = end - start
    if length <= 0:
        raise media.MediaError("clip end must be after its start")
    seek = max(0.0, start - media_start)
    if info["duration"] and seek >= info["duration"]:
        raise media.MediaError("clip start is beyond the end of the source video")
    if info["duration"]:
        length = min(length, info["duration"] - seek)

    layout = choose_layout(cfg_edit, streamer)
    fps = min(info["fps"] or 30.0, float(cfg_edit["fps"]))
    graph = video_graph(layout, info["width"], info["height"], fps, cfg_edit, streamer)

    rel = [{"w": w["w"], "s": w["s"] - start, "e": w["e"] - start} for w in (words or [])]
    credit = (streamer or {}).get("credit", "")
    ass = out_path.with_suffix(".ass")
    if media.has_filter("ass"):
        _fonts_into(out_path.parent)
        ass.write_text(build_ass(hook, rel, length, layout, cfg_edit, credit), encoding="utf-8")
        graph = graph.replace("[v0]", f",ass={ass.name}:fontsdir=fonts[v]")
    else:
        if hook or rel:
            log("  this ffmpeg build has no libass; rendering without on-screen text")
        graph = graph.replace("[v0]", "[v]")

    maps = ["-map", "[v]"]
    if info["has_audio"]:
        chain = ["afade=t=in:d=0.04", f"afade=t=out:st={max(0.0, length - 0.2):.3f}:d=0.2"]
        if cfg_edit["loudness"] is not None:
            chain.append(f"loudnorm=I={cfg_edit['loudness']}:TP=-1.5:LRA=11")
        chain.append("aresample=48000")
        graph += ";[0:a:0]" + ",".join(chain) + "[a]"
        maps += ["-map", "[a]"]

    if not media.has_encoder("libx264"):
        raise media.MediaError("this ffmpeg build has no libx264 encoder; install a full build")
    tmp = out_path.with_name(out_path.stem + ".tmp.mp4")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y",
           "-ss", f"{seek:.3f}", "-t", f"{length:.3f}", "-i", str(Path(media_path).resolve()),
           "-filter_complex", graph, *maps,
           "-c:v", "libx264", "-preset", cfg_edit["preset"], "-crf", str(cfg_edit["crf"]),
           "-maxrate", "16M", "-bufsize", "32M",
           "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", str(int(round(fps * 2))),
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
           "-c:a", "aac", "-b:a", cfg_edit["audio_bitrate"], "-ac", "2",
           "-movflags", "+faststart", "-max_muxing_queue_size", "1024", tmp.name]
    try:
        # run inside the output folder so the ass= and fontsdir= paths need no escaping
        media.run(cmd, cwd=out_path.parent)
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out_path


def thumbnail(clip_path, at, out_path):
    """Save a small still from a finished clip (used by the review page)."""
    out_path = Path(out_path)
    media.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, at):.3f}",
               "-i", str(clip_path), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4",
               str(out_path)])
    return out_path
