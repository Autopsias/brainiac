"""Graphify discovery build (GRF-01, ADR-0003 Ruling 6 + Ruling (a)).

Periodically builds a derived, DISCOVERY-ONLY graph: nodes = indexed notes,
edges = explicit wikilinks (``kind: WIKILINK``, exact reuse of
``brain.graph.build_graph``) plus a capped, scored layer of embedding-neighbour
proposals (``kind: INFERRED``). Same doctrine as ``brain.graph``:
``authoritative: false``, never authoritative on its own, and NEVER
auto-written into a note body — INFERRED edges are candidates for human review
only (a hot-queue entry / ``brain graphify`` output), gated through
``egress.apply_gate`` before they reach any surface, exactly like
``graph_expand`` candidates.

ADR-0003 Ruling (a) explicitly SUPERSEDES the earlier "documented only"
disposition (``core.py`` graphify branch, ``routines/manifest.json`` row
graphify-discovery) on two grounds, both closed by this module's design:

  1. *"No clean fold"* — this module gives the maintain branch a single
     function to call, exactly like ``health``/``integrity``.
  2. *"Runtime budget"* — three caps bound the monthly build:
       * **Drift gate** — a corpus manifest keyed by ``(note id, content
         hash)``; unchanged since the last build => a no-op in milliseconds
         (``manifest_unchanged``). The content hash is the SAME
         ``content_hash`` column the index's own incremental sync (IDX-03)
         already maintains — this module never re-hashes note bodies.
       * **Embedding reuse** — INFERRED edges are scored from vectors ALREADY
         stored in the index (``note_vectors`` reads the persisted per-chunk
         vectors via the vector backend's ``get_vectors``/``search``
         contract); this module never re-embeds the corpus.
       * **Wall-clock budget** — the caller (``BrainCore.graphify``) times the
         build and flags ``action_required`` past a 5-minute soft ceiling
         (target <=60s at the current corpus scale, ADR-0003 Ruling 6).

Provenance note (session s10): ADR-0003 Appendix B pins ONLY
``90 System/_ppr/ppr.py`` from the reference vault (already reused by s08's
whole-corpus PageRank in ``brain.graph.revisit_sample``) — no
``_graphify``-named script is in the fingerprint list. Per the session brief
this build therefore ports the DESIGN directly from Ruling 6 (this module has
no upstream file to sha256-verify), not any unverified reference-vault file.

INFERRED-edge precision (HARDENED:grill): raw cosine similarity is tempered by
(a) a small "2-hop bridge" boost when two notes already share a wikilink
neighbour (a structural signal that they are plausibly related even though
they are not directly linked), and (b) a small recency boost for two recently
updated notes. Skip rules: no self-links (guaranteed — pairs are built over
``i < j`` distinct ids); no already-linked pairs (skipped against the
explicit wikilink graph); no frontmatter/code-fence "anchors" — moot by
construction, because INFERRED edges score MEAN CHUNK VECTORS, and chunks are
built from ``note.body`` which ``frontmatter.parse_text`` has already split
the YAML frontmatter block out of (``brain.notes.load_note``) before
``chunk_text`` ever sees it; a code fence inside the body is just more text
fed to the embedder, not a link anchor, so there is no anchor-parsing path
for stray frontmatter/code-fence content to leak into.
"""
from __future__ import annotations

import datetime
import math
from typing import Any

from .graph import LinkGraph

GRAPH_SCHEMA_VERSION = 1
PROVENANCE = "graphify-derived (discovery-only)"

# ADR-0003 Ruling 6 caps.
DEFAULT_TOPK = 5                        # per-note INFERRED cap (k <= 5)
DEFAULT_SCORE_FLOOR = 0.72              # fixed cosine threshold for a proposal
DEFAULT_GLOBAL_CAP_MULTIPLIER = 2.0     # INFERRED <= 2x explicit-edge count
DEFAULT_BUDGET_SECONDS = 60.0           # ADR target at current corpus scale
ACTION_REQUIRED_SECONDS = 300.0         # ADR: log action_required past 5 min

