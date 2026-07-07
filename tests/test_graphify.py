"""GRF-01/GRF-02 — the graphify discovery build (ADR-0003 Ruling 6/(a)).

Offline + deterministic: HashEmbedder + BruteForceBackend, env-injected audit
key (same discipline as tests/test_maintenance.py). Covers the evidence
contract: drift gate, per-note/global edge caps, egress-gated candidate
surfacing, the monthly maintain date-gate REALLY invoking a build,
``brain status`` surfacing, and atomic publish (a failed/partial build never
touches the consumable ``graph.json`` path). The dense ~69-note
``synthetic_core``/``synthetic_vault`` fixture (s08) backs the rank/cap tests
— the small fixtures below can't exercise a k<=5 / global-2x cap at all.
"""
from __future__ import annotations

import datetime
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from brain import cli, config, graphify as gmod, maintenance as maint
from brain.core import BrainCore


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _json(argv) -> tuple[int, dict]:
    code, out = _run(argv)
    return code, json.loads(out)


def _note(nid, title, classification, body, *, links=()):
    linktext = (" " + " ".join(f"[[{l}]]" for l in links)) if links else ""
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n"
        f"classification: {classification}\ncreated: 2026-06-27\n"
        f"updated: 2026-06-27\n---\n\n{body}{linktext}\n"
    )


