"""Build graph-report semantic sections."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

import numpy as np

from .graphreport_sections import (
    _DUP_DISPLAY_LIMIT,
    _MISMATCH_DISPLAY_LIMIT,
    _MISMATCH_LOW,
    _NEAR_DUP_THRESHOLD,
    _NEIGHBORS_K,
    _SRC_DUP_RATE,
    _SRC_GIANT_COMPONENT,
    _SRC_LINK_DENSITY,
    _SRC_VAULT_CONVENTION,
    _status_band,
    _union_find_clusters,
)


def _collect_semantic_points(
    core: Any,
    conn: Any,
    live_ids: set[str],
) -> tuple[list[str], list[np.ndarray]]:
    """Collect normalized note-level embedding points."""
    chunk_rows = conn.execute("SELECT rowid, note_rowid FROM chunks").fetchall()
    chunk_rowid_by_note: dict[int, list[int]] = defaultdict(list)
    for chunk_rowid, note_rowid in chunk_rows:
        chunk_rowid_by_note[note_rowid].append(chunk_rowid)
    id_by_rowid = {
        row[0]: row[1] for row in conn.execute("SELECT rowid, id FROM notes").fetchall()
    }
    all_chunk_rowids = [
        chunk_rowid
        for chunk_rowids in chunk_rowid_by_note.values()
        for chunk_rowid in chunk_rowids
    ]
    vectors_by_chunk = (
        core.index.backend.get_vectors(conn, all_chunk_rowids)
        if all_chunk_rowids else {}
    )
    point_ids: list[str] = []
    mean_vectors: list[np.ndarray] = []
    for note_rowid, chunk_rowids in chunk_rowid_by_note.items():
        note_id = id_by_rowid.get(note_rowid)
        if note_id is None or note_id not in live_ids:
            continue
        raw_vectors = [
            np.asarray(vectors_by_chunk[chunk_rowid], dtype=np.float64)
            for chunk_rowid in chunk_rowids
            if chunk_rowid in vectors_by_chunk
        ]
        if not raw_vectors:
            continue
        mean_vector = np.mean(np.vstack(raw_vectors), axis=0)
        norm = np.linalg.norm(mean_vector)
        if norm == 0:
            continue
        point_ids.append(note_id)
        mean_vectors.append(mean_vector / norm)
    return point_ids, mean_vectors


def _project_semantic_points(
    point_ids: list[str],
    mean_vectors: list[np.ndarray],
    note_by_id: dict[str, dict[str, Any]],
    live_graph_ids: set[str],
) -> dict[str, Any]:
    """Project normalized vectors into finite three-dimensional coordinates."""
    point_count = len(mean_vectors)
    explained_variance = [0.0, 0.0, 0.0]
    if point_count < 2:
        return {
            "point_count": point_count,
            "semantic_note": (
                "semantic layer unavailable — fewer than 2 live notes carry an embedding "
                "(empty/unavailable vector table); showing the link view only."
            ),
            "points": [],
            "explained_variance": explained_variance,
            "matrix": None,
        }
    matrix = np.vstack(mean_vectors)
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    _u, singular_values, right_vectors = np.linalg.svd(centered, full_matrices=False)
    total_variance = float(np.sum(singular_values ** 2))
    component_count = min(3, right_vectors.shape[0])
    coordinates = np.zeros((point_count, 3))
    if total_variance:
        explained_variance = [
            float((singular_values[index] ** 2) / total_variance)
            for index in range(component_count)
        ]
        explained_variance += [0.0] * (3 - component_count)
        coordinates[:, :component_count] = centered @ right_vectors[:component_count].T
    if not np.all(np.isfinite(coordinates)) or not np.all(np.isfinite(explained_variance)):
        return {
            "point_count": point_count,
            "semantic_note": (
                "semantic layer unavailable — PCA produced non-finite values "
                "(degenerate embedding matrix); showing the link view only."
            ),
            "points": [],
            "explained_variance": [0.0, 0.0, 0.0],
            "matrix": None,
        }
    points = []
    for index, note_id in enumerate(point_ids):
        note = note_by_id.get(note_id, {})
        points.append({
            "id": note_id,
            "title": note.get("title", note_id),
            "type": note.get("type", ""),
            "classification": note.get("classification", "MNPI"),
            "zone": note.get("zone", "unknown"),
            "x": round(float(coordinates[index, 0]), 6),
            "y": round(float(coordinates[index, 1]), 6),
            "z": round(float(coordinates[index, 2]), 6),
            "in_graph": note_id in live_graph_ids,
        })
    return {
        "point_count": point_count,
        "semantic_note": None,
        "points": points,
        "explained_variance": explained_variance,
        "matrix": matrix,
    }


def _exact_duplicate_pairs(
    conn: Any,
    live_ids: set[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Find exact body duplicates among live notes."""
    body_rows = conn.execute("SELECT id, body FROM notes").fetchall()
    body_groups: dict[str, list[str]] = defaultdict(list)
    for note_id, body in body_rows:
        if note_id not in live_ids:
            continue
        normalized = re.sub(r"\s+", " ", body or "").strip()
        if normalized:
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            body_groups[digest].append(note_id)
    pairs: list[tuple[str, str]] = []
    for ids in body_groups.values():
        ordered_ids = sorted(ids)
        pairs.extend(
            (ordered_ids[left], ordered_ids[right])
            for left in range(len(ordered_ids))
            for right in range(left + 1, len(ordered_ids))
        )
    return pairs, sorted({note_id for pair in pairs for note_id in pair})


