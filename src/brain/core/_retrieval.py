"""Unfiltered retrieval methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Hit,
    Path,
    config,
    frontmatter,
)


class _CoreRetrievalMixin:
    """Unfiltered retrieval methods for BrainCore."""

    def search(self, query: str, k: int = 10) -> list[Hit]:
        return self.index.search(query, k)
    def source_freshness(self, newest_hit_date: str, max_tier: str) -> dict[str, Any]:
        """RET-09 freshness signal: count + newest date of notes whose
        valid-time date is strictly newer than ``newest_hit_date``, at the
        caller's egress cap. See ``BrainIndex.freshness``."""
        return self.index.freshness(newest_hit_date, max_tier)
    def hybrid_search(
        self, query: str, k: int = 10, *, rerank: bool = False, rerank_top: int = 15,
        rrf_k: int = 60, rerank_gate: bool | None = None,
    ) -> list[Hit]:
        """Fused RRF(k) BM25 + dense retrieval (RET-01), optional skippable
        reranker (RET-02), RK-02 adaptive rerank gate. UNFILTERED — the CLI
        applies the egress gate."""
        return self.index.hybrid_search(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
            rerank_gate=rerank_gate,
        )
    def hybrid_search_with_trace(
        self, query: str, k: int = 10, *, rerank: bool = False,
        rerank_top: int = 15, rrf_k: int = 60, rerank_gate: bool | None = None,
    ):
        """Production hybrid search plus opt-in, pre-egress S03 attribution.

        Callers must still route hits through the CLI's egress gate before
        serialising either a full explanation or the compact capture digest.
        """
        return self.index.hybrid_search_with_trace(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
            rerank_gate=rerank_gate,
        )
    def diagnose_target(
        self, query: str, target_id: str, *, max_tier: str, trace: Any,
        final_rank: int | None,
    ) -> dict[str, Any]:
        """Run the S03 target probe after an unchanged production search."""
        return self.index.diagnose_target(
            query, target_id, max_tier=max_tier, trace=trace, final_rank=final_rank,
        )
    def annotate_create_safety(
        self, query: str, surfaced: list[dict[str, Any]], max_tier: str
    ) -> set[str]:
        """Finalize ADR-0008 create safety after the CLI egress decision.

        The engine can identify a full alias/title owner, but only the egress
        boundary knows whether every owner is visible at the caller's cap.
        """
        return self.index.annotate_create_safety(query, surfaced, max_tier)
    def hybrid_search_graph(
        self, query: str, k: int = 10, *, rerank: bool = False, rerank_top: int = 15,
        rrf_k: int = 60, depth: int = 2, graph_weight: float = 0.5,
        seed_flat_top: int = 3, flat_pool: int = 30, return_trace: bool = False,
    ):
        """Gated graph-augmented multi-hop retrieval (RET-06).

        Single-hop queries pass through to ``hybrid_search`` UNCHANGED (the gate
        does not fire); multi-hop-shaped queries (>= 2 named non-hub entities)
        get a wikilink-graph expansion fused into the flat ranking. DISCOVERY-
        ONLY (RET-03): the graph never overrides an authoritative flat hit. See
        ``brain.multihop``. UNFILTERED — the CLI applies the egress gate."""
        return self.index.hybrid_search_graph(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
            depth=depth, graph_weight=graph_weight, seed_flat_top=seed_flat_top,
            flat_pool=flat_pool, return_trace=return_trace,
        )
    def grep(self, pattern: str, *, k: int = 20, regex: bool = False) -> list[dict[str, Any]]:
        """Lexical-first scan over note bodies — no embedding (RET-04)."""
        return self.index.grep(pattern, k=k, regex=regex)
    def bases_query(
        self, filters: dict[str, str] | None = None, *, k: int = 50,
        latest_only: bool = False, as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Structured frontmatter view over indexed columns — no embedding (RET-04).
        TMP-02: ``latest_only``/``as_of`` are temporal views (Latest Only / As Of)."""
        return self.index.bases_query(filters, k=k, latest_only=latest_only, as_of=as_of)
    def dossier(self, query: str, k: int = 12) -> dict[str, Any]:
        """RET-10: the ONE-CALL retrieval sweep — what a careful agent
        orchestrates by hand (decision layer + corroborating sources +
        contradiction check + version noise handling), composed engine-side
        so even a minimal-path harness gets the full sweep deterministically.

        Motivation (2026-07-11 benchmark series close): on the same
        substrate, the remaining quality gap between harnesses was
        ORCHESTRATION BREADTH — one agent cross-checked newer sources
        against the decision layer and caught superseded thinking; the
        other walked the minimal path and could not see contradictions off
        it. This verb makes the sweep the minimal path.

        Returns (UNFILTERED — callers apply the egress gate):
        - ``decisions``: hits with ``type: decision`` (the authority
          layer), each carrying a ``tensions`` list — NEWER-dated,
          non-decision hits from the same sweep (a proposal/deck that
          post-dates the recorded decision: report the tension, never
          promote the proposal).
        - ``sources``: the remaining live hits (material under
          consideration).
        - ``retired_excluded``: hits dropped because a supersession chain
          retired them (``is_latest_version: false``) — version noise the
          sweep already handled.
        """
        # A DEEP candidate pool: decision notes are scarce and often rank
        # below big source documents on broad queries — the decision layer
        # must never come back empty just because the top-k was crowded
        # (measured on the live corpus: decisions at rank ~30 on a broad
        # decision-state query). Scanning deeper is one indexed query.
        pool = [h.to_dict() for h in self.hybrid_search(query, k=max(k * 2, 60))]
        live = [h for h in pool if h.get("is_latest_version") != "false"]
        retired_excluded = len(pool) - len(live)
        decisions = [h for h in live if h.get("type") == "decision"]
        # RET-10b: MERGE a targeted BM25 probe over the decision layer — the
        # decision layer must never come back empty just because a phrasing
        # shift pushed decision notes below the semantic pool (measured live:
        # a rewording emptied the layer while the notes plainly existed).
        seen_ids = {d["id"] for d in decisions}
        for h in self.index.decision_layer_hits(query, k=max(5, k // 2)):
            hd = h.to_dict()
            if hd["id"] not in seen_ids:
                decisions.append(hd)
                seen_ids.add(hd["id"])
        decisions = decisions[:max(5, k // 2)]
        sources = [h for h in live if h.get("type") != "decision"][:k]
        # Identity-confidence on tension candidates (engine-feedback 2026-07-19):
        # a calendar-asserted transcript whose title the audio doesn't support
        # can post-date a decision and surface as a tension purely on metadata.
        # Carry the source's `identity:` stamp so the caller can discount a
        # title/calendar-derived tension vs a content-verified one. Lazy
        # frontmatter read of the few tension candidates only — no index column.
        _identity_cache: dict[str, str] = {}

        def _identity(s: dict[str, Any]) -> str:
            p = s.get("path", "")
            if p not in _identity_cache:
                try:
                    meta, _ = frontmatter.parse_text(
                        Path(p).read_text(encoding="utf-8", errors="replace"))
                    _identity_cache[p] = str(meta.get("identity", "") or "")
                except OSError:
                    _identity_cache[p] = ""
            return _identity_cache[p]

        for d in decisions:
            d_date = d.get("date") or ""
            d["tensions"] = [
                {"id": s["id"], "date": s.get("date", ""), "type": s.get("type", ""),
                 "identity": _identity(s)}
                for s in sources
                if d_date and s.get("date") and s["date"] > d_date
            ]
        return {
            "query": query,
            "decisions": decisions,
            "sources": sources,
            "retired_excluded": retired_excluded,
        }
    def graph_expand(
        self, seeds: list[str], *, depth: int = 2, k: int = 10, use_ppr: bool = True,
        use_inferred: bool = False,
    ) -> dict[str, Any]:
        """On-demand wikilink-BFS + PPR — DISCOVERY-ONLY (RET-03).

        ``use_inferred`` (GRF-01, ADR-0003 Ruling 6, "Optional"): fold the
        published graphify build's INFERRED edges in as extra traversal
        input. HOST-ONLY read of the graphify artifact — on the VM leg this
        is silently ignored (degrades to the plain wikilink graph) rather
        than reaching for a host-only runtime artifact through the shared
        mount, mirroring the session-memory host-only-by-contract posture
        (ADR-0003 Ruling 4)."""
        extra_edges = None
        if use_inferred and self.role == config.ROLE_HOST:
            from .. import graphify as gmod

            extra_edges = gmod.read_published_inferred_edges(
                config.graph_json_path(self.vault))
        return self.index.graph_expand(
            seeds, depth=depth, k=k, use_ppr=use_ppr, extra_edges=extra_edges)
    def get(self, note_id: str) -> dict[str, Any] | None:
        return self.index.get(note_id)
    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.index.recent(limit)
