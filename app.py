"""Streamlit front end: upload a show's screenplays (zip), find promos, inspect each one against the script.

    streamlit run app.py
No show data is bundled with the app; it only processes what you upload.
"""
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from promo_finder.config import Config, PROVIDERS
from promo_finder.parse import load_show, fmt, parse_episode_filter
from promo_finder.progress import Pipeline
from promo_finder.rank import rank
from promo_finder import rules_backend

st.set_page_config(page_title="Promo Finder", layout="wide")
st.title("Promo Finder")
st.caption("Finds 20–60s promo clips (hook in 3s → cliffhanger cut → makes sense alone) in microdrama screenplays.")

with st.sidebar:
    up = st.file_uploader("Show screenplays (.zip of episode .txt files)", type="zip")
    RULES, HYBRID = "Rules only (no API key)", "Hybrid: LLM + rules"
    backend = st.radio(
        "Backend", [RULES, HYBRID], index=0,
        help="Rules only: deterministic search, no key needed.  \n"
             "Hybrid: the LLM proposes clips; rules snap them to clean cuts, re-score them "
             "(final score = 70% LLM + 30% rules) and fill any episode the LLM misses.")
    cfg = Config()
    if backend == HYBRID:
        cfg.provider = st.selectbox("Provider", list(PROVIDERS), index=0)
        cfg.model = st.text_input("Model", PROVIDERS[cfg.provider][1])
        key = st.text_input("API key", type="password", help="Used for this run only, never stored.")
        cfg.api_key = key.strip()  # passed to this run only; pasted keys often carry a stray space or newline
        cfg.montages = st.slider("Cross-episode promos", 0, 10, 5)
    eps_filter = st.text_input("Episodes (blank = all, e.g. 1-10)", "")
    with st.expander("Ranking weights"):
        cfg.w_hook = st.slider("Hook", 0.0, 1.0, cfg.w_hook)
        cfg.w_cliff = st.slider("Cliffhanger", 0.0, 1.0, cfg.w_cliff)
        cfg.w_standalone = st.slider("Standalone", 0.0, 1.0, cfg.w_standalone)
        cfg.funnel_bonus = st.slider("Early-episode bonus", 0.0, 0.2, cfg.funnel_bonus)
    go = st.button("Find promos", type="primary", disabled=up is None)


def run_rules(eps, pipe):
    return rules_backend.run(eps, cfg, log=lambda *_: None, progress=pipe)


try:
    wanted = parse_episode_filter(eps_filter)
except ValueError as e:
    wanted = None
    if go:
        st.error(f"Couldn't read the Episodes box (\"{e}\"). Use numbers like `1-10` or `1, 3, 5`, "
                 "or leave it blank for all episodes.")
        go = False

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")
base_url, _, api_key = cfg.llm_settings()
if go and backend == HYBRID and not api_key and not any(h in base_url for h in LOCAL_HOSTS):
    st.error("Hybrid mode needs an API key for the model. Paste one in the sidebar "
             "(a free Gemini key: aistudio.google.com/apikey), or choose **Rules only**, which needs no key.")
    go = False

