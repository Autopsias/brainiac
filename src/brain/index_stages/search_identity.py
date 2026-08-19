"""Resolve exact identities for search."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..frontmatter import phrase_tokens


def identity_owner_rowids(
    index: Any, query_norm: str
) -> tuple[set[int], set[int], set[int]]:
    """Return the complete pre-egress alias/title owner union for a query."""
    if not query_norm:
        return set(), set(), set()
    cached = index._identity_owner_cache.get(query_norm)
    if cached is not None:
        owner, aliases, title = cached
        return set(owner), set(aliases), set(title)
    try:
        rows = index.conn.execute(
            "SELECT rowid, 0 FROM notes WHERE title_norm=? "
            "UNION ALL "
            "SELECT note_rowid, 1 FROM aliases WHERE alias_norm=?",
            (query_norm, query_norm),
        ).fetchall()
    except sqlite3.OperationalError:
        return set(), set(), set()
    title = {int(rowid) for rowid, is_alias in rows if not is_alias}
    aliases = {int(rowid) for rowid, is_alias in rows if is_alias}
    owner = title | aliases
    index._identity_owner_cache[query_norm] = (
        frozenset(owner),
        frozenset(aliases),
        frozenset(title),
    )
    return owner, aliases, title


def identity_records(index: Any, rowids: set[int]) -> dict[int, dict[str, Any]]:
    """Fetch only the metadata needed for deterministic exact ordering."""
    if not rowids:
        return {}
    qmarks = ",".join("?" * len(rowids))
    date_expr = "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created)"
    rows = index.conn.execute(
        f"SELECT rowid,id,title,classification,zone,path,is_latest_version,{date_expr} "  # nosec B608 -- bound placeholders only
        f"FROM notes WHERE rowid IN ({qmarks})",
        tuple(sorted(rowids)),
    ).fetchall()
    return {
        int(row[0]): {
            "rowid": int(row[0]),
            "id": str(row[1] or ""),
            "title": str(row[2] or ""),
            "classification": str(row[3] or ""),
            "zone": str(row[4] or ""),
            "path": str(row[5] or ""),
            "is_latest_version": str(row[6] or ""),
            "date": str(row[7] or ""),
        }
        for row in rows
    }


def title_phrase_records(index: Any) -> list[dict[str, Any]]:
    """Return the cached title projection used to verify phrase matches."""
    if index._title_phrase_records_cache is not None:
        return index._title_phrase_records_cache[0]
    try:
        rows = index.conn.execute(
            "SELECT rowid,id,title,zone,path,is_latest_version,"
            "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) "
            "FROM notes"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    records = [
        {
            "rowid": int(row[0]),
            "id": str(row[1] or ""),
            "title": str(row[2] or ""),
            "zone": str(row[3] or ""),
            "path": str(row[4] or ""),
            "is_latest_version": str(row[5] or ""),
            "date": str(row[6] or ""),
        }
        for row in rows
    ]
    by_token: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        # This is only a prefilter; the exact contiguous phrase check follows.
        for token in set(phrase_tokens(record["title"])):
            by_token.setdefault(token, []).append(record)
    index._title_phrase_records_cache = (records, by_token)
    return records


def title_phrase_candidates(index: Any, qtokens: list[str]) -> list[dict[str, Any]]:
    """Return titles that can possibly contain the query tokens contiguously."""
    title_phrase_records(index)
    if not qtokens or index._title_phrase_records_cache is None:
        return []
    return index._title_phrase_records_cache[1].get(qtokens[0], [])
