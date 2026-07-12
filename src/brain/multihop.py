"""Graph-augmented multi-hop retrieval (RET-06).

Flat top-k retrieval answers "which single note is most similar to this query".
A *multi-hop* question — "how does Alex Silva's exit connect to the org-transition
programme", "Contoso runs S/4HANA while Northwind runs a different SAP estate, what
does the JV do" — asks about a RELATIONSHIP between two or more named entities.
The relevant evidence is often spread across several notes linked by
``[[wikilinks]]``; no single note is top-k-similar to the whole question, so
flat dense/lexical retrieval misses the corroborating notes in the tail.

This module adds a *gated* graph-expansion step on top of ``hybrid_search``:

  1. **Gate (query-intrinsic).** Detect whether the query is "multi-hop-shaped"
     — it names **>= 2 distinct entities** (people / companies / decisions /
     projects / concepts) that exist as notes. A ubiquitous hub entity
     (``contoso`` — mentioned by almost every query in this corpus) does NOT count
     toward the threshold, though it may still seed the walk. Single-hop
     queries fail the gate and are returned by flat ``hybrid_search`` UNCHANGED
     — so single-hop latency and quality can never regress (the graph code is
     never even reached).

  2. **Seed.** Seeds = the entity notes named in the query, UNION the top few
     flat hits (the notes flat retrieval already judged relevant).

  3. **Expand.** Wikilink-BFS + Personalized-PageRank from the seeds
     (``brain.graph``) — DISCOVERY-ONLY (RET-03): the derived graph is never
     authoritative, it only nominates candidate note ids.

  4. **Re-rank (flat-dominant weighted RRF).** Fuse the flat ranking with the
     graph ranking. The flat list carries weight 1.0; the graph list a smaller
     ``graph_weight`` (<= 1). This is deliberately conservative: the top flat
     hit can never be displaced by a discovery-only candidate, and a graph-only
     note (one flat never retrieved) lands in the tail — but a note that flat
     retrieved *weakly* (rank 11-30) AND the graph corroborates gets promoted
     into the top-k. That promotion of a graph-corroborated flat-tail note is
     the multi-hop recall/nDCG mechanism.

The graph and the entity lexicon are built once and cached on the owning index
(both are derived from the immutable ``notes`` table), so the per-query cost of
the multi-hop path is a walk over an in-memory graph, and the per-query cost of
the single-hop path is one cheap regex scan over ~250 entity surface forms.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from .graph import (
    LinkGraph,
    personalized_pagerank,
    wikilink_bfs,
)

# Entity-typed notes whose titles are the multi-hop "hop" vocabulary. These are
# the ``type:`` frontmatter values the projection assigns to People / Companies
# / Decisions / Projects / Concepts notes.
ENTITY_TYPES: tuple[str, ...] = ("person", "company", "decision", "project", "concept")

# Ubiquitous hub entities: mentioned by nearly every query in this corpus, so
# they are not DISCRIMINATING evidence of multi-hop intent. They may still seed
# the graph walk, but they do not count toward the >= 2 gate.
HUB_STOPLIST: frozenset[str] = frozenset({"contoso"})

# Minimum surface-form length — avoids matching 2-3 char acronyms that collide
# with common substrings (word-boundary anchored, but still noisy when short).
_MIN_FORM_LEN = 4

# Generic single-word concept titles that are too common to be entity evidence.
_GENERIC_FORMS: frozenset[str] = frozenset(
    {"governance", "legal", "budget", "roadmap", "strategy", "security",
     "compliance", "risk", "data", "cloud", "digital"}
)


def _norm(s: str | None) -> str:
    return unicodedata.normalize("NFC", (s or "")).lower().strip()


@dataclass(frozen=True)
class EntityMention:
    note_id: str
    surface: str
    etype: str
    is_hub: bool


class EntityLexicon:
    """Query -> entity-note mentions, built from the entity-typed notes.

    Cheap to query (regex scan over the compiled surface forms), built once."""

    __slots__ = ("_forms",)

    def __init__(self, forms: list[tuple[re.Pattern[str], str, str, str, bool]]):
        self._forms = forms

    @classmethod
    def build(cls, conn: sqlite3.Connection) -> "EntityLexicon":
        placeholders = ",".join("?" * len(ENTITY_TYPES))
        rows = conn.execute(
            f"SELECT id, title, type FROM notes WHERE type IN ({placeholders})",
            ENTITY_TYPES,
        ).fetchall()
        forms: list[tuple[re.Pattern[str], str, str, str, bool]] = []
        for nid, title, etype in rows:
            surface = _norm(title)
            if len(surface) < _MIN_FORM_LEN or surface in _GENERIC_FORMS:
                continue
            pat = re.compile(r"(?<!\w)" + re.escape(surface) + r"(?!\w)")
            forms.append((pat, nid, surface, etype, surface in HUB_STOPLIST))
        # Longest surface first so a specific "s/4hana" is preferred over a bare
        # substring when both would match (mentions() de-dups by note id anyway).
        forms.sort(key=lambda t: -len(t[2]))
        return cls(forms)

    def mentions(self, query: str) -> list[EntityMention]:
        """Distinct entity notes whose surface form appears in the query."""
        q = _norm(query)
        seen: dict[str, EntityMention] = {}
        for pat, nid, surface, etype, is_hub in self._forms:
            if nid in seen:
                continue
            if pat.search(q):
                seen[nid] = EntityMention(nid, surface, etype, is_hub)
        return list(seen.values())


def is_multihop_shaped(mentions: list[EntityMention]) -> bool:
    """True iff the query names >= 2 distinct NON-HUB entity notes.

    This is the deployable, query-intrinsic gate: a multi-hop question is one
    that references at least two specific entities whose relationship the graph
    can traverse. Hub entities (``contoso``) are excluded from the count so that
    "<hub> + one incidental entity" single-hop queries do not trip the gate."""
    non_hub = {m.note_id for m in mentions if not m.is_hub}
    return len(non_hub) >= 2


def rank_graph_candidates(
    graph: LinkGraph,
    seeds: list[str],
    *,
    depth: int = 2,
    limit: int = 30,
) -> list[str]:
    """Ordered discovery-only candidate note ids from a PREBUILT graph.

    Mirrors ``graph.graph_expand`` but takes an already-built graph so the eval
    harness can reuse one graph across every query. Ranks by
    ``(ppr desc, hops asc, id)`` and drops the seeds themselves."""
    known = [s for s in dict.fromkeys(seeds) if s in graph.nodes]
    if not known:
        return []
    bfs = wikilink_bfs(graph, known, depth=depth)
    hops = {d["id"]: d["hops"] for d in bfs}
    ppr = personalized_pagerank(graph, known)
    seed_set = set(known)
    cands = (set(hops) | {n for n, s in ppr.items() if s > 0.0}) - seed_set
    ranked = sorted(cands, key=lambda n: (-ppr.get(n, 0.0), hops.get(n, 99), n))
    return ranked[:limit]


def fuse_flat_and_graph(
    flat_ids: list[str],
    graph_ids: list[str],
    *,
    rrf_k: int = 60,
    graph_weight: float = 0.5,
) -> list[tuple[str, float]]:
    """Flat-dominant weighted Reciprocal Rank Fusion.

    ``score(id) = 1/(rrf_k + rank_flat) + graph_weight * 1/(rrf_k + rank_graph)``

    With ``graph_weight <= 1`` the flat list dominates: the flat rank-1 note
    (contribution ``1/(rrf_k+1)``) can never be overtaken by a graph-ONLY note
    (max contribution ``graph_weight/(rrf_k+1)``). A note present in BOTH lists
    accumulates from both and is promoted. Returns ``[(id, score)]`` descending;
    ties broken by flat order (stable)."""
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for rank, nid in enumerate(flat_ids, start=1):
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (rrf_k + rank)
        order.setdefault(nid, rank)
    for rank, nid in enumerate(graph_ids, start=1):
        scores[nid] = scores.get(nid, 0.0) + graph_weight * (1.0 / (rrf_k + rank))
        order.setdefault(nid, 10_000 + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def graph_augmented_ranking(
    query: str,
    flat_ids: list[str],
    lexicon: EntityLexicon,
    graph: LinkGraph,
    *,
    depth: int = 2,
    graph_weight: float = 0.5,
    rrf_k: int = 60,
    seed_flat_top: int = 3,
    candidate_limit: int = 30,
) -> tuple[bool, list[str], dict]:
    """Return ``(fired, ranked_ids, trace)``.

    ``fired`` is False for single-hop queries — then ``ranked_ids == flat_ids``
    unchanged. When fired, seeds = named entities UNION the top ``seed_flat_top``
    flat hits; graph candidates are fused into the flat ranking (flat-dominant)."""
    mentions = lexicon.mentions(query)
    fired = is_multihop_shaped(mentions)
    trace: dict = {
        "fired": fired,
        "entities": [m.surface for m in mentions],
        "non_hub_entities": [m.surface for m in mentions if not m.is_hub],
    }
    if not fired:
        return False, list(flat_ids), trace

    entity_seeds = [m.note_id for m in mentions]
    seeds = list(dict.fromkeys(entity_seeds + flat_ids[:seed_flat_top]))
    graph_ids = rank_graph_candidates(
        graph, seeds, depth=depth, limit=candidate_limit
    )
    trace["seeds"] = seeds
    trace["graph_candidates"] = graph_ids
    if not graph_ids:
        # No reachable neighbourhood — nothing to fuse; flat order stands.
        return True, list(flat_ids), trace
    fused = fuse_flat_and_graph(
        flat_ids, graph_ids, rrf_k=rrf_k, graph_weight=graph_weight
    )
    return True, [nid for nid, _ in fused], trace
