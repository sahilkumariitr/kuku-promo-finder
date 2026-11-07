"""Cheap, explainable signals computed from the screenplay itself.

These are used three ways:
  1. the rules backend scores every candidate window with them;
  2. the validator uses them to sanity-check what an LLM proposes;
  3. the ranker blends them with LLM scores so one bad LLM call can't dominate.
Word lists were built from the emotion tags that actually occur in the pack.
"""
from __future__ import annotations

import re
from .parse import Block, Episode

HIGH = set("""screaming shouting furious outraged enraged hysterical desperate sobbing weeping
terrified panicked horrified shocked gasping venomous cruel menacing threatening chilling sinister
gloating mocking taunting smug spiteful scornful vicious ruthless deadly booming thunderous accusing
accusatory frantic begging heartbroken broken shattered stunned incredulous defiant fierce triumphant
condemning malicious vindictive murderous livid seething roaring wailing crying piercing callous
sneering derisive contemptuous savage feral unhinged agonized anguished""".split())
MID = set("""cold stern commanding urgent sharp harsh bitter dismissive condescending arrogant demanding
trembling pleading tearful alarmed intense warning disdainful scoffing resolute steely grim dark
ominous authoritative firm unyielding choked shaken indignant calculating scheming hard icy
steady powerful unwavering unflinching decisive final grave solemn""".split())
LOW = set("""gentle soft quiet tender warm calm reflective reassuring loving relieved humble sincere
earnest amused curious concerned intimate murmuring softly hushed measured subdued""".split())

PHYSICAL = re.compile(r"\b(slap\w*|strik\w+|struck|punch\w*|grab\w*|seiz\w+|shov\w+|throw\w*|threw|hurl\w*|"
                      r"drag\w*|chain\w*|shackle\w*|blood\w*|burn\w*|fire|flame\w*|blaze|explo\w+|collaps\w+|"
                      r"knife|dagger|sword|blade|crop|whip\w*|kneel\w*|knelt|kiss\w*|scream\w*|smash\w*|"
                      r"crash\w*|gavel|gasp\w*|faint\w*|choke\w*|stab\w*|shatter\w*|rip\w*|tear\w* (?:open|apart)|"
                      r"poison\w*|arrest\w*|seize|guards? (?:storm|burst|seize)|burst\w*|storm\w*)\b", re.I)
REVEAL = re.compile(r"\b(duke|duchess|married|marry|marrying|wedding|bride|husband|wife|poison\w*|arsenic|"
                    r"witch\w*|execut\w+|hang\w*|dead|alive|son|heir|pregnan\w+|betray\w*|traitor|arrest\w*|"
                    r"guilty|truth|secret|ring|crest|title|disown\w*|inherit\w*|kill\w*|die|murder\w*|"
                    r"liar|lie|lied|never|sister|mother|father)\b", re.I)
CLIFF_SFX = re.compile(r"cliffhanger|swell\w*|ominous|sting|thunder|boom|crash|gasp|cut to black|"
                       r"dramatic|explosion|shatter|scream|silence", re.I)
OPEN_NEEDS_CONTEXT = re.compile(r"^(?i:and|but|so|then|because|or|also|still|yes|no|okay|well)\b|^\.\.\.|^…|^[a-z]")


def emotion_intensity(emotion: str) -> float:
    words = [w.strip().lower() for w in re.split(r"[,/;]", emotion) if w.strip()]
    toks = set()
    for w in words:
        toks.update(w.split())
    if toks & HIGH:
        return 1.0
    if toks & MID:
        return 0.6
    if toks and toks <= LOW | {"and", "with", "very"}:
        return 0.1
    return 0.35


def beat_intensity(b: Block) -> float:
    if b.kind == "DIALOGUE":
        x = emotion_intensity(b.emotion)
        if "!" in b.text:
            x += 0.15
        if "?" in b.text:
            x += 0.08
        if REVEAL.search(b.text):
            x += 0.12
        return min(x, 1.2)
    if b.kind in ("ACTION", "SFX"):
        return 0.9 if PHYSICAL.search(b.text) else 0.3
    return 0.0  # camera directions carry no story weight on their own


def beats_in(ep: Episode, t0: float, t1: float, *, by_start=True) -> list[Block]:
    out = []
    for b in ep.beats:
        key = b.start if by_start else b.end
        if t0 - 1e-6 <= key <= t1 + 1e-6:
            out.append(b)
    return out


def hook_score(ep: Episode, t0: float, window: float = 3.0) -> tuple[float, list[str]]:
    """How strongly the first `window` seconds stop a scrolling viewer."""
    notes = []
    first = beats_in(ep, t0, t0 + window)
    first = [b for b in first if b.kind in ("DIALOGUE", "ACTION", "SFX")]
    if not any(b.kind in ("DIALOGUE", "ACTION") for b in first):
        return 0.0, ["no line or action in first 3s"]
    s = max(beat_intensity(b) for b in first)
    lead = next((b for b in first if b.kind == "DIALOGUE"), None)
    if lead is not None:
        if len(lead.text.split()) <= 9:
            s += 0.1  # short punchy lines read faster on a scroll
        prev = [b for b in ep.beats if b.kind == "DIALOGUE" and b.end <= lead.start + 0.05]
        prev = prev[-1] if prev else None
        continues = prev is not None and lead.start - prev.end < 1.5 and \
            prev.text.rstrip().endswith(("...", "…", "—", "-", ","))
        reaction = lead.text.lstrip().startswith("[")  # "[scoffs] ..." reacts to something unseen
        if OPEN_NEEDS_CONTEXT.search(lead.text) or continues or reaction:
            s *= 0.5  # a reply/continuation confuses a stranger however intense it is
            notes.append("opens mid-conversation")
    return max(min(s, 1.2), 0.0) / 1.2, notes


