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
import time
from contextlib import redirect_stdout

import pytest

from brain import cli, config, maintenance as maint
from brain.core import BrainCore, RoleError


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _json(argv) -> tuple[int, dict]:
    code, out = _run(argv)
    return code, json.loads(out)


HOST_WRITE_VERBS = ["check", "health", "curate", "integrity", "promote-scan", "maintain", "graphify"]


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


@pytest.mark.parametrize("method", ["check", "health", "curate", "integrity", "promote_scan", "maintain", "graphify"])
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
    monkeypatch.setenv("BRAIN_AUDIT_KEYCHAIN_SERVICE", "profile-a-brain-test-absent-xyz")
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
# `maintain` — the umbrella + date-gated branches (ADR-0003 Ruling 5/(d) —
# due-since-last-run catch-up, not calendar-day-only).
# ---------------------------------------------------------------------------
# STEADY_STATE[d] = the per-branch last_run a vault would show on the MORNING
# of `d` if brain-nightly had been running every day without a miss — i.e.
# each branch's last_run is its most recent occurrence strictly BEFORE `d`.
# Verified against July 2026's real calendar: Jul 1 2026 = Wednesday.
STEADY_STATE = {
    "2026-07-06": {"daily": "2026-07-05", "health": "2026-06-29", "integrity": "2026-06-30",
                   "digest": "2026-07-05", "graphify": "2026-07-01"},
    "2026-07-07": {"daily": "2026-07-06", "health": "2026-07-06", "integrity": "2026-06-30",
                   "digest": "2026-07-05", "graphify": "2026-07-01"},
    "2026-07-12": {"daily": "2026-07-11", "health": "2026-07-06", "integrity": "2026-07-07",
                   "digest": "2026-07-05", "graphify": "2026-07-01"},
    "2026-07-01": {"daily": "2026-06-30", "health": "2026-06-29", "integrity": "2026-06-30",
                   "digest": "2026-06-28", "graphify": "2026-06-01"},
    "2026-07-08": {"daily": "2026-07-07", "health": "2026-07-06", "integrity": "2026-07-07",
                   "digest": "2026-07-05", "graphify": "2026-07-01"},
}


def _seed_state(core: BrainCore, per_branch_last_run: dict) -> None:
    """Write a ``.brain/maintain-state.json`` as if every listed branch's last
    run succeeded on the given date (steady-state fixture)."""
    path = config.maintain_state_path(core.vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({b: {"last_run": v, "status": "ok"} for b, v in per_branch_last_run.items()}),
        encoding="utf-8",
    )


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
def test_maintain_branches_date_gate_steady_state(iso_date, expected):
    d = datetime.date.fromisoformat(iso_date)
    assert set(maint.maintain_branches(d, last_runs=STEADY_STATE[iso_date])) == expected


def test_maintain_branches_no_state_means_everything_due():
    """A brand-new install (no ``.brain/maintain-state.json`` yet) has every
    branch due immediately, regardless of weekday — safe because every
    branch is idempotent (ADR-0003 Ruling d)."""
    d = datetime.date.fromisoformat("2026-07-08")  # a plain Wednesday
    all_branches = {"daily", "health", "integrity", "digest", "graphify"}
    assert set(maint.maintain_branches(d, last_runs=None)) == all_branches
    assert set(maint.maintain_branches(d, last_runs={})) == all_branches
    assert set(maint.maintain_branches(d)) == all_branches  # default


def test_maintain_branches_catchup_fires_missed_sunday_exactly_once():
    """The laptop was off on 2026-07-05 (a Sunday) — digest last ran the
    Sunday before (2026-06-28). By 2026-07-08 (Wed) the missed digest is
    still due, fires once, and does not re-fire again until the real next
    Sunday (2026-07-12)."""
    last_runs = {"daily": "2026-07-07", "health": "2026-07-06", "integrity": "2026-07-07",
                 "digest": "2026-06-28", "graphify": "2026-07-01"}
    d = datetime.date.fromisoformat("2026-07-08")
    assert "digest" in maint.maintain_branches(d, last_runs=last_runs)

    # Once the catch-up run's marker advances to today, the SAME day's rerun
    # (or the next day) must not re-fire it again.
    last_runs["digest"] = d.isoformat()
    assert "digest" not in maint.maintain_branches(d, last_runs=last_runs)
    later = datetime.date.fromisoformat("2026-07-09")
    assert "digest" not in maint.maintain_branches(later, last_runs=last_runs)
    next_sunday = datetime.date.fromisoformat("2026-07-12")
    assert "digest" in maint.maintain_branches(next_sunday, last_runs=last_runs)


