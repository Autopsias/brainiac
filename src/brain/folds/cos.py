"""Broker COS proposals during maintenance."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from .context import MaintenanceRun
from .. import cos, cos_runverify, maintenance


def _broker_stages(
    core: Any, now: dt.datetime
) -> tuple[tuple[str, Callable[[], Any]], ...]:
    """Return the load-bearing COS broker stage order."""

    def expire_batches() -> list[str]:
        expired = cos.expire_batches(core.vault, now)
        cos.close_expired_batch_questions(core, expired)
        return expired

    return (
        (
            "run_validity",
            lambda: cos_runverify.verify_pending_runs(core.vault, now=now),
        ),
        ("claimed", lambda: cos.claim_drops(core.vault, now)),
        ("batch_expired", expire_batches),
        ("proposals_expired", lambda: cos.expire_proposals(core.vault, now)),
        ("consumed", lambda: cos.consume_answers(core, now)),
        ("holds_released", lambda: cos.hold_release_due(core.vault, now)),
        ("corrections_asked", lambda: cos.enqueue_correction_questions(core, now)),
        ("auto_captured", lambda: cos.auto_capture_fold(core.vault, now)),
        ("version_links", lambda: cos.version_link_fold(core, now)),
        ("batch", lambda: cos.enqueue_batch(core, now)),
        ("gc", lambda: cos.gc_compact(core.vault, now)),
        ("spine_rendered", lambda: core.cos_spine_render(now=now)),
        ("grounding_pack", lambda: core.cos_grounding_pack(now=now)),
    )


def _record_broker_claims(run: MaintenanceRun, result: dict[str, Any]) -> None:
    """Record claimed drops and a newly enqueued owner batch."""
    if result.get("claimed", {}).get("claimed"):
        run.auto_fixed.append(
            maintenance.auto_fixed_item(
                "cos-broker",
                str(run.core.vault),
                f"claimed {len(result['claimed']['claimed'])} COS proposal drop(s) "
                "for owner review",
            )
        )
    if result.get("batch", {}).get("enqueued"):
        run.auto_fixed.append(
            maintenance.auto_fixed_item(
                "cos-broker",
                "owner inbox",
                f"queued COS ingestion batch {result['batch']['batch_id']} "
                f"({len(result['batch']['candidates'])} candidate(s))",
            )
        )


def _record_broker_validity(run: MaintenanceRun, result: dict[str, Any]) -> None:
    """Log newly unclaimable COS runs once by run-verdict identity."""
    invalid = [
        score
        for score in (result.get("run_validity") or {}).get("scored", [])
        if score.get("verdict") not in cos.CLAIMABLE_VERDICTS
    ]
    if not invalid:
        return
    run.core._append_hot_once(
        "maintain:cos-run-invalid:"
        + ",".join(
            f"{score['run_id']}={score['verdict']}"
            for score in sorted(invalid, key=lambda item: item["run_id"])
        ),
        cos_runverify.hot_entry(invalid, run.date.isoformat()),
    )


def _record_broker_waiting(run: MaintenanceRun, result: dict[str, Any]) -> None:
    """Log owner-batch backpressure once per distinct waiting-id set."""
    waiting = (result.get("batch", {}) or {}).get("waiting") or []
    if not waiting:
        return
    run.core._append_hot_once(
        "maintain:cos-broker-waiting:"
        + maintenance.promote_scan_finding_key([{"id": item} for item in waiting]),
        maintenance.render_cos_waiting_hot_entry(waiting, run.date),
    )


def _record_broker_consumed(run: MaintenanceRun, result: dict[str, Any]) -> None:
    """Record accepted captures and owner-approved version links separately."""
    consumed = result.get("consumed", {}) or {}
    applied_links = consumed.get("supersedes_applied") or []
    captured = len(consumed.get("accepted") or []) - len(applied_links)
    if captured > 0:
        run.auto_fixed.append(
            maintenance.auto_fixed_item(
                "cos-broker",
                str(cos.approved_queue_dir(run.core.vault)),
                f"moved {captured} owner-accepted candidate(s) into the host-only "
                "approved queue for signing",
            )
        )
    if applied_links:
        run.auto_fixed.append(
            maintenance.auto_fixed_item(
                "version-link",
                str(run.core.vault),
                f"applied {len(applied_links)} owner-accepted supersede proposal(s) "
                "deduced from email context",
            )
        )


def _record_broker_state(run: MaintenanceRun, result: dict[str, Any]) -> None:
    """Persist curated coverage and report due hold releases."""
    coverage = (result.get("version_links") or {}).get("coverage")
    if isinstance(coverage, dict):
        previous = (
            run.state.get("daily") if isinstance(run.state.get("daily"), dict) else {}
        )
        run.state["daily"] = {**previous, "curated_coverage": dict(coverage)}
    if result.get("holds_released"):
        run.auto_fixed.append(
            maintenance.auto_fixed_item(
                "cos-broker",
                str(cos.approved_queue_dir(run.core.vault)),
                f"released {len(result['holds_released'])} due auto-capture hold(s)",
            )
        )
    for error in result.get("errors", []):
        run.blocked.append(
            maintenance.blocked_item(
                f"COS broker stage failed: {error}",
                "cos ops dir / owner inbox / signing key",
                "next maintain run",
            )
        )


class CosFoldsMixin:
    """Provide BrainCore's COS maintenance folds."""

    def cos_broker_fold(self, *, today: Any = None) -> dict[str, Any]:
        """Run the broker's stage-isolated CUT-01E sequence."""
        self._require_host("run the COS broker")
        now = (
            dt.datetime.combine(today, dt.time(3, 0), tzinfo=dt.timezone.utc)
            if isinstance(today, dt.date) and not isinstance(today, dt.datetime)
            else (today or dt.datetime.now(dt.timezone.utc))
        )
        report: dict[str, Any] = {"ritual": "cos-broker", "errors": []}
        cos.ensure_layout(self.vault)
        for stage, function in _broker_stages(self, now):
            try:
                report[stage] = function()
            except Exception as exc:
                report["errors"].append(f"{stage}: {type(exc).__name__}: {exc}")
        return report

    def cos_broker_summary_fold(self, run: MaintenanceRun) -> None:
        """Run CUT-01E before sync and project its result into maintain outcomes."""
        try:
            result = self.cos_broker_fold(today=run.date)
            run.results["cos_broker"] = result
            _record_broker_claims(run, result)
            _record_broker_validity(run, result)
            _record_broker_waiting(run, result)
            _record_broker_consumed(run, result)
            _record_broker_state(run, result)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"COS broker fold failed: {exc}",
                    "cos ops dir",
                    "next maintain run",
                )
            )
