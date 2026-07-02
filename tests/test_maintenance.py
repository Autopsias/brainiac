"""CUT-03/CUT-05 — the maintenance subcommands (check/health/curate/integrity/
promote-scan/maintain) + the populated fixture corpus they run against.

Offline + deterministic: HashEmbedder + BruteForceBackend (via the
``populated_core``/``populated_index`` fixtures in conftest.py), env-injected
audit key. Every test here either (a) exercises the REAL populated corpus
read-only (health/integrity/curate-dry-run/promote-scan/maintain --dry-run —
proving real engagement, never an empty-index false-green) or (b) uses the
small mutable ``sample_vault`` fixture for anything that actually writes
(drain-bucket behaviour) so the committed ``tests/fixtures/sample_corpus/``
tree is NEVER mutated by a test run.
"""
from __future__ import annotations

import datetime
import io
import json
from contextlib import redirect_stdout

import pytest

from brain import cli, maintenance as maint
from brain.core import BrainCore, RoleError


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _json(argv) -> tuple[int, dict]:
    code, out = _run(argv)
    return code, json.loads(out)


HOST_WRITE_VERBS = ["check", "health", "curate", "integrity", "promote-scan", "maintain"]


# ---------------------------------------------------------------------------
# role gate: ALL SIX are host-broker only, never runnable under role=vm
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", HOST_WRITE_VERBS)
def test_vm_role_refuses_every_maintenance_verb(sample_vault, audit_key_env, monkeypatch, tmp_path, cmd):
    monkeypatch.setenv("BRAIN_VAULT", str(sample_vault))
    monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
    code, out = _json(["--role", "vm", cmd, "--json"])
    assert code == 4
    assert out["error"] == "role_forbidden"
    assert out["cmd"] == cmd


@pytest.mark.parametrize("method", ["check", "health", "curate", "integrity", "promote_scan", "maintain"])
def test_braincore_methods_require_host_role(sample_vault, audit_key_env, tmp_path, method):
    from brain.index import BrainIndex
    from brain.vectors import BruteForceBackend
    from brain.embed import HashEmbedder

    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(sample_vault)
    core = BrainCore(vault=sample_vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="vm")
    with pytest.raises(RoleError):
        getattr(core, method)()


# ---------------------------------------------------------------------------
# `check` — daily-check fold
# ---------------------------------------------------------------------------
def test_check_dry_run_does_not_mutate_index(populated_core, populated_vault):
    before = populated_core.index.stats()["notes"]
    res1 = populated_core.check(dry_run=True)
    res2 = populated_core.check(dry_run=True)
    after = populated_core.index.stats()["notes"]
    assert before == after == 16
    assert res1["sync"] is None and res2["sync"] is None
    assert res1["outcomes"]["counts"]["auto_fixed"] == 0
    # never writes into the committed fixture corpus
    assert len(list(populated_vault.rglob("*.md"))) == 16


def test_check_real_run_is_a_noop_when_nothing_changed(populated_core):
    # No pending drafts, no on-disk changes since the index was built ->
    # sync finds 0 added/0 updated/0 deleted -> no auto_fixed entries, but it
    # IS a real sync call (mode == "incremental"), proving real engagement.
    res = populated_core.check(dry_run=False)
    assert res["sync"]["mode"] == "incremental"
    assert res["sync"]["added"] == 0 and res["sync"]["updated"] == 0
    assert res["outcomes"]["counts"]["auto_fixed"] == 0
    assert res["outcomes"]["counts"]["blocked"] == 0


def test_check_drain_reports_auto_fixed_and_blocked(sample_vault, audit_key_env, tmp_path, monkeypatch):
    """Drain-bucket behaviour uses the small MUTABLE sample_vault (never the
    committed populated fixture) because a real drain calls write_note,
    which writes into vault/ — must never land in the repo's fixture tree."""
    from brain.index import BrainIndex
    from brain.vectors import BruteForceBackend
    from brain.embed import HashEmbedder

    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(sample_vault)
    core = BrainCore(vault=sample_vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")

    inbox = core.capture_inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "draft-ok.md").write_text(
        "---\nid: draft-ok\ntype: note\nclassification: Internal\n---\n\nA staged draft.\n",
        encoding="utf-8",
    )
    (inbox / "draft-bad.md").write_text("no frontmatter here at all\n", encoding="utf-8")

    res = core.check(dry_run=False)
    assert res["outcomes"]["counts"]["auto_fixed"] >= 1  # the drain + sync
    drain_reasons = [a["reason"] for a in res["outcomes"]["auto_fixed"]]
    assert any("drained" in r for r in drain_reasons)
    assert res["outcomes"]["counts"]["action_required"] == 1  # draft-bad: no-frontmatter
    assert "draft-bad" in res["outcomes"]["action_required"][0]["finding"]


# ---------------------------------------------------------------------------
# `health` — read-only by construction
# ---------------------------------------------------------------------------
def test_health_runs_real_selftest_against_populated_corpus(populated_core):
    res = populated_core.health()
    assert res["selftest"]["probe_ok"] is True
    assert res["selftest"]["result_count"] >= 1
    assert res["audit"]["status"] in ("ok", "empty")
    assert res["outcomes"]["counts"]["blocked"] == 0
    assert res["outcomes"]["counts"]["action_required"] == 0


