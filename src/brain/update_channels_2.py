"""Update execution flow."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .update_channels import Runner


UpdateCallback = Callable[..., Any]


@dataclass(frozen=True)
class UpdateFlowCallbacks:
    """Source callbacks used by the update execution flow."""

    probe_capability: UpdateCallback
    refresh_marketplace: UpdateCallback
    run_doctor: UpdateCallback
    render_human: UpdateCallback
    decide_plugin_action: UpdateCallback
    apply_plugin_action: UpdateCallback
    check_installed_cli_plugins: UpdateCallback
    refresh_engine_venv: UpdateCallback
    rebuild_dist: UpdateCallback
    restage_workspaces: UpdateCallback
    render_before_after: UpdateCallback
    compare: UpdateCallback
    reexec_after_engine_move: UpdateCallback


def _record_doctor_after(
    result: dict[str, Any],
    callbacks: UpdateFlowCallbacks,
    brainiac_home: Path,
    claude_home: Path,
) -> None:
    """Store the post-failure doctor rendering in the update result."""
    result["steps"]["doctor_after"] = callbacks.render_human(
        callbacks.run_doctor(
            brainiac_home=brainiac_home,
            claude_home=claude_home,
        )
    )


def _run_plugin_reinstall(
    before_doctor: dict[str, Any],
    claude_home: Path,
    marketplace_name: str,
    run: Runner,
    *,
    dry_run: bool,
    callbacks: UpdateFlowCallbacks,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the ordered CLI plugin actions selected by the doctor."""
    plugin_rows = [
        row for row in before_doctor["rows"]
        if row["surface"].startswith("Installed CLI plugin")
    ]
    plugin_actions: list[dict[str, Any]] = []
    half_applied: list[dict[str, Any]] = []
    failed_updates: list[dict[str, Any]] = []
    for row in plugin_rows:
        raw = row.get("raw") or {}
        installed = raw.get("installed")
        marketplace = raw.get("marketplace")
        plugin_name = row["surface"].split("(", 1)[1].rstrip(")")
        action = callbacks.decide_plugin_action(installed, marketplace)
        if dry_run:
            applied = {
                "action": action,
                "ok": True,
                "detail": f"[dry-run] would {action}",
            }
        else:
            applied = callbacks.apply_plugin_action(
                action,
                plugin_name,
                marketplace_name,
                run=run,
            )
            if action == "update" and applied.get("ok"):
                refreshed = callbacks.check_installed_cli_plugins(
                    claude_home,
                    "",
                    marketplace_name,
                )
                after_row = next(
                    (
                        refreshed_row for refreshed_row in refreshed
                        if refreshed_row["surface"] == row["surface"]
                    ),
                    None,
                )
                after_version = (after_row or {}).get("raw", {}).get("installed")
                if after_version == installed:
                    applied = {
                        **applied,
                        "ok": False,
                        "detail": (
                            f"plugin {plugin_name} still at {installed} after update — "
                            "the claude plugin CLI no-op'd; run `/plugin update "
                            f"{plugin_name}@{marketplace_name}` manually and restart"
                        ),
                    }
        plugin_actions.append({
            "plugin": plugin_name,
            "installed_before": installed,
            "marketplace": marketplace,
            **applied,
        })
        if applied.get("half_applied"):
            half_applied.append(applied)
        elif action == "update" and not applied.get("ok"):
            failed_updates.append(applied)
    return plugin_actions, half_applied, failed_updates


def _run_engine_refresh(
    engine_src: Optional[Path],
    brainiac_home: Path,
    before_table: dict[str, str],
    run: Runner,
    *,
    dry_run: bool,
    callbacks: UpdateFlowCallbacks,
) -> dict[str, Any]:
    """Refresh the selected engine installation or describe a dry run."""
    if dry_run:
        return {
            "ok": True,
            "old_version": before_table.get("Host engine venv", "unknown"),
            "new_version": "[dry-run] not executed",
            "detail": "[dry-run] pip -e skipped",
        }
    result = callbacks.refresh_engine_venv(
        engine_src,
        brainiac_home,
        run=run,
    )
    callbacks.reexec_after_engine_move(result, dry_run=dry_run)
    return result


def _run_dist_rebuild(
    engine_src: Optional[Path],
    run: Runner,
    *,
    dry_run: bool,
    callbacks: UpdateFlowCallbacks,
) -> tuple[dict[str, Any], bool, str]:
    """Rebuild distributable client bundles when a checkout is available."""
    engine_src_available = (
        engine_src is not None and (engine_src / "pyproject.toml").exists()
    )
    no_checkout_detail = (
        "skipped — no local checkout resolved (tried explicit/$BRAINIAC_ENGINE_SRC/"
        "__file__/marketplace installLocation) (a PyPI-first install "
        "has none by default; only needed to re-stage Cowork workspaces — clone "
        "https://github.com/Autopsias/brainiac.git and pass --engine-src, or set "
        "$BRAINIAC_ENGINE_SRC, if you use Cowork)"
    )
    if dry_run:
        dist_rebuild = {
            "ok": True,
            "detail": "[dry-run] tools/package_clients.py (skipped)",
        }
    elif not engine_src_available:
        dist_rebuild = {
            "ok": True,
            "skipped": True,
            "detail": no_checkout_detail,
        }
    else:
        dist_rebuild = callbacks.rebuild_dist(engine_src, run=run)
    return dist_rebuild, engine_src_available, no_checkout_detail


