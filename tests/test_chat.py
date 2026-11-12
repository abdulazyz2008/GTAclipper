import json
import socket
import threading
import time

from clipper import chat


def test_normalise_own_format():
    out = chat.normalise([{"t": 5, "text": " hi ", "user": "a"}, {"t": 1.5, "text": "yo"}])
    assert out == [{"t": 1.5, "text": "yo", "user": ""}, {"t": 5.0, "text": "hi", "user": "a"}]


def test_normalise_chat_downloader_format():
    raw = [{"time_in_seconds": 12.5, "message": "LUL", "author": {"name": "bob"}},
           {"time_in_seconds": -3, "message": "before the stream"},
           {"time_in_seconds": 20, "message": ""}]
    assert chat.normalise(raw) == [{"t": 12.5, "text": "LUL", "user": "bob"}]


def test_normalise_twitchdownloader_format():
    raw = {"comments": [{"content_offset_seconds": 7, "message": {"body": "KEKW"},
                         "commenter": {"display_name": "Zed"}}]}
    assert chat.normalise(raw) == [{"t": 7.0, "text": "KEKW", "user": "Zed"}]


def test_load_chat_file_json_and_jsonl(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps([{"t": 1, "text": "x"}]), encoding="utf-8")
    b = tmp_path / "b.jsonl"
    b.write_text('{"t": 1, "text": "x", "user": "u"}\n{"t": 2, "text": "y", "user": "v"}\n')
    assert len(chat.load_chat_file(a)) == 1
    assert [m["text"] for m in chat.load_chat_file(b)] == ["x", "y"]


def test_twitch_channel_from_url():
    assert chat.twitch_channel("https://www.twitch.tv/SomeOne") == "someone"
    assert chat.twitch_channel("twitch.tv/some_one/") == "some_one"
    assert chat.twitch_channel("https://www.twitch.tv/videos/123456") is None
    assert chat.twitch_channel("https://www.youtube.com/watch?v=abc") is None
    assert chat.twitch_channel("https://kick.com/someone") is None


def test_parse_irc_privmsg_with_tags():
    line = ("@badge-info=;display-name=Cool\\sName;emotes= :coolname!coolname@coolname.tmi.twitch.tv "
            "PRIVMSG #chan :NO WAY :O that just happened")
    assert chat.parse_irc(line) == ("PRIVMSG", "Cool Name", "NO WAY :O that just happened")


def test_parse_irc_other_lines():
    assert chat.parse_irc("PING :tmi.twitch.tv") == ("PING", "", "tmi.twitch.tv")
    assert chat.parse_irc(":bob!bob@bob.tmi.twitch.tv PRIVMSG #c :\x01ACTION dances\x01") == \
        ("PRIVMSG", "bob", "dances")
    assert chat.parse_irc("")[0] == ""


def test_live_chat_reader_against_a_local_irc_server(tmp_path):
    """Full round trip: login, join, answer a PING, receive messages, log them."""
    got = {}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def server():
        conn, _ = listener.accept()
        conn.settimeout(5)
        buf = b""
        while b"JOIN #testchan" not in buf:
            buf += conn.recv(4096)
        got["login"] = buf.decode()
        conn.sendall(b":tmi.twitch.tv 366 justinfan1 #testchan :End of /NAMES list\r\n"
                     b"PING :tmi.twitch.tv\r\n")
        got["pong"] = conn.recv(4096).decode()
        conn.sendall(b"@display-name=Alice :alice!alice@alice.tmi.twitch.tv PRIVMSG #testchan :LMAO\r\n"
                     b":bob!bob@bob.tmi.twitch.tv PRIVMSG #testchan :clip ")
        time.sleep(0.1)                         # a message split across two packets
        conn.sendall(b"it\r\n")
        time.sleep(1.0)
        conn.close()

    threading.Thread(target=server, daemon=True).start()
    log = tmp_path / "chat.jsonl"
    reader = chat.TwitchChat("TestChan", log, host="127.0.0.1",
                             port=listener.getsockname()[1], tls=False)
    reader.start_clock()
    reader.start()
    deadline = time.time() + 5
    while len(reader.snapshot()) < 2 and time.time() < deadline:
        time.sleep(0.05)
    reader.stop()
    listener.close()
    msgs = reader.snapshot()
    assert [(m["user"], m["text"]) for m in msgs] == [("Alice", "LMAO"), ("bob", "clip it")]
    assert all(0 <= m["t"] < 5 for m in msgs)
    assert "NICK justinfan" in got["login"] and "PASS" not in got["login"]
    assert got["pong"].startswith("PONG")
    assert len(log.read_text().splitlines()) == 2


def test_messages_before_the_recording_starts_are_dropped():
    reader = chat.TwitchChat("x")
    reader._add("a", "too early")                # clock not started
    reader.start_clock(at=time.time() + 100)     # recording starts later
    reader._add("a", "still too early")
    assert reader.snapshot() == []
