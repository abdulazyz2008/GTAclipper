import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import needs_ffmpeg

from clipper import cli, rank, store

ROOT = Path(__file__).resolve().parent.parent


def cand(**kw):
    return dict({"start": 60.0, "end": 95.0, "score": 4.0, "chat_score": 5.0, "accel_score": 3.0,
                 "audio_score": 2.0, "transcript": "no way", "chat_sample": ["LMAO x9"]}, **kw)


def test_check_judgement_accepts_and_cleans():
    fields, problems = rank.check_judgement(
        {"score": "87", "hook": "  He  did it ", "title": "A title", "reason": "why"})
    assert problems == []
    assert fields == {"llm_score": 87.0, "hook": "He did it", "title": "A title", "reason": "why"}


@pytest.mark.parametrize("d, fragment", [
    ({"score": 101, "title": "t"}, "score"),
    ({"score": None, "title": "t"}, "score"),
    ({"score": 5, "title": ""}, "title is empty"),
    ({"score": 5, "title": "t" * 96}, "at most 95"),
    ({"score": 5, "title": "THIS TITLE IS SHOUTING"}, "ALL CAPS"),
    ({"score": 5, "title": "a > b"}, "< or >"),
    ({"score": 5, "title": "t", "hook": "x" * 61}, "hook"),
    ({"score": 5, "title": "t", "hook": "a b c d e f g h i"}, "hook"),
])
def test_check_judgement_rejects(d, fragment):
    assert any(fragment in p for p in rank.check_judgement(d)[1])


def test_heuristic_ranking_scales_to_100_and_keeps_existing_verdicts():
    cands = [cand(score=8.0), cand(score=2.0), cand(score=4.0, judge="api", llm_score=77, title="Kept")]
    rank.heuristic(cands, "ex", "GTA 6")
    assert [c["llm_score"] for c in cands] == [100.0, 25.0, 77]
    assert cands[0]["title"] == "ex GTA 6 moment" and cands[2]["title"] == "Kept"


def test_rank_without_credentials_uses_detector_score(workspace):
    logs = []
    out = rank.rank([cand()], "ex", "GTA 6", {"model": "m"}, use_llm=True, log=logs.append)
    assert out[0]["judge"] == "heuristic" and "no Claude API key" in logs[0]


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns canned replies and records requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(clips, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text=json.dumps({"clips": clips}))])


def test_rank_api_applies_valid_answers_and_guards_bad_ones():
    pytest.importorskip("anthropic")
    cands = [cand(), cand(), cand()]
    client = FakeClient([reply([
        {"id": 0, "score": 91, "hook": "He did not see it", "title": "Worst timing", "reason": "r"},
        {"id": 1, "score": 40, "hook": "one two three four five six seven eight nine",
         "title": "x" * 200, "reason": "r"},
        {"id": 7, "score": 99, "hook": "", "title": "not one of ours", "reason": ""},
    ])])
    n = rank.rank_api(cands, "ex", "GTA 6", {"model": "claude-haiku-5-5"}, client=client,
                      log=lambda *_: None)
    assert n == 2
    assert (cands[0]["llm_score"], cands[0]["hook"], cands[0]["judge"]) == (91, "He did not see it", "api")
    assert cands[1]["hook"] == "" and cands[1]["title"] == "ex GTA 6 moment"   # never cut mid-word
    assert "judge" not in cands[2]
    req = client.requests[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in req and "thinking" not in req
    assert "no way" in req["messages"][0]["content"]


def test_rank_api_survives_refusals_and_bad_json():
    pytest.importorskip("anthropic")
    cands = [cand() for _ in range(12)]
    client = FakeClient([
        reply([], stop="refusal"),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="oops")]),
    ])
    logs = []
    assert rank.rank_api(cands, "ex", "GTA 6", {"model": "m"}, client=client, log=logs.append) == 0
    assert len(client.requests) == 2 and len(logs) == 2
    rank.heuristic(cands, "ex", "GTA 6")
    assert all(c["judge"] == "heuristic" for c in cands)


# ---- command line ----------------------------------------------------------


def run_cli(*args):
    try:
        cli.main(list(args))
    except SystemExit as e:
        return e.code
    return 0


def test_init_creates_config_and_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path))
    assert run_cli("init") == 0
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".claude" / "skills" / "stream-clipper" / "SKILL.md").exists()
    assert run_cli("init") == 0
    assert "already set up" in capsys.readouterr().out


