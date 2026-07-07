"""UP-01/UP-02 (ADR-0005 Ruling 3) — `brain update` version-compare /
downgrade-decision logic, the preflight capability probe, and the
uninstall-then-install rollback/half-applied path. All-fixture / fake-runner:
no test here ever calls a real subprocess or mutates a real plugin store,
venv, or workspace.
"""
from __future__ import annotations

import platform
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from brain import update

REPO_ROOT = Path(update.__file__).resolve().parents[2]


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["fake"], returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------
# decide_plugin_action — pure version-compare/downgrade-decision logic
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "installed,marketplace,expected",
    [
        (None, "1.0.0", "install"),
        ("1.0.0", None, "skip"),
        ("0.9.1", "0.10.0", "update"),         # normal forward update, never string-compare
        ("0.10.0", "0.9.1", "reinstall"),      # 0.10.0 > 0.9.1 numerically (not stringwise)
        ("1.1.0", "0.9.1", "reinstall"),       # the ADR-0004 Ruling 5 reconciliation downgrade
        ("0.9.1", "0.9.1", "skip"),            # already current
        ("1.2.3-pre.1", "1.2.3", "update"),    # pre-release sorts below the release
    ],
)
def test_decide_plugin_action(installed, marketplace, expected):
    assert update.decide_plugin_action(installed, marketplace) == expected


def test_decide_plugin_action_invalid_manifest_never_crashes():
    # An invalid manifest string must not raise; `_compare`'s mixed-scheme
    # fallback (doctor.py) resolves it deterministically (falls back to a
    # string compare) rather than crashing the whole update run.
    action = update.decide_plugin_action("not-a-version", "0.9.1")
    assert action in {"install", "update", "reinstall", "skip"}
    action2 = update.decide_plugin_action("0.9.1", "garbage-manifest-value")
    assert action2 in {"install", "update", "reinstall", "skip"}


# --------------------------------------------------------------------------
# Preflight capability probe (HARDEN:consensus-HIGH)
# --------------------------------------------------------------------------

