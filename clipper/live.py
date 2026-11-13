"""Watch a live stream and cut clips while it is still running.

  recorder   the stream is saved in short pieces (a rolling window on disk)
  chat       live chat is read alongside and stamped with stream time
  detector   every few seconds the same scoring as for VODs runs on what has
             been recorded so far; a moment is cut as soon as its whole clip
             is on disk, then transcribed, ranked, rendered and queued for review

A local video file can be played through the same path with `simulate=True`,
which is how this is tested without a real stream.
"""
import csv
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import chat as chatmod
from . import detect, media, pipeline, rank, store
from .config import ensure_dirs, safe_name, streamer_cfg

HOP = 0.25      # resolution of the stored loudness curve, seconds
# several streams can be watched at once; transcribing and rendering take turns
WORK_LOCK = threading.Lock()


class LiveError(Exception):
    pass


def source_command(source, cfg, simulate=False, speed=1.0):
    """Command that writes the stream to stdout as MPEG-TS."""
    if simulate:
        rate = ["-re"] if speed == 1.0 else ["-readrate", f"{speed:g}"]
        return ["ffmpeg", "-v", "error", "-nostdin", *rate, "-i", str(source),
                "-c", "copy", "-f", "mpegts", "-"]
    quality = cfg["live"]["quality"]
    max_h = cfg["ingest"]["max_height"]
    if quality == "best" and max_h < 1080:
        steps = [h for h in (936, 900, 720, 540, 480, 360, 160) if h <= max_h]
        quality = ",".join(f"{h}p60,{h}p" for h in steps) + ",worst"
    try:
        import streamlink  # noqa: F401
        return [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error",
                str(source), quality]
    except ImportError:
        return [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings",
                "-f", f"b[height<={max_h}]/b", "-o", "-", str(source)]


class Recorder:
    """Runs the source command and an ffmpeg that cuts its output into pieces."""

    def __init__(self, cmd, folder, segment_sec):
        self.cmd = cmd
        self.folder = Path(folder)
        self.segment_sec = segment_sec
        self.list_path = self.folder / "segments.csv"
        self.started_at = None          # wall time of the first bytes of video
        self.bytes = 0
        self.src = self.seg = None
        self._pump = None
        self._origin = None

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.err_path = self.folder / "source.log"
        self._err = self.err_path.open("wb")
        try:
            self.src = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=self._err)
        except FileNotFoundError:
            raise LiveError(f"could not start {self.cmd[0]}") from None
        self.seg = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
             "-f", "segment", "-segment_time", f"{self.segment_sec:g}",
             "-segment_format", "mpegts", "-segment_list", self.list_path.name,
             "-segment_list_type", "csv", "-segment_list_flags", "+live",
             "seg_%05d.ts"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._err, cwd=self.folder)
        self._pump = threading.Thread(target=self._copy, daemon=True, name="recorder")
        self._pump.start()

    def _copy(self):
        try:
            while True:
                buf = self.src.stdout.read(65536)
                if not buf:
                    break
                if self.started_at is None:
                    self.started_at = time.time()
                self.bytes += len(buf)
                self.seg.stdin.write(buf)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.seg.stdin.close()
            except OSError:
                pass

    def alive(self):
        return self._pump is not None and self._pump.is_alive()

    def finish(self, timeout=30):
        """Wait for the last piece to be written after the source has ended."""
        if self.seg:
            try:
                self.seg.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.seg.kill()

    def stop(self):
        for p in (self.src, self.seg):
            if p and p.poll() is None:
                p.terminate()
        self.finish(timeout=15)
        if self.src and self.src.poll() is None:
            self.src.kill()
        if getattr(self, "_err", None):
            self._err.close()

    def error_text(self):
        try:
            lines = self.err_path.read_text("utf-8", "replace").strip().splitlines()
        except OSError:
            return ""
        return " | ".join(lines[-3:])[:400]

    def segments(self):
        """Finished pieces as (path, start, end) in stream time (first piece starts at 0)."""
        out = []
        try:
            with self.list_path.open(newline="") as f:
                rows = list(csv.reader(f))
        except OSError:
            return out
        for r in rows:
            if len(r) < 3:
                continue
            try:
                s, e = float(r[1]), float(r[2])
            except ValueError:
                continue
            if self._origin is None:
                self._origin = s
            out.append((self.folder / r[0], s - self._origin, e - self._origin))
        return out


