"""Final score + posting order (spread across the story so the feed isn't five clips of one scene)."""
from __future__ import annotations

from .promo import Promo


def base_score(p: Promo, cfg) -> float:
    r = p.rule_scores
    rule = (cfg.w_hook * r["hook"] + cfg.w_cliff * r["cliffhanger"] + cfg.w_standalone * r["standalone"])
    if p.llm_scores:
        l = p.llm_scores
        llm = (cfg.w_hook * l.get("hook", 3) + cfg.w_cliff * l.get("cliffhanger", 3)
               + cfg.w_standalone * l.get("standalone", 3)) / 5.0
        s = cfg.w_llm * llm + (1 - cfg.w_llm) * rule
    else:
        s = rule
    s *= r.get("duration_fit", 1.0)
    # early-episode bonus as a blend, so the final score stays in 0..1
    s = s * (1 - cfg.funnel_bonus) + (cfg.funnel_bonus if min(p.episodes) <= cfg.funnel_max_ep else 0.0)
    if any(f.startswith("weak_open") for f in p.flags):
        s -= 0.15
    return round(max(s, 0.0), 4)


def rank(promos: list[Promo], cfg) -> list[Promo]:
    for p in promos:
        p.score = base_score(p, cfg)
        if p.score < cfg.low_confidence and not any(f.startswith("low_confidence") for f in p.flags):
            p.flags.append("low_confidence: quiet episode, editor should check or skip")
    pool = sorted(promos, key=lambda p: -p.score)
    ordered: list[Promo] = []
    while pool:
        def adj(p):
            pen = 0.0
            for q in ordered[-5:]:  # diversity only matters among neighbouring posts
                if set(p.episodes) & set(q.episodes):
                    pen += cfg.same_ep_penalty
                elif min(abs(a - b) for a in p.episodes for b in q.episodes) <= cfg.near_ep_distance:
                    pen += cfg.near_ep_penalty
            return p.score - pen
        best = max(pool, key=adj)
        pool.remove(best)
        ordered.append(best)
    for i, p in enumerate(ordered, 1):
        p.rank = i
        p.id = f"P{i:03d}"
    return ordered
