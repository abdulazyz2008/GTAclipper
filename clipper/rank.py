"""Judge the candidates: score them and write a hook + title.

Three ways to get a judgement, all checked by the same rules:

  claude     Claude, used as a skill, reads `clipper candidates --json`, looks
             at the frames, and hands back a JSON file (`clipper judge`). No
             API key needed.
  api        `clipper run` calls the Claude API itself when a key is set.
  heuristic  No model at all: order by the detector score.
"""
import json
import os
import re
from pathlib import Path

HOOK_MAX_WORDS = 8
HOOK_MAX_CHARS = 60
TITLE_MAX_CHARS = 95

SYSTEM = """You are an experienced short-form video editor choosing clips from a {game} livestream by {streamer}.

Each candidate is a moment where live chat suddenly sped up or the audio spiked. You get what the streamer said (speech-to-text, may contain errors) and the messages chat repeated most. Decide how well each would work as a standalone 30-50 second vertical clip for viewers who never saw the stream.

score (0-100): reward a clear setup and payoff, a strong genuine reaction, something funny, shocking or skilful. Penalise dead air, menus and loading screens, moments that only make sense with earlier context, and spikes caused by something off-stream (raids, subs, giveaways, chat talking among themselves).

hook: the on-screen text, at most {hook_words} words and {hook_chars} characters. It must describe what actually happens in this moment and make someone stop scrolling. Never invent events, names or outcomes that are not in the transcript or chat - a misleading hook gets the clip reported.

title: at most {title_chars} characters, natural sentence case (not ALL CAPS), no angle brackets, and no story spoilers (no endings, deaths or plot twists).

reason: one short sentence on why you gave that score.

If the transcript and chat do not tell you enough, give a low score and say so in the reason rather than guessing. Return one entry per candidate, using the same id."""

SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "hook": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "hook", "title", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def _one_line(s):
    return " ".join(str(s).split())


def check_judgement(d):
    """Validate one judgement. Returns (clean_fields, list_of_problems)."""
    problems = []
    out = {}
    try:
        score = float(d.get("score"))
        if not 0 <= score <= 100:
            raise ValueError
        out["llm_score"] = round(score, 1)
    except (TypeError, ValueError):
        problems.append("score must be a number from 0 to 100")
    hook = _one_line(d.get("hook", ""))
    if len(hook) > HOOK_MAX_CHARS or len(hook.split()) > HOOK_MAX_WORDS:
        problems.append(f"hook must be at most {HOOK_MAX_WORDS} words and "
                        f"{HOOK_MAX_CHARS} characters (got {len(hook.split())} words, "
                        f"{len(hook)} characters)")
    out["hook"] = hook
    title = _one_line(d.get("title", ""))
    if not title:
        problems.append("title is empty")
    elif len(title) > TITLE_MAX_CHARS:
        problems.append(f"title must be at most {TITLE_MAX_CHARS} characters (got {len(title)})")
    elif re.search(r"[<>]", title):
        problems.append("title may not contain < or > (YouTube rejects them)")
    elif len(title) > 12 and title.upper() == title and any(c.isalpha() for c in title):
        problems.append("title is ALL CAPS; use normal sentence case")
    out["title"] = title
    out["reason"] = _one_line(d.get("reason", ""))[:300]
    return out, problems


def heuristic(cands, streamer, game):
    """Fallback ranking: detector score scaled to 0-100, plain title, no hook."""
    top = max((c["score"] for c in cands), default=1) or 1
    for c in cands:
        if c.get("judge"):
            continue
        c["llm_score"] = round(100 * c["score"] / top, 1)
        c["hook"] = c.get("hook") or ""
        c["title"] = c.get("title") or f"{streamer} {game} moment"[:TITLE_MAX_CHARS]
        c["reason"] = "ranked by chat and audio spike only"
        c["judge"] = "heuristic"
    return cands


def api_configured():
    """True when the Claude API looks usable (key, token or a saved login)."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    cfg_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return (Path(cfg_home) / "anthropic").is_dir()


def _item(i, c):
    return {
        "id": i,
        "at": f"{int(c['start'] // 60)}m{int(c['start'] % 60):02d}s",
        "seconds": round(c["end"] - c["start"]),
        "chat_spike": c.get("chat_score", 0),
        "chat_acceleration": c.get("accel_score", 0),
        "audio_spike": c.get("audio_score", 0),
        "streamer_said": c.get("transcript") or "",
        "chat_said": c.get("chat_sample") or [],
    }


def rank_api(cands, streamer, game, cfg, log=print, client=None):
    """Score candidates with the Claude API. Anything it cannot judge is left
    for the heuristic. Returns the number judged by the model."""
    import anthropic
    client = client or anthropic.Anthropic()
    system = SYSTEM.format(game=game, streamer=streamer, hook_words=HOOK_MAX_WORDS,
                           hook_chars=HOOK_MAX_CHARS, title_chars=TITLE_MAX_CHARS)
    judged = 0
    for lo in range(0, len(cands), 10):
        batch = cands[lo: lo + 10]
        items = "\n".join(json.dumps(_item(lo + i, c), ensure_ascii=False)
                          for i, c in enumerate(batch))
        try:
            msg = client.messages.create(
                model=cfg["model"], max_tokens=16000, system=system,
                messages=[{"role": "user", "content": "Candidates, one JSON object per line:\n"
                           + items}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
        except anthropic.AuthenticationError:
            log("  Claude API key was rejected; ranking by detector score instead")
            break
        except anthropic.RateLimitError:
            log("  Claude API rate limit reached; the rest is ranked by detector score")
            break
        except anthropic.APIStatusError as e:
            log(f"  Claude API error {e.status_code}: {str(e.message)[:160]}; "
                "the rest is ranked by detector score")
            break
        except anthropic.APIConnectionError:
            log("  could not reach the Claude API; ranking by detector score instead")
            break
        if msg.stop_reason != "end_turn":
            log(f"  Claude stopped early ({msg.stop_reason}) on candidates {lo}-{lo + len(batch) - 1}")
            continue
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            data = json.loads(text)["clips"]
        except (ValueError, KeyError, TypeError):
            log(f"  unreadable answer for candidates {lo}-{lo + len(batch) - 1}")
            continue
        for d in data:
            try:
                i = int(d["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if not lo <= i < lo + len(batch):
                continue
            fields, problems = check_judgement(d)
            if "llm_score" not in fields:
                continue
            if any(p.startswith("hook") for p in problems):     # too long: drop, never cut mid-word
                fields["hook"] = ""
            if any(p.startswith("title") for p in problems):
                fields["title"] = f"{streamer} {game} moment"[:TITLE_MAX_CHARS]
            cands[i].update(fields, judge="api")
            judged += 1
    return judged


def rank(cands, streamer, game, cfg, use_llm=True, log=print):
    """Fill llm_score / hook / title / reason / judge on every candidate."""
    if not cands:
        return cands
    if use_llm and not api_configured():
        log("  no Claude API key set - ranking by detector score "
            "(inside Claude, use `clipper judge` instead)")
        use_llm = False
    if use_llm:
        try:
            n = rank_api(cands, streamer, game, cfg, log=log)
            log(f"  Claude judged {n} of {len(cands)} candidates")
        except ImportError:
            log("  the 'anthropic' package is not installed (pip install anthropic); "
                "ranking by detector score")
        except Exception as e:      # never lose a finished scan to a ranking problem
            log(f"  ranking failed ({type(e).__name__}: {str(e)[:160]}); using detector score")
    return heuristic(cands, streamer, game)
