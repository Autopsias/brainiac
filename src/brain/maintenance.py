"""Outcomes-report shape + date-gate logic for the `brain` maintenance verbs
(CUT-03): `check` / `health` / `curate` / `integrity` / `promote-scan` / `maintain`.

Pure, dependency-free helpers — no I/O, no BrainCore import (mirrors brain.brief).
``check``/``health``/``curate``/``integrity``/``promote-scan`` are the FOLDED
maintenance rituals from ``routines/manifest.json`` (per-task ``disposition``); ``maintain`` is
the single sanctioned host task (``brain-nightly``) that multiplexes the
weekly/monthly cadences into one OS scheduler entry via date gates, per
``routines/manifest.json`` ``locked_counts``.

The three-bucket shape here is the generic, vault-agnostic structured
equivalent of the owner-vault scheduled-task outcomes contract's three-block
report (✅ Auto-remediated / ⚠ Action Required / 🚧 Blocked): a thin host-side
scheduled-task wrapper can render ``render_outcomes_markdown`` directly, or
consume the structured JSON.

MEM-03/AUT-02 (session s08) add two more pure pieces: recommendations
lifecycle (open -> aging -> surfaced -> resolved) file-format helpers, and
markdown renderers for the Sunday curation/promotion-scan findings. This
module still does NO I/O — ``BrainCore.maintain`` (src/brain/core.py) is the
one place that reads/writes `.brain/memory/{recommendations-open.jsonl,
recommendations-log.md,hot.md}`, guarded by an idempotency key so a re-run
never duplicates a hot-queue entry (ADR-0003 Ruling 5/HARDENED:codex).
"""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

_log = logging.getLogger("brain.maintenance")


def auto_fixed_item(verb: str, path: str, reason: str) -> dict[str, Any]:
    return {"verb": verb, "path": path, "reason": reason}


_DATED_ARTIFACT = re.compile(r"^(?:brief|digest)-(\d{4}-\d{2}-\d{2})\.html$")


def reap_future_dated_artifacts(brief_dir: Path, today: datetime.date) -> list[str]:
    """Delete generation-stamped brief/digest HTML whose embedded date is AFTER
    ``today``. Such a file can only exist because a maintain run computed a
    future date (field bug 1, e.g. a `--date <future>` exercise leaked onto a
    live vault) — and it SHADOWS the real artifact for that day. Self-heal: the
    next real nightly reaps it, so the corruption clears with no manual ritual
    (self-organizing-vault ruling). Touches only derived, regenerable
    `.brain/brief/` files — never a source note. Returns reaped basenames."""
    reaped: list[str] = []
    if not brief_dir.is_dir():
        return reaped
    for f in sorted(brief_dir.glob("*.html")):
        m = _DATED_ARTIFACT.match(f.name)
        if not m:
            continue
        try:
            fdate = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if fdate > today:
            try:
                f.unlink()
                reaped.append(f.name)
            except OSError:
                pass
    return reaped


def action_required_item(
    finding: str, why: str, proposed: str, inspect: str
) -> dict[str, Any]:
    return {"finding": finding, "why": why, "proposed_action": proposed, "inspect": inspect}


def blocked_item(finding: str, blocking_on: str, retry_when: str) -> dict[str, Any]:
    return {"finding": finding, "blocking_on": blocking_on, "retry_when": retry_when}


def framework_sync_finding(report: dict[str, Any]) -> dict[str, Any] | None:
    """HYG-02 (ADR-0003 Ruling 5): shape a pre-computed
    ``tools.framework_sync.audit()`` report into a Monday-health
    ``action_required`` item, or ``None`` when the report is clean. Pure —
    the actual file-hashing/reading I/O lives in ``tools/framework_sync.py``
    and is invoked by ``BrainCore`` (host-only), never here. Never
    auto-fixes: the proposed action is always "re-run package_clients.py"."""
    if report.get("clean"):
        return None
    drift = report.get("skill_drift") or []
    claude_md = report.get("claude_md_import") or {}
    paths = [f"{d['skill']} [{d['mirror']}] {d.get('path') or d['reason']}" for d in drift[:5]]
    parts = []
    if drift:
        parts.append(f"{len(drift)} skill-mirror file(s) diverged")
    if not claude_md.get("ok"):
        parts.append(f"CLAUDE.md: {claude_md.get('reason')}")
    return action_required_item(
        "; ".join(parts) or "framework-sync drift detected",
        "the .claude/skills canonical tree, .agents/skills mirror, and/or "
        "plugins/ marketplace copies have drifted apart (or CLAUDE.md's "
        "@AGENTS.md import broke)",
        "run `python3 tools/package_clients.py` to resync, then re-run health",
        "; ".join(paths) or "CLAUDE.md",
    )


