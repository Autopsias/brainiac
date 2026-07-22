"""``brain graph-report`` — a static, self-contained HTML "vault explorer"
(WebGL link graph + 3D semantic map) rendered from the graphify discovery
build (``.brain/graph/graph.json``, ``authoritative: false`` — GRF-01) plus
the live index. HOST-broker only (same posture as ``brain.healthreport``):
reads the writable index, writes a file, never mutates the index itself.

Ported from a proven ad-hoc extraction script (session 2026-07-20/21) into an
engine module so the report regenerates itself whenever the underlying data
changes — see the wiring in ``BrainCore.graphify`` (success path) and
``BrainCore.maintain``'s ``graph_hygiene`` branch — rather than needing a
one-off script re-run by hand.

Two-half split (mirrors ``healthreport``/``brief``): ``build_payload`` does
all the I/O + computation (read-only) and returns a plain dict; ``render_html``
is a pure string-splice (no I/O) that drops the payload JSON into the packaged
HTML template. ``generate_graph_report`` is the one entry point that does both
+ writes the file, called by ``BrainCore.graph_report``.

Design choices carried over from the prototype (see its own docstring for the
full "why"):
  * Only LIVE notes (``is_latest_version`` not explicitly ``"false"``) count
    toward nodes/points/duplicate-mismatch detection — post-dedup reality.
  * PCA is plain numpy (mean-pool chunk vectors per note -> L2-normalize ->
    SVD on the mean-centered matrix). No sklearn.
  * Near-dup/mismatch detection reuses vectors ALREADY in the index (no
    re-embedding pass) via ``core.index.backend.get_vectors`` — backend
    agnostic (works against sqlite-vec OR the brute-force fallback).
  * If the vector table is empty/unavailable (no embeddings at all), this
    still renders the LINK view — semantic fields are just empty and
    ``payload["semantic_note"]`` carries a visible caption (spliced into the
    template's freshness banner) instead of crashing.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

TEMPLATE_PLACEHOLDER = "__BRAIN_GRAPHREPORT_PAYLOAD_JSON__"
NEAR_DUP_THRESHOLD = 0.97
MISMATCH_LOW = 0.90
_TOP_HUBS_LIMIT = 20
_DUP_DISPLAY_LIMIT = 50
_MISMATCH_DISPLAY_LIMIT = 50
_NEIGHBORS_K = 8


def _load_template() -> str:
    """The packaged HTML shell (everything outside the payload JSON block).
    Committed at repo root as ``assets/graph-explorer-template.html`` and
    mirrored into ``src/brain/_assets/assets/`` by
    ``tools/package_clients.py`` (PYP-02 pattern) — so this resolves the same
    way whether running from a checkout or an installed wheel."""
    from importlib.resources import files

    candidate = files("brain") / "_assets" / "assets" / "graph-explorer-template.html"
    try:
        return candidate.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        pass
    # Editable-checkout fallback (mirrors brain.init.discover_repo_root order):
    # importlib.resources on some zipimport-backed installs can't read a real
    # path; fall back to the repo-root original directly.
    from . import init as brain_init

    root = brain_init.discover_repo_root()
    if root is not None:
        p = root / "assets" / "graph-explorer-template.html"
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "graph-explorer-template.html not found in the packaged assets or repo checkout"
    )


def render_html(payload: dict[str, Any]) -> str:
    """Pure string splice — no I/O. Drops ``payload`` (as compact JSON) into
    the packaged template in place of the placeholder token."""
    template = _load_template()
    if TEMPLATE_PLACEHOLDER not in template:
        raise ValueError("graph-explorer template is missing its payload placeholder")
    payload_json = json.dumps(payload, ensure_ascii=True)
    # Note bodies are untrusted: a "</script>" inside the payload would end the
    # template's script block. Unicode-escape the HTML-sensitive characters —
    # JSON.parse decodes them back, so runtime data is unchanged.
    payload_json = (
        payload_json.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return template.replace(TEMPLATE_PLACEHOLDER, payload_json, 1)


# ---------------------------------------------------------------------------
# pure helpers (unpacking, components, union-find) — same shape as the
# prototype script, kept private and dependency-free.
# ---------------------------------------------------------------------------
def _para_zone(zone: str, path: str) -> str:
    if zone != "brain":
        return zone
    for sub in ("projects", "areas", "resources", "archive"):
        if f"/brain/{sub}/" in path:
            return sub
    return "brain-root"


def _connected_components_list(nodes: set, adj: dict) -> list[list[str]]:
    seen: set = set()
    comps: list[list[str]] = []
    for start in nodes:
        if start in seen:
            continue
        comp = [start]
        seen.add(start)
        stack = [start]
        while stack:
            cur = stack.pop()
            for nb in adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    comp.append(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def _union_find_clusters(n: int, pairs: list[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _status_band(current: float, excellent: float, acceptable: float, direction: str) -> str:
    """Classify ``current`` against a sourced excellent/acceptable BAND (not a
    single point target) — see the benchmark table in the module docstring
    of ``graph-report`` research (2026-07-21 recalibration). ``direction``
    is ``higher_pct``/``higher_count`` (more is better) or
    ``lower_pct``/``lower_count`` (less is better)."""
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


# External benchmark sources backing the recalibrated bands (2026-07-21
# research pass — see PR description / commit for the full citation table).
# Each carries an honest confidence label; where evidence is thin ("thin
# evidence" / "analogical, low confidence") that is stated, not hidden.
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


# ---------------------------------------------------------------------------
# payload build
# ---------------------------------------------------------------------------
def build_payload(core: Any, *, today: datetime.date | None = None) -> dict[str, Any]:
    """Build the full graph-report payload from ``graph.json`` (if a graphify
    build has ever published one) + the live index attached to ``core``.
    Read-only; never raises on missing/empty data — degrades gracefully
    (empty graph, or ``semantic_note`` set when no embeddings exist)."""
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
    all_nodes_raw = graph.get("nodes") or []
    all_edges_raw = graph.get("edges") or []

    notes_rows = conn.execute(
        "SELECT id, title, type, classification, zone, path, is_latest_version FROM notes"
    ).fetchall()
    note_by_id: dict[str, dict[str, Any]] = {}
    for nid, title, ntype, cls, zone, path, is_latest in notes_rows:
        note_by_id[nid] = {
            "title": title or nid,
            "type": ntype or "",
            "classification": cls or "MNPI",
            "zone": _para_zone(zone or "", path or ""),
            "raw_zone": zone or "",
            "path": path or "",
            "is_latest": str(is_latest or "").strip().lower() != "false",
        }
    live_ids = {nid for nid, n in note_by_id.items() if n["is_latest"]}

    # -- 1. link graph, filtered to live notes -----------------------------
    all_ids_in_graph = {n["id"] for n in all_nodes_raw}
    live_graph_ids = {nid for nid in all_ids_in_graph if nid in live_ids}

    edges = [
        e for e in all_edges_raw
        if e["from"] in live_graph_ids and e["to"] in live_graph_ids and e["from"] != e["to"]
    ]
    dangling = [
        e for e in all_edges_raw
        if e["from"] not in note_by_id or e["to"] not in note_by_id
    ]

    degree: dict[str, int] = defaultdict(int)
    wiki_degree: dict[str, int] = defaultdict(int)
    adj_all: dict[str, set] = defaultdict(set)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
        adj_all[e["from"]].add(e["to"])
        adj_all[e["to"]].add(e["from"])
        if e.get("kind") == "WIKILINK":
            wiki_degree[e["from"]] += 1
            wiki_degree[e["to"]] += 1

    dup_ids: set = set()
    mismatch_ids: set = set()

    nodes = []
    for nid in sorted(live_graph_ids):
        n = note_by_id.get(nid, {"title": nid, "type": "", "classification": "MNPI", "zone": "unknown"})
        nodes.append({
            "id": nid, "type": n["type"], "classification": n["classification"], "zone": n["zone"],
            "degree": degree.get(nid, 0), "wiki_degree": wiki_degree.get(nid, 0),
            "orphan": wiki_degree.get(nid, 0) == 0, "truly_isolated": degree.get(nid, 0) == 0,
            "dup_suspect": False, "mismatch_flag": False, "title": n["title"],
        })

    orphans_list = sorted(nid for nid in live_graph_ids if wiki_degree.get(nid, 0) == 0)
    truly_isolated_ids = sorted(nid for nid in live_graph_ids if degree.get(nid, 0) == 0)
    # Scope the TARGET to the layer the linking convention actually governs
    # (AGENTS.md §3 binds brain/, not raw/): a raw/ source, a review-gate
    # working file, or a deliberate test fixture has no linking obligation, so
    # counting it kept the bar permanently red over unfixable-by-design nodes
    # (field lesson 2026-07-21: 45 of 45 "isolated" residue after honest
    # curation were exactly those). Whole-vault count stays in the payload as
    # `truly_isolated_ids` for the diagnostics section.
    kl_isolated_ids = sorted(
        nid for nid in truly_isolated_ids
        if note_by_id.get(nid, {}).get("zone") == "brain"
        and note_by_id.get(nid, {}).get("type") not in ("source", "source-derived", "draft")
    )

    whole_components = _connected_components_list(live_graph_ids, adj_all)
    whole_components.sort(key=len, reverse=True)
    component_sizes_top5 = [len(c) for c in whole_components[:5]]

    brain_ids = {nid for nid in live_graph_ids if note_by_id.get(nid, {}).get("raw_zone") == "brain"}
    brain_adj = {nid: {t for t in adj_all.get(nid, ()) if t in brain_ids} for nid in brain_ids}
    brain_components = _connected_components_list(brain_ids, brain_adj)
    brain_components.sort(key=len, reverse=True)
    brain_component_sizes_top5 = [len(c) for c in brain_components[:5]]

    top_hubs = sorted(
        ({"id": nid, "degree": degree.get(nid, 0), "type": note_by_id.get(nid, {}).get("type", ""),
          "classification": note_by_id.get(nid, {}).get("classification", "MNPI")}
         for nid in live_graph_ids),
        key=lambda h: -h["degree"],
    )[:_TOP_HUBS_LIMIT]

    zone_counts: dict[str, int] = defaultdict(int)
    for nid in live_graph_ids:
        zone_counts[note_by_id.get(nid, {}).get("zone", "unknown")] += 1

    # -- 2. GRH-01 knowledge-layer hygiene, via the engine's own canonical
    #    implementation, so this matches `brain health-report` exactly. -----
    hygiene = graph_mod.graph_hygiene_metrics(conn)
    knowledge_note_count = hygiene["knowledge_note_count"]
    hygiene_orphan_count = hygiene["orphan_count"]
    hygiene_island_count = hygiene["island_count"]

    # -- 3. semantic points: mean-pooled, L2-normalized note embeddings -----
    #    (live notes only) -> PCA (numpy only). Backend-agnostic: uses
    #    core.index.backend.get_vectors, not a backend-specific table name,
    #    so this works against sqlite-vec OR the brute-force fallback.
    chunk_rows = conn.execute("SELECT rowid, note_rowid FROM chunks").fetchall()
    chunk_rowid_by_note: dict[int, list[int]] = defaultdict(list)
    for crid, note_rowid in chunk_rows:
        chunk_rowid_by_note[note_rowid].append(crid)

    id_by_rowid = {r[0]: r[1] for r in conn.execute("SELECT rowid, id FROM notes").fetchall()}
    all_chunk_rowids = [crid for crids in chunk_rowid_by_note.values() for crid in crids]
    vecs_by_chunk = core.index.backend.get_vectors(conn, all_chunk_rowids) if all_chunk_rowids else {}

    point_ids: list[str] = []
    mean_vecs: list[np.ndarray] = []
    for note_rowid, crids in chunk_rowid_by_note.items():
        nid = id_by_rowid.get(note_rowid)
        if nid is None or nid not in live_ids:
            continue
        raw_vecs = [np.asarray(vecs_by_chunk[c], dtype=np.float64) for c in crids if c in vecs_by_chunk]
        if not raw_vecs:
            continue
        mv = np.mean(np.vstack(raw_vecs), axis=0)
        norm = np.linalg.norm(mv)
        if norm == 0:
            continue
        point_ids.append(nid)
        mean_vecs.append(mv / norm)

    n_live_pts = len(mean_vecs)
    semantic_note: str | None = None
    points: list[dict[str, Any]] = []
    explained_variance = [0.0, 0.0, 0.0]
    duplicate_pairs: list[dict[str, Any]] = []
    mismatch_pairs: list[dict[str, Any]] = []
    neighbors: dict[str, list[dict[str, Any]]] = {}
    near_dup_pairs_idx: list[tuple[int, int]] = []
    near_dup_cluster_count = 0
    mismatch_pairs_raw_count = 0
    exact_dup_pair_count = 0
    exact_dup_ids: list[str] = []
    near_dup_member_ids: list[str] = []

    if n_live_pts < 2:
        semantic_note = (
            "semantic layer unavailable — fewer than 2 live notes carry an embedding "
            "(empty/unavailable vector table); showing the link view only."
        )
    else:
        X = np.vstack(mean_vecs)
        Xc = X - X.mean(axis=0, keepdims=True)
        U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
        total_var = float(np.sum(S ** 2))
        k = min(3, Vt.shape[0])
        if total_var == 0:
            explained_variance = [0.0] * 3
            coords = np.zeros((n_live_pts, 3))
        else:
            ev = [float((S[i] ** 2) / total_var) for i in range(k)]
            explained_variance = ev + [0.0] * (3 - k)
            coords = np.zeros((n_live_pts, 3))
            coords[:, :k] = Xc @ Vt[:k].T

        if not np.all(np.isfinite(coords)) or not np.all(np.isfinite(explained_variance)):
            # Never ship NaN/Inf data — degrade to link-view-only instead of crashing.
            semantic_note = (
                "semantic layer unavailable — PCA produced non-finite values "
                "(degenerate embedding matrix); showing the link view only."
            )
            explained_variance = [0.0, 0.0, 0.0]
        else:
            for i, nid in enumerate(point_ids):
                n = note_by_id.get(nid, {})
                points.append({
                    "id": nid, "title": n.get("title", nid), "type": n.get("type", ""),
                    "classification": n.get("classification", "MNPI"), "zone": n.get("zone", "unknown"),
                    "x": round(float(coords[i, 0]), 6), "y": round(float(coords[i, 1]), 6),
                    "z": round(float(coords[i, 2]), 6), "in_graph": nid in live_graph_ids,
                })

            # -- 4. duplicate/mismatch detection over the same matrix -------
            sim = X @ X.T
            np.fill_diagonal(sim, -1.0)

            body_rows = conn.execute("SELECT id, body FROM notes").fetchall()
            body_by_id = {r[0]: (r[1] or "") for r in body_rows if r[0] in live_ids}
            hash_groups: dict[str, list[str]] = defaultdict(list)
            for nid, body in body_by_id.items():
                norm = re.sub(r"\s+", " ", body).strip()
                if not norm:
                    continue
                h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                hash_groups[h].append(nid)

            exact_pairs: list[tuple[str, str]] = []
            for ids in hash_groups.values():
                if len(ids) < 2:
                    continue
                ids_sorted = sorted(ids)
                for i in range(len(ids_sorted)):
                    for j in range(i + 1, len(ids_sorted)):
                        exact_pairs.append((ids_sorted[i], ids_sorted[j]))
            exact_dup_pair_count = len(exact_pairs)
            exact_dup_ids = sorted({nid for pair in exact_pairs for nid in pair})

            idx_pairs = np.argwhere(sim >= NEAR_DUP_THRESHOLD)
            idx_pairs = idx_pairs[idx_pairs[:, 0] < idx_pairs[:, 1]]
            near_dup_pairs_idx = [(int(a), int(b)) for a, b in idx_pairs]
            clusters = _union_find_clusters(n_live_pts, near_dup_pairs_idx)
            real_clusters = [c for c in clusters if len(c) >= 2]
            near_dup_cluster_count = len(real_clusters)
            near_dup_member_ids = sorted({point_ids[i] for c in real_clusters for i in c})

            display_pairs = []
            seen_pairs = set()
            for a, b in exact_pairs:
                key = (a, b) if a < b else (b, a)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                display_pairs.append({"cosine": 1.0, "a": a, "a_title": note_by_id.get(a, {}).get("title", a),
                                       "b": b, "b_title": note_by_id.get(b, {}).get("title", b)})
            scored_near = sorted(
                ((float(sim[a, b]), point_ids[a], point_ids[b]) for a, b in near_dup_pairs_idx),
                key=lambda t: -t[0],
            )
            for score, a, b in scored_near:
                key = (a, b) if a < b else (b, a)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                display_pairs.append({"cosine": round(score, 4), "a": a, "a_title": note_by_id.get(a, {}).get("title", a),
                                       "b": b, "b_title": note_by_id.get(b, {}).get("title", b)})
            display_pairs.sort(key=lambda p: -p["cosine"])
            duplicate_pairs = display_pairs[:_DUP_DISPLAY_LIMIT]
            dup_ids = {p["a"] for p in duplicate_pairs} | {p["b"] for p in duplicate_pairs}

            mismatch_idx = np.argwhere((sim >= MISMATCH_LOW) & (sim < NEAR_DUP_THRESHOLD))
            mismatch_idx = mismatch_idx[mismatch_idx[:, 0] < mismatch_idx[:, 1]]
            mismatch_scored = sorted(
                ((float(sim[a, b]), point_ids[a], point_ids[b]) for a, b in mismatch_idx),
                key=lambda t: -t[0],
            )[:_MISMATCH_DISPLAY_LIMIT]
            mismatch_pairs = [
                {"cosine": round(score, 4), "a": a, "a_title": note_by_id.get(a, {}).get("title", a),
                 "b": b, "b_title": note_by_id.get(b, {}).get("title", b)}
                for score, a, b in mismatch_scored
            ]
            mismatch_ids = {p["a"] for p in mismatch_pairs} | {p["b"] for p in mismatch_pairs}
            mismatch_pairs_raw_count = n_live_pts * (n_live_pts - 1) // 2

            for node in nodes:
                if node["id"] in dup_ids:
                    node["dup_suspect"] = True
                if node["id"] in mismatch_ids:
                    node["mismatch_flag"] = True

            # -- 5. neighbors: top-k cosine nearest neighbors ---------------
            k_n = min(_NEIGHBORS_K, n_live_pts - 1)
            if k_n > 0:
                top_idx = np.argpartition(-sim, kth=k_n - 1, axis=1)[:, :k_n]
                for i, nid in enumerate(point_ids):
                    row_idx = top_idx[i]
                    scored = sorted(((float(sim[i, j]), point_ids[j]) for j in row_idx), key=lambda t: -t[0])
                    neighbors[nid] = [{"id": pid, "cosine": round(c, 4)} for c, pid in scored if c > -0.5]

    # -- 6. targets block — BANDED against sourced external benchmarks, not
    #    "100% perfection" points (2026-07-21 recalibration). Two targets stay
    #    absolute (true_orphans, dangling_wikilinks) because they're structural
    #    correctness rules from AGENTS.md, not density judgment calls. ---------
    brain_link_n_total = knowledge_note_count
    brain_link_n_current = knowledge_note_count - hygiene_orphan_count
    brain_link_pct = round(100.0 * brain_link_n_current / brain_link_n_total, 1) if brain_link_n_total else 0.0
    islands_beyond_main = hygiene_island_count - 1 if hygiene_island_count else 0

    # giant-component share (brain/ zone, live notes) — the network-science
    # literature (WWW/Facebook giant-component share) treats this as a
    # stronger health signal than a raw island count: a vault can have 3
    # "islands" that are each tiny stragglers, or 3 islands where one holds
    # half the vault — same island count, very different health.
    main_component_size = brain_component_sizes_top5[0] if brain_component_sizes_top5 else 0
    giant_component_pct = (
        round(100.0 * main_component_size / len(brain_ids), 1) if brain_ids else 100.0
    )
    non_main_ids = sorted(
        nid for comp in brain_components[1:] for nid in comp
    ) if len(brain_components) > 1 else []

    dup_note_rate_pct = round(100.0 * len(exact_dup_ids) / n_live_pts, 2) if n_live_pts else 0.0

    targets = [
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
            "current": len(kl_isolated_ids), "unit": "count", "direction": "lower_count",
            "excellent": 0, "acceptable": 5,
            "status": _status_band(len(kl_isolated_ids), 0, 5, "lower_count"),
            "n_total": knowledge_note_count,
            "whole_vault_isolated": len(truly_isolated_ids),
            "source": _SRC_VAULT_CONVENTION,
            "offending_ids": kl_isolated_ids,
        },
        {
            "id": "brain_islands", "label": "Giant-component share (brain/ zone, live notes)",
            "current": giant_component_pct, "unit": "%", "direction": "higher_pct",
            "excellent": 90.0, "acceptable": 75.0,
            "status": _status_band(giant_component_pct, 90.0, 75.0, "higher_pct"),
            "n_total": hygiene_island_count, "islands_beyond_main": islands_beyond_main,
            "source": _SRC_GIANT_COMPONENT,
            "offending_ids": non_main_ids,
        },
        {
            "id": "exact_dup_pairs", "label": "Exact-duplicate note rate (sha256-identical bodies)",
            "current": dup_note_rate_pct, "unit": "%", "direction": "lower_pct",
            "excellent": 1.0, "acceptable": 5.0, "aspirational": 0.0,
            "status": _status_band(dup_note_rate_pct, 1.0, 5.0, "lower_pct"),
            "n_total": n_live_pts, "pair_count": exact_dup_pair_count,
            "source": _SRC_DUP_RATE,
            "offending_ids": exact_dup_ids,
        },
        {
            "id": "near_dup_clusters_per_1k",
            "label": "Near-duplicate clusters (seed-anchored, cosine≥0.97) per 1k notes",
            "current": round(near_dup_cluster_count * 1000.0 / n_live_pts, 1) if n_live_pts else 0.0,
            "unit": "per 1k", "direction": "lower_count", "status": "trend",
            "n_total": n_live_pts,
            "source": {
                "label": "heuristic, trend-only — union-find over every cosine≥0.97 pair "
                         f"({len(near_dup_pairs_idx)} raw pairs collapse to "
                         f"{near_dup_cluster_count} clusters). No citable external benchmark "
                         "found for this threshold/metric during the 2026-07-21 research pass "
                         "— stays trend-only rather than inventing a band.",
                "url": None, "confidence": "no external basis — engineering heuristic",
            },
            "offending_ids": near_dup_member_ids[:200],
        },
        {
            "id": "dangling_wikilinks", "label": "Dangling wikilink targets",
            "current": len(dangling), "unit": "count", "direction": "lower_count",
            "excellent": 0, "acceptable": 5,
            "status": _status_band(len(dangling), 0, 5, "lower_count"),
            "n_total": len(all_edges_raw),
            "source": _SRC_VAULT_CONVENTION,
            "offending_ids": [e["to"] if e["to"] not in note_by_id else e["from"] for e in dangling[:20]],
        },
    ]

    payload: dict[str, Any] = {
        "generated_at": now_iso,
        "engine_version": engine_version,
        "graph_generation": graph_generation,
        "graph_built_at": graph_built_at,
        "graph_authoritative": graph_authoritative,
        # meta reads (not `core.index.embedder`, which lazily CONSTRUCTS an
        # embedder on first access — a report render must never trigger a
        # model load/download just to label the payload).
        "embed_model": core.index.get_meta("embed_model") or "",
        "embed_dim": int(core.index.get_meta("embed_dim") or 0),
        "semantic_note": semantic_note,
        "counts": {
            "graph_nodes": len(nodes), "graph_edges": len(edges), "valid_edges": len(edges),
            "dangling_edges": len(dangling), "semantic_points": len(points),
            "notes_total_in_index": len(live_ids),
            "notes_missing_from_graph": len(live_ids - live_graph_ids),
            "orphans": len(orphans_list), "orphans_truly_isolated": len(truly_isolated_ids),
            "components": len(whole_components), "duplicate_pairs": len(duplicate_pairs),
            "duplicate_pairs_raw_count": len(near_dup_pairs_idx),
            "duplicate_clusters_seed_anchored": near_dup_cluster_count,
            "mismatch_pairs": len(mismatch_pairs), "mismatch_pairs_raw_count": mismatch_pairs_raw_count,
        },
        "explained_variance": explained_variance,
        "component_sizes_top5": component_sizes_top5,
        "brain_component_sizes_top5": brain_component_sizes_top5,
        "targets": targets,
        "top_hubs": top_hubs,
        "orphans": orphans_list,
        "dangling_edges_sample": [{"from": e["from"], "to": e["to"], "kind": e.get("kind")} for e in dangling[:20]],
        "zone_counts": dict(zone_counts),
        "nodes": nodes,
        "edges": [{"from": e["from"], "to": e["to"], "kind": e.get("kind")} for e in edges],
        "points": points,
        "duplicate_pairs": duplicate_pairs,
        "mismatch_pairs": mismatch_pairs,
        "neighbors": neighbors,
    }
    return payload


def generate_graph_report(core: Any, *, today: datetime.date | None = None) -> dict[str, Any]:
    """Build the payload, render it, and write ``.brain/graph/graph-explorer.html``.
    Returns ``{"path", "graph_generation", "nodes", "edges", "points"}``."""
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
