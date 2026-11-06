"""Compare tool output to a human editor's picks.

gold.csv columns: episode,start,end[,note]   (MM:SS.ss or seconds; one row per promo the editor would cut)
Only episodes that appear in gold.csv are scored, so you can label 5 episodes and still get numbers.

    python -m promo_finder.evaluate --pred outputs/llm/promos.json --gold eval/gold.csv
"""
from __future__ import annotations

import argparse
import csv
import json

from .promo import parse_time


def overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--min-cover", type=float, default=0.5, help="share of the gold clip a pick must cover")
    a = ap.parse_args(argv)

    gold = []
    with open(a.gold, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gold.append((int(r["episode"]), parse_time(r["start"]), parse_time(r["end"]), r.get("note", "")))
    eps = {g[0] for g in gold}
    promos = json.load(open(a.pred, encoding="utf-8"))["promos"]
    preds = [(s["episode"], s["start_s"], s["end_s"], p["rank"]) for p in promos for s in p["segments"]
             if s["episode"] in eps]

    hits, rows = 0, []
    for ep, gs, ge, note in gold:
        best = max(((overlap((gs, ge), (ps, pe)) / (ge - gs), r, abs(ps - gs)) for e, ps, pe, r in preds if e == ep),
                   default=(0, None, None))
        hit = best[0] >= a.min_cover
        hits += hit
        rows.append((ep, gs, ge, round(best[0], 2), best[1], None if best[2] is None else round(best[2], 1), hit, note))

    matched_preds = sum(1 for e, ps, pe, _ in preds
                        if any(e == g[0] and overlap((ps, pe), (g[1], g[2])) / (pe - ps) >= a.min_cover for g in gold))
    print(f"Scored episodes: {sorted(eps)}")
    print(f"Recall    : {hits}/{len(gold)} editor picks found ({hits / max(len(gold), 1):.0%})")
    print(f"Precision : {matched_preds}/{len(preds)} tool segments match an editor pick "
          f"({matched_preds / max(len(preds), 1):.0%})")
    print("\nep  gold          cover  tool_rank  start_err  hit  note")
    for ep, gs, ge, cov, r, err, hit, note in rows:
        print(f"{ep:>2}  {gs:6.1f}-{ge:6.1f}  {cov:5.2f}  {str(r):>9}  {str(err):>9}  {'Y' if hit else '-'}    {note}")


if __name__ == "__main__":
    main()
