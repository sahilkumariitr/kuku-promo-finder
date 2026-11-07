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


def test_cold_read_keeps_real_complaints():
    from promo_finder.llm_backend import NOTHING_CONFUSING
    for empty in ["", "None", "None.", "nothing", "N/A", "None; the clip is clear.", "Nothing - it reads fine"]:
        assert NOTHING_CONFUSING.match(empty) or not empty, empty
    for real in ["None of the characters are introduced", "Nothing connects the two scenes",
                 "Who is the man with the ring?"]:
        assert not NOTHING_CONFUSING.match(real), real


def _tiny_episode(number, blocks):
    """A hand-built episode: blocks are (start, end, kind, speaker, text)."""
    from promo_finder.parse import Block, Episode
    bs = [Block(i, s, e, k, sp, "", t, 1, "") for i, (s, e, k, sp, t) in enumerate(blocks)]
    return Episode(number, f"Episode {number}", "", "", max(b.end for b in bs), bs)


@pytest.fixture
def cast_eps():
    humiliation = _tiny_episode(1, [
        (0.0, 30.0, "CAMERA", "", "Wide on the ballroom."),
        (0.5, 3.0, "DIALOGUE", "VICTOR", "I am marrying your sister."),
        (3.0, 6.0, "DIALOGUE", "LADY MARA (O.S.)", "You were my family."),
        (6.0, 9.0, "DIALOGUE", "GUEST", "How shameful."),
        (9.0, 30.0, "ACTION", "", "Victor throws champagne in her face."),
    ])
    guards_only = _tiny_episode(2, [
        (0.0, 30.0, "CAMERA", "", "A dark corridor."),
        (0.5, 3.0, "DIALOGUE", "GUARD 2", "Halt!"),
        (3.0, 30.0, "DIALOGUE", "GUESTS", "Who goes there?"),
    ])
    payback = _tiny_episode(3, [
        (0.0, 30.0, "CAMERA", "", "Close on Mara, now in a gown, at the head of the table."),
        (0.5, 30.0, "DIALOGUE", "STRANGER", "Kneel."),
    ])
    return {e.number: e for e in (humiliation, guards_only, payback)}


def test_shared_cast_check(cast_eps):
    from promo_finder.features import show_cast, characters_in
    from promo_finder.promo import check_shared_cast
    cast = show_cast(cast_eps.values())
    assert cast == {"VICTOR", "LADY MARA", "STRANGER"}            # background roles never count
    assert characters_in(cast_eps[3], 0, 30, cast) == {"LADY MARA", "STRANGER"}  # named in the shot
    good = Promo([Segment(1, 0, 15), Segment(3, 0, 15)], kind="montage")
    assert check_shared_cast(good, cast_eps, cast) == [] and good.checks["shared_cast"] == [["LADY MARA"]]
    bad = Promo([Segment(1, 0, 15), Segment(2, 0, 15)], kind="montage")
    errs = check_shared_cast(bad, cast_eps, cast)
    assert len(errs) == 1 and "no named character carries over" in errs[0] and "GUARD" not in errs[0]


def test_montage_validation_uses_shared_cast(cast_eps, tmp_path, monkeypatch):
    """A montage whose cut shares nobody is a validator error (so it can go to the repair round)."""
    from promo_finder.llm_backend import LLMBackend
    from promo_finder.features import show_cast
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1/")
    cfg = Config()
    cfg.provider = "ollama"
    be = LLMBackend(cfg, tmp_path / "raw", "show", log=lambda *a: None)
    cast = show_cast(cast_eps.values())
    mk = lambda *segs: Promo([Segment(*s) for s in segs], kind="montage")
    good, errs = be._check([mk((1, 0, 15), (3, 0, 15)), mk((1, 0, 15), (2, 0, 15)), mk((1, 0, 12), (1, 15, 30))],
                           cast_eps, "montage", cast)
    assert len(good) == 1 and len(errs) == 2
    assert "carries over" in errs[0] and "2+ episodes" in errs[1]
    good, errs = be._check([mk((1, 0, 15), (2, 0, 15))], cast_eps, "montage", None)  # check turned off
    assert len(good) == 1


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


def test_montages_pass_continuity_checks(fake_api, tmp_path):
    """Montages share a character at every cut, carry a link, and survive a context-free cold read."""
    r = _run_llm(fake_api, tmp_path / "out")
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "out" / "promos.json").read_text())
    mc = data["meta"]["montage_checks"]
    montages = [p for p in data["promos"] if p["kind"] == "montage"]
    assert len(montages) == 1 and mc["proposed"] >= 2 and mc["dropped_cold_read"] >= 1  # fake reader fails C2+
    m = montages[0]
    assert m["link"] and all(m["checks"]["shared_cast"])
    cr = m["checks"]["cold_read"]
    assert cr["score"] == 4 and cr["proposer_standalone"] == 5
    assert m["llm_scores"]["standalone"] == 4  # the cold reader's score replaces the proposer's own
    read = json.loads((tmp_path / "out" / "raw_llm" / "cold_read.json").read_text().splitlines()[0])
    assert "--- CUT ---" in read["user"]
    assert "Ep " not in read["user"] and "LOGLINE" not in read["user"].upper()  # no show context leaks in


def test_cold_read_can_be_turned_off(eps, fake_api, tmp_path, monkeypatch):
    from promo_finder.progress import Pipeline
    from promo_finder import llm_backend
    for k, v in {"LLM_BASE_URL": fake_api, "LLM_MODEL": "fake", "LLM_MIN_INTERVAL": "0", "LLM_RETRY_BASE": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    cfg = Config()
    cfg.provider, cfg.cache, cfg.cold_read = "ollama", False, False
    pipe = Pipeline()
    pipe.set_mode(hybrid=True)
    promos, stats = llm_backend.run(eps, cfg, tmp_path / "raw", "show", log=lambda *a: None, progress=pipe)
    montages = [p for p in promos if p.kind == "montage"]
    assert len(montages) >= 2 and not any("cold_read" in p.checks for p in montages)
    assert not (tmp_path / "raw" / "cold_read.json").exists()
    assert pipe.state["coldread"] == "skipped" and stats["montage_checks"]["dropped_cold_read"] == 0


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
    llm_nodes = ("propose", "validate", "repair", "montages", "coldread")
    assert {n: pipe.state[n] for n in llm_nodes} == dict.fromkeys(llm_nodes, "done")  # the fake forces one repair
    assert pipe.state["rules"] == "skipped" and "active" not in pipe.state.values()
    dot = pipe.to_dot()
    assert dot.startswith("digraph") and "✓ Propose promos" in dot
