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

import datetime as _dt
import json
from pathlib import Path

import pytest

from brain import cli
from brain import config as cfg
from brain.brief import (
    build_brief, format_brief, build_digest, format_digest,
    parse_hot_entries, render_brief_html, render_digest_html,
)
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


def _mixed_classification_vault(tmp_path: Path) -> Path:
    """A tiny vault with one Internal note and one Restricted note that both
    carry a dangling wikilink, so BOTH the ``recent``/``revisit_sample`` gate
    path AND the ``stale_links`` gate path (from/target sub-dicts) get
    exercised by a single fixture."""
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMap. [[vanished-target]]\n",
        encoding="utf-8",
    )
    (v / "brain" / "resources" / "public-note.md").write_text(
        "---\nid: public-note\ntitle: Public Note\ntype: note\nclassification: Internal\n"
        "created: 2026-07-01\nupdated: 2026-07-05\n---\n\nPublic content.\n",
        encoding="utf-8",
    )
    (v / "brain" / "resources" / "secret-note.md").write_text(
        "---\nid: secret-note\ntitle: TOPSECRET Codename Meridian\ntype: note\n"
        "classification: Restricted\ncreated: 2026-07-01\nupdated: 2026-07-05\n---\n\n"
        "Sensitive content. [[another-vanished-target]]\n",
        encoding="utf-8",
    )
    return v


# ---------------------------------------------------------------------------
# AUT-01/AUT-03 — HTML renderer smoke tests (pure, no I/O)
# ---------------------------------------------------------------------------

class TestRenderBriefHtmlPure:
    def _base_brief(self):
        return build_brief(
            index_stats={"notes": 3, "chunks": 6},
            recent_notes=[{"id": "seed", "title": "Seed", "classification": "Internal",
                          "updated": "2026-07-05"}],
            pending_before_drain=0, drain_result={"promoted": 0, "skipped": 0},
            snapshot_age_hours=1.5,
        )

    def test_valid_html_document_no_script(self):
        html = render_brief_html(self._base_brief())
        assert html.startswith("<!DOCTYPE html>")
        assert "<html" in html and "</html>" in html
        assert "<script" not in html.lower()

    def test_all_five_sections_present(self):
        html = render_brief_html(self._base_brief())
        for heading in (
            "Pending captures", "Notes added / updated", "What needs a re-read",
            "Open recommendations", "Index health",
        ):
            assert heading in html, f"missing section: {heading}"

    def test_neutral_fallback_when_no_brand(self):
        html = render_brief_html(self._base_brief())
        assert "Brain Brief" in html
        assert "#2563eb" in html

    def test_brand_overrides_title_owner_and_accent(self):
        brand = {"present": True, "title": "Acme Brief", "owner_name": "Jordan Rivers",
                  "accent_color": "#ff0000"}
        html = render_brief_html(self._base_brief(), brand=brand)
        assert "Acme Brief" in html
        assert "Jordan Rivers" in html
        assert "#ff0000" in html

    def test_maintenance_line_shown_when_autoresearch_stale(self):
        html = render_brief_html(
            self._base_brief(),
            autoresearch={"never_run": True, "age_days": None, "last_run": None, "stale": True},
        )
        assert "autoresearch" in html.lower()

    def test_maintenance_line_absent_when_autoresearch_fresh(self):
        html = render_brief_html(
            self._base_brief(),
            autoresearch={"never_run": False, "age_days": 5, "last_run": "2026-07-01", "stale": False},
        )
        assert "autoresearch" not in html.lower()

    def test_xss_note_title_script_tag_escaped(self):
        b = build_brief(
            index_stats={"notes": 1, "chunks": 1},
            recent_notes=[{"id": "<script>alert(1)</script>", "classification": "Internal",
                          "updated": "2026-07-05"}],
            pending_before_drain=0, drain_result={}, snapshot_age_hours=None,
        )
        html = render_brief_html(b)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_xss_onerror_payload_in_recommendation_text_escaped(self):
        html = render_brief_html(
            self._base_brief(),
            open_recommendations=[{"id": "r1", "text": "<img src=x onerror=alert(1)>", "status": "open"}],
        )
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img" in html

    def test_xss_hot_head_entry_escaped(self):
        html = render_brief_html(self._base_brief(), hot_head=["<script>bad()</script> — title"])
        assert "<script>bad()</script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderDigestHtmlPure:
    def _base_digest(self):
        return build_digest(
            index_stats={"notes": 5},
            recent_notes=[
                {"id": "proj-note", "title": "Proj Note", "classification": "Internal",
                 "zone": "projects", "updated": _dt.date.today().isoformat()},
                {"id": "arch-note", "title": "Arch Note", "classification": "Public",
                 "zone": "archive", "updated": _dt.date.today().isoformat()},
            ],
            days=7,
        )

    def test_valid_html_document_no_script(self):
        html = render_digest_html(self._base_digest())
        assert html.startswith("<!DOCTYPE html>")
        assert "<script" not in html.lower()

    def test_neutral_fallback_when_no_brand(self):
        html = render_digest_html(self._base_digest())
        assert "Brain Digest" in html

    def test_zone_ordering_projects_before_archive(self):
        html = render_digest_html(self._base_digest())
        assert html.index("proj-note") < html.index("arch-note")

    def test_xss_note_title_script_tag_escaped(self):
        d = build_digest(
            index_stats={"notes": 1},
            recent_notes=[{"id": "x", "title": "<script>alert(1)</script>",
                          "classification": "Internal", "zone": "projects",
                          "updated": _dt.date.today().isoformat()}],
            days=7,
        )
        html = render_digest_html(d)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_xss_onerror_payload_escaped(self):
        d = build_digest(
            index_stats={"notes": 1},
            recent_notes=[{"id": "y", "title": "<img src=x onerror=alert(1)>",
                          "classification": "Internal", "zone": "projects",
                          "updated": _dt.date.today().isoformat()}],
            days=7,
        )
        html = render_digest_html(d)
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img" in html


