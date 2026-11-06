"""LLM backend: the model proposes promos per episode; code validates, snaps, repairs and scores them."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .parse import Episode, fmt, transcript
from .promo import Promo, Segment, parse_time, validate, rule_score
from .llm import LLMClient, LLMError, extract_json
from . import prompts as P


def _short(err: str, n: int = 300) -> str:
    """Pull the provider's human-readable message out of an error body."""
    m = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', err)
    msg = m.group(1) if m else err
    code = re.match(r"HTTP \d+", err)
    return ((code.group(0) + ": ") if code and m else "") + msg[:n]


def _clamp_score(x) -> int:
    try:
        return max(1, min(5, int(round(float(x)))))
    except (TypeError, ValueError):
        return 3


def _to_promos(data: dict, default_ep: int | None, kind: str) -> list[Promo]:
    out = []
    for item in (data.get("promos") or [])[:6]:
        segs = []
        for s in item.get("segments") or []:
            try:
                ep = int(s.get("episode", default_ep)) if s.get("episode", default_ep) is not None else None
                segs.append(Segment(ep, parse_time(s["start"]), parse_time(s["end"])))
            except (KeyError, ValueError, TypeError):
                continue
        sc = item.get("scores") or {}
        p = Promo(segs, hook=str(item.get("hook", "")), cliffhanger=str(item.get("cliffhanger", "")),
                  why=str(item.get("why", "")), backend="llm", kind=kind,
                  llm_scores={k: _clamp_score(sc.get(k)) for k in ("hook", "cliffhanger", "standalone")},
                  raw=item)
        nc = str(item.get("needs_context", "")).strip()
        if nc and nc.lower() not in ("", "none", "n/a", "nothing"):
            p.flags.append(f"needs_context: {nc}")
        out.append(p)
    return out


class LLMBackend:
    def __init__(self, cfg, raw_dir: Path, show_name: str, log=print):
        base, model, key = cfg.llm_settings()
        local = any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))
        if not key and not local:
            raise SystemExit(f"No API key for provider '{cfg.provider}'. Set LLM_API_KEY (or the provider's key "
                             f"env var) or run with --backend rules.")
        self.client = LLMClient(base, model, key, cfg.temperature, cfg.max_retries)
        self.cfg, self.raw_dir, self.show, self.log = cfg, raw_dir, show_name, log
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model
        self.last_error = ""

    def _call(self, tag: str, system: str, user: str) -> tuple[dict | None, str]:
        rec = {"model": self.model_name, "system": system, "user": user}
        fatal = None
        try:
            text = self.client.chat(system, user)
            rec["response"] = text
            data = extract_json(text)
        except (LLMError, ValueError, json.JSONDecodeError) as e:
            rec["error"] = str(e)
            data, text = None, rec.get("response", "")
            if isinstance(e, LLMError) and e.fatal:
                fatal = e
        with open(self.raw_dir / f"{tag}.json", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.last_error = rec.get("error", "")
        if fatal is not None:
            raise SystemExit(f"Stopping: the model endpoint rejected the request and every call would fail "
                             f"the same way.\n{_short(str(fatal))}\nFix the key/model (--model NAME or LLM_MODEL) "
                             f"and run again.")
        return data, text

    def _validated(self, data, text, eps, default_ep, kind, tag, system):
        promos = _to_promos(data or {}, default_ep, kind)
        for round_ in range(self.cfg.repair_rounds + 1):
            good, errs = [], []
            for i, p in enumerate(promos, 1):
                e = validate(p, eps, self.cfg)
                if e:
                    errs.append(f"promo {i}: " + "; ".join(e))
                else:
                    good.append(p)
            if not errs or round_ == self.cfg.repair_rounds or data is None:
                if errs:
                    self.log(f"    {tag}: dropped {len(errs)} invalid promo(s): {errs[0][:120]}")
                return good
            data, text = self._call(tag, system, P.REPAIR_USER.format(errors="\n".join(errs), previous=text))
            promos = _to_promos(data or {}, default_ep, kind)
        return []

    def episode(self, ep: Episode, premise: str) -> list[Promo]:
        eps = {ep.number: ep}
        user = P.EPISODE_USER.format(show=self.show, premise=premise, number=ep.number, duration=fmt(ep.duration),
                                     logline=ep.logline, continuity=ep.continuity,
                                     transcript=transcript(ep, self.cfg.include_camera))
        tag = f"ep_{ep.number:03d}"
        data, text = self._call(tag, P.EPISODE_SYSTEM, user)
        if data is None:
            self.log(f"    {tag}: model call failed: {_short(self.last_error)}")
            return []
        promos = self._validated(data, text, eps, ep.number, "single", tag, P.EPISODE_SYSTEM)
        for p in promos:
            rule_score(p, eps, self.cfg)
        return promos

    def montages(self, episodes: list[Episode], pool: list[Promo]) -> list[Promo]:
        if self.cfg.montages <= 0 or not pool:
            return []
        eps = {e.number: e for e in episodes}
        loglines = "\n".join(f"Ep {e.number}: {e.logline}" for e in episodes)
        rows = []
        for i, p in enumerate(pool, 1):
            rng = ", ".join(f"{fmt(s.start)}-{fmt(s.end)}" for s in p.segments)
            rows.append(f"M{i} | ep {p.segments[0].episode} | {rng} | {p.hook[:140]} | {p.cliffhanger[:140]}")
        user = P.MONTAGE_USER.format(show=self.show, loglines=loglines, pool="\n".join(rows), n=self.cfg.montages)
        data, text = self._call("montages", P.MONTAGE_SYSTEM, user)
        if data is None:
            return []
        promos = self._validated(data, text, eps, None, "montage", "montages", P.MONTAGE_SYSTEM)
        promos = [p for p in promos if len(p.episodes) >= 2]
        for p in promos:
            rule_score(p, eps, self.cfg)
        return promos


def run(episodes: list[Episode], cfg, raw_dir: Path, show_name: str, log=print) -> tuple[list[Promo], dict]:
    be = LLMBackend(cfg, raw_dir, show_name, log)
    premise = episodes[0].logline if episodes else ""
    singles = []
    for ep in episodes:
        ps = be.episode(ep, premise)
        log(f"  ep {ep.number:>3}: {len(ps)} promos")
        singles += ps
    # pool for montages: best few per episode by the model's own scores
    by_ep: dict[int, list[Promo]] = {}
    for p in singles:
        by_ep.setdefault(p.segments[0].episode, []).append(p)
    pool = []
    for ps in by_ep.values():
        ps.sort(key=lambda p: -sum(p.llm_scores.values()))
        pool += ps[:cfg.pool_per_episode]
    pool.sort(key=lambda p: -sum(p.llm_scores.values()))
    montage = be.montages(episodes, pool[:60])
    log(f"  cross-episode: {len(montage)} promos")
    stats = {"model": be.model_name, "calls": be.client.calls, **be.client.usage}
    return singles + montage, stats