def test_doctor_quick(workspace, capsys):
    code = run_cli("doctor", "--quick")
    out = capsys.readouterr().out
    assert "streamers: example, nocam" in out
    assert code in (0, 1) and ("all required parts are working" in out) == (code == 0)


def test_errors_are_one_clean_line(workspace, capsys):
    code = run_cli("run", "missing.mp4", "--streamer", "nobody")
    assert isinstance(code, str) and code.startswith("error: streamer 'nobody'")
    code = run_cli("render", "42")
    assert code == "error: no clip with id 42"
    (workspace / "config.yaml").write_text("detect: {clip_len: 9999}\n")
    assert "between 10 and 180" in run_cli("render")


def test_judge_command(workspace, capsys, tmp_path):
    con = store.connect()
    cid = store.insert_clip(con, {"vod_id": "v", "streamer": "example", "start": 1, "end": 30})
    f = tmp_path / "j.json"
    f.write_text(json.dumps([{"id": cid, "score": 300, "hook": "", "title": "t"}]))
    assert run_cli("judge", str(f)) == 1
    assert "nothing was changed" in capsys.readouterr().out
    f.write_text(json.dumps([{"id": cid, "score": 70, "hook": "Hook", "title": "Title"}]))
    assert run_cli("judge", str(f)) == 0
    assert store.get_clip(con, cid)["judge"] == "claude"
    f.write_text("{not json")
    assert "not valid JSON" in run_cli("judge", str(f))
    con.close()


def test_candidates_json_and_approve(workspace, capsys):
    con = store.connect()
    cid = store.insert_clip(con, {"vod_id": "v", "streamer": "example", "start": 3725, "end": 3760,
                                  "transcript": "hello", "chat_sample": ["W x3"]})
    capsys.readouterr()
    assert run_cli("candidates", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["id"] == cid and data[0]["at"] == "1h02m05s" and data[0]["chat_said"] == ["W x3"]
    assert run_cli("approve", str(cid)) == 0                 # not rendered: must refuse
    assert "no rendered video" in capsys.readouterr().out
    assert store.get_clip(con, cid)["status"] == "candidate"
    assert run_cli("reject", str(cid)) == 0
    assert store.get_clip(con, cid)["status"] == "rejected"
    capsys.readouterr()
    assert run_cli("status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["clips"]["rejected"] == 1
    con.close()


@needs_ffmpeg
def test_run_command_end_to_end(vod, workspace, capsys):
    assert run_cli("run", str(vod), "--streamer", "example", "--no-whisper", "--no-llm",
                   "--top", "1") == 0
    out = capsys.readouterr().out
    assert "2 candidate moments" in out and "1 clip(s) waiting for review" in out
    assert run_cli("list") == 0
    assert "rendered" in capsys.readouterr().out
    assert run_cli("publish", "--dry-run") == 0
    assert "no approved clips" in capsys.readouterr().out


# ---- the skill -------------------------------------------------------------


def test_skill_files_are_in_sync_and_well_formed():
    packaged = (ROOT / "clipper" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    project = (ROOT / ".claude" / "skills" / "stream-clipper" / "SKILL.md").read_text(encoding="utf-8")
    assert packaged == project, "run: cp clipper/skill/SKILL.md .claude/skills/stream-clipper/"
    assert packaged.startswith("---\n")
    import yaml
    front = yaml.safe_load(packaged.split("---")[1])
    assert front["name"] == "stream-clipper" and 100 < len(front["description"]) < 1536


def test_skill_only_mentions_real_commands_and_limits():
    text = (ROOT / "clipper" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    import re
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")
    commands = set(sub.choices)
    used = set(re.findall(r"clipper ([a-z]+)", text))
    assert used <= commands, used - commands
    assert f"{rank.HOOK_MAX_WORDS} words and {rank.HOOK_MAX_CHARS} characters" in text
    assert f"{rank.TITLE_MAX_CHARS} characters" in text


def test_plugin_manifest_and_package_data():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    for rel in manifest["skills"]:
        assert (ROOT / rel / "SKILL.md").exists()
    assert (ROOT / "clipper" / "assets" / "fonts" / "Poppins-ExtraBold.ttf").exists()
    assert (ROOT / "clipper" / "assets" / "fonts" / "OFL.txt").exists()
    assert (ROOT / "clipper" / "assets" / "review.html").exists()
