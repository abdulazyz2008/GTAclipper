"""Long-running work as background jobs.

Scanning a VOD, watching a stream or running the autopilot can take hours, far
longer than one request from Claude may last. A job is simply another
`clipper` command started in its own process, with its output going to a log
file. Jobs keep running if the program that started them goes away.

Everything is plain files in data/jobs/, so it works the same on Windows,
macOS and Linux:

  <id>.json   what was started
  <id>.log    everything the command printed
  <id>.alive  touched every few seconds while the job runs
  <id>.stop   created to ask the job to finish up and stop
  <id>.exit   the exit code, written when the job ends
"""
import _thread
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import ensure_dirs, home

BEAT = 3.0          # seconds between heartbeats


def _folder():
    return ensure_dirs()["jobs"]


def start(args, name=""):
    """Start `clipper <args>` in the background. Returns the job record."""
    job_id = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    folder = _folder()
    meta = {"id": job_id, "name": name or (args[0] if args else "job"), "args": list(args),
            "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    (folder / f"{job_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    log = open(folder / f"{job_id}.log", "wb")
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
              | getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" \
        else {"start_new_session": True}
    env = dict(os.environ, CLIPPER_HOME=str(home()), PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    try:
        subprocess.Popen([sys.executable, "-m", "clipper.jobs", job_id, *map(str, args)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         env=env, cwd=str(home()), **kwargs)
    finally:
        log.close()
    (folder / f"{job_id}.alive").touch()
    return status(job_id)


def status(job_id, tail=15):
    """State of one job: running / finished / failed / stopped, plus the end of its log."""
    folder = _folder()
    meta_file = folder / f"{job_id}.json"
    if not meta_file.exists():
        return None
    info = json.loads(meta_file.read_text(encoding="utf-8"))
    exit_file = folder / f"{job_id}.exit"
    alive = folder / f"{job_id}.alive"
    if exit_file.exists():
        code = exit_file.read_text().strip()
        info["state"] = "finished" if code == "0" else ("stopped" if code == "130" else "failed")
    elif alive.exists() and time.time() - alive.stat().st_mtime < BEAT * 5:
        info["state"] = "stopping" if (folder / f"{job_id}.stop").exists() else "running"
    else:
        info["state"] = "failed"        # no heartbeat and no exit code: it died
    info["log"] = log_tail(job_id, tail)
    return info


def log_tail(job_id, lines=15):
    try:
        text = (_folder() / f"{job_id}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def list_jobs(limit=10):
    ids = sorted((p.stem for p in _folder().glob("*.json")), reverse=True)[:limit]
    return [s for s in (status(i, tail=3) for i in ids) if s]


def running(name):
    """The newest running job with this name, or None."""
    for j in list_jobs(limit=30):
        if j["name"] == name and j["state"] == "running":
            return j
    return None


def stop(job_id):
    """Ask a job to finish up and stop. Returns its status (it may take a moment)."""
    if status(job_id) is None:
        return None
    (_folder() / f"{job_id}.stop").touch()
    return status(job_id)


def wait(job_id, timeout=60.0):
    """Block until the job ends or `timeout` passes. Returns its status."""
    end = time.time() + timeout
    while time.time() < end:
        s = status(job_id)
        if s is None or s["state"] not in ("running", "stopping"):
            return s
        time.sleep(0.3)
    return status(job_id)


# ---- the job process itself ------------------------------------------------


def _heartbeat(folder, job_id, done):
    alive, stop_file = folder / f"{job_id}.alive", folder / f"{job_id}.stop"
    asked = False
    while not done.wait(1.0):
        try:
            alive.touch()
        except OSError:
            pass
        if not asked and stop_file.exists():
            asked = True
            _thread.interrupt_main()        # same as pressing Ctrl+C in that command


def _main(argv):
    job_id, args = argv[0], argv[1:]
    folder = Path(_folder())
    done = threading.Event()
    threading.Thread(target=_heartbeat, args=(folder, job_id, done), daemon=True).start()
    code = 1
    try:
        from .cli import main
        main(args)
        code = 0
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
            code = 1
        else:
            code = e.code or 0
    except KeyboardInterrupt:
        code = 130
    except BaseException as e:      # the log must show why a job died
        print(f"error: {type(e).__name__}: {e}")
        code = 1
    finally:
        done.set()
        sys.stdout.flush()
        (folder / f"{job_id}.exit").write_text(str(code))
    return code


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
