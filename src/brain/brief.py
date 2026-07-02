"""Morning brief + weekly digest generators (UX-02).

Pure functions — no I/O. The caller (BrainCore) passes pre-collected data;
these assemble and format the output.

The scheduled morning brief is the ONE sanctioned scheduled task and the
guaranteed daily drain FLOOR. The tripwire line surfaces a stalled drain so
it is visible next morning rather than silently losing notes.
"""
from __future__ import annotations

import datetime
from typing import Any


def _today() -> str:
    return datetime.date.today().isoformat()


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def build_brief(
    *,
    index_stats: dict[str, Any],
    recent_notes: list[dict[str, Any]],
    pending_before_drain: int,
    drain_result: dict[str, Any],
    snapshot_age_hours: float | None,
    max_recent: int = 5,
) -> dict[str, Any]:
    """Build the morning brief data structure.

    Tripwire logic:
    - ``pending_before_drain > 0`` AND ``drain_result.promoted == 0``
      AND ``drain_result.skipped > 0`` → stalled drain: emit tripwire line.
    - ``drain_result.promoted > 0`` → drain ran successfully, tripwire cleared.
    - ``pending_before_drain == 0`` → nothing to drain, clean.
    """
    drain_promoted = int(drain_result.get("promoted", 0))
    drain_skipped = int(drain_result.get("skipped", 0))
    drain_stalled = (
        pending_before_drain > 0 and drain_promoted == 0 and drain_skipped > 0
    )

    snap_age: str | None = None
    if snapshot_age_hours is not None:
        if snapshot_age_hours < 1:
            snap_age = f"{int(snapshot_age_hours * 60)}m"
        elif snapshot_age_hours < 24:
            snap_age = f"{snapshot_age_hours:.1f}h"
        else:
            snap_age = f"{snapshot_age_hours / 24:.1f}d"

    tripwire: str | None = None
    drain_note: str | None = None
    if drain_stalled:
        tripwire = (
            f"{pending_before_drain} captures pending · "
            "last successful drain: stalled (no key?)"
        )
    elif drain_promoted > 0:
        drain_note = f"drained {drain_promoted} capture(s)"

    return {
        "date": _today(),
        "notes": int(index_stats.get("notes", 0)),
        "chunks": int(index_stats.get("chunks", 0)),
        "pending_before_drain": pending_before_drain,
        "drain": {
            "promoted": drain_promoted,
            "skipped": drain_skipped,
            "stalled": drain_stalled,
        },
        "snapshot_age": snap_age,
        "recent": recent_notes[:max_recent],
        "tripwire": tripwire,
        "drain_note": drain_note,
    }


def format_brief(brief: dict[str, Any]) -> str:
    """Human-readable morning brief. Quiet — no plumbing noise."""
    lines = [f"brain brief · {brief['date']}"]
    lines.append(f"  {brief['notes']} notes  {brief['chunks']} chunks")

    tw = brief.get("tripwire")
    dn = brief.get("drain_note")
    if tw:
        lines.append(f"  ⚠ {tw}")
    elif dn:
        lines.append(f"  ✓ {dn}")
    elif brief.get("pending_before_drain", 0) == 0:
        lines.append("  ✓ no pending captures")

    snap = brief.get("snapshot_age")
    if snap:
        lines.append(f"  snapshot age: {snap}")

    if brief.get("recent"):
        lines.append("  recent:")
        for n in brief["recent"]:
            lines.append(
                f"    {str(n.get('updated', ''))[:10]}  {n.get('id', '')}  "
                f"({n.get('classification') or 'UNLABELLED'})"
            )

    return "\n".join(lines)


def build_digest(
    *,
    index_stats: dict[str, Any],
    recent_notes: list[dict[str, Any]],
    days: int = 7,
) -> dict[str, Any]:
    """Build the weekly digest data structure."""
    cutoff = _days_ago(days)
    in_period = [n for n in recent_notes if str(n.get("updated") or "") >= cutoff]
    return {
        "date": _today(),
        "period_days": days,
        "period_start": cutoff,
        "notes_total": int(index_stats.get("notes", 0)),
        "notes_in_period": len(in_period),
        "notes": in_period[:20],
    }


def format_digest(digest: dict[str, Any]) -> str:
    """Human-readable weekly digest. Quiet."""
    lines = [
        f"brain digest · {digest['date']} (past {digest['period_days']}d)",
        f"  {digest['notes_total']} notes total  "
        f"  {digest['notes_in_period']} in period",
    ]
    if digest.get("notes"):
        lines.append("  added/updated:")
        for n in digest["notes"]:
            lines.append(
                f"    {str(n.get('updated', ''))[:10]}  {n.get('id', '')}  "
                f"({n.get('classification') or 'UNLABELLED'})"
            )
    return "\n".join(lines)