def test_maintain_dry_run_proves_real_work_without_mutating(populated_core, populated_vault):
    """The CUT-05 evidence shape: --dry-run must still run REAL reads (health
    selftest probe, near-dup scan) against the populated corpus on a due
    branch, while never mutating the index or touching the vault tree."""
    before_notes = populated_core.index.stats()["notes"]
    tuesday = datetime.date.fromisoformat("2026-07-07")
    _seed_state(populated_core, STEADY_STATE["2026-07-07"])
    state_path = config.maintain_state_path(populated_core.vault)
    state_before = state_path.read_text(encoding="utf-8")

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
    # dry-run never mutates the state file (no marker advance, no rewrite).
    assert state_path.read_text(encoding="utf-8") == state_before


def test_maintain_real_run_syncs_and_publishes(populated_core):
    monday = datetime.date.fromisoformat("2026-07-06")
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])
    res = populated_core.maintain(dry_run=False, today=monday)
    assert res["results"]["sync"]["mode"] == "incremental"
    assert "snapshot" in res["results"]["sync"]
    assert "health" in res["results"]
    assert res["results"]["health"]["selftest"]["probe_ok"] is True

    state = json.loads(config.maintain_state_path(populated_core.vault).read_text(encoding="utf-8"))
    assert state["daily"]["last_run"] == monday.isoformat()
    assert state["health"]["last_run"] == monday.isoformat()
    assert state["integrity"]["last_run"] == STEADY_STATE["2026-07-06"]["integrity"]  # untouched, not due


def test_maintain_cli_dry_run_json_shape(populated_core, populated_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(populated_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: populated_core)
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])

    code, res = _json(["maintain", "--dry-run", "--date", "2026-07-06", "--json"])
    assert code == 0
    assert res["ritual"] == "maintain" and res["dry_run"] is True
    assert res["weekday"] == "Monday"
    assert set(res["outcomes"]) == {"auto_fixed", "action_required", "blocked", "counts"}


# ---------------------------------------------------------------------------
# single-runner lock + crash-before-marker (HARDENED:codex)
# ---------------------------------------------------------------------------
def test_maintain_skips_when_another_run_holds_the_lock(populated_core):
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])
    state_path = config.maintain_state_path(populated_core.vault)
    state_before = state_path.read_text(encoding="utf-8")
    lock_path = config.maintain_lock_path(populated_core.vault)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999, "started": time.time()}), encoding="utf-8")

    res = populated_core.maintain(dry_run=False, today=datetime.date.fromisoformat("2026-07-06"))
    assert res.get("skipped") == "locked"
    assert res["outcomes"]["counts"] == {"auto_fixed": 0, "action_required": 0, "blocked": 0}
    # the held lock is untouched — a live-looking lock is never broken.
    assert lock_path.exists()
    # a skipped run must not touch the state file at all.
    assert state_path.read_text(encoding="utf-8") == state_before


def test_maintain_breaks_a_stale_abandoned_lock(populated_core):
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])
    lock_path = config.maintain_lock_path(populated_core.vault)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # A lock "started" far beyond the stale-after window looks like a crashed
    # prior run, not a live concurrent one -- it must be broken automatically.
    lock_path.write_text(
        json.dumps({"pid": 999999, "started": time.time() - 10 * 3600}), encoding="utf-8"
    )
    res = populated_core.maintain(dry_run=False, today=datetime.date.fromisoformat("2026-07-06"))
    assert "skipped" not in res
    assert res["results"]["sync"]["mode"] == "incremental"


