"""DV-02 (ADR-0005 Ruling 2) — `brain doctor` surface classification + table
rendering. All-fixture: no probe here ever touches the live machine (host
venv, real ~/.claude, real Cowork session dirs) — every surface is exercised
against a constructed temp tree so the test is deterministic on any box.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from brain import doctor


def _write_pyproject(root: Path, version: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'version = "{version}"\n', encoding="utf-8")


def _write_stamp(root: Path, version: str) -> None:
    d = root / "src" / "brain"
    d.mkdir(parents=True, exist_ok=True)
    (d / "_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _write_plugin_manifests(root: Path, version: str, names=doctor.PLUGIN_NAMES) -> None:
    for pname in names:
        d = root / "plugins" / pname / ".claude-plugin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "plugin.json").write_text(
            json.dumps({"name": pname, "version": version}), encoding="utf-8"
        )


# --------------------------------------------------------------------------
# Version compare helper (mixed-scheme tag set per HARDEN:blindspot)
# --------------------------------------------------------------------------

def test_version_key_handles_mixed_scheme_tags_without_crashing():
    # A non-semver string (legacy opaque tag shape) must not raise; it should
    # fall back gracefully rather than crash the whole doctor run.
    assert doctor._compare("0.9.1", "0.10.0") == -1
    assert doctor._compare("1.2.0", "1.2.0") == 0
    assert doctor._compare("legacy-export-v1", "0.9.1") in (-1, 0, 1)  # never raises


def test_raw_version_downgrade_is_reported_not_asserted_as_regression():
    # 1.x -> 0.9.x is a RECONCILIATION downgrade (ADR-0004 Ruling 5), not a
    # regression to flag as broken — the row must report the raw triple and
    # let the caller interpret it, never say "regression"/"broken".
    assert doctor._compare("1.1.0", "0.9.1") == 1


# --------------------------------------------------------------------------
# Committed stamp / dist/COMPAT / plugin manifests
# --------------------------------------------------------------------------

def test_committed_stamp_current_when_matching(tmp_path):
    _write_pyproject(tmp_path, "1.2.3")
    _write_stamp(tmp_path, "1.2.3")
    row = doctor.check_committed_stamp(tmp_path, "1.2.3")
    assert row["status"] == doctor.CURRENT


def test_committed_stamp_stale_when_mismatched(tmp_path):
    _write_stamp(tmp_path, "1.0.0")
    row = doctor.check_committed_stamp(tmp_path, "1.2.3")
    assert row["status"] == doctor.STALE
    assert "python tools/package_clients.py" in row["remediation"]


def test_committed_stamp_unknown_when_missing(tmp_path):
    row = doctor.check_committed_stamp(tmp_path, "1.2.3")
    assert row["status"] == doctor.UNKNOWN


def test_dist_compat_not_detectable_when_absent(tmp_path):
    row = doctor.check_dist_compat(tmp_path, "1.2.3")
    assert row["status"] == doctor.NOT_DETECTABLE


def test_dist_compat_stale_when_gitignored_marker_stale(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "COMPAT").write_text("0.9.0\n", encoding="utf-8")
    row = doctor.check_dist_compat(tmp_path, "1.2.3")
    assert row["status"] == doctor.STALE
    assert "regenerate" in row["detail"]


def test_plugin_manifests_flag_only_the_mismatched_one(tmp_path):
    _write_plugin_manifests(tmp_path, "1.2.3")
    # desync one plugin
    pjson = tmp_path / "plugins" / "profile-a-extras" / ".claude-plugin" / "plugin.json"
    pjson.write_text(json.dumps({"name": "profile-a-extras", "version": "1.1.0"}), encoding="utf-8")
    rows = doctor.check_plugin_manifests(tmp_path, "1.2.3")
    statuses = {r["surface"]: r["status"] for r in rows}
    assert statuses["Plugin manifest (brainiac-manager)"] == doctor.CURRENT
    assert statuses["Plugin manifest (profile-a-extras)"] == doctor.STALE


# --------------------------------------------------------------------------
# Host engine venv (fixture-driven fake --version, never the real machine)
# --------------------------------------------------------------------------

def test_host_venv_not_detectable_when_binary_absent(tmp_path):
    row = doctor.check_host_venv(tmp_path / "nope", "1.2.3")
    assert row["status"] == doctor.NOT_DETECTABLE


def test_host_venv_current_when_version_matches(tmp_path):
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake_brain = bindir / "brain"
    fake_brain.write_text("#!/bin/sh\necho 'brain 1.2.3'\n", encoding="utf-8")
    fake_brain.chmod(0o755)
    row = doctor.check_host_venv(tmp_path, "1.2.3")
    assert row["status"] == doctor.CURRENT


def test_host_venv_stale_when_version_mismatches(tmp_path):
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake_brain = bindir / "brain"
    fake_brain.write_text("#!/bin/sh\necho 'brain 0.3.0'\n", encoding="utf-8")
    fake_brain.chmod(0o755)
    row = doctor.check_host_venv(tmp_path, "1.2.3")
    assert row["status"] == doctor.STALE
    assert row["remediation"] == "/brainiac-update"


# --------------------------------------------------------------------------
# Installed CLI plugins — current / stale / downgrade / not-detectable
# --------------------------------------------------------------------------

def _seed_marketplace(claude_home: Path, version: str, names=doctor.PLUGIN_NAMES) -> None:
    for pname in names:
        d = claude_home / "plugins" / "marketplaces" / "profile-a-marketplace" / "plugins" / pname / ".claude-plugin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "plugin.json").write_text(json.dumps({"name": pname, "version": version}), encoding="utf-8")


def _seed_installed(claude_home: Path, pname: str, version: str) -> Path:
    install_dir = claude_home / "plugins" / "cache" / "profile-a-marketplace" / pname / version
    (install_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (install_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": pname, "version": version}), encoding="utf-8"
    )
    installed_json = claude_home / "plugins" / "installed_plugins.json"
    data = {"version": 2, "plugins": {}}
    if installed_json.exists():
        data = json.loads(installed_json.read_text(encoding="utf-8"))
    data["plugins"][f"{pname}@profile-a-marketplace"] = [
        {"scope": "user", "installPath": str(install_dir), "version": version}
    ]
    installed_json.parent.mkdir(parents=True, exist_ok=True)
    installed_json.write_text(json.dumps(data), encoding="utf-8")
    return install_dir


def test_installed_cli_plugin_current(tmp_path):
    _seed_marketplace(tmp_path, "1.2.3", names=("brainiac-manager",))
    _seed_installed(tmp_path, "brainiac-manager", "1.2.3")
    rows = doctor.check_installed_cli_plugins(tmp_path, "1.2.3")
    row = next(r for r in rows if "brainiac-manager" in r["surface"])
    assert row["status"] == doctor.CURRENT


def test_installed_cli_plugin_stale_behind_marketplace(tmp_path):
    _seed_marketplace(tmp_path, "1.2.3", names=("brainiac-manager",))
    _seed_installed(tmp_path, "brainiac-manager", "1.0.0")
    rows = doctor.check_installed_cli_plugins(tmp_path, "1.2.3")
    row = next(r for r in rows if "brainiac-manager" in r["surface"])
    assert row["status"] == doctor.STALE
    assert "/plugin update" in row["remediation"]


def test_installed_cli_plugin_downgrade_condition_reports_raw_triple(tmp_path):
    # Installed > marketplace (e.g. stale 1.1.0 vs reconciled 0.9.x) must be
    # reported with the raw versions, not asserted as a regression.
    _seed_marketplace(tmp_path, "0.9.1", names=("brainiac-manager",))
    _seed_installed(tmp_path, "brainiac-manager", "1.1.0")
    rows = doctor.check_installed_cli_plugins(tmp_path, "0.9.1")
    row = next(r for r in rows if "brainiac-manager" in r["surface"])
    assert row["status"] == doctor.STALE
    assert row["raw"] == {"installed": "1.1.0", "marketplace": "0.9.1"}
    assert "uninstall" in row["remediation"] and "install" in row["remediation"]


def test_installed_cli_plugin_not_detectable_when_marketplace_absent(tmp_path):
    rows = doctor.check_installed_cli_plugins(tmp_path, "1.2.3")
    assert all(r["status"] == doctor.NOT_DETECTABLE for r in rows)


# --------------------------------------------------------------------------
# Staged workspaces + snapshot schema
# --------------------------------------------------------------------------

def test_staged_workspace_current_when_stamp_matches(tmp_path):
    ws = tmp_path / "cowork-ws"
    engine = ws / ".brain" / "engine" / "brain"
    engine.mkdir(parents=True)
    (engine / "_version.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]
    rows = doctor.check_staged_workspaces(entries, "1.2.3")
    assert rows[0]["status"] == doctor.CURRENT


def test_staged_workspace_stale_dir_flagged(tmp_path):
    ws = tmp_path / "cowork-ws"
    engine = ws / ".brain" / "engine" / "brain"
    engine.mkdir(parents=True)
    (engine / "_version.py").write_text('__version__ = "0.5.0"\n', encoding="utf-8")
    entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]
    rows = doctor.check_staged_workspaces(entries, "1.2.3")
    assert rows[0]["status"] == doctor.STALE
    assert rows[0]["remediation"] == "/brainiac-update"


def test_staged_workspace_skips_host_entries(tmp_path):
    entries = [{"target": "host", "workspace_path": str(tmp_path)}]
    rows = doctor.check_staged_workspaces(entries, "1.2.3")
    assert rows == []


def test_staged_workspace_reads_vault_path_not_workspace_path(tmp_path):
    # Real shape: workspace_path is the PARENT checkout (its own .brain, if
    # any, is the unrelated host stage — current here), vault_path is the
    # child dir the Cowork VM actually reads (stale here). Reading
    # workspace_path instead of vault_path is exactly the false-green bug
    # (doctor reported "current" off the host stage while the real Cowork
    # engine at vault_path/.brain stayed stale).
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"

    host_engine = workspace / ".brain" / "engine" / "brain"
    host_engine.mkdir(parents=True)
    (host_engine / "_version.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")

    vault_engine = vault / ".brain" / "engine" / "brain"
    vault_engine.mkdir(parents=True)
    (vault_engine / "_version.py").write_text('__version__ = "0.5.0"\n', encoding="utf-8")

    entries = [{"target": "cowork-vm", "workspace_path": str(workspace), "vault_path": str(vault)}]
    rows = doctor.check_staged_workspaces(entries, "1.2.3")
    assert rows[0]["status"] == doctor.STALE, (
        "must read vault_path/.brain (stale 0.5.0), not workspace_path/.brain (current 1.2.3)"
    )
    assert str(vault) in rows[0]["surface"]


def test_staged_workspace_missing_version_stamp_at_vault_path_is_not_detectable(tmp_path):
    # A cowork-vm engine with a missing _version.py at vault_path must never
    # read "current" by falling through to a workspace_path stage.
    workspace = tmp_path / "workspace"
    host_engine = workspace / ".brain" / "engine" / "brain"
    host_engine.mkdir(parents=True)
    (host_engine / "_version.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")

    vault = workspace / "vault"
    vault.mkdir()

    entries = [{"target": "cowork-vm", "workspace_path": str(workspace), "vault_path": str(vault)}]
    rows = doctor.check_staged_workspaces(entries, "1.2.3")
    assert rows[0]["status"] == doctor.NOT_DETECTABLE


# --------------------------------------------------------------------------
# Staged skill bundles (cw-02) — separate row from the engine stamp so a
# version-matched engine with a stale/missing skill bundle is still visible.
# --------------------------------------------------------------------------

def _write_skill_zip(skills_dir: Path, name: str, version: str, with_version_marker: bool = True) -> None:
    import zipfile

    skills_dir.mkdir(parents=True, exist_ok=True)
    zip_path = skills_dir / f"{name}.skill"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", "---\nname: x\ndescription: x\n---\nbody\n")
        if with_version_marker:
            zf.writestr(f"{name}/VERSION", version + "\n")


def test_staged_skill_bundles_current_when_version_matches(tmp_path):
    ws = tmp_path / "cowork-ws"
    _write_skill_zip(ws / ".brain" / "skills", "kb-curator", "1.2.3")
    entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]
    rows = doctor.check_staged_skill_bundles(entries, "1.2.3")
    assert rows[0]["status"] == doctor.CURRENT


def test_staged_skill_bundles_stale_when_version_mismatches(tmp_path):
    ws = tmp_path / "cowork-ws"
    _write_skill_zip(ws / ".brain" / "skills", "kb-curator", "0.5.0")
    entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]
    rows = doctor.check_staged_skill_bundles(entries, "1.2.3")
    assert rows[0]["status"] == doctor.STALE
    assert "cowork_workspace_install.sh" in rows[0]["remediation"]


def test_staged_skill_bundles_not_detectable_when_absent(tmp_path):
    ws = tmp_path / "cowork-ws"
    ws.mkdir()
    entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]
    rows = doctor.check_staged_skill_bundles(entries, "1.2.3")
    assert rows[0]["status"] == doctor.NOT_DETECTABLE


def test_staged_skill_bundles_unknown_when_no_version_marker(tmp_path):
    ws = tmp_path / "cowork-ws"
    _write_skill_zip(ws / ".brain" / "skills", "kb-curator", "1.2.3", with_version_marker=False)
    entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]
    rows = doctor.check_staged_skill_bundles(entries, "1.2.3")
    assert rows[0]["status"] == doctor.UNKNOWN


def test_staged_skill_bundles_skips_host_entries(tmp_path):
    entries = [{"target": "host", "workspace_path": str(tmp_path)}]
    rows = doctor.check_staged_skill_bundles(entries, "1.2.3")
    assert rows == []


def test_staged_skill_bundles_reads_vault_path_not_workspace_path(tmp_path):
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    _write_skill_zip(workspace / ".brain" / "skills", "kb-curator", "1.2.3")  # host stage: current
    _write_skill_zip(vault / ".brain" / "skills", "kb-curator", "0.5.0")      # vault: stale

    entries = [{"target": "cowork-vm", "workspace_path": str(workspace), "vault_path": str(vault)}]
    rows = doctor.check_staged_skill_bundles(entries, "1.2.3")
    assert rows[0]["status"] == doctor.STALE


def test_workspace_schema_current_and_skew(tmp_path):
    ws_ok = tmp_path / "ws-ok"
    (ws_ok / ".brain" / "snapshot").mkdir(parents=True)
    (ws_ok / ".brain" / "snapshot" / "snapshot.manifest.json").write_text(
        json.dumps({"schema_version": 3}), encoding="utf-8")

    ws_skew = tmp_path / "ws-skew"
    (ws_skew / ".brain" / "snapshot").mkdir(parents=True)
    (ws_skew / ".brain" / "snapshot" / "snapshot.manifest.json").write_text(
        json.dumps({"schema_version": 2}), encoding="utf-8")

    entries = [
        {"target": "cowork-vm", "workspace_path": str(ws_ok)},
        {"target": "cowork-vm", "workspace_path": str(ws_skew)},
    ]
    rows = doctor.check_workspace_schema(entries, binary_schema_version=3)
    by_ws = {r["surface"]: r for r in rows}
    assert by_ws[f"Snapshot schema ({ws_ok})"]["status"] == doctor.CURRENT
    assert by_ws[f"Snapshot schema ({ws_skew})"]["status"] == doctor.STALE


def test_workspace_schema_reads_vault_path_not_workspace_path(tmp_path):
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    (workspace / ".brain" / "snapshot").mkdir(parents=True)
    (workspace / ".brain" / "snapshot" / "snapshot.manifest.json").write_text(
        json.dumps({"schema_version": 3}), encoding="utf-8")  # host stage: current
    (vault / ".brain" / "snapshot").mkdir(parents=True)
    (vault / ".brain" / "snapshot" / "snapshot.manifest.json").write_text(
        json.dumps({"schema_version": 2}), encoding="utf-8")  # vault: stale

    entries = [{"target": "cowork-vm", "workspace_path": str(workspace), "vault_path": str(vault)}]
    rows = doctor.check_workspace_schema(entries, binary_schema_version=3)
    assert rows[0]["status"] == doctor.STALE
    assert str(vault) in rows[0]["surface"]


# --------------------------------------------------------------------------
# Desktop/Cowork plugin store — best-effort, ALWAYS manual-required, never
# gates the exit code, even when it finds a clearly stale version.
# --------------------------------------------------------------------------

def test_desktop_store_manual_required_when_absent(tmp_path):
    rows = doctor.check_desktop_plugin_store(tmp_path, "1.2.3", plugin_dir_names=("brainiac-manager",))
    assert rows[0]["status"] == doctor.MANUAL_REQUIRED


def test_desktop_store_picks_most_recent_session_by_mtime(tmp_path):
    sessions_root = tmp_path / "local-agent-mode-sessions"
    old_dir = sessions_root / "sess-old" / "sub" / "rpm" / "plugin_AAA" / ".claude-plugin"
    new_dir = sessions_root / "sess-new" / "sub" / "rpm" / "plugin_BBB" / ".claude-plugin"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    (old_dir / "plugin.json").write_text(
        json.dumps({"name": "brainiac-manager", "version": "0.5.0"}), encoding="utf-8")
    (new_dir / "plugin.json").write_text(
        json.dumps({"name": "brainiac-manager", "version": "1.2.3"}), encoding="utf-8")
    import os
    import time

    old_time = time.time() - 1000
    os.utime(old_dir / "plugin.json", (old_time, old_time))

    rows = doctor.check_desktop_plugin_store(tmp_path, "1.2.3", plugin_dir_names=("brainiac-manager",))
    row = rows[0]
    # ALWAYS manual-required even though the newest candidate matches SSOT —
    # this surface never gates the exit code (ADR-0005 Ruling 2/4).
    assert row["status"] == doctor.MANUAL_REQUIRED
    assert row["raw"]["version"] == "1.2.3"
    assert row["raw"]["candidates"] == 2
    assert "last-seen (mtime" in row["detail"]


def test_desktop_store_never_gates_exit_code_even_when_stale(tmp_path):
    # A stale-session dir (per HARDEN:blindspot fixture requirement): the only
    # candidate reports an old version. Still manual-required, never STALE.
    sessions_root = tmp_path / "local-agent-mode-sessions"
    d = sessions_root / "sess-stale" / "sub" / "rpm" / "plugin_CCC" / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(
        json.dumps({"name": "brainiac-manager", "version": "0.1.0"}), encoding="utf-8")
    rows = doctor.check_desktop_plugin_store(tmp_path, "9.9.9", plugin_dir_names=("brainiac-manager",))
    assert rows[0]["status"] == doctor.MANUAL_REQUIRED
    assert rows[0]["status"] not in doctor._GATING_STATUSES


# --------------------------------------------------------------------------
# Marketplace cache freshness — LOCAL cache only, never conflated with
# "published" freshness (HARDEN:codex-HIGH).
# --------------------------------------------------------------------------

def _init_git_repo_with_remote(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def test_marketplace_cache_not_detectable_when_not_a_git_repo(tmp_path):
    row = doctor.check_marketplace_cache(tmp_path)
    assert row["status"] == doctor.NOT_DETECTABLE


def test_marketplace_cache_not_detectable_without_upstream(tmp_path):
    _init_git_repo_with_remote(tmp_path)
    row = doctor.check_marketplace_cache(tmp_path)
    assert row["status"] == doctor.NOT_DETECTABLE


def test_marketplace_cache_current_when_local_head_matches_tracked_upstream(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=remote, check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
    (clone / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD"], cwd=clone, check=True)

    row = doctor.check_marketplace_cache(clone)
    assert row["status"] == doctor.CURRENT
    assert row["raw"]["commits_behind_cache"] == 0
    assert "cache not refreshed" in row["detail"]


def test_marketplace_cache_stale_reports_local_cache_only_language(tmp_path):
    """A 'stale local cache' means: HEAD sits behind the LOCALLY RECORDED
    origin/master ref (last fetch), with no fetch run by this check itself.
    Built directly via plumbing (checkout at an older commit, origin/master
    ref pointed at a newer one already-known-locally) rather than a second
    real remote push, since check_marketplace_cache only ever reads local
    refs — it must never call fetch."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=remote, check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
    (clone / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD"], cwd=clone, check=True)

    # Add a second local commit, then rewind HEAD one step and point the
    # locally-recorded origin/master ref at the tip — this is exactly the
    # on-disk shape of "origin/master (as last fetched) is 1 ahead of HEAD".
    (clone / "g.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=clone, check=True)
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, check=True,
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/master", tip], cwd=clone, check=True)
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=clone, check=True)

    row = doctor.check_marketplace_cache(clone)
    assert row["status"] == doctor.STALE
    assert "cache not refreshed" in row["detail"]
    assert row["raw"]["commits_behind_cache"] == 1


# --------------------------------------------------------------------------
# Table rendering + exit-code gate semantics
# --------------------------------------------------------------------------

def test_render_human_includes_icons_and_remediation():
    report = {
        "ssot_version": "1.2.3",
        "ok": False,
        "stale_count": 1,
        "rows": [
            doctor._row("Foo", doctor.CURRENT, "all good"),
            doctor._row("Bar", doctor.STALE, "needs fixing", remediation="do the thing"),
        ],
    }
    text = doctor.render_human(report)
    assert "✅" in text
    assert "⚠️" in text
    assert "do the thing" in text
    assert "STALE: 1 required surface" in text


def test_manual_required_and_not_detectable_never_gate_exit_code():
    report = {
        "ssot_version": "1.2.3",
        "rows": [
            doctor._row("Desktop store", doctor.MANUAL_REQUIRED, "best effort"),
            doctor._row("Some optional thing", doctor.NOT_DETECTABLE, "n/a here"),
        ],
        "ok": True,
        "stale_count": 0,
    }
    # Reconstruct via the real gate logic used in run_doctor
    gating = [r for r in report["rows"] if r["status"] in doctor._GATING_STATUSES]
    assert gating == []


def test_run_doctor_end_to_end_all_fixtures(tmp_path):
    """Full orchestration against an entirely fixture-built tree — proves
    run_doctor never needs the live machine and produces at least one CURRENT
    row plus a remediation command for the row we deliberately made STALE."""
    repo_root = tmp_path / "repo"
    _write_pyproject(repo_root, "1.2.3")
    _write_stamp(repo_root, "1.2.3")
    _write_plugin_manifests(repo_root, "1.2.3")
    (repo_root / "dist").mkdir()
    (repo_root / "dist" / "COMPAT").write_text("1.2.3\n", encoding="utf-8")

    claude_home = tmp_path / "claude_home"
    _seed_marketplace(claude_home, "1.2.3")
    for pname in doctor.PLUGIN_NAMES:
        _seed_installed(claude_home, pname, "1.2.3")

    # One staged workspace, deliberately STALE, to prove the remediation
    # column + non-zero exit path.
    ws = tmp_path / "cowork-ws"
    engine = ws / ".brain" / "engine" / "brain"
    engine.mkdir(parents=True)
    (engine / "_version.py").write_text('__version__ = "0.5.0"\n', encoding="utf-8")
    registry_entries = [{"target": "cowork-vm", "workspace_path": str(ws)}]

    app_support = tmp_path / "app_support"  # absent -> manual-required rows

    report = doctor.run_doctor(
        repo_root=repo_root,
        brainiac_home=tmp_path / "brainiac_home",  # absent -> not-detectable
        claude_home=claude_home,
        app_support_dir=app_support,
        registry_entries=registry_entries,
        marketplace_dir=claude_home / "plugins" / "marketplaces" / "profile-a-marketplace",
    )

    assert report["ssot_version"] == "1.2.3"
    assert any(r["status"] == doctor.CURRENT for r in report["rows"])
    stale_ws_row = next(r for r in report["rows"] if "Staged workspace" in r["surface"])
    assert stale_ws_row["status"] == doctor.STALE
    assert stale_ws_row["remediation"] == "/brainiac-update"
    # cw-02: no .brain/skills/ staged in this fixture -> not-detectable, and
    # it must appear as its own row (not silently folded into the engine row).
    skill_row = next(r for r in report["rows"] if "Staged skill bundles" in r["surface"])
    assert skill_row["status"] == doctor.NOT_DETECTABLE
    assert report["ok"] is False
    assert report["stale_count"] >= 1

    text = doctor.render_human(report)
    assert "✅" in text
    assert "/brainiac-update" in text


# --------------------------------------------------------------------------
# VM leg (2026-07-07 addendum) — role-aware doctor for the staged
# zero-install Cowork copy. All-fixture: builds a staged-layout tmp dir
# instead of touching the real machine.
# --------------------------------------------------------------------------

def _seed_vm_workspace(vault: Path, *, skill_version: str | None = None,
                        schema_version: int = 3, snapshot_age_s: float = 10.0,
                        with_model: bool = True) -> None:
    """Build a staged Cowork VM workspace layout under ``vault/.brain/``.

    ``run_doctor_vm`` reads the ENGINE version live (``brain.__version__`` of
    the currently-running package — on a real VM that IS the staged copy's
    own stamp, transitively, via the existing fallback chain in
    ``brain/__init__.py``), so ``skill_version`` defaults to matching it."""
    import time
    import zipfile

    import brain as _brain

    if skill_version is None:
        skill_version = _brain.__version__

    brain_dir = vault / ".brain"
    (brain_dir / "engine" / "brain").mkdir(parents=True)

    skills_dir = brain_dir / "skills"
    skills_dir.mkdir(parents=True)
    if skill_version is not None:
        with zipfile.ZipFile(skills_dir / "kb-curator.skill", "w") as zf:
            zf.writestr("kb-curator/SKILL.md", "---\nname: x\ndescription: x\n---\nbody\n")
            zf.writestr("kb-curator/VERSION", skill_version + "\n")

    snap_dir = brain_dir / "snapshot"
    snap_dir.mkdir(parents=True)
    created_epoch = time.time() - snapshot_age_s
    (snap_dir / "snapshot.manifest.json").write_text(json.dumps({
        "generation": 1, "created_epoch": created_epoch,
        "created_iso": "2026-07-07T00:00:00", "source_db": "x", "snapshot_db": "x",
        "sha256": "abc", "bytes": 1, "notes": 5, "chunks": 10,
        "embed_model": "e", "embed_dim": "1", "schema_version": schema_version,
    }), encoding="utf-8")

    if with_model:
        model_dir = brain_dir / "model"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_text("x", encoding="utf-8")


def test_looks_like_vm_stage_true_when_host_only_inputs_absent(tmp_path):
    # No tools/workspace_registry.py, no pyproject.toml SSOT.
    assert doctor.looks_like_vm_stage(tmp_path) is True


def test_looks_like_vm_stage_false_on_full_host_checkout(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "workspace_registry.py").write_text("", encoding="utf-8")
    _write_pyproject(tmp_path, "1.2.3")
    assert doctor.looks_like_vm_stage(tmp_path) is False


def test_run_doctor_vm_current_on_fresh_staged_workspace(tmp_path):
    vault = tmp_path / "vault"
    _seed_vm_workspace(vault)
    report = doctor.run_doctor_vm(vault=vault)
    assert report["role"] == "vm"
    assert report["ok"] is True
    assert report["stale_count"] == 0
    engine_row = next(r for r in report["rows"] if "Engine version" in r["surface"])
    assert engine_row["status"] == doctor.CURRENT
    host_rows = [r for r in report["rows"] if "host Mac" in r["detail"]]
    assert host_rows and all(r["status"] == doctor.NOT_DETECTABLE for r in host_rows)
    text = doctor.render_human(report)
    assert "brain doctor" in text


def test_run_doctor_vm_stale_when_skill_bundle_mismatches(tmp_path):
    vault = tmp_path / "vault"
    _seed_vm_workspace(vault, skill_version="0.9.0")
    report = doctor.run_doctor_vm(vault=vault)
    assert report["ok"] is False
    skill_row = next(r for r in report["rows"] if "Staged skill bundles" in r["surface"])
    assert skill_row["status"] == doctor.STALE


def test_run_doctor_vm_stale_when_engine_stamp_missing(tmp_path, monkeypatch):
    import brain

    vault = tmp_path / "vault"
    _seed_vm_workspace(vault)
    monkeypatch.setattr(brain, "__version__", "0.0.0+unknown")
    report = doctor.run_doctor_vm(vault=vault)
    engine_row = next(r for r in report["rows"] if "Engine version" in r["surface"])
    assert engine_row["status"] == doctor.STALE
    assert "cowork_workspace_install.sh" in engine_row["remediation"]
    assert report["ok"] is False


def test_run_doctor_vm_stale_when_schema_skewed(tmp_path):
    vault = tmp_path / "vault"
    _seed_vm_workspace(vault, schema_version=999)
    report = doctor.run_doctor_vm(vault=vault)
    schema_row = next(r for r in report["rows"] if "Snapshot schema" in r["surface"])
    assert schema_row["status"] == doctor.STALE
    assert report["ok"] is False


def test_run_doctor_vm_flags_missing_model_cache(tmp_path):
    vault = tmp_path / "vault"
    _seed_vm_workspace(vault, with_model=False)
    report = doctor.run_doctor_vm(vault=vault)
    model_row = next(r for r in report["rows"] if "Model cache" in r["surface"])
    assert model_row["status"] == doctor.STALE
    assert report["ok"] is False


def test_run_doctor_vm_reports_absent_snapshot_as_not_detectable(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".brain" / "engine" / "brain").mkdir(parents=True)
    (vault / ".brain" / "engine" / "brain" / "_version.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8")
    report = doctor.run_doctor_vm(vault=vault)
    snap_row = next(r for r in report["rows"] if r["surface"].startswith("Snapshot ("))
    assert snap_row["status"] == doctor.NOT_DETECTABLE
    # never gates: absent snapshot is not-detectable, not stale
    assert snap_row["status"] not in doctor._GATING_STATUSES


def test_run_doctor_never_crashes_when_workspace_registry_unavailable(tmp_path, monkeypatch):
    """The host-mode orchestrator must degrade to a NOT_DETECTABLE row rather
    than raising ModuleNotFoundError, even when called with registry_entries
    left as the default (the exact staged-VM-with-role=host crash shape)."""
    import builtins
    import sys

    repo_root = tmp_path / "repo"  # no tools/workspace_registry.py here
    _write_pyproject(repo_root, "1.2.3")
    _write_stamp(repo_root, "1.2.3")
    # Another test (or this repo's own tools/ on sys.path from an earlier
    # sys.path.insert in run_doctor itself) may make the REAL
    # workspace_registry importable regardless of this fixture's empty repo
    # dir. Force the import to fail so this test exercises the actual
    # guarded-import path deterministically, independent of run order.
    monkeypatch.delitem(sys.modules, "workspace_registry", raising=False)
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "workspace_registry":
            raise ModuleNotFoundError("workspace_registry")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    report = doctor.run_doctor(
        repo_root=repo_root,
        brainiac_home=tmp_path / "brainiac_home",
        claude_home=tmp_path / "claude_home",
        app_support_dir=tmp_path / "app_support",
        marketplace_dir=tmp_path / "claude_home" / "plugins" / "marketplaces" / "profile-a-marketplace",
        # registry_entries left as None -> exercises the guarded import path
    )
    registry_row = next(r for r in report["rows"] if "Workspace registry" in r["surface"])
    assert registry_row["status"] == doctor.NOT_DETECTABLE
    assert "--role vm" in registry_row["remediation"]
