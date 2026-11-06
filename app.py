"""Streamlit front end: upload a show's screenplays (zip), find promos, inspect each one against the script.

    streamlit run app.py
No show data is bundled with the app; it only processes what you upload.
"""
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from promo_finder.config import Config, PROVIDERS
from promo_finder.parse import load_show, fmt
from promo_finder.rank import rank
from promo_finder import rules_backend

st.set_page_config(page_title="Promo Finder", layout="wide")
st.title("Promo Finder")
st.caption("Finds 20–60s promo clips (hook in 3s → cliffhanger cut → makes sense alone) in microdrama screenplays.")

with st.sidebar:
    up = st.file_uploader("Show screenplays (.zip of episode .txt files)", type="zip")
    backend = st.radio("Backend", ["rules (no API key)", "LLM"], index=0)
    cfg = Config()
    if backend == "LLM":
        cfg.provider = st.selectbox("Provider", list(PROVIDERS), index=0)
        cfg.model = st.text_input("Model", PROVIDERS[cfg.provider][1])
        key = st.text_input("API key", type="password", help="Used for this session only, never stored.")
        cfg.montages = st.slider("Cross-episode promos", 0, 10, 5)
    eps_filter = st.text_input("Episodes (blank = all, e.g. 1-10)", "")
    with st.expander("Ranking weights"):
        cfg.w_hook = st.slider("Hook", 0.0, 1.0, cfg.w_hook)
        cfg.w_cliff = st.slider("Cliffhanger", 0.0, 1.0, cfg.w_cliff)
        cfg.w_standalone = st.slider("Standalone", 0.0, 1.0, cfg.w_standalone)
        cfg.funnel_bonus = st.slider("Early-episode bonus", 0.0, 0.2, cfg.funnel_bonus)
    go = st.button("Find promos", type="primary", disabled=up is None)


def parse_filter(s, nums):
    if not s.strip():
        return nums
    want = set()
    for part in s.split(","):
        lo, _, hi = part.strip().partition("-")
        want.update(range(int(lo), int(hi or lo) + 1))
    return [n for n in nums if n in want]


if go and up is not None:
    tmp = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(io.BytesIO(up.getvalue())) as z:
        for m in z.infolist():  # extract .txt only, flattened, no path traversal
            if m.filename.lower().endswith(".txt") and not m.is_dir():
                (tmp / Path(m.filename).name).write_bytes(z.read(m))
    eps = load_show(tmp)
    keep = set(parse_filter(eps_filter, [e.number for e in eps]))
    eps = [e for e in eps if e.number in keep]
    show = Path(up.name).stem
    with st.status(f"Processing {len(eps)} episodes…", expanded=True) as status:
        if backend == "LLM":
            from promo_finder import llm_backend
            os.environ["LLM_API_KEY"] = key
            promos, stats = llm_backend.run(eps, cfg, tmp / "raw_llm", show, log=st.write)
        else:
            promos, stats = rules_backend.run(eps, cfg, log=lambda *_: None), {}
        status.update(label=f"Done: {len(promos)} promos", state="complete")
    st.session_state.update(promos=rank(promos, cfg), eps={e.number: e for e in eps}, show=show, stats=stats)

promos = st.session_state.get("promos")
if not promos:
    st.info("Upload a zip of episode screenplays and press **Find promos**.")
    st.stop()

eps = st.session_state["eps"]
df = pd.DataFrame([{
    "rank": p.rank, "kind": p.kind,
    "segments": " + ".join(f"E{s.episode} {fmt(s.start)}–{fmt(s.end)}" for s in p.segments),
    "sec": round(p.duration), "score": p.score, "hook": p.hook, "cliffhanger": p.cliffhanger, "why": p.why,
    "flags": "; ".join(p.flags)} for p in promos])
c1, c2, c3 = st.columns(3)
c1.metric("Promos", len(promos))
c2.metric("Episodes", len(eps))
c3.metric("LLM calls", st.session_state["stats"].get("calls", 0))
st.dataframe(df, width="stretch", hide_index=True, height=320)

d1, d2 = st.columns(2)
d1.download_button("Download JSON", json.dumps([p.to_dict() for p in promos], indent=2), "promos.json")
d2.download_button("Download CSV", df.to_csv(index=False), "promos.csv")

st.subheader("Inspect a promo")
pick = st.selectbox("Promo", promos, format_func=lambda p: f"#{p.rank} · " + " + ".join(
    f"E{s.episode} {fmt(s.start)}–{fmt(s.end)}" for s in p.segments) + f" · {p.duration:.0f}s")
r = pick.rule_scores
st.markdown(f"**Why:** {pick.why}")
cols = st.columns(4)
cols[0].metric("Hook (rules)", f"{r['hook']:.2f}", pick.llm_scores.get("hook") and f"LLM {pick.llm_scores['hook']}/5")
cols[1].metric("Cliffhanger (rules)", f"{r['cliffhanger']:.2f}",
               pick.llm_scores.get("cliffhanger") and f"LLM {pick.llm_scores['cliffhanger']}/5")
cols[2].metric("Standalone (rules)", f"{r['standalone']:.2f}",
               pick.llm_scores.get("standalone") and f"LLM {pick.llm_scores['standalone']}/5")
cols[3].metric("Length", f"{pick.duration:.0f}s")
if pick.flags:
    st.warning(" · ".join(pick.flags))
for i, s in enumerate(pick.segments, 1):
    ep = eps[s.episode]
    st.markdown(f"**Segment {i}: Episode {s.episode}, {fmt(s.start)} → {fmt(s.end)}**  \n_{ep.logline}_")
    lines = []
    for b in ep.beats:
        if s.start - 4 <= b.start <= s.end + 4:
            inside = s.start - 0.01 <= b.start <= s.end
            mark = "▶" if inside else "  "
            who = f"{b.speaker} ({b.emotion})" if b.kind == "DIALOGUE" else b.kind
            lines.append(f"{mark} {fmt(b.start)}  {who}: {b.text}")
    st.code("\n".join(lines), language=None)