def test_maintain_crash_before_marker_leaves_branch_due_and_rerun_fires_once(populated_core, monkeypatch):
    monday = datetime.date.fromisoformat("2026-07-06")
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])

    real_health = populated_core.health

    def _boom():
        raise RuntimeError("simulated crash mid-health-branch")

    monkeypatch.setattr(populated_core, "health", _boom)
    res1 = populated_core.maintain(dry_run=False, today=monday)
    assert res1["outcomes"]["counts"]["blocked"] >= 1

    state_path = config.maintain_state_path(populated_core.vault)
    state1 = json.loads(state_path.read_text(encoding="utf-8"))
    # the crash left the marker UNADVANCED -- health is still due.
    assert state1["health"]["last_run"] == STEADY_STATE["2026-07-06"]["health"]
    assert state1["health"]["failed"] is True
    assert state1["health"]["consecutive_failures"] == 1
    # the daily branch (unaffected by health's crash) still succeeded.
    assert state1["daily"]["last_run"] == monday.isoformat()

    monkeypatch.setattr(populated_core, "health", real_health)
    res2 = populated_core.maintain(dry_run=False, today=monday)
    assert "health" in res2["branches_due"]  # still due -- the crash left it pending
    state2 = json.loads(state_path.read_text(encoding="utf-8"))
    assert state2["health"]["last_run"] == monday.isoformat()  # now advanced, exactly once
    assert state2["health"]["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# hot-queue idempotency (HARDENED:codex) + Sunday curate/promote-scan folds
# ---------------------------------------------------------------------------
def test_append_hot_once_is_idempotent(populated_core):
    key = "maintain:curate:2026-07-12"
    first = populated_core._append_hot_once(key, "## entry one\ncontent\n")
    second = populated_core._append_hot_once(key, "## entry one (dup attempt)\n")
    assert first is True
    assert second is False
    hot_path = config.memory_dir(populated_core.vault) / "hot.md"
    text = hot_path.read_text(encoding="utf-8")
    assert text.count(key) == 1
    assert "dup attempt" not in text


def test_maintain_sunday_branch_runs_curate_and_promote_scan_and_queues_hot_findings(populated_core):
    sunday = datetime.date.fromisoformat("2026-07-12")
    _seed_state(populated_core, STEADY_STATE["2026-07-12"])

    res = populated_core.maintain(dry_run=False, today=sunday)
    assert "curate" in res["results"]
    assert "promote_scan" in res["results"]
    assert res["results"]["promote_scan"]["candidates"]  # real raw/ candidates in the fixture

    hot_text = (config.memory_dir(populated_core.vault) / "hot.md").read_text(encoding="utf-8")
    assert f"maintain:promote-scan:{sunday.isoformat()}" in hot_text


# ---------------------------------------------------------------------------
# `brain status` heartbeat surfacing (HARDENED:premortem)
# ---------------------------------------------------------------------------
def test_status_surfaces_maintain_heartbeat(populated_core):
    monday = datetime.date.fromisoformat("2026-07-06")
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])
    populated_core.maintain(dry_run=False, today=monday)

    res = populated_core.status()
    hb = res["maintain_heartbeat"]
    assert hb["status"] == "ok"
    assert hb["branches"]["daily"]["last_run"] == monday.isoformat()


def test_status_flags_repeated_branch_failures(populated_core, monkeypatch):
    monday = datetime.date.fromisoformat("2026-07-06")
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])
    monkeypatch.setattr(populated_core, "health", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    populated_core.maintain(dry_run=False, today=monday)
    populated_core.maintain(dry_run=False, today=monday)

    hb = populated_core.status()["maintain_heartbeat"]
    assert "health" in hb["repeated_failure_branches"]
    assert hb["status"] == "repeated_failures"


# ---------------------------------------------------------------------------
# curate: stale-wikilink-target detection + age x centrality revisit sample
# (AUT-02) — real engagement over the dense synthetic fixture.
# ---------------------------------------------------------------------------
def test_curate_finds_stale_wikilink_targets(synthetic_core):
    res = synthetic_core.curate(dry_run=True, today=datetime.date.fromisoformat("2026-07-05"))
    assert res["stale_links"], "expected stale links in the dense synthetic fixture"
    reasons = {s["reason"] for s in res["stale_links"]}
    assert "vanished" in reasons
    assert "archived" in reasons


def test_curate_revisit_sample_is_ranked_descending(synthetic_core):
    res = synthetic_core.curate(dry_run=True, today=datetime.date.fromisoformat("2026-07-05"))
    sample = res["revisit_sample"]
    assert len(sample) >= 1
    scores = [r["score"] for r in sample]
    assert scores == sorted(scores, reverse=True)


def test_curate_cli_gates_stale_links_and_revisit_sample(synthetic_core, synthetic_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(synthetic_vault))
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: synthetic_core)

    code, res = _json(["curate", "--dry-run", "--json", "--max-tier", "MNPI"])
    assert code == 0
    assert res["stale_egress"]["total"] >= 1
    assert res["revisit_egress"]["total"] >= 1

    code2, res2 = _json(["curate", "--dry-run", "--json", "--max-tier", "Public"])
    assert code2 == 0
    # a strict Public cap withholds at least some higher-tier revisit rows.
    assert len(res2["revisit_sample"]) <= len(res["revisit_sample"])


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


# ---------------------------------------------------------------------------
# recommendations lifecycle (MEM-03): open -> aging -> surfaced -> resolved
# ---------------------------------------------------------------------------
def test_recommendations_aging_scan_boundary():
    today = datetime.date(2026, 7, 20)
    entries = [
        {"id": "rec-a", "created": "2026-07-06", "text": "A", "status": "open"},   # age 14 -> surfaces
        {"id": "rec-b", "created": "2026-07-07", "text": "B", "status": "open"},   # age 13 -> stays open
        {"id": "rec-c", "created": "2026-06-01", "text": "C", "status": "surfaced",
         "surfaced_at": "2026-06-20"},                                             # already surfaced
    ]
    updated, newly = maint.recommendations_aging_scan(entries, today, aging_days=14)

    assert {e["id"] for e in newly} == {"rec-a"}
    by_id = {e["id"]: e for e in updated}
    assert by_id["rec-a"]["status"] == "surfaced" and by_id["rec-a"]["surfaced_at"] == today.isoformat()
    assert by_id["rec-b"]["status"] == "open"
    assert by_id["rec-c"]["status"] == "surfaced" and by_id["rec-c"]["surfaced_at"] == "2026-06-20"


