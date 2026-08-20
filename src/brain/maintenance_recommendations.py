"""Recommendations lifecycle helpers and curation/promote-scan hot entries."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import hashlib


# content). Lifecycle: open -> aging (implicit: an open entry past the aging
# threshold) -> surfaced (flipped + queued into hot.md, exactly once) ->
# resolved (removed from the open file, appended to the log as a closed
# record). Appending a NEW open entry, and resolving one, are both simple
# enough that no CLI verb exists yet — an agent/owner appends/edits the JSONL
# directly, the same convention as `hot.md` itself (docs/session-memory.md).
# ---------------------------------------------------------------------------
DEFAULT_RECOMMENDATION_AGING_DAYS = 14


def parse_recommendation_lines(text: str) -> list[dict[str, Any]]:
    """Parse a ``recommendations-open.jsonl`` blob into entry dicts.

    A blank or unparsable line is dropped, never raised — the aging fold is a
    cheap unconditional maintain step and must never abort the run over one
    corrupt line."""
    out: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def render_recommendation_lines(entries: list[dict[str, Any]]) -> str:
    """Serialise entries back to the one-JSON-object-per-line file shape."""
    if not entries:
        return ""
    return "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n"


def recommendations_aging_scan(
    entries: list[dict[str, Any]],
    today: datetime.date,
    aging_days: int = DEFAULT_RECOMMENDATION_AGING_DAYS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flip any ``status: open`` entry whose ``created`` is >= ``aging_days``
    old to ``status: surfaced`` (+ ``surfaced_at``). Returns ``(updated,
    newly_surfaced)`` — ``updated`` is the FULL list (for rewriting the JSONL),
    ``newly_surfaced`` is only what changed THIS run (what the caller queues
    into ``hot.md``). Idempotent by construction: an entry already
    ``surfaced``/``resolved`` is left untouched and never re-emitted, so a
    caller that reruns this scan never duplicates a hot-queue entry for the
    same recommendation."""
    updated: list[dict[str, Any]] = []
    newly: list[dict[str, Any]] = []
    for raw in entries:
        entry = dict(raw)
        if entry.get("status", "open") == "open":
            age: int | None = None
            try:
                age = (today - datetime.date.fromisoformat(str(entry.get("created")))).days
            except (TypeError, ValueError):
                age = None
            if age is not None and age >= aging_days:
                entry["status"] = "surfaced"
                entry["surfaced_at"] = today.isoformat()
                newly.append(entry)
        updated.append(entry)
    return updated, newly


def render_recommendation_hot_entry(entry: dict[str, Any], today: datetime.date) -> str:
    """One ``hot.md``-shaped dated entry (docs/session-memory.md format) for a
    newly-aged recommendation."""
    text = str(entry.get("text") or "").strip()
    title = (text.splitlines()[0] if text else entry.get("id", "recommendation"))[:80]
    return (
        f"## {today.isoformat()} — Recommendation aged: {title}\n"
        f"- **Context:** proposed {entry.get('created', '?')}, still open with no "
        f"action (id: `{entry.get('id')}`).\n"
        f"- **Question:** still worth doing — act on it, defer it, or drop it?\n"
        f"- **Owner input needed:** resolve `{entry.get('id')}` in "
        f"recommendations-open.jsonl (moves to recommendations-log.md once decided).\n"
    )


def resolve_recommendation(
    entries: list[dict[str, Any]], rec_id: str, resolution: str, today: datetime.date
) -> tuple[list[dict[str, Any]], str | None]:
    """Close out ``rec_id``: returns ``(remaining_entries, log_line)`` —
    ``remaining_entries`` is the open list with ``rec_id`` removed (rewrite the
    JSONL with it), ``log_line`` is the Markdown line to append to
    ``recommendations-log.md`` (``None`` if ``rec_id`` was not found — no-op,
    never raises)."""
    remaining: list[dict[str, Any]] = []
    resolved: dict[str, Any] | None = None
    for entry in entries:
        if entry.get("id") == rec_id and resolved is None:
            resolved = entry
        else:
            remaining.append(entry)
    if resolved is None:
        return entries, None
    log_line = (
        f"## {today.isoformat()} — {resolved.get('text', '(no text)')} (resolved)\n"
        f"- **Opened:** {resolved.get('created', '?')}\n"
        f"- **Resolution:** {resolution}\n\n"
    )
    return remaining, log_line


