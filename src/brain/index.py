"""The derived SQLite index: FTS5 (lexical) + a vector backend (semantic).

Single file (default under app-data, see config.py). Derived and DISPOSABLE —
``rebuild`` drops and recreates everything from the Markdown in ``vault/``, so
deleting the file is always safe. Retrieval depends on the vector ADAPTER
(brain.vectors.VectorBackend), never on sqlite-vec directly.

S03 added:
  * **Chunk-level vectors (IDX-02).** Notes are split into section/block chunks
    (``brain.chunk``); each chunk is embedded with an in-language contextual
    prefix. The vector backend is keyed by *chunk* rowid; a semantic hit maps
    back to its note and we keep the best chunk per note.
  * **Incremental sync (IDX-03).** ``sync`` re-indexes only notes whose
    path+content-hash changed and propagates deletes — no full rebuild.
  * **Model-change guard (IDX-01).** ``embed_model`` + ``embed_dim`` are stored
    in ``meta``; a mismatch on ``sync`` forces a clean rebuild (Arctic vectors
    must never be mixed with HashEmbedder vectors).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .chunk import chunk_text
from .embed import Embedder, get_embedder
from .notes import Note, load_note, scan_vault
from .vectors import SqliteVecBackend, VectorBackend, get_backend

SCHEMA_VERSION = 2


@dataclass
class Hit:
    id: str
    title: str
    classification: str
    zone: str
    path: str
    score: float
    source: str  # "lexical" | "semantic" | "both"
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "classification": self.classification,
            "zone": self.zone,
            "path": self.path,
            "score": round(self.score, 6),
            "source": self.source,
            "snippet": self.snippet,
        }


@dataclass
class _NotePlan:
    """A note's planned index rows (chunking/prefix/dedup done) BEFORE embedding.

    Decouples planning from writing so ``rebuild`` can bulk-embed every note's
    chunk inputs in one batched call (the S11 indexing speed fix) instead of one
    tiny embed per note."""

    note_rowid: int
    row: dict[str, Any]
    chunks: list[Any]
    inputs: list[str]


class BrainIndex:
    def __init__(
        self,
        db_path: Path | None = None,
        backend: VectorBackend | None = None,
        embedder: Embedder | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path else config.index_path()
        self.backend: VectorBackend = backend or get_backend("auto")
        # $BRAIN_EMBEDDER overrides embedder selection (auto|hash|arctic|catalog);
        # default auto. CI + air-gapped validation force "hash" (offline, no model
        # download), per get_embedder's contract — same one-line swap tests use.
        self.embedder: Embedder = embedder or get_embedder(
            os.environ.get("BRAIN_EMBEDDER", "auto")
        )
        # Cache the reranker on the index instance so the ONNX session is loaded
        # ONCE, not on every _apply_rerank call. Without this, qwen3-embed's
        # TextCrossEncoder reloads the 573MB ONNX model per query (S11 finding),
        # making rerank-bound eval pathologically slow. The cache is keyed on the
        # resolved model id so a mid-session BRAIN_RERANKER_MODEL change is honoured.
        self._reranker_cache: tuple[str, Any] | None = None
        # read_only is the VM-leg posture (S06): the connection is opened
        # ``mode=ro`` so the engine CANNOT open WAL or mutate the index. Any
        # write raises ``sqlite3.OperationalError`` (attempt to write a readonly
        # database) and no ``-wal``/``-shm`` sidecar is ever created.
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None

    # -- connection -------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.read_only:
                # Open the (snapshot) DB strictly read-only. mode=ro means SQLite
                # will not create the file, will not open a write journal/WAL, and
                # fails any write — the VM-leg guarantee enforced at the engine.
                uri = f"file:{self.db_path}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True)
                if isinstance(self.backend, SqliteVecBackend):
                    try:
                        self.backend.load_into(self._conn)
                    except Exception:
                        pass
                # Belt-and-suspenders: forbid writes at the connection level too.
                try:
                    self._conn.execute("PRAGMA query_only=ON")
                except sqlite3.OperationalError:
                    pass
                return self._conn
            if self.db_path != Path(":memory:"):
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            # sqlite-vec needs its extension loaded on EVERY connection.
            if isinstance(self.backend, SqliteVecBackend):
                try:
                    self.backend.load_into(self._conn)
                except Exception:
                    pass
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema -----------------------------------------------------------
    def _create_schema(self) -> None:
        c = self.conn
        c.execute("DROP TABLE IF EXISTS notes")
        c.execute("DROP TABLE IF EXISTS notes_fts")
        c.execute("DROP TABLE IF EXISTS chunks")
        c.execute("DROP TABLE IF EXISTS meta")
        c.execute(
            """CREATE TABLE notes (
                rowid INTEGER PRIMARY KEY,
                id TEXT UNIQUE, title TEXT, type TEXT,
                classification TEXT, zone TEXT, path TEXT UNIQUE,
                created TEXT, updated TEXT, sha256 TEXT, content_hash TEXT, body TEXT
            )"""
        )
        # Plain (non-contentless) fts5 so incremental DELETE WHERE rowid works.
        c.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(id, title, body)")
        c.execute(
            """CREATE TABLE chunks (
                rowid INTEGER PRIMARY KEY,
                note_rowid INTEGER NOT NULL,
                ordinal INTEGER, heading TEXT, lang TEXT, text TEXT
            )"""
        )
        c.execute("CREATE INDEX idx_chunks_note ON chunks(note_rowid)")
        c.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self._set_meta("vector_backend", self.backend.name)
        self._set_meta("embed_model", self.embedder.model_id)
        self._set_meta("embed_dim", str(self.embedder.dim))
        self.backend.setup(c, self.embedder.dim)

    def _set_meta(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, v))

    def get_meta(self, k: str) -> str | None:
        try:
            r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return r[0] if r else None

    def _schema_ready(self) -> bool:
        return self.get_meta("schema_version") == str(SCHEMA_VERSION)

    def model_matches(self) -> bool:
        """True iff the stored embed_model/dim match the current embedder."""
        return (
            self.get_meta("embed_model") == self.embedder.model_id
            and self.get_meta("embed_dim") == str(self.embedder.dim)
        )

    # -- insertion --------------------------------------------------------
    def _next_rowid(self, table: str) -> int:
        r = self.conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()
        return int(r[0]) + 1

    def _plan_note(self, note: Note, note_rowid: int) -> _NotePlan:
        """Plan one note's index rows WITHOUT embedding or DB writes.

        Splits the chunking/context-prefix/dedup work out of the write path so
        ``rebuild`` can collect EVERY note's embed inputs first and embed them
        in ONE bulk batched call (the S11 indexing speed fix — see ``rebuild``)
        instead of ~one tiny embed per note. ``sync`` keeps the per-note path
        (``_insert_note``), where only a handful of notes re-embed."""
        row = note.to_row()
        # Real-corpus robustness: a foreign vault has many frontmatter-bearing
        # notes whose id falls back to a non-unique stem (e.g. dozens of
        # SKILL.md / _index.md). notes.id is UNIQUE, so disambiguate a colliding
        # id with a short path hash. Brain-native notes carry unique explicit
        # ids, so this never fires for them (in-process tests are unaffected).
        # Retrieval keys on path (Hit.path), so the synthetic id is internal only.
        seen = getattr(self, "_seen_ids", None)
        if seen is not None:
            if row["id"] in seen:
                row["id"] = f"{row['id']}__{hashlib.sha1(row['path'].encode()).hexdigest()[:8]}"
            seen.add(row["id"])
        chunks = chunk_text(note.body)
        if not chunks:
            # A note with an empty body still gets one chunk (the title) so it is
            # retrievable semantically.
            from .chunk import Chunk, detect_language

            chunks = [Chunk(0, "", note.title or note.id, detect_language(note.title))]
        # UPG-04 Contextual Retrieval: generate a per-note doc-context once and
        # prepend it to every chunk. Inert (returns "") when no LLM is configured
        # ($BRAIN_CONTEXTUAL_LLM unset) — degrades cleanly to the S10 path.
        from .context import doc_context as _doc_context

        dctx = _doc_context(note.title or note.id, note.zone, note.body)
        inputs = [ch.embed_input(note.title, note.zone, dctx) for ch in chunks]
        return _NotePlan(note_rowid=note_rowid, row=row, chunks=chunks, inputs=inputs)

    def _write_planned(
        self, plan: "_NotePlan", vecs: list[list[float]], chunk_rowid: int
    ) -> int:
        """Write a planned note + its FTS row + its chunks (with vectors).

        ``vecs`` must be aligned 1:1 with ``plan.inputs``/``plan.chunks``. Pure
        DB writes — no embedding (the bulk ``rebuild`` path embeds everything up
        front). Returns the next free chunk rowid."""
        c = self.conn
        row = plan.row
        c.execute(
            "INSERT INTO notes(rowid, id, title, type, classification, zone, path,"
            " created, updated, sha256, content_hash, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan.note_rowid, row["id"], row["title"], row["type"], row["classification"],
                row["zone"], row["path"], row["created"], row["updated"],
                row["sha256"], row["content_hash"], row["body"],
            ),
        )
        c.execute(
            "INSERT INTO notes_fts(rowid, id, title, body) VALUES (?,?,?,?)",
            (plan.note_rowid, row["id"], row["title"], row["body"]),
        )
        for ch, vec in zip(plan.chunks, vecs):
            c.execute(
                "INSERT INTO chunks(rowid, note_rowid, ordinal, heading, lang, text)"
                " VALUES (?,?,?,?,?,?)",
                (chunk_rowid, plan.note_rowid, ch.ordinal, ch.heading, ch.lang, ch.text),
            )
            self.backend.upsert(c, chunk_rowid, vec)
            chunk_rowid += 1
        return chunk_rowid

    def _insert_note(self, note: Note, note_rowid: int, chunk_rowid: int) -> int:
        """Plan + embed + write ONE note (the incremental ``sync`` path).

        ``rebuild`` does NOT use this — it plans every note first, then embeds
        all inputs in one bulk batched call (see ``rebuild``), which is the S11
        indexing speed fix. Returns the next free chunk rowid."""
        plan = self._plan_note(note, note_rowid)
        vecs = self.embedder.embed_batch(plan.inputs, is_query=False)
        return self._write_planned(plan, vecs, chunk_rowid)

    def _delete_note(self, note_rowid: int) -> None:
        c = self.conn
        for (crid,) in c.execute(
            "SELECT rowid FROM chunks WHERE note_rowid=?", (note_rowid,)
        ).fetchall():
            self.backend.delete(c, int(crid))
        c.execute("DELETE FROM chunks WHERE note_rowid=?", (note_rowid,))
        c.execute("DELETE FROM notes_fts WHERE rowid=?", (note_rowid,))
        c.execute("DELETE FROM notes WHERE rowid=?", (note_rowid,))

    # -- build (full) -----------------------------------------------------
    def rebuild(self, vault: Path) -> dict[str, Any]:
        """Drop and rebuild the entire index from vault/. Always safe.

        S11 indexing speed fix: chunking / embedding / writing are now THREE
        separate passes. Previously ``_insert_note`` was called once per note
        and embedded only that note's ~2-10 chunks, so the ONNX session ran one
        tiny forward pass per note (~2254 for the real vault) that badly
        under-used the batch dimension and the intra-op threads. Now every note
        is planned (chunked + prefixed) first, ALL chunk inputs are embedded in
        ONE bulk batched call (fastembed batches internally — default 256 — and
        saturates the cores), and only then are the rows + vectors written.
        Same vectors, same retrieval — just far fewer, much larger forward
        passes."""
        self._create_schema()
        self._seen_ids: set[str] = set()  # collision-only id dedup (real corpora)
        # Pass 1: plan every note (chunk + contextual prefix + id dedup) — no embedding.
        plans: list[_NotePlan] = [
            self._plan_note(note, i)
            for i, note in enumerate(scan_vault(vault), start=1)
        ]
        # Pass 2: embed ALL chunk inputs in ONE bulk batched call.
        all_inputs = [inp for p in plans for inp in p.inputs]
        all_vecs = (
            self.embedder.embed_batch(all_inputs, is_query=False) if all_inputs else []
        )
        # Pass 3: write notes + FTS + chunks + vectors.
        chunk_rowid = 1
        vi = 0
        for p in plans:
            nch = len(p.inputs)
            chunk_rowid = self._write_planned(p, all_vecs[vi : vi + nch], chunk_rowid)
            vi += nch
        self.conn.commit()
        self._seen_ids = None  # scope dedup strictly to the rebuild loop
        return {
            "indexed": len(plans),
            "chunks": chunk_rowid - 1,
            "backend": self.backend.name,
            "embed_model": self.embedder.model_id,
            "embed_dim": self.embedder.dim,
            "db": str(self.db_path),
        }

    # -- build (incremental, IDX-03) -------------------------------------
    def sync(self, vault: Path) -> dict[str, Any]:
        """Incrementally reconcile the index with vault/ by path + content-hash.

        Only changed/new notes are re-indexed; notes whose file vanished are
        deleted (delete-propagation). A schema or embed-model mismatch forces a
        clean rebuild (mixing model vectors would corrupt retrieval)."""
        if not self._schema_ready():
            res = self.rebuild(vault)
            res["mode"] = "rebuild(no-schema)"
            return res
        if not self.model_matches():
            res = self.rebuild(vault)
            res["mode"] = "rebuild(model-change)"
            return res

        c = self.conn
        # Current on-disk state: path -> Note.
        on_disk: dict[str, Note] = {}
        for note in scan_vault(vault):
            on_disk[note.path.as_posix()] = note
        # Indexed state: path -> (note_rowid, content_hash).
        indexed: dict[str, tuple[int, str]] = {
            r[0]: (int(r[1]), r[2] or "")
            for r in c.execute("SELECT path, rowid, content_hash FROM notes").fetchall()
        }

        added = updated = unchanged = deleted = 0
        chunk_rowid = self._next_rowid("chunks")

        for path, note in on_disk.items():
            if path not in indexed:
                note_rowid = self._next_rowid("notes")
                chunk_rowid = self._insert_note(note, note_rowid, chunk_rowid)
                added += 1
            elif indexed[path][1] != note.content_hash:
                old_rowid = indexed[path][0]
                self._delete_note(old_rowid)
                # reuse the old note_rowid for stability
                chunk_rowid = self._insert_note(note, old_rowid, chunk_rowid)
                updated += 1
            else:
                unchanged += 1

        for path, (note_rowid, _h) in indexed.items():
            if path not in on_disk:
                self._delete_note(note_rowid)
                deleted += 1

        self.conn.commit()
        total_chunks = int(
            c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        )
        return {
            "mode": "incremental",
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "deleted": deleted,
            "indexed": added + updated + unchanged,
            "chunks": total_chunks,
            "backend": self.backend.name,
            "embed_model": self.embedder.model_id,
            "embed_dim": self.embedder.dim,
            "db": str(self.db_path),
        }

    # -- retrieval --------------------------------------------------------
    def _note_row(self, rowid: int) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT id,title,classification,zone,path,body FROM notes WHERE rowid=?",
            (rowid,),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "title": r[1], "classification": r[2],
            "zone": r[3], "path": r[4], "body": r[5],
        }

    @staticmethod
    def _snippet(body: str, n: int = 160) -> str:
        s = " ".join(body.split())
        return s[:n] + ("…" if len(s) > n else "")

    # Default typed-zone authority weights (anti-burial). Gentle by design.
    _DEFAULT_ZONE_WEIGHTS = {
        "10 People": 1.35, "20 Companies": 1.35, "30 Projects": 1.35,
        "60 Concepts": 1.35, "70 Decisions": 1.35,
    }

    def _zone_weight(self, zone: str) -> float:
        """Authority multiplier for a note's zone (see hybrid_search). Curated
        typed zones get a modest boost over voluminous transcript/source zones;
        unknown zones default to 1.0. Override via BRAIN_ZONE_WEIGHTS (JSON)."""
        weights = getattr(self, "_zone_weights", None)
        if weights is None:
            import json as _json
            import os as _os
            weights = dict(self._DEFAULT_ZONE_WEIGHTS)
            raw = _os.environ.get("BRAIN_ZONE_WEIGHTS")
            if raw:
                try:
                    weights.update({str(k): float(v) for k, v in _json.loads(raw).items()})
                except Exception:
                    pass
            self._zone_weights = weights
        return float(weights.get(zone, 1.0))

    # -- ranked sub-lists for fusion (RET-01) ----------------------------
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

    def _dense_ranked(self, query: str, n: int) -> tuple[list[int], dict[int, str]]:
        """Dense (vector) ranked note rowids best-first + best chunk text per note.

        Embeds the query LAZILY here (with the canonical ``query:`` prefix) — the
        only place a query embedding is computed, so lexical-only tools never pay
        the embed cost. Goes through the ``VectorBackend`` ADAPTER (CORE-01); it
        never depends on sqlite-vec directly, so brute-force is identical."""
        c = self.conn
        qvec = self.embedder.embed(query, is_query=True)
        chunk_hits = self.backend.search(c, qvec, n * 4)
        best: dict[int, float] = {}
        best_chunk_text: dict[int, str] = {}
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
        # Re-rank notes by their best chunk score (chunk_hits is chunk-order).
        order = sorted(best, key=lambda r: best[r], reverse=True)[:n]
        return order, best_chunk_text

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
    ) -> list[Hit]:
        """Fuse FTS5 BM25 (lexical, note-level) + dense vectors (semantic,
        chunk-level → folded to note) into ONE ranking via Reciprocal Rank
        Fusion, RRF(k=60). UNFILTERED — classification filtering is the
        integration surface's job (CLI).

        RRF score for a note = Σ over each list it appears in of
        ``1 / (rrf_k + rank)`` (rank 1-based). RRF needs only the *rank* of each
        item in each list, so the two retrievers' incomparable score scales
        (BM25 vs cosine) never have to be reconciled — the property that makes
        the fusion at-least-as-good-as-either across languages.

        Adapter seam (HARDENED:codex): the dense list comes through the
        ``VectorBackend`` adapter, NOT a hard-wired sqlite-vec call, so a pre-v1
        sqlite-vec change cannot force a retrieval rewrite and brute-force fuses
        identically. The reranker (RET-02) is strictly optional and bounded to
        the top ``rerank_top`` (10-20) candidates.
        """
        n = max(k * candidate_factor, k)
        lex = self._lexical_ranked(query, n)
        dense, best_chunk_text = self._dense_ranked(query, n)

        in_lex = set(lex)
        in_dense = set(dense)
        scores: dict[int, float] = {}
        for rank, rid in enumerate(lex, start=1):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (rrf_k + rank)
        for rank, rid in enumerate(dense, start=1):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (rrf_k + rank)

        # Zone-authority prior (RET-01 anti-burial). Curated/typed notes
        # (Concepts/Decisions/Projects/People/Companies) are authoritative
        # SUMMARIES; raw transcript/source zones are voluminous and near-
        # duplicative, so a single canonical note is easily out-ranked purely by
        # the *volume* of transcript chunks semantically near a query — most
        # acutely for cross-lingual hits, where the canonical note is reachable
        # only via the dense leg. We multiply the fused score by a modest
        # per-zone weight so authority, not volume, decides ties. This mirrors
        # the vault's own typed-zone design (it is NOT tuned to any eval); the
        # weights are deliberately gentle so a genuinely relevant transcript
        # still ranks. Tunable via BRAIN_ZONE_WEIGHTS (json zone->float).
        #
        # SCOPE (RET-01b). A *uniform* boost (scope=all) rescues cross-lingually
        # buried canonical notes but collateral-damages exact-match retrieval: an
        # identifier query's correct hit lives in any zone, and boosting curated
        # zones demotes it. The burial it must fix is, by construction, a
        # DENSE-ONLY phenomenon — a PT query reaching an EN-content canonical note
        # shares no tokens, so it never appears in the lexical leg. So
        # scope=semantic_only applies the prior ONLY to notes found via the dense
        # leg and NOT the lexical leg, leaving exact-match (lexical / "both") hits
        # at weight 1.0. This protects lexical_identifier while still de-burying
        # the cross-lingual golds. It is the DEFAULT because it is principled
        # (an exact-token match needs no authority help and must not be demoted)
        # and dominated scope=all on every segment in the S10 e5-small A/B
        # (identifier 0.958 vs 0.833; overall 0.573 vs 0.526 at the best weight).
        # Tunable via BRAIN_ZONE_SCOPE = all | semantic_only (default
        # semantic_only). Evidence: docs/operations/s10-pt-rootcause-and-fix.md.
        if scores:
            rids = tuple(scores)
            qmarks = ",".join("?" * len(rids))
            zmap = {
                int(r): (z or "")
                for r, z in self.conn.execute(
                    f"SELECT rowid, zone FROM notes WHERE rowid IN ({qmarks})", rids
                )
            }
            scope = os.environ.get("BRAIN_ZONE_SCOPE", "semantic_only").strip().lower()
            for rid in scores:
                if scope == "semantic_only" and rid in in_lex:
                    continue  # exact-match hit — authority prior does not apply
                scores[rid] *= self._zone_weight(zmap.get(rid, ""))

        def _source(rid: int) -> str:
            if rid in in_lex and rid in in_dense:
                return "both"
            return "lexical" if rid in in_lex else "semantic"

        ordered = sorted(scores, key=lambda r: (-scores[r], r))

        hits: list[Hit] = []
        for rid in ordered:
            row = self._note_row(rid)
            if not row:
                continue
            snippet_src = best_chunk_text.get(rid, row["body"])
            hits.append(
                Hit(
                    id=row["id"], title=row["title"],
                    classification=row["classification"], zone=row["zone"],
                    path=row["path"], score=scores[rid], source=_source(rid),
                    snippet=self._snippet(snippet_src),
                )
            )

        if rerank and hits:
            hits = self._apply_rerank(query, hits, reranker, rerank_top)
        return hits[:k]

    def _apply_rerank(
        self, query: str, hits: list[Hit], reranker: Any | None, rerank_top: int
    ) -> list[Hit]:
        """Re-order ONLY the top ``rerank_top`` hits with a cross-encoder; the
        tail is left untouched and appended after. The window is clamped to
        [10, ceiling] where ceiling is 20 by default but raisable via
        BRAIN_RERANK_MAX; ``BRAIN_RERANK_TOP`` overrides the requested window
        size itself (so a wide-candidate pass can be driven by env without
        changing call sites). Skippable: a None/identity reranker is a no-op
        (RET-02)."""
        from .rerank import NoopReranker, clamp_rerank_top, get_reranker, _resolve_reranker_model

        env_top = os.environ.get("BRAIN_RERANK_TOP")
        if env_top:
            try:
                rerank_top = int(env_top)
            except ValueError:
                pass
        # Resolve the reranker ONCE and cache it on the instance. A caller-supplied
        # reranker wins; otherwise we honour $BRAIN_RERANKER_MODEL, caching the
        # constructed cross-encoder so its ONNX session is loaded only once per
        # index lifetime (not per query — S11 perf fix).
        if reranker is not None:
            rr = reranker
        else:
            mid = _resolve_reranker_model()
            if self._reranker_cache and self._reranker_cache[0] == mid:
                rr = self._reranker_cache[1]
            else:
                rr = get_reranker("auto")
                self._reranker_cache = (mid, rr)
        top_n = clamp_rerank_top(rerank_top)
        head, tail = hits[:top_n], hits[top_n:]
        passages = [
            (self._note_row(self._rowid_of(h.id)) or {}).get("body", h.snippet) or h.snippet
            for h in head
        ]
        passages = [p[:2000] for p in passages]
        try:
            rel = rr.rerank(query, passages)
        except Exception:
            # SKIPPABLE contract (RET-02): if the cross-encoder runtime/model is
            # unavailable (offline, not bundled), degrade to identity — never let
            # an absent precision-booster break retrieval.
            rel = NoopReranker().rerank(query, passages)
        reordered = [h for _, h in sorted(zip(rel, head), key=lambda t: t[0], reverse=True)]
        return reordered + tail

    def _rowid_of(self, note_id: str) -> int:
        r = self.conn.execute("SELECT rowid FROM notes WHERE id=?", (note_id,)).fetchone()
        return int(r[0]) if r else -1

    # -- agentic tool surface (RET-04): grep + bases_query ---------------
    def grep(
        self, pattern: str, *, k: int = 20, ignore_case: bool = True, regex: bool = False
    ) -> list[dict[str, Any]]:
        """Lexical-first exact/regex scan over note bodies — NO embedding.

        The agent's lexical-first entry point: it never embeds the query, so it
        is the cheap first probe before escalating to :meth:`hybrid_search`.
        Returns note-shaped dicts (filterable by the CLI egress gate) with the
        first matching line as the snippet and a match count."""
        import re as _re

        flags = _re.IGNORECASE if ignore_case else 0
        # Multi-word NATURAL-LANGUAGE handling: a literal full-question pattern
        # never matches a line verbatim, so a non-regex multi-token query is
        # treated as OR-of-terms (significant tokens only) and ranked by how many
        # DISTINCT terms a note hits, then total matches. Single-token and
        # explicit --regex patterns keep exact literal/regex behaviour (so the
        # tool's precise-pattern contract — and its tests — are unchanged).
        _STOP = {"the", "a", "an", "of", "to", "is", "are", "was", "were", "what",
                 "which", "who", "and", "or", "for", "on", "in", "about", "que",
                 "qual", "quais", "foi", "sobre", "ele", "ela", "com", "para",
                 "uma", "dos", "das", "no", "na", "em", "se", "de", "do", "da"}
        terms: list[str] = []
        if not regex:
            terms = [t for t in _re.split(r"\W+", pattern, flags=_re.UNICODE)
                     if len(t) > 2 and t.lower() not in _STOP]
        multi = (not regex) and len(terms) > 1
        if multi:
            rxs = [_re.compile(_re.escape(t), flags) for t in terms]
        else:
            try:
                rx = _re.compile(pattern if regex else _re.escape(pattern), flags)
            except _re.error:
                rx = _re.compile(_re.escape(pattern), flags)
            rxs = [rx]
        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path,body FROM notes"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            body = r[5] or ""
            matches = [ln for ln in body.splitlines() if any(x.search(ln) for x in rxs)]
            if not matches:
                continue
            distinct = (
                sum(1 for x in rxs if any(x.search(ln) for ln in matches)) if multi else 1
            )
            out.append({
                "id": r[0], "title": r[1], "classification": r[2],
                "zone": r[3], "path": r[4],
                "match_count": len(matches),
                "terms_matched": distinct,
                "snippet": self._snippet(matches[0]),
                "source": "grep",
            })
        out.sort(key=lambda d: (-d.get("terms_matched", 1), -d["match_count"], d["id"]))
        return out[:k]

    def bases_query(
        self,
        filters: dict[str, str] | None = None,
        *,
        k: int = 50,
        order_by: str = "updated",
    ) -> list[dict[str, Any]]:
        """Structured frontmatter query (an Obsidian-Bases-style view) over the
        indexed columns — NO embedding. Filters are exact-match on
        id/title/type/classification/zone/path; unknown keys are ignored. Returns
        note-shaped dicts for the CLI egress gate."""
        cols = {"id", "title", "type", "classification", "zone", "path",
                "created", "updated"}
        filters = filters or {}
        where, params = [], []
        for key, val in filters.items():
            if key in cols:
                where.append(f"{key} = ?")
                params.append(val)
        order_col = order_by if order_by in cols else "updated"
        sql = (
            "SELECT id,title,classification,zone,path,type,updated FROM notes"
            + (" WHERE " + " AND ".join(where) if where else "")
            + f" ORDER BY {order_col} DESC, id ASC LIMIT ?"
        )
        params.append(k)
        rows = self.conn.execute(sql, params).fetchall()
        keys = ["id", "title", "classification", "zone", "path", "type", "updated"]
        return [dict(zip(keys, r)) for r in rows]

    def graph_expand(
        self, seeds: list[str], *, depth: int = 2, k: int = 10, use_ppr: bool = True
    ) -> dict[str, Any]:
        """On-demand wikilink-BFS + PPR expansion (RET-03). DISCOVERY-ONLY — the
        derived graph is never authoritative; results carry that flag."""
        from .graph import graph_expand as _expand

        return _expand(self.conn, seeds, depth=depth, k=k, use_ppr=use_ppr)

    def get(self, note_id: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT id,title,type,classification,zone,path,created,updated,sha256,body"
            " FROM notes WHERE id=?",
            (note_id,),
        ).fetchone()
        if not r:
            return None
        keys = ["id", "title", "type", "classification", "zone", "path",
                "created", "updated", "sha256", "body"]
        return dict(zip(keys, r))

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path,updated FROM notes "
            "ORDER BY updated DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ["id", "title", "classification", "zone", "path", "updated"]
        return [dict(zip(keys, r)) for r in rows]

    def stats(self) -> dict[str, Any]:
        c = self.conn
        notes = int(c.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        chunks = int(c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        return {
            "notes": notes,
            "chunks": chunks,
            "schema_version": self.get_meta("schema_version"),
            "vector_backend": self.get_meta("vector_backend"),
            "embed_model": self.get_meta("embed_model"),
            "embed_dim": self.get_meta("embed_dim"),
            "db": str(self.db_path),
        }
