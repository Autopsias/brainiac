"""The three-bucket outcomes shape shared by every maintenance verb."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any


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
    agents_md = report.get("agents_md_mirror") or {}
    paths = [f"{d['skill']} [{d['mirror']}] {d.get('path') or d['reason']}" for d in drift[:5]]
    parts = []
    if drift:
        parts.append(f"{len(drift)} skill-mirror file(s) diverged")
    if not claude_md.get("ok"):
        parts.append(f"CLAUDE.md: {claude_md.get('reason')}")
    if not agents_md.get("ok", True):
        parts.append(f"AGENTS.md mirror: {agents_md.get('reason')}")
    return action_required_item(
        "; ".join(parts) or "framework-sync drift detected",
        "the .claude/skills canonical tree, .agents/skills mirror, "
        "plugins/ marketplace copies, and/or src/brain/_assets/AGENTS.md "
        "have drifted apart (or CLAUDE.md's @AGENTS.md import broke)",
        "run `python3 tools/package_clients.py` to resync, then re-run health "
        "(AGENTS.md drift must be fixed by hand — copy AGENTS.md over "
        "src/brain/_assets/AGENTS.md)",
        "; ".join(paths) or ("AGENTS.md mirror" if not agents_md.get("ok", True) else "CLAUDE.md"),
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

# Cross-section binds, deferred past this module's own defs.
