"""``brain update`` (ADR-0005 Ruling 3, UP-01/UP-02) — the self-executing
refresh: marketplace refresh -> downgrade-safe CLI-plugin reinstall -> engine
venv refresh -> dist/ rebuild -> workspace re-stage -> ``brain doctor`` verify,
with one before->after version table and one pass/fail.

This module RUNS the operations; it does not print instructions for a human
to copy-paste (that was the old ``/brainiac-update`` skill's failure mode —
ADR-0005 Ruling 3 amends ADR-0004 Ruling 5's "print exactly that instruction"
migration prose into an automatic clean reinstall).

Every external effect (subprocess call, filesystem mutation) goes through the
injectable ``runner`` so tests exercise the real decision logic against
fixtures/fakes with zero live-machine or plugin-store mutation — required for
this session's unattended, non-destructive contract.

Preflight capability probe (HARDEN:consensus-HIGH): before any destructive
`claude plugin` call, confirm the CLI surface this module depends on
(marketplace update / list / uninstall / install) actually exists. Never
drive an unversioned CLI blind.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys as sys
import tempfile as tempfile
from pathlib import Path
from typing import Any, Callable, Optional


from .reexec import reexec_after_engine_move  # noqa: F401
from .vmstaging import stage_vm_binaries, vm_binaries_verdict
from .doctor import (
    CHANNEL_EDITABLE,
    CHANNEL_PIP_USER,
    CHANNEL_PIPX,
    CHANNEL_PYPI_UV,
    CHANNEL_VENV_WHEEL,
    _compare,
    _ssot_version,
    check_installed_cli_plugins,
    detect_install_channel,
    fetch_pypi_latest_version,
    marketplace_install_location as marketplace_install_location,
    render_human,
    run_doctor,
)
from .update_channels import (
    WorkspaceStageCallbacks,
    refresh_engine_channel as refresh_engine_channel,
    stage_engine_and_skills as stage_engine_and_skills_impl,
    restage_workspaces as restage_workspaces_impl,
)
from .update_channels_2 import UpdateFlowCallbacks, run_update_flow
from .update_plugins import _default_runner as _default_runner  # noqa: F401
from .update_state import (  # noqa: F401  (facade re-export)
    UPDATE_RETRY_ESCALATE_AFTER as UPDATE_RETRY_ESCALATE_AFTER,
    failure_is_moot as failure_is_moot,
    read_update_state as read_update_state,
    retry_decision as retry_decision,
    update_state_path as update_state_path,
    write_update_state as write_update_state,
)

# --------------------------------------------------------------------------
# Runner abstraction — the ONLY place subprocess.run is called from this
# module. Tests inject a fake that records calls instead of executing them
# (dry-run-by-construction), so the version-compare/downgrade-decision logic
# is exercised without ever touching a real plugin store or venv.
# --------------------------------------------------------------------------

CLAUDE_HOME_ENV_VAR = "BRAIN_CLAUDE_HOME"


def claude_home_default() -> Path:
    """The Claude Code config dir these verbs write to when no caller names one.

    Honours ``$BRAIN_CLAUDE_HOME`` so a caller that must NOT touch the real
    ``~/.claude`` redirects the BASE once and every site follows. Three sites
    in this module spelled ``Path.home() / ".claude"`` inline, and the
    update-flow tests drove all of them: they monkeypatch every other
    side-effecting step, so ``run_update`` placed real hook scripts into the
    owner's own ``~/.claude`` on every suite run. That looked harmless while
    the only script was one whose content never changed; the moment a SECOND
    hook shipped (2026-09-01) the same tests began depositing an UNTRACKED
    file into a tree ``gearbox deploy`` refuses to deploy over.

    An env var, not a patched function: a fixed call site is one call site,
    and the next one added would leak again.
    """
    override = os.environ.get(CLAUDE_HOME_ENV_VAR, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".claude"


def resolve_claude_bin() -> Optional[str]:
    """Locate the ``claude`` CLI the way the shipped scripts already do.

    A bare ``shutil.which`` is correct in a login shell and WRONG under
    launchd, which hands a job the minimal ``/usr/bin:/bin:/usr/sbin:/sbin``
    unless its plist sets PATH. On 2026-08-12 the nightly auto-update aborted
    with "`claude` CLI not found on PATH" while the binary sat at
    ``~/.local/bin/claude`` — the nightly plist sets no PATH, and the synthesis
    plist (which does) is why that lane never hit this.

    Same precedence as ``brain-synthesis.sh``: an explicit override, then PATH,
    then the standard install location. Returns None only if there is genuinely
    no executable to run.
    """
    override = os.environ.get("BRAIN_CLAUDE_BIN")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("claude")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/claude")
    return fallback if os.access(fallback, os.X_OK) else None


Runner = Callable[..., "subprocess.CompletedProcess[str]"]




# --------------------------------------------------------------------------
# Update-available detection (RC5) + the update-state record. `brain doctor`'s
# maintainer-facing `--check-registry` row answers "is the marketplace ahead of
# PyPI"; this is the CONSUMER-facing "is a newer version available to ME, on my
# channel" — channel-aware and zero-network when a local checkout is the SSOT.
# --------------------------------------------------------------------------

def check_update_available(
    installed: Optional[str],
    channel: str,
    engine_src: Optional[Path],
    *,
    fetch: Callable[[], Optional[str]] = fetch_pypi_latest_version,
) -> dict:
    """Channel-aware: a local-checkout channel compares ``installed`` against the
    checkout's own SSOT (``pyproject.toml`` — no network); a PyPI channel does
    the existing 3s-timeout PyPI fetch. A ``None`` latest, or a downgrade
    (latest <= installed), reports ``available: False`` — never nags."""
    latest: Optional[str] = None
    source: Optional[str] = None
    has_checkout = engine_src is not None and (engine_src / "pyproject.toml").exists()
    if channel in (CHANNEL_EDITABLE, CHANNEL_VENV_WHEEL) and has_checkout:
        latest = _ssot_version(engine_src)  # type: ignore[arg-type]
        source = "local-ssot"
    elif channel in (CHANNEL_PYPI_UV, CHANNEL_PIPX, CHANNEL_PIP_USER, CHANNEL_VENV_WHEEL):
        latest = fetch()
        source = "pypi"
    if not latest or not installed:
        return {"available": False, "installed": installed, "latest": latest, "source": source}
    available = _compare(installed, latest) < 0
    return {"available": available, "installed": installed, "latest": latest, "source": source}


def detect_and_check_update(
    brainiac_home: Path, claude_home: Optional[Path] = None, run: Runner = _default_runner,
) -> dict:
    """One-call detection the hourly maintain auto-apply uses: resolve the live
    channel + installed version + engine source, then answer "is a newer
    version available to this machine". Zero network on a local-checkout
    channel; a 3s PyPI fetch otherwise (via ``check_update_available``)."""
    claude_home = claude_home or claude_home_default()
    legacy_bin = _venv_bin(brainiac_home / "venv", "brain")
    which_brain = shutil.which("brain")
    brain_bin: Optional[Path] = legacy_bin if legacy_bin.exists() else (
        Path(which_brain) if which_brain else None
    )
    channel = detect_install_channel(brain_bin) if brain_bin else CHANNEL_EDITABLE
    installed: Optional[str] = None
    if brain_bin and Path(brain_bin).exists():
        try:
            out = run([str(brain_bin), "--version"])
            txt = (out.stdout or out.stderr or "").strip()
            m = re.search(r"(\d+\.\d+\.\d+\S*)", txt)
            installed = m.group(1) if m else (txt or None)
        except Exception:
            installed = None
    engine_src = resolve_engine_source(claude_home=claude_home)
    avail = check_update_available(installed, channel, engine_src)
    return {**avail, "channel": channel, "engine_src": engine_src,
            "brain_bin": str(brain_bin) if brain_bin else None}

# --------------------------------------------------------------------------
# Workspace re-stage — thin wrapper delegating to workspace_registry; never
# reimplements its locking/schema. For target "cowork-vm" this re-runs the
# (a) engine-source and (d) skill-bundle legs of
# tools/cowork_workspace_install.sh directly in Python (not by shelling into
# the whole script, which also re-rebuilds the host index and re-scaffolds
# the overlay every run — steps the `brain sync --publish` call right after
# already covers for a routine update). Doing the copy in Python keeps it
# testable with plain tmp-path fixtures instead of a real bash subprocess.
# --------------------------------------------------------------------------

def stage_engine_and_skills(
    engine_src: Path,
    vault_path: str,
    *,
    workspace_path: str | None = None,
    model_source: tuple[Path, str] | None = None,
) -> dict:
    """(a)+(b)+(d) legs of cowork_workspace_install.sh for one cowork-vm
    workspace: re-copy the engine source into the staging root's
    ``engine/brain/`` and refresh the ``.skill`` bundles into its ``skills/``
    from whatever ``dist/cowork-skills/*.skill`` currently ships in
    ``engine_src`` (tools/release.py already runs tools/package_clients.py
    before every cut, so that dir is the SSOT-version build by the time
    `brain update` runs).

    The second positional argument is the VAULT. It was named
    ``workspace_path`` and called with ``vault_path``; the two are the same
    shape only while the vault sits inside the workspace. ``workspace_path``
    is now a separate keyword and
    :func:`brain.cowork_staging.staging_root` resolves the staging root from
    both, so the VM keeps executing an engine ON the mount after the notes
    leave it.

    Returns a dict with the SSOT/staged versions and skill count so the
    caller can assert-and-fail rather than silently report ok.
    """
    from .cowork_staging import staging_root

    callbacks = WorkspaceStageCallbacks(
        packaged_script=_packaged_script,
        resolve_model_source=_resolve_shipped_model_source,
        stage_model_cache=_stage_model_cache,
        ssot_version=_ssot_version,
        read_version_stamp=_read_version_stamp,
    )
    staged = stage_engine_and_skills_impl(
        engine_src,
        vault_path,
        workspace_path=workspace_path,
        model_source=model_source,
        callbacks=callbacks,
    )
    # The frozen VM binaries go to the SAME staging root as the engine they
    # run --- `Path(workspace_path) / ".brain"` split them apart the moment
    # the two paths differ, staging an engine the ELFs could not find.
    bin_status = stage_vm_binaries(
        staging_root(vault_path, workspace_path), engine_src / "dist"
    )
    binaries_ok, binaries_detail = vm_binaries_verdict(
        bin_status, staged.get("ssot_version")
    )
    return {
        **staged,
        "binaries": bin_status,
        "binaries_ok": binaries_ok,
        "binaries_detail": binaries_detail,
    }


def restage_workspaces(
    engine_src: Path, brainiac_home: Path, run: Runner = _default_runner,
) -> list[dict]:
    return restage_workspaces_impl(
        engine_src,
        brainiac_home,
        run,
        stage_workspace=stage_engine_and_skills,
        resolve_model_source=_resolve_shipped_model_source,
    )


# --------------------------------------------------------------------------
# Before -> after version table
# --------------------------------------------------------------------------

def render_before_after(table: list[dict]) -> str:
    lines = ["Before -> after version table", "-" * 32]
    surface_w = max((len(r["surface"]) for r in table), default=8) + 2
    for r in table:
        lines.append(f"{r['surface']:<{surface_w}}{r['before']:<16} -> {r['after']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# UP-02 — the single top-level orchestrating entry point
# --------------------------------------------------------------------------

def run_update(
    *,
    marketplace_name: str = "brainiac",
    engine_src: Optional[str] = None,
    brainiac_home: Optional[Path] = None,
    claude_home: Optional[Path] = None,
    run: Runner = _default_runner,
    skip_capability_probe: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Chain, in order: capability probe -> marketplace refresh -> per-plugin
    downgrade-safe reinstall -> engine venv refresh -> dist/ rebuild ->
    workspace re-stage -> `brain doctor` verify. One pass/fail, one
    before->after table.

    ``dry_run=True`` runs every read/decision step for real but skips every
    mutating call (marketplace update, plugin install/uninstall, pip install,
    workspace re-stage) — used by the evidence transcript in this session so
    no live plugin store or venv is touched.

    The delegated flow preserves the source ordering contract:
    ``refresh_marketplace(marketplace_name, run=run)`` precedes
    ``before_doctor = run_doctor(``.
    """
    brainiac_home = brainiac_home or Path(
        os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac")
    )
    claude_home = claude_home or claude_home_default()
    resolved_engine_src = resolve_engine_source(
        explicit=engine_src,
        claude_home=claude_home,
    )
    callbacks = UpdateFlowCallbacks(
        probe_capability=probe_cli_capability,
        refresh_marketplace=refresh_marketplace,
        run_doctor=run_doctor,
        render_human=render_human,
        decide_plugin_action=decide_plugin_action,
        apply_plugin_action=apply_plugin_action,
        check_installed_cli_plugins=check_installed_cli_plugins,
        refresh_engine_venv=refresh_engine_venv,
        rebuild_dist=rebuild_dist,
        restage_workspaces=restage_workspaces,
        render_before_after=render_before_after,
        compare=_compare,
        reexec_after_engine_move=reexec_after_engine_move,
    )
    return run_update_flow(
        marketplace_name=marketplace_name,
        engine_src=resolved_engine_src,
        brainiac_home=brainiac_home,
        claude_home=claude_home,
        run=run,
        skip_capability_probe=skip_capability_probe,
        dry_run=dry_run,
        callbacks=callbacks,
    )


def _demo() -> None:
    """ponytail self-check: decision function + dry-run orchestration never
    crash and never call a real subprocess in dry-run mode."""

    assert decide_plugin_action(None, "1.0.0") == "install"
    assert decide_plugin_action("1.0.0", None) == "skip"
    assert decide_plugin_action("1.1.0", "0.9.1") == "reinstall"
    assert decide_plugin_action("0.9.0", "0.9.1") == "update"
    assert decide_plugin_action("0.9.1", "0.9.1") == "skip"

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    report = run_update(run=fake_run, dry_run=True, skip_capability_probe=True)
    assert "ok" in report
    assert calls == [], "dry_run must never invoke the injected runner for mutating steps"
    print("OK: update self-check passed")



# The plugin-channel steps live in update_plugins.py and the dist/model staging
# legs in update_model.py since the 2026-08-16 size ratchet; re-exported so
# every `brain.update.<name>` caller and monkeypatch target is unchanged.
# The engine-source and venv-refresh legs live in update_venv.py since the
# 2026-09-04 size ratchet; re-exported so every `brain.update.<name>` caller
# and monkeypatch target is unchanged.
from .update_venv import (  # noqa: E402,F401  (facade re-export)
    _owned_by_current_uid as _owned_by_current_uid,
    _packaged_script as _packaged_script,
    _venv_bin as _venv_bin,
    refresh_engine_venv as refresh_engine_venv,
    resolve_engine_source as resolve_engine_source,
)

from .update_model import (  # noqa: E402,F401  (facade re-export)
    _VERSION_STAMP_RE as _VERSION_STAMP_RE,
    _read_version_stamp as _read_version_stamp,
    _resolve_shipped_model_source as _resolve_shipped_model_source,
    _stage_model_cache as _stage_model_cache,
    rebuild_dist as rebuild_dist,
)
from .update_plugins import (  # noqa: E402,F401  (facade re-export)
    REQUIRED_SUBCOMMANDS as REQUIRED_SUBCOMMANDS,
    apply_plugin_action as apply_plugin_action,
    decide_plugin_action as decide_plugin_action,
    probe_cli_capability as probe_cli_capability,
    refresh_marketplace as refresh_marketplace,
)


# The ``__main__`` guard lives at the very END of the module, AFTER the
# facade re-exports above: under runpy (``python -m brain.<mod>``) the guard
# fires at its source position, and every facade name must already be bound
# by then (2026-08-16 size-ratchet fix).
if __name__ == "__main__":
    _demo()
