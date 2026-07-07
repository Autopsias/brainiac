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
import json
from typing import Any


def auto_fixed_item(verb: str, path: str, reason: str) -> dict[str, Any]:
    return {"verb": verb, "path": path, "reason": reason}


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
_WEEKLY_TRIGGER_WEEKDAY = {"health": _MONDAY, "integrity": _TUESDAY, "digest": _SUNDAY}
_ALL_BRANCHES = ("daily", "health", "integrity", "digest", "graphify")


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
def render_curation_hot_entry(
    stale_links: list[dict[str, Any]], revisit_sample: list[dict[str, Any]],
    today: datetime.date,
) -> str:
    lines = [f"## {today.isoformat()} — Sunday curation scan"]
    lines.append(
        f"- **Context:** scheduled curation fold found {len(stale_links)} stale "
        f"wikilink target(s) and a {len(revisit_sample)}-note revisit sample."
    )
    for s in stale_links[:10]:
        target = s.get("target_text")
        reason = s.get("reason")
        frm = s.get("from", {}).get("id")
        lines.append(f"  - stale link: `{frm}` -> `{target}` ({reason})")
    for r in revisit_sample[:10]:
        lines.append(
            f"  - revisit: `{r.get('id')}` (last updated {r.get('updated')}, "
            f"age {r.get('age_days')}d, score {r.get('score')})"
        )
    lines.append(
        "- **Owner input needed:** review via `brain curate --json` or the "
        "`.claude/skills/curation` skill for full detail."
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


def render_promote_scan_hot_entry(candidates: list[dict[str, Any]], today: datetime.date) -> str:
    lines = [f"## {today.isoformat()} — Sunday promotion-scan"]
    lines.append(
        f"- **Context:** {len(candidates)} `raw/` source(s) not yet promoted "
        "into a typed `brain/` note."
    )
    for c in candidates[:10]:
        lines.append(f"  - `{c.get('id')}` ({c.get('path')})")
    lines.append(
        "- **Owner input needed:** review for promotion (`brain capture` / "
        "`brain write`) — promotion itself stays a human gate."
    )
    return "\n".join(lines) + "\n"