@pytest.fixture
def graphify_egress_core(tmp_path, audit_key_env, monkeypatch):
    """A tiny vault with two UNLINKED, near-duplicate-text notes at different
    classification tiers — enough shared vocabulary that HashEmbedder gives
    them a high cosine (an INFERRED candidate), so the egress gate on
    graphify's candidate surfacing has something real to withhold."""
    from brain.embed import HashEmbedder
    from brain.index import BrainIndex
    from brain.vectors import BruteForceBackend

    vault = tmp_path / "vault"
    (vault / "brain" / "resources").mkdir(parents=True)
    (vault / "brain" / "projects").mkdir(parents=True)
    shared_text = (
        "quarterly roadmap review covers pricing timeline milestones "
        "deliverables workstream owners"
    )
    files = {
        "brain/resources/alpha-internal.md": _note(
            "alpha-internal", "Alpha Internal", "Internal", shared_text),
        "brain/projects/alpha-restricted.md": _note(
            "alpha-restricted", "Alpha Restricted", "Restricted",
            shared_text + " plus restricted counterparty terms"),
        # A third, unrelated note so the corpus has >2 notes (guards against
        # an accidental "only one candidate pair exists" degenerate shape). It
        # links to a 4th hub note so the corpus has >=1 EXPLICIT edge — the
        # global cap (2x explicit) would otherwise be 0 and mask every
        # candidate, including the one this test cares about.
        "brain/resources/unrelated.md": _note(
            "unrelated", "Unrelated", "Internal",
            "Completely different topic: coffee brewing temperature curves.",
            links=["hub"]),
        "brain/resources/hub.md": _note(
            "hub", "Hub", "Internal", "An unrelated hub note."),
    }
    for rel, text in files.items():
        (vault / rel).write_text(text, encoding="utf-8")

    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend(),
                      embedder=HashEmbedder())
    idx.rebuild(vault)
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path / "runtime"))
    return BrainCore(vault=vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")


# ---------------------------------------------------------------------------
# drift gate (ADR-0003 Ruling 6, ground 2)
# ---------------------------------------------------------------------------
def test_drift_gate_skips_rebuild_when_corpus_unchanged(synthetic_core):
    first = synthetic_core.graphify()
    assert first["published"] is True
    assert first["generation"] == 1

    second = synthetic_core.graphify()
    assert second.get("skipped") == "unchanged"
    assert second["generation"] == 1  # untouched — no rebuild happened

    third = synthetic_core.graphify(force=True)
    assert third["published"] is True
    assert third["generation"] == 2  # --force bypasses the gate


def test_manifest_unchanged_pure_helper():
    new = {"a": "h1", "b": "h2"}
    assert gmod.manifest_unchanged(None, new) is False
    assert gmod.manifest_unchanged({}, new) is False
    assert gmod.manifest_unchanged({"notes": new}, new) is True
    assert gmod.manifest_unchanged({"notes": {"a": "h1"}}, new) is False


# ---------------------------------------------------------------------------
# per-note (k<=5) and global (<=2x explicit) INFERRED edge caps
# ---------------------------------------------------------------------------
def test_edge_caps_enforced_on_dense_synthetic_corpus(synthetic_core):
    res = synthetic_core.graphify()
    assert res["published"] is True
    artifact = json.loads(Path(res["path"]).read_text(encoding="utf-8"))

    ok, problems = gmod.validate_artifact(artifact)
    assert ok, problems

    explicit = [e for e in artifact["edges"] if e["kind"] == "WIKILINK"]
    inferred = [e for e in artifact["edges"] if e["kind"] == "INFERRED"]
    assert len(inferred) <= 2 * len(explicit)

    degree: dict[str, int] = {}
    for e in inferred:
        degree[e["from"]] = degree.get(e["from"], 0) + 1
        degree[e["to"]] = degree.get(e["to"], 0) + 1
    assert all(d <= gmod.DEFAULT_TOPK for d in degree.values())

    # the dense synthetic fixture's near-templated bodies (s08's generator)
    # guarantee real INFERRED proposals exist — a false-green empty build
    # would prove nothing about the caps.
    assert inferred, "expected at least one INFERRED candidate on the dense fixture"


def test_validate_artifact_catches_a_cap_violation():
    bad = {
        "authoritative": False, "schema_version": gmod.GRAPH_SCHEMA_VERSION,
        "provenance": gmod.PROVENANCE,
        "edges": [{"kind": "WIKILINK", "from": "a", "to": "b"}]
                 + [{"kind": "INFERRED", "from": "a", "to": f"x{i}", "score": 0.9}
                    for i in range(gmod.DEFAULT_TOPK + 1)],
    }
    ok, problems = gmod.validate_artifact(bad)
    assert ok is False
    assert any("per-node" in p for p in problems)


def test_no_self_links_and_no_already_linked_duplicates(synthetic_core):
    res = synthetic_core.graphify()
    artifact = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
    wiki_pairs = {
        frozenset((e["from"], e["to"]))
        for e in artifact["edges"] if e["kind"] == "WIKILINK"
    }
    for e in artifact["edges"]:
        if e["kind"] == "INFERRED":
            assert e["from"] != e["to"]
            assert frozenset((e["from"], e["to"])) not in wiki_pairs


# ---------------------------------------------------------------------------
# egress filtering of candidates (a withheld note must never leak here either)
# ---------------------------------------------------------------------------
def test_candidates_gate_the_sensitive_tier_by_default(graphify_egress_core):
    res = graphify_egress_core.graphify()
    assert res["published"] is True
    pairs = {frozenset((c["from"], c["to"])) for c in res["candidates"]}
    assert frozenset({"alpha-internal", "alpha-restricted"}) not in pairs
    assert res["egress"]["withheld"] >= 1

    hi = graphify_egress_core.graphify(force=True, max_tier="Restricted")
    hi_pairs = {frozenset((c["from"], c["to"])) for c in hi["candidates"]}
    assert frozenset({"alpha-internal", "alpha-restricted"}) in hi_pairs


def test_graphify_cli_gates_candidates_and_refuses_on_vm(graphify_egress_core, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: graphify_egress_core)
    code, res = _json(["graphify", "--json"])
    assert code == 0
    pairs = {frozenset((c["from"], c["to"])) for c in res["candidates"]}
    assert frozenset({"alpha-internal", "alpha-restricted"}) not in pairs

    # VM refusal happens BEFORE BrainCore is constructed (cli.py's role gate) —
    # no side effect: no .brain/graph/ ever gets created by the refused call.
    graph_dir = config.graph_dir(graphify_egress_core.vault)
    import shutil
    shutil.rmtree(graph_dir, ignore_errors=True)
    code2, res2 = _json(["--role", "vm", "graphify", "--json"])
    assert code2 == 4
    assert res2["error"] == "role_forbidden"
    assert not graph_dir.exists()


# ---------------------------------------------------------------------------
# atomic publish: a failed/partial build never touches the consumable path
# ---------------------------------------------------------------------------
def test_failed_build_writes_a_separate_marker_never_touches_graph_json(synthetic_core, monkeypatch):
    good = synthetic_core.graphify()
    assert good["published"] is True
    graph_path = Path(good["path"])
    before = graph_path.read_text(encoding="utf-8")

    real_build = gmod.build_graph_artifact

    def _boom(*a, **kw):
        raise RuntimeError("simulated build failure")

    # NOTE: deliberately NOT monkeypatch.undo() below — `monkeypatch` is a
    # function-scoped fixture SHARED with whatever fixture chain set
    # $BRAIN_RUNTIME_DIR (synthetic_core -> synthetic_index -> monkeypatch);
    # calling .undo() here would ALSO revert that env var mid-test and silently
    # redirect the next write to the vault-relative default — i.e. into the
    # COMMITTED synthetic_vault fixture tree. Restore only the one attribute
    # this test patched instead.
    monkeypatch.setattr(gmod, "build_graph_artifact", _boom)
    res = synthetic_core.graphify(force=True)
    assert res["status"] == "build_failed"
    assert res["published"] is False

    marker = config.graph_build_failed_marker_path(synthetic_core.vault)
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "build_failed"
    # the published graph.json is COMPLETELY untouched by the failed attempt.
    assert graph_path.read_text(encoding="utf-8") == before

    # a subsequent good build clears the stale marker and republishes.
    monkeypatch.setattr(gmod, "build_graph_artifact", real_build)
    res2 = synthetic_core.graphify(force=True)
    assert res2["published"] is True
    assert not marker.exists()


def test_invalid_artifact_never_publishes(synthetic_core, monkeypatch):
    monkeypatch.setattr(gmod, "validate_artifact", lambda *a, **kw: (False, ["forced invalid"]))
    res = synthetic_core.graphify()
    assert res["status"] == "invalid_artifact"
    assert res["published"] is False
    assert not config.graph_json_path(synthetic_core.vault).exists()
    marker = config.graph_build_failed_marker_path(synthetic_core.vault)
    assert json.loads(marker.read_text(encoding="utf-8"))["problems"] == ["forced invalid"]


def test_dry_run_never_publishes_or_advances_generation(synthetic_core):
    res = synthetic_core.graphify(dry_run=True)
    assert res["dry_run"] is True
    assert res["published"] is False
    assert not config.graph_json_path(synthetic_core.vault).exists()
    assert not config.graph_manifest_path(synthetic_core.vault).exists()


# ---------------------------------------------------------------------------
# `brain status` surfacing (GRF-02)
# ---------------------------------------------------------------------------
def test_status_surfaces_graph_generation_and_age(synthetic_core):
    before = synthetic_core.status()
    assert before["graph"]["status"] == "never_built"

    synthetic_core.graphify()
    after = synthetic_core.status()
    assert after["graph"]["status"] == "ok"
    assert after["graph"]["generation"] == 1
    assert after["graph"]["age_days"] == 0
    assert after["graph"]["note_count"] > 0


# ---------------------------------------------------------------------------
# the monthly `maintain` branch REALLY invokes a build (ADR-0003 Ruling a) —
# not the old `invoked: false` stub.
# ---------------------------------------------------------------------------
def _seed_state(core: BrainCore, per_branch_last_run: dict) -> None:
    path = config.maintain_state_path(core.vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({b: {"last_run": v, "status": "ok"} for b, v in per_branch_last_run.items()}),
        encoding="utf-8",
    )


FIRST_OF_MONTH_STATE = {
    "daily": "2026-06-30", "health": "2026-06-29", "integrity": "2026-06-30",
    "digest": "2026-06-28", "graphify": "2026-06-01",
}


def test_maintain_graphify_branch_invokes_a_real_build(populated_core):
    first_of_month = datetime.date.fromisoformat("2026-07-01")
    _seed_state(populated_core, FIRST_OF_MONTH_STATE)
    assert "graphify" in maint.maintain_branches(first_of_month, FIRST_OF_MONTH_STATE)

    res = populated_core.maintain(dry_run=False, today=first_of_month)
    g = res["results"]["graphify"]
    assert g["invoked"] is True
    assert g["published"] is True
    assert config.graph_json_path(populated_core.vault).exists()

    state = json.loads(config.maintain_state_path(populated_core.vault).read_text(encoding="utf-8"))
    assert state["graphify"]["last_run"] == first_of_month.isoformat()


def test_maintain_dry_run_graphify_branch_is_real_but_never_publishes(populated_core):
    first_of_month = datetime.date.fromisoformat("2026-07-01")
    _seed_state(populated_core, FIRST_OF_MONTH_STATE)

    res = populated_core.maintain(dry_run=True, today=first_of_month)
    g = res["results"]["graphify"]
    assert g["invoked"] is True
    assert g["dry_run"] is True
    assert g["published"] is False
    assert not config.graph_json_path(populated_core.vault).exists()


def test_maintain_graphify_branch_queues_hot_entry_once(populated_core):
    first_of_month = datetime.date.fromisoformat("2026-07-01")
    _seed_state(populated_core, FIRST_OF_MONTH_STATE)
    res = populated_core.maintain(dry_run=False, today=first_of_month)
    g = res["results"]["graphify"]

    hot_path = config.memory_dir(populated_core.vault) / "hot.md"
    key = f"maintain:graphify:{first_of_month.isoformat()}"
    if g.get("candidates"):
        text = hot_path.read_text(encoding="utf-8")
        assert text.count(key) == 1
        # the idempotency-key guard itself is unit-tested directly: a second
        # attempt at the SAME key is a no-op (mirrors curate/promote-scan).
        appended_again = populated_core._append_hot_once(
            key, maint.render_graphify_hot_entry(g["candidates"], first_of_month))
        assert appended_again is False
        assert hot_path.read_text(encoding="utf-8").count(key) == 1


# ---------------------------------------------------------------------------
# optional: graph-expand consuming INFERRED edges (ADR-0003 Ruling 6, "Optional")
# ---------------------------------------------------------------------------
def test_read_published_inferred_edges_degrades_gracefully(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert gmod.read_published_inferred_edges(missing) == []

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json", encoding="utf-8")
    assert gmod.read_published_inferred_edges(bad_json) == []

    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text(json.dumps({
        "authoritative": False, "schema_version": 999, "edges": [],
    }), encoding="utf-8")
    assert gmod.read_published_inferred_edges(wrong_schema) == []

    authoritative_true = tmp_path / "auth.json"
    authoritative_true.write_text(json.dumps({
        "authoritative": True, "schema_version": gmod.GRAPH_SCHEMA_VERSION,
        "edges": [{"kind": "INFERRED", "from": "a", "to": "b"}],
    }), encoding="utf-8")
    assert gmod.read_published_inferred_edges(authoritative_true) == []

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({
        "authoritative": False, "schema_version": gmod.GRAPH_SCHEMA_VERSION,
        "edges": [{"kind": "INFERRED", "from": "a", "to": "b", "score": 0.9},
                  {"kind": "WIKILINK", "from": "a", "to": "c"}],
    }), encoding="utf-8")
    assert gmod.read_published_inferred_edges(valid) == [("a", "b")]


def test_build_graph_extra_edges_are_bfs_reachable_at_one_hop(synthetic_core):
    """Unit-level proof of the fold-in mechanism itself (graph.build_graph),
    independent of PPR's re-ranking (PPR mass redistributes when the graph
    gains edges, so the ranked/truncated top-k is NOT guaranteed to be a
    superset — that would be the wrong invariant to assert at the
    graph_expand level; the fold-in wiring is what matters here)."""
    from brain.graph import build_graph, wikilink_bfs

    without = build_graph(synthetic_core.index.conn)
    a, b = "topic-000", "topic-001"
    # only wire the extra edge in if it is not already a real wikilink (the
    # test only needs ONE new hop-1 neighbour, real or not).
    if b in without.undirected_adj.get(a, set()):
        b = "topic-002"
    with_extra = build_graph(synthetic_core.index.conn, extra_edges=[(a, b)])
    bfs_without = {d["id"]: d["hops"] for d in wikilink_bfs(without, [a], depth=1)}
    bfs_with = {d["id"]: d["hops"] for d in wikilink_bfs(with_extra, [a], depth=1)}
    assert bfs_with.get(b) == 1
    assert bfs_without.get(b) != 1


def test_graph_expand_use_inferred_flag_changes_method_label(synthetic_core):
    res = synthetic_core.graphify()
    assert res["published"] is True
    artifact = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
    inferred = [e for e in artifact["edges"] if e["kind"] == "INFERRED"]
    assert inferred, "need at least one INFERRED edge to test the fold-in"
    seed = inferred[0]["from"]

    without = synthetic_core.graph_expand([seed], depth=1, k=50, use_inferred=False)
    with_inferred = synthetic_core.graph_expand([seed], depth=1, k=50, use_inferred=True)
    assert "+inferred" not in without["method"]
    assert "+inferred" in with_inferred["method"]

    # VM role never reads the host-only graphify artifact even if asked to —
    # degrades to the plain wikilink graph instead of a side-channel read.
    vm_core = BrainCore(vault=synthetic_core.vault, index=synthetic_core.index,
                         audit_log=None, role="vm")
    vm_res = vm_core.graph_expand([seed], depth=1, k=50, use_inferred=True)
    assert "+inferred" not in vm_res["method"]


# ---------------------------------------------------------------------------
# wall-clock budget (ADR-0003 Ruling 6 ground 2) — the one open tunable S01
# flagged: measure real duration on the reusable synthetic corpus.
# ---------------------------------------------------------------------------
def test_build_completes_within_a_generous_budget_on_the_synthetic_corpus(synthetic_core):
    res = synthetic_core.graphify(force=True)
    assert res["published"] is True
    duration = res["build"]["duration_seconds"]
    assert duration < 30.0, (
        f"graphify took {duration}s on the ~69-note synthetic corpus — "
        f"investigate before trusting the ADR's <=60s target at real scale"
    )
    assert res["build"]["action_required"] is False