def _display_similarity_pairs(
    exact_pairs: list[tuple[str, str]],
    near_pairs_idx: list[tuple[int, int]],
    similarity: np.ndarray,
    point_ids: list[str],
    note_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build the bounded duplicate display list and its marked ids."""
    display_pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for left, right in exact_pairs:
        key = (left, right) if left < right else (right, left)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        display_pairs.append({
            "cosine": 1.0,
            "a": left,
            "a_title": note_by_id.get(left, {}).get("title", left),
            "b": right,
            "b_title": note_by_id.get(right, {}).get("title", right),
        })
    scored_near = sorted(
        (
            (float(similarity[left, right]), point_ids[left], point_ids[right])
            for left, right in near_pairs_idx
        ),
        key=lambda item: -item[0],
    )
    for score, left, right in scored_near:
        key = (left, right) if left < right else (right, left)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        display_pairs.append({
            "cosine": round(score, 4),
            "a": left,
            "a_title": note_by_id.get(left, {}).get("title", left),
            "b": right,
            "b_title": note_by_id.get(right, {}).get("title", right),
        })
    display_pairs.sort(key=lambda pair: -pair["cosine"])
    bounded = display_pairs[:_DUP_DISPLAY_LIMIT]
    marked_ids = {pair["a"] for pair in bounded} | {pair["b"] for pair in bounded}
    return bounded, marked_ids


def _similarity_metrics(
    conn: Any,
    live_ids: set[str],
    point_ids: list[str],
    matrix: np.ndarray,
    note_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Calculate duplicate and classification-mismatch metrics."""
    exact_pairs, exact_dup_ids = _exact_duplicate_pairs(conn, live_ids)
    similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -1.0)
    near_indices = np.argwhere(similarity >= _NEAR_DUP_THRESHOLD)
    near_indices = near_indices[near_indices[:, 0] < near_indices[:, 1]]
    near_pairs_idx = [(int(left), int(right)) for left, right in near_indices]
    clusters = _union_find_clusters(len(point_ids), near_pairs_idx)
    real_clusters = [cluster for cluster in clusters if len(cluster) >= 2]
    near_member_ids = sorted({point_ids[index] for cluster in real_clusters for index in cluster})
    duplicate_pairs, duplicate_ids = _display_similarity_pairs(
        exact_pairs, near_pairs_idx, similarity, point_ids, note_by_id,
    )
    mismatch_indices = np.argwhere(
        (similarity >= _MISMATCH_LOW) & (similarity < _NEAR_DUP_THRESHOLD)
    )
    mismatch_indices = mismatch_indices[mismatch_indices[:, 0] < mismatch_indices[:, 1]]
    mismatch_scored = sorted(
        (
            (float(similarity[left, right]), point_ids[left], point_ids[right])
            for left, right in mismatch_indices
        ),
        key=lambda item: -item[0],
    )[:_MISMATCH_DISPLAY_LIMIT]
    mismatch_pairs = [
        {
            "cosine": round(score, 4),
            "a": left,
            "a_title": note_by_id.get(left, {}).get("title", left),
            "b": right,
            "b_title": note_by_id.get(right, {}).get("title", right),
        }
        for score, left, right in mismatch_scored
    ]
    mismatch_ids = {pair["a"] for pair in mismatch_pairs} | {
        pair["b"] for pair in mismatch_pairs
    }
    return {
        "similarity": similarity,
        "duplicate_pairs": duplicate_pairs,
        "duplicate_ids": duplicate_ids,
        "mismatch_pairs": mismatch_pairs,
        "mismatch_ids": mismatch_ids,
        "near_dup_pairs_idx": near_pairs_idx,
        "near_dup_cluster_count": len(real_clusters),
        "mismatch_pairs_raw_count": len(point_ids) * (len(point_ids) - 1) // 2,
        "exact_dup_pair_count": len(exact_pairs),
        "exact_dup_ids": exact_dup_ids,
        "near_dup_member_ids": near_member_ids,
    }


