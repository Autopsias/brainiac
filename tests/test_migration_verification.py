"""S06 (REL-03) — migration verification: an install predating both the
SCHEMA_VERSION bump and the ADR-0004 Ruling 5 packaging reconciliation
upgrades cleanly through `/brainiac-update`'s ordering.

Fidelity note (grill-9, honest downgrade of the claim): the OLD index/snapshot
fixture is built by an ACTUAL PRE-SCHEMA-3 CHECKOUT (git commit b8a3e71,
SCHEMA_VERSION=2, the direct parent of d963e18 which bumped it to 3) run in
its own subprocess against the same on-disk vault/index paths the current
code then upgrades — NOT by hand-mutating a schema-3 index's meta row down to
2. That means this test genuinely exercises "an old index built by old code"
rather than a synthetic downgrade of new code's own output.

What this test verifies:
  1. the rebuild TRIGGER fires (current code's `sync()` sees schema_version
     2 != SCHEMA_VERSION 3 and rebuilds, per `BrainIndex._schema_ready()`);
  2. snapshot republish ORDERING — the new snapshot's generation strictly
     increases and its schema_version matches the new binary, and this
     happens via `sync(publish=True)` in one call (Ruling 4: rebuild and
     republish must land together, never rebuild-then-later-publish);
  3. the forced-clean-plugin-reinstall branch (ADR-0004 Ruling 5) fires for
     the downgrade case — modeled directly against the version-compare rule
     `/brainiac-update` Step 0.5 implements (installed plugin version >
     marketplace version => force reinstall, never a silent no-op);
  4. every never-touch path (ADR-0004 Ruling 4) is byte-identical before and
     after the update path runs.

What this test does NOT claim (explicit downgrade, per grill-9): it does not
exercise column-level data migration fidelity inside the index (e.g. that
every bitemporal column back-fills a specific value) — ADR-0003 Ruling 6
states the index is derived, disposable state and rebuild-from-Markdown-truth
IS its complete migration story, so there is no column-level migration to
verify; this test's job is to prove the rebuild fires and the republish
ordering holds, which it does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OLD_SCHEMA2_REV = "b8a3e71"  # parent of d963e18 (the SCHEMA_VERSION 2->3 bump)


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd or REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def old_schema_checkout(tmp_path) -> Path:
    """A real git worktree pinned at the pre-schema-3 commit (visible,
    self-cleaning — no `git stash`, per repo git-safety rules)."""
    wt = tmp_path / "old-checkout"
    _git("worktree", "add", "--detach", str(wt), OLD_SCHEMA2_REV)
    try:
        yield wt
    finally:
        # Cleanup runs even on assertion failure — no orphaned worktree.
        subprocess.run(["git", "worktree", "remove", str(wt), "--force"],
                        cwd=REPO_ROOT, check=False, capture_output=True)


def _mini_vault(root: Path) -> Path:
    v = root / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "raw").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: I\ntype: index\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMap.\n", encoding="utf-8")
    (v / "brain" / "resources" / "seed.md").write_text(
        "---\nid: seed\ntitle: Seed\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nseed note, pre-migration.\n",
        encoding="utf-8")
    return v


def _run_cli(src_dir: Path, vault: Path, index_dir: Path, argv: list[str], *, audit_key_pem: str | None = None) -> tuple[int, dict]:
    """Run `brain <argv>` from a given src/ tree in an ISOLATED subprocess
    (never in-process — the old and new checkouts both define a `brain`
    top-level package and must never collide in one interpreter's sys.modules)."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "BRAIN_VAULT": str(vault),
        "BRAIN_INDEX_DIR": str(index_dir),
        "PYTHONPATH": str(src_dir),
    }
    if audit_key_pem:
        env["BRAIN_AUDIT_KEY_PEM"] = audit_key_pem
    code = (
        "import sys; sys.path.insert(0, r'" + str(src_dir) + "');"
        "from brain import cli;"
        "rc = cli.main(" + repr(argv) + ");"
        "sys.exit(rc)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True,
    )
    # CLI JSON output is pretty-printed (multi-line); warnings print to stdout
    # ABOVE it (see cli.py's degraded-embedder notice). Take everything from
    # the first '{' onward — the report is always the last thing printed.
    stdout = proc.stdout
    brace = stdout.find("{")
    payload = {}
    if brace != -1:
        try:
            payload = json.loads(stdout[brace:])
        except json.JSONDecodeError:
            payload = {"_raw_stdout": stdout, "_stderr": proc.stderr}
    return proc.returncode, payload


