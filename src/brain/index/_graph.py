"""Index graph retrieval methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _GraphMixin:
    """Index graph retrieval methods."""

    def _link_graph_cached(self) -> Any:
        from ..graph import build_graph
        if self._link_graph is None:
            self._link_graph = build_graph(self.conn)
        return self._link_graph

    def _entity_lexicon_cached(self) -> Any:
        from ..multihop import EntityLexicon
        if self._entity_lex is None:
            self._entity_lex = EntityLexicon.build(self.conn)
        return self._entity_lex

    def hybrid_search_graph(
        self,
        query: str,
        k: int = 10,
        *,
        rerank: bool = False,
        rerank_top: int = 15,
        rrf_k: int = 60,
        depth: int = 2,
        graph_weight: float = 0.5,
        seed_flat_top: int = 3,
        flat_pool: int = 30,
        return_trace: bool = False,
    ) -> list[Hit] | tuple[list[Hit], dict]:
        """Gated graph-augmented multi-hop retrieval (RET-06).

        For a SINGLE-HOP query (the gate does not fire) this returns EXACTLY
        ``hybrid_search(query, k, rerank=...)`` — same call, same result — so
        single-hop latency and quality can never regress. For a multi-hop-shaped
        query (>= 2 named non-hub entities) it fetches a wider flat pool, expands
        the wikilink graph from the named entities + top flat hits, and fuses the
        graph candidates into the flat ranking (flat-dominant weighted RRF).

        DISCOVERY-ONLY (RET-03): the graph only nominates candidate note ids that
        flat retrieval could reach; it never fabricates a note and never
        overrides an authoritative flat hit. See ``brain.multihop``."""
        from ..multihop import graph_augmented_ranking

        lexicon = self._entity_lexicon_cached()
        mentions = lexicon.mentions(query)
        from ..multihop import is_multihop_shaped

        if not is_multihop_shaped(mentions):
            # PASSTHROUGH — byte-identical to flat. The graph is never built.
            hits = self.hybrid_search(
                query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k
            )
            return (hits, {"fired": False, "entities": [m.surface for m in mentions]}) \
                if return_trace else hits

        # Multi-hop path: wider flat pool so tail relevant notes exist to promote.
        pool = self.hybrid_search(
            query, k=max(k, flat_pool), rerank=rerank, rerank_top=rerank_top,
            rrf_k=rrf_k,
        )
        pool_by_id = {h.id: h for h in pool}
        flat_ids = [h.id for h in pool]
        graph = self._link_graph_cached()
        fired, ranked_ids, trace = graph_augmented_ranking(
            query, flat_ids, lexicon, graph,
            depth=depth, graph_weight=graph_weight, rrf_k=rrf_k,
            seed_flat_top=seed_flat_top,
        )
        # Assemble Hit objects in fused order. Notes flat already retrieved reuse
        # their Hit (title/snippet); graph-only notes are hydrated from the notes
        # table and tagged source="graph" so a caller sees the discovery
        # provenance. CRITICAL: re-stamp a strictly-DESCENDING score encoding the
        # fused RANK so the fused order survives any ``{path: score}`` round-trip
        # (e.g. the eval harness re-sorts a run by score) — mirrors the
        # post-fusion re-stamp in ``BrainCore.search_multi`` (RET-05b).
        from dataclasses import replace

        top = ranked_ids[:k]
        n = len(top)
        out: list[Hit] = []
        for i, nid in enumerate(top):
            h = pool_by_id.get(nid)
            if h is None:
                h = self._graph_hit(nid)
                if h is None:
                    continue
            out.append(replace(h, score=float(n - i)))
        return (out, trace) if return_trace else out

    def _graph_hit(self, note_id: str) -> Hit | None:
        """Build a Hit for a graph-ONLY candidate (flat never retrieved it).
        Tagged source="graph" for discovery provenance; the score is re-stamped
        by the caller to encode fused rank."""
        row = self._note_row(self._rowid_of(note_id))
        if not row:
            return None
        return Hit(
            id=row["id"], title=row["title"],
            classification=row["classification"], zone=row["zone"],
            path=row["path"], score=0.0, source="graph",
            snippet=self._snippet(row["body"]),
            is_latest_version=row.get("is_latest_version", ""),
            type=row.get("type", ""),
        )

    def _apply_rerank(
        self, query: str, hits: list[Hit], reranker: Any | None, rerank_top: int
    ) -> list[Hit]:
        """Compatibility wrapper for the normal, trace-free rerank path."""
        return self._rerank_impl(query, hits, reranker, rerank_top, collect_scores=False)

    def _apply_rerank_with_scores(
        self, query: str, hits: list[Hit], reranker: Any | None, rerank_top: int
    ) -> tuple[list[Hit], dict[str, tuple[float, int]], bool]:
        result = self._rerank_impl(query, hits, reranker, rerank_top, collect_scores=True)
        reordered, scores, applied = result
        return reordered, scores or {}, applied

    def _rerank_impl(
        self, query: str, hits: list[Hit], reranker: Any | None, rerank_top: int,
        *, collect_scores: bool,
    ) -> list[Hit] | tuple[list[Hit], dict[str, tuple[float, int]], bool]:
        """Apply the bounded, skippable reranker implementation."""
        from ..index_stages.reranking import rerank_hits

        return rerank_hits(
            self,
            query,
            hits,
            reranker,
            rerank_top,
            collect_scores=collect_scores,
        )

    def _rowid_of(self, note_id: str) -> int:
        r = self.conn.execute("SELECT rowid FROM notes WHERE id=?", (note_id,)).fetchone()
        return int(r[0]) if r else -1

