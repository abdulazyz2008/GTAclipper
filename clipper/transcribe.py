"""Speech-to-text for the candidate windows only, with per-word timings.

Uses faster-whisper if it is installed (`pip install faster-whisper`). Without
it everything still works; clips just have no captions.
"""
import tempfile
from pathlib import Path

from . import media

_model = None
_model_key = None
_unavailable = ""


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load(cfg):
    global _model, _model_key
    key = (cfg["model"], cfg.get("device", "auto"))
    if _model is None or _model_key != key:
        from faster_whisper import WhisperModel
        _model = WhisperModel(cfg["model"], device=cfg.get("device", "auto"), compute_type="int8")
        _model_key = key
    return _model


def clean_words(words, length):
    """Keep words inside [0, length], in order, with sane timings."""
    out = []
    last = 0.0
    for w in words:
        tok = str(w.get("w", "")).strip()
        try:
            s, e = float(w["s"]), float(w["e"])
        except (KeyError, TypeError, ValueError):
            continue
        if not tok or e <= 0 or s >= length:
            continue
        s = max(s, last, 0.0)
        e = min(max(e, s + 0.05), length)
        if e <= s:
            continue
        out.append({"w": tok, "s": round(s, 2), "e": round(e, 2)})
        last = s
    return out


def transcribe_window(audio_path, start, end, cfg, offset=0.0, log=print):
    """Return (text, words). Word times are seconds from the clip start.

    `offset` is the stream time at which `audio_path` begins.
    """
    global _unavailable
    if _unavailable:
        return "", []
    try:
        model = _load(cfg)
    except Exception as e:
        _unavailable = f"{type(e).__name__}: {str(e)[:160]}"
        log(f"  captions are off: speech-to-text could not start ({_unavailable}).")
        log("  Install it with: pip install faster-whisper")
        return "", []
    length = end - start
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "seg.wav"
        media.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, start - offset):.3f}",
                   "-t", f"{length:.3f}", "-i", str(audio_path), "-vn", "-ac", "1",
                   "-ar", "16000", str(wav)])
        segments, _ = model.transcribe(str(wav), language=cfg.get("language"),
                                       word_timestamps=True, vad_filter=True)
        words = []
        for seg in segments:
            for w in seg.words or []:
                words.append({"w": w.word, "s": w.start, "e": w.end})
    words = clean_words(words, length)
    return " ".join(w["w"] for w in words), words
