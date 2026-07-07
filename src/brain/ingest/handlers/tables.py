"""Shared table-to-Markdown reconstruction (HARDENED:grill).

Header loss silently corrupts facts at low character-error rates (a table
flattened to prose loses the row/column association entirely). Rule: preserve
structure as a Markdown table with headers retained; a table that cannot be
reconstructed with headers is emitted as a fenced block flagged
``table-unparsed`` instead of being flattened to prose.
"""
from __future__ import annotations


def _cell(v: object) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def rows_to_markdown(rows: list[list[object]], *, label: str = "table") -> str:
    """Render ``rows`` (first row = header) as a Markdown table, or a fenced
    ``table-unparsed`` block if headers can't be trusted (empty header row,
    ragged column counts, or zero data rows)."""
    rows = [r for r in rows if any(_cell(c) for c in r)]  # drop fully-blank rows
    if not rows:
        return ""
    header = [_cell(c) for c in rows[0]]
    ncols = len(header)
    unparsed = (
        ncols == 0
        or not any(header)
        or len(rows) < 2
        or any(len(r) != ncols for r in rows[1:])
    )
    if unparsed:
        raw = "\n".join(" | ".join(_cell(c) for c in r) for r in rows)
        return f"```table-unparsed\n{raw}\n```\n"
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for r in rows[1:]:
        lines.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(lines) + "\n"
