"""Detect corpus near-duplicate pairs."""

from __future__ import annotations

from typing import Any, Callable

from ..chunk import Chunk
from ..vectors import cosine


def _representative_rows(index: Any) -> list[tuple[Any, ...]]:
    return index.conn.execute(
        "SELECT n.rowid, n.id, n.title, n.zone, c.heading, c.lang, c.text "
        "FROM notes n JOIN chunks c ON c.note_rowid = n.rowid AND c.ordinal = 0"
    ).fetchall()


def _representative_inputs(rows: list[tuple[Any, ...]]) -> list[str]:
    return [
        Chunk(
            ordinal=0,
            heading=row[4] or "",
            text=row[6] or "",
            lang=row[5] or "en",
        ).embed_input(row[2] or row[1], row[3] or "", "")
        for row in rows
    ]


def _candidate_pairs(
    index: Any,
    rows: list[tuple[Any, ...]],
    vectors: list[list[float]],
    *,
    min_score: float,
    neighbours: int,
) -> dict[tuple[str, str], float]:
    note_id = {int(row[0]): row[1] for row in rows}
    vector_by_note = {int(row[0]): vector for row, vector in zip(rows, vectors)}
    chunk_to_note = {
        int(chunk_rowid): int(note_rowid)
        for note_rowid, chunk_rowid in index.conn.execute(
            "SELECT note_rowid, rowid FROM chunks WHERE ordinal = 0"
        ).fetchall()
    }
    best: dict[tuple[str, str], float] = {}
    for row, vector in zip(rows, vectors):
        rowid = int(row[0])
        for chunk_rowid, _backend_score in index.backend.search(
            index.conn, vector, neighbours + 1
        ):
            other_rowid = chunk_to_note.get(int(chunk_rowid))
            if other_rowid is None or other_rowid == rowid:
                continue
            score = cosine(vector, vector_by_note[other_rowid])
            if score < min_score:
                continue
            first, second = note_id[rowid], note_id[other_rowid]
            key = (first, second) if first < second else (second, first)
            if score > best.get(key, -1.0):
                best[key] = score
    return best


def _recurring_caveat(
    first_id: str,
    second_id: str,
    patterns: tuple[str, ...],
    match_pattern: Callable[[str, tuple[str, ...]], str | None],
) -> str | None:
    first_pattern = match_pattern(first_id, patterns)
    second_pattern = match_pattern(second_id, patterns)
    if not first_pattern or not second_pattern:
        return None
    return (
        "both notes match a recurring-artifact id pattern "
        f"({first_pattern!r} / {second_pattern!r}) -- high similarity may "
        "reflect shared template/boilerplate text rather than duplicate content; "
        "verify the actual bodies before superseding"
    )


def _note_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "classification": row["classification"],
        "zone": row["zone"],
        "path": row["path"],
    }


def near_duplicates(
    index: Any,
    *,
    min_score: float,
    neighbours: int,
    patterns: tuple[str, ...],
    match_pattern: Callable[[str, tuple[str, ...]], str | None],
) -> list[dict[str, Any]]:
    """Nominate neighbours then score every pair with true cosine."""
    rows = _representative_rows(index)
    if len(rows) < 2:
        return []
    vectors = index.embedder.embed_batch(_representative_inputs(rows), is_query=False)
    pairs = _candidate_pairs(
        index,
        rows,
        vectors,
        min_score=min_score,
        neighbours=neighbours,
    )
    output = []
    for (first_id, second_id), score in pairs.items():
        first = index._note_row(index._rowid_of(first_id))
        second = index._note_row(index._rowid_of(second_id))
        if not first or not second:
            continue
        output.append(
            {
                "a": _note_projection(first),
                "b": _note_projection(second),
                "score": round(score, 6),
                "caveat": _recurring_caveat(
                    first_id, second_id, patterns, match_pattern
                ),
            }
        )
    output.sort(key=lambda row: -row["score"])
    return output
