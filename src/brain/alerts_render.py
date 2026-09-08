"""The two human-readable renderings of an alerts report.

Split out of `alerts.py` on 2026-09-05 purely for the file-size ratchet:
these are pure dict-to-string formatters with no dependency on anything
else in that module. `alerts` re-exports both, so every caller still
reaches them as `alerts.render_human` / `alerts.one_line`.
"""
from __future__ import annotations

from typing import Any


def render_human(report: dict[str, Any]) -> str:
    """One line per alert. Deliberately terse — this is read at session start."""
    lines: list[str] = []
    for item in report["alerts"]:
        scope = item.get("scope")
        lines.append(f"  ! {scope + ': ' if scope else ''}{item['text']}")
    if not lines:
        lines.append("  no alerts")
    for note in report.get("unreachable", []):
        lines.append(f"  - not checkable from role={report['role']}: {note}")
    header = f"brain alerts — {len(report['alerts'])} finding(s), role={report['role']}"
    return "\n".join([header, *lines])


def one_line(report: dict[str, Any]) -> str:
    """The banner form a SessionStart hook injects. Empty when all clear."""
    if not report["alerts"]:
        return ""
    parts = []
    for item in report["alerts"]:
        scope = item.get("scope")
        parts.append(f"{scope}: {item['text']}" if scope else item["text"])
    return "BRAINIAC ALERTS: " + " | ".join(parts)
