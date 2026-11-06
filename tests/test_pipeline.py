"""Pipeline tests.

By default they run on the synthetic show in tests/fixtures (original content, safe to commit).
Point PROMO_DATA at a real show folder to run the same checks on it:
    PROMO_DATA=path/to/show pytest -q
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from promo_finder.config import Config
from promo_finder.parse import load_show
from promo_finder.promo import Promo, Segment, validate
from promo_finder import rules_backend
from promo_finder.rank import rank

ROOT = Path(__file__).resolve().parents[1]
DATA = os.environ.get("PROMO_DATA") or str(ROOT / "tests" / "fixtures" / "synthetic_show")


@pytest.fixture(scope="module")
def eps():
    return load_show(DATA)


def test_parse_every_block(eps):
    assert eps
    for e in eps:
        assert e.blocks, e.number
        assert all(b.end >= b.start for b in e.blocks)
        assert e.duration > 20
        assert {"CAMERA", "DIALOGUE"} <= {b.kind for b in e.blocks}


def test_validator_snaps_and_rejects(eps):
    cfg, e = Config(), eps[0]
    p = Promo([Segment(e.number, 3.2, 30.0)])
    assert validate(p, {e.number: e}, cfg) == []
    seg = p.segments[0]
    assert seg.start in {b.start for b in e.shots} or seg.start == 3.2       # snapped to a shot start
    dialogue = [b for b in e.beats if b.kind == "DIALOGUE"]
    assert not any(b.start < seg.end - 0.05 < b.end - 0.05 for b in dialogue)  # never cut mid-line
    assert any("over" in err for err in validate(Promo([Segment(e.number, 0.0, 90.0)]), {e.number: e}, cfg))
    assert validate(Promo([Segment(999, 0.0, 30.0)]), {e.number: e}, cfg)


def test_rules_backend_respects_brief(eps):
    cfg = Config()
    promos = rank(rules_backend.run(eps, cfg, log=lambda *a: None), cfg)
    assert promos
    for p in promos:
        assert cfg.min_len - 0.5 <= p.duration <= cfg.max_len + 0.5
        assert p.hook and p.cliffhanger and p.why
    assert [p.rank for p in promos] == list(range(1, len(promos) + 1))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_llm_backend_end_to_end(tmp_path):
    """Full LLM path (proposal -> validation -> repair -> montage -> ranking) against a local fake API."""
    port = _free_port()
    srv = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_llm_server.py"), str(port)])
    try:
        time.sleep(0.8)
        env = {**os.environ, "LLM_BASE_URL": f"http://127.0.0.1:{port}/v1/", "LLM_MODEL": "fake"}
        out = tmp_path / "out"
        subprocess.run([sys.executable, "-m", "promo_finder.run", "--input", DATA, "--out", str(out),
                        "--backend", "llm", "--provider", "ollama"], check=True, env=env, cwd=ROOT)
        data = json.loads((out / "promos.json").read_text())
        promos = data["promos"]
        assert promos and all(20 <= p["duration"] <= 60.5 for p in promos)
        assert any(p["kind"] == "montage" for p in promos)
        raw = list((out / "raw_llm").glob("ep_*.json"))
        assert raw and all(len(f.read_text().splitlines()) == 2 for f in raw)  # proposal + 1 repair round
    finally:
        srv.terminate()


def test_llm_backend_stops_on_fatal_error(tmp_path):
    """A retired model / bad key must stop the run after one call with a readable reason, not burn 50 calls."""
    port = _free_port()
    srv = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_llm_server.py"), str(port)])
    try:
        time.sleep(0.8)
        env = {**os.environ, "LLM_BASE_URL": f"http://127.0.0.1:{port}/v1/", "LLM_MODEL": "retired"}
        out = tmp_path / "out"
        r = subprocess.run([sys.executable, "-m", "promo_finder.run", "--input", DATA, "--out", str(out),
                            "--backend", "llm", "--provider", "ollama"], env=env, cwd=ROOT,
                           capture_output=True, text=True)
        assert r.returncode != 0
        assert "no longer available" in r.stderr
        logs = list((out / "raw_llm").glob("*.json"))
        assert len(logs) == 1 and len(logs[0].read_text().splitlines()) == 1  # exactly one call made
    finally:
        srv.terminate()
