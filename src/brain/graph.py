"""Derived wikilink graph: BFS + Personalized PageRank, DISCOVERY-ONLY (RET-03).

Structure in this vault is wikilinks, not folders (substrate-spec §1). For a
multi-entity / multi-hop question, following ``[[links]]`` between notes surfaces
context that neither lexical nor dense retrieval reaches in one shot. Two
on-demand traversals are offered:

  * **Wikilink-BFS** — breadth-first neighbours of a seed set out to ``depth``
    hops (treating links as undirected so backlinks count).
  * **Personalized PageRank (PPR)** — random-walk-with-restart biased to the
    seed set; ranks the whole reachable neighbourhood by graph centrality
    *relative to the seeds*. Better than raw BFS when many notes are 1-2 hops
    out and you want the few that matter.

THE GRAPH IS DISCOVERY-ONLY AND NEVER AUTHORITATIVE. It is derived from note
bodies (rebuildable, disposable) and its edges are heuristic (a ``[[link]]`` is
an association, not a verified claim). Every result is tagged
``authoritative: False`` / ``provenance: "graph-derived (discovery-only)"``. Its
sole job is to nominate candidate note ids to feed back into the AUTHORITATIVE
surfaces (hybrid_search / get / grep); a curated note and the retrieval cascade
WIN on any conflict. Callers must never quote a graph result as fact without
confirming it on the cited note.

Built on demand from the ``notes`` table (id + body) of an open index
connection — no schema change, no migration, no persisted edge table.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

# [[target]] | [[target|alias]] | [[target#heading]] | [[target#heading|alias]]
_WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")

PROVENANCE = "graph-derived (discovery-only)"


def parse_wikilinks(body: str) -> list[str]:
    """Return the raw link targets in a note body, order-preserving, de-duped.

    Strips ``#heading`` anchors and ``|alias`` display text; the target is the
    note reference (an id, a path stem, or a title)."""
    seen: dict[str, None] = {}
    for m in _WIKILINK.finditer(body or ""):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)


@dataclass
class LinkGraph:
    """A directed wikilink graph with a resolver for id/stem/title targets.

    ``out[a]`` are the notes ``a`` links to; ``inn[b]`` are the notes that link
    to ``b``. ``undirected_adj`` merges both directions (discovery treats a link
    as a symmetric association)."""

    out: dict[str, set[str]] = field(default_factory=dict)
    inn: dict[str, set[str]] = field(default_factory=dict)
    nodes: set[str] = field(default_factory=set)
    unresolved: dict[str, list[str]] = field(default_factory=dict)

    def neighbours(self, node: str) -> set[str]:
        return self.out.get(node, set()) | self.inn.get(node, set())

    @property
    def undirected_adj(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {n: set() for n in self.nodes}
        for a, outs in self.out.items():
            for b in outs:
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)
        return adj


def _build_resolver(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    """Map every alias (id, path stem, lowercased title) -> canonical note id."""
    resolver: dict[str, str] = {}
    for nid, title, path in rows:
        resolver[nid] = nid
        resolver[nid.lower()] = nid
        stem = path.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        resolver.setdefault(stem, nid)
        resolver.setdefault(stem.lower(), nid)
        if title:
            resolver.setdefault(title.lower(), nid)
    return resolver


def build_graph(conn: sqlite3.Connection) -> LinkGraph:
    """Build the wikilink graph on demand from the index's ``notes`` table.

    Discovery-only and derived: nothing is persisted; re-call to rebuild."""
    rows = conn.execute("SELECT id, title, path, body FROM notes").fetchall()
    id_rows = [(r[0], r[1] or "", r[2] or "") for r in rows]
    resolver = _build_resolver(id_rows)
    g = LinkGraph()
    for nid, _title, _path in id_rows:
        g.nodes.add(nid)
        g.out.setdefault(nid, set())
        g.inn.setdefault(nid, set())
    for nid, _title, _path, body in ((r[0], r[1], r[2], r[3]) for r in rows):
        for target in parse_wikilinks(body or ""):
            tgt = resolver.get(target) or resolver.get(target.lower())
            if tgt is None:
                g.unresolved.setdefault(nid, []).append(target)
                continue
            if tgt == nid:
                continue
            g.out[nid].add(tgt)
            g.inn[tgt].add(nid)
    return g


def wikilink_bfs(
    g: LinkGraph, seeds: Iterable[str], *, depth: int = 2
) -> list[dict[str, Any]]:
    """Breadth-first neighbours of the seeds out to ``depth`` hops (undirected).

    Returns [{id, hops}] excluding the seeds themselves, nearest-first."""
    adj = g.undirected_adj
    seed_set = {s for s in seeds if s in g.nodes}
    visited: dict[str, int] = {s: 0 for s in seed_set}
    frontier = set(seed_set)
    for hop in range(1, max(0, depth) + 1):
        nxt: set[str] = set()
        for node in frontier:
            for nb in adj.get(node, set()):
                if nb not in visited:
                    visited[nb] = hop
                    nxt.add(nb)
        frontier = nxt
        if not frontier:
            break
    out = [
        {"id": nid, "hops": hops, "authoritative": False, "provenance": PROVENANCE}
        for nid, hops in visited.items()
        if nid not in seed_set
    ]
    out.sort(key=lambda d: (d["hops"], d["id"]))
    return out


def personalized_pagerank(
    g: LinkGraph,
    seeds: Iterable[str],
    *,
    alpha: float = 0.85,
    iters: int = 40,
    tol: float = 1e-9,
) -> dict[str, float]:
    """Random-walk-with-restart biased to ``seeds`` over the undirected graph.

    ``r = (1-alpha)*p + alpha * sum_j r[j] * (1/deg[j]) for j in adj``, where
    ``p`` restarts uniformly onto the seed set. Returns id -> score (sums ~1)."""
    adj = g.undirected_adj
    nodes = list(g.nodes)
    if not nodes:
        return {}
    seed_set = [s for s in seeds if s in g.nodes]
    if not seed_set:
        return {}
    restart = {n: 0.0 for n in nodes}
    for s in seed_set:
        restart[s] = 1.0 / len(seed_set)
    rank = dict(restart)
    deg = {n: len(adj.get(n, set())) for n in nodes}
    for _ in range(max(1, iters)):
        nxt = {n: (1.0 - alpha) * restart[n] for n in nodes}
        for j in nodes:
            dj = deg[j]
            if dj == 0:
                # Dangling node: teleport its mass back to the restart set.
                share = alpha * rank[j]
                for s in seed_set:
                    nxt[s] += share / len(seed_set)
                continue
            spread = alpha * rank[j] / dj
            for nb in adj[j]:
                nxt[nb] += spread
        delta = sum(abs(nxt[n] - rank[n]) for n in nodes)
        rank = nxt
        if delta < tol:
            break
    return rank


def graph_expand(
    conn: sqlite3.Connection,
    seeds: Iterable[str],
    *,
    depth: int = 2,
    k: int = 10,
    use_ppr: bool = True,
) -> dict[str, Any]:
    """On-demand multi-hop expansion for multi-entity / multi-hop queries.

    Combines wikilink-BFS (reachability + hop distance) with PPR (centrality
    relative to the seeds) and returns DISCOVERY-ONLY candidate note ids to feed
    back into the authoritative surfaces. Never authoritative on its own."""
    g = build_graph(conn)
    seed_list = list(dict.fromkeys(seeds))
    resolver = _build_resolver(
        [(r[0], r[1] or "", r[2] or "")
         for r in conn.execute("SELECT id, title, path FROM notes").fetchall()]
    )
    resolved_seeds = [resolver.get(s) or resolver.get(s.lower()) or s for s in seed_list]
    known_seeds = [s for s in resolved_seeds if s in g.nodes]

    bfs = wikilink_bfs(g, known_seeds, depth=depth)
    bfs_hops = {d["id"]: d["hops"] for d in bfs}

    ppr_scores: dict[str, float] = (
        personalized_pagerank(g, known_seeds) if use_ppr else {}
    )

    candidates = set(bfs_hops)
    if use_ppr:
        candidates |= {n for n, s in ppr_scores.items() if s > 0.0}
    candidates -= set(known_seeds)

    ranked = sorted(
        candidates,
        key=lambda n: (-ppr_scores.get(n, 0.0), bfs_hops.get(n, 99), n),
    )[:k]

    # Title + classification lookup (classification lets the CLI egress gate
    # filter graph candidates so the discovery surface cannot leak the existence
    # of a withheld note).
    meta = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT id, title, classification FROM notes").fetchall()
    }
    results = [
        {
            "id": nid,
            "title": meta.get(nid, (nid, ""))[0],
            "classification": meta.get(nid, (nid, ""))[1],
            "hops": bfs_hops.get(nid),
            "ppr": round(ppr_scores.get(nid, 0.0), 6),
            "authoritative": False,
            "provenance": PROVENANCE,
        }
        for nid in ranked
    ]
    return {
        "seeds": seed_list,
        "resolved_seeds": known_seeds,
        "unresolved_seeds": [s for s in resolved_seeds if s not in g.nodes],
        "depth": depth,
        "method": "wikilink-bfs+ppr" if use_ppr else "wikilink-bfs",
        "authoritative": False,
        "provenance": PROVENANCE,
        "note": "discovery-only candidate ids; confirm on the cited note via "
                "`brain get <id>` before asserting — never authoritative.",
        "results": results,
    }
