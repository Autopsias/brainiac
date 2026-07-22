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

import datetime
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

# [[target]] | [[target|alias]] | [[target#heading]] | [[target#heading|alias]]
# Alias text is matched non-greedily and right-anchored to the FINAL ]] so an
# alias containing nested brackets (e.g. "display [x]") doesn't truncate the
# match at the first ']' and drop the link entirely (M-5).
# Target and heading classes exclude newlines (\n\r) so an UNTERMINATED "[[X"
# can't run away across lines and borrow a later link's closing "]]" — that
# runaway match both invented garbage stale targets and destroyed the real
# link whose "]]" it stole (field bug 2, 2026-07-13). The alias branch uses
# "." which already excludes newlines without re.DOTALL, so it needs no change.
_WIKILINK = re.compile(r"\[\[([^\]\|#\n\r]+)(?:#[^\]\|\n\r]+)?(?:\|.+?)?\]\]")

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


def build_graph(
    conn: sqlite3.Connection, *, extra_edges: Iterable[tuple[str, str]] | None = None
) -> LinkGraph:
    """Build the wikilink graph on demand from the index's ``notes`` table.

    Discovery-only and derived: nothing is persisted; re-call to rebuild.

    ``extra_edges`` (GRF-01, ADR-0003 Ruling 6, "Optional") folds additional
    undirected adjacency pairs — the graphify build's INFERRED edges — into
    the SAME graph before BFS/PPR run, so ``graph_expand`` can optionally
    treat them as discovery-only traversal input. Never persisted here; the
    caller decides whether to pass any."""
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
    for a, b in extra_edges or ():
        if a in g.nodes and b in g.nodes and a != b:
            g.out[a].add(b)
            g.inn[b].add(a)
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
    extra_edges: Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """On-demand multi-hop expansion for multi-entity / multi-hop queries.

    Combines wikilink-BFS (reachability + hop distance) with PPR (centrality
    relative to the seeds) and returns DISCOVERY-ONLY candidate note ids to feed
    back into the authoritative surfaces. Never authoritative on its own.

    ``extra_edges`` (GRF-01, optional): graphify's INFERRED edges, folded into
    the SAME derived graph when the caller opts in (``brain graph-expand
    --use-inferred``) — still discovery-only, still gated the same way."""
    g = build_graph(conn, extra_edges=extra_edges)
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
        "method": ("wikilink-bfs+ppr" if use_ppr else "wikilink-bfs")
                  + ("+inferred" if extra_edges else ""),
        "authoritative": False,
        "provenance": PROVENANCE,
        "note": "discovery-only candidate ids; confirm on the cited note via "
                "`brain get <id>` before asserting — never authoritative.",
        "results": results,
    }


# --------------------------------------------------------------------------
# Curation folds (AUT-02, ADR-0003 Ruling 5 Sunday branch): stale wikilink
# targets + a staleness revisit sample. Both reuse this same derived graph —
# DISCOVERY-ONLY, never authoritative, exactly like graph_expand above.
# --------------------------------------------------------------------------
def stale_wikilink_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Outbound wikilinks whose target has vanished (no note resolves to it
    any more) or has moved to ``archive/`` while still linked from an active
    note. UNFILTERED (note-shaped, by id) — caller egress-gates ``from`` and
    ``target`` before surfacing, same discipline as ``near_dup``."""
    rows = conn.execute(
        "SELECT id, title, path, classification, body FROM notes"
    ).fetchall()
    resolver = _build_resolver([(r[0], r[1] or "", r[2] or "") for r in rows])
    meta = {
        r[0]: {"id": r[0], "title": r[1], "path": r[2], "classification": r[3]}
        for r in rows
    }
    out: list[dict[str, Any]] = []
    for nid, _title, _path, _cls, body in rows:
        for target in parse_wikilinks(body or ""):
            resolved = resolver.get(target) or resolver.get(target.lower())
            if resolved is None:
                out.append({
                    "from": meta[nid], "target": None, "target_text": target,
                    "reason": "vanished",
                })
                continue
            if resolved == nid:
                continue
            tpath = (meta[resolved]["path"] or "").replace("\\", "/")
            if tpath.startswith("archive/") or "/archive/" in tpath:
                out.append({
                    "from": meta[nid], "target": meta[resolved], "target_text": target,
                    "reason": "archived",
                })
    return out


def revisit_sample(
    conn: sqlite3.Connection, today: datetime.date, *, k: int = 10
) -> list[dict[str, Any]]:
    """Notes overdue for a re-read, ranked by ``age_days * (centrality + 1)``.

    Centrality is the existing wikilink-BFS+PPR module's Personalized
    PageRank run with EVERY note as its own seed (uniform restart across the
    whole corpus) — i.e. standard whole-corpus PageRank, reusing
    ``personalized_pagerank`` rather than a new ranking module (this
    supersedes the curation skill's previously-documented "global centrality
    is gap G3, age-only fallback" framing). The ``+1`` smoothing means an
    isolated/orphan note (centrality 0) still ranks by age alone rather than
    scoring zero and never surfacing.

    Upgrade path (s10, grf-01): once ``graphify`` builds INFERRED
    (embedding-neighbour) edges, feed the merged explicit+INFERRED graph into
    this same function for a richer centrality signal — the ranking formula
    itself does not need to change.

    UNFILTERED (note-shaped, by id) — caller egress-gates before surfacing.
    """
    g = build_graph(conn)
    centrality = personalized_pagerank(g, list(g.nodes)) if g.nodes else {}
    rows = conn.execute(
        "SELECT id, title, path, classification, updated FROM notes"
    ).fetchall()
    scored: list[dict[str, Any]] = []
    for nid, title, path, classification, updated in rows:
        age_days = 0
        try:
            age_days = max(
                (today - datetime.date.fromisoformat(str(updated)[:10])).days, 0
            )
        except (TypeError, ValueError):
            pass
        cscore = centrality.get(nid, 0.0)
        score = age_days * (cscore + 1.0)
        scored.append({
            "id": nid, "title": title, "path": path, "classification": classification,
            "updated": updated, "age_days": age_days,
            "centrality": round(cscore, 6), "score": round(score, 3),
        })
    scored.sort(key=lambda d: (-d["score"], d["id"]))
    return scored[:k]


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
            tgt = resolver.get(target) or resolver.get(target.lower())
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
