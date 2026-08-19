"""Knowledge-layer wikilink-hygiene metrics for the GRH-01 weekly fold."""
from __future__ import annotations

import sqlite3
from typing import Any

from .link_targets import _build_resolver, resolve_target

# ---------------------------------------------------------------------------
# GRH-01 (2026-07-20 dedup batch, finding-driven weekly fold) — cheap,
# no-model, no-embedding graph-hygiene metrics over the KNOWLEDGE LAYER
# (vault/brain/ zone, non-`source` types, is_latest_version not `false`).
# ---------------------------------------------------------------------------
_GENERATED_MAP_BASENAMES = {"backlinks.md", "catalog.md"}


def _connected_components(nodes: set[str], adj: dict[str, set[str]]) -> int:
    """Count of connected components over ``nodes`` under undirected ``adj``
    (an isolated/orphan node is its own size-1 component)."""
    seen: set[str] = set()
    count = 0
    for start in nodes:
        if start in seen:
            continue
        count += 1
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            for nb in adj.get(cur, set()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
    return count


def graph_hygiene_metrics(conn: sqlite3.Connection, *, cap: int = 20) -> dict[str, Any]:
    """Cheap knowledge-layer wikilink-hygiene metrics (no model, no embedding
    — pure index-and-body reads + graph traversal, same cost class as
    ``build_graph``).

    Knowledge layer = ``zone == "brain"``, ``type != "source"``,
    ``is_latest_version`` not explicitly ``"false"``, AND not a generated map
    (``backlinks.md`` — actually never indexed at all, see
    ``notes.scan_vault`` — or a per-zone ``catalog.md``, which IS indexed and
    DOES wikilink literally every note in its zone by design, NAV-01).
    Counting a generated map as a knowledge-layer member at all — even just as
    a link SOURCE — both hides every genuine orphan its outgoing links touch
    AND, since nothing wikilinks back to a generated map itself, adds a
    constant-noise "orphan" every single run (both false positives found
    live, 2026-07-20 dedup batch / this fold's own first fixture run against
    a real `maintain()` pass). So generated maps are excluded from the
    knowledge layer ENTIRELY, not merely as sources.

    Returns ``{knowledge_note_count, orphan_count, orphan_ids, island_count,
    dangling_target_count, dangling_targets, exact_duplicate_pairs,
    exact_duplicate_note}`` — the last two are ``None``/a fixed string: an
    exact-duplicate count is NOT cheaply available here (integrity's near-dup
    scan embeds and is deliberately a separate, on-invoke ritual), so this
    fold skips it rather than re-deriving a second embedding pass."""
    rows = conn.execute("SELECT id, title, path, body, zone, type, is_latest_version FROM notes").fetchall()
    id_rows = [(r[0], r[1] or "", r[2] or "") for r in rows]
    resolver = _build_resolver(id_rows)

    knowledge_ids: set[str] = set()
    body_by_id: dict[str, str] = {}
    for nid, _title, path, body, zone, ntype, is_latest in rows:
        basename = (path or "").rsplit("/", 1)[-1]
        body_by_id[nid] = body or ""
        if (zone or "") == "brain" and (ntype or "") != "source" \
                and str(is_latest or "").strip().lower() != "false" \
                and basename not in _GENERATED_MAP_BASENAMES:
            knowledge_ids.add(nid)

    adj: dict[str, set[str]] = {n: set() for n in knowledge_ids}
    dangling: set[str] = set()
    for nid in knowledge_ids:
        for target in parse_wikilinks(body_by_id.get(nid, "")):
            tgt = resolve_target(resolver, target)
            if tgt is None:
                dangling.add(target)
                continue
            if tgt == nid or tgt not in knowledge_ids:
                continue
            adj[nid].add(tgt)
            adj[tgt].add(nid)

    orphans = sorted(n for n in knowledge_ids if not adj.get(n))
    island_count = _connected_components(knowledge_ids, adj) if knowledge_ids else 0
    dangling_sorted = sorted(dangling)

    return {
        "knowledge_note_count": len(knowledge_ids),
        "orphan_count": len(orphans),
        "orphan_ids": orphans[:cap],
        "island_count": island_count,
        "dangling_target_count": len(dangling_sorted),
        "dangling_targets": dangling_sorted[:cap],
        "exact_duplicate_pairs": None,
        "exact_duplicate_note": "not computed here — see `brain integrity` "
                                 "(near-dup scan requires embedding, out of "
                                 "this cheap fold's scope)",
    }

# Parent-namespace bind (deferred past this module's own defs so the
# circular import with brain.graph resolves whichever side loads first).
from .graph import parse_wikilinks as parse_wikilinks  # noqa: E402
