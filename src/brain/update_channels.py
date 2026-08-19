"""Update rollout operations."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .doctor import (
    CHANNEL_EDITABLE,
    CHANNEL_PIP_USER,
    CHANNEL_PIPX,
    CHANNEL_PYPI_UV,
    CHANNEL_VENV_WHEEL,
)


Runner = Callable[..., subprocess.CompletedProcess[str]]
ChannelDetector = Callable[[Optional[Path]], str]
VenvBinResolver = Callable[[Path, str], Path]


@dataclass(frozen=True)
class WorkspaceStageCallbacks:
    """Source callbacks for filesystem staging seams."""

    packaged_script: Callable[[str, Optional[Path]], Optional[Path]]
    resolve_model_source: Callable[[], tuple[Path, str]]
    stage_model_cache: Callable[[Path, tuple[Path, str]], dict]
    ssot_version: Callable[[Path], Optional[str]]
    read_version_stamp: Callable[[Path], Optional[str]]


def _read_installed_version(
    bin_path: Optional[Path],
    run: Runner,
) -> str:
    """Read an installed console version without allowing probe errors to gate."""
    if not bin_path or not bin_path.exists():
        return "unknown"
    try:
        output = run([str(bin_path), "--version"])
    except Exception:
        return "unknown"
    return (output.stdout or output.stderr or "unknown").strip() or "unknown"


def upgrade_uv_tool(run: Runner) -> subprocess.CompletedProcess[str]:
    """Upgrade the uv-managed distribution."""
    return run(["uv", "tool", "upgrade", "brainiac-cli"])


def upgrade_pipx(run: Runner) -> subprocess.CompletedProcess[str]:
    """Upgrade the pipx-managed distribution."""
    return run(["pipx", "upgrade", "brainiac-cli"])


def upgrade_pip_user(
    run: Runner,
    python_executable: str,
) -> subprocess.CompletedProcess[str]:
    """Upgrade the user-site distribution."""
    return run([
        python_executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--upgrade",
        "brainiac-cli[mcp]",
    ])


def upgrade_venv_wheel(
    run: Runner,
    pip_path: Path,
    target: str,
) -> subprocess.CompletedProcess[str]:
    """Upgrade a non-editable wheel inside the legacy venv."""
    return run([str(pip_path), "install", "--upgrade", target])


def upgrade_editable(
    run: Runner,
    pip_path: Path,
    engine_src: Path,
) -> subprocess.CompletedProcess[str]:
    """Upgrade the editable checkout inside the legacy venv."""
    return run([str(pip_path), "install", "--upgrade", "-e", f"{engine_src}[mcp]"])


def refresh_engine_channel(
    engine_src: Optional[Path],
    brainiac_home: Path,
    run: Runner,
    *,
    detect_channel: ChannelDetector,
    venv_bin: VenvBinResolver,
    which_brain: Optional[str],
    python_executable: str,
) -> dict:
    """Select one install channel, then run its upgrade command."""
    legacy_venv_dir = brainiac_home / "venv"
    legacy_bin = venv_bin(legacy_venv_dir, "brain")
    brain_bin: Optional[Path] = legacy_bin if legacy_bin.exists() else (
        Path(which_brain) if which_brain else None
    )
    channel = detect_channel(brain_bin) if brain_bin else CHANNEL_EDITABLE
    old_version = _read_installed_version(brain_bin, run)
    has_checkout = engine_src is not None and (engine_src / "pyproject.toml").exists()

    if channel == CHANNEL_PYPI_UV:
        install_out = upgrade_uv_tool(run)
    elif channel == CHANNEL_PIPX:
        install_out = upgrade_pipx(run)
    elif channel == CHANNEL_PIP_USER:
        install_out = upgrade_pip_user(run, python_executable)
    elif channel == CHANNEL_VENV_WHEEL:
        pip_path = venv_bin(legacy_venv_dir, "pip")
        target = f"{engine_src}[mcp]" if has_checkout else "brainiac-cli[mcp]"
        install_out = upgrade_venv_wheel(run, pip_path, target)
        brain_bin = legacy_bin
    else:
        if not has_checkout:
            return {
                "ok": False,
                "old_version": old_version,
                "new_version": old_version,
                "detail": (
                    "editable-checkout channel but no engine checkout resolved — "
                    "pass --engine-src <clone> or set $BRAINIAC_ENGINE_SRC to a "
                    "checkout with pyproject.toml"
                ),
                "channel": channel,
            }
        pip_path = venv_bin(legacy_venv_dir, "pip")
        if not pip_path.exists():
            run(["python3", "-m", "venv", str(legacy_venv_dir)])
        install_out = upgrade_editable(run, pip_path, engine_src)
        brain_bin = legacy_bin

    ok = install_out.returncode == 0
    new_version = _read_installed_version(brain_bin, run) if ok else old_version
    return {
        "ok": ok,
        "old_version": old_version,
        "new_version": new_version,
        "detail": (install_out.stdout or install_out.stderr or "").strip(),
        "channel": channel,
    }


def stage_engine_and_skills(
    engine_src: Path,
    workspace_path: str,
    *,
    model_source: tuple[Path, str] | None = None,
    callbacks: WorkspaceStageCallbacks,
) -> dict:
    """Stage one Cowork workspace from the current checkout."""
    brain_dir = Path(workspace_path) / ".brain"

    engine_dir = brain_dir / "engine"
    if engine_dir.exists():
        shutil.rmtree(engine_dir)
    engine_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(engine_src / "src" / "brain", engine_dir / "brain")
    for cache_dir in (engine_dir / "brain").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    source = model_source or callbacks.resolve_model_source()
    model_status = callbacks.stage_model_cache(brain_dir, source)

    skills_src_dir = engine_src / "dist" / "cowork-skills"
    skills_dst_dir = brain_dir / "skills"
    skills_dst_dir.mkdir(parents=True, exist_ok=True)
    zips = sorted(skills_src_dir.glob("*.skill")) if skills_src_dir.is_dir() else []
    for bundle in zips:
        shutil.copyfile(bundle, skills_dst_dir / bundle.name)

    vendor_status: dict[str, str] = {}
    try:
        sys.path.insert(0, str(engine_src / "tools"))
        import vendor_semantic_deps as vendor_deps  # type: ignore

        vendor_deps.write_shim(brain_dir)
        vendor_status = vendor_deps.stage_vendor(brain_dir)
    except Exception as exc:
        vendor_status = {"error": f"{type(exc).__name__}: {exc}"}

    prompt_src = engine_src / "docs" / "install" / "cowork-session-prompt.md"
    if prompt_src.exists():
        routines_dst = brain_dir / "routines"
        routines_dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prompt_src, routines_dst / "cowork-session-prompt.md")

    agents_src = engine_src / "AGENTS.md"
    if agents_src.exists():
        shutil.copyfile(agents_src, brain_dir / "AGENTS.md")

    for script_name in ("vm-selftest.sh", "vm-boundary-probe.sh"):
        script_src = callbacks.packaged_script(script_name, engine_src)
        if script_src is not None:
            destination = brain_dir / script_name
            shutil.copyfile(script_src, destination)
            destination.chmod(0o755)

    ssot = callbacks.ssot_version(engine_src)
    staged = callbacks.read_version_stamp(engine_dir / "brain" / "_version.py")
    return {
        "ssot_version": ssot,
        "staged_version": staged,
        "version_ok": ssot is not None and staged == ssot,
        "skills_shipped": len(zips),
        "skills_src_dir": str(skills_src_dir),
        "vendor_status": vendor_status,
        "model_status": model_status,
    }


def _restage_cowork_workspace(
    entry: dict[str, Any],
    engine_src: Path,
    brain_bin: Path,
    run: Runner,
    *,
    stage_workspace: Callable[..., dict],
    resolve_model_source: Callable[[], tuple[Path, str]],
    touch_refreshed: Callable[..., Any],
    model_source: tuple[Path, str] | None,
    model_source_error: str | None,
) -> tuple[dict[str, Any], tuple[Path, str] | None, str | None]:
    """Restage one Cowork workspace and return the cached model resolution."""
    vault_path = entry.get("vault_path", "")
    workspace_path = entry.get("workspace_path", "")
    target = entry.get("target")
    try:
        if model_source is None and model_source_error is None:
            try:
                model_source = resolve_model_source()
            except Exception as exc:
                model_source_error = f"{type(exc).__name__}: {exc}"
        if model_source_error is not None:
            raise RuntimeError(
                f"selected model could not be resolved: {model_source_error}"
            )
        assert model_source is not None
        stage_info = stage_workspace(
            engine_src,
            vault_path,
            model_source=model_source,
        )
    except (OSError, RuntimeError) as exc:
        return (
            {
                "workspace_path": workspace_path,
                "target": target,
                "status": "failed",
                "reason": (
                    "engine/model/skill re-stage failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            },
            model_source,
            model_source_error,
        )
    failure = None
    if not stage_info["version_ok"]:
        failure = (
            f"staged engine version {stage_info['staged_version']!r} != "
            f"SSOT {stage_info['ssot_version']!r} after re-stage — "
            "engine copy landed a missing/stale _version.py stamp"
        )
    elif not stage_info.get("binaries_ok", True):
        failure = stage_info["binaries_detail"]
    if failure is not None:
        return (
            {
                "workspace_path": workspace_path,
                "target": target,
                "status": "failed",
                "reason": failure,
            },
            model_source,
            model_source_error,
        )
    if stage_info["skills_shipped"] == 0:
        return (
            {
                "workspace_path": workspace_path,
                "target": target,
                "status": "failed",
                "reason": (
                    f"no .skill bundles found in {stage_info['skills_src_dir']} "
                    "to refresh — run tools/package_clients.py in the checkout"
                ),
            },
            model_source,
            model_source_error,
        )
    sync_out = run(
        [str(brain_bin), "sync", "--publish"],
        env={**os.environ, "BRAIN_VAULT": vault_path},
    )
    ok = sync_out.returncode == 0
    result = {
        "workspace_path": workspace_path,
        "target": target,
        "status": "ok" if ok else "failed",
        "reason": (sync_out.stdout or sync_out.stderr or "").strip(),
    }
    if ok:
        touch_refreshed(
            vault_path=vault_path,
            workspace_path=workspace_path,
            target=target,
        )
    return result, model_source, model_source_error


def restage_workspaces(
    engine_src: Path,
    brainiac_home: Path,
    run: Runner,
    *,
    stage_workspace: Callable[..., dict],
    resolve_model_source: Callable[[], tuple[Path, str]],
) -> list[dict]:
    """Restage every registered Cowork workspace."""
    sys.path.insert(0, str(engine_src / "tools"))
    import workspace_registry as workspace_registry_module  # type: ignore

    results: list[dict] = []
    model_source: tuple[Path, str] | None = None
    model_source_error: str | None = None
    brain_bin = brainiac_home / "venv" / "bin" / "brain"
    for entry in workspace_registry_module.list_entries():
        vault_path = entry.get("vault_path", "")
        workspace_path = entry.get("workspace_path", "")
        target = entry.get("target")
        import platform
        import socket

        if entry.get("arch") != platform.machine():
            results.append({
                "workspace_path": workspace_path,
                "target": target,
                "status": "skipped",
                "reason": "different arch",
            })
            continue
        if entry.get("host") != socket.gethostname():
            workspace_registry_module.upsert_entry(
                vault_path=vault_path,
                workspace_path=workspace_path,
                target=target,
                model_dir=entry.get("model_dir"),
            )
        if not Path(vault_path or workspace_path).exists():
            results.append({
                "workspace_path": workspace_path,
                "target": target,
                "status": "skipped",
                "reason": "folder missing",
            })
            continue

        if target == "host":
            results.append({
                "workspace_path": workspace_path,
                "target": target,
                "status": "ok",
                "reason": "engine refresh covers host leg",
            })
        elif target == "cowork-vm":
            workspace_result, model_source, model_source_error = (
                _restage_cowork_workspace(
                    entry,
                    engine_src,
                    brain_bin,
                    run,
                    stage_workspace=stage_workspace,
                    resolve_model_source=resolve_model_source,
                    touch_refreshed=workspace_registry_module.touch_refreshed,
                    model_source=model_source,
                    model_source_error=model_source_error,
                )
            )
            results.append(workspace_result)
        else:
            results.append({
                "workspace_path": workspace_path,
                "target": target,
                "status": "skipped",
                "reason": f"unknown target {target!r}",
            })
    return results