def test_recommendations_aging_scan_never_raises_on_corrupt_lines():
    entries = maint.parse_recommendation_lines(
        "not json at all\n"
        '{"id": "rec-ok", "created": "2026-06-01", "status": "open"}\n'
        "\n"
        "[1, 2, 3]\n"  # valid JSON but not a dict -> dropped
    )
    assert [e["id"] for e in entries] == ["rec-ok"]


def test_resolve_recommendation_moves_entry_to_log_line():
    entries = [{"id": "rec-x", "created": "2026-06-01", "text": "Investigate X", "status": "surfaced"}]
    remaining, log_line = maint.resolve_recommendation(
        entries, "rec-x", "done, filed as note", datetime.date(2026, 7, 20))
    assert remaining == []
    assert "Investigate X" in log_line and "done, filed as note" in log_line


def test_resolve_recommendation_missing_id_is_a_noop():
    entries = [{"id": "rec-x", "created": "2026-06-01", "status": "open"}]
    remaining, log_line = maint.resolve_recommendation(
        entries, "rec-does-not-exist", "n/a", datetime.date(2026, 7, 20))
    assert remaining == entries
    assert log_line is None


def test_recommendations_aging_fold_surfaces_once_and_updates_jsonl(populated_core):
    open_path = config.recommendations_open_path(populated_core.vault)
    open_path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"id": "rec-x", "created": "2026-06-01", "text": "Investigate X", "status": "open"},
        {"id": "rec-y", "created": "2026-07-15", "text": "Investigate Y (too new)", "status": "open"},
    ]
    open_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    today = datetime.date.fromisoformat("2026-07-20")
    res = populated_core._recommendations_aging_fold(today)
    assert res["surfaced"] == 1
    assert res["appended_to_hot"] == 1

    hot_text = (config.memory_dir(populated_core.vault) / "hot.md").read_text(encoding="utf-8")
    assert "rec-x" in hot_text
    assert "rec-y" not in hot_text

    updated_entries = [json.loads(line) for line in open_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {e["id"]: e for e in updated_entries}
    assert by_id["rec-x"]["status"] == "surfaced"
    assert by_id["rec-y"]["status"] == "open"

    # idempotent: re-running the fold must not duplicate the hot.md entry.
    res2 = populated_core._recommendations_aging_fold(today)
    assert res2["surfaced"] == 0
    hot_text2 = (config.memory_dir(populated_core.vault) / "hot.md").read_text(encoding="utf-8")
    assert hot_text2 == hot_text


def test_maintain_daily_branch_folds_in_recommendations_aging(populated_core):
    open_path = config.recommendations_open_path(populated_core.vault)
    open_path.parent.mkdir(parents=True, exist_ok=True)
    open_path.write_text(
        json.dumps({"id": "rec-old", "created": "2026-01-01", "text": "Old idea", "status": "open"}) + "\n",
        encoding="utf-8",
    )
    monday = datetime.date.fromisoformat("2026-07-06")
    _seed_state(populated_core, STEADY_STATE["2026-07-06"])

    res = populated_core.maintain(dry_run=False, today=monday)
    assert res["results"]["recommendations_aging"]["surfaced"] == 1
    hot_text = (config.memory_dir(populated_core.vault) / "hot.md").read_text(encoding="utf-8")
    assert "rec-old" in hot_text


# ---------------------------------------------------------------------------
# synthetic vault fixture shape (HARDENED:claude) — a regeneration that
# silently drops the archive-link / vanished-link cases would false-green
# every test above; pin the shape explicitly.
# ---------------------------------------------------------------------------
def test_synthetic_vault_fixture_has_expected_shape(synthetic_vault):
    md_files = list(synthetic_vault.rglob("*.md"))
    assert 50 <= len(md_files) <= 100
    assert (synthetic_vault / "brain" / "archive").is_dir()
    archive_notes = list((synthetic_vault / "brain" / "archive").glob("*.md"))
    assert len(archive_notes) >= 3

    # at least one active note's body links into archive/, and at least one
    # links to an id that resolves to nothing at all.
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in md_files)
    assert "[[legacy-" in corpus
    assert "[[deleted-idea-" in corpus
