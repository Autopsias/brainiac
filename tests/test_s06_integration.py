"""S06 — multi-harness integration: VM read+draft-only hard guarantees, the
full Cowork capture loop (VM draft → host commit → snapshot publish → retrievable),
the optional MCP adapter (INT-03), and the per-harness wiring files (INT-01).

Offline + deterministic: HashEmbedder + BruteForceBackend, env-injected audit key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import cli
from brain import config
from brain import mcp_adapter
from brain.core import BrainCore, RoleError
from brain.index import BrainIndex
from brain.snapshot import SNAPSHOT_DB, publish_snapshot
from brain.vectors import BruteForceBackend
from brain.embed import HashEmbedder

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _mini_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: I\ntype: index\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMap.\n", encoding="utf-8")
    (v / "brain" / "resources" / "seed.md").write_text(
        "---\nid: seed\ntitle: Seed\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nseed about arctic embed.\n",
        encoding="utf-8")
    return v


def _host_index(tmp_path: Path) -> BrainIndex:
    return BrainIndex(db_path=tmp_path / "host.sqlite",
                      backend=BruteForceBackend(), embedder=HashEmbedder())


def _vm_index_on_snapshot(snap_db: Path) -> BrainIndex:
    return BrainIndex(db_path=snap_db, backend=BruteForceBackend(),
                      embedder=HashEmbedder(), read_only=True)


# --------------------------------------------------------------------------
# INT-02 hard test: VM binary is READ + DRAFT only
# --------------------------------------------------------------------------
def test_vm_core_constructs_no_audit_chain(tmp_path):
    v = _mini_vault(tmp_path)
    core = BrainCore(vault=v, role="vm")
    assert core.role == "vm"
    assert core.audit is None  # NO signing surface at all on the VM


def test_vm_cannot_write_note_and_never_resolves_key(tmp_path, monkeypatch):
    v = _mini_vault(tmp_path)
    calls = {"resolve": 0}
    import brain.audit as audit_mod
    monkeypatch.setattr(audit_mod, "resolve_signing_key",
                        lambda *a, **k: calls.__setitem__("resolve", calls["resolve"] + 1))
    core = BrainCore(vault=v, role="vm")
    with pytest.raises(RoleError):
        core.write_note("brain/resources/x.md", "x")
    with pytest.raises(RoleError):
        core.drain_drafts()
    with pytest.raises(RoleError):
        core.sync()
    with pytest.raises(RoleError):
        core.publish_snapshot()
    with pytest.raises(RoleError):
        core.rebuild()
    with pytest.raises(RoleError):
        core.verify_audit()
    assert calls["resolve"] == 0  # the VM never reaches signing-key resolution


def test_vm_index_is_read_only_cannot_write_and_creates_no_wal(tmp_path):
    v = _mini_vault(tmp_path)
    host = _host_index(tmp_path)
    host.rebuild(v)
    host.close()
    snap_dir = tmp_path / "snap"
    publish_snapshot(host.db_path, snap_dir)
    snap_db = snap_dir / SNAPSHOT_DB

    vm = _vm_index_on_snapshot(snap_db)
    # reads work
    assert vm.get("seed") is not None
    # writes are impossible — read-only database
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        vm.conn.execute("INSERT INTO meta(k, v) VALUES ('x', 'y')")
    vm.close()
    # ...and no WAL/SHM sidecar was ever created next to the snapshot
    assert not (snap_dir / (SNAPSHOT_DB + "-wal")).exists()
    assert not (snap_dir / (SNAPSHOT_DB + "-shm")).exists()


def test_published_snapshot_is_single_self_contained_file(tmp_path):
    v = _mini_vault(tmp_path)
    host = _host_index(tmp_path)
    host.rebuild(v)
    host.close()
    snap_dir = tmp_path / "snap"
    publish_snapshot(host.db_path, snap_dir)
    files = sorted(p.name for p in snap_dir.iterdir())
    assert files == ["index.snapshot.sqlite", "snapshot.manifest.json"]


# --------------------------------------------------------------------------
# INT-02: draft_capture is the ONE VM quasi-write (unsigned, untrusted, no index)
# --------------------------------------------------------------------------
def test_draft_capture_stages_unsigned_untrusted_not_indexed(tmp_path, monkeypatch):
    v = _mini_vault(tmp_path)
    import brain.audit as audit_mod
    monkeypatch.setattr(audit_mod, "resolve_signing_key",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("key resolved!")))
    core = BrainCore(vault=v, role="vm")
    content = (
        "---\nid: vmcap\ntitle: VM Capture\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nCaptured on the VM.\n")
    res = core.draft_capture(content)
    assert res["signed"] is False and res["indexed"] is False
    draft = Path(res["draft"])
    assert draft.exists()
    assert draft.parent == config.capture_inbox_dir(v)
    text = draft.read_text(encoding="utf-8")
    assert "status: draft" in text
    assert "provenance.trust: untrusted" in text
    # capture-inbox lives under .brain/ -> excluded from scan_vault (not auto-indexed)
    from brain.notes import scan_vault
    ids = {n.id for n in scan_vault(v)}
    assert "vmcap" not in ids


def test_draft_capture_synthesises_id_when_absent(tmp_path):
    v = _mini_vault(tmp_path)
    core = BrainCore(vault=v, role="vm")
    res = core.draft_capture("just a freeform thought, no frontmatter")
    assert res["id"].startswith("draft-")
    assert Path(res["draft"]).exists()


# --------------------------------------------------------------------------
# INT-02 r2-codex: the FULL loop — VM draft → host commit → snapshot → retrievable
# --------------------------------------------------------------------------
def test_full_loop_vm_draft_to_host_commit_to_snapshot_retrievable(tmp_path, audit_key_env):
    v = _mini_vault(tmp_path)

    # 1. VM stages a draft (no sign / no index / no WAL)
    vm_capture = BrainCore(vault=v, role="vm")
    vm_capture.draft_capture(
        "---\nid: vmcap\ntitle: VM Capture\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMeridian capture from the VM.\n")
    assert vm_capture._count_pending_drafts() == 1

    # 2. HOST drains + indexes + republishes the snapshot
    snap_dir = tmp_path / "snap"
    host = BrainCore(vault=v, index=_host_index(tmp_path),
                     audit_log=tmp_path / "audit.jsonl", role="host")
    host.rebuild()
    res = host.sync(drain=True, publish=False)
    assert res["drain"]["promoted"] == 1            # the draft was signed + promoted
    assert (v / "brain" / "resources" / "vmcap.md").is_file()
    m = host.publish_snapshot(snap_dir)
    assert m["generation"] >= 1
    assert host._count_pending_drafts() == 0        # capture-inbox drained

    # 3. VM reads the SAME note from the read-only snapshot — capture closed the loop
    vm_read = BrainCore(vault=v, index=_vm_index_on_snapshot(snap_dir / SNAPSHOT_DB),
                        role="vm")
    got = vm_read.get("vmcap")
    assert got is not None and got["id"] == "vmcap"
    hits = {h.id for h in vm_read.hybrid_search("Meridian capture")}
    assert "vmcap" in hits


def test_status_reports_snapshot_generation_age_and_pending(tmp_path, audit_key_env):
    v = _mini_vault(tmp_path)
    host = BrainCore(vault=v, index=_host_index(tmp_path),
                     audit_log=tmp_path / "audit.jsonl", role="host")
    host.rebuild()
    snap_dir = config.snapshot_dir(v)
    host.publish_snapshot(snap_dir)
    # stage one pending draft via the VM
    BrainCore(vault=v, role="vm").draft_capture(
        "---\nid: pend\ntype: note\nclassification: Internal\n---\n\npending.\n")
    st = host.status(snap_dir)
    assert st["snapshot"]["snapshot"] == "present"
    assert st["snapshot"]["generation"] >= 1
    assert "age_human" in st["snapshot"]
    assert st["pending_drafts"] == 1


# --------------------------------------------------------------------------
# INT-02: CLI role gate (the binary refuses host commands on the VM leg)
# --------------------------------------------------------------------------
def _cli_env(monkeypatch, tmp_path, vault):
    monkeypatch.setenv("BRAIN_VAULT", str(vault))
    monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))


def test_cli_vm_refuses_host_commands_without_resolving_key(tmp_path, monkeypatch, capsys):
    v = _mini_vault(tmp_path)
    _cli_env(monkeypatch, tmp_path, v)
    import brain.audit as audit_mod
    monkeypatch.setattr(audit_mod, "resolve_signing_key",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("key resolved!")))
    for host_cmd in (["--role", "vm", "write", "brain/resources/x.md", "--content", "x", "--json"],
                     ["--role", "vm", "rebuild", "--json"],
                     ["--role", "vm", "sync", "--json"],
                     ["--role", "vm", "snapshot", "--json"],
                     ["--role", "vm", "verify-audit", "--json"]):
        rc = cli.main(host_cmd)
        out = capsys.readouterr().out
        assert rc == 4, f"{host_cmd} should be refused on vm"
        assert json.loads(out)["error"] == "role_forbidden"


def test_cli_vm_allows_read_and_draft(tmp_path, monkeypatch, capsys):
    v = _mini_vault(tmp_path)
    _cli_env(monkeypatch, tmp_path, v)
    # host builds + publishes a snapshot the VM can read
    assert cli.main(["rebuild"]) == 0
    capsys.readouterr()
    assert cli.main(["snapshot"]) == 0
    capsys.readouterr()
    # VM: draft-capture (the one quasi-write)
    rc = cli.main(["--role", "vm", "draft-capture", "--id", "vmnote",
                   "--content", "---\nid: vmnote\ntype: note\nclassification: Internal\n---\n\nx.\n",
                   "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["signed"] is False
    # VM: status + recent read fine
    assert cli.main(["--role", "vm", "status", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["--role", "vm", "recent", "--json"]) == 0


# --------------------------------------------------------------------------
# INT-03: the optional MCP adapter wraps the SAME core + egress gate
# --------------------------------------------------------------------------
def test_mcp_adapter_applies_default_deny_egress(sample_vault, tmp_path):
    idx = BrainIndex(db_path=tmp_path / "i.sqlite",
                     backend=BruteForceBackend(), embedder=HashEmbedder())
    core = BrainCore(vault=sample_vault, index=idx)
    core.rebuild()
    out = mcp_adapter.dispatch("search", {"query": "arctic deal merger pricing"}, core=core)
    tiers = {h["classification"] for h in out["results"]}
    assert tiers <= {"Public", "Internal"}              # same deny-by-default as CLI
    assert out["egress"]["withheld"] >= 1
    # a Restricted note is withheld at default cap.
    assert mcp_adapter.dispatch("get", {"id": "restricted-deal"}, core=core)["result"] is None
    # SEC-01 egress-ceiling clamp: a caller CANNOT self-elevate past the
    # server-configured ceiling (default "Internal") just by asking for a
    # higher max_tier -- unlike the CLI's --max-tier (a human typed it), an
    # MCP request has no such signal, so the requested "Restricted" is
    # clamped back down to the ceiling and the note stays withheld.
    still_denied = mcp_adapter.dispatch(
        "get", {"id": "restricted-deal", "max_tier": "Restricted"}, core=core)
    assert still_denied["result"] is None
    assert still_denied["egress"]["max_tier"] == "Internal"


def test_mcp_adapter_egress_ceiling_clamps_requested_max_tier(sample_vault, tmp_path, monkeypatch):
    """SEC-01: BRAIN_MAX_EGRESS_TIER is the hard ceiling; a caller can request
    LESS than it (always honored) but never MORE (always clamped down)."""
    idx = BrainIndex(db_path=tmp_path / "i.sqlite",
                     backend=BruteForceBackend(), embedder=HashEmbedder())
    core = BrainCore(vault=sample_vault, index=idx)
    core.rebuild()

    # default ceiling (no env var set) -> "Internal"; a request for "Secret"
    # is clamped down and the Restricted note stays withheld.
    monkeypatch.delenv("BRAIN_MAX_EGRESS_TIER", raising=False)
    assert mcp_adapter._egress_ceiling_tier() == "Internal"
    assert mcp_adapter._clamp_max_tier("Secret") == "Internal"
    out = mcp_adapter.dispatch("get", {"id": "restricted-deal", "max_tier": "Secret"}, core=core)
    assert out["result"] is None

    # a NARROWER request than the ceiling is always honored unchanged.
    assert mcp_adapter._clamp_max_tier("Public") == "Public"

    # operator raises the ceiling explicitly -> the same request now surfaces.
    monkeypatch.setenv("BRAIN_MAX_EGRESS_TIER", "Restricted")
    assert mcp_adapter._clamp_max_tier("Secret") == "Restricted"
    elevated = mcp_adapter.dispatch("get", {"id": "restricted-deal", "max_tier": "Restricted"},
                                    core=core)
    assert elevated["result"]["id"] == "restricted-deal"

    # an unrecognised env value fails closed to the conservative default,
    # never fails open to "allow everything".
    monkeypatch.setenv("BRAIN_MAX_EGRESS_TIER", "not-a-real-tier")
    assert mcp_adapter._egress_ceiling_tier() == "Internal"

    # an unrecognised REQUESTED tier is passed through unchanged so the
    # existing ClassificationFilter validation still raises its normal error
    # -- the clamp never manufactures or swallows that error.
    monkeypatch.delenv("BRAIN_MAX_EGRESS_TIER", raising=False)
    with pytest.raises(ValueError):
        mcp_adapter.dispatch("get", {"id": "restricted-deal", "max_tier": "not-a-real-tier"},
                             core=core)


def test_mcp_adapter_exposes_only_read_tools(sample_vault, tmp_path):
    idx = BrainIndex(db_path=tmp_path / "i.sqlite",
                     backend=BruteForceBackend(), embedder=HashEmbedder())
    core = BrainCore(vault=sample_vault, index=idx)
    core.rebuild()
    for forbidden in ("write", "draft_capture", "sync", "snapshot", "rebuild"):
        with pytest.raises(ValueError):
            mcp_adapter.dispatch(forbidden, {"id": "x"}, core=core)


# --------------------------------------------------------------------------
# INT-01: one canonical instruction file, imported by every harness
# --------------------------------------------------------------------------
def test_claude_md_imports_agents_md():
    txt = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@AGENTS.md" in txt  # Claude Code expands the import at startup


def test_gemini_settings_points_at_agents_md():
    cfg = json.loads((REPO_ROOT / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert cfg["contextFileName"] == "AGENTS.md"


def test_agents_md_is_canonical_brain_usage():
    txt = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for token in ("brain draft-capture", "--role vm", "brain --help", "no MCP".lower()):
        assert token.lower() in txt.lower(), f"AGENTS.md missing brain-usage token: {token!r}"


def test_help_advertises_role_and_draft_capture(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for token in ("--role", "draft-capture", "status"):
        assert token in out, f"--help missing S06 token: {token!r}"
