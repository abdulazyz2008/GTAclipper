"""Step 2b - transcribe only the candidate windows (cheap) with word timings."""
import subprocess
import tempfile
from pathlib import Path

_model = None


def _load(name):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(name, device="auto", compute_type="int8")
    return _model


def transcribe_window(vod_path, start, end, cfg):
    """Return (text, [{"w","s","e"}]) with times relative to the clip start."""
    try:
        model = _load(cfg["model"])
    except Exception as e:
        print(f"  whisper unavailable ({type(e).__name__}); clips will have no captions")
        return "", []
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "seg.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(end - start),
             "-i", str(vod_path), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            check=True)
        segments, _ = model.transcribe(
            str(wav), language=cfg.get("language"), word_timestamps=True, vad_filter=True)
        words = []
        for seg in segments:
            for w in seg.words or []:
                tok = w.word.strip()
                if tok:
                    words.append({"w": tok, "s": round(w.start, 2), "e": round(w.end, 2)})
    return " ".join(w["w"] for w in words), words
