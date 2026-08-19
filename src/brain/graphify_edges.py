"""Inferred graph-edge scoring."""
from __future__ import annotations

import datetime
from typing import Any, Callable

from .graph import LinkGraph


def _rank_neighbours(
    conn: Any,
    backend: Any,
    note_id: str,
    vector: list[float],
    chunk_to_note: dict[int, str],
    vectors: dict[str, list[float]],
    *,
    cosine: Callable[[list[float], list[float]], float],
    topk: int,
    probe_k: int,
) -> list[tuple[str, float]]:
    """Rank vector neighbours for one note."""
    best_per_neighbour: dict[str, float] = {}
    for chunk_row_id, _backend_score in backend.search(conn, vector, probe_k):
        neighbour = chunk_to_note.get(int(chunk_row_id))
        if neighbour is None or neighbour == note_id or neighbour not in vectors:
            continue
        similarity = cosine(vector, vectors[neighbour])
        if similarity > best_per_neighbour.get(neighbour, -1.0):
            best_per_neighbour[neighbour] = similarity
    return sorted(best_per_neighbour.items(), key=lambda item: -item[1])[:topk]


def _record_edge_candidate(
    seen_pairs: dict[tuple[str, str], dict[str, Any]],
    link_graph: LinkGraph,
    note_id: str,
    neighbour: str,
    similarity: float,
    updated_by_id: dict[str, Any],
    *,
    today: datetime.date,
    score_floor: float,
    bridge_boost: Callable[[LinkGraph, str, str], tuple[float, int]],
    recency_boost: Callable[[datetime.date, Any, Any], float],
) -> None:
    """Score and retain one candidate edge when it clears the gates."""
    if similarity < score_floor:
        return
    if neighbour in link_graph.undirected_adj.get(note_id, set()):
        return
    pair = (
        (note_id, neighbour)
        if note_id < neighbour
        else (neighbour, note_id)
    )
    if pair in seen_pairs and seen_pairs[pair]["cosine"] >= similarity:
        return
    bridge, shared = bridge_boost(link_graph, note_id, neighbour)
    recency = recency_boost(
        today,
        updated_by_id.get(note_id),
        updated_by_id.get(neighbour),
    )
    score = similarity * (1.0 + bridge + recency)
    reason = f"embedding cosine {similarity:.3f}"
    if shared:
        reason += f"; {shared} shared wikilink neighbour(s)"
    if recency > 0:
        reason += "; both recently updated"
    seen_pairs[pair] = {
        "kind": "INFERRED",
        "from": pair[0],
        "to": pair[1],
        "cosine": round(similarity, 6),
        "score": round(score, 6),
        "reason": reason,
    }


def _collect_edge_candidates(
    conn: Any,
    backend: Any,
    link_graph: LinkGraph,
    ids: list[str],
    vectors: dict[str, list[float]],
    chunk_to_note: dict[int, str],
    updated_by_id: dict[str, Any],
    *,
    today: datetime.date,
    topk: int,
    score_floor: float,
    cosine: Callable[[list[float], list[float]], float],
    bridge_boost: Callable[[LinkGraph, str, str], tuple[float, int]],
    recency_boost: Callable[[datetime.date, Any, Any], float],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Collect scored inferred-edge candidates before degree capping."""
    probe_k = max(topk * 4, topk + 10)
    seen_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for note_id in ids:
        ranked = _rank_neighbours(
            conn,
            backend,
            note_id,
            vectors[note_id],
            chunk_to_note,
            vectors,
            cosine=cosine,
            topk=topk,
            probe_k=probe_k,
        )
        for neighbour, similarity in ranked:
            _record_edge_candidate(
                seen_pairs,
                link_graph,
                note_id,
                neighbour,
                similarity,
                updated_by_id,
                today=today,
                score_floor=score_floor,
                bridge_boost=bridge_boost,
                recency_boost=recency_boost,
            )
    return seen_pairs


def _select_capped_edges(
    seen_pairs: dict[tuple[str, str], dict[str, Any]],
    *,
    topk: int,
    global_cap_multiplier: float,
    explicit_edge_count: int,
) -> list[dict[str, Any]]:
    """Apply global and per-note caps to scored candidates."""
    global_cap = int(global_cap_multiplier * explicit_edge_count)
    ordered = sorted(seen_pairs.values(), key=lambda edge: -edge["score"])
    degree: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for edge in ordered:
        if len(selected) >= global_cap:
            break
        first, second = edge["from"], edge["to"]
        if degree.get(first, 0) >= topk or degree.get(second, 0) >= topk:
            continue
        degree[first] = degree.get(first, 0) + 1
        degree[second] = degree.get(second, 0) + 1
        selected.append(edge)
    return selected


def build_inferred_edges(
    conn: Any,
    backend: Any,
    link_graph: LinkGraph,
    *,
    today: datetime.date,
    topk: int,
    score_floor: float,
    global_cap_multiplier: float,
    explicit_edge_count: int,
    note_vectors: Callable[[Any, Any], dict[str, list[float]]],
    cosine: Callable[[list[float], list[float]], float],
    bridge_boost: Callable[[LinkGraph, str, str], tuple[float, int]],
    recency_boost: Callable[[datetime.date, Any, Any], float],
) -> list[dict[str, Any]]:
    """Build capped inferred edges from persisted vectors."""
    vectors = note_vectors(conn, backend)
    ids = sorted(vectors)
    if len(ids) < 2:
        return []

    chunk_to_note = {
        int(chunk_row_id): note_id
        for note_id, chunk_row_id in conn.execute(
            "SELECT n.id, c.rowid FROM chunks c JOIN notes n ON n.rowid = c.note_rowid"
        ).fetchall()
    }
    updated_by_id = dict(conn.execute("SELECT id, updated FROM notes").fetchall())

    seen_pairs = _collect_edge_candidates(
        conn,
        backend,
        link_graph,
        ids,
        vectors,
        chunk_to_note,
        updated_by_id,
        today=today,
        topk=topk,
        score_floor=score_floor,
        cosine=cosine,
        bridge_boost=bridge_boost,
        recency_boost=recency_boost,
    )
    return _select_capped_edges(
        seen_pairs,
        topk=topk,
        global_cap_multiplier=global_cap_multiplier,
        explicit_edge_count=explicit_edge_count,
    )