def _neighbor_metrics(
    similarity: np.ndarray,
    point_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Return the bounded nearest-neighbor list for each semantic point."""
    neighbors: dict[str, list[dict[str, Any]]] = {}
    neighbor_count = min(_NEIGHBORS_K, len(point_ids) - 1)
    if neighbor_count <= 0:
        return neighbors
    top_indices = np.argpartition(
        -similarity, kth=neighbor_count - 1, axis=1,
    )[:, :neighbor_count]
    for index, note_id in enumerate(point_ids):
        scored = sorted(
            ((float(similarity[index, neighbor]), point_ids[neighbor])
             for neighbor in top_indices[index]),
            key=lambda item: -item[0],
        )
        neighbors[note_id] = [
            {"id": neighbor_id, "cosine": round(score, 4)}
            for score, neighbor_id in scored
            if score > -0.5
        ]
    return neighbors


def build_semantic_section(
    core: Any,
    *,
    conn: Any,
    note_by_id: dict[str, dict[str, Any]],
    live_ids: set[str],
    live_graph_ids: set[str],
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build PCA, duplicate, mismatch, and neighbor report fields."""
    point_ids, mean_vectors = _collect_semantic_points(core, conn, live_ids)
    projection = _project_semantic_points(
        point_ids, mean_vectors, note_by_id, live_graph_ids,
    )
    matrix = projection["matrix"]
    similarity_data = {
        "duplicate_pairs": [],
        "mismatch_pairs": [],
        "near_dup_pairs_idx": [],
        "near_dup_cluster_count": 0,
        "mismatch_pairs_raw_count": 0,
        "exact_dup_pair_count": 0,
        "exact_dup_ids": [],
        "near_dup_member_ids": [],
        "duplicate_ids": set(),
    }
    neighbors: dict[str, list[dict[str, Any]]] = {}
    if matrix is not None:
        similarity_data = _similarity_metrics(
            conn, live_ids, point_ids, matrix, note_by_id,
        )
        neighbors = _neighbor_metrics(similarity_data["similarity"], point_ids)
        for node in nodes:
            node["dup_suspect"] = node["id"] in similarity_data["duplicate_ids"]
            node["mismatch_flag"] = node["id"] in {
                pair["a"] for pair in similarity_data["mismatch_pairs"]
            } | {pair["b"] for pair in similarity_data["mismatch_pairs"]}
    return {
        "point_count": projection["point_count"],
        "semantic_note": projection["semantic_note"],
        "points": projection["points"],
        "explained_variance": projection["explained_variance"],
        "duplicate_pairs": similarity_data["duplicate_pairs"],
        "mismatch_pairs": similarity_data["mismatch_pairs"],
        "neighbors": neighbors,
        "near_dup_pairs_idx": similarity_data["near_dup_pairs_idx"],
        "near_dup_cluster_count": similarity_data["near_dup_cluster_count"],
        "mismatch_pairs_raw_count": similarity_data["mismatch_pairs_raw_count"],
        "exact_dup_pair_count": similarity_data["exact_dup_pair_count"],
        "exact_dup_ids": similarity_data["exact_dup_ids"],
        "near_dup_member_ids": similarity_data["near_dup_member_ids"],
    }


def _target_metrics(
    link: dict[str, Any],
    hygiene: dict[str, Any],
    semantic: dict[str, Any],
) -> dict[str, Any]:
    """Prepare the scalar inputs shared by graph benchmark target rows."""
    knowledge_note_count = hygiene["knowledge_note_count"]
    hygiene_orphan_count = hygiene["orphan_count"]
    hygiene_island_count = hygiene["island_count"]
    brain_link_n_total = knowledge_note_count
    brain_link_n_current = knowledge_note_count - hygiene_orphan_count
    brain_link_pct = (
        round(100.0 * brain_link_n_current / brain_link_n_total, 1)
        if brain_link_n_total else 0.0
    )
    brain_ids = link["brain_ids"]
    brain_components = link["brain_components"]
    main_component_size = (
        link["brain_component_sizes_top5"][0]
        if link["brain_component_sizes_top5"] else 0
    )
    giant_component_pct = (
        round(100.0 * main_component_size / len(brain_ids), 1)
        if brain_ids else 100.0
    )
    non_main_ids = (
        sorted(nid for component in brain_components[1:] for nid in component)
        if len(brain_components) > 1 else []
    )
    point_count = semantic["point_count"]
    exact_dup_ids = semantic["exact_dup_ids"]
    duplicate_rate_pct = (
        round(100.0 * len(exact_dup_ids) / point_count, 2)
        if point_count else 0.0
    )
    return {
        "knowledge_note_count": knowledge_note_count,
        "hygiene_island_count": hygiene_island_count,
        "brain_link_n_total": brain_link_n_total,
        "brain_link_n_current": brain_link_n_current,
        "brain_link_pct": brain_link_pct,
        "brain_ids": brain_ids,
        "brain_components": brain_components,
        "giant_component_pct": giant_component_pct,
        "non_main_ids": non_main_ids,
        "point_count": point_count,
        "exact_dup_ids": exact_dup_ids,
        "duplicate_rate_pct": duplicate_rate_pct,
        "dangling": link["dangling"],
        "near_dup_pairs_idx": semantic["near_dup_pairs_idx"],
        "near_dup_cluster_count": semantic["near_dup_cluster_count"],
    }


def build_targets_section(
    link: dict[str, Any], hygiene: dict[str, Any], semantic: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build benchmark target rows from structural and semantic metrics."""
    metrics = _target_metrics(link, hygiene, semantic)
    knowledge_note_count = metrics["knowledge_note_count"]
    hygiene_island_count = metrics["hygiene_island_count"]
    brain_link_n_total = metrics["brain_link_n_total"]
    brain_link_n_current = metrics["brain_link_n_current"]
    brain_link_pct = metrics["brain_link_pct"]
    islands_beyond_main = hygiene_island_count - 1 if hygiene_island_count else 0
    giant_component_pct = metrics["giant_component_pct"]
    non_main_ids = metrics["non_main_ids"]
    point_count = metrics["point_count"]
    exact_dup_ids = metrics["exact_dup_ids"]
    duplicate_rate_pct = metrics["duplicate_rate_pct"]
    dangling = metrics["dangling"]
    near_dup_pairs_idx = metrics["near_dup_pairs_idx"]
    near_dup_cluster_count = metrics["near_dup_cluster_count"]

    return [
        {
            "id": "brain_link_density", "label": "brain/-zone notes with ≥1 wikilink",
            "current": brain_link_pct, "unit": "%", "direction": "higher_pct",
            "excellent": 95.0, "acceptable": 80.0, "aspirational": 100.0,
            "status": _status_band(brain_link_pct, 95.0, 80.0, "higher_pct"),
            "n_current": brain_link_n_current, "n_total": brain_link_n_total,
            "source": _SRC_LINK_DENSITY,
            "offending_ids": hygiene["orphan_ids"],
        },
        {
            "id": "true_orphans",
            "label": "Isolated knowledge notes (degree = 0, brain/ layer)",
            "current": len(link["knowledge_layer_isolated_ids"]), "unit": "count",
            "direction": "lower_count", "excellent": 0, "acceptable": 5,
            "status": _status_band(
                len(link["knowledge_layer_isolated_ids"]), 0, 5, "lower_count",
            ),
            "n_total": knowledge_note_count,
            "whole_vault_isolated": len(link["truly_isolated_ids"]),
            "source": _SRC_VAULT_CONVENTION,
            "offending_ids": link["knowledge_layer_isolated_ids"],
        },
        {
            "id": "brain_islands", "label": "Giant-component share (brain/ zone, live notes)",
            "current": giant_component_pct, "unit": "%", "direction": "higher_pct",
            "excellent": 90.0, "acceptable": 75.0,
            "status": _status_band(giant_component_pct, 90.0, 75.0, "higher_pct"),
            "n_total": hygiene_island_count, "islands_beyond_main": islands_beyond_main,
            "source": _SRC_GIANT_COMPONENT, "offending_ids": non_main_ids,
        },
        {
            "id": "exact_dup_pairs", "label": "Exact-duplicate note rate (sha256-identical bodies)",
            "current": duplicate_rate_pct, "unit": "%", "direction": "lower_pct",
            "excellent": 1.0, "acceptable": 5.0, "aspirational": 0.0,
            "status": _status_band(duplicate_rate_pct, 1.0, 5.0, "lower_pct"),
            "n_total": point_count, "pair_count": semantic["exact_dup_pair_count"],
            "source": _SRC_DUP_RATE, "offending_ids": exact_dup_ids,
        },
        {
            "id": "near_dup_clusters_per_1k",
            "label": f"Near-duplicate clusters (seed-anchored, cosine≥{_NEAR_DUP_THRESHOLD:.2f}) per 1k notes",
            "current": round(near_dup_cluster_count * 1000.0 / point_count, 1)
            if point_count else 0.0,
            "unit": "per 1k", "direction": "lower_count", "status": "trend",
            "n_total": point_count,
            "source": {
                "label": "heuristic, trend-only — union-find over every "
                         f"cosine≥{_NEAR_DUP_THRESHOLD:.2f} pair "
                         f"({len(near_dup_pairs_idx)} raw pairs collapse to "
                         f"{near_dup_cluster_count} clusters). No citable external benchmark "
                         "found for this threshold/metric during the 2026-07-21 research pass "
                         "— stays trend-only rather than inventing a band.",
                "url": None, "confidence": "no external basis — engineering heuristic",
            },
            "offending_ids": semantic["near_dup_member_ids"][:200],
        },
        {
            "id": "dangling_wikilinks", "label": "Dangling wikilink targets",
            "current": len(dangling), "unit": "count", "direction": "lower_count",
            "excellent": 0, "acceptable": 5,
            "status": _status_band(len(dangling), 0, 5, "lower_count"),
            "n_total": len(link["all_edges_raw"]), "source": _SRC_VAULT_CONVENTION,
            "offending_ids": [
                edge["to"] if edge["to"] not in link["note_by_id"] else edge["from"]
                for edge in dangling[:20]
            ],
        },
    ]
