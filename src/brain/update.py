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
import sys
import tempfile
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
    marketplace_install_location,
    render_human,
    run_doctor,
)
from .update_channels import (
    WorkspaceStageCallbacks,
    refresh_engine_channel,
    stage_engine_and_skills as stage_engine_and_skills_impl,
    restage_workspaces as restage_workspaces_impl,
)
from .update_channels_2 import UpdateFlowCallbacks, run_update_flow
from .update_plugins import _default_runner as _default_runner  # noqa: F401

# --------------------------------------------------------------------------
# Runner abstraction — the ONLY place subprocess.run is called from this
# module. Tests inject a fake that records calls instead of executing them
# (dry-run-by-construction), so the version-compare/downgrade-decision logic
# is exercised without ever touching a real plugin store or venv.
# --------------------------------------------------------------------------

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


def _packaged_script(name: str, engine_src: Optional[Path] = None) -> Optional[Path]:
    """Resolve a bundled script (``vm-selftest.sh``, ``brainiac-alerts.sh``)
    from either the packaged wheel mirror (``brain/_assets/scripts/``) or the
    checkout's own ``scripts/`` original — first hit wins. Returns None if
    neither exists."""
    candidates: list[Path] = []
    try:
        from importlib.resources import files
        candidates.append(Path(str(files("brain") / "_assets" / "scripts" / name)))
    except Exception:
        pass
    if engine_src is not None:
        candidates.append(engine_src / "src" / "brain" / "_assets" / "scripts" / name)
        candidates.append(engine_src / "scripts" / name)
    for c in candidates:
        if c.is_file():
            return c
    return None


# --------------------------------------------------------------------------
# Engine venv refresh — resolves the engine source path from the workspace
# registry / explicit override, NEVER a hardcoded ~/brainiac (HARDEN:codex-MEDIUM).
# --------------------------------------------------------------------------

def resolve_engine_source(
    *, explicit: Optional[str] = None, repo_root: Optional[Path] = None,
    claude_home: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the engine checkout to build/refresh against, in order (RC1):

    1. an explicit override (config / CLI flag),
    2. ``$BRAINIAC_ENGINE_SRC``,
    3. an explicit ``repo_root`` (caller-supplied),
    4. the repo root this module ships from — but ONLY when it actually carries
       a ``pyproject.toml`` (a wheel install resolves this inside site-packages,
       which has none — the exact RC1 mis-resolution),
    5. the marketplace's ``installLocation`` (known_marketplaces.json), again
       pyproject-guarded — the one already-persisted pointer to the real
       checkout on a directory-source install,
    6. ``None`` — no checkout resolvable. The downstream gate then honestly
       skips dist-rebuild / workspace re-stage instead of pointing pip at a
       nonexistent ``~/brainiac`` (the deleted last-resort fallback).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAINIAC_ENGINE_SRC")
    if env:
        return Path(env).expanduser().resolve()
    if repo_root is not None:
        return repo_root.resolve()
    inferred = Path(__file__).resolve().parent.parent.parent
    if (inferred / "pyproject.toml").exists():
        return inferred
    claude_home = claude_home or (Path.home() / ".claude")
    loc = marketplace_install_location(claude_home)
    if loc and (loc / "pyproject.toml").exists():
        return loc
    return None


def _venv_bin(venv_dir: Path, name: str) -> Path:
    """Path to an executable inside a venv, cross-platform (Windows fix): POSIX
    venvs put executables in ``bin/`` bare; Windows in ``Scripts\\`` with a
    ``.exe`` suffix."""
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def refresh_engine_venv(
    engine_src: Optional[Path], brainiac_home: Path, run: Runner = _default_runner,
) -> dict:
    """Channel-aware refresh (PYP-04 / RC2): detect which channel the host is
    actually on — the legacy editable dev checkout, a plain wheel in
    ``~/.brainiac/venv`` (RC2: previously misdetected as editable), or one of
    the three PyPI channels (uv tool / pipx / pip --user) — and run THAT
    channel's own upgrade command. Detecting the live channel via the
    PATH-resolved `brain` binary is what makes `brain update` self-heal the
    right thing regardless of how the engine was installed.
    """
    return refresh_engine_channel(
        engine_src,
        brainiac_home,
        run,
        detect_channel=detect_install_channel,
        venv_bin=_venv_bin,
        which_brain=shutil.which("brain"),
        python_executable=sys.executable,
    )


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
    claude_home = claude_home or (Path.home() / ".claude")
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


def update_state_path(brainiac_home: Path) -> Path:
    return brainiac_home / "update-state.json"


def read_update_state(brainiac_home: Path) -> Optional[dict]:
    try:
        import json
        return json.loads(update_state_path(brainiac_home).read_text(encoding="utf-8"))
    except Exception:
        return None


def failure_is_moot(state: Optional[dict], installed: Optional[str]) -> bool:
    """True when a recorded FAILED update targeted a version the machine has
    SINCE REACHED — the record describes a past state and must stop nagging.

    Nothing else clears a `failed` record: the availability check writes one
    only when an update IS available, so once the machine catches up (manually,
    or by a later version applying cleanly) the stale banner ran on in every
    session until the hook's 7-day freshness window expired. Judged on the
    DETERMINED comparison installed >= the version that failed — never on
    `available: False`, which also means "could not check".
    """
    target = (state or {}).get("latest")
    if not target or not installed:
        return False
    try:
        return _compare(str(installed), str(target)) >= 0
    except Exception:
        return False


def write_update_state(
    brainiac_home: Path, *, status: str, installed: Optional[str] = None,
    latest: Optional[str] = None, source: Optional[str] = None,
    at: Optional[str] = None, detail: str = "",
) -> Path:
    """Persist the auto-update record the session-start hook reads (file-only,
    no engine call). ``status`` ∈ {available, applied, failed}."""
    import json
    p = update_state_path(brainiac_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "status": status, "installed": installed, "latest": latest,
        "source": source, "at": at, "detail": detail,
    }), encoding="utf-8")
    return p


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
    workspace_path: str,
    *,
    model_source: tuple[Path, str] | None = None,
) -> dict:
    """(a)+(b)+(d) legs of cowork_workspace_install.sh for one cowork-vm
    workspace: re-copy the engine source into ``<vault>/.brain/engine/brain/``
    and refresh the ``.skill`` bundles into ``<vault>/.brain/skills/`` from
    whatever ``dist/cowork-skills/*.skill`` currently ships in ``engine_src``
    (tools/release.py already runs tools/package_clients.py before every cut,
    so that dir is the SSOT-version build by the time `brain update` runs).

    Returns a dict with the SSOT/staged versions and skill count so the
    caller can assert-and-fail rather than silently report ok.
    """
    callbacks = WorkspaceStageCallbacks(
        packaged_script=_packaged_script,
        resolve_model_source=_resolve_shipped_model_source,
        stage_model_cache=_stage_model_cache,
        ssot_version=_ssot_version,
        read_version_stamp=_read_version_stamp,
    )
    staged = stage_engine_and_skills_impl(
        engine_src,
        workspace_path,
        model_source=model_source,
        callbacks=callbacks,
    )
    bin_status = stage_vm_binaries(Path(workspace_path) / ".brain", engine_src / "dist")
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
    claude_home = claude_home or (Path.home() / ".claude")
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
