"""Collapse byte-identical supersession families."""

from __future__ import annotations

from typing import Any, Callable


FAMILY_COLUMNS = (
    "SELECT rowid, id, path, classification, zone, type, "
    "is_latest_version, superseded_by, length(cast(body as blob)) FROM notes "
)


def _load_family_rows(
    index: Any,
    rows: dict[int, dict[str, Any]],
    where: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    fresh = []
    for rowid, note_id, path, tier, zone, note_type, latest, successor, size in (
        index.conn.execute(FAMILY_COLUMNS + where, params)  # nosec B608
    ):
        rid = int(rowid)
        if rid in rows:
            continue
        rows[rid] = {
            "id": str(note_id or ""),
            "path": str(path or ""),
            "cls": str(tier or ""),
            "zone": str(zone or ""),
            "type": str(note_type or ""),
            "retired": bool(
                (successor or "") or str(latest or "").strip().lower() == "false"
            ),
            "sup": str(successor or ""),
            "len": int(size or 0),
        }
        fresh.append(rows[rid])
    return fresh


def _linked_candidate_rows(index: Any, candidates: set[int]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    placeholders = ",".join("?" * len(candidates))
    frontier = _load_family_rows(
        index, rows, f"WHERE rowid IN ({placeholders})", tuple(candidates)
    )
    for _hop in range(8):
        wanted = {row["sup"] for row in frontier if row["sup"]}
        wanted -= {row["id"] for row in rows.values()}
        if not wanted:
            break
        placeholders = ",".join("?" * len(wanted))
        frontier = _load_family_rows(
            index,
            rows,
            f"WHERE id IN ({placeholders})",
            tuple(sorted(wanted)),
        )
    return rows


def _supersession_groups(rows: dict[int, dict[str, Any]]) -> list[list[int]]:
    by_note_id = {row["id"]: rid for rid, row in rows.items() if row["id"]}
    parent = {rid: rid for rid in rows}

    def root(rowid: int) -> int:
        while parent[rowid] != rowid:
            parent[rowid] = parent[parent[rowid]]
            rowid = parent[rowid]
        return rowid

    for rid, row in rows.items():
        successor = by_note_id.get(row["sup"])
        if successor is not None and successor != rid:
            parent[root(rid)] = root(successor)
    groups: dict[int, list[int]] = {}
    for rid in rows:
        groups.setdefault(root(rid), []).append(rid)
    return list(groups.values())


def _metadata_allows_collapse(meta: list[dict[str, Any]], min_body: int) -> bool:
    """Apply the ENF-01 floor before every other family predicate."""
    if min(row["len"] for row in meta) < min_body:
        return False
    if len({row["cls"] for row in meta}) > 1:
        return False
    if len({row["zone"] for row in meta}) > 1:
        return False
    return len({row["type"] for row in meta}) <= 1


def _body_identity_groups(
    index: Any, members: list[int]
) -> tuple[dict[str, list[int]], dict[str, int]]:
    from ..maintenance import _floor_bytes, body_sha256

    same: dict[str, list[int]] = {}
    normalized_sizes: dict[str, int] = {}
    placeholders = ",".join("?" * len(members))
    for rowid, body in index.conn.execute(
        f"SELECT rowid, body FROM notes WHERE rowid IN ({placeholders})",  # nosec B608
        tuple(members),
    ):
        digest = body_sha256(body or "")
        same.setdefault(digest, []).append(int(rowid))
        normalized_sizes[digest] = _floor_bytes(body or "")
    return same, normalized_sizes


def _record_identity_groups(
    output: Any,
    rows: dict[int, dict[str, Any]],
    same: dict[str, list[int]],
    normalized_sizes: dict[str, int],
    min_body: int,
) -> bool:
    collapsed = False
    for digest, family in same.items():
        if len(family) < 2 or normalized_sizes.get(digest, 0) < min_body:
            continue
        canonical = min(
            family,
            key=lambda rid: (
                rows[rid]["retired"],
                rows[rid]["path"].count("/"),
                len(rows[rid]["path"]),
                rows[rid]["id"],
            ),
        )
        absorbed = sorted(rid for rid in family if rid != canonical)
        for rid in absorbed:
            output.canonical_of[rid] = canonical
        output.absorbed_ids[canonical] = [rows[rid]["id"] for rid in absorbed]
        collapsed = True
    return collapsed


def collapse_duplicate_families(
    index: Any,
    lexical: list[int],
    dense: list[int],
    exact: Any,
    *,
    enabled: bool,
    min_body: int,
    collapse_factory: Callable[[], Any],
) -> Any:
    """Group eligible candidates into one-document ranking votes."""
    output = collapse_factory()
    candidates = set(lexical) | set(dense)
    if not candidates or not enabled:
        return output
    rows = _linked_candidate_rows(index, candidates)
    for members in _supersession_groups(rows):
        if len(members) < 2:
            continue
        meta = [rows[rid] for rid in members]
        if not _metadata_allows_collapse(meta, min_body):
            output.declined += 1
            continue
        if any(rid in exact.tiers or rid in exact.owner_rowids for rid in members):
            output.declined += 1
            continue
        same, normalized_sizes = _body_identity_groups(index, members)
        if not _record_identity_groups(
            output, rows, same, normalized_sizes, min_body
        ):
            output.declined += 1
    return output