# HARDENED:grill tempering constants — small, capped nudges; cosine (already
# floor-gated) stays the dominant term.
_BRIDGE_BOOST_PER_SHARED = 0.05
_BRIDGE_BOOST_CAP = 0.15
_RECENCY_BOOST_CAP = 0.10
_RECENCY_HALF_LIFE_DAYS = 730.0         # ~2 years: beyond this, no recency boost


def corpus_manifest(conn) -> dict[str, str]:
    """Per-note ``id -> content_hash`` manifest. REUSES the index's own
    ``content_hash`` column (already maintained by incremental sync's
    path+hash comparison, IDX-03) instead of re-hashing note bodies."""
    rows = conn.execute("SELECT id, content_hash FROM notes").fetchall()
    return {r[0]: r[1] or "" for r in rows}


def manifest_unchanged(old_state: dict[str, Any] | None, new_manifest: dict[str, str]) -> bool:
    """True iff the corpus identity is unchanged since the last build — the
    drift gate (ADR-0003 Ruling 6, ground 2). ``old_state`` is the persisted
    ``manifest.json`` dict (``{"notes": {...}, "generation": N, ...}``)."""
    if not old_state:
        return False
    return old_state.get("notes") == new_manifest


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def note_vectors(conn, backend) -> dict[str, list[float]]:
    """One representative vector per note: the mean of its chunk vectors
    ALREADY stored by the index (embedding reuse — this never re-embeds).
    A note with no indexed chunks (e.g. an empty body) is simply absent."""
    note_rows = conn.execute("SELECT rowid, id FROM notes").fetchall()
    chunk_rows = conn.execute("SELECT rowid, note_rowid FROM chunks").fetchall()
    by_note: dict[int, list[int]] = {}
    for crowid, nrowid in chunk_rows:
        by_note.setdefault(nrowid, []).append(crowid)
    all_chunk_ids = [c for cs in by_note.values() for c in cs]
    vecs = backend.get_vectors(conn, all_chunk_ids) if all_chunk_ids else {}
    out: dict[str, list[float]] = {}
    for note_rowid, note_id in note_rows:
        cvecs = [vecs[c] for c in by_note.get(note_rowid, []) if c in vecs]
        if not cvecs:
            continue
        dim = len(cvecs[0])
        out[note_id] = [sum(v[i] for v in cvecs) / len(cvecs) for i in range(dim)]
    return out


def _bridge_boost(link_graph: LinkGraph, a: str, b: str) -> tuple[float, int]:
    """A small boost when ``a``/``b`` already share a wikilink neighbour (a
    "2-hop bridge" — structural evidence they are plausibly related even
    though no direct link exists yet)."""
    adj = link_graph.undirected_adj
    shared = len(adj.get(a, set()) & adj.get(b, set()))
    return min(shared, 3) * _BRIDGE_BOOST_PER_SHARED, shared


