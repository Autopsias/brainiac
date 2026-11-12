"""Failure-row helpers for the workspace re-stage, split out of ``update_channels``."""

from __future__ import annotations

from typing import Any


def _failed(workspace_path: str, target: Any, reason: str) -> dict:
    """One shape for every failure row this restage can produce."""
    return {"workspace_path": workspace_path, "target": target,
            "status": "failed", "reason": reason}


def _stage_failure(stage_info: dict) -> str | None:
    """Reason a re-stage landed wrong, or None when it is sound."""
    if not stage_info["version_ok"]:
        return (
            f"staged engine version {stage_info['staged_version']!r} != "
            f"SSOT {stage_info['ssot_version']!r} after re-stage — "
            "engine copy landed a missing/stale _version.py stamp"
        )
    if not stage_info.get("binaries_ok", True):
        return stage_info["binaries_detail"]
    if stage_info["skills_shipped"] == 0:
        return (f"no .skill bundles found in {stage_info['skills_src_dir']} "
                "to refresh — run tools/package_clients.py in the checkout")
    return None
