"""Rendering helper for the unsigned-notes corpus-invariant row."""
from __future__ import annotations

from typing import Any

from .brief import _esc as _escape_html


def _unsigned_notes_context(metric: dict[str, Any]) -> str:
    """Explain the count without treating an unavailable measurement as zero."""
    zones = metric.get("by_zone") or {}
    zones_text = (", ".join(f"{_escape_html(k)}/ {_escape_html(v)}"
                            for k, v in sorted(zones.items()))
                  or "none")
    if metric.get("available"):
        return (f"by zone: {zones_text}, of "
                f"{_escape_html(metric.get('population', '?'))} note(s)")
    return "no readable audit chain — NOT measured (never read as 0)"
