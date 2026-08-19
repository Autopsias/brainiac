"""Index ranking retrieval methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SearchMixin:
    """Index ranking retrieval methods."""

    def _lexical_ranked(self, query: str, n: int) -> list[int]:
        """FTS5 BM25 ranked note rowids, best-first. (`rank` is BM25; lower is
        better, so ``ORDER BY rank`` is best-first.)"""
        c = self.conn
        try:
            toks = [t for t in query.replace('"', " ").split() if t]
            fts_q = " OR ".join(f'"{t}"' for t in toks) if toks else '""'
            return [
                int(rowid)
                for (rowid,) in c.execute(
                    "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (fts_q, n),
                )
            ]
        except sqlite3.OperationalError:
            return []

    def decision_layer_hits(self, query: str, k: int = 8) -> list["Hit"]:
        """RET-10b: TARGETED lexical probe over the DECISION LAYER — live
        ``type: decision`` notes BM25-ranked against ``query``. Exists so a
        dossier's decision layer never depends on decision notes cracking
        the general semantic top-k (measured: a phrasing shift pushed them
        below rank 60 and the sweep's decision layer came back empty while
        the notes plainly existed). Decisions are scarce, FTS is indexed —
        this probe is one cheap query."""
        c = self.conn
        try:
            toks = [t for t in query.replace('"', " ").split() if t]
            fts_q = " OR ".join(f'"{t}"' for t in toks) if toks else '""'
            rowids = [
                int(r) for (r,) in c.execute(
                    "SELECT f.rowid FROM notes_fts f JOIN notes n ON n.rowid = f.rowid "
                    "WHERE f.notes_fts MATCH ? AND n.type = 'decision' "
                    "AND COALESCE(n.is_latest_version,'') != 'false' "
                    "ORDER BY f.rank LIMIT ?", (fts_q, k))
            ]
        except sqlite3.OperationalError:
            return []
        date_expr = ("COALESCE(NULLIF(effective_date,''), "
                     "NULLIF(document_date,''), created)")
        # Dossier's targeted decision probe is still a retrieval surface. It
        # must carry the same additive identity explanation as hybrid hits,
        # even when it was added outside the broad hybrid pool.
        exact = self._exact_leg(query, RRF_K_EXACT)
        owner_count = len(exact.owner_rowids)
        hits: list[Hit] = []
        for rid in rowids:
            row = self._note_row(rid)
            if not row:
                continue
            (d,) = c.execute(
                f"SELECT {date_expr} FROM notes WHERE rowid = ?", (rid,)).fetchone()  # nosec B608 — placeholders only
            hits.append(Hit(
                id=row["id"], title=row["title"],
                classification=row["classification"], zone=row["zone"],
                path=row["path"], score=0.0, source="lexical",
                snippet=self._snippet(row["body"]),
                is_latest_version=row.get("is_latest_version", ""),
                date=str(d or ""), type=row.get("type", ""),
                evidence=(
                    self._evidence_from_exact(exact, rid)
                    or ("keyword_exact" if self._literal_keyword_match(
                        query, row["title"], row["body"]
                    ) else "weak_semantic")
                ),
            ))
            hits[-1].create_safety = self._create_safety_from_evidence(
                hits[-1].evidence, owner_count
            )
        return hits

    def _dense_ranked(
        self, query: str, n: int
    ) -> tuple[list[int], dict[int, str], dict[int, int], dict[int, float]]:
        """Dense (vector) ranked note rowids best-first + best chunk text per note
        + best chunk ROWID per note (the last enables retrieval-time near-dup
        suppression to fetch each note's representative vector without
        re-embedding).

        Embeds the query LAZILY here (with the canonical ``query:`` prefix) — the
        only place a query embedding is computed, so lexical-only tools never pay
        the embed cost. Goes through the ``VectorBackend`` ADAPTER (CORE-01); it
        never depends on sqlite-vec directly, so brute-force is identical.

        Embedder-pending guard (S02/CS-01): a cold-start install builds the
        index with an offline placeholder (``BRAIN_EMBEDDER=hash``) so lexical
        search works without a network model download. If the LIVE embedder
        (real, once cached) doesn't match what the stored chunk vectors were
        built with (:meth:`model_matches`), embedding the query with the real
        model and comparing it against placeholder passage vectors would be
        pure noise — worse than no dense leg at all. So this degrades to
        FTS-only (empty dense list) until a rebuild/sync re-embeds with the
        matching model (``brain warmup`` then `brain sync`, which self-heals
        via the SAME model-mismatch check `sync()` already applies)."""
        if not self.model_matches():
            return [], {}, {}, {}
        c = self.conn
        qvec = self.embedder.embed(query, is_query=True)
        chunk_hits = self.backend.search(c, qvec, n * 4)
        best: dict[int, float] = {}
        best_chunk_text: dict[int, str] = {}
        best_chunk_rowid: dict[int, int] = {}
        order: list[int] = []
        for chunk_rowid, score in chunk_hits:
            row = c.execute(
                "SELECT note_rowid, text FROM chunks WHERE rowid=?", (chunk_rowid,)
            ).fetchone()
            if not row:
                continue
            nrid, ctext = int(row[0]), row[1]
            if score > best.get(nrid, -1.0):
                best[nrid] = score
                best_chunk_text[nrid] = ctext
                best_chunk_rowid[nrid] = int(chunk_rowid)
        # Re-rank notes by their best chunk score (chunk_hits is chunk-order).
        order = sorted(best, key=lambda r: best[r], reverse=True)[:n]
        return order, best_chunk_text, best_chunk_rowid, best

    def search(self, query: str, k: int = 10) -> list[Hit]:
        """Back-compat alias for :meth:`hybrid_search` (reranking off)."""
        return self.hybrid_search(query, k=k)

    def hybrid_search(
        self,
        query: str,
        k: int = 10,
        *,
        rrf_k: int = 60,
        candidate_factor: int = 8,
        rerank: bool = False,
        reranker: Any | None = None,
        rerank_top: int = 15,
        rerank_gate: bool | None = None,
    ) -> list[Hit]:
        """Normal fused retrieval path, deliberately free of trace records."""
        return self._hybrid_search_impl(
            query, k=k, rrf_k=rrf_k, candidate_factor=candidate_factor,
            rerank=rerank, reranker=reranker, rerank_top=rerank_top,
            rerank_gate=rerank_gate, trace=None,
        )

    def hybrid_search_with_trace(
        self,
        query: str,
        k: int = 10,
        *,
        rrf_k: int = 60,
        candidate_factor: int = 8,
        rerank: bool = False,
        reranker: Any | None = None,
        rerank_top: int = 15,
        rerank_gate: bool | None = None,
    ) -> tuple[list[Hit], _SearchTrace]:
        """Run the production ranking with opt-in, pre-egress attribution.

        This has the same candidates, scoring, suppression and reranking as
        :meth:`hybrid_search`; it merely records those stages for the caller to
        gate before serialisation.
        """
        n = max(k * candidate_factor, k) if k > 0 else 0
        trace = _SearchTrace(
            # The constant the legs were actually fused at, so `--explain`'s
            # per-leg `contribution` reconciles with the `rrf_k` beside it.
            # Whether the exact leg ran is recorded separately (it is gated on
            # the ADR-0008 calibration key, not on the fusion constant).
            rrf_k=self._fusion_k(rrf_k),
            exact_leg_enabled=self._exact_leg_enabled(rrf_k),
            rerank_requested=rerank,
            candidate_limit=n,
            result_limit=max(0, k),
        )
        hits = self._hybrid_search_impl(
            query, k=k, rrf_k=rrf_k, candidate_factor=candidate_factor,
            rerank=rerank, reranker=reranker, rerank_top=rerank_top,
            rerank_gate=rerank_gate, trace=trace,
        )
        return hits, trace

    def _hybrid_search_impl(
        self,
        query: str,
        k: int = 10,
        *,
        rrf_k: int = 60,
        candidate_factor: int = 8,
        rerank: bool = False,
        reranker: Any | None = None,
        rerank_top: int = 15,
        rerank_gate: bool | None = None,
        trace: _SearchTrace | None,
    ) -> list[Hit]:
        """Run the production ranking stages without applying CLI egress."""
        from ..index_stages.search import run_hybrid_search

        return run_hybrid_search(
            self,
            query,
            k=k,
            rrf_k=rrf_k,
            candidate_factor=candidate_factor,
            rerank=rerank,
            reranker=reranker,
            rerank_top=rerank_top,
            rerank_gate=rerank_gate,
            trace=trace,
            hit_factory=Hit,
        )

    def _diagnose_lexical_rank(self, query: str, rowid: int) -> int | None:
        """Return a target's full FTS rank in an out-of-band diagnostic scan."""
        total = int(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] or 0)
        ranked = self._lexical_ranked(query, total)
        try:
            return ranked.index(rowid) + 1
        except ValueError:
            return None

    def _diagnose_dense_rank(self, query: str, rowid: int) -> int | None:
        """Return a target's full pooled dense rank without changing production.

        ``_dense_ranked`` is called with the total chunk count only after the
        normal query has returned.  Its backend request is therefore a
        diagnostic-only full scan; it neither widens nor feeds the production
        candidate list being explained.
        """
        if not self.model_matches():
            return None
        total_chunks = int(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 0)
        if total_chunks <= 0:
            return None
        dense_result = self._dense_ranked(query, total_chunks)
        ranked = dense_result[0]
        try:
            return ranked.index(rowid) + 1
        except ValueError:
            return None

    def _diagnose_exact_rank(
        self, query: str, rowid: int, enabled: bool,
    ) -> tuple[int | None, int, bool]:
        """Return exact-leg rank/cutoff/membership for a target-only probe.

        ``enabled`` is the decision the SEARCH made, carried on the trace —
        never re-derived here. Since RET-11 the fusion constant on the trace is
        no longer the ADR-0008 calibration key, so re-deriving it from that
        would report an exact leg that ran as disabled.
        """
        query_norm = normalize_identity(query)
        if not query_norm:
            return None, 0, False
        owners, _aliases, _titles = identity_owner_rowids(self, query_norm)
        if rowid in owners:
            records = identity_records(self, owners)
            ordered = sorted(records, key=lambda rid: self._exact_tiebreak(records[rid]))
            return ordered.index(rowid) + 1, EXACT_FULL_CAP if enabled else 0, True

        qtokens = phrase_tokens(query_norm)
        if len(qtokens) < 2:
            return None, 0, False
        partial_records: dict[int, dict[str, Any]] = {}
        for record in title_phrase_candidates(self, qtokens):
            rid = int(record["rowid"])
            if rid in owners or not self._title_phrase_tokens_eligible(qtokens, record["title"]):
                continue
            partial_records[rid] = record
        if rowid not in partial_records:
            return None, 0, False
        ordered = sorted(partial_records, key=lambda rid: self._exact_tiebreak(partial_records[rid]))
        # Exact lists concatenate the capped full tier before their capped
        # partial tier, so a partial candidate's global exact rank starts after
        # the injected full-owner slots.
        full_slots = min(len(owners), EXACT_FULL_CAP) if enabled else 0
        return (
            ordered.index(rowid) + 1 + full_slots,
            full_slots + EXACT_PARTIAL_CAP if enabled else 0,
            True,
        )

    def diagnose_target(
        self,
        query: str,
        target_id: str,
        *,
        max_tier: str,
        trace: _SearchTrace,
        final_rank: int | None,
    ) -> dict[str, Any]:
        """Explain a target from the completed production-stage trace."""
        from ..index_stages.diagnostics import diagnose_target

        return diagnose_target(
            self,
            query,
            target_id,
            max_tier=max_tier,
            trace=trace,
            final_rank=final_rank,
        )

    def freshness(self, newest_hit_date: str, max_tier: str) -> dict[str, Any]:
        """RET-09: how much NEWER material the vault holds past
        ``newest_hit_date`` (valid-time chain: effective_date → document_date
        → created), respecting the caller's egress cap.

        Motivation (2026-07 G&P benchmark): an agent answering a "latest
        decisions" question from a coherent set of curated hits has no signal
        that the vault continues PAST its newest hit — it declares victory on
        stale material. This is that signal: cheap (one aggregate query),
        generic (no topic modelling), and honest about the cap (a capped
        caller learns only counts, consistent with the egress report's
        existing ``withheld`` counter)."""
        date_expr = ("COALESCE(NULLIF(effective_date,''), "
                     "NULLIF(document_date,''), created)")
        # Only ISO-shaped dates participate: a garbage `created` value like
        # "unknown" sorts lexicographically above every real date and would
        # both inflate the count and win the MAX.
        where = (f"{date_expr} > ? AND {date_expr} GLOB "
                 "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'")
        params: list[str] = [newest_hit_date]
        if max_tier != cls_mod.TIERS[-1]:
            # Below the MNPI cap, count only notes the caller could actually
            # surface (unlabelled ranks MNPI, so it is excluded here too).
            allowed = [t for t in cls_mod.TIERS
                       if cls_mod.RANK[t] <= cls_mod.RANK.get(max_tier, 0)]
            where += f" AND classification IN ({','.join('?' * len(allowed))})"
            params += allowed
        row = self.conn.execute(
            f"SELECT COUNT(*), MAX({date_expr}) FROM notes WHERE {where}",  # nosec B608 — placeholders only
            params).fetchone()
        return {"newest_hit_date": newest_hit_date,
                "newer_count": int(row[0] or 0),
                "vault_newest": row[1] or ""}

