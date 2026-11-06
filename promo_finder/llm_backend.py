"""LLM backend: the model proposes promos; code validates, snaps, repairs and scores them.

Episodes are sent in batches (cfg.episodes_per_call) because free tiers cap requests per day, not tokens:
one show is ~6 requests instead of ~51. Successful responses are cached by prompt hash, so a run that stops
(quota, network, Ctrl+C) resumes on the next run without re-spending requests. If the model gives no valid
promo for an episode, the rules backend's best pick is used and flagged.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .parse import Episode, fmt, transcript
from .promo import Promo, Segment, parse_time, validate, rule_score
from .llm import LLMClient, LLMError, extract_json
from . import prompts as P
from . import rules_backend


class StopLLM(Exception):
    """The provider will keep refusing (bad key, retired model, daily quota): stop calling it."""

    def __init__(self, msg: str, kind: str):
        super().__init__(msg)
        self.kind = kind


ADVICE = {
    "auth": "Check the API key (in .env or the provider's key variable).",
    "model": "The model name is wrong or retired for this key: pass --model NAME or set LLM_MODEL.",
    "quota_day": "The daily free-tier request quota is used up for every model in the chain. Finished batches "
                 "are cached: run the same command after the quota resets (Gemini: midnight Pacific time) "
                 "and it resumes where it stopped.",
}


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


def _to_promos(data: dict, default_ep: int | None, kind: str, max_items: int) -> list[Promo]:
    out = []
    for item in (data.get("promos") or [])[:max_items]:
        if not isinstance(item, dict):
            continue
        item_ep = item.get("episode", default_ep)
        segs = []
        for s in item.get("segments") or []:
            try:
                ep = s.get("episode", item_ep)
                segs.append(Segment(int(ep) if ep is not None else None, parse_time(s["start"]), parse_time(s["end"])))
            except (KeyError, ValueError, TypeError, AttributeError):
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
            raise SystemExit(f"No API key for provider '{cfg.provider}'. Put it in .env (e.g. GEMINI_API_KEY=...), "
                             f"set LLM_API_KEY, or run with --backend rules.")
        self.client = LLMClient(base, cfg.llm_models(), key, cfg.temperature, cfg.max_retries,
                                min_interval=cfg.pacing(), overload_retries=cfg.overload_retries,
                                retry_base=cfg.retry_base, log=log)
        self.cfg, self.raw_dir, self.show, self.log = cfg, raw_dir, show_name, log
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = raw_dir.parent / "cache"
        if cfg.cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model
        self.last_error = ""
        self.cache_hits = 0
        self.answered_by: dict[str, int] = {}  # live + cached answers, by model
        self._logged: set[str] = set()
        self._answer_model: str | None = None

    # ---------- one request (cached, logged) ----------

    def _call(self, tag: str, system: str, user: str) -> tuple[dict | None, str]:
        key = hashlib.sha256((system + "\0" + user).encode()).hexdigest()[:24]
        cpath = self.cache_dir / f"{key}.json"
        rec = {"system": system, "user": user}
        stop = None
        if self.cfg.cache and cpath.is_file():
            hit = json.loads(cpath.read_text(encoding="utf-8"))
            rec.update(model=hit["model"], response=hit["response"], cached=True)
            text = hit["response"]
            data = extract_json(text)
            self.cache_hits += 1
        else:
            try:
                text = self.client.chat(system, user)
                rec.update(model=self.client.last_model, response=text)
                data = extract_json(text)
                if self.cfg.cache:
                    cpath.write_text(json.dumps({"model": self.client.last_model, "response": text},
                                                ensure_ascii=False), encoding="utf-8")
            except (LLMError, ValueError, json.JSONDecodeError) as e:
                rec.update(model=self.client.model, error=str(e))
                data, text = None, rec.get("response", "")
                if isinstance(e, LLMError) and e.fatal:
                    stop = StopLLM(f"{_short(str(e))}\n{ADVICE[e.kind]}", e.kind)
        if rec.get("response") is not None and not rec.get("error"):
            self.answered_by[rec["model"]] = self.answered_by.get(rec["model"], 0) + 1
        mode = "a" if tag in self._logged else "w"  # fresh file per run; repair rounds append
        self._logged.add(tag)
        with open(self.raw_dir / f"{tag}.json", mode, encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.last_error = rec.get("error", "")
        self._answer_model = rec.get("model")
        if stop is not None:
            raise stop
        return data, text

    # ---------- validation + one repair round ----------

    def _check(self, promos, eps, kind):
        good, errs = [], []
        for i, p in enumerate(promos, 1):
            label = f"promo {i}" + (f" (episode {p.raw.get('episode')})" if p.raw.get("episode") is not None else "")
            e = validate(p, eps, self.cfg)
            if not e and kind == "single" and len(p.episodes) > 1:
                e = ["segments come from different episodes; a promo here must use one episode"]
            if e:
                errs.append(f"{label}: " + "; ".join(e))
            else:
                good.append(p)
        return good, errs

    def _validated(self, data, text, eps, default_ep, kind, tag, system, user, max_items):
        promos = _to_promos(data or {}, default_ep, kind, max_items)
        good, errs = self._check(promos, eps, kind)
        # A repair costs a whole request, and free tiers allow ~20/day: only spend one when an episode
        # (or the montage stage) would otherwise end up with nothing. Stray bad promos are just dropped.
        if kind == "single":
            uncovered = sorted(set(eps) - {p.segments[0].episode for p in good})
        else:
            uncovered = [] if good else ["montage"]
        if errs and uncovered and self.cfg.repair_rounds > 0 and data is not None:
            self.log(f"    {tag}: {len(errs)} invalid promo(s) leave {uncovered} uncovered; asking for a repair")
            fixed_data, fixed_text = self._call(tag, system, P.REPAIR_USER.format(
                original=user, errors="\n".join(errs), previous=text))
            if fixed_data is not None:
                good2, errs2 = self._check(_to_promos(fixed_data, default_ep, kind, max_items), eps, kind)
                if len(good2) >= len(good):
                    good, errs = good2, errs2
        if errs:
            self.log(f"    {tag}: dropped {len(errs)} invalid promo(s), e.g. {errs[0][:110]}")
        return good

    # ---------- stages ----------

    def batch(self, batch: list[Episode], premise: str) -> list[Promo]:
        eps = {e.number: e for e in batch}
        blocks = "\n\n".join(P.EPISODE_BLOCK.format(
            number=e.number, duration=fmt(e.duration), logline=e.logline, continuity=e.continuity,
            transcript=transcript(e, self.cfg.include_camera)) for e in batch)
        user = P.BATCH_USER.format(show=self.show, premise=premise, n=len(batch),
                                   numbers=", ".join(str(e.number) for e in batch), episodes=blocks)
        a, b = batch[0].number, batch[-1].number
        tag = f"ep_{a:03d}" if a == b else f"ep_{a:03d}-{b:03d}"
        data, text = self._call(tag, P.EPISODE_SYSTEM, user)
        if data is None:
            self.log(f"    {tag}: model call failed: {_short(self.last_error)}")
            return []
        default_ep = a if len(batch) == 1 else None
        promos = self._validated(data, text, eps, default_ep, "single", tag, P.EPISODE_SYSTEM, user,
                                 max_items=4 * len(batch))
        for p in promos:
            rule_score(p, eps, self.cfg)
            p.raw["model"] = self._answer_model
        return promos

    def montages(self, episodes: list[Episode], pool: list[Promo]) -> list[Promo]:
        if self.cfg.montages <= 0 or len({p.segments[0].episode for p in pool}) < 2:
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
        promos = self._validated(data, text, eps, None, "montage", "montages", P.MONTAGE_SYSTEM, user,
                                 max_items=self.cfg.montages + 2)
        promos = [p for p in promos if len(p.episodes) >= 2]
        for p in promos:
            rule_score(p, eps, self.cfg)
        return promos


def run(episodes: list[Episode], cfg, raw_dir: Path, show_name: str, log=print) -> tuple[list[Promo], dict]:
    be = LLMBackend(cfg, raw_dir, show_name, log)
    premise = episodes[0].logline if episodes else ""
    k = max(1, int(cfg.episodes_per_call))
    batches = [episodes[i:i + k] for i in range(0, len(episodes), k)]
    planned = len(batches) + (1 if cfg.montages > 0 else 0)
    log(f"  models: {' -> '.join(be.client.models)} | {len(batches)} request(s) of up to {k} episodes"
        f"{' + 1 montage request' if cfg.montages > 0 else ''} (+ at most one repair each), "
        f"pacing {be.client.min_interval:.0f}s")
    stopped: StopLLM | None = None
    singles: list[Promo] = []
    fallback_eps: list[int] = []
    for batch in batches:
        ps: list[Promo] = []
        if stopped is None:
            try:
                ps = be.batch(batch, premise)
            except StopLLM as e:
                stopped = e
                log(f"  STOPPED calling the model: {e}")
        for ep in batch:
            mine = [p for p in ps if p.segments[0].episode == ep.number]
            if not mine and cfg.rules_fallback:
                mine = rules_backend.episode_promos(ep, cfg)[:1]
                for p in mine:
                    p.backend = "rules-fallback"
                    p.flags.append("fallback: rules backend pick (no valid LLM promo for this episode)")
                fallback_eps.append(ep.number)
            log(f"  ep {ep.number:>3}: {len(mine)} promo(s)" + ("  [rules fallback]" if
                                                                 mine and mine[0].backend == "rules-fallback" else ""))
            singles += mine

    if stopped is not None and not any(p.backend == "llm" for p in singles):
        raise SystemExit(f"Stopping: the model endpoint refused every request.\n{stopped}")

    montage: list[Promo] = []
    if stopped is None:
        by_ep: dict[int, list[Promo]] = {}
        for p in singles:
            if p.backend == "llm":
                by_ep.setdefault(p.segments[0].episode, []).append(p)
        pool = []
        for ps in by_ep.values():
            ps.sort(key=lambda p: -sum(p.llm_scores.values()))
            pool += ps[:cfg.pool_per_episode]
        pool.sort(key=lambda p: -sum(p.llm_scores.values()))
        try:
            montage = be.montages(episodes, pool[:60])
        except StopLLM as e:
            stopped = e
            log(f"  STOPPED before montages: {e}")
        log(f"  cross-episode: {len(montage)} promo(s)")

    c = be.client
    stats = {"model": be.model_name, "models_used": be.answered_by, "live_requests_by_model": c.by_model, "calls": c.calls, "http_requests": c.attempts,
             "cache_hits": be.cache_hits, "planned_requests": planned, **c.usage,
             "rules_fallback_episodes": fallback_eps}
    if stopped is not None:
        stats["incomplete"] = str(stopped)
        log("  Run is INCOMPLETE (see above). Rerun the same command later: finished requests come from cache.")
    return singles + montage, stats
