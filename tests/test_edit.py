import subprocess

import pytest
from conftest import needs_ffmpeg

from clipper import config, edit, media


@pytest.fixture
def ecfg():
    return config.validate({"edit": {"fps": 30, "preset": "ultrafast"}})["edit"]


WORDS = [{"w": w, "s": 1 + i * 0.4, "e": 1.35 + i * 0.4}
         for i, w in enumerate("No way he just did that, oh my god!".split())]


def test_wrap_hook_balances_lines():
    lines, shrink = edit.wrap_hook("He did NOT expect the cops to do this")
    assert not shrink and len(lines) == 2
    assert " ".join(lines) == "He did NOT expect the cops to do this"
    assert max(map(len, lines)) <= 20


def test_wrap_hook_short_and_long():
    assert edit.wrap_hook("Wow") == (["Wow"], False)
    assert edit.wrap_hook("") == ([], False)
    text = "this hook is far too long because a person typed a whole paragraph into the box"
    lines, shrink = edit.wrap_hook(text)
    assert shrink and " ".join(lines) == text


def test_caption_groups_break_on_pauses_sentences_and_length():
    words = [{"w": "No", "s": 0, "e": 0.2}, {"w": "way.", "s": 0.2, "e": 0.5},
             {"w": "He", "s": 0.5, "e": 0.7}, {"w": "did", "s": 2.0, "e": 2.2},
             {"w": "extraordinarily", "s": 2.2, "e": 2.9}, {"w": "well", "s": 2.9, "e": 3.1}]
    groups = [[w["w"] for w in g] for g in edit.caption_groups(words)]
    assert groups == [["No", "way."], ["He"], ["did"], ["extraordinarily"], ["well"]]


def test_ass_text_cannot_inject_style_codes(ecfg):
    ass = edit.build_ass("{\\fs300}BIG\nhook", [{"w": "{\\an7}hi", "s": 0, "e": 1}], 5,
                         "blur", ecfg, credit="{\\b1}@me")
    events = ass.split("[Events]")[1]
    assert "\\fs300" not in events and "\\an7" not in events and "\\b1" not in events
    assert "(/fs300)BIG hook" in events.replace("\\N", " ")


def test_ass_has_one_event_per_spoken_word_with_highlight(ecfg):
    ass = edit.build_ass("Hook", WORDS, 10, "blur", ecfg)
    caps = [line for line in ass.splitlines() if ",Cap," in line]
    assert len(caps) == len(WORDS)
    assert all("\\1c&H0000E6FF&" in c for c in caps)          # #FFE600 in ASS colour order
    assert "THAT" in caps[4] and "that," not in caps[4]        # upper case, punctuation dropped


def test_words_outside_the_clip_are_dropped(ecfg):
    words = [{"w": "before", "s": -3, "e": -2}, {"w": "inside", "s": 1, "e": 2},
             {"w": "after", "s": 30, "e": 31}]
    caps = [line for line in edit.build_ass("", words, 10, "crop", ecfg).splitlines()
            if ",Cap," in line]
    assert len(caps) == 1 and "INSIDE" in caps[0]


def test_censor():
    assert "nigg" not in edit.censor("what a nigga").lower()          # slurs: always
    assert edit.censor("oh shit") == "oh shit"                         # swearing: only on request
    assert edit.censor("oh shit", swears=True) == "oh s**t"
    assert edit.caption_token("classic,") == "CLASSIC"                 # no false positives


def test_layout_choice():
    cam = {"facecam": {"x": 0, "y": 0, "w": 10, "h": 10}, "layout": None}
    e = config.validate({})["edit"]
    assert edit.choose_layout(e, cam) == "split"
    assert edit.choose_layout(e, {"facecam": None, "layout": None}) == "blur"
    assert edit.choose_layout(e, {"facecam": None, "layout": "split"}) == "blur"
    assert edit.choose_layout(e, {"facecam": None, "layout": "crop"}) == "crop"
    assert edit.choose_layout(e, None) == "blur"


def test_text_stays_inside_the_safe_area():
    e = config.validate({})["edit"]
    for layout in ("split", "blur", "crop"):
        g = edit.geometry(layout, e)
        assert edit.SAFE_TOP <= g["hook_y"] and g["cap_y"] <= edit.SAFE_BOTTOM, layout


