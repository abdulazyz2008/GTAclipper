"""Step 5 - review screen.  Run with:  python -m clipper review"""
import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper import edit  # noqa: E402
from clipper.common import db, fetch_clips, load_config, update_clip  # noqa: E402

st.set_page_config(page_title="Clip review", layout="centered")
cfg = load_config()
con = db()

status = st.sidebar.radio("Show", ["rendered", "approved", "rejected", "uploaded"])
counts = {s: len(fetch_clips(con, "status=?", (s,)))
          for s in ("rendered", "approved", "rejected", "uploaded")}
st.sidebar.caption(" | ".join(f"{k}: {v}" for k, v in counts.items()))

clips = fetch_clips(con, "status=?", (status,))
st.title(f"{status.title()} clips ({len(clips)})")
if not clips:
    st.info("Nothing here yet.")

for c in clips:
    with st.container(border=True):
        st.caption(f"#{c['id']} | {c['streamer']} | at {int(c['start'] // 60)}m{int(c['start'] % 60):02d}s "
                   f"| score {c['llm_score']:.0f} | chat {c['chat_score']:.1f} | audio {c['audio_score']:.1f}")
        if c["file"] and Path(c["file"]).exists():
            st.video(c["file"])
        if c["reason"]:
            st.write(c["reason"])
        hook = st.text_input("Hook (on-screen text)", c["hook"], key=f"h{c['id']}")
        title = st.text_input("Title", c["title"], key=f"t{c['id']}", max_chars=95)
        a, b, r = st.columns(3)

        def save(new_status=None, rerender=False, c=c, hook=hook, title=title):
            fields = {"hook": hook, "title": title}
            if new_status:
                fields["status"] = new_status
            if rerender or hook != c["hook"]:
                facecam = cfg["streamers"].get(c["streamer"], {}).get("facecam")
                edit.render(c["vod_path"], c["start"], c["end"], c["file"], hook,
                            json.loads(c["words"] or "[]"), facecam)
            update_clip(con, c["id"], **fields)
            st.rerun()

        if a.button("Approve", key=f"a{c['id']}", type="primary"):
            save("approved")
        if b.button("Reject", key=f"r{c['id']}"):
            save("rejected")
        if r.button("Save + re-render", key=f"s{c['id']}"):
            save(rerender=True)
