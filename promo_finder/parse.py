"""Parse Kuku microdrama screenplays into timestamped blocks.

Format (one file per episode):
    EPISODE NUMBER: Episode 1
    EPISODE LOGLINE: ...
    CONTINUITY NOTES: ...
    SCENE 01
    Scene heading: INT. CATHEDRAL – DAY
    Scene start: 00:00.00
    ...
    **CAMERA:** | **ACTION:** | **SOUND FX:** | **SPEAKER (Emotion, Tags):**
    Start: (00:11.30) | End: (00:13.10)
    <text, possibly several lines>
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

BLOCK_RE = re.compile(
    r"^\*\*(?P<label>[^*\n]+?):\*\*\s*\n"
    r"Start: \((?P<s>\d+:\d+(?:\.\d+)?)\) \| End: \((?P<e>\d+:\d+(?:\.\d+)?)\)\s*\n"
    r"(?P<text>.*?)(?=^\*\*[^*\n]+:\*\*\s*$|^SCENE \d+|^--- SCRIPT END|\Z)",
    re.M | re.S,
)
SCENE_RE = re.compile(r"^SCENE (\d+)\s*\n(?:scene heading|SCENE HEADING): (.+)$", re.M | re.I)
NON_DIALOGUE = {"CAMERA", "ACTION", "SOUND FX"}


def to_sec(ts: str) -> float:
    m, s = ts.split(":")
    return int(m) * 60 + float(s)


def fmt(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m):02d}:{s:05.2f}"


@dataclass
class Block:
    idx: int
    start: float
    end: float
    kind: str           # CAMERA | ACTION | SFX | DIALOGUE
    speaker: str        # only for DIALOGUE
    emotion: str        # parenthetical tags, e.g. "Cold, Commanding"
    text: str
    scene: int
    heading: str


@dataclass
class Episode:
    number: int
    title: str
    logline: str
    continuity: str
    duration: float
    blocks: list[Block] = field(default_factory=list)
    path: str = ""

    @property
    def shots(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "CAMERA"]

    @property
    def beats(self) -> list[Block]:
        """Story beats: everything except camera directions."""
        return [b for b in self.blocks if b.kind != "CAMERA"]

    def to_dict(self):
        return asdict(self)


def _split_label(label: str) -> tuple[str, str, str]:
    label = label.strip()
    if label in NON_DIALOGUE:
        return ("SFX" if label == "SOUND FX" else label), "", ""
    # "LADY MARA (Confused, Breathless)" / "MATRIARCH (O.S.) (Firm)"
    parens = re.findall(r"\(([^)]*)\)", label)
    speaker = re.sub(r"\s*\([^)]*\)", "", label).strip()
    tags = [p for p in parens if p.upper() not in {"O.S.", "V.O.", "CONT'D", "O.C."}]
    if any(p.upper() in {"O.S.", "V.O."} for p in parens):
        speaker += " (" + ",".join(p for p in parens if p.upper() in {"O.S.", "V.O."}) + ")"
    return "DIALOGUE", speaker, ", ".join(tags)


def parse_episode(path: str | Path) -> Episode:
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")

    def header(name):
        m = re.search(rf"^{name}:\s*(.+)$", raw, re.M)
        return m.group(1).strip() if m else ""

    title = header("EPISODE NUMBER")
    num_m = re.search(r"(\d+)", title) or re.search(r"episode-(\d+)", path.name)
    number = int(num_m.group(1)) if num_m else 0

    # scene boundaries by character offset
    scenes = [(m.start(), int(m.group(1)), m.group(2).strip()) for m in SCENE_RE.finditer(raw)]

    def scene_at(pos):
        cur = (1, "")
        for off, n, h in scenes:
            if off <= pos:
                cur = (n, h)
        return cur

    blocks = []
    for m in BLOCK_RE.finditer(raw):
        kind, speaker, emotion = _split_label(m.group("label"))
        text = " ".join(l.strip() for l in m.group("text").strip().splitlines() if l.strip())
        text = text.strip().strip('"').strip("“”")
        sc, head = scene_at(m.start())
        blocks.append(Block(len(blocks), to_sec(m.group("s")), to_sec(m.group("e")),
                            kind, speaker, emotion, text, sc, head))
    blocks.sort(key=lambda b: (b.start, b.idx))
    for i, b in enumerate(blocks):
        b.idx = i
    duration = max((b.end for b in blocks), default=0.0)
    return Episode(number, title, header("EPISODE LOGLINE"), header("CONTINUITY NOTES"),
                   duration, blocks, str(path))


def load_show(folder: str | Path) -> list[Episode]:
    folder = Path(folder)
    files = sorted(p for p in folder.rglob("*.txt") if not p.name.startswith("."))
    eps = [parse_episode(p) for p in files]
    eps.sort(key=lambda e: e.number)
    return eps


def transcript(ep: Episode, include_camera: bool = False, max_cam_chars: int = 90) -> str:
    """Compact, timestamped transcript for LLM prompts."""
    lines = []
    for b in ep.blocks:
        t = f"[{fmt(b.start)}-{fmt(b.end)}]"
        if b.kind == "DIALOGUE":
            emo = f" ({b.emotion})" if b.emotion else ""
            lines.append(f'{t} {b.speaker}{emo}: "{b.text}"')
        elif b.kind == "CAMERA":
            if include_camera:
                lines.append(f"{t} SHOT: {b.text[:max_cam_chars]}")
        else:
            lines.append(f"{t} {b.kind}: {b.text}")
    return "\n".join(lines)


def parse_episode_filter(s: str) -> set[int] | None:
    """'1-10, 15' -> {1..10, 15}; blank -> None (all episodes). Raises ValueError on anything else.

    Accepts en/em dashes and an optional 'ep'/'episode' prefix, since phones and docs auto-format text."""
    s = s.strip().lower().replace("–", "-").replace("—", "-")
    if not s:
        return None
    want = set()
    for part in filter(None, (p.strip() for p in s.split(","))):
        m = re.fullmatch(r"(?:ep(?:isode)?s?\.?\s*)?(\d+)(?:\s*-\s*(?:ep(?:isode)?\.?\s*)?(\d+))?", part)
        if not m:
            raise ValueError(part)
        lo, hi = sorted((int(m.group(1)), int(m.group(2) or m.group(1))))
        if hi - lo > 10_000:  # a typo like 1-999999999 would otherwise build a huge set and hang the app
            raise ValueError(part)
        want.update(range(lo, hi + 1))
    return want