def _run_workspace_restage(
    engine_src: Optional[Path],
    brainiac_home: Path,
    run: Runner,
    *,
    dry_run: bool,
    engine_src_available: bool,
    no_checkout_detail: str,
    callbacks: UpdateFlowCallbacks,
) -> list[dict[str, Any]]:
    """Restage Cowork workspaces only when the update has a checkout."""
    if dry_run:
        return [{
            "workspace_path": "[dry-run]",
            "target": "n/a",
            "status": "skipped",
            "reason": "dry-run: no re-stage executed",
        }]
    if not engine_src_available:
        return [{
            "workspace_path": "n/a",
            "target": "n/a",
            "status": "skipped",
            "reason": no_checkout_detail,
        }]
    return callbacks.restage_workspaces(
        engine_src,
        brainiac_home,
        run=run,
    )


def _finalize_update(
    result: dict[str, Any],
    before_table: dict[str, str],
    after_doctor: dict[str, Any],
    callbacks: UpdateFlowCallbacks,
) -> None:
    """Build the final before/after table and completion note."""
    result["steps"]["doctor_after"] = callbacks.render_human(after_doctor)
    ssot_after = after_doctor.get("ssot_version") or "0.0.0"
    desktop_stale = any(
        row["surface"].startswith("Desktop/Cowork plugin store")
        and (row.get("raw") or {}).get("version")
        and callbacks.compare(str((row.get("raw") or {})["version"]), ssot_after) < 0
        for row in after_doctor["rows"]
    )
    if desktop_stale:
        result["residual_human_steps"].append(
            "Desktop/Cowork plugin store is STALE and has no external CLI: in a "
            "Cowork session use /skill-creator to repackage + Save-and-Replace the "
            "stale skill(s) — /brainiac-update is host-only and refuses in Cowork — "
            "then re-run `brain doctor` on the host to confirm it took."
        )

    table: list[dict[str, str]] = []
    surfaces = sorted(set(before_table) | {
        row["surface"] for row in after_doctor["rows"]
    })
    for surface in surfaces:
        after_value = next(
            (
                row["detail"] for row in after_doctor["rows"]
                if row["surface"] == surface
            ),
            "n/a",
        )
        table.append({
            "surface": surface,
            "before": before_table.get(surface, "n/a"),
            "after": after_value,
        })
    result["before_after"] = table
    result["before_after_rendered"] = callbacks.render_before_after(table)
    result["ok"] = after_doctor["ok"]
    result["notes"] = (
        "update complete, all required surfaces current"
        if after_doctor["ok"]
        else (
            f"update ran to completion but {after_doctor['stale_count']} required "
            "surface(s) still stale — see brain doctor output above"
        )
    )


def _prepare_update(
    marketplace_name: str,
    brainiac_home: Path,
    claude_home: Path,
    run: Runner,
    *,
    skip_capability_probe: bool,
    dry_run: bool,
    callbacks: UpdateFlowCallbacks,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, str] | None]:
    """Run the guarded marketplace refresh and capture the before state."""
    result: dict[str, Any] = {
        "ok": False,
        "steps": {},
        "before_after": [],
        "residual_human_steps": [],
    }
    if skip_capability_probe:
        probe = {"ok": True, "reason": "probe skipped by caller", "manual_commands": []}
    else:
        probe = callbacks.probe_capability(run=run)
    result["steps"]["capability_probe"] = probe
    if not probe["ok"]:
        result["notes"] = (
            "BLOCKED before any destructive call: "
            + probe["reason"]
            + ". Manual commands: "
            + "; ".join(probe["manual_commands"])
        )
        return result, None, None

    if dry_run:
        marketplace_result = {
            "ok": True,
            "detail": (
                "[dry-run] claude plugin marketplace update "
                f"{marketplace_name} (skipped)"
            ),
        }
    else:
        marketplace_result = callbacks.refresh_marketplace(marketplace_name, run=run)
    result["steps"]["marketplace_refresh"] = marketplace_result
    if not marketplace_result["ok"]:
        result["notes"] = (
            f"marketplace refresh failed: {marketplace_result['detail']} — "
            "stopping before any plugin mutation."
        )
        return result, None, None

    before_doctor = callbacks.run_doctor(
        brainiac_home=brainiac_home,
        claude_home=claude_home,
    )
    before_table = {row["surface"]: row["detail"] for row in before_doctor["rows"]}
    result["steps"]["doctor_before"] = callbacks.render_human(before_doctor)
    return result, before_doctor, before_table


