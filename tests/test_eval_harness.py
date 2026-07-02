"""S05 — regression tests for the eval harness + ship gate.

These lock the load-bearing behaviours: golden-set validation, qrels
version-stamping for temporal, harness paired scoring with empty-run handling,
and the gate's PASS / FAIL(abort) / ERROR exit contract.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVAL = REPO / "eval"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=REPO)


def test_golden_set_builds_and_validates(tmp_path):
    out = tmp_path / "g.json"
    qr = tmp_path / "q.json"
    r = _run(str(EVAL / "build_golden_set.py"), "--out", str(out), "--qrels-out", str(qr))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "validation: OK" in r.stdout
    doc = json.loads(out.read_text())
    assert 60 <= len(doc["queries"]) <= 80
    # five mandated strata + cross-lingual present
    strata = {q["stratum"] for q in doc["queries"]}
    for s in ("cross_lingual_en_pt", "cross_lingual_en_es", "multi_hop",
              "temporal", "monolingual_pt", "monolingual_es", "lexical_identifier"):
        assert s in strata, s


def test_temporal_qrels_are_version_stamped(tmp_path):
    qr = tmp_path / "q.json"
    _run(str(EVAL / "build_golden_set.py"), "--out", str(tmp_path / "g.json"),
         "--qrels-out", str(qr))
    qrels = json.loads(qr.read_text())
    # every temporal query has at least one '#current' or '#superseded' doc key
    assert any(any("#" in k for k in d) for d in qrels.values())


def _write_runs(tmp_path, golden):
    """Two synthetic runs over a 3-query golden subset: 'new' ties 'current'."""
    qrels = {"a": {"docA": 3}, "b": {"docB": 2}, "c": {"docC": 3}}
    g = {"schema_version": "t", "coverage": {"strata": {}, "languages": {"EN": {"power": "gate"}}},
         "queries": [{"id": "a", "lang": "EN", "stratum": "x", "held_out": False, "qrels": []},
                     {"id": "b", "lang": "EN", "stratum": "x", "held_out": False, "qrels": []},
                     {"id": "c", "lang": "EN", "stratum": "x", "held_out": True, "qrels": []}]}
    cur = {"system": "cur", "runs": {"a": {"docA": 0.9}, "b": {"docB": 0.8}, "c": {"docZ": 0.5}},
           "latency_ms": {"a": 10, "b": 12, "c": 11}}
    new = {"system": "new", "runs": {"a": {"docA": 0.9}, "b": {"docB": 0.8}, "c": {"docC": 0.7}},
           "latency_ms": {"a": 5, "b": 6, "c": 5}}
    (tmp_path / "g.json").write_text(json.dumps(g))
    (tmp_path / "q.json").write_text(json.dumps(qrels))
    (tmp_path / "cur.json").write_text(json.dumps(cur))
    (tmp_path / "new.json").write_text(json.dumps(new))
    return golden


def test_harness_scores_and_gate_passes_when_non_inferior(tmp_path):
    _write_runs(tmp_path, None)
    sc = tmp_path / "sc.json"
    r = _run(str(EVAL / "harness.py"), "--golden", str(tmp_path / "g.json"),
             "--qrels", str(tmp_path / "q.json"), "--current", str(tmp_path / "cur.json"),
             "--new", str(tmp_path / "new.json"), "--out", str(sc))
    assert r.returncode == 0, r.stdout + r.stderr
    card = json.loads(sc.read_text())
    assert card["paired_scope"]["scored_n"] == 3
    # new found docC for c where current missed -> new >= current
    g = _run(str(EVAL / "gate.py"), "--scorecard", str(sc), "--bootstrap", "2000")
    assert g.returncode == 0, g.stdout
    assert "GATE: PASS" in g.stdout


def test_gate_fails_and_aborts_when_inferior(tmp_path):
    qrels = {"a": {"docA": 3}, "b": {"docB": 3}, "c": {"docC": 3}}
    g = {"schema_version": "t", "coverage": {"strata": {}, "languages": {"EN": {"power": "gate"}}},
         "queries": [{"id": x, "lang": "EN", "stratum": "x", "held_out": False, "qrels": []}
                     for x in ("a", "b", "c")]}
    cur = {"system": "cur", "runs": {"a": {"docA": .9}, "b": {"docB": .9}, "c": {"docC": .9}},
           "latency_ms": {"a": 10, "b": 10, "c": 10}}
    new = {"system": "new", "runs": {"a": {"docZ": .9}, "b": {"docZ": .9}, "c": {"docZ": .9}},
           "latency_ms": {"a": 5, "b": 5, "c": 5}}
    for n, o in [("g", g), ("q", qrels), ("cur", cur), ("new", new)]:
        (tmp_path / f"{n}.json").write_text(json.dumps(o))
    sc = tmp_path / "sc.json"
    _run(str(EVAL / "harness.py"), "--golden", str(tmp_path / "g.json"),
         "--qrels", str(tmp_path / "q.json"), "--current", str(tmp_path / "cur.json"),
         "--new", str(tmp_path / "new.json"), "--out", str(sc))
    res = _run(str(EVAL / "gate.py"), "--scorecard", str(sc), "--bootstrap", "2000")
    assert res.returncode == 1, res.stdout
    assert "ABORT BRANCH" in res.stdout
    assert "Obsidian + Smart Connections" in res.stdout


def test_gate_errors_on_empty_scored_set(tmp_path):
    g = {"schema_version": "t", "coverage": {"strata": {}, "languages": {}},
         "queries": [{"id": "a", "lang": "EN", "stratum": "x", "held_out": False, "qrels": []}]}
    qrels = {"a": {"docA": 3}}
    cur = {"system": "cur", "runs": {"a": {"docA": .9}}, "latency_ms": {}}
    new = {"system": "new", "runs": {"zzz": {"docA": .9}}, "latency_ms": {}}  # no overlap
    for n, o in [("g", g), ("q", qrels), ("cur", cur), ("new", new)]:
        (tmp_path / f"{n}.json").write_text(json.dumps(o))
    sc = tmp_path / "sc.json"
    _run(str(EVAL / "harness.py"), "--golden", str(tmp_path / "g.json"),
         "--qrels", str(tmp_path / "q.json"), "--current", str(tmp_path / "cur.json"),
         "--new", str(tmp_path / "new.json"), "--out", str(sc))
    res = _run(str(EVAL / "gate.py"), "--scorecard", str(sc))
    assert res.returncode == 2, res.stdout  # ERROR, not pass


def test_sc_baseline_fail_loud_on_empty(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps({"active_model": "x"}))
    res = _run(str(EVAL / "capture_sc_baseline.py"), "--golden", str(EVAL / "golden_set.json"),
               "--sc-results", str(empty), "--sc-stats", str(stats), "--out", str(tmp_path / "o.json"))
    assert res.returncode == 2
    assert not (tmp_path / "o.json").exists()
