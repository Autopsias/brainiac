"""Host doctor row orchestration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


DoctorCheck = Callable[..., Any]


@dataclass(frozen=True)
class DoctorCheckContext:
    """Inputs shared by the ordered host doctor checks."""

    repo_root: Path
    brainiac_home: Path
    claude_home: Path
    app_support_dir: Path
    registry_entries: list[dict]
    marketplace_dir: Path
    marketplace_name: str
    registry_fetch: Optional[Callable[[], Optional[dict]]]
    vault: Optional[str | os.PathLike[str]]
    resolved_brain: Optional[Path]
    is_dev_checkout: bool
    schema_version: int
    engine_version: str
    registry_unavailable: bool
    vm_python: str


@dataclass(frozen=True)
class DoctorChecks:
    """Source-module callbacks used by the ordered check groups."""

    row: DoctorCheck
    ssot_version: DoctorCheck
    committed_stamp: DoctorCheck
    host_venv: DoctorCheck
    embedder_liveness: DoctorCheck
    dist_compat: DoctorCheck
    plugin_manifests: DoctorCheck
    installed_cli_plugins: DoctorCheck
    stale_name_plugins: DoctorCheck
    staged_workspaces: DoctorCheck
    vendor_abi: DoctorCheck
    staged_skill_bundles: DoctorCheck
    workspace_schema: DoctorCheck
    maintain_heartbeat: DoctorCheck
    marketplace_cache: DoctorCheck
    vault_root: DoctorCheck
    query_capture: DoctorCheck
    audit_content_drift: DoctorCheck
    corpus_invariants: DoctorCheck
    pypi_registry_drift: DoctorCheck
    mcpb_desktop_collision: DoctorCheck
    mcp_vault_paths: DoctorCheck
    cos_deployed_skill: DoctorCheck
    desktop_plugin_store: DoctorCheck
    cowork_vault_dir: DoctorCheck
    quarantine: DoctorCheck
    ingest_capability: DoctorCheck
    unreadable_registry_row: DoctorCheck


def _version_rows(
    context: DoctorCheckContext,
    checks: DoctorChecks,
) -> tuple[list[dict], str]:
    """Build the SSOT row and settle the comparison version."""
    ssot = checks.ssot_version(context.repo_root) if context.is_dev_checkout else None
    if not context.is_dev_checkout:
        rows = [checks.row(
            "Version SSOT (pyproject.toml)",
            "not-detectable",
            f"no dev checkout at {context.repo_root} — installed engine "
            f"(running {context.engine_version}); repo drift surfaces skipped",
        )]
        return rows, context.engine_version or "0.0.0"
    if ssot is None:
        return [checks.row(
            "Version SSOT (pyproject.toml)",
            "unknown",
            "no version found in pyproject.toml",
        )], "0.0.0"
    return [checks.row(
        "Version SSOT (pyproject.toml)",
        "current",
        ssot,
        raw={"version": ssot},
    )], ssot


def _host_rows(
    context: DoctorCheckContext,
    checks: DoctorChecks,
    ssot: str,
) -> list[dict]:
    """Build version, plugin, and engine rows in their contract order."""
    rows: list[dict] = []
    if context.is_dev_checkout:
        rows.append(checks.committed_stamp(context.repo_root, ssot))
    rows.append(checks.host_venv(
        context.brainiac_home,
        ssot,
        resolved_brain=context.resolved_brain,
    ))
    rows.append(checks.embedder_liveness())
    if context.is_dev_checkout:
        rows.append(checks.dist_compat(context.repo_root, ssot))
        rows.extend(checks.plugin_manifests(context.repo_root, ssot))
    rows.extend(checks.installed_cli_plugins(
        context.claude_home,
        ssot,
        context.marketplace_name,
        marketplace_dir=context.marketplace_dir,
    ))
    rows.extend(checks.stale_name_plugins(context.claude_home))
    return rows


def _workspace_rows(
    context: DoctorCheckContext,
    checks: DoctorChecks,
    ssot: str,
) -> list[dict]:
    """Build workspace and staged-asset rows in their contract order."""
    rows: list[dict] = []
    if context.registry_unavailable:
        rows.append(checks.unreadable_registry_row(context.repo_root))
    rows.extend(checks.staged_workspaces(context.registry_entries, ssot))
    for entry in context.registry_entries:
        if entry.get("target") == "host":
            continue
        vdir = Path(checks.cowork_vault_dir(entry)) / ".brain" / "vendor"
        if vdir.is_dir():
            rows.append(checks.vendor_abi(vdir, context.vm_python))
    rows.extend(checks.staged_skill_bundles(context.registry_entries, ssot))
    rows.extend(checks.workspace_schema(context.registry_entries, context.schema_version))
    for entry in context.registry_entries:
        if entry.get("target") != "host":
            continue
        vault_path = entry.get("vault_path")
        if vault_path:
            rows.append(checks.maintain_heartbeat(Path(vault_path)))
    rows.append(checks.marketplace_cache(context.marketplace_dir))
    return rows


def _capture_rows(
    context: DoctorCheckContext,
    checks: DoctorChecks,
) -> list[dict]:
    """Build per-vault capture, audit, and invariant rows."""
    capture_vaults: list[Path] = []
    raw_vaults: list[Any] = [context.vault] if context.vault is not None else [
        entry.get("vault_path") for entry in context.registry_entries
        if entry.get("target") == "host" and entry.get("vault_path")
    ]
    if context.vault is None:
        raw_vaults.append(None)
    for raw_vault in raw_vaults:
        try:
            resolved = checks.vault_root(raw_vault)
        except Exception:
            continue
        if resolved not in capture_vaults:
            capture_vaults.append(resolved)
    rows: list[dict] = []
    for capture_vault in capture_vaults:
        rows.append(checks.query_capture(capture_vault))
        rows.append(checks.audit_content_drift(capture_vault))
        rows.append(checks.corpus_invariants(capture_vault))
        # 2026-08-17: a document dropped in and refused is invisible from
        # every other surface until the month turns. Per vault, and gating.
        rows.append(checks.quarantine(capture_vault))
    # Host capability, not per-vault: one engine reads for every vault, and a
    # missing OCR engine silently refuses every scan dropped into any of them.
    rows.append(checks.ingest_capability())
    return rows


def _terminal_rows(
    context: DoctorCheckContext,
    checks: DoctorChecks,
    ssot: str,
) -> list[dict]:
    """Build optional registry and final desktop rows."""
    rows: list[dict] = []
    if context.registry_fetch is not None:
        rows.append(checks.pypi_registry_drift(
            context.repo_root,
            ssot,
            fetch=context.registry_fetch,
        ))
    rows.extend([checks.mcpb_desktop_collision(context.app_support_dir),
                 *checks.mcp_vault_paths()])
    rows.append(checks.cos_deployed_skill())
    rows.extend(checks.desktop_plugin_store(context.app_support_dir, ssot))
    return rows


def build_doctor_rows(
    context: DoctorCheckContext,
    checks: DoctorChecks,
) -> tuple[str, list[dict]]:
    """Build host doctor rows while keeping the established row order."""
    rows, ssot = _version_rows(context, checks)
    rows.extend(_host_rows(context, checks, ssot))
    rows.extend(_workspace_rows(context, checks, ssot))
    rows.extend(_capture_rows(context, checks))
    rows.extend(_terminal_rows(context, checks, ssot))
    return ssot, rows
