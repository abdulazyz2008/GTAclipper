"""Step 3 - let an LLM judge the candidates and write a hook + title.

Without ANTHROPIC_API_KEY (or with --no-llm) it falls back to the detector
score and a plain title, so the pipeline still runs end to end.
"""
import json
import os

PROMPT = """You are a short-form video editor picking clips from a {game} livestream by {streamer}.
For each candidate you get the streamer's words and what chat said.
Score how well it would work as a standalone 30-40 second vertical clip.

Rules:
- score: 0-100. Reward a clear setup and payoff, strong reactions, funny or shocking moments.
  Penalise dead air, menu screens, and moments that need earlier context.
- hook: max 8 words of on-screen text. It must describe what really happens. No invented claims.
- title: max 70 characters, no ALL CAPS, no story spoilers (no endings, deaths or plot twists).
- reason: one short sentence.

Reply with ONLY a JSON array, one object per candidate:
[{{"id": 0, "score": 0, "hook": "", "title": "", "reason": ""}}]

Candidates:
{items}"""


def _fallback(cands, streamer, game):
    top = max((c["score"] for c in cands), default=1) or 1
    for c in cands:
        c.setdefault("llm_score", round(100 * c["score"] / top, 1))
        c.setdefault("hook", "")
        c.setdefault("title", f"{streamer} {game} moment")
        c.setdefault("reason", "ranked by chat and audio spike only")
    return cands


def rank(cands, streamer, game, cfg, use_llm=True):
    if not cands:
        return cands
    if not use_llm or not os.environ.get("ANTHROPIC_API_KEY"):
        if use_llm:
            print("  ANTHROPIC_API_KEY not set; ranking by detector score")
        return _fallback(cands, streamer, game)
    try:
        import anthropic
        client = anthropic.Anthropic()
        for lo in range(0, len(cands), 10):
            batch = cands[lo: lo + 10]
            items = "\n".join(json.dumps({
                "id": lo + i,
                "at": f"{int(c['start'] // 60)}m{int(c['start'] % 60)}s",
                "chat_spike": c["chat_score"], "audio_spike": c["audio_score"],
                "streamer_said": (c.get("transcript") or "")[:600],
                "chat_said": c.get("chat_sample", [])[:15],
            }, ensure_ascii=False) for i, c in enumerate(batch))
            msg = client.messages.create(
                model=cfg["model"], max_tokens=2500,
                messages=[{"role": "user", "content": PROMPT.format(
                    game=game, streamer=streamer, items=items)}])
            text = "".join(b.text for b in msg.content if b.type == "text")
            data = json.loads(text[text.index("["): text.rindex("]") + 1])
            for d in data:
                i = int(d["id"])
                if 0 <= i < len(cands):
                    cands[i].update(
                        llm_score=float(d.get("score", 0)),
                        hook=str(d.get("hook", ""))[:80],
                        title=str(d.get("title", ""))[:95],
                        reason=str(d.get("reason", ""))[:300])
    except Exception as e:
        print(f"  LLM ranking failed ({type(e).__name__}: {e}); using detector score")
    return _fallback(cands, streamer, game)
