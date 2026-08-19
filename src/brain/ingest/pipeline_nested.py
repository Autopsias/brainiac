"""Expand bounded nested ingest candidates."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import handlers as H


def process_nested(
    nested: list[dict[str, Any]],
    *,
    parent_slug: str,
    depth: int,
    budget: dict[str, int],
    drain: Any,
    provenance: dict[str, Any] | None = None,
) -> None:
    """Re-enter admission for each safe synthetic nested candidate."""
    from . import pipeline as facade
    from . import pipeline_stages as stages

    if not nested:
        return
    if depth >= facade.MAX_NESTED_DEPTH:
        for item in nested:
            drain.report["skipped"].append({
                "file": H.strip_control_chars(item.get("name") or "?"),
                "reason": "nested_depth_exceeded",
                "parent": parent_slug,
            })
        return
    for idx, item in enumerate(nested):
        name = H.strip_control_chars(item.get("name") or f"member-{idx}")
        data = item.get("data", b"")
        if (
            budget["items"] >= facade.MAX_TOTAL_NESTED_ITEMS
            or budget["bytes"] + len(data) > facade.MAX_TOTAL_NESTED_BYTES
        ):
            drain.report["quarantined"].append({
                "file": name,
                "reason": "nested_budget_exceeded",
                "parent": parent_slug,
            })
            continue
        budget["items"] += 1
        budget["bytes"] += len(data)
        extension = Path(name).suffix.lower()
        safe_stem = facade._slugify_stem(Path(name).stem)
        synthetic_name = f"{parent_slug}-nested-{idx}-{safe_stem}{extension}"
        temp_path = facade._unique_dest(drain.processing_dir, synthetic_name)
        try:
            drain.processing_dir.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(data)
        except OSError as exc:
            drain.report["skipped"].append({
                "file": name,
                "reason": f"nested_write_error:{type(exc).__name__}",
                "parent": parent_slug,
            })
            continue
        record = stages.ClaimRecord(
            drain=drain,
            path=temp_path,
            orig_name=name,
            claimed=temp_path,
            original_bytes=data,
            original_sha=facade._sha256_bytes(data),
            provenance=provenance,
            depth=depth + 1,
            budget=budget,
            parent=parent_slug,
        )
        try:
            stages.process_verified_claim(record)
        except Exception as exc:
            if facade._is_systemic_error(exc):
                raise
            reason = "nested_processing_error"
            if temp_path.exists():
                facade._quarantine(
                    temp_path,
                    drain.quarantine_dir,
                    reason,
                    [f"{type(exc).__name__}: {exc}"],
                )
            drain.report["quarantined"].append({
                "file": name,
                "reason": reason,
                "parent": parent_slug,
            })
