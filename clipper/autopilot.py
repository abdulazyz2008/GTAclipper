"""Autopilot: keep an eye on every streamer in config.yaml at once.

For each streamer that has a `channel`, the autopilot checks whether they are
live. When they are, it records the stream and cuts clips as moments happen
(the same watcher as `clipper watch`); when the stream ends it goes back to
checking. Several streamers can be live at the same time.

Clips land in the review queue. Nothing is posted from here - a person still
approves every clip.
"""
import json
import shutil
import threading
import time

from . import live, store
from .config import ConfigError, ensure_dirs


def status_path():
    return ensure_dirs()["data"] / "autopilot.json"


def read_status(max_age=120):
    """The autopilot's last report, or None if it is not running."""
    p = status_path()
    try:
        if time.time() - p.stat().st_mtime > max_age:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Autopilot:
    def __init__(self, cfg, streamers=None, use_whisper=True, use_llm=True, max_minutes=None,
                 once=False, log=print, sources=None, chats=None, simulate=False, speed=1.0):
        self.cfg = cfg
        names = list(streamers) if streamers else list(cfg["streamers"])
        unknown = [n for n in names if n not in cfg["streamers"]]
        if unknown:
            raise ConfigError(f"not in config.yaml: {', '.join(unknown)}")
        self.sources = dict(sources or {})          # tests / simulation: name -> file
        for n in names:
            self.sources.setdefault(n, cfg["streamers"][n]["channel"])
        skipped = [n for n in names if not self.sources[n]]
        self.names = [n for n in names if self.sources[n]]
        if not self.names:
            raise ConfigError("no streamer has a 'channel' in config.yaml - add one, e.g. "
                              "channel: \"https://www.twitch.tv/name\"")
        self.skipped = skipped
        self.chats = chats or {}
        self.simulate, self.speed = simulate, speed
        self.use_whisper, self.use_llm = use_whisper, use_llm
        self.max_minutes, self.once = max_minutes, once
        self.log = log
        self.stop_flag = threading.Event()
        self.state = {n: {"state": "starting", "clips": [], "streams": 0, "error": ""}
                      for n in self.names}
        self.watchers = {}
        self.lock = threading.Lock()

    def _say(self, name, text):
        with self.lock:
            self.log(f"[{name}] {text}")

    def _loop(self, name):
        con = store.connect()
        st = self.state[name]
        try:
            while not self.stop_flag.is_set():
                w = live.Watcher(con, self.sources[name], name, self.cfg,
                                 chat_source=self.chats.get(name), simulate=self.simulate,
                                 speed=self.speed, use_whisper=self.use_whisper,
                                 use_llm=self.use_llm, log=lambda t, n=name: self._say(n, t))
                self.watchers[name] = w
                st["state"] = "checking"
                try:
                    ids = w.run()
                    st["clips"] += ids
                    st["streams"] += 1
                    st["error"] = ""
                except live.LiveError:
                    shutil.rmtree(w.folder, ignore_errors=True)     # offline: nothing recorded
                except Exception as e:      # one streamer's trouble must not stop the others
                    st["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                    self._say(name, f"problem: {st['error']}")
                finally:
                    self.watchers.pop(name, None)
                st["state"] = "offline"
                if self.once:
                    break
                self.stop_flag.wait(self.cfg["autopilot"]["check_min"] * 60)
        finally:
            st["state"] = "stopped"
            con.close()

    def _report(self):
        for name, w in list(self.watchers.items()):
            if w.rec is not None and w.rec.started_at is not None:
                self.state[name]["state"] = "recording"
                self.state[name]["live_clips"] = [c for _, c in w.made]
        info = {"updated": store.now(), "streamers": self.state, "skipped_no_channel": self.skipped}
        try:
            tmp = status_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(info, indent=1), encoding="utf-8")
            tmp.replace(status_path())
        except OSError:
            pass

    def run(self):
        """Run until stopped (Ctrl+C), `max_minutes` passes, or - with `once` -
        every streamer has been checked once. Returns all clip ids made."""
        if self.skipped:
            self.log(f"skipping (no channel set): {', '.join(self.skipped)}")
        self.log(f"autopilot watching: {', '.join(self.names)}")
        threads = [threading.Thread(target=self._loop, args=(n,), name=f"auto-{n}", daemon=True)
                   for n in self.names]
        for t in threads:
            t.start()
        deadline = time.time() + self.max_minutes * 60 if self.max_minutes else None
        try:
            while any(t.is_alive() for t in threads):
                self._report()
                if deadline and time.time() >= deadline:
                    self.log("time limit reached")
                    break
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.log("stopping - finishing the clips already recorded")
        self.stop()
        for t in threads:
            t.join(timeout=600)
        self._report()
        ids = sorted({c for s in self.state.values() for c in s["clips"]})
        self.log(f"autopilot stopped: {len(ids)} clip(s) made, waiting in review")
        return ids

    def stop(self):
        self.stop_flag.set()
        for w in list(self.watchers.values()):
            w.stop()
