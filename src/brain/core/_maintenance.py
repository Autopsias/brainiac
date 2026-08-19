"""Maintenance orchestration method for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    MaintenanceOrchestratorMixin,
)


class _MaintenanceMixin:
    """Maintenance orchestration method for BrainCore."""

    def maintain(
        self, *, dry_run: bool = False, today: Any = None,
        min_score: float = 0.95, near_dup_k: int = 5,
        graphify_runner: Any = None, golden_runner: Any = None,
    ) -> dict[str, Any]:
        """Delegate the host umbrella to the extracted fold orchestrator.

        ``cos_broker_fold`` and ``cos_ingest_sweep`` remain part of its daily
        intake path; this explicit adapter preserves the public BrainCore seam
        and the source-level scheduler guard while the implementation lives in
        ``brain.folds``.
        """
        return MaintenanceOrchestratorMixin.maintain(
            self, dry_run=dry_run, today=today, min_score=min_score,
            near_dup_k=near_dup_k, graphify_runner=graphify_runner,
            golden_runner=golden_runner,
        )
