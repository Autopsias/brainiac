"""Graph-hygiene and graphify-drift weekly folds (GRH-01)."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import os


# ---------------------------------------------------------------------------
# GRH-01 (2026-07-20 dedup batch, finding-driven weekly fold) — the
# `graph_hygiene` branch: cheap, no-model wikilink-hygiene metrics
# (`graph.graph_hygiene_metrics`), Wednesday-gated (see `_WEEKLY_TRIGGER_WEEKDAY`
# above), escalating to a hot.md LOG line (never a queue/ritual) when
# knowledge-layer orphans grow past a threshold since the last run.
# ---------------------------------------------------------------------------
GRAPH_ORPHAN_GROWTH_MAX_ENV = "BRAIN_GRAPH_ORPHAN_GROWTH_MAX"
DEFAULT_GRAPH_ORPHAN_GROWTH_MAX = 10


def graph_hygiene_orphan_growth(
    prev_metrics: dict[str, Any] | None, new_metrics: dict[str, Any],
) -> int:
    """Orphan-count delta since the last recorded run. No prior baseline
    (never run, or a metrics-less legacy entry) -> 0 growth, never a spurious
    alarm on the very first run."""
    prev_count = (prev_metrics or {}).get("orphan_count")
    if not isinstance(prev_count, int):
        return 0
    return max(0, int(new_metrics.get("orphan_count", 0)) - prev_count)


def should_alert_graph_hygiene_growth(growth: int, *, max_growth: int | None = None) -> bool:
    import os as _os

    threshold = max_growth if max_growth is not None else int(
        _os.environ.get(GRAPH_ORPHAN_GROWTH_MAX_ENV, DEFAULT_GRAPH_ORPHAN_GROWTH_MAX))
    return growth > threshold


def render_graph_hygiene_hot_entry(
    metrics: dict[str, Any], growth: int, today: datetime.date,
) -> str:
    """A LOG line (never a queue item, never owner-input-needed — self-
    organizing-vault ruling): tells the weekly synthesis session to work the
    new orphans and regenerate the link-candidates artifact reference."""
    lines = [f"## {today.isoformat()} — Graph hygiene: orphan growth"]
    lines.append(
        f"- **Context:** knowledge-layer orphans grew by {growth} since the last "
        f"run (now {metrics.get('orphan_count')} of {metrics.get('knowledge_note_count')} "
        f"notes; {metrics.get('island_count')} connected component(s); "
        f"{metrics.get('dangling_target_count')} dangling wikilink target(s))."
    )
    if metrics.get("orphan_ids"):
        lines.append("- **New/existing orphans (sample):** " +
                      ", ".join(f"`{i}`" for i in metrics["orphan_ids"][:10]))
    lines.append(
        "- **Next:** work the new orphans (link them in, or move to archive/) "
        "and regenerate the graphify link-candidates artifact (`brain graphify "
        "--dry-run --json`) for review — self-organization fold, not an owner ritual."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# LNK-03b — the DAILY (not Wednesday-gated) cheap knowledge-layer orphan
# counter. Reuses `graph.graph_hygiene_metrics` as-is (measured: ~11ms on a
# synthetic 2,500-note index — well under a "lighter helper needed" bar) and
# persists only the orphan count as `kl_orphans`, every day, so a SUSTAINED
# multi-day drift is visible even though the full graph_hygiene branch (with
# its own separate, single-prior-run growth alarm) only runs weekly.
# ---------------------------------------------------------------------------
KL_ORPHAN_SUSTAINED_GROWTH_ENV = "BRAIN_ORPHAN_SUSTAINED_GROWTH"
DEFAULT_KL_ORPHAN_SUSTAINED_GROWTH = 5
KL_ORPHAN_TRAILING_DAYS = 7


def kl_orphan_sustained_growth(
    history: list[dict[str, Any]], new_count: int,
) -> int | None:
    """Growth in ``kl_orphans`` versus the value recorded
    ``KL_ORPHAN_TRAILING_DAYS`` prior RECORDED runs ago (``history`` is
    already-sorted-by-ts health-history rows, prior to appending this run's
    own). Returns ``None`` when fewer than that many prior daily values
    exist yet — no baseline, never a spurious alarm while the feature is
    still warming up."""
    vals = [h.get("kl_orphans") for h in history if isinstance(h.get("kl_orphans"), int)]
    if len(vals) < KL_ORPHAN_TRAILING_DAYS:
        return None
    baseline = vals[-KL_ORPHAN_TRAILING_DAYS]
    return new_count - baseline


def should_alert_kl_orphan_sustained_growth(
    growth: int | None, *, max_growth: int | None = None,
) -> bool:
    import os as _os

    if growth is None:
        return False
    threshold = max_growth if max_growth is not None else int(
        _os.environ.get(KL_ORPHAN_SUSTAINED_GROWTH_ENV, DEFAULT_KL_ORPHAN_SUSTAINED_GROWTH))
    return growth > threshold


def render_kl_orphan_sustained_growth_hot_entry(
    new_count: int, growth: int, today: datetime.date,
) -> str:
    """A LOG line (never a queue item — same PUSH posture as every other
    self-organization fold): names the sustained trend and points at the
    graph explorer (`brain graph-report`) rather than enumerating ids (the
    weekly `graph_hygiene` fold already surfaces a sample of those)."""
    lines = [f"## {today.isoformat()} — Knowledge-layer orphans: sustained growth"]
    lines.append(
        f"- **Context:** knowledge-layer orphan count grew by {growth} over the "
        f"trailing {KL_ORPHAN_TRAILING_DAYS} recorded day(s) (now {new_count})."
    )
    lines.append(
        "- **Next:** review the trend in `brain health-report` (Graph hygiene "
        "section) or `brain graph-report` for the full explorer — self-organization "
        "log, not an owner ritual; the weekly `graph_hygiene` fold's own single-run "
        "growth alarm stays independent."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# FRESH-01 (2026-07-11) — drift-triggered graphify. The monthly date-gate
# (``maintain_branches``' "graphify" branch, `_next_trigger` above) is a
# FLOOR, not a GATE: a vault that doubles mid-month (measured on the owner's
# real vault — graph built at 1,169 notes, index at 2,239 sixteen days
# later) must not wait out the calendar for the next 1st-of-month. The daily
# fold instead measures corpus drift since the last graphify build and fires
# a BOUNDED rebuild early once drift crosses a threshold.
#
# ``core.graphify()`` runs synchronously IN-PROCESS (owner decision
# 2026-07-12, superseding the earlier subprocess-wrapper hardening) and its
# OWN branch bookkeeping (``_mark("graphify", ok)``) only persists at the END
# of a run — so a build that hangs (until the maintain run-lock recovers it)
# would never advance any cooldown on its own, and a naive drift check would
# re-fire a fresh build every single hourly maintain, forever. The
# ``_graphify_drift`` maintain-state marker is therefore ATTEMPT-keyed
# (``BrainCore._run_bounded_graphify`` persists ``last_attempt`` to disk
# BEFORE the build runs) and backs off exponentially (capped) on consecutive
# failures, resetting only after a build that actually publishes.
# ---------------------------------------------------------------------------
GRAPHIFY_DRIFT_PCT_ENV = "BRAIN_GRAPHIFY_DRIFT_PCT"
DEFAULT_GRAPHIFY_DRIFT_PCT = 0.15
GRAPHIFY_COOLDOWN_DAYS_ENV = "BRAIN_GRAPHIFY_COOLDOWN_DAYS"
DEFAULT_GRAPHIFY_COOLDOWN_DAYS = 2
GRAPHIFY_BACKOFF_MAX_MULTIPLIER = 8  # capped exponential backoff on consecutive overruns


def graphify_drift(manifest: dict[str, Any] | None, conn: Any) -> float:
    """Corpus drift ratio since the persisted graphify ``manifest`` — the
    SAME ``{"notes": {id: content_hash}, ...}`` shape ``BrainCore.graphify``
    reads from/writes to ``graph_manifest_path``. Reuses the index's own
    ``content_hash`` column via ``graphify.corpus_manifest`` (never
    re-hashes note bodies, same embedding-reuse doctrine as the build
    itself). ``(changed + added + removed) / len(old_notes)``.

    No persisted manifest yet (fresh vault, or first-ever build) is treated
    as full drift — ``1.0`` — so a brand-new vault is eligible on its very
    first drift check (still subject to the same cooldown as every other
    trigger)."""
    from . import graphify as gmod

    old_notes: dict[str, str] = (manifest or {}).get("notes") or {}
    new_notes = gmod.corpus_manifest(conn)
    if not old_notes:
        # No (or empty) baseline: full drift ONLY if there is anything to
        # build — an empty baseline over a still-empty corpus is 0.0, not a
        # perpetual build/skip churn every cooldown (review finding [3]).
        return 1.0 if new_notes else 0.0
    changed = sum(1 for nid, h in old_notes.items() if nid in new_notes and new_notes[nid] != h)
    removed = sum(1 for nid in old_notes if nid not in new_notes)
    added = sum(1 for nid in new_notes if nid not in old_notes)
    return (changed + added + removed) / len(old_notes)


def graphify_backoff_days(cooldown_days: int, consecutive_overruns: int) -> int:
    """Capped exponential backoff (HARDENED correction c): each consecutive
    overrun/failure doubles the effective cooldown, capped at
    ``GRAPHIFY_BACKOFF_MAX_MULTIPLIER``x — a corpus that keeps timing out
    backs off instead of re-attempting (and re-failing) a fresh bounded
    build every single hourly maintain run forever."""
    multiplier = min(2 ** max(0, consecutive_overruns), GRAPHIFY_BACKOFF_MAX_MULTIPLIER)
    return cooldown_days * multiplier


def graphify_drift_marker_due(
    marker: dict[str, Any] | None, today: datetime.date, cooldown_days: int,
) -> bool:
    """True iff enough time has passed since the last bounded-graphify
    ATTEMPT (never "success" alone — HARDENED correction b) to allow
    another one. ``marker`` is the persisted ``_graphify_drift`` maintain-
    state entry; absent/never-attempted is always due. A corrupt
    ``last_attempt`` (unparsable date) degrades to "due now" rather than
    permanently wedging the trigger."""
    last = marker.get("last_attempt") if marker else None
    if not last:
        return True
    try:
        last_date = datetime.date.fromisoformat(str(last))
    except ValueError:
        return True
    overruns = int((marker or {}).get("consecutive_overruns", 0))
    effective_cooldown = graphify_backoff_days(cooldown_days, overruns)
    return (today - last_date).days >= effective_cooldown


def should_trigger_drift_graphify(
    ratio: float, marker: dict[str, Any] | None, today: datetime.date, *,
    drift_pct: float | None = None, cooldown_days: int | None = None,
) -> bool:
    """The daily fold's drift-trigger decision: drift over threshold AND the
    attempt-keyed cooldown (with backoff) has elapsed. Pure — the caller
    supplies ``ratio`` (from ``graphify_drift``) and ``marker`` (the loaded
    ``_graphify_drift`` maintain-state entry). The monthly date-gate
    (``maintain_branches``) remains the FLOOR trigger, independent of this
    function — a maintain run ORs the two triggers together (session
    context bundle FRESH-01)."""
    import os as _os

    pct = drift_pct if drift_pct is not None else float(
        _os.environ.get(GRAPHIFY_DRIFT_PCT_ENV, DEFAULT_GRAPHIFY_DRIFT_PCT))
    days = cooldown_days if cooldown_days is not None else int(
        _os.environ.get(GRAPHIFY_COOLDOWN_DAYS_ENV, DEFAULT_GRAPHIFY_COOLDOWN_DAYS))
    if ratio <= pct:
        return False
    return graphify_drift_marker_due(marker, today, days)

# Cross-section binds, deferred past this module's own defs.