def test_old_schema_index_upgrades_and_republishes_on_current_code(old_schema_checkout, tmp_path, audit_key_env):
    """End-to-end: schema-2 index + gen-1 snapshot (built by REAL old code) ->
    current code's `sync(publish=True)` rebuilds + republishes in one call."""
    vault = _mini_vault(tmp_path)
    index_dir = tmp_path / "idx"

    # 1. Build the OLD state with the OLD checkout's own code.
    rc, rebuild_res = _run_cli(old_schema_checkout / "src", vault, index_dir, ["rebuild", "--json"])
    assert rc == 0, rebuild_res
    assert rebuild_res["indexed"] == 2

    rc, old_snapshot = _run_cli(old_schema_checkout / "src", vault, index_dir, ["snapshot", "--json"])
    assert rc == 0, old_snapshot
    assert old_snapshot["schema_version"] == "2"
    old_generation = old_snapshot["generation"]
    assert old_generation == 1

    # Sanity: the on-disk index really is schema 2, per the OLD code's own report.
    rc, old_status = _run_cli(old_schema_checkout / "src", vault, index_dir, ["status", "--json"])
    assert rc == 0, old_status
    assert old_status["index"]["schema_version"] == "2"

    # Snapshot the vault's Markdown bytes (the never-touch surface) before
    # running any update-path code, for the byte-identical assertion below.
    before_bytes = {
        p.relative_to(vault): p.read_bytes()
        for p in vault.rglob("*.md")
        if ".brain" not in p.relative_to(vault).parts
    }

    # HARDEN:blindspot — extend never-touch coverage beyond .md byte-identity.
    # Pre-populate the ADR-0004 Ruling 4 state surfaces an update/sync must
    # never mutate: `.brain/memory/` (handoff/hot/lessons + recommendations),
    # `.brain/maintain-state.json`, `.brain/maintain.lock`, and the audit
    # chain (its WAL is the index's SQLite WAL, which lives beside the audit
    # chain under the same index_dir — checked as a set-of-files snapshot so
    # a WAL present before but silently dropped/mutated after would also be
    # caught). Silent corruption here breaks semantic-search history + the
    # nightly loop with NO error surfaced anywhere else.
    from brain import config as _config

    memory_dir = _config.memory_dir(vault)
    maintain_state = _config.maintain_state_path(vault)
    maintain_lock = _config.maintain_lock_path(vault)
    audit_log = index_dir / "audit_chain.jsonl"  # BRAIN_INDEX_DIR override -> index_dir directly (see config.default_audit_log)

    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "handoff.md").write_text("pre-existing handoff content\n", encoding="utf-8")
    (memory_dir / "recommendations-open.jsonl").write_text('{"id": "r1", "status": "open"}\n', encoding="utf-8")
    maintain_state.parent.mkdir(parents=True, exist_ok=True)
    maintain_state.write_text(json.dumps({"branches": {"daily": {"last_run": "2026-07-01"}}}), encoding="utf-8")
    maintain_lock.write_text('{"pid": 12345, "started": "2026-07-01T00:00:00Z"}\n', encoding="utf-8")
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.write_text('{"seq": 1, "hash": "deadbeef"}\n', encoding="utf-8")

    never_touch_before = {
        "memory/handoff.md": (memory_dir / "handoff.md").read_bytes(),
        "memory/recommendations-open.jsonl": (memory_dir / "recommendations-open.jsonl").read_bytes(),
        "maintain-state.json": maintain_state.read_bytes(),
        "maintain.lock": maintain_lock.read_bytes(),
        "audit_chain.jsonl": audit_log.read_bytes(),
    }

    # 2. Run the CURRENT code's sync(publish=True) — this is the exact
    # operation Step 3.5 of /brainiac-update's ordering performs.
    rc, sync_res = _run_cli(REPO_ROOT / "src", vault, index_dir,
                             ["sync", "--publish", "--json"],
                             audit_key_pem=audit_key_env.decode("utf-8"))
    assert rc == 0, sync_res

    # --- Assertion 1: rebuild TRIGGER fired -----------------------------
    assert sync_res.get("mode") == "rebuild(no-schema)", (
        f"expected the schema mismatch to force a rebuild, got mode={sync_res.get('mode')!r}"
    )

    # --- Assertion 2: snapshot republish ORDERING -----------------------
    new_snapshot = sync_res.get("snapshot")
    assert new_snapshot, "sync(publish=True) must report the republished snapshot in the SAME call"
    assert new_snapshot["generation"] > old_generation, (
        "republished snapshot generation must strictly increase — a stale "
        "generation number would mean the VM can't tell fresh from stale"
    )
    from brain.index import SCHEMA_VERSION as CURRENT_SCHEMA_VERSION
    assert new_snapshot["schema_version"] == str(CURRENT_SCHEMA_VERSION)

    # Never a silently-wrong intermediate state: confirm CURRENT code's own
    # `status` now reports the index AND snapshot as both current, no skew.
    rc, new_status = _run_cli(REPO_ROOT / "src", vault, index_dir, ["status", "--json"])
    assert rc == 0, new_status
    assert new_status["index"]["schema_version"] == str(CURRENT_SCHEMA_VERSION)
    assert new_status["version"]["index_newer_than_binary"] is False
    assert new_status["version"]["snapshot_newer_than_binary"] is False

    # --- Assertion 3: never-touch paths byte-identical before/after ------
    after_bytes = {
        p.relative_to(vault): p.read_bytes()
        for p in vault.rglob("*.md")
        if ".brain" not in p.relative_to(vault).parts
    }
    assert before_bytes == after_bytes, "vault Markdown (never-touch surface) must be byte-identical across an update"

    # --- Assertion 4 (HARDEN:blindspot): extended never-touch surfaces ---
    # memory/, maintain-state.json, maintain.lock, and the audit chain must
    # survive `sync(publish=True)` byte-identical — a rebuild+republish must
    # never reach outside the index/snapshot it owns.
    never_touch_after = {
        "memory/handoff.md": (memory_dir / "handoff.md").read_bytes(),
        "memory/recommendations-open.jsonl": (memory_dir / "recommendations-open.jsonl").read_bytes(),
        "maintain-state.json": maintain_state.read_bytes(),
        "maintain.lock": maintain_lock.read_bytes(),
        "audit_chain.jsonl": audit_log.read_bytes(),
    }
    assert never_touch_before == never_touch_after, (
        "extended never-touch surfaces (.brain/memory/, maintain-state.json, "
        "maintain.lock, audit chain) must be byte-identical across an update — "
        "silent corruption here breaks semantic-search history + the nightly "
        "loop with no error surfaced anywhere else"
    )


