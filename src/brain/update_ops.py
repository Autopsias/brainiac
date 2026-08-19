"""Apply the scheduled engine auto-update policy."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import config
from . import update as brain_update
from .lock import WriterLockBusy, vault_writer_lock


def _auto_update_gate() -> dict[str, Any] | None:
    """Return a disabled result unless scheduled auto-update is explicitly armed."""
    if os.environ.get("BRAIN_NO_AUTO_UPDATE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {"auto_update": "disabled", "reason": "BRAIN_NO_AUTO_UPDATE set"}
    if os.environ.get("BRAIN_AUTO_UPDATE") != "1":
        return {
            "auto_update": "disabled",
            "reason": "BRAIN_AUTO_UPDATE!=1 (scheduled-task only)",
        }
    if config.is_managed():
        return {"auto_update": "skipped", "reason": "managed endpoint"}
    return None


def _clear_moot_failure(
    brainiac_home: Path, previous: dict[str, Any] | None, installed: Any
) -> dict[str, Any] | None:
    """Remove a failed-version banner once the machine has reached that version."""
    if not (
        previous
        and previous.get("status") == "failed"
        and brain_update.failure_is_moot(previous, installed)
    ):
        return previous
    try:
        brain_update.update_state_path(brainiac_home).unlink()
    except FileNotFoundError:
        pass
    return None


def _clear_stale_available(
    brainiac_home: Path, previous: dict[str, Any] | None
) -> None:
    """Remove an availability banner once no newer version remains available."""
    if not previous or previous.get("status") != "available":
        return
    try:
        brain_update.update_state_path(brainiac_home).unlink()
    except FileNotFoundError:
        pass


def _defer_for_writer(
    core: Any,
    *,
    brainiac_home: Path,
    installed: Any,
    latest: Any,
    source: Any,
    today: Any,
) -> dict[str, Any] | None:
    """Defer an update cleanly while another host writer owns the index lock."""
    try:
        with vault_writer_lock(core.vault, verb="update-probe", timeout=0.1):
            pass
    except WriterLockBusy as exc:
        brain_update.write_update_state(
            brainiac_home,
            status="available",
            installed=installed,
            latest=latest,
            source=source,
            at=today.isoformat(),
            detail=f"deferred — writer busy (pid={exc.holder.get('pid')})",
        )
        return {
            "auto_update": "deferred",
            "reason": "writer busy",
            "latest": latest,
        }
    return None


def _record_update_report(
    brainiac_home: Path,
    *,
    report: dict[str, Any],
    installed: Any,
    latest: Any,
    source: Any,
    today: Any,
) -> dict[str, Any]:
    """Persist the verified update outcome for the session-start banner."""
    if report.get("ok"):
        brain_update.write_update_state(
            brainiac_home,
            status="applied",
            installed=installed,
            latest=latest,
            source=source,
            at=today.isoformat(),
            detail="auto-applied by brain-nightly",
        )
        return {"auto_update": "applied", "latest": latest}
    brain_update.write_update_state(
        brainiac_home,
        status="failed",
        installed=installed,
        latest=latest,
        source=source,
        at=today.isoformat(),
        detail=(report.get("notes") or "update pipeline reported not-ok")[:200],
    )
    return {
        "auto_update": "failed",
        "latest": latest,
        "notes": report.get("notes"),
    }


class UpdateOpsMixin:
    """Provide BrainCore's scheduled auto-update operation."""

    def _maybe_auto_update(self, today: Any) -> dict[str, Any]:
        """Apply a newer version once per release under the nightly opt-in.

        Owner decision 2026-07-25: the scheduler must explicitly arm this path;
        managed endpoints and interactive maintenance remain non-mutating.
        """
        gated = _auto_update_gate()
        if gated is not None:
            return gated
        brainiac_home = Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
        info = brain_update.detect_and_check_update(brainiac_home)
        installed = info.get("installed")
        latest = info.get("latest")
        previous = _clear_moot_failure(
            brainiac_home, brain_update.read_update_state(brainiac_home), installed
        )
        if not info.get("available"):
            _clear_stale_available(brainiac_home, previous)
            return {
                "auto_update": "none",
                "installed": installed,
                "latest": latest,
            }
        if (
            previous
            and previous.get("status") == "failed"
            and previous.get("latest") == latest
        ):
            return {
                "auto_update": "skipped",
                "reason": "version already failed",
                "latest": latest,
            }
        deferred = _defer_for_writer(
            self,
            brainiac_home=brainiac_home,
            installed=installed,
            latest=latest,
            source=info.get("source"),
            today=today,
        )
        if deferred is not None:
            return deferred
        report = brain_update.run_update(brainiac_home=brainiac_home)
        return _record_update_report(
            brainiac_home,
            report=report,
            installed=installed,
            latest=latest,
            source=info.get("source"),
            today=today,
        )
