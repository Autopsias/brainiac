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
        "invariant_age_days": age_days,
    }
