"""Shared table-to-Markdown reconstruction (HARDENED:grill).

Header loss silently corrupts facts at low character-error rates (a table
flattened to prose loses the row/column association entirely). Rule: preserve
structure as a Markdown table with headers retained; a table that cannot be
reconstructed with headers is emitted as a fenced block flagged
``table-unparsed`` instead of being flattened to prose.
"""
from __future__ import annotations

from typing import Any


def _cell(v: object) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _admit_row(cells: list[str], admit: Any, *, piped: bool) -> None:
    """Record one rendered row on a coverage ledger, cell by cell.

    The pipes and the fence are the HANDLER's text; the cells are the
    DOCUMENT's. Splitting them is what lets a walker's report be compared
    against the document text alone — a fence label counted as document text
    reads as a payload the walker never saw (round 6, 2026-09-03).

    Emission order is the rendered order, because the ledger's residue check
    requires the chunks to tile the body exactly; get it wrong and the note
    reads ``unknown`` rather than silently passing.
    """
    if piped:
        admit("chrome", "|")
    for i, cell in enumerate(cells):
        admit("text", cell)
        if piped or i < len(cells) - 1:
            admit("chrome", "|")


def rows_to_markdown(rows: list[list[object]], *, label: str = "table",
                     admit: Any = None) -> str:
    """Render ``rows`` (first row = header) as a Markdown table, or a fenced
    ``table-unparsed`` block if headers can't be trusted (empty header row,
    ragged column counts, or zero data rows).

    ``admit`` is an optional coverage-ledger sink, ``admit(kind, text)`` with
    ``kind`` either ``"chrome"`` (this module's own scaffolding) or ``"text"``
    (a cell out of the document). It changes nothing about the string
    returned; a lane with no ledger passes nothing and is unaffected.
    """
    keep = admit if admit is not None else (lambda kind, text: text)
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
        keep("chrome", "```table-unparsed")
        for r in rows:
            _admit_row([_cell(c) for c in r], keep, piped=False)
        keep("chrome", "```")
        return f"```table-unparsed\n{raw}\n```\n"
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    _admit_row(header, keep, piped=True)
    keep("chrome", "| " + " | ".join(["---"] * ncols) + " |")
    for r in rows[1:]:
        cells = [_cell(c) for c in r]
        lines.append("| " + " | ".join(cells) + " |")
        _admit_row(cells, keep, piped=True)
    return "\n".join(lines) + "\n"
