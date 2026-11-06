"""No-API baseline: exhaustively score every shot-aligned 20-60s window and keep the best."""
from __future__ import annotations

from .parse import Episode, fmt
from .promo import Promo, Segment, rule_score, hook_text, cliff_text
from . import features as F
from .progress import noop


def _overlap(a: Promo, b: Promo) -> float:
    sa, sb = a.segments[0], b.segments[0]
    inter = max(0.0, min(sa.end, sb.end) - max(sa.start, sb.start))
    return inter / min(sa.length, sb.length)


def _why(p: Promo, ep: Episode) -> str:
    r = p.rule_scores
    first = F.beats_in(ep, p.segments[0].start, p.segments[0].start + 3)
    lead = next((b for b in first if b.kind in ("DIALOGUE", "ACTION")), None)
    if lead and lead.kind == "DIALOGUE":
        adj = lead.emotion.split(',')[0].strip().lower() or "sharp"
        open_desc = f"{'an' if adj[0] in 'aeiou' else 'a'} {adj} line"
    else:
        open_desc = "physical action" if lead else "a quiet beat"
    end_notes = [n for n in r["notes"] if n in ("uses the episode's own cliffhanger ending",
                                                "music/SFX sting at the cut", "cuts on an unanswered line")]
    end_desc = end_notes[0] if end_notes else "cuts on a charged moment"
    return f"Opens on {open_desc}; {end_desc}. (rule scores h={r['hook']:.2f} c={r['cliffhanger']:.2f} s={r['standalone']:.2f})"


def episode_promos(ep: Episode, cfg) -> list[Promo]:
    eps = {ep.number: ep}
    beat_starts = sorted({b.start for b in ep.beats if b.kind in ("DIALOGUE", "ACTION")})
    starts = sorted({s.start for s in ep.shots
                     if any(s.start - 0.05 <= t <= s.start + cfg.hook_window for t in beat_starts)})
    ends = sorted({s.end for s in ep.shots})
    dialogue = [b for b in ep.beats if b.kind == "DIALOGUE"]
    ends = [e for e in ends if not any(b.start < e - 0.05 < b.end - 0.05 for b in dialogue)]

    hook_cache = {t: F.hook_score(ep, t, cfg.hook_window)[0] for t in starts}
    cliff_cache = {t: F.cliff_score(ep, t)[0] for t in ends}

    scored = []
    for a in starts:
        if hook_cache[a] < 0.35:
            continue
        for b in ends:
            d = b - a
            if d < cfg.min_len or d > cfg.max_len:
                continue
            p = Promo([Segment(ep.number, a, b)], backend="rules")
            rule_score(p, eps, cfg)
            r = p.rule_scores
            p.score = (cfg.w_hook * r["hook"] + cfg.w_cliff * r["cliffhanger"]
                       + cfg.w_standalone * r["standalone"]) * r["duration_fit"]
            scored.append(p)
    scored.sort(key=lambda p: -p.score)

    kept: list[Promo] = []
    for p in scored:
        if all(_overlap(p, k) <= cfg.max_overlap for k in kept):
            kept.append(p)
        if len(kept) >= cfg.per_episode:
            break
    for p in kept:
        s = p.segments[0]
        p.hook = hook_text(ep, s.start, cfg.hook_window)
        p.cliffhanger = cliff_text(ep, s.end)
        p.why = _why(p, ep)
    return kept


def run(episodes: list[Episode], cfg, log=print, progress=noop) -> list[Promo]:
    out = []
    for i, ep in enumerate(episodes, 1):
        progress("rules", "active", f"episode {i}/{len(episodes)}")
        ps = episode_promos(ep, cfg)
        log(f"  ep {ep.number:>3}: {len(ps)} promos" + (f" (best {ps[0].score:.3f})" if ps else ""))
        out += ps
    progress("rules", "done", f"{len(out)} candidates")
    return out