ran_now = False
if go and up is not None:
    ran_now = True
    st.subheader("Pipeline")
    map_col, log_col = st.columns([2, 3])
    chart = map_col.empty()
    pipe = Pipeline(on_change=lambda p: chart.graphviz_chart(p.to_dot(), width="stretch"))
    pipe.set_mode(hybrid=backend == HYBRID)
    tmp = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(io.BytesIO(up.getvalue())) as z:
        for m in z.infolist():  # extract .txt only, flattened, no path traversal
            if m.filename.lower().endswith(".txt") and not m.is_dir():
                (tmp / Path(m.filename).name).write_bytes(z.read(m))
    n_files = len([f for f in tmp.glob("*.txt") if not f.name.startswith(".")])
    pipe("input", "done", f"{n_files} .txt file(s)")
    pipe("parse", "active")
    parsed = load_show(tmp)
    not_scripts = [Path(e.path).name for e in parsed if not e.blocks]  # no timestamped [mm:ss-mm:ss] lines
    all_eps = [e for e in parsed if e.blocks]
    eps = [e for e in all_eps if wanted is None or e.number in wanted]
    if not_scripts:
        st.warning(f"Skipped {len(not_scripts)} file(s) with no timestamped screenplay lines: "
                   + ", ".join(not_scripts[:5]) + ("…" if len(not_scripts) > 5 else ""))
    if all_eps and not eps:
        nums = sorted(e.number for e in all_eps)
        pipe.fail("no episodes in range", "parse")
        st.session_state.update(promos=None, pipeline_dot=None)
        st.error(f"No episodes match \"{eps_filter.strip()}\". This upload has episodes "
                 f"{nums[0]}–{nums[-1]}; clear the Episodes box to use all of them.")
        st.stop()
    if not eps:
        pipe.fail("no screenplays found", "parse")
        st.session_state.update(promos=None, pipeline_dot=None)
        st.error("No episode screenplays found. Upload a .zip of episode .txt screenplays "
                 "(e.g. episode-001.txt), not the tool's output files.")
        st.stop()
    pipe("parse", "done", f"{len(eps)} episode(s)")
    unnumbered = [Path(e.path).name for e in eps if e.number == 0]
    if unnumbered:
        st.warning("No episode number found in: " + ", ".join(unnumbered[:5])
                   + ". Is this a screenplay? Episodes need an `EPISODE NUMBER:` line or a name like episode-001.txt.")
    pipe("backend", "done", "hybrid" if backend == HYBRID else "rules only")
    show = Path(up.name).stem
    llm_failed = None
    with log_col.status(f"Processing {len(eps)} episode(s)…", expanded=True) as status:
        if backend == HYBRID:
            from promo_finder import llm_backend
            try:
                promos, stats = llm_backend.run(eps, cfg, tmp / "raw_llm", show, log=st.write, progress=pipe)
            except SystemExit as e:  # the provider refused every request (bad key, quota, retired model)
                llm_failed = str(e)
            except Exception as e:  # never show a traceback: fall back to the no-key backend instead
                llm_failed = f"{type(e).__name__}: {e}"
            if llm_failed:
                pipe = Pipeline(on_change=lambda p: chart.graphviz_chart(p.to_dot(), width="stretch"))
                pipe.set_mode(hybrid=False)
                pipe("input", "done", f"{n_files} .txt file(s)")
                pipe("parse", "done", f"{len(eps)} episode(s)")
                pipe("backend", "done", "rules only (model failed)")
                promos, stats = run_rules(eps, pipe), {}
        else:
            promos, stats = run_rules(eps, pipe), {}
        status.update(label=f"Done: {len(promos)} promos (log)", state="complete", expanded=False)
    shutil.rmtree(tmp, ignore_errors=True)
    if llm_failed:
        st.warning("The model run failed, so these are **Rules only** results (no API key needed). "
                   f"Details: {llm_failed[:400]}")
    if stats.get("incomplete"):
        st.warning("The model stopped partway, so this run is incomplete. " + stats["incomplete"])
    pipe("rank", "active")
    ranked = rank(promos, cfg)
    pipe("rank", "done", f"{len(ranked)} promos")
    pipe("output", "done", "see below")
    pipe("editor", "active", "your turn")
    st.session_state.update(promos=ranked, eps={e.number: e for e in eps}, show=show, stats=stats,
                            pipeline_dot=pipe.to_dot())

promos = st.session_state.get("promos")
if not promos:
    st.info("Upload a .zip of episode screenplays in the sidebar (on a phone, tap **»** at the top left), "
            "then press **Find promos**. **Rules only** needs no API key and takes a few seconds.")
    st.stop()

if not ran_now and st.session_state.get("pipeline_dot"):
    with st.expander("Pipeline for this run"):
        st.graphviz_chart(st.session_state["pipeline_dot"], width="stretch")

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