def _apply_plugin_actions(
    result: dict[str, Any],
    before_doctor: dict[str, Any],
    claude_home: Path,
    marketplace_name: str,
    brainiac_home: Path,
    run: Runner,
    *,
    dry_run: bool,
    callbacks: UpdateFlowCallbacks,
) -> bool:
    """Apply plugin actions and record a post-failure doctor report."""
    plugin_actions, half_applied, failed_updates = _run_plugin_reinstall(
        before_doctor,
        claude_home,
        marketplace_name,
        run,
        dry_run=dry_run,
        callbacks=callbacks,
    )
    result["steps"]["plugin_reinstall"] = plugin_actions
    if half_applied:
        result["notes"] = (
            "update INCOMPLETE — surfaces at mixed versions: a reinstall "
            "half-applied (uninstall ok, install failed). Recovery: "
            + "; ".join(item["recovery_command"] for item in half_applied)
        )
        _record_doctor_after(result, callbacks, brainiac_home, claude_home)
        return False
    if failed_updates:
        result["notes"] = (
            "update INCOMPLETE — plugin update no-op'd: "
            + "; ".join(item["detail"] for item in failed_updates)
        )
        _record_doctor_after(result, callbacks, brainiac_home, claude_home)
        return False
    return True


def _run_session_hook(
    engine_src: Optional[Path], claude_home: Path, *, dry_run: bool,
) -> dict[str, Any]:
    """Re-place and re-register the SessionStart alert hook on every update.

    It rides the update rather than only the install for the reason every
    other staging leg here does: the hook is a thin caller whose CONTENT this
    engine owns, and a host carrying an older copy has no other way to be
    fixed. Hosts updating from before 0.20.25 carry the pre-0.20.7 INLINE
    implementation of the whole digest — one that reads notify markers and so
    reports findings that cleared two days ago.

    Never fatal to the update: a failed hook refresh costs a banner, while
    stopping here would cost the engine refresh that already succeeded."""
    if dry_run:
        return {"ok": True, "script": "skipped", "settings": "skipped",
                "detail": "dry run"}
    try:
        from . import session_hook
        from .update import _packaged_script

        return session_hook.install(
            claude_home, _packaged_script(session_hook.HOOK_SCRIPT, engine_src))
    except Exception as exc:  # noqa: BLE001 — a banner must never fail an update
        return {"ok": False, "script": "error", "settings": "skipped",
                "detail": f"{type(exc).__name__}: {exc}"}


def run_update_flow(
    *,
    marketplace_name: str,
    engine_src: Optional[Path],
    brainiac_home: Path,
    claude_home: Path,
    run: Runner,
    skip_capability_probe: bool,
    dry_run: bool,
    callbacks: UpdateFlowCallbacks,
) -> dict[str, Any]:
    """Execute the ordered update steps with injected source callbacks."""
    result, before_doctor, before_table = _prepare_update(
        marketplace_name,
        brainiac_home,
        claude_home,
        run,
        skip_capability_probe=skip_capability_probe,
        dry_run=dry_run,
        callbacks=callbacks,
    )
    if before_doctor is None or before_table is None:
        return result
    if not _apply_plugin_actions(
        result,
        before_doctor,
        claude_home,
        marketplace_name,
        brainiac_home,
        run,
        dry_run=dry_run,
        callbacks=callbacks,
    ):
        return result

    engine_result = _run_engine_refresh(
        engine_src,
        brainiac_home,
        before_table,
        run,
        dry_run=dry_run,
        callbacks=callbacks,
    )
    result["steps"]["engine_refresh"] = engine_result
    if not engine_result["ok"] and not dry_run:
        result["notes"] = (
            f"engine venv refresh failed: {engine_result['detail']} — "
            "stopping before workspace re-stage."
        )
        return result

    dist_rebuild, engine_src_available, no_checkout_detail = _run_dist_rebuild(
        engine_src,
        run,
        dry_run=dry_run,
        callbacks=callbacks,
    )
    result["steps"]["dist_rebuild"] = dist_rebuild
    if not dist_rebuild["ok"] and not dry_run:
        result["notes"] = (
            f"dist rebuild failed: {dist_rebuild['detail']} — "
            "stopping before workspace re-stage."
        )
        return result

    workspace_results = _run_workspace_restage(
        engine_src,
        brainiac_home,
        run,
        dry_run=dry_run,
        engine_src_available=engine_src_available,
        no_checkout_detail=no_checkout_detail,
        callbacks=callbacks,
    )
    result["steps"]["workspace_restage"] = workspace_results
    result["steps"]["session_hook"] = _run_session_hook(
        engine_src, claude_home, dry_run=dry_run
    )

    after_doctor = callbacks.run_doctor(
        brainiac_home=brainiac_home,
        claude_home=claude_home,
    )
    _finalize_update(
        result,
        before_table,
        after_doctor,
        callbacks,
    )
    return result
