"""Chat: loading saved chat logs, and reading live Twitch chat.

Every chat message in this program is a dict:  {"t": seconds, "text": str, "user": str}
where `t` is seconds from the start of the stream / recording.

Live Twitch chat is read over Twitch's public IRC gateway with an anonymous
read-only login, so no account, key or app registration is needed.
"""
import json
import random
import re
import socket
import ssl
import threading
import time
from pathlib import Path

# ---- saved chat logs -------------------------------------------------------


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalise(raw):
    """Accept the common chat-log shapes and return our message list.

    Understood: this program's own format, chat-downloader output, and
    TwitchDownloader's chat JSON ({"comments": [...]}).
    """
    if isinstance(raw, dict):
        raw = raw.get("comments") or raw.get("messages") or raw.get("chat") or []
    if not isinstance(raw, list):
        raise ValueError("chat file must be a list of messages")
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        t = _num(m.get("t"))
        if t is None:
            t = _num(m.get("time_in_seconds"))
        if t is None:
            t = _num(m.get("content_offset_seconds"))
        text = m.get("text")
        if text is None:
            msg = m.get("message")
            text = msg.get("body") if isinstance(msg, dict) else msg
        user = m.get("user")
        if user is None:
            who = m.get("author") or m.get("commenter") or {}
            if isinstance(who, dict):
                user = who.get("name") or who.get("display_name") or who.get("login")
        if t is None or t < 0 or not isinstance(text, str) or not text.strip():
            continue
        out.append({"t": float(t), "text": text.strip(), "user": str(user or "")})
    out.sort(key=lambda m: m["t"])
    return out


def load_chat_file(path):
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    try:
        raw = json.loads(text)
    except ValueError:
        # JSON lines (one message per line)
        raw = [json.loads(line) for line in text.splitlines() if line.strip()]
    return normalise(raw)


# ---- live Twitch chat ------------------------------------------------------

_TWITCH_URL = re.compile(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{2,30})/?(?:\?.*)?$")


def twitch_channel(source):
    """Channel login from a twitch.tv URL (or a bare name). None for VOD / other URLs."""
    m = _TWITCH_URL.match(source.strip())
    if m and m.group(1).lower() not in ("videos", "directory", "settings"):
        return m.group(1).lower()
    if re.fullmatch(r"[A-Za-z0-9_]{2,30}", source.strip()):
        return source.strip().lower()
    return None


_UNESCAPE = {"\\s": " ", "\\:": ";", "\\\\": "\\", "\\r": "\r", "\\n": "\n"}


def parse_irc(line):
    """Parse one IRC line. Returns (command, user, text) - user/text may be ''."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = re.sub(r"\\[s:\\rn]", lambda m: _UNESCAPE[m.group(0)], v)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, sep, trailing = line.partition(" :")
    parts = head.split()
    if not parts:
        return "", "", ""
    user = tags.get("display-name") or prefix.partition("!")[0]
    text = trailing if sep else ""
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]                    # /me messages
    return parts[0].upper(), user, text


class TwitchChat(threading.Thread):
    """Background reader for one channel's live chat.

    Messages are stamped with seconds since `clock_start` (a time.time() value,
    set with `start_clock()` when the recording begins) and appended to
    `self.messages`. Reconnects by itself with a growing delay.
    """

    def __init__(self, channel, log_path=None, host="irc.chat.twitch.tv", port=6697, tls=True):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel = channel.lower().lstrip("#")
        self.host, self.port, self.tls = host, port, tls
        self.log_path = Path(log_path) if log_path else None
        self.messages = []
        self.lock = threading.Lock()
        self.clock_start = None
        self.offset = 0.0
        self.connected = threading.Event()
        self._stop = threading.Event()
        self.error = ""

    def start_clock(self, at=None, offset=0.0):
        self.clock_start = time.time() if at is None else at
        self.offset = offset

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self.lock:
            return list(self.messages)

    def _add(self, user, text):
        if self.clock_start is None:
            return
        m = {"t": round(time.time() - self.clock_start - self.offset, 2), "text": text, "user": user}
        if m["t"] < 0:
            return
        with self.lock:
            self.messages.append(m)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    def _session(self):
        raw = socket.create_connection((self.host, self.port), timeout=20)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) \
            if self.tls else raw
        try:
            sock.settimeout(1.0)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"CAP REQ :twitch.tv/tags\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n"
                         .encode())
            buf = b""
            last = time.time()
            while not self._stop.is_set():
                try:
                    data = sock.recv(8192)
                except (socket.timeout, ssl.SSLWantReadError):
                    if time.time() - last > 360:      # Twitch pings every ~5 minutes
                        raise ConnectionError("chat went silent") from None
                    continue
                if not data:
                    raise ConnectionError("chat connection closed")
                last = time.time()
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for b in lines:
                    cmd, user, text = parse_irc(b.decode("utf-8", "replace"))
                    if cmd == "PING":
                        sock.sendall(f"PONG :{text or 'tmi.twitch.tv'}\r\n".encode())
                    elif cmd == "PRIVMSG" and text:
                        self._add(user, text)
                    elif cmd in ("366", "ROOMSTATE"):
                        self.connected.set()
                    elif cmd == "RECONNECT":
                        raise ConnectionError("server asked for a reconnect")
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        delay = 2.0
        while not self._stop.is_set():
            started = time.time()
            try:
                self._session()
            except (OSError, ConnectionError) as e:
                self.error = f"{type(e).__name__}: {e}"
            self.connected.clear()
            if time.time() - started > 60:
                delay = 2.0
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 60.0)


class ReplayChat:
    """Stand-in for TwitchChat that plays back a saved chat log (tests, simulation)."""

    def __init__(self, messages):
        self._all = sorted(messages, key=lambda m: m["t"])
        self.error = ""

    def start(self):
        pass

    def start_clock(self, at=None, offset=0.0):
        pass

    def stop(self):
        pass

    def snapshot(self, until=None):
        if until is None:
            return list(self._all)
        return [m for m in self._all if m["t"] <= until]