# ---------------------------------------------------------------------------
# Sunday curation/promotion-scan hot-queue renderers (AUT-02). Pure markdown
# builders — ``BrainCore.maintain`` does the idempotent file I/O.
# ---------------------------------------------------------------------------
def aggregate_stale_links(
    stale_links: list[dict[str, Any]],
) -> tuple[list[tuple[str, dict[str, Any]]], int, int]:
    """Collapse a (possibly huge) raw stale-link list into per-target
    aggregates sorted by frequency. Returns ``(sorted_targets, distinct_srcs,
    total_occurrences)`` where each ``sorted_targets`` item is
    ``(target_text, {"count", "reason", "example"})``. Field bug 2: the fold
    once dumped 4341 raw rows into hot.md as a wall of text — aggregate so an
    unbounded dangling set becomes a bounded summary + top offenders."""
    by_target: dict[str, dict[str, Any]] = {}
    src_notes: set[str] = set()
    for s in stale_links:
        target = s.get("target_text") or "(empty)"
        frm = (s.get("from") or {}).get("id") or "(unknown)"
        src_notes.add(frm)
        agg = by_target.setdefault(
            target, {"count": 0, "reason": s.get("reason"), "example": frm})
        agg["count"] += 1
    ordered = sorted(by_target.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    return ordered, len(src_notes), len(stale_links)


def curation_finding_key(stale_links: list[dict[str, Any]]) -> str:
    """A content hash of the DISTINCT stale-target set, for the hot.md
    idempotency key. Keying on this instead of the run date stops the fold
    re-reporting an IDENTICAL dangling set every week under a fresh
    ``maintain:curate:<date>`` key (field bug 2). An empty set yields
    ``"none"`` (the caller only appends when there are findings)."""
    targets = sorted({(s.get("target_text") or "") for s in stale_links})
    if not targets:
        return "none"
    return hashlib.sha256("\n".join(targets).encode("utf-8")).hexdigest()[:12]


def promote_scan_finding_key(candidates: list[dict[str, Any]]) -> str:
    """A content hash of the DISTINCT candidate-id set, for the hot.md
    idempotency key — same treatment as ``curation_finding_key``. Keying on
    the run date re-reported an IDENTICAL candidate set every run under a
    fresh ``maintain:promote-scan:<date>`` key (retro signature
    ``duplicate-findings``); a changed candidate set still yields a new key,
    so a genuinely new finding still logs."""
    ids = sorted({str(c.get("id") or "") for c in candidates})
    if not ids:
        return "none"
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:12]


def render_curation_hot_entry(
    stale_links: list[dict[str, Any]], revisit_sample: list[dict[str, Any]],
    today: datetime.date,
) -> str:
    # Neutral label (no weekday): this is the "Sunday branch" but runs on
    # whatever day it's DUE via due-since-last-run catch-up, so a hardcoded
    # "Sunday" mislabelled catch-up runs on other weekdays (field bug 1).
    ordered, distinct_srcs, total = aggregate_stale_links(stale_links)
    lines = [f"## {today.isoformat()} — curation scan"]
    lines.append(
        f"- **Context:** curation fold found {total} stale wikilink "
        f"occurrence(s) — {len(ordered)} distinct target(s) across "
        f"{distinct_srcs} note(s); {len(revisit_sample)}-note revisit sample."
    )
    if ordered:
        lines.append(f"- Top offenders (of {len(ordered)} distinct targets):")
        for target, agg in ordered[:10]:
            lines.append(
                f"  - `{target}` — {agg['count']}× ({agg['reason']}), "
                f"e.g. from `{agg['example']}`"
            )
        if len(ordered) > 10:
            lines.append(f"  - … {len(ordered) - 10} more distinct target(s)")
    for r in revisit_sample[:10]:
        lines.append(
            f"  - revisit: `{r.get('id')}` (last updated {r.get('updated')}, "
            f"age {r.get('age_days')}d, score {r.get('score')})"
        )
    lines.append(
        "- **Tier-1 (auto-resolved by the weekly synthesis session):** "
        "unambiguous stale-link fixes are applied on the audited path; this is "
        "the LOG, not a queue. Detail: `brain curate --json` / the `curation` skill."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Autoresearch quarterly-poke visibility (HARDENED:claude, AUT-01). aut-04
# (session s11, after this one) is the skill that actually RUNS autoresearch
# and writes an evidence artifact under eval/runs/ each time; this helper is
# the pure staleness judgment the brief renders. No autoresearch run has ever
# landed at the time this module ships (s09 precedes s11) — ``last_run=None``
# (never run) is handled the same as "very stale", not as an error, so the
# quarterly convention is visible from day one instead of silently starting
# blind.
# ---------------------------------------------------------------------------
DEFAULT_AUTORESEARCH_STALE_DAYS = 90


def autoresearch_staleness(
    last_run: datetime.date | None, today: datetime.date,
    stale_days: int = DEFAULT_AUTORESEARCH_STALE_DAYS,
) -> dict[str, Any]:
    """Judge whether the quarterly autoresearch cadence looks alive.

    ``last_run`` is the date of the newest ``eval/runs/autoresearch-*.json``
    artifact (the caller does that file scan; this function is pure). Returns
    ``never_run`` (no artifact found yet), ``age_days`` (``None`` if never
    run), and ``stale`` (true when overdue — the brief only surfaces a line
    when this is true, per the ~90-day threshold)."""
    if last_run is None:
        return {"never_run": True, "age_days": None, "last_run": None, "stale": True}
    age_days = (today - last_run).days
    return {
        "never_run": False,
        "age_days": age_days,
        "last_run": last_run.isoformat(),
        "stale": age_days > stale_days,
    }


def render_graphify_hot_entry(candidates: list[dict[str, Any]], today: datetime.date) -> str:
    """The monthly graphify-build hot-queue entry (GRF-01/GRF-02, ADR-0003
    Ruling 6/(a)). ``candidates`` are already egress-gated INFERRED edges —
    review-only, NEVER auto-written into a note body."""
    lines = [f"## {today.isoformat()} — Monthly graphify discovery build"]
    lines.append(
        f"- **Context:** {len(candidates)} INFERRED link candidate(s) proposed "
        "from embedding-neighbour similarity (discovery-only, non-authoritative)."
    )
    for c in candidates[:10]:
        lines.append(
            f"  - `{c.get('from')}` <-> `{c.get('to')}` (score {c.get('score')}) — {c.get('reason')}"
        )
    lines.append(
        "- **Owner input needed:** review via `brain graphify --json` and, if a "
        "candidate is genuinely related, add the wikilink yourself — graphify "
        "never writes a link into a note."
    )
    return "\n".join(lines) + "\n"

# Cross-section binds, deferred past this module's own defs.