def _parse_date(raw: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _recency_boost(today: datetime.date, updated_a: Any, updated_b: Any) -> float:
    """A small boost for two notes both updated recently. Missing/unparsable
    dates contribute no boost (neutral) rather than raising."""
    da, db = _parse_date(updated_a), _parse_date(updated_b)
    if da is None or db is None:
        return 0.0
    avg_age = ((today - da).days + (today - db).days) / 2.0
    frac = max(0.0, 1.0 - avg_age / _RECENCY_HALF_LIFE_DAYS)
    return frac * _RECENCY_BOOST_CAP


def build_inferred_edges(
    conn,
    backend,
    link_graph: LinkGraph,
    *,
    today: datetime.date,
    topk: int = DEFAULT_TOPK,
    score_floor: float = DEFAULT_SCORE_FLOOR,
    global_cap_multiplier: float = DEFAULT_GLOBAL_CAP_MULTIPLIER,
    explicit_edge_count: int = 0,
) -> list[dict[str, Any]]:
    """Capped, scored embedding-neighbour proposals (ADR-0003 Ruling 6).

    For each note, probe the vector backend (ANN under sqlite-vec, exact under
    the brute-force fallback — same adapter contract ``near_dup`` already
    uses) with that note's mean chunk vector to NOMINATE candidate neighbours,
    then recompute TRUE cosine between the two notes' own mean vectors
    (backend-independent scoring, mirrors ``BrainIndex.near_dup``). Skip
    rules: no self (guaranteed by construction), no already-linked pair
    (checked against ``link_graph``). A qualifying pair's raw cosine must
    clear ``score_floor``; the final ``score`` additionally tempers cosine
    with a 2-hop bridge boost + a recency boost (HARDENED:grill) and is what
    the greedy cap-respecting selection below ranks by.

    Caps are enforced ONCE, globally, by a single greedy walk over ALL
    qualifying candidate pairs sorted by final score descending: a pair is
    selected only while BOTH endpoints are still under the per-note ``topk``
    degree AND the running total is still under
    ``global_cap_multiplier * explicit_edge_count``. This is what makes the
    per-note cap hold even for a node that is somebody else's top-k neighbour
    without being in its own — inbound proposals are capped exactly like
    outbound ones.
    """
    vecs = note_vectors(conn, backend)
    ids = sorted(vecs)
    if len(ids) < 2:
        return []

    chunk_to_note = {
        int(crid): nid
        for nid, crid in conn.execute(
            "SELECT n.id, c.rowid FROM chunks c JOIN notes n ON n.rowid = c.note_rowid"
        ).fetchall()
    }
    updated_by_id = dict(conn.execute("SELECT id, updated FROM notes").fetchall())

    probe_k = max(topk * 4, topk + 10)
    seen_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for a in ids:
        hits = backend.search(conn, vecs[a], probe_k)
        best_per_neighbour: dict[str, float] = {}
        for chunk_rowid, _backend_score in hits:
            b = chunk_to_note.get(int(chunk_rowid))
            if b is None or b == a or b not in vecs:
                continue
            cosine = _cosine(vecs[a], vecs[b])
            if cosine > best_per_neighbour.get(b, -1.0):
                best_per_neighbour[b] = cosine
        # This node's own top-`topk` neighbours by raw cosine, above the floor.
        ranked = sorted(best_per_neighbour.items(), key=lambda kv: -kv[1])[:topk]
        for b, cosine in ranked:
            if cosine < score_floor:
                continue
            if b in link_graph.undirected_adj.get(a, set()):
                continue  # already linked — no INFERRED duplicate of a real edge
            key = (a, b) if a < b else (b, a)
            if key in seen_pairs and seen_pairs[key]["cosine"] >= cosine:
                continue
            boost, shared = _bridge_boost(link_graph, a, b)
            recency = _recency_boost(today, updated_by_id.get(a), updated_by_id.get(b))
            score = cosine * (1.0 + boost + recency)
            reason = f"embedding cosine {cosine:.3f}"
            if shared:
                reason += f"; {shared} shared wikilink neighbour(s)"
            if recency > 0:
                reason += "; both recently updated"
            seen_pairs[key] = {
                "kind": "INFERRED", "from": key[0], "to": key[1],
                "cosine": round(cosine, 6), "score": round(score, 6),
                "reason": reason,
            }

    global_cap = int(global_cap_multiplier * explicit_edge_count)
    ordered = sorted(seen_pairs.values(), key=lambda e: -e["score"])
    degree: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for edge in ordered:
        if len(selected) >= global_cap:
            break
        a, b = edge["from"], edge["to"]
        if degree.get(a, 0) >= topk or degree.get(b, 0) >= topk:
            continue
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
        selected.append(edge)
    return selected


def build_graph_artifact(
    conn,
    backend,
    link_graph: LinkGraph,
    *,
    today: datetime.date,
    topk: int = DEFAULT_TOPK,
    score_floor: float = DEFAULT_SCORE_FLOOR,
    global_cap_multiplier: float = DEFAULT_GLOBAL_CAP_MULTIPLIER,
) -> dict[str, Any]:
    """The build proper (sans provenance/generation stamping, which the
    HOST-only caller adds — this stays a pure function of an open index
    connection + backend + a prebuilt wikilink graph)."""
    nodes = [
        {"id": r[0], "type": r[1], "classification": r[2]}
        for r in conn.execute("SELECT id, type, classification FROM notes").fetchall()
    ]
    wikilink_edges = [
        {"kind": "WIKILINK", "from": a, "to": b}
        for a, outs in sorted(link_graph.out.items())
        for b in sorted(outs)
    ]
    inferred_edges = build_inferred_edges(
        conn, backend, link_graph, today=today, topk=topk, score_floor=score_floor,
        global_cap_multiplier=global_cap_multiplier,
        explicit_edge_count=len(wikilink_edges),
    )
    return {
        "nodes": nodes,
        "edges": wikilink_edges + inferred_edges,
        "corpus": {
            "note_count": len(nodes),
            "explicit_edge_count": len(wikilink_edges),
            "inferred_edge_count": len(inferred_edges),
        },
    }


def validate_artifact(
    artifact: dict[str, Any], *, topk: int = DEFAULT_TOPK,
    global_cap_multiplier: float = DEFAULT_GLOBAL_CAP_MULTIPLIER,
) -> tuple[bool, list[str]]:
    """Schema + cap validation run BEFORE the atomic publish (HARDENED:codex):
    a build that fails this never replaces the consumable ``graph.json``."""
    problems: list[str] = []
    if artifact.get("authoritative") is not False:
        problems.append("authoritative must be false")
    if artifact.get("schema_version") != GRAPH_SCHEMA_VERSION:
        problems.append(f"schema_version must be {GRAPH_SCHEMA_VERSION}")
    if not artifact.get("provenance"):
        problems.append("missing provenance stamp")
    edges = artifact.get("edges") or []
    explicit = [e for e in edges if e.get("kind") == "WIKILINK"]
    inferred = [e for e in edges if e.get("kind") == "INFERRED"]
    cap = global_cap_multiplier * len(explicit)
    if len(inferred) > cap + 1e-9:
        problems.append(f"INFERRED edge count {len(inferred)} exceeds global cap {cap}")
    degree: dict[str, int] = {}
    for e in inferred:
        degree[e["from"]] = degree.get(e["from"], 0) + 1
        degree[e["to"]] = degree.get(e["to"], 0) + 1
    over = {n: d for n, d in degree.items() if d > topk}
    if over:
        problems.append(f"per-node INFERRED cap ({topk}) exceeded: {over}")
    return (not problems), problems


def top_candidates(edges: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Top-``limit`` INFERRED edges by score — the human-review candidate
    list (never auto-written into a note; review-only, hot-queue shaped by
    the caller)."""
    inferred = [e for e in edges if e.get("kind") == "INFERRED"]
    return sorted(inferred, key=lambda e: -e["score"])[:limit]


def read_published_inferred_edges(graph_json_path) -> list[tuple[str, str]]:
    """Read the published graph's INFERRED edges as ``(a, b)`` pairs, for the
    OPTIONAL ``graph_expand(..., use_inferred=True)`` consumer (ADR-0003
    Ruling 6, "Optional"). Degrades to ``[]`` on anything short of a valid,
    non-partial artifact — missing file, unreadable JSON, or a stale
    ``authoritative``/``schema_version`` stamp — never raises. A build failure
    marker lives at a SEPARATE path (``BUILD_FAILED.json``) by construction,
    so a partial build is never even a candidate for this reader to pick up."""
    import json as _json
    from pathlib import Path

    p = Path(graph_json_path)
    try:
        artifact = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(artifact, dict):
        return []
    if artifact.get("authoritative") is not False:
        return []
    if artifact.get("schema_version") != GRAPH_SCHEMA_VERSION:
        return []
    return [
        (e["from"], e["to"]) for e in artifact.get("edges") or []
        if e.get("kind") == "INFERRED" and e.get("from") and e.get("to")
    ]
