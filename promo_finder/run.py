"""CLI: python -m promo_finder.run --input <folder of episode .txt> --out outputs/ [--backend llm|rules]"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from .config import load_config
from .parse import load_show, fmt
from .rank import rank
from . import rules_backend


def write_outputs(promos, out: Path, meta: dict):
    out.mkdir(parents=True, exist_ok=True)
    data = {"meta": meta, "promos": [p.to_dict() for p in promos]}
    (out / "promos.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(out / "promos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "id", "kind", "episodes", "segments", "duration_s", "score", "hook", "cliffhanger",
                    "why", "llm_hook", "llm_cliff", "llm_standalone", "rule_hook", "rule_cliff",
                    "rule_standalone", "flags"])
        for p in promos:
            segs = " + ".join(f"E{s.episode} {fmt(s.start)}-{fmt(s.end)}" for s in p.segments)
            l, r = p.llm_scores, p.rule_scores
            w.writerow([p.rank, p.id, p.kind, " ".join(map(str, p.episodes)), segs, round(p.duration, 1),
                        p.score, p.hook, p.cliffhanger, p.why, l.get("hook", ""), l.get("cliffhanger", ""),
                        l.get("standalone", ""), r.get("hook"), r.get("cliffhanger"), r.get("standalone"),
                        " | ".join(p.flags)])

    lines = [f"# Promos — {meta['show']}", "",
             f"Backend: **{meta['backend']}**" + (f" (model `{meta.get('model')}`)" if meta.get("model") else "")
             + f" · {meta['episodes']} episodes · {len(promos)} promos · generated {meta['generated']}", ""]
    if meta.get("backend") == "llm":
        used = ", ".join(f"{m} ×{n}" for m, n in (meta.get("models_used") or {}).items()) or "none"  # incl. cache
        lines += [f"Requests: {meta.get('http_requests', 0)} sent, {meta.get('cache_hits', 0)} from cache · "
                  f"answered by: {used}", ""]
        if meta.get("rules_fallback_episodes"):
            lines += [f"Rules-backend fallback used for episodes: "
                      f"{', '.join(map(str, meta['rules_fallback_episodes']))}", ""]
        if meta.get("incomplete"):
            lines += [f"> **Incomplete run:** {meta['incomplete'].splitlines()[0]}  ", 
                      "> Rerun the same command later; finished requests are reused from cache.", ""]
    lines += ["## Post first (top 10)", ""]
    for p in promos[:10]:
        segs = " + ".join(f"Ep {s.episode} {fmt(s.start)}–{fmt(s.end)}" for s in p.segments)
        lines += [f"### {p.rank}. {segs} ({p.duration:.0f}s, score {p.score:.3f})",
                  f"- **Hook:** {p.hook}", f"- **Cliffhanger:** {p.cliffhanger}", f"- **Why:** {p.why}"]
        if p.flags:
            lines.append(f"- **Flags:** {'; '.join(p.flags)}")
        lines.append("")
    lines += ["## All promos", "", "| Rank | Kind | Segments | Len | Score | Hook |", "|---|---|---|---|---|---|"]
    for p in promos:
        segs = " + ".join(f"E{s.episode} {fmt(s.start)}–{fmt(s.end)}" for s in p.segments)
        lines.append(f"| {p.rank} | {p.kind} | {segs} | {p.duration:.0f}s | {p.score:.3f} | "
                     f"{p.hook[:90].replace('|', '/')} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from .env (git-ignored) so keys never go on the command line."""
    import os
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description="Find ranked promo clips in microdrama screenplays.")
    ap.add_argument("--input", required=True, help="folder containing episode .txt screenplays")
    ap.add_argument("--out", default="outputs", help="output folder")
    ap.add_argument("--backend", choices=["llm", "rules"], default="llm")
    ap.add_argument("--config", help="optional TOML file overriding defaults")
    ap.add_argument("--provider", help="gemini | groq | openrouter | openai | ollama")
    ap.add_argument("--model", help="model name override")
    ap.add_argument("--episodes", help="subset, e.g. 1-5 or 1,4,9")
    ap.add_argument("--no-montages", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config(a.config, provider=a.provider, model=a.model, montages=0 if a.no_montages else None)
    eps = load_show(a.input)
    if a.episodes:
        want = set()
        for part in a.episodes.split(","):
            lo, _, hi = part.partition("-")
            want.update(range(int(lo), int(hi or lo) + 1))
        eps = [e for e in eps if e.number in want]
    if not eps:
        raise SystemExit("No episodes found.")
    show = Path(a.input).resolve().name
    sub = [p for p in Path(a.input).iterdir() if p.is_dir()]
    if len(sub) == 1:
        show = sub[0].name
    out = Path(a.out)
    print(f"{show}: {len(eps)} episodes, {sum(len(e.blocks) for e in eps)} blocks, backend={a.backend}")

    t0 = time.time()
    meta = {"show": show, "backend": a.backend, "episodes": len(eps),
            "generated": time.strftime("%Y-%m-%d %H:%M"), "config": {k: v for k, v in vars(cfg).items()
                                                                     if k != "extra"}}
    if a.backend == "rules":
        promos = rules_backend.run(eps, cfg)
    else:
        from . import llm_backend
        promos, stats = llm_backend.run(eps, cfg, out / "raw_llm", show)
        meta.update(stats)
    ranked = rank(promos, cfg)
    meta["seconds"] = round(time.time() - t0, 1)
    write_outputs(ranked, out, meta)
    print(f"{len(ranked)} promos -> {out}/promos.json, promos.csv, report.md ({meta['seconds']}s)")
    if a.backend == "llm":
        print(f"requests sent: {meta.get('http_requests', 0)} · from cache: {meta.get('cache_hits', 0)} · "
              f"models: {meta.get('models_used')}")


if __name__ == "__main__":
    main()