def test_forced_clean_reinstall_branch_fires_for_reconciliation_downgrade():
    """ADR-0004 Ruling 5: the reconciliation moves plugin versions BACKWARDS
    (1.1.0 -> 0.9.x). A marketplace in-place update refuses that as a
    downgrade, so /brainiac-update's Step 0.5 must detect
    installed-version > marketplace-version and force the clean
    uninstall/reinstall branch rather than silently reporting nothing to do.

    This models the exact comparison Step 0.5 of the SKILL.md performs
    (semver tuple compare) against the real pre/post-reconciliation numbers,
    since the skill itself is prose driven by a live Claude Code session, not
    an importable function — the comparison RULE is what's under test."""

    def _semver(v: str) -> tuple[int, int, int]:
        major, minor, patch = (int(x) for x in v.split("."))
        return (major, minor, patch)

    def packaging_downgrade_detected(installed: str, marketplace: str) -> bool:
        """Mirrors SKILL.md Step 0.5's rule: installed > marketplace => the
        one-time reconciliation downgrade => force clean reinstall."""
        return _semver(installed) > _semver(marketplace)

    # The real pre-reconciliation numbers (this session's own before-state).
    assert packaging_downgrade_detected("1.1.0", "0.9.0") is True   # kernel/extras
    assert packaging_downgrade_detected("1.0.0", "0.9.0") is True   # brainiac-manager
    # Normal case: no reconciliation in play, ordinary forward update.
    assert packaging_downgrade_detected("0.9.0", "0.9.0") is False  # in sync
    assert packaging_downgrade_detected("0.9.0", "0.9.1") is False  # ordinary forward update
    # Post-reconciliation: every future release stays on one line, never fires again.
    assert packaging_downgrade_detected("0.9.1", "0.10.0") is False


def test_package_clients_validate_only_fails_on_plugin_version_skew(tmp_path):
    """Confirms tools/package_clients.py --validate-only is the hard gate this
    migration path relies on: a plugin.json that still carries a
    pre-reconciliation version must fail, not warn."""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import importlib

    pc = importlib.import_module("package_clients")
    importlib.reload(pc)  # ensure REPO_ROOT-relative paths bind to the real repo

    ssot_version = pc.read_source_version()
    with pytest.raises(pc.ValidationError):
        pc.validate_plugin_version_lockstep("9.9.9")  # any version != real plugin.json files
    # The real on-disk state (post s06 reconciliation) must currently PASS.
    pc.validate_plugin_version_lockstep(ssot_version)
