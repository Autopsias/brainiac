"""Outcomes-report shape + date-gate logic for the `brain` maintenance verbs
(CUT-03): `check` / `health` / `curate` / `integrity` / `promote-scan` / `maintain`.

Pure, dependency-free helpers — no I/O, no BrainCore import (mirrors brain.brief).
``check``/``health``/``curate``/``integrity``/``promote-scan`` are the FOLDED
maintenance rituals from ``docs/cutover/task-disposition.md``; ``maintain`` is
the single sanctioned host task (``brain-nightly``) that multiplexes the
weekly/monthly cadences into one OS scheduler entry via date gates, per
``docs/cutover/persistence-budget.md``.

The three-bucket shape here is the generic, vault-agnostic structured
equivalent of the Acme-vault scheduled-task outcomes contract's three-block
report (✅ Auto-remediated / ⚠ Action Required / 🚧 Blocked): a thin host-side
scheduled-task wrapper can render ``render_outcomes_markdown`` directly, or
consume the structured JSON. brain itself never writes to a vault-specific
file like `_hot.md` — that propagation, if wanted, is the wrapper's job.
"""
from __future__ import annotations

import datetime
from typing import Any


def auto_fixed_item(verb: str, path: str, reason: str) -> dict[str, Any]:
    return {"verb": verb, "path": path, "reason": reason}


def action_required_item(
    finding: str, why: str, proposed: str, inspect: str
) -> dict[str, Any]:
    return {"finding": finding, "why": why, "proposed_action": proposed, "inspect": inspect}


def blocked_item(finding: str, blocking_on: str, retry_when: str) -> dict[str, Any]:
    return {"finding": finding, "blocking_on": blocking_on, "retry_when": retry_when}


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


def maintain_branches(today: datetime.date | None = None) -> list[str]:
    """Which date-gated branches are due for ``today`` (default: real today).

    Mirrors persistence-budget.md's "Date-gated branches inside the same run":
    Mon -> health, Tue -> integrity, Sun -> digest, 1st-of-month -> graphify
    (graph build stays separate tooling per task-disposition row 7 — `maintain`
    only documents the date-gate, it never invokes a graphify build itself).
    "daily" (index sync + drain + curate-style surfacing) is due every run.
    """
    d = today or datetime.date.today()
    branches = ["daily"]
    if d.weekday() == _MONDAY:
        branches.append("health")
    if d.weekday() == _TUESDAY:
        branches.append("integrity")
    if d.weekday() == _SUNDAY:
        branches.append("digest")
    if d.day == 1:
        branches.append("graphify")
    return branches
