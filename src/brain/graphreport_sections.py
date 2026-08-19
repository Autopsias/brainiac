"""Build graph-report link sections."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


_TOP_HUBS_LIMIT = 20
_DUP_DISPLAY_LIMIT = 50
_MISMATCH_DISPLAY_LIMIT = 50
_NEIGHBORS_K = 8
_NEAR_DUP_THRESHOLD = 0.97
_MISMATCH_LOW = 0.90


def _para_zone(zone: str, path: str) -> str:
    if zone != "brain":
        return zone
    for sub in ("projects", "areas", "resources", "archive"):
        if f"/brain/{sub}/" in path:
            return sub
    return "brain-root"


def _connected_components_list(
    nodes: set[str], adj: dict[str, set[str]],
) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in nodes:
        if start in seen:
            continue
        component = [start]
        seen.add(start)
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbor in adj.get(current, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    component.append(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components


def _union_find_clusters(
    count: int, pairs: list[tuple[int, int]],
) -> list[list[int]]:
    parent = list(range(count))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    for left, right in pairs:
        union(left, right)
    groups: dict[int, list[int]] = defaultdict(list)
    for item in range(count):
        groups[find(item)].append(item)
    return list(groups.values())


def _status_band(
    current: float, excellent: float, acceptable: float, direction: str,
) -> str:
    """Classify a metric against its sourced excellent/acceptable band."""
    if direction in ("higher_pct", "higher_count"):
        if current >= excellent:
            return "excellent"
        if current >= acceptable:
            return "good"
        return "needs_work"
    if current <= excellent:
        return "excellent"
    if current <= acceptable:
        return "good"
    return "needs_work"


_SRC_LINK_DENSITY = {
    "label": "Zettelkasten community orphan-rate studies (forum quantitative "
              "link analyses, 5.3%-20.5% orphaned notes in practitioner vaults) "
              "+ Luhmann archive network analysis (~23% cards with no "
              "cross-branch links)",
    "url": "https://forum.zettelkasten.de/discussion/1419/quantitative-link-analysis",
    "confidence": "community heuristic — several independent practitioner vault "
                   "analyses agree on the range, none peer-reviewed",
}
_SRC_GIANT_COMPONENT = {
    "label": "Web Data Commons Hyperlink Graph (94% of 3.5B pages in the giant "
              "weakly-connected component) + Facebook social graph analysis "
              "(99.91% of users in one component)",
    "url": "http://www.webdatacommons.org/hyperlinkgraph/2012-08/topology.html",
    "confidence": "analogical, low confidence — these are huge, densely-grown "
                   "networks; a small, manually-curated vault legitimately runs "
                   "sparser, so the band is set well below those figures",
}
_SRC_DUP_RATE = {
    "label": "AHIMA \"Realistic Approach to a 1% Duplicate Record Error Rate\" "
              "(healthcare MDM) + industry duplicate-record-rate compilations "
              "(1% = emerging achievable benchmark, ~22% of orgs meet it; "
              "world-class ~0.14%)",
    "url": "https://ahima.org/media/m1pldevh/ahima-pim-whitepaper.pdf",
    "confidence": "strong evidence for record-dedup rate as a KPI shape; "
                   "analogical when applied to note bodies rather than CRM/MDM "
                   "records",
}
_SRC_VAULT_CONVENTION = {
    "label": "AGENTS.md §3 — every note should connect to ≥1 other; a note with "
              "zero edges of any kind or a dangling wikilink target is a "
              "structural defect, not a density judgment call",
    "url": None,
    "confidence": "vault convention, not externally benchmarked — this is an "
                  "absolute correctness rule, so excellent=0 not a soft band",
}


def _collect_link_data(conn: Any, graph: dict[str, Any]) -> dict[str, Any]:
    """Collect live note rows and graph edges for the link report."""
    all_nodes_raw = graph.get("nodes") or []
    all_edges_raw = graph.get("edges") or []
    notes_rows = conn.execute(
        "SELECT id, title, type, classification, zone, path, is_latest_version FROM notes"
    ).fetchall()
    note_by_id: dict[str, dict[str, Any]] = {}
    for nid, title, ntype, classification, zone, path, is_latest in notes_rows:
        note_by_id[nid] = {
            "title": title or nid,
            "type": ntype or "",
            "classification": classification or "MNPI",
            "zone": _para_zone(zone or "", path or ""),
            "raw_zone": zone or "",
            "path": path or "",
            "is_latest": str(is_latest or "").strip().lower() != "false",
        }
    live_ids = {nid for nid, note in note_by_id.items() if note["is_latest"]}
    all_ids_in_graph = {node["id"] for node in all_nodes_raw}
    live_graph_ids = {nid for nid in all_ids_in_graph if nid in live_ids}
    edges = [
        edge for edge in all_edges_raw
        if edge["from"] in live_graph_ids
        and edge["to"] in live_graph_ids
        and edge["from"] != edge["to"]
    ]
    dangling = [
        edge for edge in all_edges_raw
        if edge["from"] not in note_by_id or edge["to"] not in note_by_id
    ]
    return {
        "all_nodes_raw": all_nodes_raw,
        "all_edges_raw": all_edges_raw,
        "note_by_id": note_by_id,
        "live_ids": live_ids,
        "live_graph_ids": live_graph_ids,
        "edges": edges,
        "dangling": dangling,
    }


def _link_degrees(
    edges: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], dict[str, set[str]]]:
    """Calculate total degree, wikilink degree, and undirected adjacency."""
    degree: dict[str, int] = defaultdict(int)
    wiki_degree: dict[str, int] = defaultdict(int)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        degree[edge["from"]] += 1
        degree[edge["to"]] += 1
        adjacency[edge["from"]].add(edge["to"])
        adjacency[edge["to"]].add(edge["from"])
        if edge.get("kind") == "WIKILINK":
            wiki_degree[edge["from"]] += 1
            wiki_degree[edge["to"]] += 1
    return degree, wiki_degree, adjacency


def build_link_section(conn: Any, graph: dict[str, Any]) -> dict[str, Any]:
    """Build the live-note link graph portion of a report payload."""
    data = _collect_link_data(conn, graph)
    all_nodes_raw = data["all_nodes_raw"]
    all_edges_raw = data["all_edges_raw"]
    note_by_id = data["note_by_id"]
    live_ids = data["live_ids"]
    live_graph_ids = data["live_graph_ids"]
    edges = data["edges"]
    dangling = data["dangling"]

    degree, wiki_degree, adjacency = _link_degrees(edges)

    nodes: list[dict[str, Any]] = []
    for nid in sorted(live_graph_ids):
        note = note_by_id.get(
            nid,
            {"title": nid, "type": "", "classification": "MNPI", "zone": "unknown"},
        )
        nodes.append({
            "id": nid,
            "type": note["type"],
            "classification": note["classification"],
            "zone": note["zone"],
            "degree": degree.get(nid, 0),
            "wiki_degree": wiki_degree.get(nid, 0),
            "orphan": wiki_degree.get(nid, 0) == 0,
            "truly_isolated": degree.get(nid, 0) == 0,
            "dup_suspect": False,
            "mismatch_flag": False,
            "title": note["title"],
        })

    orphans = sorted(nid for nid in live_graph_ids if wiki_degree.get(nid, 0) == 0)
    truly_isolated_ids = sorted(nid for nid in live_graph_ids if degree.get(nid, 0) == 0)
    knowledge_layer_isolated_ids = sorted(
        nid for nid in truly_isolated_ids
        if note_by_id.get(nid, {}).get("zone") == "brain"
        and note_by_id.get(nid, {}).get("type") not in ("source", "source-derived", "draft")
    )

    whole_components = _connected_components_list(live_graph_ids, adjacency)
    whole_components.sort(key=len, reverse=True)
    component_sizes_top5 = [len(component) for component in whole_components[:5]]

    brain_ids = {
        nid for nid in live_graph_ids if note_by_id.get(nid, {}).get("raw_zone") == "brain"
    }
    brain_adjacency = {
        nid: {target for target in adjacency.get(nid, ()) if target in brain_ids}
        for nid in brain_ids
    }
    brain_components = _connected_components_list(brain_ids, brain_adjacency)
    brain_components.sort(key=len, reverse=True)
    brain_component_sizes_top5 = [len(component) for component in brain_components[:5]]

    top_hubs = sorted(
        (
            {
                "id": nid,
                "degree": degree.get(nid, 0),
                "type": note_by_id.get(nid, {}).get("type", ""),
                "classification": note_by_id.get(nid, {}).get("classification", "MNPI"),
            }
            for nid in live_graph_ids
        ),
        key=lambda hub: -hub["degree"],
    )[:_TOP_HUBS_LIMIT]

    zone_counts: dict[str, int] = defaultdict(int)
    for nid in live_graph_ids:
        zone_counts[note_by_id.get(nid, {}).get("zone", "unknown")] += 1

    return {
        "all_nodes_raw": all_nodes_raw,
        "all_edges_raw": all_edges_raw,
        "note_by_id": note_by_id,
        "live_ids": live_ids,
        "live_graph_ids": live_graph_ids,
        "edges": edges,
        "dangling": dangling,
        "nodes": nodes,
        "orphans": orphans,
        "truly_isolated_ids": truly_isolated_ids,
        "knowledge_layer_isolated_ids": knowledge_layer_isolated_ids,
        "whole_components": whole_components,
        "component_sizes_top5": component_sizes_top5,
        "brain_ids": brain_ids,
        "brain_components": brain_components,
        "brain_component_sizes_top5": brain_component_sizes_top5,
        "top_hubs": top_hubs,
        "zone_counts": zone_counts,
    }
