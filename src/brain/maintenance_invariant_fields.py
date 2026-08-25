"""Health-history fields owned by the corpus-invariants fold."""
from __future__ import annotations

from typing import Any


def invariant_health_history_fields(
    values: dict[str, Any], metrics: dict[str, Any], age_days: int | None,
) -> dict[str, Any]:
    """Build the invariant fields in their persisted health-record order."""
    return {
        "invariant_unlinked_sources": values.get("unlinked_sources"),
        "invariant_cross_tier_twins": values.get("cross_tier_twins"),
        "invariant_cross_tier_duplicates": values.get("cross_tier_duplicates"),
        "invariant_cross_tier_candidates": values.get("cross_tier_candidates"),
        "invariant_unguarded_ingests": values.get("unguarded_ingests"),
        "invariant_ingest_guard_raises": (
            (metrics.get("unguarded_ingests") or {}).get("raised")
            if isinstance(metrics.get("unguarded_ingests"), dict) else None),
        "invariant_subfloor_families": values.get("subfloor_families"),
        "invariant_unreachable_gold": values.get("unreachable_gold"),
        "invariant_unsigned_notes": values.get("unsigned_notes"),
        # DLV-05's three shelf counters. The record is a fixed dict literal, so
        # a metric not named here never persists and never trends.
        "invariant_unshelved_deliverables": values.get("unshelved_deliverables"),
        "invariant_stale_shelf_entries": values.get("stale_shelf_entries"),
        "invariant_unanchored_deliverable_payloads": values.get(
            "unanchored_deliverable_payloads"),
        "invariant_age_days": age_days,
    }