def build_outcomes(
    auto_fixed: list[dict[str, Any]] | None = None,
    action_required: list[dict[str, Any]] | None = None,
    blocked: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The structured three-bucket disposition. Buckets are ALWAYS present
    (possibly empty) so the shape is stable and grep/parse-friendly — mirrors
    the vault outcomes contract's "(none)" convention."""
    af = list(auto_fixed or [])
    ar = list(action_required or [])
    bl = list(blocked or [])
    return {
        "auto_fixed": af,
        "action_required": ar,
        "blocked": bl,
        "counts": {"auto_fixed": len(af), "action_required": len(ar), "blocked": len(bl)},
    }


def render_outcomes_markdown(outcomes: dict[str, Any]) -> str:
    """Render the three-block markdown shape (✅/⚠/🚧), generic — no
    vault-specific file paths or chain-script invocations baked in."""
    lines: list[str] = []

    af = outcomes.get("auto_fixed", [])
    lines.append(f"## Auto-remediated this run ({len(af)} items)")
    if af:
        for it in af:
            lines.append(f"- **[{it.get('verb')}]** `{it.get('path')}` — {it.get('reason')}")
    else:
        lines.append("(none)")
    lines.append("")

    ar = outcomes.get("action_required", [])
    lines.append(f"## Action Required ({len(ar)} items)")
    if ar:
        for i, it in enumerate(ar, 1):
            lines.append(f"**Finding {i}:** {it.get('finding')}")
            lines.append(f"**Why it can't auto-fix:** {it.get('why')}")
            lines.append(f"**Proposed action:** {it.get('proposed_action')}")
            lines.append(f"**Inspect:** `{it.get('inspect')}`")
            lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    bl = outcomes.get("blocked", [])
    lines.append(f"## Blocked — external dependency ({len(bl)} items)")
    if bl:
        for i, it in enumerate(bl, 1):
            lines.append(f"**Finding {i}:** {it.get('finding')}")
            lines.append(f"**Blocking on:** {it.get('blocking_on')}")
            lines.append(f"**Retry when:** {it.get('retry_when')}")
            lines.append("")
    else:
        lines.append("(none)")

    return "\n".join(lines).rstrip() + "\n"


# Date-gated branch names, in cadence order. "daily" is always due; the rest
# fire on the weekday/day-of-month the persistence budget assigns them so the
# WHOLE roster rides the one sanctioned OS task (`brain-nightly`) with ZERO
# extra scheduler entries (persistence-budget.md THE LOCK).
_MONDAY, _TUESDAY, _SUNDAY = 0, 1, 6
_WEEKLY_TRIGGER_WEEKDAY = {
    "health": _MONDAY, "integrity": _TUESDAY, "digest": _SUNDAY, "golden": _SUNDAY,
}
_ALL_BRANCHES = ("daily", "health", "integrity", "digest", "graphify", "golden")


def _next_trigger(branch: str, last_run: datetime.date) -> datetime.date:
    """First calendar date STRICTLY AFTER ``last_run`` on which ``branch`` is
    next due (ADR-0003 Ruling d)."""
    if branch == "daily":
        return last_run + datetime.timedelta(days=1)
    if branch == "graphify":
        y, m = last_run.year, last_run.month + 1
        if m > 12:
            y, m = y + 1, 1
        return datetime.date(y, m, 1)
    weekday = _WEEKLY_TRIGGER_WEEKDAY[branch]
    days_ahead = (weekday - last_run.weekday()) % 7
    return last_run + datetime.timedelta(days=days_ahead or 7)


def maintain_branches(
    today: datetime.date | None = None,
    last_runs: dict[str, str | None] | None = None,
) -> list[str]:
    """Which date-gated branches are due for ``today`` (default: real today).

    ADR-0003 Ruling 5/(d) — DUE-SINCE-LAST-RUN, not calendar-day-only: a
    branch is due when ``today >= next_trigger(last_run)``. ``last_runs`` maps
    branch -> its last SUCCESSFUL run date (ISO string) as persisted in
    ``.brain/maintain-state.json``; a branch absent from the mapping (or
    ``last_runs=None``, e.g. no state file yet — a brand-new install) has
    never run and is due immediately, regardless of weekday. This is safe
    because every branch is idempotent (a re-run finds nothing new to do).

    A missed weekly/monthly branch (the laptop was off on its trigger day)
    fires once on the next run that reaches or passes its next trigger date —
    not once per day it stayed missed.

    Mon -> health, Tue -> integrity, Sun -> digest, 1st-of-month -> graphify
    (ADR-0003 Ruling 6/(a): `maintain` invokes a REAL, bounded graph build on
    this date-gate — drift-gated, embedding-reuse, wall-clock-budgeted; see
    `BrainCore.graphify`). "daily" (index sync + drain + recommendations-aging
    scan) is due at most once per day.
    """
    d = today or datetime.date.today()
    last_runs = last_runs or {}
    due: list[str] = []
    for branch in _ALL_BRANCHES:
        raw = last_runs.get(branch)
        if not raw:
            due.append(branch)  # never run -> due now
            continue
        last = raw if isinstance(raw, datetime.date) else datetime.date.fromisoformat(raw)
        if last > d:
            # A marker AFTER today is corrupt state (clock skew, or a
            # `--today` date-gate test run against a live vault). Left alone
            # it silently disables the branch until real time catches up, so
            # treat it like never-run: due now, and the success rewrite of
            # last_run to today's date self-heals the state file.
            due.append(branch)
            continue
        if d >= _next_trigger(branch, last):
            due.append(branch)
    return due


# ---------------------------------------------------------------------------
# Recommendations lifecycle (MEM-03, ADR-0003 Ruling 5 "daily" unconditional
# fold). Ported pattern: the reference vault's `_recommendations_open.jsonl` /
# `_recommendations_log.md` (schema/pattern only, per Appendix B — never
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
    import hashlib
    return hashlib.sha256("\n".join(targets).encode("utf-8")).hexdigest()[:12]


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


# ---------------------------------------------------------------------------
# WD-03 (2026-07-12) — Sunday cross-family golden-probe EXECUTION. Codex (the
# family that did NOT build the retrieval engine) shells the SAME `brain`
# CLI the probes exercise — this is cross-family EXECUTION of a deterministic
# scorer, correction 5: NEVER "independent verification" / "independent
# eyes" / "Codex grades retrieval". A shared retrieval bug is invisible to
# both invokers; only the INVOKER differs, not the measurement.
#
# Pure helpers only, here (parsing/validation/marker arithmetic) — the actual
# `codex exec` / self-run subprocess calls are host I/O and live on
# `BrainCore._run_golden_probe` (mirrors `_run_bounded_graphify`'s split).
# The 4 exit codes mirror `brain.golden_probe`'s own contract BY HAND (that
# module is deliberately engine-decoupled/stdlib-only and never imports
# anything from this package, so this fold never imports it either — see
# golden_probe.py's own VALID_TIERS for the same by-hand-sync precedent).
# ---------------------------------------------------------------------------
GOLDEN_EXIT_OK = 0
GOLDEN_EXIT_REGRESSION = 1
GOLDEN_EXIT_ACTION_REQUIRED = 2
GOLDEN_EXIT_TRANSIENT = 3
_GOLDEN_VALID_EXIT_CODES = (GOLDEN_EXIT_OK, GOLDEN_EXIT_REGRESSION,
                            GOLDEN_EXIT_ACTION_REQUIRED, GOLDEN_EXIT_TRANSIENT)
_GOLDEN_VALID_DISPOSITIONS = ("ok", "regression", "action_required", "transient")

GOLDEN_RETRY_BASE_MINUTES_ENV = "BRAIN_GOLDEN_RETRY_BASE_MINUTES"
# 6h base (re-review): the old 60m EQUALLED the hourly maintain cadence, so a
# run repeatedly killed mid-`codex exec` re-fired every hour despite the
# provisional backoff. golden is a WEEKLY branch — a base well above the
# cadence, escalating on consecutive failures (incl. kills), is the point.
DEFAULT_GOLDEN_RETRY_BASE_MINUTES = 360
GOLDEN_RETRY_MAX_MULTIPLIER = 8  # capped exponential backoff, same shape as graphify's

GOLDEN_CODEX_TIMEOUT_SECONDS_ENV = "BRAIN_GOLDEN_CODEX_TIMEOUT_SECONDS"
# HARD cap <=10min (correction 1) — strictly below the 2h maintain-lock stale
# window, so a wedged codex child can never itself become the reason a
# concurrent maintain run thinks the lock is abandoned.
DEFAULT_GOLDEN_CODEX_TIMEOUT_SECONDS = 600
MAX_GOLDEN_CODEX_TIMEOUT_SECONDS = 600


def golden_codex_timeout_seconds() -> int:
    import os as _os

    raw = int(_os.environ.get(GOLDEN_CODEX_TIMEOUT_SECONDS_ENV, DEFAULT_GOLDEN_CODEX_TIMEOUT_SECONDS))
    return max(1, min(raw, MAX_GOLDEN_CODEX_TIMEOUT_SECONDS))


def golden_probes_path(vault: Path) -> Path:
    """Per-vault probes file (WD-02) — absence is a loud SKIP, never an
    error (session context bundle: 'skips loudly when codex is absent' /
    here, when the probes file itself is absent)."""
    return Path(vault) / "eval" / "golden-probes.json"


def build_codex_golden_prompt(probes_path: Path, vault: Path, python_exe: str) -> str:
    """The FIXED instruction handed to `codex exec` (correction 1): run
    ONLY the golden-probe scorer, read-only, and return ONLY its JSON — no
    prose wrapper — so the caller's strict shape/range validation is
    checking the scorer's own emitted document, not codex's summary of it.

    ``python_exe`` (review fixes [2] + the re-review's OUTER-interpreter fix)
    is the ABSOLUTE host interpreter that has ``brain`` importable
    (``sys.executable`` from the running maintain). BOTH the outer
    ``-m brain.golden_probe`` invocation AND the inner ``--brain-cmd`` use it:
    a bare ``python3`` here would ``ModuleNotFoundError`` under codex's ambient
    interpreter on a uv-tool/pipx-isolated brain install (the recommended
    channels), so the codex leg would fail every Sunday to the degraded
    self-run and cross-family EXECUTION would never actually happen."""
    brain_cmd = shlex.join([python_exe, "-m", "brain.cli"])
    return (
        "Run exactly this command and reply with ONLY its stdout, verbatim, "
        "and nothing else before or after it:\n\n"
        f"{shlex.quote(python_exe)} -m brain.golden_probe {shlex.quote(str(probes_path))} "
        f"--vault {shlex.quote(str(vault))} "
        f"--brain-cmd {shlex.quote(brain_cmd)}\n\n"
        "Do not modify any files. Do not run any other command. Do not "
        "interpret, summarize, explain, or comment on the result — your "
        "entire reply must be exactly that command's JSON stdout."
    )


def parse_codex_final_message(stdout: str) -> str | None:
    """Extract the text of the LAST `item.completed` event whose
    `item.type == "agent_message"` from a `codex exec --json` JSONL stream
    (correction 1): the stream interleaves thread/turn/tool-call/error
    events, so a caller must never treat the first (or only) JSON-shaped
    line as the answer — a run can emit an `item.type: "error"` info event
    before its real final message. Returns ``None`` when no agent_message
    event is found (or the stream is not JSONL at all); the caller treats
    that as a codex-path failure and falls back to the self-run."""
    last_text: str | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                last_text = text
    return last_text


def validate_golden_probe_doc(doc: Any) -> str | None:
    """Strict shape/range check on a parsed golden-probe result document
    (from either the codex path or the self-run fallback) — the
    "exit-0-with-garbage" trap (correction 1): a codex run can exit 0 while
    its final message is empty prose, a truncated fragment, or a
    well-formed-but-nonsensical object. Returns an error string, or
    ``None`` when the doc is trustworthy enough to source a score from."""
    if not isinstance(doc, dict):
        return f"not a JSON object: {type(doc).__name__}"
    if "disposition" not in doc or "exit_code" not in doc:
        return "missing disposition/exit_code key(s)"
    disposition = doc.get("disposition")
    if disposition not in _GOLDEN_VALID_DISPOSITIONS:
        return f"unrecognized disposition: {disposition!r}"
    exit_code = doc.get("exit_code")
    if isinstance(exit_code, bool) or exit_code not in _GOLDEN_VALID_EXIT_CODES:
        return f"unrecognized exit_code: {exit_code!r}"
    score = doc.get("score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return f"score is not a number: {score!r}"
        if not (0.0 <= float(score) <= 1.0):
            return f"score out of [0,1]: {score!r}"
    return None


def golden_retry_backoff_minutes(base_minutes: int, consecutive_transient: int) -> int:
    """Capped exponential backoff, same shape as `graphify_backoff_days`:
    each consecutive TRANSIENT failure doubles the effective wait, capped at
    `GOLDEN_RETRY_MAX_MULTIPLIER`x, so a flaky codex/CLI backs off instead of
    re-attempting (and re-failing) every single hourly maintain run."""
    multiplier = min(2 ** max(0, consecutive_transient - 1), GOLDEN_RETRY_MAX_MULTIPLIER)
    return base_minutes * multiplier


def golden_attempt_due(marker: dict[str, Any] | None, now: datetime.datetime) -> bool:
    """True iff the persisted `_golden_attempt` marker's `next_retry_at` has
    elapsed (or there is none yet — never attempted, or the last attempt
    resolved deterministically and cleared it). A corrupt/unparsable
    timestamp degrades to "due now" rather than permanently wedging the
    branch (mirrors `graphify_drift_marker_due`'s same fail-open posture)."""
    nxt = (marker or {}).get("next_retry_at")
    if not nxt:
        return True
    try:
        nxt_dt = datetime.datetime.fromisoformat(str(nxt).replace("Z", "+00:00"))
    except ValueError:
        return True
    if nxt_dt.tzinfo is None:
        nxt_dt = nxt_dt.replace(tzinfo=datetime.timezone.utc)
    return now >= nxt_dt


def update_golden_attempt_marker(
    marker: dict[str, Any] | None, now: datetime.datetime, *,
    transient: bool, base_minutes: int | None = None,
) -> dict[str, Any]:
    """The `_golden_attempt` marker to persist AFTER an attempt (the caller
    persists `last_attempt` itself BEFORE the shell-out, mirroring
    `_run_bounded_graphify`'s crash-safety ordering). `transient` is True
    ONLY for exit 3 — every other resolved outcome (ok/regression/
    action_required) is a DETERMINISTIC answer and resets the backoff, since
    the branch got its weekly answer whatever it was."""
    import os as _os

    base = base_minutes if base_minutes is not None else int(
        _os.environ.get(GOLDEN_RETRY_BASE_MINUTES_ENV, DEFAULT_GOLDEN_RETRY_BASE_MINUTES))
    prev = dict(marker or {})
    consecutive = int(prev.get("consecutive_transient_failures", 0))
    consecutive = consecutive + 1 if transient else 0
    out = dict(prev)
    out["consecutive_transient_failures"] = consecutive
    if transient:
        backoff_min = golden_retry_backoff_minutes(base, consecutive)
        out["next_retry_at"] = (now + datetime.timedelta(minutes=backoff_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    else:
        out["next_retry_at"] = None
    return out


def render_promote_scan_hot_entry(candidates: list[dict[str, Any]], today: datetime.date) -> str:
    # Neutral label (see render_curation_hot_entry) — catch-up runs fire on
    # non-Sunday weekdays, so a hardcoded "Sunday" mislabels them (field bug 1).
    lines = [f"## {today.isoformat()} — promotion-scan"]
    lines.append(
        f"- **Context:** {len(candidates)} `raw/` source(s) not yet promoted "
        "into a typed `brain/` note."
    )
    # Note id only — never the absolute path. The stored path is absolute, so
    # echoing it into hot.md left every entry stale after a vault move (field
    # bug 3); the id is a stable, move-proof handle.
    for c in candidates[:10]:
        lines.append(f"  - `{c.get('id')}`")
    if len(candidates) > 10:
        lines.append(f"  - … {len(candidates) - 10} more")
    lines.append(
        "- **Tier-1 (auto-resolved by the weekly synthesis session):** the "
        "obviously-promotable candidates are promoted into typed notes on the "
        "audited path; this is the LOG. A genuinely owner-only call is enqueued "
        "to the `brain inbox` instead."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Workspace sweep (WSP-01, 2026-07-11). A live working folder (e.g. an
# an Obsidian workspace folder) accumulates hundreds of session artifacts
# with no lifecycle. The sweep gives them one: a SETTLED file (mtime older
# than the age gate — nobody is editing it any more) is MOVED into the
# vault's `inbox/`, where the standard ingest drain archives the original
# immutably under `raw/originals/`, signs + writes the `raw/` note (with a
# filename-derived `document_date`), the next sync embeds it, and the
# monthly graphify wires it into the discovery graph. Content already
# ingested dedups by content hash (parked in the inbox duplicate dir), so
# the sweep is idempotent and never double-ingests.
#
# Scope rules (deliberately dumb): TOP-LEVEL FILES ONLY — subdirectories are
# other systems' machine state (skill packages, archives, trust runs) and are
# never touched; dotfiles are skipped. Actively-edited files have a fresh
# mtime and are skipped by the age gate. Configuration: the sweep only runs
# when dirs are configured ($BRAIN_WORKSPACE_SWEEP_DIRS, os.pathsep-separated;
# $BRAIN_WORKSPACE_SWEEP_AGE_DAYS, default 14) — no config, no sweep.
# ---------------------------------------------------------------------------
WORKSPACE_SWEEP_DIRS_ENV = "BRAIN_WORKSPACE_SWEEP_DIRS"
WORKSPACE_SWEEP_AGE_ENV = "BRAIN_WORKSPACE_SWEEP_AGE_DAYS"
WORKSPACE_SWEEP_DEFAULT_AGE_DAYS = 14


def workspace_sweep_config() -> tuple[list[tuple[Path, int | None]], int]:
    """Configured sweep sources + default age gate. Empty list = disabled.

    Each $BRAIN_WORKSPACE_SWEEP_DIRS entry is ``path`` or ``path=N`` — the
    per-dir age override (2026-07-11, round-5 benchmark): a CAPTURE folder
    (an inbox / a meetings drop folder) holds FINAL documents that
    settle in a day, while a WORKING folder needs the long gate so
    in-progress files are never swept. One global age starved the capture
    folders by a week; ``path=1`` fixes that per source."""
    import os

    raw = os.environ.get(WORKSPACE_SWEEP_DIRS_ENV, "").strip()
    dirs: list[tuple[Path, int | None]] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path_part, sep, age_part = entry.rpartition("=")
        if sep and age_part.isdigit():
            # age 0 is legal: same-day capture sweep (a 15-minute
            # write-settle guard still applies inside sweep_workspace).
            dirs.append((Path(path_part).expanduser(), int(age_part)))
        else:
            dirs.append((Path(entry).expanduser(), None))
    try:
        age = int(os.environ.get(WORKSPACE_SWEEP_AGE_ENV, ""))
    except ValueError:
        age = WORKSPACE_SWEEP_DEFAULT_AGE_DAYS
    if age < 1:
        age = WORKSPACE_SWEEP_DEFAULT_AGE_DAYS
    return dirs, age


def sweep_workspace(
    dirs: list[Path] | list[tuple[Path, int | None]], inbox: Path, age_days: int,
    now: float | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Move settled top-level files from ``dirs`` into ``inbox``.

    ``dirs`` entries are ``Path`` (use the global ``age_days``) or
    ``(Path, age)`` tuples (per-dir override; ``None`` = global). Pure file
    motion — classification/signing/dedup all happen downstream in the
    ingest drain. Collisions uniquify (never clobber an inbox file).
    Returns an honest report; a missing dir is reported, never raised."""
    import time

    from .ingest.pipeline import _move, _unique_dest

    from .ingest.handlers import handler_for

    base_now = now if now is not None else time.time()
    report: dict[str, Any] = {
        "swept": [], "skipped_active": 0, "skipped_unsupported": 0,
        "missing_dirs": [], "errors": [],
        "age_days": age_days, "dry_run": dry_run,
    }
    for entry in dirs:
        d, dir_age = entry if isinstance(entry, tuple) else (entry, None)
        eff_age = dir_age if dir_age is not None else age_days
        # age 0 = capture-inbox mode: sweep same-day, but never a file
        # younger than 15 minutes (write-settle guard against partial copies).
        cutoff = base_now - (900.0 if eff_age == 0 else eff_age * 86400.0)
        if not d.is_dir():
            report["missing_dirs"].append(str(d))
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            if handler_for(p) is None:
                # Machine artifacts (.py, .json, .tsv, …) have no ingest
                # handler — sweeping them only detours through quarantine
                # (measured: 344 on the first real sweep). Leave them where
                # they live; the count keeps the skip honest.
                report["skipped_unsupported"] += 1
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError as exc:
                report["errors"].append({"file": str(p), "error": str(exc)})
                continue
            if mtime > cutoff:
                report["skipped_active"] += 1
                continue
            if dry_run:
                report["swept"].append({"file": str(p), "would_move": True})
                continue
            try:
                inbox.mkdir(parents=True, exist_ok=True)
                _move(p, _unique_dest(inbox, p.name))
                report["swept"].append({"file": str(p)})
            except OSError as exc:
                report["errors"].append({"file": str(p), "error": str(exc)})
    return report


# ---------------------------------------------------------------------------
# CUT-02 — quarantine & duplicate retention fold. The ingest pipeline
# (``ingest/pipeline.py``) parks two kinds of leftovers under ``inbox/`` that
# used to grow forever with nobody looking:
#   - ``_duplicate/`` — content that hashed identical to something already
#     promoted into ``raw/``. Safe to prune after a grace period, but ONLY
#     once the full provenance chain re-confirms the redundancy (see
#     ``_verify_duplicate_provenance`` — HARDENED:codex, a manifest hit alone
#     is not proof: the manifest's ``sha256`` key is the ORIGINAL file's hash,
#     but a raw note's own ``sha256:`` frontmatter is the EXTRACTED
#     MARKDOWN's hash, not the original's — the only thing that can prove
#     "this parked file's bytes truly live elsewhere" is the archived
#     original under ``raw/originals/`` the note's ``origin:`` points at).
#   - ``_quarantine/`` — content that could NOT be safely processed (bad
#     encoding, missing handler, collision, ...). NEVER auto-deleted: it may
#     be the only copy. Instead it gets a non-destructive monthly triage
#     summary queued to ``hot.md`` (see ``quarantine_triage_summary`` /
#     ``render_quarantine_summary_hot_entry`` / ``quarantine_summary_due``).
# ---------------------------------------------------------------------------
DUPLICATE_RETENTION_DAYS_ENV = "BRAIN_DUPLICATE_RETENTION_DAYS"
DEFAULT_DUPLICATE_RETENTION_DAYS = 30
# Mirrors the sidecar convention ``ingest/pipeline.py``'s ``_process_claimed``
# writes alongside every parked duplicate (``<file>.duplicate-of.txt``).
_DUPLICATE_SIDECAR_SUFFIX = ".duplicate-of.txt"


def _sha256_file(path: Path) -> str:
    """Streaming sha256 (reuses snapshot's chunked helper — review finding [5]:
    a parked duplicate AND its archived original are BOTH hashed per aged
    candidate, and raw/originals are exactly the large binaries — never load a
    whole PDF/video into memory)."""
    from .snapshot import _sha256_file as _stream_sha256_file

    return _stream_sha256_file(path)


def _verify_duplicate_provenance(dup_file: Path, vault: Path, manifest: dict[str, str]) -> tuple[bool, str]:
    """The FULL provenance chain a parked duplicate must clear before it is
    ever deleted (HARDENED:codex — the naive "a manifest entry exists" guard
    is insufficient, see the module docstring above):

    1. hash the PARKED file itself (its bytes are the original bytes, moved
       verbatim into ``_duplicate/`` — never re-encoded);
    2. that hash must be a key in the ingest manifest (``sha256(original) ->
       raw-note-id``);
    3. resolve the manifest's target ``raw/<id>.md`` note;
    4. read ITS ``origin:`` frontmatter — the archived-original's path;
    5. confirm that archived file still exists under ``raw/originals/`` AND
       still hashes to the SAME original sha.

    Only step 5 passing proves the parked bytes are truly redundant. Returns
    ``(True, "verified")`` on success, else ``(False, <specific reason>)`` —
    every failure reason is precise enough to explain a skip without
    re-deriving it, since a failed chain is reported, never silently dropped."""
    try:
        dup_sha = _sha256_file(dup_file)
    except OSError as exc:
        return False, f"parked file unreadable: {exc}"
    existing_id = manifest.get(dup_sha)
    if existing_id is None:
        return False, "no ingest-manifest entry for this file's content hash"
    note_path = vault / "raw" / f"{existing_id}.md"
    if not note_path.is_file():
        return False, f"manifest target raw/{existing_id}.md does not exist"
    from . import frontmatter as fm

    try:
        meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — review finding [3]: a malformed raw
        # note (invalid UTF-8 = UnicodeDecodeError, or a YAML parse error — both
        # ValueError, not OSError) must SKIP this one candidate, never propagate
        # and abort the whole fold (which would starve pruning of every other
        # verifiable duplicate). Fail-safe: an unverifiable note is never pruned.
        return False, f"could not read/parse raw/{existing_id}.md: {exc}"
    origin = meta.get("origin")
    if not origin:
        return False, f"raw/{existing_id}.md has no origin: provenance"
    archive_path = vault / str(origin)
    if not archive_path.is_file():
        return False, f"archived original '{origin}' is missing"
    try:
        archive_sha = _sha256_file(archive_path)
    except OSError as exc:
        return False, f"could not hash archived original: {exc}"
    if archive_sha != dup_sha:
        return False, "archived original's hash no longer matches the parked duplicate"
    return True, "verified"


def retention_fold(vault: Path, today: datetime.date, *, dry_run: bool = False) -> dict[str, Any]:
    """CUT-02: prune ``inbox/_duplicate/`` entries older than
    ``BRAIN_DUPLICATE_RETENTION_DAYS`` (default 30, mtime-based). Safe by
    construction ONLY because every candidate is re-verified through
    ``_verify_duplicate_provenance`` right before deletion — a manifest hit
    is necessary but not sufficient (see the module docstring); anything
    that fails the chain is left in place and reported, never deleted on an
    unverified chain. Deletes the parked file + its ``.duplicate-of.txt``
    sidecar together, as one lot. Never touches ``inbox/_quarantine/`` (see
    ``quarantine_triage_summary`` for that lifecycle instead)."""
    import os
    import time

    from .ingest.pipeline import _load_manifest as _load_ingest_manifest

    # Review finding [1]: a non-integer $BRAIN_DUPLICATE_RETENTION_DAYS must not
    # raise (which would block the whole fold and silently disable pruning) —
    # fall back to the default on any bad value.
    try:
        retention_days = int(os.environ.get(
            DUPLICATE_RETENTION_DAYS_ENV, DEFAULT_DUPLICATE_RETENTION_DAYS))
    except (TypeError, ValueError):
        retention_days = DEFAULT_DUPLICATE_RETENTION_DAYS
    dup_dir = vault / "inbox" / "_duplicate"
    report: dict[str, Any] = {
        "retention_days": retention_days, "dry_run": dry_run,
        "considered": 0, "not_due": 0, "pruned": [], "skipped": [],
    }
    if not dup_dir.is_dir():
        return report

    manifest = _load_ingest_manifest(vault)
    cutoff_ts = time.mktime(today.timetuple()) - retention_days * 86400.0

    for p in sorted(dup_dir.iterdir()):
        if not p.is_file() or p.name.endswith(_DUPLICATE_SIDECAR_SUFFIX):
            continue  # sidecars are handled alongside their primary file
        report["considered"] += 1
        try:
            mtime = p.stat().st_mtime
        except OSError as exc:
            report["skipped"].append({"file": p.name, "kind": "stat", "reason": f"stat failed: {exc}"})
            continue
        if mtime > cutoff_ts:
            report["not_due"] += 1
            continue
        # Per-candidate isolation (review finding [3]): any unexpected error in
        # the verify chain skips THIS candidate, never aborts the fold.
        try:
            ok, reason = _verify_duplicate_provenance(p, vault, manifest)
        except Exception as exc:  # noqa: BLE001 — defensive; verify is fail-safe
            report["skipped"].append(
                {"file": p.name, "kind": "provenance", "reason": f"verify error: {exc}"})
            continue
        if not ok:
            report["skipped"].append({"file": p.name, "kind": "provenance", "reason": reason})
            continue
        if dry_run:
            report["pruned"].append({"file": p.name, "would_delete": True})
            continue
        # Delete the primary FIRST (the data-safe step, since provenance
        # verified the bytes are archived). Review finding [2]: only a PRIMARY
        # delete failure is a "not pruned" skip — a sidecar-unlink failure AFTER
        # the primary is gone must NOT be misreported as a provenance failure
        # (the file IS pruned); record the orphaned sidecar separately instead.
        sidecar = dup_dir / f"{p.name}{_DUPLICATE_SIDECAR_SUFFIX}"
        try:
            p.unlink()
        except OSError as exc:
            report["skipped"].append(
                {"file": p.name, "kind": "delete", "reason": f"delete failed: {exc}"})
            continue
        entry: dict[str, Any] = {"file": p.name}
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            entry["orphaned_sidecar"] = f"{sidecar.name}: {exc}"
        report["pruned"].append(entry)
    return report


def quarantine_summary_due(marker: dict[str, Any] | None, today: datetime.date) -> bool:
    """True when the monthly quarantine-triage summary is due: never fired,
    or last fired in an earlier calendar month than ``today`` — "due since
    last run" catch-up (mirrors every other monthly ``maintain`` branch),
    not a strict "only on the 1st" calendar check."""
    if not marker:
        return True
    return marker.get("last_month") != today.strftime("%Y-%m")


def quarantine_triage_summary(vault: Path, today: datetime.date | None = None) -> dict[str, Any]:
    """A non-destructive snapshot of ``inbox/_quarantine/`` — counts by
    reason (the reason subdirectory ``_quarantine()`` files each candidate
    into, see ``ingest/pipeline.py``'s ``_quarantine``), oldest item's age in
    days, and total size. Never deletes anything: a quarantined file may be
    the only copy of its content (that's exactly why it never reached
    ``raw/`` in the first place)."""
    import time

    qdir = vault / "inbox" / "_quarantine"
    result: dict[str, Any] = {
        "total": 0, "by_reason": {}, "oldest_age_days": None, "total_bytes": 0,
    }
    if not qdir.is_dir():
        return result

    now_ts = time.mktime(today.timetuple()) if today else time.time()
    oldest_mtime: float | None = None
    for reason_dir in sorted(p for p in qdir.iterdir() if p.is_dir()):
        count = 0
        for f in reason_dir.rglob("*"):
            if not f.is_file() or f.name.endswith(".reason.txt"):
                continue
            count += 1
            try:
                st = f.stat()
            except OSError:
                continue
            result["total_bytes"] += st.st_size
            if oldest_mtime is None or st.st_mtime < oldest_mtime:
                oldest_mtime = st.st_mtime
        if count:
            result["by_reason"][reason_dir.name] = count
            result["total"] += count
    if oldest_mtime is not None:
        result["oldest_age_days"] = max(0, int((now_ts - oldest_mtime) // 86400))
    return result


def render_quarantine_summary_hot_entry(summary: dict[str, Any], today: datetime.date) -> str:
    """The monthly quarantine-aging hot-queue entry — never silent, never a
    prompt to delete anything (see ``retention_fold``'s docstring for why
    quarantine is excluded from auto-pruning)."""
    lines = [f"## {today.isoformat()} — Monthly quarantine triage"]
    total = summary.get("total", 0)
    if not total:
        lines.append("- **Context:** `inbox/_quarantine/` is empty. Nothing to triage.")
        return "\n".join(lines) + "\n"
    size_mb = round(summary.get("total_bytes", 0) / (1024 * 1024), 1)
    lines.append(
        f"- **Context:** {total} quarantined file(s), {size_mb} MB total, "
        f"oldest {summary.get('oldest_age_days')} day(s) old."
    )
    for reason, count in sorted(summary.get("by_reason", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"  - `{reason}`: {count}")
    lines.append(
        "- **Owner input needed:** review `inbox/_quarantine/` and fix or "
        "discard each reason bucket by hand — quarantined files are NEVER "
        "auto-deleted; a file here may be the only copy of its content."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Self-organization folds (owner decision 2026-07-11): metadata, versioning,
# PARA zoning and navigation are AUTOMATIC nightly maintenance, not user
# input. Synthesis (writing new prose notes) remains session work — these
# folds only manage METADATA and generated views, never note bodies.
# ---------------------------------------------------------------------------
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|.+?)?\]\]")
_PARA_ZONES = ("projects", "areas", "resources", "archive")
_VERSION_ID_RE = re.compile(r"^(?P<base>.+?)-v(?P<num>\d{1,3})$")
_LEADING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def version_family_key(note_id: str) -> tuple[str, int] | None:
    """(family, version) for an id that names an explicit document version
    (``…-v12``), else None. The family key strips ONE leading capture-date
    prefix so re-captures of the same document line up
    (``2026-07-09-…-annex-v12`` and ``2026-05-27-…-annex-v10`` are one
    family). Deliberately conservative: only a trailing ``-v<digits>``
    counts — ``-v4-0``, ``-vf``, ``-vcomentada-26`` never chain."""
    m = _VERSION_ID_RE.match(note_id)
    if not m:
        return None
    base = _LEADING_DATE_RE.sub("", m.group("base"), count=1)
    return base, int(m.group("num"))


def auto_version_chains(core: Any) -> dict[str, Any]:
    """VER-01: stamp supersession chains across explicit version families.

    Groups indexed notes by ``version_family_key``, orders each family by
    (version, valid-date, id), and retires each predecessor via the AUDITED
    ``core.supersede`` path (both sides signed, journaled, invariant-checked
    — never a raw frontmatter poke). Idempotent: an already-retired
    predecessor is skipped; a predecessor already superseded by something
    OUTSIDE the computed chain (a human's manual call) freezes its family —
    reported, never overridden."""
    rows = core.index.conn.execute(
        "SELECT id, is_latest_version, superseded_by, "
        "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) "
        "FROM notes").fetchall()
    families: dict[str, list[tuple[int, str, str, dict[str, str]]]] = {}
    for nid, ilv, sup_by, vdate in rows:
        key = version_family_key(str(nid))
        if key is None:
            continue
        fam, num = key
        families.setdefault(fam, []).append(
            (num, str(vdate or ""), str(nid),
             {"is_latest": str(ilv or ""), "superseded_by": str(sup_by or "")}))

    report: dict[str, Any] = {"chained": [], "skipped_conflict": [], "errors": []}
    for fam, members in sorted(families.items()):
        if len(members) < 2:
            continue
        members.sort()
        ordered = [m[2] for m in members]
        meta = {m[2]: m[3] for m in members}
        # A manual chain pointing outside the computed order freezes the family.
        conflict = any(
            meta[nid]["superseded_by"] and meta[nid]["superseded_by"] != ordered[i + 1]
            for i, nid in enumerate(ordered[:-1]))
        if conflict:
            report["skipped_conflict"].append(fam)
            continue
        for old_id, new_id in zip(ordered[:-1], ordered[1:]):
            if meta[old_id]["superseded_by"] == new_id:
                continue  # already chained — idempotent re-run
            try:
                core.supersede(old_id, new_id, reason="auto version-chain (nightly self-organization)")
                report["chained"].append({"old": old_id, "new": new_id, "family": fam})
            except Exception as exc:  # noqa: BLE001 — one bad family never aborts the fold
                report["errors"].append({"family": fam, "old": old_id,
                                         "new": new_id, "error": str(exc)})
                break
    return report


def auto_para(vault: Path) -> dict[str, Any]:
    """PAR-01: file brain/ notes into their PARA zone by METADATA, not by a
    human dragging files. Two deliberately small rules:

    - ``type: project``          -> ``brain/projects/``
    - ``is_latest_version: false`` (retired by a supersession chain)
                                  -> ``brain/archive/``

    Generated views (``type: index``/``moc``) and everything else stay where
    they are. Moves are by-id-safe: wikilinks target ids, not paths, and the
    next index sync reconciles paths."""
    from . import frontmatter as fm

    brain_dir = vault / "brain"
    report: dict[str, Any] = {"moved": [], "errors": []}
    if not brain_dir.is_dir():
        return report
    for p in sorted(brain_dir.rglob("*.md")):
        if p.name in ("backlinks.md", "catalog.md", "index.md"):
            continue
        try:
            meta, _ = fm.parse_text(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"file": str(p), "error": str(exc)})
            continue
        ntype = str(meta.get("type") or "")
        if ntype in ("index", "moc"):
            continue
        retired = str(meta.get("is_latest_version")).lower() == "false"
        dest_zone = ("archive" if retired
                     else "projects" if ntype == "project" else None)
        if dest_zone is None or p.parent.name == dest_zone:
            continue
        dest_dir = brain_dir / dest_zone
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        if dest.exists():
            report["errors"].append({"file": str(p), "error": f"collision at {dest}"})
            continue
        p.rename(dest)
        report["moved"].append({"id": str(meta.get("id") or p.stem),
                                "to": f"brain/{dest_zone}/"})
    return report


def refresh_navigation(vault: Path) -> dict[str, Any]:
    """NAV-01: regenerate the human navigation surfaces nightly —
    ``brain/backlinks.md`` + one ``catalog.md`` per PARA zone. Byte-compatible
    with ``tools/validate.py --backlinks --catalogs`` (same formats, both
    deterministic, no wall-clock timestamps), so either producer yields a
    no-op diff over an unchanged vault."""
    from . import frontmatter as fm

    brain_dir = vault / "brain"
    raw_dir = vault / "raw"
    notes: list[dict[str, Any]] = []
    for base, zone in ((raw_dir, "raw"), (brain_dir, "brain")):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            if p.name in ("backlinks.md", "catalog.md"):
                continue
            try:
                meta, body = fm.parse_text(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — a broken note never kills navigation
                continue
            notes.append({"meta": meta, "body": body, "path": p, "zone": zone})

    ids = {n["meta"].get("id") for n in notes if n["meta"].get("id")}
    backlinks: dict[str, set[str]] = {}
    for n in notes:
        src = n["meta"].get("id")
        for m in _WIKILINK_RE.finditer(n["body"]):
            target = m.group(1).strip()
            if target in ids:
                backlinks.setdefault(target, set()).add(src)

    title_by_id = {n["meta"].get("id"): n["meta"].get("title", n["meta"].get("id"))
                   for n in notes}
    lines = [
        "---", "id: backlinks", "title: \"Backlinks (generated)\"",
        "type: index", "classification: Internal", "---", "",
        "# Backlinks (generated — do not hand-edit)", "",
    ]
    for tgt in sorted(backlinks):
        lines.append(f"## [[{tgt}]]")
        for s in sorted(x for x in backlinks[tgt] if x):
            lines.append(f"- [[{s}|{title_by_id.get(s, s)}]]")
        lines.append("")
    brain_dir.mkdir(parents=True, exist_ok=True)
    (brain_dir / "backlinks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    by_zone: dict[str, list[dict]] = {z: [] for z in _PARA_ZONES}
    for n in notes:
        if n["zone"] != "brain":
            continue
        rel = n["path"].relative_to(brain_dir).parts
        if len(rel) > 1 and rel[0] in by_zone:
            by_zone[rel[0]].append(n)
    for zone in _PARA_ZONES:
        zone_dir = brain_dir / zone
        zone_dir.mkdir(parents=True, exist_ok=True)
        cat = [
            "---", f"id: catalog-{zone}",
            f"title: \"{zone.capitalize()} catalog (generated)\"",
            "type: index", "classification: Internal", "---", "",
            f"# {zone.capitalize()} catalog (generated — do not hand-edit)", "",
            "| id | title | type | updated | classification |",
            "|---|---|---|---|---|",
        ]
        for n in sorted(by_zone[zone], key=lambda n: n["meta"].get("id") or ""):
            meta = n["meta"]
            cat.append(
                f"| [[{meta.get('id', '')}]] | {meta.get('title', '')} | "
                f"{meta.get('type', '')} | {meta.get('updated', '')} | {meta.get('classification', '')} |")
        cat.append("")
        (zone_dir / "catalog.md").write_text("\n".join(cat) + "\n", encoding="utf-8")

    return {"backlink_targets": len(backlinks),
            "catalog_counts": {z: len(by_zone[z]) for z in _PARA_ZONES}}


# ---------------------------------------------------------------------------
# Decision-capture nudge (DEC-01, 2026-07-11). Measured failure, G&P
# benchmark round 6: a real perimeter decision lived FIVE DAYS in a slide
# deck ("decided 6-Jul", "decision taken") without a `type: decision` note —
# so every decision-first agent was confidently stale, and the
# decision-layer-authoritative rule amplified the gap. This fold closes the
# loop: every maintain run scans RECENTLY captured non-decision notes for
# decision language and queues each hit ONCE to hot.md as a decision-note
# candidate. A nudge, not a writer — capturing the decision note stays
# owner/synthesis work (P-10 human gate), so false positives cost one
# hot-queue line, never a wrong decision record.
# ---------------------------------------------------------------------------
_DECISION_LANGUAGE_RE = re.compile(
    r"(?<![a-z])("
    r"decided(?:\s+on)?\s+\d|decided:\s|decision\s+(?:was\s+)?taken|"
    r"we\s+(?:have\s+)?decided|formally\s+approved|approved\s+on\s+\d|"
    r"signed\s+off\s+on|sign-off\s+given|"
    r"foi\s+decidido|decidiu-se|decis[aã]o\s+tomada|aprovado\s+em\s+\d"
    r")", re.IGNORECASE)
DECISION_CAPTURE_LOOKBACK_DAYS = 3
DECISION_CAPTURE_MAX_CANDIDATES = 10


def decision_capture_scan(
    conn: Any, today: datetime.date,
    lookback_days: int = DECISION_CAPTURE_LOOKBACK_DAYS,
    limit: int = DECISION_CAPTURE_MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Notes captured within ``lookback_days`` whose body carries decision
    language but whose ``type`` is not ``decision``. Returns at most
    ``limit`` candidates (id, date, phrase, snippet), newest first. Pure
    read — no writes, no egress concern (consumers queue to host-only
    hot.md)."""
    since = (today - datetime.timedelta(days=lookback_days)).isoformat()
    # Retired version-family members (is_latest_version: false) are excluded:
    # every sibling of a versioned deck repeats the same decision language —
    # only the family head is a meaningful capture candidate (live run
    # 2026-07-11: retired 6pager versions crowded the candidate cap).
    rows = conn.execute(
        "SELECT id, type, body, "
        "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) "
        "FROM notes WHERE type != 'decision' AND created >= ? "
        "AND COALESCE(is_latest_version,'') != 'false' "
        "ORDER BY created DESC", (since,)).fetchall()
    out: list[dict[str, Any]] = []
    for nid, ntype, body, vdate in rows:
        m = _DECISION_LANGUAGE_RE.search(body or "")
        if not m:
            continue
        start = max(0, m.start() - 80)
        snippet = " ".join((body[start:m.end() + 120]).split())
        out.append({"id": str(nid), "type": str(ntype or ""),
                    "date": str(vdate or ""), "phrase": m.group(0),
                    "snippet": snippet})
        if len(out) >= limit:
            break
    return out


def render_decision_capture_hot_entry(c: dict[str, Any], today: datetime.date) -> str:
    return "\n".join([
        f"## {today.isoformat()} — decision-capture candidate: `{c['id']}`",
        f"- **Context:** a freshly captured source (valid date {c.get('date') or '?'}) "
        f"carries decision language (“{c['phrase']}”) but no `type: decision` "
        f"note records it.",
        f"- **Snippet:** …{c['snippet']}…",
        "- **Owner input needed:** if this is a real decision, capture it as a "
        "`type: decision` note (and `brain supersede` whatever it reverses); "
        "if not, ignore — this entry never repeats for this note.",
    ]) + "\n"


# ---------------------------------------------------------------------------
# WATCHDOG-01 (2026-07-11): the two sanctioned scheduled tasks watch EACH
# OTHER, so a dead task is caught by the live one instead of by a human
# reading logs. Direction 1 (here): the hourly maintain umbrella checks the
# synthesis heartbeat (written by scripts/brain-synthesis.sh after every
# vault pass). Direction 2: the weekly synthesis session's prompt starts
# with `brain status`/`brain doctor`, which surface the maintain heartbeat.
# ---------------------------------------------------------------------------
SYNTHESIS_STATE_ENV = "BRAIN_SYNTHESIS_STATE"
SYNTHESIS_STALE_DAYS = 8  # weekly task + one day of grace


def _load_synthesis_entry(
    vault: Path, state_path: Path | None = None,
) -> tuple[Path, dict[str, Any] | None]:
    """Shared ``synthesis-state.json`` resolution + read + per-vault lookup
    (fix for review finding [8] — this state-file/read/lookup sequence was
    duplicated between ``synthesis_heartbeat_finding`` and
    ``latest_synthesis_cost``). Returns ``(resolved_path, entry)`` — entry is
    ``None`` on any absence/parse failure/missing-vault-entry (never
    raises); the path is still returned so a caller (the watchdog finding)
    can report where it looked."""
    import os

    path = state_path or Path(
        os.environ.get(SYNTHESIS_STATE_ENV, "")
        or Path.home() / ".brain" / "synthesis-state.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, None
    entry = state.get(str(vault))
    return path, (entry if isinstance(entry, dict) else None)


def synthesis_heartbeat_finding(
    vault: Path, today: datetime.date,
    state_path: Path | None = None,
    stale_days: int = SYNTHESIS_STALE_DAYS,
) -> dict[str, Any] | None:
    """An ``action_required`` finding when this vault's last SUCCESSFUL
    synthesis run is older than ``stale_days`` (or attempts keep failing) —
    ``None`` when healthy, unknown, or synthesis simply isn't set up here.

    Fail-quiet on absence by design: no state file means the synthesis task
    has never run on this host (not installed, or first week) — flagging
    that would nag every non-synthesis install forever. Once a heartbeat
    EXISTS for this vault, silence longer than the cadence is a real
    failure signal."""
    path, entry = _load_synthesis_entry(vault, state_path)
    if entry is None:
        return None
    last_ok = entry.get("last_success")
    last_try = entry.get("last_attempt")
    ref = last_ok or last_try
    if not ref:
        return None
    try:
        age = (today - datetime.date.fromisoformat(str(ref)[:10])).days
    except ValueError:
        return None
    failing = entry.get("rc", 0) != 0 and last_ok != last_try
    if age <= stale_days and not failing:
        return None
    what = (f"last SUCCESSFUL synthesis for this vault was {ref} "
            f"({age}d ago; cadence is weekly)" if not failing else
            f"synthesis attempts are FAILING (last attempt {last_try}, "
            f"rc={entry.get('rc')}; last success {last_ok or 'never'})")
    return action_required_item(
        f"brain-synthesis watchdog: {what}",
        "the weekly synthesis session keeps the state/MOC layer current; "
        "a silent death re-opens the decision-staleness gap the 2026-07 "
        "benchmark exposed",
        "check ~/.brain/logs/synthesis-*.log and `launchctl list "
        "com.brainiac.synthesis`; re-run scripts/brain-synthesis.sh manually "
        "to confirm the fix",
        str(path))


def latest_synthesis_cost(vault: Path, state_path: Path | None = None) -> float | None:
    """OBS-04 lift: the most recently METERED ``est_cost_usd`` for this vault
    from ``synthesis-state.json`` (written by ``scripts/brain-synthesis.sh``
    from the ``claude -p --output-format json`` structured usage stream —
    NEVER scraped human-readable text, per HARDENED correction 4: a scraped
    format drifts silently to a wrong zero). Absent, unparseable, or a bare
    ``0`` all record as ``None`` — a real cost of exactly zero is
    indistinguishable from "never measured" and the caller must not read a
    zero as a healthy trend point."""
    _, entry = _load_synthesis_entry(vault, state_path)
    if entry is None:
        return None
    cost = entry.get("est_cost_usd")
    return float(cost) if isinstance(cost, (int, float)) and cost > 0 else None


# ---------------------------------------------------------------------------
# WD-01 (2026-07-12) — off-host watchdog of last resort. If launchd itself
# dies (or its plists get wiped), the brain-nightly umbrella — and the
# synthesis-heartbeat check running INSIDE it (WATCHDOG-01, commit d28c0ce)
# — die with it. This EXTENDS that shipped pattern rather than rebuilding
# it: `offhost_watchdog_findings` reuses `synthesis_heartbeat_finding` and
# `health_trend` UNCHANGED, adding only the one check neither of those
# covers — "is `maintain` itself still firing at all".
#
# Freshness is keyed on the LATEST health-history record's `ts` (a precise
# ISO datetime, written every maintain run), never on `maintain-state.json`'s
# `last_run` (an ISO DATE only — it advances in whole-day steps, so it
# cannot express a >26h threshold: the first value it could ever cross is
# 48h). This is the correction that makes an hourly-cadence watchdog
# meaningful at sub-day granularity.
#
# LOCAL-first (owner decision 2026-07-12): the off-host CLOUD leg (a Claude
# `/schedule` routine reading this remotely, weekly) is DEFERRED. Verified
# (not assumed) via the `schedule` skill's own documentation: a `/schedule`
# cloud routine "cannot access local files, local services, or local
# environment variables" — there is no remote-export transport yet to get
# this vault's local state to it. What ships now is the LOCAL check +
# macOS-push failsafe (the SAME `fire_notification` osascript channel
# OBS-02 already uses — deliberately not a new outbound/remote channel).
# See docs/operations/wd01-offhost-watchdog-spec.md.
# ---------------------------------------------------------------------------
OFFHOST_DAILY_STALE_HOURS_ENV = "BRAIN_OFFHOST_DAILY_STALE_HOURS"
DEFAULT_OFFHOST_DAILY_STALE_HOURS = 26


def _union_by_run_id(*record_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union health records across the main history + sparse sidecar, deduped
    by ``run_id`` (falling back to ``ts`` then object identity). Later lists
    win on a key collision — callers pass ``(history, sparse)`` so a sidecar
    record supersedes the same-id main-history one. Fix [8]: the ONE
    union-dedup both the off-host watchdog's freshness check and
    ``health_trend``'s golden lookback share, instead of two copies."""
    merged: dict[str, dict[str, Any]] = {}
    for r in itertools.chain.from_iterable(record_lists):
        rid = str(r.get("run_id") or r.get("ts") or id(r))
        merged[rid] = r
    return list(merged.values())


def offhost_watchdog_findings(
    vault: Path, now: datetime.datetime | None = None,
    *, daily_stale_hours: float | None = None,
) -> list[str]:
    """Human-readable breach strings for THIS vault — an empty list means
    healthy (silent); any entry means fire the failsafe notification.

    The failsafe's PURPOSE is "maintain/health is DEAD" — so it is scoped to
    STALENESS ONLY (fix [4]); the transient blocked/regression fold was
    DROPPED because that is OBS-02's on-host job and folding it here fired an
    un-deduped, hourly-re-firing notification that duplicated the on-host
    alarm. Two independent staleness checks (one absent signal never hides
    another):

    1. **Heartbeat freshness** — the latest health-history record (main +
       sparse union) is older than ``daily_stale_hours`` (default 26h — an
       hourly cadence + grace). NO history at all (fresh install, or
       genuinely never run) is SILENT, not a breach — mirrors
       `synthesis_heartbeat_finding`'s fail-quiet-on-absence: nothing to
       watch yet is not "went quiet". If NO record has a parseable ``ts`` at
       all, freshness cannot be determined — that IS a breach ("cannot
       determine health freshness"; fix [6]), never silent-healthy. When some
       records parse, the newest VALID record drives freshness: a lone
       corrupt/lexically-large ts cannot be mistaken for "latest" and mask a
       fresh valid one (re-review [825]), and a PERSISTENT corruption ages the
       youngest valid record past the window so it still breaches on staleness.
    2. **Synthesis heartbeat** — delegates to `synthesis_heartbeat_finding`
       (WATCHDOG-01) UNCHANGED — itself a staleness (last-success-too-old)
       check.
    """
    import os as _os

    now = now or datetime.datetime.now(datetime.timezone.utc)
    stale_hours = daily_stale_hours if daily_stale_hours is not None else float(
        _os.environ.get(OFFHOST_DAILY_STALE_HOURS_ENV, DEFAULT_OFFHOST_DAILY_STALE_HOURS))
    findings: list[str] = []

    merged = _union_by_run_id(read_health_history(vault), read_sparse_history(vault))
    if merged:
        # Pick the newest record by PARSED ts, not a lexical max over the raw
        # strings (re-review): a corrupt but lexically-large ts on a NON-newest
        # record ("2026-07-12T25:00:00Z") would otherwise be selected as
        # "latest" and force a false freshness breach while a genuinely fresh
        # valid record exists. Parse every ts; the newest VALID record drives
        # freshness. Only if NO record has a parseable ts is it a breach.
        parsed = []
        for r in merged:
            try:
                parsed.append((datetime.datetime.strptime(
                    str(r.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=datetime.timezone.utc), r))
            except ValueError:
                continue
        if not parsed:
            findings.append(
                f"{vault}: no health-history record has a parseable ts — cannot "
                f"determine health freshness (a breach, not silent-healthy)")
        else:
            latest_dt, latest = max(parsed, key=lambda t: t[0])
            age_hours = (now - latest_dt).total_seconds() / 3600.0
            if age_hours > stale_hours:
                findings.append(
                    f"{vault}: no health-history record in {age_hours:.1f}h "
                    f"(> {stale_hours:.0f}h) — the hourly maintain umbrella "
                    f"(and whatever schedules it) may have died")

    synth = synthesis_heartbeat_finding(vault, now.date())
    if synth is not None:
        findings.append(f"{vault}: {synth['finding']}")

    return findings


# ---------------------------------------------------------------------------
# OBS-01 — health-metrics history (health-history.jsonl). Every ``maintain``
# run appends ONE record (schema below) so trend questions ("worse than last
# week?") have something to answer from instead of amnesia. HARDENED
# corrections applied throughout (see s02 context bundle):
#   1. time-based (7-CALENDAR-DAY) baseline for high-frequency metrics, never
#      "last 7 records" (that's ~7 hours on an hourly cadence); sparse weekly
#      metrics (golden_score, synthesis_cost_usd) compare against the
#      trailing non-null observation regardless of window.
#   2. ONE final immutable append per run, built from a single run-context
#      object (``results``) that a later branch (s07's golden-eval fold) can
#      still fold into via ``results["golden"] = {...}`` before this append —
#      never a JSONL line rewrite.
#   3. concurrency-safe append+rotation under a DEDICATED short-lived lock
#      (the coarse 2h maintain-lock can legitimately let two runs overlap);
#      monotonically-named archive segments; every record carries a unique
#      ``run_id`` the reader dedups on, tolerating one trailing partial line.
#   4. every new maintain-state marker this session touches is ``_``-prefixed
#      (core.py:1973-1977 treats a bare key as a due-branch name); cost is
#      metered from the structured usage stream only.
#   5. a PER-METRIC daily-bucket reducer (never one generic "representative")
#      so a single-hour blocked/latency spike survives bucketing.
# ---------------------------------------------------------------------------
HEALTH_HISTORY_MAX_BYTES_ENV = "BRAIN_HEALTH_HISTORY_MAX_BYTES"
DEFAULT_HEALTH_HISTORY_MAX_BYTES = 1_000_000
HEALTH_HISTORY_LOCK_STALE_SECONDS = 30.0
# Fix [6]: bound the archive re-read + add retention pruning. 14 days
# comfortably covers health_trend's 7-day trailing baseline plus a weekly
# sparse-metric (golden_score/synthesis_cost_usd) lookback; retention is a
# much longer, separate knob (mirrors scripts/brain-synthesis.sh's
# `find -mtime +N -delete` posture for its own out-json captures).
HEALTH_HISTORY_READ_WINDOW_DAYS_ENV = "BRAIN_HEALTH_HISTORY_READ_WINDOW_DAYS"
DEFAULT_HEALTH_HISTORY_READ_WINDOW_DAYS = 14
HEALTH_ARCHIVE_RETENTION_DAYS_ENV = "BRAIN_HEALTH_ARCHIVE_RETENTION_DAYS"
DEFAULT_HEALTH_ARCHIVE_RETENTION_DAYS = 90


def new_health_run_id() -> str:
    """A short, unique-enough id stamped on every health-history record so a
    reader merging the live file + rotated archives can dedup instead of
    double-counting a record two racing writers might otherwise both see."""
    import time
    import uuid

    return f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:8]}"


def _count_files(dir_path: Path) -> int:
    """Recursive file count under ``dir_path`` — 0 if it does not exist yet
    (a fresh vault has no ``_quarantine``/``_duplicate`` dir at all)."""
    if not dir_path.is_dir():
        return 0
    return sum(1 for p in dir_path.rglob("*") if p.is_file())


def collect_health_metrics(
    core: Any, *, outcomes: dict[str, Any], results: dict[str, Any],
    run_id: str, ts: str | None = None,
) -> dict[str, Any]:
    """Build ONE health-history record (schema in the s02 context bundle) from
    already-computed run state. ``results`` is the SAME run-context dict
    ``BrainCore.maintain`` accumulates branch outputs into — this is the
    "structured partial-result hook" a later golden-eval branch (s07) folds
    into: it need only set ``results["golden"] = {"score": ...}`` before this
    is called, no JSONL rewrite required. Never raises on a missing piece —
    every field degrades to ``None`` rather than aborting the run's own
    health-history append."""
    import datetime as _dt
    import time as _time

    status: dict[str, Any] = {}
    try:
        status = core.status()
    except Exception:  # noqa: BLE001 — a broken status() must not break history
        status = {}
    idx = status.get("index") if isinstance(status.get("index"), dict) else {}
    snap = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}

    selftest_ms: float | None = None
    try:
        t0 = _time.perf_counter()
        core.hybrid_search("brain", k=1)
        selftest_ms = round((_time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001 — probe failure is just a null latency point
        selftest_ms = None

    vault = Path(core.vault)
    counts = outcomes.get("counts", {}) if isinstance(outcomes.get("counts"), dict) else {}
    decision_candidates = None
    dc = results.get("decision_capture")
    if isinstance(dc, dict):
        decision_candidates = dc.get("candidates")
    golden = results.get("golden") if isinstance(results.get("golden"), dict) else {}

    return {
        "ts": ts or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "notes": idx.get("notes") if isinstance(idx, dict) else None,
        "chunks": idx.get("chunks") if isinstance(idx, dict) else None,
        "snapshot_gen": snap.get("generation") if isinstance(snap, dict) else None,
        "snapshot_age_s": snap.get("age_seconds") if isinstance(snap, dict) else None,
        "quarantine": _count_files(vault / "inbox" / "_quarantine"),
        "duplicate": _count_files(vault / "inbox" / "_duplicate"),
        "selftest_ms": selftest_ms,
        "action_required": counts.get("action_required", 0),
        "blocked": counts.get("blocked", 0),
        "decision_candidates": decision_candidates,
        "golden_score": golden.get("score"),
        "synthesis_cost_usd": latest_synthesis_cost(vault),
    }


def _acquire_health_history_lock(
    lock_path: Path, *, stale_after: float = HEALTH_HISTORY_LOCK_STALE_SECONDS,
) -> None:
    """Best-effort exclusive lock scoped ONLY to the tiny append+rotate
    critical section (correction 3) — deliberately separate from
    ``BrainCore._acquire_maintain_lock``: that lock's 2h auto-break lets two
    ``maintain`` runs overlap by design, so append/rotation needs its own
    much-shorter-lived lock or two overlapping runs could both decide to
    rotate onto the same archive name. Blocks briefly (busy-wait), self-heals
    a lock older than ``stale_after`` (a crash mid-critical-section), and
    never blocks indefinitely."""
    import os
    import time

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode("ascii"))
            os.close(fd)
            return
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = stale_after + 1
            if age > stale_after:
                lock_path.unlink(missing_ok=True)
                continue
            time.sleep(0.05)


def _release_health_history_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def _rotate_health_history(path: Path, archive_dir: Path) -> str:
    """Move the current file to a MONOTONICALLY-NAMED, create-exclusive
    archive segment — never overwrites an existing segment even if two
    rotations somehow land in the same millisecond."""
    import os
    import time

    archive_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1000):
        stamp = f"{int(time.time() * 1000):x}-{attempt:03d}"
        dest = archive_dir / f"health-history-{stamp}.jsonl"
        try:
            fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            continue
        os.replace(path, dest)
        return str(dest)
    raise RuntimeError("could not allocate a unique health-history archive segment")


def _prune_old_files(dir_path: Path, pattern: str, retention_days: int) -> None:
    """Delete files under ``dir_path`` matching ``pattern`` whose mtime is
    older than ``retention_days``. Best-effort: a file that vanishes mid-scan
    or resists deletion is skipped, never raised. Mirrors
    ``scripts/brain-synthesis.sh``'s ``find -mtime +N -delete`` posture. One
    shared implementation for the two near-identical mtime pruners this
    session added — the health-archive and the notify-marker cleanups (review
    finding [8])."""
    import time

    if not dir_path.is_dir():
        return
    cutoff = time.time() - retention_days * 86400.0
    for p in dir_path.glob(pattern):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def _prune_health_archive(archive_dir: Path, retention_days: int) -> None:
    """Delete rotated ``health-history-*.jsonl`` segments older than
    ``retention_days`` (fix [6] retention companion)."""
    _prune_old_files(archive_dir, "health-history-*.jsonl", retention_days)


def append_health_record(
    vault: Path, record: dict[str, Any], *, max_bytes: int | None = None,
    archive_retention_days: int | None = None,
) -> dict[str, Any]:
    """Append ONE JSONL record under the dedicated health-history lock,
    rotating to an archive segment first if the live file would cross
    ``max_bytes`` (~1MB default, env-overridable). Never raises past a
    caller — a health-history write failure is reported by the caller as a
    ``blocked`` item, never allowed to fail the whole maintain run.

    Also prunes archive segments past ``archive_retention_days`` (default
    90, env-overridable — fix [6]) every call: cheap (a small glob under the
    lock already held) and keeps the archive dir from growing forever."""
    import os

    from . import config as _config

    limit = max_bytes if max_bytes is not None else int(
        os.environ.get(HEALTH_HISTORY_MAX_BYTES_ENV, DEFAULT_HEALTH_HISTORY_MAX_BYTES))
    retention = archive_retention_days if archive_retention_days is not None else int(
        os.environ.get(HEALTH_ARCHIVE_RETENTION_DAYS_ENV, DEFAULT_HEALTH_ARCHIVE_RETENTION_DAYS))
    path = _config.health_history_path(vault)
    archive_dir = _config.health_archive_dir(vault)
    lock_path = _config.health_history_lock_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)

    _acquire_health_history_lock(lock_path)
    try:
        line = json.dumps(record, sort_keys=True)
        rotated = None
        if path.is_file() and path.stat().st_size + len(line) + 1 > limit:
            rotated = _rotate_health_history(path, archive_dir)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _append_sparse_metrics(_config.health_sparse_path(vault), record)
        _prune_health_archive(archive_dir, retention)
        return {"appended": True, "rotated": rotated}
    finally:
        _release_health_history_lock(lock_path)


# The sidecar mirrors ONLY the GENUINELY sparse metric — ``golden_score``,
# which is null on every record until the (quarterly-cadence) golden-eval
# branch produces one. ``synthesis_cost_usd`` is deliberately NOT here: it is
# the PERSISTED last metered cost (``latest_synthesis_cost``), non-null on
# every hourly record once synthesis has run once — mirroring it would grow
# the never-rotated sidecar unbounded (review finding [0]), and nothing
# trend-compares it anyway (only ``golden_score`` has a sparse check).
SPARSE_METRICS = ("golden_score",)


def _append_sparse_metrics(sparse_path: Path, record: dict[str, Any]) -> None:
    """Mirror a record's non-null sparse metrics into the never-rotated
    sidecar (review finding [7]). No-op when the record carries none — so the
    sidecar only ever gains a line on the golden-eval (quarterly) cadence.
    Called under the same append lock as the main history write; BEST-EFFORT
    (review finding [2]): a sidecar write failure must NOT propagate and fail
    the main history append that already succeeded — the golden point also
    lives in the main record, and ``health_trend`` unions the two sources."""
    sparse = {k: record.get(k) for k in SPARSE_METRICS if record.get(k) is not None}
    if not sparse:
        return
    sparse["ts"] = record.get("ts")
    sparse["run_id"] = record.get("run_id")
    try:
        sparse_path.parent.mkdir(parents=True, exist_ok=True)
        with sparse_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sparse, sort_keys=True) + "\n")
    except OSError:
        _log.warning("[health] sparse sidecar append failed (main record kept): %s",
                     sparse_path)


def read_sparse_history(vault: Path) -> list[dict[str, Any]]:
    """Full (never-windowed) sparse-metric history from the sidecar — tiny by
    construction (review finding [7]). De-duplicated by ``run_id`` and sorted
    by ``ts``; tolerant of a trailing partial line. Empty list when the
    sidecar does not exist yet."""
    from . import config as _config

    path = _config.health_sparse_path(vault)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: dict[str, dict[str, Any]] = {}
    for rec in parse_recommendation_lines(text):
        key = rec.get("run_id") or f"__no_run_id__{rec.get('ts')}"
        records[key] = rec
    return sorted(records.values(), key=lambda r: str(r.get("ts") or ""))


def read_health_history(
    vault: Path, *, window_days: int | None = None,
) -> list[dict[str, Any]]:
    """Merge the live ``health-history.jsonl`` with RECENT rotated archive
    segments — bounded to the last ``window_days`` by file mtime (default
    14, env-overridable via ``$BRAIN_HEALTH_HISTORY_READ_WINDOW_DAYS``; fix
    [6] — re-reading and re-parsing EVERY archive segment on every hourly
    run does not scale as segments accumulate). De-duplicated by ``run_id``
    and sorted by ``ts``. Read-only — safe to call from ``health_trend`` on
    every run without touching state. The live file is always included
    regardless of age (it is small until it next rotates)."""
    import os
    import time

    from . import config as _config

    win = window_days if window_days is not None else int(
        os.environ.get(HEALTH_HISTORY_READ_WINDOW_DAYS_ENV, DEFAULT_HEALTH_HISTORY_READ_WINDOW_DAYS))
    cutoff = time.time() - win * 86400.0

    records: dict[str, dict[str, Any]] = {}
    paths: list[Path] = []
    archive_dir = _config.health_archive_dir(vault)
    if archive_dir.is_dir():
        for p in sorted(archive_dir.glob("health-history-*.jsonl")):
            try:
                if p.stat().st_mtime >= cutoff:
                    paths.append(p)
            except OSError:
                continue
    live = _config.health_history_path(vault)
    if live.is_file():
        paths.append(live)
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for rec in parse_recommendation_lines(text):
            key = rec.get("run_id") or f"__no_run_id__{p}__{rec.get('ts')}"
            records[key] = rec
    return sorted(records.values(), key=lambda r: str(r.get("ts") or ""))


DEFAULT_LATENCY_REGRESSION_PCT = 0.50
DEFAULT_QUARANTINE_REGRESSION_PCT = 0.25
DEFAULT_GOLDEN_REGRESSION_PCT = 0.05
HEALTH_TREND_MIN_DAYS = 7
HEALTH_TREND_MIN_BASELINE_DAYS = 2

# Correction 5 — per-metric daily-bucket reducer. A single generic
# "representative" (e.g. always "last") would average/suppress a real
# single-hour spike; each metric family gets the reducer that keeps that
# spike visible after bucketing hourly records into one-per-day.
_DAILY_REDUCERS: dict[str, str] = {
    "notes": "last", "chunks": "last",
    "snapshot_gen": "last", "snapshot_age_s": "last",
    "quarantine": "last", "duplicate": "last", "decision_candidates": "last",
    "selftest_ms": "median",
    "blocked": "max", "action_required": "max",
    "golden_score": "last_non_null", "synthesis_cost_usd": "last_non_null",
}


def _record_date(rec: dict[str, Any]) -> datetime.date | None:
    ts = str(rec.get("ts") or "")
    try:
        return datetime.date.fromisoformat(ts[:10])
    except ValueError:
        return None


def _bucket_daily(history: list[dict[str, Any]], metric: str) -> dict[datetime.date, Any]:
    """One representative value per calendar day for ``metric``, per its
    schema reducer (see ``_DAILY_REDUCERS``)."""
    reducer = _DAILY_REDUCERS.get(metric, "last")
    per_day: dict[datetime.date, list[Any]] = {}
    for rec in history:
        d = _record_date(rec)
        if d is None:
            continue
        per_day.setdefault(d, []).append(rec.get(metric))

    out: dict[datetime.date, Any] = {}
    for d, values in per_day.items():
        if reducer == "median":
            nums = sorted(x for x in values if isinstance(x, (int, float)))
            if not nums:
                v = None
            else:
                mid = len(nums) // 2
                v = nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2
        elif reducer == "max":
            nums = [x for x in values if isinstance(x, (int, float))]
            v = max(nums) if nums else None
        elif reducer == "last_non_null":
            v = next((x for x in reversed(values) if x is not None), None)
        else:  # "last" — gauge counts (end-of-day snapshot)
            v = values[-1]
        out[d] = v
    return out


def health_trend(
    history: list[dict[str, Any]], today: datetime.date, *,
    sparse_history: list[dict[str, Any]] | None = None,
    latency_regression_pct: float | None = None,
    quarantine_regression_pct: float | None = None,
    golden_regression_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Week-over-week regression findings. Each finding is
    ``{metric, severity, current, baseline, delta_pct, summary}``.

    - ``blocked`` fires immediately from the LATEST record alone (no
      baseline needed — any blocked>0 is already actionable).
    - ``selftest_ms``/``quarantine`` (high-frequency, appended every hourly
      run) are daily-bucketed and compared against a trailing-median
      baseline, but ONLY once >= ``HEALTH_TREND_MIN_DAYS`` calendar days of
      history exist AND the baseline has >= ``HEALTH_TREND_MIN_BASELINE_DAYS``
      non-null days — otherwise these two checks silently skip (never a
      false regression from a too-thin history; correction 1).
    - ``golden_score`` (sparse — null on nearly every hourly record) compares
      the latest non-null value against the PREVIOUS non-null value
      regardless of window/day-count (correction 1). A null on either side
      skips the check — a null is "absent", never a "-100%" drop.
    """
    import os

    lat_pct = latency_regression_pct if latency_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_LATENCY_REGRESSION_PCT", DEFAULT_LATENCY_REGRESSION_PCT))
    quar_pct = quarantine_regression_pct if quarantine_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_QUARANTINE_REGRESSION_PCT", DEFAULT_QUARANTINE_REGRESSION_PCT))
    gold_pct = golden_regression_pct if golden_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_GOLDEN_REGRESSION_PCT", DEFAULT_GOLDEN_REGRESSION_PCT))

    findings: list[dict[str, Any]] = []
    if not history:
        return findings

    ordered = sorted(history, key=lambda r: str(r.get("ts") or ""))

    latest = ordered[-1]
    blocked_now = latest.get("blocked")
    if isinstance(blocked_now, (int, float)) and blocked_now > 0:
        findings.append({
            "metric": "blocked", "severity": "regression",
            "current": blocked_now, "baseline": 0, "delta_pct": None,
            "summary": f"{int(blocked_now)} blocked finding(s) in the latest maintain run",
        })

    dates_present = sorted({d for r in ordered if (d := _record_date(r)) is not None})
    span_ok = bool(dates_present) and (today - dates_present[0]).days >= HEALTH_TREND_MIN_DAYS

    def _high_freq(metric: str, pct: float, label: str) -> None:
        buckets = _bucket_daily(ordered, metric)
        if not buckets:
            return
        days_sorted = sorted(buckets)
        current = buckets[days_sorted[-1]]
        baseline_vals = [buckets[d] for d in days_sorted[:-1] if buckets[d] is not None]
        if current is None or not span_ok or len(baseline_vals) < HEALTH_TREND_MIN_BASELINE_DAYS:
            return
        base_sorted = sorted(baseline_vals)
        mid = len(base_sorted) // 2
        baseline = (base_sorted[mid] if len(base_sorted) % 2
                    else (base_sorted[mid - 1] + base_sorted[mid]) / 2)
        if not baseline:  # 0 or None — a % delta off a zero baseline is meaningless
            return
        delta = (current - baseline) / baseline
        if delta > pct:
            findings.append({
                "metric": metric, "severity": "regression",
                "current": current, "baseline": baseline,
                "delta_pct": round(delta * 100, 1),
                "summary": f"{label}: {current} vs trailing baseline {baseline} "
                           f"(+{round(delta * 100, 1)}%, threshold +{round(pct * 100)}%)",
            })

    _high_freq("selftest_ms", lat_pct, "search self-test latency regressed")
    _high_freq("quarantine", quar_pct, "quarantine growth")

    # Fix [7]+[2]: draw golden observations from the UNION of the never-windowed
    # sparse sidecar AND the windowed main history, deduped by run_id. The
    # sidecar carries points that have aged out of the 14-day window (golden
    # scores land quarterly); the main history covers a point a transient
    # sidecar-write failure (finding [2]) dropped that is still in-window. Only
    # a point BOTH absent from the sidecar AND aged out of the window is lost —
    # a double fault on a quarterly metric.
    golden_records = [r for r in _union_by_run_id(ordered, sparse_history or [])
                      if r.get("golden_score") is not None]
    golden_points = [r["golden_score"]
                     for r in sorted(golden_records, key=lambda r: str(r.get("ts") or ""))]
    if len(golden_points) >= 2:
        prev_score, cur_score = golden_points[-2], golden_points[-1]
        if (isinstance(prev_score, (int, float)) and isinstance(cur_score, (int, float))
                and prev_score):
            delta = (cur_score - prev_score) / prev_score
            if delta < -gold_pct:
                findings.append({
                    "metric": "golden_score", "severity": "regression",
                    "current": cur_score, "baseline": prev_score,
                    "delta_pct": round(delta * 100, 1),
                    "summary": f"golden retrieval score regressed: {cur_score} vs "
                               f"previous {prev_score} ({round(delta * 100, 1)}%, "
                               f"threshold -{round(gold_pct * 100)}%)",
                })

    return findings


# ---------------------------------------------------------------------------
# OBS-02 — macOS degradation alarm. A trend regression, a tripped watchdog
# finding, or blocked>0 fires a single osascript notification per finding per
# day (dedup marker under ``.brain/notify-sent/``); ``BRAIN_NOTIFY=off`` kills
# it; non-macOS degrades to a log-only no-op (the Windows installer path is a
# separate, already-covered surface — WD-01's cloud push is the other leg).
# ---------------------------------------------------------------------------
NOTIFY_ENV = "BRAIN_NOTIFY"
NOTIFY_MARKER_RETENTION_DAYS_ENV = "BRAIN_NOTIFY_MARKER_RETENTION_DAYS"
DEFAULT_NOTIFY_MARKER_RETENTION_DAYS = 30


def degradation_findings(
    outcomes: dict[str, Any], trend_findings: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """``(dedup_key, display_text)`` pairs, in priority order: blocked>0 (from
    ``outcomes`` — the ritual-level count, distinct from ``health_trend``'s
    own per-record ``blocked`` metric check so a blocked item is never
    reported twice), the synthesis watchdog (if tripped this run), then every
    trend regression. Pure — no I/O, no dedup bookkeeping (that's
    ``should_notify``).

    The KEY is a STABLE per-finding-identity string (``"blocked"``,
    ``"synthesis-watchdog"``, ``"trend:<metric>"``); the TEXT is the
    human-readable notification body, which DELIBERATELY carries the
    fluctuating measured values (``540ms vs baseline 480ms``). Review finding
    [1]: the dedup marker must hash the KEY, never the value-bearing text —
    otherwise the same ongoing regression, whose daily-median ``current``
    shifts each hourly run, hashes a different marker every hour and re-fires
    a fresh notification hourly, defeating "one notification per finding per
    day."

    Fix [3]: ``health_trend`` ALSO appends its own ``metric: "blocked"`` entry
    to ``trend_findings`` whenever the latest record shows blocked>0 — that
    entry is skipped here so the SAME blocked condition never produces two
    findings in one run."""
    pairs: list[tuple[str, str]] = []
    blocked = (outcomes.get("counts") or {}).get("blocked", 0)
    if blocked:
        pairs.append(("blocked", f"{blocked} blocked finding(s) this maintain run"))
    for item in outcomes.get("action_required") or []:
        finding = str(item.get("finding", ""))
        if finding.startswith("brain-synthesis watchdog:"):
            pairs.append(("synthesis-watchdog", finding))
    for f in trend_findings:
        metric = str(f.get("metric") or "")
        if metric == "blocked":
            continue  # already reported via outcomes.counts above
        text = str(f.get("summary") or f"{metric} regression")
        pairs.append((f"trend:{metric}", text))
    return pairs


def _notify_marker_dir(vault: Path) -> Path:
    from . import config as _config

    return _config.brain_runtime_dir(vault) / "notify-sent"


def _notify_marker_path(vault: Path, key: str, today: datetime.date) -> Path:
    """Marker path for a STABLE dedup ``key`` (not the value-bearing display
    text — review finding [1]). One marker per key per day."""
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _notify_marker_dir(vault) / f"{today.isoformat()}-{digest}.marker"


def _prune_notify_markers(vault: Path, retention_days: int | None = None) -> None:
    """Delete per-day dedup markers older than ``retention_days`` (default
    30, env-overridable — fix [7]): one marker file accumulates per unique
    finding per day forever otherwise. Best-effort, never raises."""
    import os

    retention = retention_days if retention_days is not None else int(
        os.environ.get(NOTIFY_MARKER_RETENTION_DAYS_ENV, DEFAULT_NOTIFY_MARKER_RETENTION_DAYS))
    _prune_old_files(_notify_marker_dir(vault), "*.marker", retention)


def should_notify(vault: Path, key: str, today: datetime.date) -> bool:
    """True iff this ``key`` has NOT yet been surfaced today. PURE READ — no
    marker write (review finding [1]: the check used to write the marker
    eagerly). Pair with ``mark_notified``."""
    return not _notify_marker_path(vault, key, today).exists()


def mark_notified(vault: Path, key: str, today: datetime.date) -> str:
    """ATOMICALLY claim the per-day dedup marker for ``key`` via an
    ``O_CREAT | O_EXCL`` create. Returns:

    - ``"claimed"`` — this call created the marker (we own the notification);
    - ``"exists"``  — the marker already existed, so an earlier run today (or a
      concurrently-overlapping maintain) already surfaced this key;
    - ``"unwritable"`` — the marker dir could not be written.

    The exclusive create closes the check-then-write TOCTOU (review finding
    [1]): the coarse 2h maintain-lock auto-break can let two maintains overlap
    (the health-history append is locked for exactly this reason), and a
    plain ``should_notify`` read + later write let both fire the same banner.
    Now only ONE overlapping run wins the create; the other sees ``"exists"``
    and skips.

    ``"unwritable"`` is best-effort (review finding [1] sibling): on a
    read-only/full ``.brain`` dedup state cannot be persisted by ANY design —
    the caller still fires (the finding must not be lost) and it may re-surface
    next run until the dir recovers. The durable WARNING ``[degradation]`` log
    line fires regardless, so the finding is never lost."""
    import os

    marker = _notify_marker_path(vault, key, today)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return "exists"
    except OSError:
        _log.warning("[degradation] dedup marker unwritable (%s); "
                     "finding may re-surface next run", marker.parent)
        return "unwritable"
    try:
        os.write(fd, key.encode("utf-8"))
    finally:
        os.close(fd)
    return "claimed"


def fire_notification(text: str, *, title: str = "Brainiac health") -> str:
    """Best-effort ``osascript`` notification on macOS. Returns
    ``"skipped (non-macOS)"`` off Darwin and ``"failed: …"`` on a send error,
    never raises — a GUI ping is pure convenience on top of the durable log
    line the caller always emits. Slowing or failing the maintain run over a
    notification is never acceptable."""
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return "skipped (non-macOS)"
    try:
        result = subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(text)} with title {json.dumps(title)}'],
            check=False, capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            return "failed: osascript rc={}".format(result.returncode)
        return "notified"
    except Exception as exc:  # noqa: BLE001 — a notification failure is cosmetic
        return f"failed: {type(exc).__name__}"


def pending_notifications(
    vault: Path, outcomes: dict[str, Any], trend_findings: list[dict[str, Any]],
    today: datetime.date,
) -> list[tuple[str, str]]:
    """The ``(key, text)`` findings still pending notification for TODAY —
    empty when ``BRAIN_NOTIFY=off`` or when every candidate was already
    surfaced today (dedup by stable KEY). Pure read (does not mark) — pair
    with ``fire_and_mark_notifications``. Also opportunistically prunes old
    markers (fix [7]) since this is called once per maintain run."""
    import os

    _prune_notify_markers(vault)
    if os.environ.get(NOTIFY_ENV, "").strip().lower() == "off":
        return []
    return [(k, t) for (k, t) in degradation_findings(outcomes, trend_findings)
            if should_notify(vault, k, today)]


def fire_and_mark_notifications(
    vault: Path, findings: list[tuple[str, str]], today: datetime.date,
) -> list[str]:
    """Surface each pending ``(key, text)`` finding, then persist its per-day
    dedup marker. Returns the display texts surfaced this call.

    Two review fixes converge here:

    - **Fix [4] / durable half of [2]:** a WARNING log line is emitted for
      every finding FIRST, independent of the GUI channel and platform, so a
      failed ``osascript`` (headless/non-Aqua launchd) or a non-macOS host
      never means the degradation went unrecorded. The GUI ``osascript`` ping
      is best-effort convenience on top; its result is intentionally ignored.
    - **Fix [2]:** the marker is written REGARDLESS of the GUI send result, so
      the finding is surfaced at most ONCE per key per day. ``osascript`` on a
      headless/non-Aqua launchd session is PERMANENTLY unavailable, not
      transient — the prior "withhold the marker on failure and retry next
      run" behavior just respawned a subprocess that can never succeed, every
      hour, forever. The durable WARNING log above is the channel that
      guarantees the finding is never lost; the once-a-day GUI ping is not."""
    sent: list[str] = []
    for key, text in findings:
        # Claim the day's marker FIRST (atomic O_EXCL). "exists" means a
        # concurrently-overlapping maintain already surfaced this key today —
        # skip, so one condition never double-fires (finding [1]). "claimed"
        # and "unwritable" both fire: "unwritable" is the degraded read-only
        # `.brain` case where the finding must still reach the durable log
        # (finding [0] tradeoff — it may re-surface next run).
        if mark_notified(vault, key, today) == "exists":
            continue
        _log.warning("[degradation] %s", text)
        fire_notification(text)  # best-effort GUI ping; result ignored by design
        sent.append(text)
    return sent
