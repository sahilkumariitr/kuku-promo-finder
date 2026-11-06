"""Live pipeline map: which stage of the README flowchart is running, rendered as a Graphviz DOT string.

Standard library only, so the backends can report progress without depending on any UI. A backend calls
`progress(node, state, note)`; the Streamlit app passes a `Pipeline` as that callback and redraws
`Pipeline.to_dot()` whenever it changes.
"""
from __future__ import annotations

import time
from html import escape

PENDING, ACTIVE, DONE, SKIPPED, FALLBACK, ERROR = "pending", "active", "done", "skipped", "fallback", "error"
RAN = (ACTIVE, DONE, FALLBACK, ERROR)

# id: (title, who does it)  -- same stages as the README flowchart
NODES = {
    "input": ("Episode screenplays", ""),
    "parse": ("Parse", "code"),
    "backend": ("Backend", ""),
    "propose": ("Propose promos\nper episode", "LLM"),
    "rules": ("Score every shot-aligned\n20-60 s window", "code"),
    "validate": ("Validate and snap", "code"),
    "repair": ("Repair once", "LLM"),
    "montages": ("Cross-episode\nmontages", "LLM"),
    "rank": ("Score and rank", "code"),
    "output": ("Ranked table,\nJSON / CSV", ""),
    "editor": ("Editor review", "human"),
}
# (from, to, label, extra attributes)
EDGES = [
    ("input", "parse", "", ""),
    ("parse", "backend", "", ""),
    ("backend", "propose", "hybrid", ""),
    ("backend", "rules", "rules only", ""),
    ("propose", "validate", "", ""),
    ("validate", "repair", "errors", ""),
    ("repair", "validate", "", ""),
    ("validate", "montages", "", ""),
    ("validate", "rules", "no valid promo:\nrules fallback", 'style="dashed", constraint=false'),
    ("rules", "rank", "", ""),
    ("montages", "rank", "", ""),
    ("rank", "output", "", ""),
    ("output", "editor", "", ""),
]
ICON = {PENDING: "", ACTIVE: "▶ ", DONE: "✓ ", SKIPPED: "", FALLBACK: "↺ ", ERROR: "✗ "}
STYLE = {  # light fills with dark text read well on both light and dark Streamlit themes
    PENDING: 'fillcolor="#f3f4f6", color="#9ca3af", fontcolor="#4b5563"',
    ACTIVE: 'fillcolor="#fde68a", color="#d97706", fontcolor="#111827", penwidth=2.5',
    DONE: 'fillcolor="#bbf7d0", color="#16a34a", fontcolor="#111827"',
    SKIPPED: 'fillcolor="#e5e7eb", color="#9ca3af", fontcolor="#6b7280", style="filled,rounded,dashed"',
    FALLBACK: 'fillcolor="#fed7aa", color="#ea580c", fontcolor="#111827", penwidth=2',
    ERROR: 'fillcolor="#fecaca", color="#dc2626", fontcolor="#111827", penwidth=2.5',
}


def noop(*_args, **_kwargs) -> None:
    """Default progress callback for callers that don't show a map (CLI, tests)."""


class Pipeline:
    """Holds each stage's state and a short note; call it like `pipe("propose", "active", "request 2/6")`."""

    def __init__(self, on_change=None, min_interval: float = 0.25):
        self.state = {n: PENDING for n in NODES}
        self.note = {n: "" for n in NODES}
        self.on_change = on_change
        self.min_interval = min_interval  # note-only updates are throttled; state changes always redraw
        self._last = 0.0

    def __call__(self, node: str, state: str | None = None, note: str | None = None) -> None:
        changed = False
        if state is not None and state != self.state[node]:
            if state == ACTIVE:  # one stage runs at a time: whatever was running has finished
                for n, s in self.state.items():
                    if s == ACTIVE and n != node:
                        self.state[n] = DONE
            self.state[node] = state
            changed = True
        if note is not None and note != self.note[node]:
            self.note[node] = note
        else:
            note = None
        now = time.monotonic()
        if self.on_change and (changed or (note is not None and now - self._last >= self.min_interval)):
            self._last = now
            self.on_change(self)

    def set_mode(self, hybrid: bool) -> None:
        """Grey out the branch the chosen backend doesn't take, before anything runs."""
        if hybrid:
            self.state["rules"], self.note["rules"] = SKIPPED, "only used as a fallback"
        else:
            for n in ("propose", "validate", "repair", "montages"):
                self.state[n], self.note[n] = SKIPPED, "hybrid only"
        self._redraw()

    def fail(self, note: str, node: str | None = None) -> None:
        node = node or next((n for n, s in self.state.items() if s == ACTIVE), "backend")
        self.state[node], self.note[node] = ERROR, note
        self._redraw()

    def _redraw(self) -> None:
        if self.on_change:
            self._last = time.monotonic()
            self.on_change(self)

    def _label(self, node: str) -> str:
        title, who = NODES[node]
        state, note = self.state[node], self.note[node]
        lines = [f"<b>{escape(ICON[state])}{escape(title.splitlines()[0])}</b>"]
        lines += [f"<b>{escape(t)}</b>" for t in title.splitlines()[1:]]
        small = " · ".join(x for x in (who, note) if x)
        if small:
            lines.append(f'<font point-size="9">{escape(small)}</font>')
        return "<" + "<br/>".join(lines) + ">"

    def to_dot(self, rankdir: str = "TB") -> str:
        out = ['digraph pipeline {',
               f'  rankdir={rankdir}; bgcolor="transparent"; nodesep=0.3; ranksep=0.28; pad=0.1;',
               '  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=11, margin="0.12,0.06"];',
               '  edge [fontname="Helvetica", fontsize=9, fontcolor="#9ca3af", arrowsize=0.7];']
        for n in NODES:
            shape = ', shape=diamond, margin="0.02,0.02"' if n == "backend" else ""
            out.append(f'  {n} [label={self._label(n)}, {STYLE[self.state[n]]}{shape}];')
        out.append("  {rank=same; propose; rules}  {rank=same; validate; repair}")  # branches side by side
        for a, b, label, extra in EDGES:
            lit = self.state[a] in RAN and self.state[b] in RAN
            if (a, b) == ("backend", "rules"):  # the "rules only" branch, not the fallback path
                lit = lit and self.state[b] != FALLBACK
            colour = 'color="#16a34a", penwidth=2' if lit else 'color="#9ca3af"'
            attrs = ", ".join(x for x in (colour, f'label="{label}"'.replace("\n", "\\n") if label else "", extra) if x)
            out.append(f"  {a} -> {b} [{attrs}];")
        out.append("}")
        return "\n".join(out)