def test_capability_probe_blocks_when_claude_missing(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    report = update.probe_cli_capability(run=lambda cmd, **kw: _cp())
    assert report["ok"] is False
    assert "not found" in report["reason"]
    assert report["manual_commands"]


def test_capability_probe_blocks_when_subcommand_missing(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/claude")
    # --help text missing "uninstall" -- surface mismatch, must block.
    fake_help = "Usage: claude plugin [marketplace|list|install]"
    report = update.probe_cli_capability(run=lambda cmd, **kw: _cp(stdout=fake_help))
    assert report["ok"] is False
    assert "uninstall" in report["reason"]
    assert report["manual_commands"]


def test_capability_probe_ok_when_all_subcommands_present(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/claude")
    fake_help = "Usage: claude plugin [marketplace|list|install|uninstall|update]"
    report = update.probe_cli_capability(run=lambda cmd, **kw: _cp(stdout=fake_help))
    assert report["ok"] is True
    assert report["manual_commands"] == []


def test_capability_probe_blocks_when_update_subcommand_missing(monkeypatch):
    # A `claude` build without `plugin update` must block rather than let the
    # upgrade path silently fall back to the no-op `plugin install`.
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/claude")
    fake_help = "Usage: claude plugin [marketplace|list|install|uninstall]"
    report = update.probe_cli_capability(run=lambda cmd, **kw: _cp(stdout=fake_help))
    assert report["ok"] is False
    assert "update" in report["reason"]
    assert any("plugin update" in c for c in report["manual_commands"])


def test_run_update_stops_before_any_mutation_when_probe_fails(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    report = update.run_update(run=fake_run)
    assert report["ok"] is False
    assert calls == [], "no subprocess call may happen once the capability probe blocks"
    assert "BLOCKED" in report["notes"]


# --------------------------------------------------------------------------
# apply_plugin_action — full decision-matrix command dispatch
# --------------------------------------------------------------------------

def test_apply_plugin_action_install_runs_claude_plugin_install():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    result = update.apply_plugin_action("install", "brainiac-manager", "profile-a-marketplace", run=fake_run)
    assert result["ok"] is True
    assert calls == [[update.shutil.which("claude") or "claude", "plugin", "install",
                       "brainiac-manager@profile-a-marketplace"]]


def test_apply_plugin_action_update_runs_claude_plugin_update_not_install():
    # The bug this session fixes: `claude plugin install` on an already
    # installed plugin no-ops instead of upgrading. The "update" action MUST
    # dispatch `claude plugin update`, never `install`.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    result = update.apply_plugin_action("update", "brainiac-manager", "profile-a-marketplace", run=fake_run)
    assert result["ok"] is True
    assert calls[0][2] == "update"
    assert calls[0] != [update.shutil.which("claude") or "claude", "plugin", "install",
                         "brainiac-manager@profile-a-marketplace"]


def test_apply_plugin_action_skip_runs_no_subprocess():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    result = update.apply_plugin_action("skip", "brainiac-manager", "profile-a-marketplace", run=fake_run)
    assert result == {"action": "skip", "ok": True, "detail": "already current"}
    assert calls == []


# --------------------------------------------------------------------------
# apply_plugin_action — rollback / half-applied recovery (HARDEN:claude-MEDIUM)
# --------------------------------------------------------------------------

def test_reinstall_success_uninstalls_then_installs_in_order():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    result = update.apply_plugin_action("reinstall", "brainiac-manager", "profile-a-marketplace", run=fake_run)
    assert result["ok"] is True
    assert calls[0][2] == "uninstall"
    assert calls[1][2] == "install"


def test_reinstall_half_applied_when_install_fails_after_uninstall_succeeds():
    # The worst case named in the hardening: uninstall lands, install fails ->
    # plugin now ABSENT, strictly worse than the downgraded start. Must be
    # reported explicitly with an exact recovery command, never silently ok.
    def fake_run(cmd, **kwargs):
        if cmd[2] == "uninstall":
            return _cp(returncode=0)
        return _cp(returncode=1, stderr="network error")

    result = update.apply_plugin_action("reinstall", "brainiac-manager", "profile-a-marketplace", run=fake_run)
    assert result["ok"] is False
    assert result["half_applied"] is True
    assert "claude plugin install brainiac-manager@profile-a-marketplace" == result["recovery_command"]


def test_reinstall_uninstall_failure_is_not_half_applied():
    # Uninstall itself fails: plugin presumably untouched -- not worse than
    # before, so this must NOT be flagged half_applied.
    def fake_run(cmd, **kwargs):
        return _cp(returncode=1, stderr="permission denied")

    result = update.apply_plugin_action("reinstall", "brainiac-manager", "profile-a-marketplace", run=fake_run)
    assert result["ok"] is False
    assert result.get("half_applied") is None
    assert result["stage"] == "uninstall"


def test_run_update_reports_incomplete_on_half_applied_reinstall(monkeypatch):
    monkeypatch.setattr(update, "probe_cli_capability", lambda run: {"ok": True, "reason": "ok", "manual_commands": []})
    monkeypatch.setattr(update, "refresh_marketplace", lambda name, run: {"ok": True, "detail": "refreshed"})

    fake_before = {
        "rows": [
            {"surface": "Installed CLI plugin (brainiac-manager)", "status": "stale",
             "detail": "installed 1.1.0 > marketplace 0.9.1", "raw": {"installed": "1.1.0", "marketplace": "0.9.1"}},
        ],
        "ok": False, "stale_count": 1, "ssot_version": "0.9.1",
    }
    monkeypatch.setattr(update, "run_doctor", lambda **kw: fake_before)
    monkeypatch.setattr(update, "render_human", lambda report: "doctor rendered")

    def fake_apply(action, pname, marketplace_name, run):
        return {"action": "reinstall", "ok": False, "stage": "install", "detail": "boom",
                "half_applied": True, "recovery_command": f"claude plugin install {pname}@{marketplace_name}"}

    monkeypatch.setattr(update, "apply_plugin_action", fake_apply)

    report = update.run_update(run=lambda cmd, **kw: _cp())
    assert report["ok"] is False
    assert "INCOMPLETE" in report["notes"]
    assert "recovery" in report["notes"].lower() or "claude plugin install" in report["notes"]


def test_run_update_reports_failed_upgrade_when_update_action_no_ops(monkeypatch):
    # The core bug this session fixes: `claude plugin update` (or a pre-fix
    # `install`) can return returncode 0 while leaving the plugin at its old
    # version. `run_update` must re-read the installed version and report
    # this as a FAILED upgrade, never ok:true.
    monkeypatch.setattr(update, "probe_cli_capability", lambda run: {"ok": True, "reason": "ok", "manual_commands": []})
    monkeypatch.setattr(update, "refresh_marketplace", lambda name, run: {"ok": True, "detail": "refreshed"})

    fake_before = {
        "rows": [
            {"surface": "Installed CLI plugin (brainiac-manager)", "status": "stale",
             "detail": "installed 0.9.1 < marketplace 0.10.0",
             "raw": {"installed": "0.9.1", "marketplace": "0.10.0"}},
        ],
        "ok": False, "stale_count": 1, "ssot_version": "0.10.0",
    }
    monkeypatch.setattr(update, "run_doctor", lambda **kw: fake_before)
    monkeypatch.setattr(update, "render_human", lambda report: "doctor rendered")

    # apply_plugin_action reports ok=True (returncode 0 / "already installed")
    # even though the version never moved -- exactly the CLI no-op.
    monkeypatch.setattr(
        update, "apply_plugin_action",
        lambda action, pname, marketplace_name, run: {
            "action": "update", "ok": True,
            "detail": '✔ Plugin "brainiac-manager" is already installed (scope: user)',
        },
    )
    # Re-read after the action still shows the OLD version -- the no-op.
    monkeypatch.setattr(
        update, "check_installed_cli_plugins",
        lambda claude_home, ssot, marketplace_name: [
            {"surface": "Installed CLI plugin (brainiac-manager)",
             "raw": {"installed": "0.9.1", "marketplace": "0.10.0"}},
        ],
    )

    report = update.run_update(run=lambda cmd, **kw: _cp())
    assert report["ok"] is False
    assert "INCOMPLETE" in report["notes"]
    assert "no-op" in report["notes"].lower()
    plugin_step = report["steps"]["plugin_reinstall"][0]
    assert plugin_step["ok"] is False, "a no-op'd update must never report ok:true"
    assert "0.9.1" in plugin_step["detail"]


def test_run_update_upgrade_succeeds_when_version_actually_moves(monkeypatch):
    # Control case: the update action genuinely moved the version -- must NOT
    # be flagged as a no-op / failed upgrade.
    monkeypatch.setattr(update, "probe_cli_capability", lambda run: {"ok": True, "reason": "ok", "manual_commands": []})
    monkeypatch.setattr(update, "refresh_marketplace", lambda name, run: {"ok": True, "detail": "refreshed"})

    fake_before = {
        "rows": [
            {"surface": "Installed CLI plugin (brainiac-manager)", "status": "stale",
             "detail": "installed 0.9.1 < marketplace 0.10.0",
             "raw": {"installed": "0.9.1", "marketplace": "0.10.0"}},
        ],
        "ok": True, "stale_count": 0, "ssot_version": "0.10.0",
    }
    monkeypatch.setattr(update, "run_doctor", lambda **kw: fake_before)
    monkeypatch.setattr(update, "render_human", lambda report: "doctor rendered")
    monkeypatch.setattr(
        update, "apply_plugin_action",
        lambda action, pname, marketplace_name, run: {
            "action": "update", "ok": True,
            "detail": 'updated from 0.9.1 to 0.10.0 for scope user',
        },
    )
    monkeypatch.setattr(
        update, "check_installed_cli_plugins",
        lambda claude_home, ssot, marketplace_name: [
            {"surface": "Installed CLI plugin (brainiac-manager)",
             "raw": {"installed": "0.10.0", "marketplace": "0.10.0"}},
        ],
    )
    monkeypatch.setattr(update, "refresh_engine_venv", lambda *a, **kw: {"ok": True, "old_version": "x", "new_version": "y", "detail": "ok"})
    monkeypatch.setattr(update, "restage_workspaces", lambda *a, **kw: [])

    report = update.run_update(run=lambda cmd, **kw: _cp())
    plugin_step = report["steps"]["plugin_reinstall"][0]
    assert plugin_step["ok"] is True
    assert report["notes"] != "update INCOMPLETE — plugin update no-op'd"


# --------------------------------------------------------------------------
# resolve_engine_source (HARDEN:codex-MEDIUM — never hardcode ~/brainiac)
# --------------------------------------------------------------------------

def test_resolve_engine_source_prefers_explicit_override(tmp_path):
    assert update.resolve_engine_source(explicit=str(tmp_path)) == tmp_path.resolve()


def test_resolve_engine_source_prefers_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINIAC_ENGINE_SRC", str(tmp_path))
    assert update.resolve_engine_source() == tmp_path.resolve()


def test_resolve_engine_source_falls_back_to_repo_root_when_pyproject_present(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nversion = \"1.0.0\"\n")
    assert update.resolve_engine_source(repo_root=tmp_path) == tmp_path.resolve()


# --------------------------------------------------------------------------
# restage_workspaces — cowork-vm target (real bug found on the host after
# publishing v0.10.1: the re-stage only ran an index sync, never re-copied
# the engine source or refreshed the .skill bundles, so a Cowork VM stayed
# on a stale pre-0.10.0 engine with no committed _version.py stamp). These
# tests exercise the REAL restage_workspaces / stage_engine_and_skills
# against tmp_path workspace fixtures — never a real Cowork session — by
# monkeypatching tools/workspace_registry.py's list_entries so no real
# ~/.brainiac/workspaces.json is touched.
# --------------------------------------------------------------------------

sys.path.insert(0, str(REPO_ROOT / "tools"))
import workspace_registry as _wr  # noqa: E402  (path must be set first)


def _cowork_vm_entry(vault_path: Path, workspace_path: Path, **overrides) -> dict:
    entry = {
        "vault_path": str(vault_path),
        "workspace_path": str(workspace_path),
        "target": "cowork-vm",
        "host": socket.gethostname(),
        "arch": platform.machine(),
        "model_dir": "",
    }
    entry.update(overrides)
    return entry


def test_restage_workspaces_cowork_vm_stages_engine_at_vault_path_not_workspace_path(tmp_path, monkeypatch):
    # BUG B fixture: vault_path is a CHILD of workspace_path (the real shape —
    # e.g. workspace_path=".../example-vault", vault_path=".../example-vault/vault"),
    # matching cowork_workspace_install.sh's $VAULT/.brain contract. A re-stage
    # that only touched the parent workspace_path must never satisfy this test.
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setattr(_wr, "list_entries", lambda *a, **kw: [_cowork_vm_entry(vault, workspace)])
    monkeypatch.setattr(_wr, "touch_refreshed", lambda **kw: {"ok": True})

    results = update.restage_workspaces(
        REPO_ROOT, tmp_path, run=lambda cmd, **kw: _cp(stdout="sync [incremental]: +0 ~0 -0 =4")
    )

    assert len(results) == 1
    assert results[0]["status"] == "ok", results[0]

    ssot = update._ssot_version(REPO_ROOT)

    vault_stamp = vault / ".brain" / "engine" / "brain" / "_version.py"
    assert vault_stamp.exists(), "engine source must be re-staged into vault_path/.brain/engine/brain/"
    assert update._read_version_stamp(vault_stamp) == ssot

    # The parent workspace_path's OWN .brain (the unrelated host stage) must
    # not be what the version-ok assertion read — a wrong-path stage that
    # only touched workspace_path would leave this missing.
    assert not (workspace / ".brain" / "engine" / "brain" / "_version.py").exists(), (
        "re-stage must not touch workspace_path's .brain — only vault_path's"
    )

    skills_dir = vault / ".brain" / "skills"
    assert list(skills_dir.glob("*.skill")), ".skill bundles must be refreshed into vault_path/.brain/skills/"


def test_restage_workspaces_cowork_vm_fails_on_stamp_mismatch(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setattr(_wr, "list_entries", lambda *a, **kw: [_cowork_vm_entry(vault, workspace)])
    # Force a post-restage stamp mismatch (engine copied for real, but the
    # SSOT read comes back different) -- must be reported FAILED, not ok:true.
    monkeypatch.setattr(update, "_ssot_version", lambda repo_root: "999.0.0")

    results = update.restage_workspaces(REPO_ROOT, tmp_path, run=lambda cmd, **kw: _cp())

    assert results[0]["status"] == "failed"
    assert "999.0.0" in results[0]["reason"]
    # The engine copy itself still happened (no silent no-op) -- just the
    # verify step must catch and report the mismatch.
    assert (vault / ".brain" / "engine" / "brain" / "_version.py").exists()


# --------------------------------------------------------------------------
# restage_workspaces — BUG A: hostname-vs-arch skip gate (pre-existing since
# v0.10.0). macOS `socket.gethostname()` is unstable (flips between the mDNS
# `.local` name and the DHCP name for the SAME machine), so gating the skip
# decision on hostname silently skipped the user's own workspace and the
# re-stage never ran. Fix: gate on arch only; self-heal a stale host field.
# --------------------------------------------------------------------------

def test_restage_workspaces_mismatched_hostname_same_arch_is_not_skipped(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    vault.mkdir(parents=True)
    entry = _cowork_vm_entry(vault, workspace, host="oldhost.local")
    monkeypatch.setattr(_wr, "list_entries", lambda *a, **kw: [entry])
    monkeypatch.setattr(_wr, "touch_refreshed", lambda **kw: {"ok": True})
    healed = []
    monkeypatch.setattr(_wr, "upsert_entry", lambda **kw: healed.append(kw))

    results = update.restage_workspaces(
        REPO_ROOT, tmp_path, run=lambda cmd, **kw: _cp(stdout="sync [incremental]: +0 ~0 -0 =4")
    )

    assert results[0]["status"] == "ok", results[0]
    assert results[0]["reason"] != "different host/arch"
    assert healed, "a same-arch hostname mismatch must self-heal the registry entry, not just proceed silently"


def test_restage_workspaces_different_arch_is_still_skipped(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    vault.mkdir(parents=True)
    entry = _cowork_vm_entry(vault, workspace, arch="x86_64_STALE_OTHER_MACHINE")
    monkeypatch.setattr(_wr, "list_entries", lambda *a, **kw: [entry])

    results = update.restage_workspaces(REPO_ROOT, tmp_path, run=lambda cmd, **kw: _cp())

    assert results[0]["status"] == "skipped"
    assert "arch" in results[0]["reason"]
    assert not (vault / ".brain").exists(), "a genuinely different-arch entry must never be staged"


def test_restage_workspaces_host_target_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(_wr, "list_entries", lambda *a, **kw: [{
        "vault_path": str(tmp_path), "workspace_path": str(tmp_path), "target": "host",
        "host": socket.gethostname(), "arch": platform.machine(),
    }])

    results = update.restage_workspaces(REPO_ROOT, tmp_path, run=lambda cmd, **kw: _cp())

    assert results == [{
        "workspace_path": str(tmp_path), "target": "host",
        "status": "ok", "reason": "engine refresh covers host leg",
    }]


# --------------------------------------------------------------------------
# Dry-run orchestration never calls the injected runner for mutating steps
# --------------------------------------------------------------------------

def test_dry_run_never_invokes_runner():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    report = update.run_update(run=fake_run, dry_run=True, skip_capability_probe=True)
    assert calls == []
    assert report["ok"] in (True, False)  # doesn't crash either way
    assert "residual_human_steps" in report
    assert any("Desktop" in s for s in report["residual_human_steps"])


# --------------------------------------------------------------------------
# dist/ rebuild — `pip install -e` (engine venv refresh) never regenerates
# dist/COMPAT + dist/cowork-skills/*.skill; only tools/package_clients.py
# does. Observed live twice (0.10.2->0.10.3, 0.10.3->0.10.4): skipping this
# left restage_workspaces staging one-build-stale .skill bundles.
# --------------------------------------------------------------------------

def test_dist_rebuild_runs_after_engine_refresh_and_before_workspace_restage(monkeypatch):
    call_order = []
    monkeypatch.setattr(update, "probe_cli_capability", lambda run: {"ok": True, "reason": "ok", "manual_commands": []})
    monkeypatch.setattr(update, "refresh_marketplace", lambda name, run: {"ok": True, "detail": "refreshed"})
    monkeypatch.setattr(update, "run_doctor", lambda **kw: {"rows": [], "ok": True, "stale_count": 0, "ssot_version": "0.9.1"})
    monkeypatch.setattr(update, "render_human", lambda report: "rendered")

    def tracking_engine_refresh(*a, **kw):
        call_order.append("engine_refresh")
        return {"ok": True, "old_version": "x", "new_version": "y", "detail": "ok"}

    def tracking_rebuild_dist(*a, **kw):
        call_order.append("dist_rebuild")
        return {"ok": True, "detail": "built"}

    def tracking_restage(*a, **kw):
        call_order.append("workspace_restage")
        return []

    monkeypatch.setattr(update, "refresh_engine_venv", tracking_engine_refresh)
    monkeypatch.setattr(update, "rebuild_dist", tracking_rebuild_dist)
    monkeypatch.setattr(update, "restage_workspaces", tracking_restage)

    update.run_update(run=lambda cmd, **kw: _cp())

    assert call_order == ["engine_refresh", "dist_rebuild", "workspace_restage"], call_order


def test_dist_rebuild_failure_halts_before_workspace_restage(monkeypatch):
    monkeypatch.setattr(update, "probe_cli_capability", lambda run: {"ok": True, "reason": "ok", "manual_commands": []})
    monkeypatch.setattr(update, "refresh_marketplace", lambda name, run: {"ok": True, "detail": "refreshed"})
    monkeypatch.setattr(update, "run_doctor", lambda **kw: {"rows": [], "ok": True, "stale_count": 0, "ssot_version": "0.9.1"})
    monkeypatch.setattr(update, "render_human", lambda report: "rendered")
    monkeypatch.setattr(update, "refresh_engine_venv", lambda *a, **kw: {"ok": True, "old_version": "x", "new_version": "y", "detail": "ok"})
    monkeypatch.setattr(update, "rebuild_dist", lambda *a, **kw: {"ok": False, "detail": "FAIL: plugin.json version skew"})

    restage_calls = []
    monkeypatch.setattr(update, "restage_workspaces", lambda *a, **kw: restage_calls.append(1))

    report = update.run_update(run=lambda cmd, **kw: _cp())

    assert report["ok"] is False
    assert restage_calls == [], "restage_workspaces must not run when dist rebuild fails"
    assert "dist rebuild failed" in report["notes"]
    assert report["steps"]["dist_rebuild"]["ok"] is False


def test_dist_rebuild_dry_run_records_without_executing():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp()

    report = update.run_update(run=fake_run, dry_run=True, skip_capability_probe=True)

    assert calls == [], "dry-run must never invoke the injected runner"
    assert report["steps"]["dist_rebuild"]["ok"] is True
    assert "dry-run" in report["steps"]["dist_rebuild"]["detail"].lower()


def test_render_before_after_produces_arrow_table():
    table = [{"surface": "X", "before": "1.0.0", "after": "1.1.0"}]
    text = update.render_before_after(table)
    assert "1.0.0" in text and "1.1.0" in text and "->" in text


# --------------------------------------------------------------------------
# GV-02 — marketplace refresh MUST run before the doctor snapshot that feeds
# the plugin-comparison decision. `brain doctor`'s "Installed CLI plugin" rows
# read the marketplace's on-disk checkout — the exact cache `refresh_marketplace`
# updates — so calling doctor first would silently compare against pre-refresh
# data (the stale-cache no-op this whole ordering exists to make impossible).
# --------------------------------------------------------------------------

def test_marketplace_refresh_runs_before_the_doctor_snapshot_used_for_comparison(monkeypatch):
    call_order = []

    real_refresh = update.refresh_marketplace

    def tracking_refresh(marketplace_name, run):
        call_order.append("marketplace_refresh")
        return real_refresh(marketplace_name, run=run)

    def tracking_doctor(**kw):
        call_order.append("doctor")
        return {"rows": [], "ok": True, "stale_count": 0, "ssot_version": "0.9.1"}

    monkeypatch.setattr(update, "probe_cli_capability", lambda run: {"ok": True, "reason": "ok", "manual_commands": []})
    monkeypatch.setattr(update, "refresh_marketplace", tracking_refresh)
    monkeypatch.setattr(update, "run_doctor", tracking_doctor)
    monkeypatch.setattr(update, "render_human", lambda report: "rendered")
    monkeypatch.setattr(update, "restage_workspaces", lambda *a, **kw: [])
    monkeypatch.setattr(update, "refresh_engine_venv", lambda *a, **kw: {"ok": True, "old_version": "x", "new_version": "y", "detail": "ok"})

    update.run_update(run=lambda cmd, **kw: _cp(), marketplace_name="profile-a-marketplace")

    assert call_order[0] == "marketplace_refresh", (
        f"expected marketplace refresh to run before any doctor snapshot, got order={call_order}"
    )
    assert "doctor" in call_order, "doctor must still run (for the before-snapshot + final verify)"


def test_run_update_source_never_computes_before_doctor_ahead_of_marketplace_refresh():
    """Static ordering guard: the `before_doctor = run_doctor(...)` call site
    in run_update's source must appear AFTER the `refresh_marketplace(...)`
    call site, not before — regression against exactly the bug this session
    fixed (before_doctor was computed pre-refresh, so the plugin-reinstall
    decision compared against a stale marketplace cache)."""
    import inspect

    src = inspect.getsource(update.run_update)
    refresh_idx = src.index("refresh_marketplace(marketplace_name, run=run)")
    before_doctor_idx = src.index("before_doctor = run_doctor(")
    assert refresh_idx < before_doctor_idx, (
        "before_doctor must be computed after refresh_marketplace() runs, "
        "otherwise the plugin-reinstall decision uses a pre-refresh marketplace snapshot"
    )
