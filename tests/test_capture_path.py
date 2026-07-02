"""S09 — UX-01: write_note-routed capture path + drain tripwire.

Verifies:
1. capture.enforce() guarantees required frontmatter (additive, non-clobbering).
2. capture.validate() catches missing keys and bad classification.
3. HOST capture: signed + audited + file written + index synced.
4. VM capture: routes to draft_capture (capture-inbox/), NEVER touches signing key.
5. Cowork-direct-write-blocked hard test: `brain write` on VM → role_forbidden (exit 4).
6. Drain tripwire: brief shows tripwire when captures pending and drain stalls.
7. Drain tripwire clears when drain succeeds.

Offline + deterministic: HashEmbedder + BruteForceBackend, env-injected audit key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import cli
from brain import config
from brain.capture import enforce, validate, REQUIRED_KEYS
from brain.core import BrainCore, RoleError
from brain.embed import HashEmbedder
from brain.index import BrainIndex
from brain.vectors import BruteForceBackend

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mini_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMap.\n",
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
    # Pass audit_log explicitly to isolate each test's chain (avoids cross-test
    # signature collisions from the global system-level audit.jsonl).
    return BrainCore(vault=vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")


def _vm_core(vault: Path, snap_db: Path) -> BrainCore:
    snap_idx = BrainIndex(
        db_path=snap_db, backend=BruteForceBackend(),
        embedder=HashEmbedder(), read_only=True,
    )
    return BrainCore(vault=vault, index=snap_idx, role="vm")


# ---------------------------------------------------------------------------
# enforce() — frontmatter enforcement
# ---------------------------------------------------------------------------

class TestEnforce:
    def test_adds_all_required_keys_when_absent(self):
        """Raw body with no frontmatter gets all required keys."""
        result = enforce("Just a thought.")
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        for key in REQUIRED_KEYS:
            assert key in meta, f"missing required key: {key}"

    def test_does_not_overwrite_existing_id(self):
        content = "---\nid: my-note\ntype: note\nclassification: Internal\ncreated: 2026-01-01\n---\n\nBody."
        result = enforce(content)
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        assert meta["id"] == "my-note"

    def test_override_takes_precedence_over_existing(self):
        content = "---\nid: existing-id\ntype: note\nclassification: Internal\ncreated: 2026-01-01\n---\n\nBody."
        result = enforce(content, override={"id": "override-id"})
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        assert meta["id"] == "override-id"

    def test_classification_defaults_to_internal_not_secret(self):
        """Missing classification must default to Internal (surfaceable by default)."""
        result = enforce("No classification here.")
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        assert meta.get("classification") == "Internal"

    def test_always_stamps_status_draft(self):
        content = "---\nid: x\ntype: note\nclassification: Internal\ncreated: 2026-01-01\n---\n\nBody."
        result = enforce(content)
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        assert meta.get("status") == "draft"

    def test_always_stamps_provenance_untrusted(self):
        result = enforce("Body only.")
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        assert meta.get("provenance.trust") == "untrusted"

    def test_preserves_extra_existing_keys(self):
        content = (
            "---\nid: n\ntype: note\nclassification: Internal\ncreated: 2026-01-01\n"
            "tags: [a, b]\n---\n\nBody."
        )
        result = enforce(content)
        from brain.frontmatter import parse_text
        meta, _ = parse_text(result)
        assert "tags" in meta

    def test_derive_id_is_deterministic(self):
        """Same body → same derived id."""
        body = "Deterministic capture note."
        id1 = enforce(body)
        id2 = enforce(body)
        from brain.frontmatter import parse_text
        assert parse_text(id1)[0]["id"] == parse_text(id2)[0]["id"]

    def test_idempotent_on_already_complete_note(self):
        """Calling enforce twice does not change the id or classification."""
        content = "---\nid: stable\ntype: note\nclassification: Confidential\ncreated: 2026-01-01\n---\n\nBody."
        first = enforce(content)
        second = enforce(first)
        from brain.frontmatter import parse_text
        m1, _ = parse_text(first)
        m2, _ = parse_text(second)
        assert m1["id"] == m2["id"] == "stable"
        assert m1["classification"] == m2["classification"] == "Confidential"


# ---------------------------------------------------------------------------
# validate() — ingest validation (host-side)
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_note_returns_no_errors(self):
        content = (
            "---\nid: v\ntitle: V\ntype: note\nclassification: Internal\n"
            "created: 2026-01-01\n---\n\nBody.\n"
        )
        assert validate(content) == []

    def test_missing_frontmatter_is_error(self):
        errors = validate("No frontmatter here.")
        assert errors and any("frontmatter" in e for e in errors)

    def test_missing_required_keys_reported(self):
        content = "---\nid: x\n---\n\nBody."
        errors = validate(content)
        # Should report at least type, classification, created as missing.
        assert len(errors) >= 2

    def test_unknown_classification_is_error(self):
        content = (
            "---\nid: x\ntype: note\nclassification: Bogus\ncreated: 2026-01-01\n---\n\nBody."
        )
        errors = validate(content)
        assert any("classification" in e.lower() for e in errors)

    def test_known_tiers_all_valid(self):
        from brain.classification import TIERS
        for tier in TIERS:
            content = (
                f"---\nid: x\ntype: note\nclassification: {tier}\ncreated: 2026-01-01\n---\n\nBody."
            )
            assert validate(content) == [], f"tier {tier} should be valid"


# ---------------------------------------------------------------------------
# HOST capture path (UX-01 host-routed capture proof)
# ---------------------------------------------------------------------------

class TestHostCapture:
    def test_host_capture_writes_file_and_signs(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        content = "---\nid: my-capture\ntype: note\nclassification: Internal\ncreated: 2026-06-27\n---\n\nCapture me.\n"
        res = core.capture(content, reason="test host capture")
        assert res["signed"] is True
        assert res["indexed"] is True
        assert res["role"] == "host"
        written = Path(res["path"])
        assert written.exists(), "file must be written to vault"
        # Audit chain must have a signed entry.
        chain = core.verify_audit()
        assert chain["status"] in ("ok",), f"audit chain broken: {chain}"

    def test_host_capture_note_immediately_retrievable(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        content = "---\nid: retrieve-me\ntype: note\nclassification: Internal\ncreated: 2026-06-27\n---\n\nArctic embed retrieval test.\n"
        core.capture(content)
        note = core.get("retrieve-me")
        assert note is not None, "captured note must be findable by id"

    def test_host_capture_enforces_frontmatter(self, tmp_path, audit_key_env):
        """Raw content gets complete frontmatter before being written."""
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        # Raw content — no frontmatter at all.
        res = core.capture("Raw thought with no frontmatter.")
        written = Path(res["path"])
        from brain.frontmatter import parse_text
        meta, _ = parse_text(written.read_text(encoding="utf-8"))
        for key in REQUIRED_KEYS:
            assert key in meta, f"written note missing required key: {key}"
        assert meta.get("classification") == "Internal"

    def test_host_capture_does_not_route_to_capture_inbox(self, tmp_path, audit_key_env):
        """HOST capture must write directly to vault, NOT to capture-inbox/."""
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        core.capture("Direct host note.")
        inbox = config.capture_inbox_dir(vault)
        pending = list(inbox.glob("*.md")) if inbox.is_dir() else []
        assert pending == [], (
            "HOST capture must NOT drop drafts to capture-inbox/ — "
            f"found {pending}"
        )


# ---------------------------------------------------------------------------
# VM capture path (Cowork-direct-write-blocked hard test)
# ---------------------------------------------------------------------------

class TestVMCapturePath:
    def test_vm_capture_routes_to_draft_not_write_note(self, tmp_path, audit_key_env):
        """VM capture MUST route to draft_capture (capture-inbox/), never write_note."""
        vault = _mini_vault(tmp_path)
        # Build host index, publish snapshot.
        host_idx = BrainIndex(
            db_path=tmp_path / "host.sqlite",
            backend=BruteForceBackend(),
            embedder=HashEmbedder(),
        )
        host_idx.rebuild(vault)
        from brain.snapshot import publish_snapshot, SNAPSHOT_DB
        snap_dir = vault / ".brain" / "snapshot"
        publish_snapshot(host_idx.db_path, snap_dir)
        snap_db = snap_dir / SNAPSHOT_DB

        vm_core = _vm_core(vault, snap_db)
        res = vm_core.capture("VM drop note.")
        assert res["signed"] is False, "VM must NOT sign"
        assert res["indexed"] is False, "VM must NOT index"
        assert res["role"] == "vm"
        assert "draft" in res
        # Draft must be in capture-inbox/, NOT directly in vault.
        draft_path = Path(res["draft"])
        assert str(config.capture_inbox_dir(vault)) in str(draft_path), (
            f"VM draft must land in capture-inbox/, got: {draft_path}"
        )

    def test_vm_write_note_raises_role_error(self, tmp_path, audit_key_env):
        """VM must not be able to call write_note directly."""
        vault = _mini_vault(tmp_path)
        from brain.snapshot import publish_snapshot, SNAPSHOT_DB
        host_idx = BrainIndex(
            db_path=tmp_path / "host.sqlite",
            backend=BruteForceBackend(), embedder=HashEmbedder(),
        )
        host_idx.rebuild(vault)
        snap_dir = vault / ".brain" / "snapshot"
        publish_snapshot(host_idx.db_path, snap_dir)
        vm_core = _vm_core(vault, snap_dir / SNAPSHOT_DB)
        with pytest.raises(RoleError):
            vm_core.write_note("brain/resources/escape.md", "bad write")

    def test_vm_audit_key_never_resolved(self, tmp_path, monkeypatch):
        """VM path must never call resolve_signing_key."""
        vault = _mini_vault(tmp_path)
        resolved_calls = []
        import brain.audit as audit_mod
        monkeypatch.setattr(
            audit_mod, "resolve_signing_key",
            lambda *a, **k: resolved_calls.append(1),
        )
        from brain.snapshot import publish_snapshot, SNAPSHOT_DB
        host_idx = BrainIndex(
            db_path=tmp_path / "host.sqlite",
            backend=BruteForceBackend(), embedder=HashEmbedder(),
        )
        host_idx.rebuild(vault)
        snap_dir = vault / ".brain" / "snapshot"
        publish_snapshot(host_idx.db_path, snap_dir)
        vm_core = _vm_core(vault, snap_dir / SNAPSHOT_DB)
        vm_core.capture("No key resolution please.")
        assert resolved_calls == [], (
            "VM capture path must NEVER call resolve_signing_key"
        )

    def test_cli_write_on_vm_returns_role_forbidden(self, tmp_path, monkeypatch):
        """brain write on VM role → exit 4 role_forbidden (the hard CLI test)."""
        monkeypatch.setenv("BRAIN_ROLE", "vm")
        vault = _mini_vault(tmp_path)
        from brain.snapshot import publish_snapshot, SNAPSHOT_DB
        host_idx = BrainIndex(
            db_path=tmp_path / "host.sqlite",
            backend=BruteForceBackend(), embedder=HashEmbedder(),
        )
        host_idx.rebuild(vault)
        snap_dir = vault / ".brain" / "snapshot"
        publish_snapshot(host_idx.db_path, snap_dir)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        ret = cli.main(["write", "brain/resources/x.md", "--content", "bad"])
        assert ret == 4, "write on VM must return 4 (role_forbidden)"

    def test_cli_capture_on_vm_routes_to_draft(self, tmp_path, monkeypatch):
        """brain capture on VM role → draft staged in capture-inbox/ (not blocked)."""
        monkeypatch.setenv("BRAIN_ROLE", "vm")
        vault = _mini_vault(tmp_path)
        from brain.snapshot import publish_snapshot, SNAPSHOT_DB
        host_idx = BrainIndex(
            db_path=tmp_path / "host.sqlite",
            backend=BruteForceBackend(), embedder=HashEmbedder(),
        )
        host_idx.rebuild(vault)
        snap_dir = vault / ".brain" / "snapshot"
        publish_snapshot(host_idx.db_path, snap_dir)
        monkeypatch.setenv("BRAIN_VAULT", str(vault))
        ret = cli.main(["capture", "--content", "VM thought.", "--json"])
        # Should succeed (0) — VM capture drops to draft, not rejected.
        assert ret == 0, "brain capture on VM must succeed (routes to draft_capture)"
        # Draft must appear in capture-inbox/.
        inbox = config.capture_inbox_dir(vault)
        drafts = list(inbox.glob("*.md"))
        assert drafts, "VM capture must create a draft in capture-inbox/"


# ---------------------------------------------------------------------------
# Drain tripwire
# ---------------------------------------------------------------------------

class TestDrainTripwire:
    def test_tripwire_fires_when_drain_stalls(self):
        """Tripwire appears when pending > 0 and drain promoted=0 skipped>0."""
        from brain.brief import build_brief
        brief = build_brief(
            index_stats={"notes": 5, "chunks": 10},
            recent_notes=[],
            pending_before_drain=2,
            drain_result={"promoted": 0, "skipped": 2},
            snapshot_age_hours=None,
        )
        assert brief["tripwire"] is not None, "tripwire must fire when drain stalls"
        assert "2 captures pending" in brief["tripwire"]
        assert brief["drain"]["stalled"] is True

    def test_tripwire_absent_when_drain_succeeds(self):
        from brain.brief import build_brief
        brief = build_brief(
            index_stats={"notes": 5, "chunks": 10},
            recent_notes=[],
            pending_before_drain=2,
            drain_result={"promoted": 2, "skipped": 0},
            snapshot_age_hours=None,
        )
        assert brief["tripwire"] is None, "tripwire must clear when drain succeeds"
        assert brief.get("drain_note") is not None

    def test_tripwire_absent_when_nothing_pending(self):
        from brain.brief import build_brief
        brief = build_brief(
            index_stats={"notes": 5, "chunks": 10},
            recent_notes=[],
            pending_before_drain=0,
            drain_result={"promoted": 0, "skipped": 0},
            snapshot_age_hours=None,
        )
        assert brief["tripwire"] is None

    def test_core_brief_includes_tripwire_field(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _host_core(tmp_path, vault)
        # Stage a draft manually in capture-inbox/ without a signing key to
        # simulate a stalled drain.
        inbox = config.capture_inbox_dir(vault)
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "fake-draft.md").write_text(
            "---\nid: fake\ntype: note\nstatus: draft\nprovenance.trust: untrusted\n---\n\nStalled.\n",
            encoding="utf-8",
        )
        # Patch drain_drafts to simulate a stall (returns skipped=1, promoted=0).
        original_drain = core.drain_drafts.__func__

        def _stalled_drain(self):
            return {"promoted": 0, "skipped": 1, "details": {}}

        core.drain_drafts = lambda: _stalled_drain(core)
        brief = core.brief()
        assert "tripwire" in brief
        assert brief["tripwire"] is not None
        assert "captures pending" in brief["tripwire"]

    def test_brief_format_includes_tripwire_line(self):
        from brain.brief import build_brief, format_brief
        brief = build_brief(
            index_stats={"notes": 3, "chunks": 6},
            recent_notes=[],
            pending_before_drain=1,
            drain_result={"promoted": 0, "skipped": 1},
            snapshot_age_hours=2.5,
        )
        text = format_brief(brief)
        assert "⚠" in text, "formatted brief must show ⚠ when tripwire fires"
        assert "captures pending" in text