def test_facecam_box_is_scaled_to_the_real_frame():
    cam = {"x": 1440, "y": 810, "w": 480, "h": 270}
    assert edit.scaled_cam(cam, {"w": 1920, "h": 1080}, 1280, 720) == (320, 180, 960, 540)
    # a box that would poke outside is pulled back in
    cw, ch, cx, cy = edit.scaled_cam(cam, {"w": 1920, "h": 1080}, 640, 360)
    assert cx + cw <= 640 and cy + ch <= 360


def _streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_name,pix_fmt,sample_rate,channels", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return out.split()


@needs_ffmpeg
@pytest.mark.parametrize("name", ["split", "blur", "crop"])
def test_render_layouts(vod, tmp_path, ecfg, name):
    streamer = {"credit": "@example", "frame": {"w": 1920, "h": 1080}, "layout": name,
                "facecam": {"x": 0, "y": 0, "w": 480, "h": 270} if name == "split" else None}
    words = [dict(w, s=w["s"] + 44, e=w["e"] + 44) for w in WORDS]      # stream time
    out = edit.render(vod, 44, 52, tmp_path / f"{name}.mp4", ecfg, "He did NOT expect this",
                      words, streamer)
    info = media.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert abs(info["duration"] - 8) < 0.3 and abs(info["fps"] - 30) < 0.1
    assert info["has_audio"]
    streams = _streams(out)
    assert "h264,yuv420p" in streams and "aac,48000,2" in streams
    assert not (tmp_path / f"{name}.tmp.mp4").exists()
    assert ",Cap," in (tmp_path / f"{name}.ass").read_text()


@needs_ffmpeg
def test_render_uses_media_start_offset(vod, tmp_path, ecfg):
    """A clip cut from a section file: stream time 100-106 is 4-10 s into a file starting at 96."""
    section = tmp_path / "section.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-ss", "96", "-t", "20", "-i", str(vod),
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(section)])
    out = edit.render(section, 100, 106, tmp_path / "o.mp4", ecfg, media_start=96)
    assert abs(media.probe(out)["duration"] - 6) < 0.3


@needs_ffmpeg
def test_render_source_without_audio(tmp_path, ecfg):
    src = tmp_path / "silent.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
               "testsrc2=size=640x360:rate=30:duration=6", "-c:v", "libx264", "-preset",
               "ultrafast", str(src)])
    out = edit.render(src, 0, 6, tmp_path / "o.mp4", ecfg, "No sound")
    info = media.probe(out)
    assert info["has_video"] and not info["has_audio"]


@needs_ffmpeg
def test_render_vertical_source(tmp_path, ecfg):
    src = tmp_path / "tall.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
               "testsrc2=size=360x640:rate=30:duration=6", "-c:v", "libx264", "-preset",
               "ultrafast", str(src)])
    for layout in ("blur", "crop"):
        out = edit.render(src, 0, 6, tmp_path / f"{layout}.mp4", ecfg, "Tall",
                          streamer={"credit": "x", "facecam": None, "layout": layout,
                                    "frame": {"w": 1920, "h": 1080}})
        info = media.probe(out)
        assert (info["width"], info["height"]) == (1080, 1920)


@needs_ffmpeg
def test_render_rejects_bad_ranges(vod, tmp_path, ecfg):
    with pytest.raises(media.MediaError):
        edit.render(vod, 10, 10, tmp_path / "a.mp4", ecfg)
    with pytest.raises(media.MediaError):
        edit.render(vod, 5000, 5010, tmp_path / "b.mp4", ecfg)


@needs_ffmpeg
def test_envelope_and_probe(vod):
    info = media.probe(vod)
    assert abs(info["duration"] - 150) < 0.5 and (info["width"], info["height"]) == (640, 360)
    env = media.envelope(vod, 5)
    assert len(env) == 30
    assert env[10] > 5 * env[5] and env[22] > 5 * env[5]       # loud at 50 s and 110 s
    part = media.envelope(vod, 1, start=48, length=6)
    assert len(part) == 6 and part[3] > 5 * part[0]