class TestParseHotEntries:
    def test_extracts_header_lines_in_order(self):
        text = (
            "<!-- idempotency-key: rec:abc -->\n"
            "## 2026-07-05 — Recommendation aged: Do the thing\n"
            "- **Context:** blah\n\n"
            "## 2026-07-06 — Another one\n"
            "- **Context:** blah2\n"
        )
        assert parse_hot_entries(text) == [
            "2026-07-05 — Recommendation aged: Do the thing",
            "2026-07-06 — Another one",
        ]

    def test_empty_text_returns_empty_list(self):
        assert parse_hot_entries("") == []
        assert parse_hot_entries(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AUT-01/AUT-03 REQUIRED test: a Restricted/MNPI note must never appear in
# generated brief or digest HTML — end-to-end (index -> gate -> render -> file).
# ---------------------------------------------------------------------------

class TestRestrictedNeverLeaksIntoHtml:
    def test_brief_html_excludes_restricted_and_mnpi_notes(self, populated_core):
        res = populated_core.brief_html(drain=False, max_recent=20)
        html = Path(res["path"]).read_text(encoding="utf-8")
        assert "restricted-deal" not in html
        assert "mnpi-merger" not in html
        assert "Meridian" not in html
        assert "public-overview" in html or "internal-arch" in html  # sanity: something surfaces

    def test_digest_html_excludes_restricted_and_mnpi_notes(self, populated_core):
        res = populated_core.digest_html(days=3650)
        html = Path(res["path"]).read_text(encoding="utf-8")
        assert "restricted-deal" not in html
        assert "mnpi-merger" not in html
        assert "Meridian" not in html

    def test_brief_html_stale_link_gating_drops_restricted_from_side(self, tmp_path, audit_key_env):
        vault = _mixed_classification_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        res = core.brief_html(drain=False, max_recent=20)
        html = Path(res["path"]).read_text(encoding="utf-8")
        assert "secret-note" not in html
        assert "TOPSECRET" not in html
        assert "Meridian" not in html
        # sanity: the Internal note's own stale link DOES surface
        assert "vanished-target" in html


# ---------------------------------------------------------------------------
# AUT-01/AUT-03 — dated + latest file emission, and the maintain fold
# ---------------------------------------------------------------------------

class TestBriefDigestHtmlFileEmission:
    def test_brief_html_writes_dated_and_latest_copy(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        today = _dt.date(2026, 7, 5)
        res = core.brief_html(drain=False, today=today)
        dated, latest = Path(res["path"]), Path(res["latest_path"])
        assert dated.name == "brief-2026-07-05.html"
        assert latest.name == "brief-latest.html"
        assert dated.read_text(encoding="utf-8") == latest.read_text(encoding="utf-8")
        assert dated.stat().st_size > 0

    def test_digest_html_writes_dated_and_latest_copy(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        today = _dt.date(2026, 7, 5)
        res = core.digest_html(days=7, today=today)
        dated, latest = Path(res["path"]), Path(res["latest_path"])
        assert dated.name == "digest-2026-07-05.html"
        assert latest.name == "digest-latest.html"
        assert dated.stat().st_size > 0

    def test_maintain_emits_both_html_files_on_a_fresh_sunday_run(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        sunday = _dt.date(2026, 7, 5)
        assert sunday.weekday() == 6  # confirm the fixture date IS a Sunday
        res = core.maintain(dry_run=False, today=sunday)
        brief_dir = cfg.brief_dir(vault)
        assert (brief_dir / f"brief-{sunday.isoformat()}.html").exists()
        assert (brief_dir / f"digest-{sunday.isoformat()}.html").exists()
        assert (brief_dir / "brief-latest.html").exists()
        assert (brief_dir / "digest-latest.html").exists()
        assert res["results"]["brief_html"]["path"]
        assert res["results"]["digest_html"]["path"]

    def test_maintain_dry_run_writes_no_html_files(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        sunday = _dt.date(2026, 7, 5)
        core.maintain(dry_run=True, today=sunday)
        assert not cfg.brief_dir(vault).exists()


# ---------------------------------------------------------------------------
# HARDENED:codex-verify-r1 — HTML file output is HOST-ONLY: role=vm refuses
# BEFORE any file write (never renders to a file surface for the VM leg).
# ---------------------------------------------------------------------------

class TestCliHtmlHostOnlyRefusal:
    def test_brief_html_refused_on_vm_before_any_file_write(
        self, tmp_path, audit_key_env, monkeypatch, capsys,
    ):
        vault = _mini_vault(tmp_path)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
        cli.main(["rebuild", "--json"])
        capsys.readouterr()
        ret = cli.main(["--role", "vm", "brief", "--html", "--json"])
        assert ret == 4
        data = json.loads(capsys.readouterr().out)
        assert data["error"] == "role_forbidden"
        assert not cfg.brief_dir(vault).exists()

    def test_digest_html_refused_on_vm_before_any_file_write(
        self, tmp_path, audit_key_env, monkeypatch, capsys,
    ):
        vault = _mini_vault(tmp_path)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
        cli.main(["rebuild", "--json"])
        capsys.readouterr()
        ret = cli.main(["--role", "vm", "digest", "--html", "--json"])
        assert ret == 4
        assert not cfg.brief_dir(vault).exists()

    def test_brief_html_still_works_on_host(
        self, tmp_path, audit_key_env, monkeypatch, capsys,
    ):
        vault = _mini_vault(tmp_path)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
        cli.main(["rebuild", "--json"])
        capsys.readouterr()
        ret = cli.main(["brief", "--no-drain", "--html", "--json"])
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert Path(data["path"]).exists()


# ---------------------------------------------------------------------------
# Committed evidence samples (_evidence/s09/) — ADR-0003 Ruling e: MUST be
# generated with the NEUTRAL FALLBACK overlay, never real owner content.
# ---------------------------------------------------------------------------

_EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "_evidence" / "s09"


class TestEvidenceSamplesUseNeutralFallback:
    def test_brief_and_digest_samples_exist(self):
        assert (_EVIDENCE_DIR / "brief.html").is_file()
        assert (_EVIDENCE_DIR / "digest.html").is_file()
        assert (_EVIDENCE_DIR / "brief.html").stat().st_size > 0
        assert (_EVIDENCE_DIR / "digest.html").stat().st_size > 0

    def test_samples_carry_no_real_owner_marker_strings(self):
        brief_html = (_EVIDENCE_DIR / "brief.html").read_text(encoding="utf-8")
        digest_html = (_EVIDENCE_DIR / "digest.html").read_text(encoding="utf-8")
        assert "Brain Brief" in brief_html
        assert "Brain Digest" in digest_html
        for text in (brief_html, digest_html):
            assert "<script" not in text.lower()
            assert "Ricardo" not in text
            assert "autopsias" not in text