def test_health_blocked_when_no_signing_key(sample_vault, tmp_path, monkeypatch):
    # No audit_key_env fixture here -> no BRAIN_AUDIT_KEY_PEM, no Keychain in
    # the sandbox -> verify_audit() raises KeyUnavailable -> BLOCKED, not a
    # crash (health degrades gracefully, per the disposition contract).
    from brain.index import BrainIndex
    from brain.vectors import BruteForceBackend
    from brain.embed import HashEmbedder

    monkeypatch.delenv("BRAIN_AUDIT_KEY_PEM", raising=False)
    monkeypatch.delenv("BRAIN_AUDIT_KEY_CMD", raising=False)
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(sample_vault)
    core = BrainCore(vault=sample_vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")
    res = core.health()
    assert res["selftest"]["probe_ok"] is True  # retrieval itself still works
    assert res["outcomes"]["counts"]["blocked"] >= 1


# ---------------------------------------------------------------------------
# `curate` — refresh-index fold + unclassified-notes lint (egress-gated)
# ---------------------------------------------------------------------------
def test_curate_finds_unclassified_notes_only_when_elevated(populated_core, populated_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)

    # default --max-tier Internal: unclassified/invalid-tier notes default-deny
    # to MNPI, so they are WITHHELD just like any other over-cap note — the
    # human-gate elevation is required to see the lint findings.
    code, res = _json(["curate", "--dry-run", "--json"])
    assert code == 0
    assert res["unclassified_notes"] == []
    assert res["egress"]["withheld"] == 2

    code2, res2 = _json(["curate", "--dry-run", "--json", "--max-tier", "MNPI"])
    assert code2 == 0
    ids = {n["id"] for n in res2["unclassified_notes"]}
    assert ids == {"unclassified-note", "bad-tier-note"}
    assert res2["outcomes"]["counts"]["action_required"] == 2
    assert res2["overlay_only_skipped"]["orphans"]  # documented, not invented


def test_curate_refresh_index_is_real_when_not_dry_run(populated_core):
    res = populated_core.curate(dry_run=False)
    assert res["sync"]["mode"] == "incremental"


# ---------------------------------------------------------------------------
# `integrity` — audit verify + corpus-wide near-dup scan (G1), egress-gated
# ---------------------------------------------------------------------------
def test_integrity_finds_the_intentional_near_dup_pair(populated_core, populated_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)

    code, res = _json(["integrity", "--json"])
    assert code == 0
    found = {frozenset((p["a"]["id"], p["b"]["id"])) for p in res["near_dup_pairs"]}
    assert frozenset({"quarterly-review-draft", "quarterly-review-draft-v2"}) in found
    assert res["outcomes"]["counts"]["action_required"] >= 1


def test_integrity_gates_pairs_above_max_tier(populated_core, populated_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)

    # The near-dup pair is classification=Internal on both sides; capping at
    # Public must withhold it (both members exceed the cap).
    code, res = _json(["integrity", "--json", "--max-tier", "Public"])
    assert code == 0
    found = {frozenset((p["a"]["id"], p["b"]["id"])) for p in res["near_dup_pairs"]}
    assert frozenset({"quarterly-review-draft", "quarterly-review-draft-v2"}) not in found
    assert res["egress"]["withheld_pairs"] >= 1


def test_integrity_core_min_score_threshold(populated_core):
    # A very high threshold finds nothing — the scan still ran for real.
    res = populated_core.integrity(min_score=0.999, k=5)
    assert res["near_dup_pairs"] == []
    res2 = populated_core.integrity(min_score=0.9, k=5)
    assert len(res2["near_dup_pairs"]) >= 1


# ---------------------------------------------------------------------------
# S11-BUG-01 regression: `brain integrity --json` must emit VALID JSON even when
# near-dup scores are numpy scalars (the real ONNX embedder returns numpy.float32
# despite the list[list[float]] contract). The HashEmbedder fixture returns
# native floats, so this bug was invisible until run over the real corpus.
# The fix is a `default=` handler in cli._emit — it covers ALL --json subcommands.
# ---------------------------------------------------------------------------
def test_integrity_json_serialises_numpy_scores(populated_core, populated_vault, monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)

    # Force a numpy.float32 score into the near-dup output — exactly what the
    # real OnnxEmbedder path produces and what crashed the stdlib json.dump.
    real_near_dup = populated_core.index.near_dup

    def _numpy_scored(**kw):
        pairs = real_near_dup(**kw)
        for p in pairs:
            p["score"] = np.float32(p["score"])
        return pairs

    monkeypatch.setattr(populated_core.index, "near_dup", _numpy_scored)

    # _json() calls json.loads on the CLI's stdout — it would raise if the
    # emission crashed or produced invalid JSON.
    code, res = _json(["integrity", "--json"])
    assert code == 0
    assert res["near_dup_pairs"], "expected the intentional near-dup pair"
    # Score round-tripped as a native JSON number (float), not a numpy repr/string.
    assert all(isinstance(p["score"], float) for p in res["near_dup_pairs"])


def test_emit_json_coerces_numpy_scalars_and_arrays():
    np = pytest.importorskip("numpy")
    obj = {
        "f32": np.float32(0.98765),
        "i64": np.int64(42),
        "arr": np.array([1.0, 2.0], dtype=np.float32),
        "native": 1.5,
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._emit(obj, as_json=True)
    parsed = json.loads(buf.getvalue())  # raises if _json_default failed
    assert abs(parsed["f32"] - 0.98765) < 1e-4
    assert parsed["i64"] == 42
    assert parsed["arr"] == [1.0, 2.0]
    assert parsed["native"] == 1.5


# ---------------------------------------------------------------------------
# `promote-scan` — raw/ triage candidates (egress-gated)
# ---------------------------------------------------------------------------
def test_promote_scan_lists_unpromoted_raw_sources(populated_core, populated_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)

    code, res = _json(["promote-scan", "--json"])
    assert code == 0
    ids = {c["id"] for c in res["candidates"]}
    assert {"fixture-call-notes", "fixture-research-clip"} <= ids
    assert res["outcomes"]["counts"]["action_required"] == len(res["candidates"])
    assert res["pending_drafts"] == 0


# ---------------------------------------------------------------------------
# `maintain` — the umbrella + date-gated branches (persistence-budget.md)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "iso_date,expected",
    [
        ("2026-07-06", {"daily", "health"}),     # Monday
        ("2026-07-07", {"daily", "integrity"}),  # Tuesday
        ("2026-07-12", {"daily", "digest"}),     # Sunday
        ("2026-07-01", {"daily", "graphify"}),   # 1st of month (Wed)
        ("2026-07-08", {"daily"}),               # plain Wednesday
    ],
)
def test_maintain_branches_date_gate(iso_date, expected):
    d = datetime.date.fromisoformat(iso_date)
    assert set(maint.maintain_branches(d)) == expected


def test_maintain_dry_run_proves_real_work_without_mutating(populated_core, populated_vault):
    """The CUT-05 evidence shape: --dry-run must still run REAL reads (health
    selftest probe, near-dup scan) against the populated corpus on a due
    branch, while never mutating the index or touching the vault tree."""
    before_notes = populated_core.index.stats()["notes"]
    tuesday = datetime.date.fromisoformat("2026-07-07")

    res = populated_core.maintain(dry_run=True, today=tuesday)

    assert res["dry_run"] is True
    assert res["branches_due"] == ["daily", "integrity"]
    assert "sync" not in res["results"]  # the mutating half is skipped
    assert "integrity" in res["results"]  # the read-only half runs for real
    assert res["results"]["integrity"]["near_dup_pairs"]  # real findings, not empty
    assert res["outcomes"]["counts"]["action_required"] >= 1

    after_notes = populated_core.index.stats()["notes"]
    assert before_notes == after_notes == 16
    assert len(list(populated_vault.rglob("*.md"))) == 16


def test_maintain_real_run_syncs_and_publishes(populated_core):
    monday = datetime.date.fromisoformat("2026-07-06")
    res = populated_core.maintain(dry_run=False, today=monday)
    assert res["results"]["sync"]["mode"] == "incremental"
    assert "snapshot" in res["results"]["sync"]
    assert "health" in res["results"]
    assert res["results"]["health"]["selftest"]["probe_ok"] is True


def test_maintain_cli_dry_run_json_shape(populated_core, populated_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)

    code, res = _json(["maintain", "--dry-run", "--date", "2026-07-06", "--json"])
    assert code == 0
    assert res["ritual"] == "maintain" and res["dry_run"] is True
    assert res["weekday"] == "Monday"
    assert set(res["outcomes"]) == {"auto_fixed", "action_required", "blocked", "counts"}


# ---------------------------------------------------------------------------
# outcomes-report shape
# ---------------------------------------------------------------------------
def test_build_outcomes_buckets_always_present():
    out = maint.build_outcomes()
    assert out == {
        "auto_fixed": [], "action_required": [], "blocked": [],
        "counts": {"auto_fixed": 0, "action_required": 0, "blocked": 0},
    }


def test_render_outcomes_markdown_three_blocks_and_none_shape():
    out = maint.build_outcomes()
    md = maint.render_outcomes_markdown(out)
    assert "## Auto-remediated this run (0 items)" in md
    assert "## Action Required (0 items)" in md
    assert "## Blocked — external dependency (0 items)" in md
    assert md.count("(none)") == 3


def test_render_outcomes_markdown_renders_populated_buckets():
    out = maint.build_outcomes(
        auto_fixed=[maint.auto_fixed_item("sync", "vault/", "reconciled")],
        action_required=[maint.action_required_item("f", "why", "do x", "path")],
        blocked=[maint.blocked_item("f2", "dep", "retry")],
    )
    md = maint.render_outcomes_markdown(out)
    assert "**[sync]** `vault/` — reconciled" in md
    assert "**Finding 1:** f" in md and "**Proposed action:** do x" in md
    assert "**Blocking on:** dep" in md
