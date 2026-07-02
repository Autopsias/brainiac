"""S09 — UX-02 + UX-03: morning brief + weekly digest.

Verifies:
1. build_brief() tripwire logic (stalled / succeeded / clean).
2. format_brief() output structure and quiet defaults.
3. build_digest() date filtering.
4. format_digest() output structure.
5. BrainCore.brief() on host: returns required keys.
6. BrainCore.digest() returns required keys.
7. CLI `brain brief --json` and `brain digest --json` work end-to-end.

Offline + deterministic: HashEmbedder + BruteForceBackend, env-injected audit key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import cli
from brain.brief import build_brief, format_brief, build_digest, format_digest
from brain.core import BrainCore
from brain.embed import HashEmbedder
from brain.index import BrainIndex
from brain.vectors import BruteForceBackend


def _mini_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMap.\n", encoding="utf-8"
    )
    (v / "brain" / "resources" / "seed.md").write_text(
        "---\nid: seed\ntitle: Seed\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nSeed note for testing.\n",
        encoding="utf-8",
    )
    return v


def _host_core(tmp_path: Path, vault: Path) -> BrainCore:
    idx = BrainIndex(
        db_path=tmp_path / "idx.sqlite",
        backend=BruteForceBackend(),
        embedder=HashEmbedder(),
    )
    idx.rebuild(vault)
    # Pass audit_log explicitly to isolate each test's chain.
    return BrainCore(vault=vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")


# ---------------------------------------------------------------------------
# build_brief() — pure function tests
# ---------------------------------------------------------------------------

class TestBuildBrief:
    def test_no_pending_no_tripwire(self):
        b = build_brief(
            index_stats={"notes": 5, "chunks": 10},
            recent_notes=[],
            pending_before_drain=0,
            drain_result={"promoted": 0, "skipped": 0},
            snapshot_age_hours=None,
        )
        assert b["tripwire"] is None
        assert b["drain"]["stalled"] is False
        assert b["notes"] == 5
        assert b["chunks"] == 10

    def test_stalled_drain_fires_tripwire(self):
        b = build_brief(
            index_stats={"notes": 3, "chunks": 6},
            recent_notes=[],
            pending_before_drain=3,
            drain_result={"promoted": 0, "skipped": 3},
            snapshot_age_hours=5.0,
        )
        assert b["tripwire"] is not None
        assert "3 captures pending" in b["tripwire"]
        assert "stalled" in b["tripwire"]
        assert b["drain"]["stalled"] is True

    def test_successful_drain_clears_tripwire_sets_drain_note(self):
        b = build_brief(
            index_stats={"notes": 2, "chunks": 4},
            recent_notes=[],
            pending_before_drain=2,
            drain_result={"promoted": 2, "skipped": 0},
            snapshot_age_hours=None,
        )
        assert b["tripwire"] is None
        assert b["drain_note"] is not None
        assert "2 capture" in b["drain_note"]

    def test_snapshot_age_formatted_minutes(self):
        b = build_brief(
            index_stats={}, recent_notes=[], pending_before_drain=0,
            drain_result={}, snapshot_age_hours=0.5,
        )
        assert b["snapshot_age"] == "30m"

    def test_snapshot_age_formatted_hours(self):
        b = build_brief(
            index_stats={}, recent_notes=[], pending_before_drain=0,
            drain_result={}, snapshot_age_hours=3.0,
        )
        assert "3.0h" in b["snapshot_age"]

    def test_snapshot_age_formatted_days(self):
        b = build_brief(
            index_stats={}, recent_notes=[], pending_before_drain=0,
            drain_result={}, snapshot_age_hours=49.0,
        )
        assert "d" in b["snapshot_age"]

    def test_recent_notes_capped_at_max_recent(self):
        notes = [{"id": f"n{i}", "updated": "2026-06-27", "classification": "Internal"} for i in range(10)]
        b = build_brief(
            index_stats={}, recent_notes=notes, pending_before_drain=0,
            drain_result={}, snapshot_age_hours=None, max_recent=3,
        )
        assert len(b["recent"]) == 3

    def test_required_keys_present(self):
        b = build_brief(
            index_stats={"notes": 1, "chunks": 2},
            recent_notes=[],
            pending_before_drain=0,
            drain_result={"promoted": 0, "skipped": 0},
            snapshot_age_hours=None,
        )
        for key in ("date", "notes", "chunks", "pending_before_drain", "drain", "tripwire"):
            assert key in b, f"brief missing key: {key}"


# ---------------------------------------------------------------------------
# format_brief() — human-readable output
# ---------------------------------------------------------------------------

class TestFormatBrief:
    def test_contains_date_line(self):
        b = build_brief(
            index_stats={"notes": 1, "chunks": 2}, recent_notes=[],
            pending_before_drain=0, drain_result={}, snapshot_age_hours=None,
        )
        text = format_brief(b)
        assert "brain brief ·" in text

    def test_shows_note_count(self):
        b = build_brief(
            index_stats={"notes": 42, "chunks": 84}, recent_notes=[],
            pending_before_drain=0, drain_result={}, snapshot_age_hours=None,
        )
        text = format_brief(b)
        assert "42 notes" in text

    def test_tripwire_shown_with_warning(self):
        b = build_brief(
            index_stats={}, recent_notes=[], pending_before_drain=1,
            drain_result={"promoted": 0, "skipped": 1}, snapshot_age_hours=None,
        )
        text = format_brief(b)
        assert "⚠" in text

    def test_clean_state_shows_checkmark(self):
        b = build_brief(
            index_stats={}, recent_notes=[], pending_before_drain=0,
            drain_result={"promoted": 0, "skipped": 0}, snapshot_age_hours=None,
        )
        text = format_brief(b)
        assert "✓" in text

    def test_recent_notes_listed(self):
        notes = [{"id": "my-note", "updated": "2026-06-27", "classification": "Internal"}]
        b = build_brief(
            index_stats={}, recent_notes=notes, pending_before_drain=0,
            drain_result={}, snapshot_age_hours=None,
        )
        text = format_brief(b)
        assert "my-note" in text

    def test_quiet_no_plumbing_noise(self):
        """Output must not leak paths, DB details, or backend names."""
        b = build_brief(
            index_stats={"notes": 2, "chunks": 4, "db": "/some/path.sqlite",
                         "vector_backend": "BruteForce"},
            recent_notes=[], pending_before_drain=0,
            drain_result={}, snapshot_age_hours=None,
        )
        text = format_brief(b)
        assert ".sqlite" not in text
        assert "BruteForce" not in text


# ---------------------------------------------------------------------------
# build_digest() + format_digest()
# ---------------------------------------------------------------------------

class TestBuildDigest:
    def test_filters_by_cutoff_date(self):
        import datetime
        today = datetime.date.today().isoformat()
        old = "2020-01-01"
        notes = [
            {"id": "new", "updated": today, "classification": "Internal"},
            {"id": "old", "updated": old, "classification": "Internal"},
        ]
        d = build_digest(index_stats={"notes": 2}, recent_notes=notes, days=7)
        ids = [n["id"] for n in d["notes"]]
        assert "new" in ids
        assert "old" not in ids

    def test_required_keys_present(self):
        d = build_digest(index_stats={"notes": 5}, recent_notes=[], days=7)
        for key in ("date", "period_days", "period_start", "notes_total", "notes_in_period", "notes"):
            assert key in d

    def test_notes_total_from_stats(self):
        d = build_digest(index_stats={"notes": 99}, recent_notes=[], days=7)
        assert d["notes_total"] == 99

    def test_notes_capped_at_20(self):
        import datetime
        today = datetime.date.today().isoformat()
        notes = [{"id": f"n{i}", "updated": today, "classification": "Internal"} for i in range(30)]
        d = build_digest(index_stats={"notes": 30}, recent_notes=notes, days=7)
        assert len(d["notes"]) == 20


class TestFormatDigest:
    def test_contains_header_line(self):
        d = build_digest(index_stats={"notes": 5}, recent_notes=[], days=7)
        text = format_digest(d)
        assert "brain digest ·" in text
        assert "past 7d" in text

    def test_shows_counts(self):
        d = build_digest(index_stats={"notes": 5}, recent_notes=[], days=7)
        text = format_digest(d)
        assert "5 notes total" in text


# ---------------------------------------------------------------------------
# BrainCore.brief() end-to-end
# ---------------------------------------------------------------------------

class TestCoreBrief:
    def test_returns_required_keys(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        b = core.brief(drain=False)
        for key in ("date", "notes", "chunks", "pending_before_drain", "drain", "tripwire"):
            assert key in b, f"core.brief() missing key: {key}"

    def test_note_count_matches_index(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        b = core.brief(drain=False)
        assert b["notes"] >= 2  # index.md + seed.md

    def test_no_drain_skips_drain(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        # Drop a draft.
        from brain import config as cfg
        inbox = cfg.capture_inbox_dir(vault)
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "pending.md").write_text(
            "---\nid: pending\ntype: note\nstatus: draft\nprovenance.trust: untrusted\n"
            "classification: Internal\ncreated: 2026-06-27\n---\n\nPending.\n",
            encoding="utf-8",
        )
        b = core.brief(drain=False)
        assert b["pending_before_drain"] == 1
        # No drain should leave the draft in place.
        assert (inbox / "pending.md").exists()


# ---------------------------------------------------------------------------
# BrainCore.digest() end-to-end
# ---------------------------------------------------------------------------

class TestCoreDigest:
    def test_returns_required_keys(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        d = core.digest(days=7)
        for key in ("date", "period_days", "notes_total", "notes_in_period", "notes"):
            assert key in d, f"core.digest() missing key: {key}"

    def test_period_days_respected(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        d = core.digest(days=30)
        assert d["period_days"] == 30


# ---------------------------------------------------------------------------
# CLI end-to-end: brain brief / brain digest
# ---------------------------------------------------------------------------

class TestCliBrief:
    def test_brief_json_returns_zero(self, tmp_path, audit_key_env, monkeypatch, capsys):
        vault = _mini_vault(tmp_path)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
        # Rebuild first; clear capsys so we only see the brief output.
        cli.main(["rebuild", "--json"])
        capsys.readouterr()  # discard rebuild output
        ret = cli.main(["brief", "--no-drain", "--json"])
        assert ret == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "date" in data and "notes" in data

    def test_digest_json_returns_zero(self, tmp_path, audit_key_env, monkeypatch, capsys):
        vault = _mini_vault(tmp_path)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
        cli.main(["rebuild", "--json"])
        capsys.readouterr()  # discard rebuild output
        ret = cli.main(["digest", "--days", "7", "--json"])
        assert ret == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "date" in data and "period_days" in data

    def test_brief_human_readable(self, tmp_path, audit_key_env, monkeypatch, capsys):
        vault = _mini_vault(tmp_path)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
        cli.main(["rebuild"])
        capsys.readouterr()  # discard rebuild output
        ret = cli.main(["brief", "--no-drain"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "brain brief ·" in out
