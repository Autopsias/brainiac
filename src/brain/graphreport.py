"""Render the host-only static graph explorer report.

The payload builder keeps the public ``brain.graphreport`` facade while its
link, semantic, and target sections live in dedicated modules. The template
splice remains pure and ``generate_graph_report`` remains the file-writing
entry point used by ``BrainCore.graph_report``.
"""
from __future__ import annotations

import datetime
import json
from typing import Any

from .graphreport_sections import (
    _connected_components_list,
    _MISMATCH_LOW,
    _NEAR_DUP_THRESHOLD,
    _para_zone,
    _status_band,
    _union_find_clusters,
    build_link_section,
)
from .graphreport_sections_2 import build_semantic_section, build_targets_section

__all__ = [
    "TEMPLATE_PLACEHOLDER", "NEAR_DUP_THRESHOLD", "MISMATCH_LOW",
    "build_payload", "render_html", "generate_graph_report",
    "_connected_components_list", "_MISMATCH_LOW", "_NEAR_DUP_THRESHOLD",
    "_para_zone", "_status_band", "_union_find_clusters",
    "build_link_section", "build_semantic_section", "build_targets_section",
]

TEMPLATE_PLACEHOLDER = "__BRAIN_GRAPHREPORT_PAYLOAD_JSON__"
NEAR_DUP_THRESHOLD = _NEAR_DUP_THRESHOLD
MISMATCH_LOW = _MISMATCH_LOW


def _load_template() -> str:
    """Load the packaged graph-explorer HTML shell."""
    from importlib.resources import files

    candidate = files("brain") / "_assets" / "assets" / "graph-explorer-template.html"
    try:
        return candidate.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        pass
    from . import init as brain_init

    root = brain_init.discover_repo_root()
    if root is not None:
        path = root / "assets" / "graph-explorer-template.html"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "graph-explorer-template.html not found in the packaged assets or repo checkout"
    )


def render_html(payload: dict[str, Any]) -> str:
    """Purely splice a payload into the packaged graph-explorer template."""
    template = _load_template()
    if TEMPLATE_PLACEHOLDER not in template:
        raise ValueError("graph-explorer template is missing its payload placeholder")
    payload_json = json.dumps(payload, ensure_ascii=True)
    payload_json = (
        payload_json.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return template.replace(TEMPLATE_PLACEHOLDER, payload_json, 1)


def build_payload(core: Any, *, today: datetime.date | None = None) -> dict[str, Any]:
    """Build the graph payload while preserving the established key order."""
    from . import __version__ as engine_version
    from . import config
    from . import graph as graph_mod

    conn = core.index.conn
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    graph_path = config.graph_json_path(core.vault)
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        graph = {}
    graph_generation = graph.get("generation")
    graph_built_at = graph.get("built_at")
    graph_authoritative = graph.get("authoritative", False)

    link = build_link_section(conn, graph)
    hygiene = graph_mod.graph_hygiene_metrics(conn)
    semantic = build_semantic_section(
        core,
        conn=conn,
        note_by_id=link["note_by_id"],
        live_ids=link["live_ids"],
        live_graph_ids=link["live_graph_ids"],
        nodes=link["nodes"],
    )
    targets = build_targets_section(link, hygiene, semantic)

    payload: dict[str, Any] = {
        "generated_at": now_iso,
        "engine_version": engine_version,
        "graph_generation": graph_generation,
        "graph_built_at": graph_built_at,
        "graph_authoritative": graph_authoritative,
        "embed_model": core.index.get_meta("embed_model") or "",
        "embed_dim": int(core.index.get_meta("embed_dim") or 0),
        "semantic_note": semantic["semantic_note"],
        "counts": {
            "graph_nodes": len(link["nodes"]),
            "graph_edges": len(link["edges"]),
            "valid_edges": len(link["edges"]),
            "dangling_edges": len(link["dangling"]),
            "semantic_points": len(semantic["points"]),
            "notes_total_in_index": len(link["live_ids"]),
            "notes_missing_from_graph": len(link["live_ids"] - link["live_graph_ids"]),
            "orphans": len(link["orphans"]),
            "orphans_truly_isolated": len(link["truly_isolated_ids"]),
            "components": len(link["whole_components"]),
            "duplicate_pairs": len(semantic["duplicate_pairs"]),
            "duplicate_pairs_raw_count": len(semantic["near_dup_pairs_idx"]),
            "duplicate_clusters_seed_anchored": semantic["near_dup_cluster_count"],
            "mismatch_pairs": len(semantic["mismatch_pairs"]),
            "mismatch_pairs_raw_count": semantic["mismatch_pairs_raw_count"],
        },
        "explained_variance": semantic["explained_variance"],
        "component_sizes_top5": link["component_sizes_top5"],
        "brain_component_sizes_top5": link["brain_component_sizes_top5"],
        "targets": targets,
        "top_hubs": link["top_hubs"],
        "orphans": link["orphans"],
        "dangling_edges_sample": [
            {"from": edge["from"], "to": edge["to"], "kind": edge.get("kind")}
            for edge in link["dangling"][:20]
        ],
        "zone_counts": dict(link["zone_counts"]),
        "nodes": link["nodes"],
        "edges": [
            {"from": edge["from"], "to": edge["to"], "kind": edge.get("kind")}
            for edge in link["edges"]
        ],
        "points": semantic["points"],
        "duplicate_pairs": semantic["duplicate_pairs"],
        "mismatch_pairs": semantic["mismatch_pairs"],
        "neighbors": semantic["neighbors"],
    }
    return payload


def generate_graph_report(
    core: Any, *, today: datetime.date | None = None,
) -> dict[str, Any]:
    """Build, render, and write ``.brain/graph/graph-explorer.html``."""
    from . import config

    payload = build_payload(core, today=today)
    html_text = render_html(payload)
    out_dir = config.graph_dir(core.vault)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "graph-explorer.html"
    path.write_text(html_text, encoding="utf-8")
    return {
        "path": str(path),
        "graph_generation": payload["graph_generation"],
        "nodes": len(payload["nodes"]),
        "edges": len(payload["edges"]),
        "points": len(payload["points"]),
    }
