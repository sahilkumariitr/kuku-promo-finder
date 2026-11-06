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
    # a proposal that lands just over 60 s is trimmed back to a clean cut; a 90 s one (above) is rejected
    start = e.shots[0].start
    over = Promo([Segment(e.number, start, start + 60.6)])
    if start + 60.6 <= e.duration:
        assert validate(over, {e.number: e}, cfg) == []
        assert over.duration <= cfg.max_len and any(f.startswith("trimmed") for f in over.flags)


def test_rules_backend_respects_brief(eps):
    cfg = Config()
    promos = rank(rules_backend.run(eps, cfg, log=lambda *a: None), cfg)
    assert promos
    for p in promos:
        assert cfg.min_len <= p.duration <= cfg.max_len  # strict: the brief says 20-60 s
        assert 0.0 <= p.score <= 1.0
        assert p.hook and p.cliffhanger and p.why
    assert [p.rank for p in promos] == list(range(1, len(promos) + 1))


def test_needs_context_ignores_empty_answers():
    from promo_finder.llm_backend import _to_promos
    item = lambda nc: {"episode": 1, "segments": [{"start": "00:00.00", "end": "00:30.00"}], "needs_context": nc}
    flagged = lambda nc: any(f.startswith("needs_context") for f in _to_promos({"promos": [item(nc)]}, 1, "single", 5)[0].flags)
    for empty in ["", None, "None", "None; the betrayal is self-explanatory.", "N/A", "No additional context needed"]:
        assert not flagged(empty), empty
    assert flagged("Rupert is Alexander's cousin who stole his title.")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def fake_api():
    port = _free_port()
    srv = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_llm_server.py"), str(port)])
    time.sleep(0.8)
    yield f"http://127.0.0.1:{port}/v1/"
    srv.terminate()


def _run_llm(fake_api, out, model="fake", **env_extra):
    env = {**os.environ, "LLM_BASE_URL": fake_api, "LLM_MODEL": model, "LLM_MIN_INTERVAL": "0",
           "LLM_RETRY_BASE": "0", **env_extra}
    if "LLM_FALLBACK_MODELS" not in env_extra:
        env.pop("LLM_FALLBACK_MODELS", None)
    return subprocess.run([sys.executable, "-m", "promo_finder.run", "--input", DATA, "--out", str(out),
                           "--backend", "llm", "--provider", "ollama"], env=env, cwd=ROOT,
                          capture_output=True, text=True)


def test_llm_backend_end_to_end(fake_api, tmp_path):
    """Batched proposal -> validation -> repair (with transcript) -> montage -> ranking."""
    r = _run_llm(fake_api, tmp_path / "out")
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "out" / "promos.json").read_text())
    promos, meta = data["promos"], data["meta"]
    assert promos and all(20 <= p["duration"] <= 60 for p in promos)
    assert any(p["kind"] == "montage" for p in promos)
    assert not meta["rules_fallback_episodes"]
    raw = sorted((tmp_path / "out" / "raw_llm").glob("ep_*.json"))
    n_eps = len(load_show(DATA))
    assert len(raw) == -(-n_eps // 10)  # episodes are batched 10 per request
    lines = [json.loads(l) for l in raw[0].read_text().splitlines()]
    assert len(lines) == 2 and "TRANSCRIPT" in lines[1]["user"]  # repair re-sends the transcript


def test_llm_backend_resumes_from_cache(fake_api, tmp_path):
    out = tmp_path / "out"
    assert _run_llm(fake_api, out).returncode == 0
    first = json.loads((out / "promos.json").read_text())
    r = _run_llm(fake_api, out)
    assert r.returncode == 0, r.stderr
    second = json.loads((out / "promos.json").read_text())
    assert second["meta"]["http_requests"] == 0 and second["meta"]["cache_hits"] > 0
    strip = lambda d: [(p["segments"], p["score"]) for p in d["promos"]]
    assert strip(first) == strip(second)


def test_llm_backend_falls_back_when_model_is_busy(fake_api, tmp_path):
    r = _run_llm(fake_api, tmp_path / "out", model="busy", LLM_FALLBACK_MODELS="fake")
    assert r.returncode == 0, r.stderr
    meta = json.loads((tmp_path / "out" / "promos.json").read_text())["meta"]
    assert meta["models_used"] == {"fake": meta["calls"]} and "busy" not in meta["live_requests_by_model"]
    assert "falling back to fake" in r.stdout


@pytest.mark.parametrize("model,needle", [("retired", "no longer available"), ("noquota", "quota")])
def test_llm_backend_stops_on_fatal_error(fake_api, tmp_path, model, needle):
    """Retired model / exhausted daily quota: stop after one request with a readable reason."""
    r = _run_llm(fake_api, tmp_path / "out", model=model)
    assert r.returncode != 0
    assert needle in r.stderr.lower()
    logs = list((tmp_path / "out" / "raw_llm").glob("*.json"))
    assert len(logs) == 1 and len(logs[0].read_text().splitlines()) == 1


def test_progress_map_tracks_both_backends(eps, fake_api, tmp_path, monkeypatch):
    """The live pipeline map in the web app: each stage ends done or skipped, never stuck running."""
    from promo_finder.progress import Pipeline
    from promo_finder import llm_backend
    pipe = Pipeline()
    pipe.set_mode(hybrid=False)
    rules_backend.run(eps, Config(), log=lambda *a: None, progress=pipe)
    assert pipe.state["rules"] == "done" and pipe.state["propose"] == "skipped"

    for k, v in {"LLM_BASE_URL": fake_api, "LLM_MODEL": "fake", "LLM_MIN_INTERVAL": "0", "LLM_RETRY_BASE": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    cfg = Config()
    cfg.provider, cfg.cache = "ollama", False
    pipe = Pipeline()
    pipe.set_mode(hybrid=True)
    llm_backend.run(eps, cfg, tmp_path / "raw", "show", log=lambda *a: None, progress=pipe)
    assert {n: pipe.state[n] for n in ("propose", "validate", "repair", "montages")} == dict.fromkeys(
        ("propose", "validate", "repair", "montages"), "done")  # the fake API forces one repair
    assert pipe.state["rules"] == "skipped" and "active" not in pipe.state.values()
    dot = pipe.to_dot()
    assert dot.startswith("digraph") and "✓ Propose promos" in dot
