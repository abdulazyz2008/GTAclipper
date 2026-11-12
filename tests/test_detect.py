import numpy as np
import pytest
from conftest import MOMENTS, make_chat, near

from clipper import config, detect


@pytest.fixture
def dcfg():
    return config.validate({"detect": {"clip_len": 14, "max_len": 18, "pre_roll": 5,
                                       "min_gap": 30}})["detect"]


def quiet_audio(seconds, bucket=5, loud_at=()):
    rms = np.full(int(seconds // bucket), 300.0, dtype=np.float32)
    for t in loud_at:
        rms[int(t // bucket)] = 9000.0
    return rms


def test_finds_both_moments_from_chat_and_audio(dcfg):
    cands = detect.find_moments(make_chat(), quiet_audio(150, loud_at=MOMENTS), 150, dcfg)
    peaks = sorted(c["peak"] for c in cands)
    assert len(cands) == 2, cands
    assert near(peaks[0], 50, 10) and near(peaks[1], 110, 10)
    for c in cands:
        assert c["chat_score"] > 3 and c["accel_score"] > 3 and c["hype_score"] > 3
        assert 14 <= c["end"] - c["start"] <= 18
        assert any(" x" in s for s in c["chat_sample"])      # repeated messages are counted


def test_clip_starts_before_the_moment_not_at_the_chat_peak(dcfg):
    cands = detect.find_moments(make_chat(), quiet_audio(150, loud_at=MOMENTS), 150, dcfg)
    first = min(cands, key=lambda c: c["peak"])
    # the event is at 50 s; chat reacts from 56 s. The clip must open ~pre_roll before the event.
    assert 40 <= first["start"] <= 49, first


def test_one_spammer_is_not_a_moment(dcfg):
    cands = detect.find_moments(make_chat(moments=()), quiet_audio(150), 150, dcfg)
    assert cands == []


def test_one_person_counts_once_per_bucket(dcfg):
    rate, _ = detect.chat_series(
        [{"t": 10 + i * 0.01, "text": "spam", "user": "bot"} for i in range(50)], 10, 5, 0, dcfg)
    assert rate.max() == 1.0


def test_commands_and_known_bots_are_ignored(dcfg):
    chat = [{"t": 1.0, "text": "!discord", "user": "a"},
            {"t": 1.1, "text": "join the discord", "user": "Nightbot"},
            {"t": 1.2, "text": "hello", "user": "b"}]
    rate, _ = detect.chat_series(chat, 4, 5, 0, dcfg)
    assert rate.sum() == 1.0


def test_audio_only_when_there_is_no_chat(dcfg):
    cands = detect.find_moments([], quiet_audio(150, loud_at=MOMENTS), 150, dcfg)
    assert len(cands) == 2
    assert all(c["chat_score"] == 0 for c in cands)


def test_tiny_chat_wobble_in_a_dead_chat_is_not_a_moment(dcfg):
    chat = [{"t": 60 + i, "text": "lol", "user": f"u{i}"} for i in range(3)]
    assert detect.find_moments(chat, quiet_audio(150), 150, dcfg) == []


def test_scores_never_depend_on_the_future(dcfg):
    """A live stream and its VOD must agree: scoring the first 100 s alone gives
    the same numbers as scoring those 100 s inside the full 150 s."""
    chat = make_chat()
    audio = quiet_audio(150, loud_at=MOMENTS)
    full = detect.score_timeline(chat, audio, dcfg)["total"]
    part = detect.score_timeline([m for m in chat if m["t"] <= 100 + dcfg["chat_lag"]],
                                 audio[:20], dcfg)["total"]
    # smoothing looks two buckets ahead, so everything but the last two must match
    np.testing.assert_allclose(part[:18], full[:18], atol=1e-5)


def test_live_mode_waits_until_the_clip_is_fully_recorded(dcfg):
    chat = make_chat()
    audio = quiet_audio(150, loud_at=MOMENTS)
    # 60 s in, chat has only reacted to what happened before ~52 s
    early = detect.find_moments([m for m in chat if m["t"] <= 60], audio[:12], 60, dcfg,
                                min_score=3, relative_floor=False, ready_until=50)
    assert early == []                      # the 50 s moment is not finished yet
    later = detect.find_moments([m for m in chat if m["t"] <= 80], audio[:16], 80, dcfg,
                                min_score=3, relative_floor=False, ready_until=70)
    assert len(later) == 1 and near(later[0]["peak"], 50, 10)
    again = detect.find_moments(chat, audio, 150, dcfg, min_score=3, relative_floor=False,
                                ready_until=148, taken=[later[0]["peak"]])
    assert len(again) == 1 and near(again[0]["peak"], 110, 10)


@pytest.mark.parametrize("text, hype", [
    ("LMAOOO", True), ("KEKW", True), ("no way", True), ("clip it", True), ("W", True),
    ("???", True), ("WHAT WAS THAT", True), ("OMEGALUL OMEGALUL", True), ("what", True),
    ("bro what", True), ("no no no", True), ("that was insane lmao", True),
    ("what time is the stream tomorrow", False), ("i like this game", False),
    ("hello", False), ("following since 2019", False), ("that boss was crazy hard", False),
    ("no i think he went left", False), ("bro what game is this", False),
    ("I'll be back in five", False), ("we\u2019ll see", False),
])
def test_hype_detection(text, hype):
    assert detect.is_hype(text, detect.hype_pattern()) is hype


def test_custom_hype_words():
    p = detect.hype_pattern(["peepoClap", "a+b"])
    assert detect.is_hype("peepoClap", p) and detect.is_hype("a+b", p)
    assert not detect.is_hype("aab", p)


def test_empty_inputs():
    d = config.validate({})["detect"]
    assert detect.find_moments([], np.zeros(0, dtype=np.float32), 0, d) == []
    assert detect.find_moments([], np.full(3, 100.0, dtype=np.float32), 15, d) == []


def test_trailing_z_handles_flat_signal():
    z, scale = detect.trailing_z(np.full(40, 5.0), 20)
    assert not z.any() and np.isfinite(scale).all()