def cliff_score(ep: Episode, t1: float, window: float = 4.0) -> tuple[float, list[str]]:
    """How unresolved the cut point feels."""
    notes = []
    last = beats_in(ep, t1 - window, t1, by_start=False) or beats_in(ep, t1 - window, t1)
    s = max((beat_intensity(b) for b in last), default=0.0)
    if ep.duration - t1 <= 1.5:
        s += 0.35
        notes.append("uses the episode's own cliffhanger ending")
    if any(b.kind == "SFX" and CLIFF_SFX.search(b.text) for b in last):
        s += 0.25
        notes.append("music/SFX sting at the cut")
    last_line = next((b for b in reversed(last) if b.kind == "DIALOGUE"), None)
    if last_line is not None:
        txt = last_line.text.rstrip()
        if txt.endswith("?") or txt.endswith("...") or txt.endswith("…"):
            s += 0.15
            notes.append("cuts on an unanswered line")
        elif txt.endswith("!"):
            s += 0.08
    return min(s, 1.6) / 1.6, notes


def standalone_score(ep: Episode, t0: float, t1: float) -> tuple[float, list[str]]:
    beats = beats_in(ep, t0, t1)
    if not beats:
        return 0.0, ["empty window"]
    speakers = {b.speaker.split(" (")[0] for b in beats if b.kind == "DIALOGUE"}
    mean_int = sum(beat_intensity(b) for b in beats) / len(beats)
    s = 0.35 + 0.35 * min(mean_int, 1.0)
    if 2 <= len(speakers) <= 4:
        s += 0.2  # visible conflict between people reads without backstory
    notes = []
    if len(speakers) > 5:
        s -= 0.15
        notes.append("many speakers")
    if len(speakers) == 0:
        s -= 0.2
        notes.append("no dialogue")
    return max(min(s, 1.0), 0.0), notes


# ---------- who is on screen (continuity across a cross-episode cut) ----------

# Background roles that recur in many episodes without being the same person; they must not count as the
# "same character" that carries a viewer across a cut. Numbered variants ("GUARD 2") are generic too.
GENERIC_ROLES = set("""guest guests guard guards crowd all everyone both narrator voice voices announcer
waiter waitress servant servants maid butler driver staff worker workers bystander bystanders onlooker
onlookers reporter reporters officer officers policeman soldier soldiers man woman men women boy girl
old young male female""".split())
HONORIFICS = set("lady lord sir mr mr. mrs mrs. ms ms. miss dr dr. madam king queen prince princess duke "
                 "duchess".split())


def character_name(speaker: str) -> str:
    """'LADY MARA (O.S.)' -> 'LADY MARA'."""
    return re.sub(r"\s*\([^)]*\)", "", speaker or "").strip().upper()


def is_generic(name: str) -> bool:
    toks = name.lower().split()
    return not toks or all(t in GENERIC_ROLES or t.strip("#").isdigit() for t in toks)


def show_cast(episodes: list[Episode]) -> set[str]:
    """Named speaking characters across the whole show (background roles excluded)."""
    names = {character_name(b.speaker) for e in episodes for b in e.blocks if b.kind == "DIALOGUE"}
    return {n for n in names if n and not is_generic(n)}


def _name_pattern(name: str) -> re.Pattern:
    """Match 'LADY MARA' as 'Lady Mara' or, after an honorific, just 'Mara' in action/camera text."""
    toks = name.split()
    alts = [re.escape(name)]
    if len(toks) > 1 and toks[0].lower() in HONORIFICS and len(toks[-1]) >= 3:
        alts.append(re.escape(toks[-1]))
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.I)


def characters_in(ep: Episode, t0: float, t1: float, cast: set[str]) -> set[str]:
    """Named characters a viewer sees or hears in [t0, t1): speakers, plus cast named in action/camera lines."""
    blocks = [b for b in ep.blocks if t0 - 0.05 <= b.start < t1 - 0.05]
    present = {character_name(b.speaker) for b in blocks if b.kind == "DIALOGUE"} & cast
    text = " ".join(b.text for b in blocks if b.kind in ("ACTION", "CAMERA"))
    present |= {n for n in cast - present if _name_pattern(n).search(text)}
    return present


def duration_shape(d: float) -> float:
    """1.0 in the 25-45s sweet spot, tapering to 0.85 at 20s/60s."""
    if 25 <= d <= 45:
        return 1.0
    if d < 25:
        return 0.85 + 0.15 * (d - 20) / 5
    return 1.0 - 0.15 * (d - 45) / 15