class Watcher:
    def __init__(self, con, source, streamer, cfg, chat_source=None, simulate=False,
                 speed=1.0, use_whisper=True, use_llm=True, max_minutes=None, log=print):
        self.con, self.source, self.streamer, self.cfg = con, source, streamer, cfg
        streamer_cfg(cfg, streamer)
        self.simulate, self.speed = simulate, speed
        self.use_whisper, self.use_llm = use_whisper, use_llm
        self.max_minutes = max_minutes
        self.log = log
        self.session = safe_name(f"{streamer}_{time.strftime('%Y%m%d_%H%M%S')}")
        self.folder = ensure_dirs()["live"] / self.session
        self.chat = chat_source
        self.rec = None
        self.energy = []            # (stream_time, mean square) per HOP
        self.done_segments = 0
        self.taken = []             # peaks already clipped
        self.made = []              # (stream_time, clip_id)
        self.stop_flag = threading.Event()

    # -- bookkeeping ---------------------------------------------------------

    def _absorb(self, segs):
        """Add the loudness of newly finished pieces to the running curve."""
        for path, s, e in segs[self.done_segments:]:
            try:
                env = media.envelope(path, HOP)
            except media.MediaError:
                env = np.zeros(0, dtype=np.float32)
            for k, v in enumerate(env):
                t = s + k * HOP
                if t < e + HOP:
                    self.energy.append((t, float(v) * float(v)))
        self.done_segments = len(segs)

    def _rms(self, total, bucket):
        nb = int(total // bucket)
        acc = np.zeros(nb, dtype=np.float64)
        cnt = np.zeros(nb, dtype=np.float64)
        for t, v in self.energy:
            i = int(t // bucket)
            if 0 <= i < nb:
                acc[i] += v
                cnt[i] += 1
        return np.sqrt(acc / np.maximum(cnt, 1)).astype(np.float32)

    def _chunk(self, segs, start, end):
        """Join the pieces covering [start, end] into one file. Returns (path, begin, finish)."""
        use = [x for x in segs if x[2] > start - 4 and x[1] < end + 4 and x[0].exists()]
        if not use or use[0][1] > start + 0.05 or use[-1][2] < end - 0.5:
            return None
        out = self.folder / f"moment_{int(start * 10):07d}.ts"
        with out.open("wb") as dst:
            for path, _, _ in use:
                with path.open("rb") as src:
                    shutil.copyfileobj(src, dst, 1 << 20)
        return out, use[0][1], use[-1][2]

    def _prune(self, segs, total):
        keep_from = total - self.cfg["live"]["keep_min"] * 60
        for path, _, e in segs:
            if e < keep_from and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        self.energy = [x for x in self.energy
                       if x[0] >= total - max(self.cfg["live"]["keep_min"],
                                              self.cfg["detect"]["baseline_min"] + 5) * 60]

    def _status(self, total, state):
        info = {"session": self.session, "streamer": self.streamer, "source": str(self.source),
                "state": state, "recorded_min": round(total / 60, 1),
                "chat_messages": len(self._chat(None)), "clips": [c for _, c in self.made],
                "updated": store.now(), "chat_error": getattr(self.chat, "error", "")}
        (self.folder / "status.json").write_text(json.dumps(info, indent=1), encoding="utf-8")

    def _chat(self, until):
        if self.chat is None:
            return []
        if isinstance(self.chat, chatmod.ReplayChat):
            return self.chat.snapshot(until)
        msgs = self.chat.snapshot()
        return msgs if until is None else [m for m in msgs if m["t"] <= until]

    # -- the work ------------------------------------------------------------

    def _cut(self, cand, segs, chat):
        chunk = self._chunk(segs, cand["start"], cand["end"])
        if not chunk:
            self.log(f"  moment at {cand['peak']:.0f}s skipped: its footage is no longer on disk")
            return None
        path, begin, finish = chunk
        meta = {"vod_id": self.session, "source": str(self.source), "streamer": self.streamer,
                "audio_path": path, "audio_offset": begin, "vod_path": path,
                "media_start": begin, "lo": begin, "hi": finish}
        with WORK_LOCK:
            clip_id = pipeline.add_candidate(self.con, cand, meta, self.cfg, self.use_whisper,
                                             self.log)
            pipeline.judge(self.con, [clip_id], self.streamer, self.cfg,
                           use_llm=self.use_llm and rank.api_configured(), log=lambda *_: None)
            row = pipeline.render_clip(self.con, clip_id, self.cfg, self.log)
        self.log(f"  clip #{clip_id} ready for review: {row['start']:.0f}-{row['end']:.0f}s "
                 f"score {row['score']:.1f} (chat {row['chat_score']:.1f}, "
                 f"accel {row['accel_score']:.1f}, audio {row['audio_score']:.1f})")
        return clip_id

    def step(self, final=False):
        segs = self.rec.segments()
        if not segs:
            return 0.0
        self._absorb(segs)
        total = segs[-1][2]
        d, lv = self.cfg["detect"], self.cfg["live"]
        chat = self._chat(total + d["chat_lag"])
        rms = self._rms(total, d["bucket_sec"])
        # chat about second t only arrives around t + chat_lag: a moment is ready
        # once its whole clip is recorded *and* its chat reaction has come in
        ready = total if final else total - 2.0 - (d["chat_lag"] if chat else 0.0)
        cands = detect.find_moments(chat, rms, total, d, min_score=lv["min_score"], top_n=5,
                                    relative_floor=False, ready_until=ready, taken=self.taken)
        for cand in sorted(cands, key=lambda c: c["peak"]):
            recent = [t for t, _ in self.made if t > cand["peak"] - 3600]
            self.taken.append(cand["peak"])
            if len(recent) >= lv["max_per_hour"]:
                self.log(f"  moment at {cand['peak']:.0f}s skipped: hourly clip limit reached")
                continue
            try:
                clip_id = self._cut(cand, segs, chat)
            except (pipeline.PipelineError, media.MediaError) as e:
                self.log(f"  moment at {cand['peak']:.0f}s failed: {e}")
                continue
            if clip_id:
                self.made.append((cand["peak"], clip_id))
        self._prune(segs, total)
        self._status(total, "finished" if final else "recording")
        return total

    def run(self):
        """Record until the stream ends, `max_minutes` passes, or stop() is called.
        Returns the ids of the clips made."""
        media.require_tools()
        lv = self.cfg["live"]
        cmd = source_command(self.source, self.cfg, self.simulate, self.speed)
        self.rec = Recorder(cmd, self.folder, lv["segment_sec"])
        self.rec.start()
        if self.chat is None and not self.simulate:
            channel = chatmod.twitch_channel(str(self.source))
            if channel:
                self.chat = chatmod.TwitchChat(channel, self.folder / "chat.jsonl")
            else:
                self.log("  no live chat reader for this site; detecting from audio only")
        if self.chat is not None:
            self.chat.start()
        try:
            t0 = time.time()
            while self.rec.started_at is None:
                if not self.rec.alive():
                    raise LiveError("the stream is not live or could not be opened"
                                    + (f" ({self.rec.error_text()})" if self.rec.error_text() else ""))
                if time.time() - t0 > 90:
                    raise LiveError("no video arrived within 90 seconds")
                time.sleep(0.5)
            if self.chat is not None:
                self.chat.start_clock(self.rec.started_at, lv["chat_offset"])
            self.log(f"recording {self.source} -> {self.folder}")
            limit = self.max_minutes * 60 if self.max_minutes else None
            last = 0.0
            try:
                while self.rec.alive() and not self.stop_flag.is_set():
                    self.stop_flag.wait(lv["poll_sec"])
                    total = self.step()
                    if total - last >= 300:
                        last = total
                        self.log(f"  {total / 60:.0f} min recorded, "
                                 f"{len(self.made)} clip(s) so far")
                    if limit and total >= limit:
                        self.log("  time limit reached")
                        break
            except KeyboardInterrupt:       # Ctrl+C: stop recording, keep what we have
                self.log("  stopping - finishing the clips already recorded")
        finally:
            self.rec.stop()
            if self.chat is not None:
                self.chat.stop()
        self.step(final=True)
        for path, _, _ in self.rec.segments():      # the clips keep their own footage
            try:
                path.unlink()
            except OSError:
                pass
        self.log(f"stream finished: {len(self.made)} clip(s) waiting in review")
        return [c for _, c in self.made]

    def stop(self):
        self.stop_flag.set()


def wait_and_watch(con, source, streamer, cfg, wait_minutes=0, log=print, **kw):
    """Run a Watcher; if the stream is offline, retry every minute for `wait_minutes`."""
    deadline = time.time() + wait_minutes * 60
    while True:
        w = Watcher(con, source, streamer, cfg, log=log, **kw)
        try:
            return w.run()
        except LiveError as e:
            shutil.rmtree(w.folder, ignore_errors=True)
            if time.time() >= deadline:
                raise
            log(f"  {e}; trying again in 60s")
            time.sleep(60)


def latest_status():
    """The most recent watcher's status.json as a dict, or None."""
    folder = ensure_dirs()["live"]
    files = sorted(folder.glob("*/status.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except ValueError:
        return None
