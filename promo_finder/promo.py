"""Promo data model, validation/snapping, and rule scoring shared by every backend."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from .parse import Episode, fmt, to_sec
from . import features as F


@dataclass
class Segment:
    episode: int
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start

    def to_dict(self):
        return {"episode": self.episode, "start": fmt(self.start), "end": fmt(self.end),
                "start_s": round(self.start, 2), "end_s": round(self.end, 2)}


@dataclass
class Promo:
    segments: list[Segment]
    hook: str = ""
    cliffhanger: str = ""
    why: str = ""
    link: str = ""                             # montages: how the later moment follows from the earlier one
    backend: str = ""
    kind: str = "single"                       # single | montage
    llm_scores: dict = field(default_factory=dict)   # hook/cliffhanger/standalone, 1-5
    rule_scores: dict = field(default_factory=dict)  # 0-1
    checks: dict = field(default_factory=dict)       # montages: shared_cast per cut, cold_read verdict
    flags: list[str] = field(default_factory=list)
    score: float = 0.0
    rank: int = 0
    id: str = ""
    raw: dict = field(default_factory=dict)    # what the model originally returned, pre-snapping

    @property
    def duration(self) -> float:
        return sum(s.length for s in self.segments)

    @property
    def episodes(self) -> list[int]:
        return sorted({s.episode for s in self.segments})

    def to_dict(self):
        d = asdict(self)
        d["segments"] = [s.to_dict() for s in self.segments]
        d["duration"] = round(self.duration, 2)
        d["episodes"] = self.episodes
        return d


def parse_time(x) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    x = str(x).strip().strip("()[]")
    if ":" in x:
        parts = x.split(":")
        if len(parts) == 3:  # hh:mm:ss
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return to_sec(x)
    return float(x)


# ---------- snapping ----------

def snap_start(ep: Episode, t: float, max_back: float = 1.5) -> float:
    """Start on a shot boundary, as an editor would, without losing the hook line."""
    t = min(max(t, 0.0), ep.duration)
    starts = [b.start for b in ep.shots if b.start <= t + 0.05]
    if starts and t - max(starts) <= max_back:
        return max(starts)
    return t


def snap_end(ep: Episode, t: float, max_fwd: float = 3.0) -> float:
    """End on a shot boundary and never in the middle of a spoken line."""
    t = min(max(t, 0.0), ep.duration)
    e = t
    for _ in range(4):
        straddle = [b.end for b in ep.beats if b.kind == "DIALOGUE" and b.start < e - 0.05 < b.end - 0.05]
        if straddle:
            e = max(straddle)
        ends = [b.end for b in ep.shots if b.end >= e - 0.05]
        cand = min(ends) if ends else e
        if cand - t <= max_fwd:
            e = cand
        if not [b for b in ep.beats if b.kind == "DIALOGUE" and b.start < e - 0.05 < b.end - 0.05]:
            break
    return min(e, ep.duration)


def snap_end_inward(ep: Episode, t: float, floor: float) -> float:
    """Latest clean cut at or before t (shot end, never inside a spoken line); used to trim overlong promos."""
    dialogue = [b for b in ep.beats if b.kind == "DIALOGUE"]
    clean = lambda e: not any(b.start < e - 0.05 < b.end - 0.05 for b in dialogue)
    shots = [b.end for b in ep.shots if floor < b.end <= t + 0.05 and clean(b.end)]
    if shots:
        return max(shots)
    lines = [b.end for b in dialogue if floor < b.end <= t + 0.05]
    return max(lines) if lines else t


# ---------- rule scoring ----------

def describe_beat(b) -> str:
    if b.kind == "DIALOGUE":
        emo = f" ({b.emotion})" if b.emotion else ""
        return f'{b.speaker}{emo}: "{b.text}"'
    return f"{b.kind}: {b.text}"


def hook_text(ep: Episode, t0: float, window: float = 3.0) -> str:
    bs = [b for b in F.beats_in(ep, t0, t0 + window) if b.kind in ("DIALOGUE", "ACTION")]
    if not bs:
        bs = F.beats_in(ep, t0, t0 + 6)
    return " / ".join(describe_beat(b) for b in bs[:2])


def cliff_text(ep: Episode, t1: float, window: float = 4.0) -> str:
    bs = F.beats_in(ep, t1 - window, t1, by_start=False) or F.beats_in(ep, t1 - window, t1)
    if not bs:  # closing seconds are camera-only: describe the last story beat before the cut
        bs = [b for b in ep.beats if b.start <= t1][-1:]
    dl = [b for b in bs if b.kind == "DIALOGUE"]
    pick = dl[-1:] or bs[-1:]
    return " / ".join(describe_beat(b) for b in pick)


def rule_score(promo: Promo, eps: dict[int, Episode], cfg) -> None:
    first, last = promo.segments[0], promo.segments[-1]
    h, hn = F.hook_score(eps[first.episode], first.start, cfg.hook_window)
    c, cn = F.cliff_score(eps[last.episode], last.end)
    st_parts, sn = [], []
    for s in promo.segments:
        v, n = F.standalone_score(eps[s.episode], s.start, s.end)
        st_parts.append((v, s.length))
        sn += n
    tot = sum(w for _, w in st_parts) or 1.0
    st = sum(v * w for v, w in st_parts) / tot
    if len(promo.segments) > 1 and promo.kind == "single":
        st -= 0.05  # jump cuts inside one episode cost a little clarity
    promo.rule_scores = {
        "hook": round(h, 3), "cliffhanger": round(c, 3), "standalone": round(max(st, 0), 3),
        "duration_fit": round(F.duration_shape(promo.duration), 3),
        "notes": sorted(set(hn + cn + sn)),
    }


# ---------- validation ----------

def validate(promo: Promo, eps: dict[int, Episode], cfg) -> list[str]:
    """Snap segments in place; return hard errors (empty list = valid)."""
    errors = []
    if not promo.segments:
        return ["no segments"]
    requested = sum(max(s.end - s.start, 0.0) for s in promo.segments)  # what the proposer asked for
    fixed = []
    for i, s in enumerate(promo.segments):
        ep = eps.get(s.episode)
        if ep is None:
            errors.append(f"segment {i+1}: episode {s.episode} does not exist")
            continue
        if s.start >= ep.duration or s.end <= 0:
            errors.append(f"segment {i+1}: {fmt(s.start)}-{fmt(s.end)} is outside episode {s.episode} "
                          f"(length {fmt(ep.duration)})")
            continue
        a, b = snap_start(ep, s.start), snap_end(ep, s.end)
        if b - a < 1.5:
            errors.append(f"segment {i+1}: shorter than 1.5s after snapping")
            continue
        fixed.append(Segment(s.episode, a, b))
    # drop same-episode overlaps (keep order the model gave)
    clean = []
    for s in fixed:
        if any(c.episode == s.episode and s.start < c.end - 0.1 and s.end > c.start + 0.1 for c in clean):
            errors.append(f"segment in ep {s.episode} at {fmt(s.start)} overlaps an earlier segment")
            continue
        clean.append(s)
    if clean and sum(s.length for s in clean) > cfg.max_len and requested <= cfg.max_len + cfg.trim_tolerance:
        # small overshoot (from snapping, or a model landing at 60.8s): trim back to the latest clean cut.
        # Bigger overshoots are rejected: trimming them would silently cut the cliffhanger the model chose.
        last, ep = clean[-1], eps[clean[-1].episode]
        excess = sum(s.length for s in clean) - cfg.max_len
        e2 = snap_end_inward(ep, last.end - excess, floor=last.start + 1.5)
        if last.start + 1.5 <= e2 < last.end:
            clean[-1] = Segment(last.episode, last.start, e2)
            promo.flags.append("trimmed: end pulled in to fit 60s")
    promo.segments = clean
    if not clean:
        return errors or ["no valid segments"]
    d = promo.duration
    if d < cfg.min_len - 0.01:
        errors.append(f"total length {d:.1f}s is under {cfg.min_len:.0f}s")
    if d > cfg.max_len + 0.01:
        errors.append(f"total length {d:.1f}s is over {cfg.max_len:.0f}s")
    first = clean[0]
    h, _ = F.hook_score(eps[first.episode], first.start, cfg.hook_window)
    if h == 0.0:
        promo.flags.append("weak_open: no line or action in first 3s")
    return errors


# ---------- cross-episode continuity ----------

def check_shared_cast(promo: Promo, eps: dict[int, Episode], cast: set[str]) -> list[str]:
    """Every cut must carry at least one named character over from the previous segment.

    A stranger follows a jump across episodes by recognising a face; with nobody in common, two clips read
    as two unrelated shows. Records the characters shared at each cut in promo.checks["shared_cast"] and
    returns one error per cut that shares nobody.
    """
    present = [F.characters_in(eps[s.episode], s.start, s.end, cast) for s in promo.segments]
    shared, errors = [], []
    for i in range(len(present) - 1):
        common = sorted(present[i] & present[i + 1])
        shared.append(common)
        if not common:
            who = lambda x: ", ".join(sorted(x)) or "no named character"
            errors.append(f"cut {i + 1}->{i + 2}: no named character carries over "
                          f"(segment {i + 1}: {who(present[i])}; segment {i + 2}: {who(present[i + 1])})")
    promo.checks["shared_cast"] = shared
    return errors


def clip_script(promo: Promo, eps: dict[int, Episode], max_cam_chars: int = 90) -> str:
    """What a viewer sees and hears, in playback order, with no episode numbers, timestamps or loglines."""
    parts = []
    for s in promo.segments:
        lines = []
        for b in eps[s.episode].blocks:
            if not (s.start - 0.05 <= b.start < s.end - 0.05):
                continue
            if b.kind == "CAMERA":
                lines.append(f"SHOT: {b.text[:max_cam_chars]}")
            else:
                lines.append(describe_beat(b))
        parts.append("\n".join(lines))
    return "\n--- CUT ---\n".join(parts)
