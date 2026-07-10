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

import datetime as _dt
import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, frontmatter
from .chunk import chunk_text
from .embed import Embedder, get_embedder
from .notes import Note, load_note, scan_vault
from .vectors import SqliteVecBackend, VectorBackend, get_backend

# Optional 3rd-party regex engine (mrab-regex) with a REAL per-call match
# timeout -- unlike stdlib `re`, `regex.search(text, timeout=...)` genuinely
# bounds catastrophic backtracking (confirmed: stdlib re on r'(a+)+$' vs 30
# 'a's takes ~47s; the `regex` module's timeout on the identical input returns
# in well under a millisecond). Declared as the optional `index` extra in
# pyproject.toml; falls back to stdlib `re` when not installed, in which case
# the pattern-length cap below is the sole ReDoS mitigation (documented
# residual risk: docs/SECURITY_NOTES.md).
try:
    import regex as _grep_engine
    _GREP_HAS_TIMEOUT = True
except ImportError:  # pragma: no cover - exercised only when `regex` is absent
    _grep_engine = re
    _GREP_HAS_TIMEOUT = False

# ReDoS / resource-exhaustion guard for BrainIndex.grep (RET-04 hardening).
MAX_GREP_PATTERN_LEN = 200      # absurdly long patterns are the abuse surface, not legitimate use
GREP_REGEX_TIMEOUT_S = 2.0      # per-match wall-clock budget (only enforced with the `regex` engine)


def _today() -> _dt.date:
    """Today, overridable via ``BRAIN_NOW=YYYY-MM-DD`` so recency ranking is
    deterministic in tests (mirrors the injectable-clock pattern used by
    maintenance staleness)."""
    v = os.environ.get("BRAIN_NOW", "").strip()
    if v:
        try:
            return _dt.date.fromisoformat(v[:10])
        except ValueError:
            pass
    return _dt.date.today()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _recency_factor(date_str: str, today: _dt.date, weight: float,
                    half_life: float) -> float:
    """Gentle multiplicative STALENESS PENALTY for the RRF fusion, bounded to
    ``(1 - weight, 1.0]``. A note dated today (or in the future) is neutral at
    ``1.0``; the penalty deepens as the note ages, halving its distance-from-full
    every ``half_life`` days, asymptoting at ``1 - weight`` for very old notes.
    An undated note (or ``weight<=0``) is neutral at ``1.0`` — undated notes are
    never penalised.

    A *penalty* (≤1), not a boost (>1), so the fused score never exceeds the RRF
    ceiling ``2/(rrf_k+1)`` — the fusion-scale invariant the zone-authority prior
    also respects. Relative order between any two DATED notes is identical to a
    symmetric boost, so the newer of two topically-similar hits still wins."""
    if weight <= 0 or not date_str:
        return 1.0
    try:
        d = _dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return 1.0
    age = (today - d).days
    if age <= 0:
        return 1.0
    return 1.0 - weight * (1.0 - 0.5 ** (age / half_life))


class GrepPatternError(ValueError):
    """A user-supplied grep pattern was rejected before compilation."""


def _grep_bounded_search(compiled, text: str):
    """``compiled.search(text)`` with a wall-clock budget when the `regex`
    engine is available. A pathological pattern degrades to "no match on this
    line" (never raises out of `grep`) rather than hanging the whole call."""
    if _GREP_HAS_TIMEOUT:
        try:
            return compiled.search(text, timeout=GREP_REGEX_TIMEOUT_S)
        except TimeoutError:
            return None
    return compiled.search(text)


SCHEMA_VERSION = 3  # TMP-02: bitemporal columns added (notes gain 6 new cols).
                    # Migration-safe by construction: sync()'s _schema_ready()
                    # check already forces a rebuild() on any version mismatch —
                    # no separate ALTER-TABLE migration path is needed.


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
    is_latest_version: str = ""  # TMP-02: "true"|"false"|"" — post-egress field,
                                  # never consulted by the classification gate.

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
            "is_latest_version": self.is_latest_version,
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
        # Multi-hop retrieval (RET-06) caches: the wikilink graph and the entity
        # lexicon are both derived from the immutable ``notes`` table, so build
        # them once per index lifetime (not per query). None until first use.
        self._link_graph: Any | None = None
        self._entity_lex: Any | None = None
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
            is_file_backed = self.db_path != Path(":memory:")
            if is_file_backed:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            if is_file_backed and self.db_path.exists():
                # sqlite3.connect() creates the file (when absent) with the
                # process umask -- often 0o644 / world-readable on a typical
                # single-user default. The index can hold note bodies up to and
                # including MNPI-tier content (the classification gate is an
                # egress *decision*, not containment), so tighten to owner-only
                # immediately, regardless of umask.
                config.secure_file_permissions(self.db_path)
            # sqlite-vec needs its extension loaded on EVERY connection.
            if isinstance(self.backend, SqliteVecBackend):
                try:
                    self.backend.load_into(self._conn)
                except Exception:
                    pass
            self._conn.execute("PRAGMA journal_mode=WAL")
            # M-6: without a busy_timeout, a concurrent writer's first INSERT
            # raises "database is locked" immediately instead of waiting for
            # the other writer's transaction to finish. 5s covers a normal
            # sync; a still-locked DB past that surfaces as a real error.
            self._conn.execute("PRAGMA busy_timeout=5000")
            if is_file_backed:
                # WAL mode creates -wal/-shm sidecars on first write; make sure
                # those inherit the same owner-only posture too (they can carry
                # the same sensitive content as the main DB file).
                for suffix in ("-wal", "-shm"):
                    side = Path(str(self.db_path) + suffix)
                    if side.exists():
                        config.secure_file_permissions(side)
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
                created TEXT, updated TEXT, sha256 TEXT, content_hash TEXT, body TEXT,
                document_date TEXT, effective_date TEXT, superseded_date TEXT,
                is_latest_version TEXT, superseded_by TEXT, previous_version TEXT
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
        # FALSE POSITIVE (scanner: string-built SQL / hardcoded_sql_expressions):
        # `table` is never user input -- it is a hardcoded literal ("chunks" /
        # "notes") at both call sites below, never derived from a request
        # argument. See docs/SECURITY_NOTES.md.
        r = self.conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()  # nosec B608
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
                # FALSE POSITIVE (scanner: weak-hash / hashlib-insecure-functions):
                # SHA1 here is a non-security content-addressed de-dup suffix (a
                # short, stable disambiguator for a colliding synthetic id), not a
                # security boundary -- collision resistance / preimage resistance
                # don't matter for this use. See docs/SECURITY_NOTES.md.
                row["id"] = f"{row['id']}__{hashlib.sha1(row['path'].encode()).hexdigest()[:8]}"  # nosec B303 B324
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
            " created, updated, sha256, content_hash, body, document_date,"
            " effective_date, superseded_date, is_latest_version, superseded_by,"
            " previous_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan.note_rowid, row["id"], row["title"], row["type"], row["classification"],
                row["zone"], row["path"], row["created"], row["updated"],
                row["sha256"], row["content_hash"], row["body"],
                row.get("document_date", ""), row.get("effective_date", ""),
                row.get("superseded_date", ""), row.get("is_latest_version", ""),
                row.get("superseded_by", ""), row.get("previous_version", ""),
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

        # Delete-propagation FIRST (H-2): a renamed/moved note keeps its id but
        # gets a new path, landing in both "path not in on_disk" (old path,
        # deleted below) and "path not in indexed" (new path, inserted above).
        # If we insert before deleting, the new-path insert can collide with
        # the still-present old-path row on the UNIQUE `id` column. Deleting
        # every stale path (by path, and belt-and-suspenders by id collision)
        # before the insert/update pass makes rename-with-same-id a no-crash,
        # normal reconcile.
        for path, (note_rowid, _h) in indexed.items():
            if path not in on_disk:
                self._delete_note(note_rowid)
                deleted += 1

        # Re-fetch indexed ids after the deletion pass above so a same-id
        # rename never hits "UNIQUE constraint failed: notes.id".
        indexed_ids: dict[str, int] = {
            r[0]: int(r[1])
            for r in c.execute("SELECT id, rowid FROM notes").fetchall()
        }

        for path, note in on_disk.items():
            if path not in indexed:
                if note.id in indexed_ids:
                    self._delete_note(indexed_ids[note.id])
                    del indexed_ids[note.id]
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
            "SELECT id,title,classification,zone,path,body,is_latest_version"
            " FROM notes WHERE rowid=?",
            (rowid,),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "title": r[1], "classification": r[2],
            "zone": r[3], "path": r[4], "body": r[5], "is_latest_version": r[6] or "",
        }

    @staticmethod
    def _snippet(body: str, n: int = 160) -> str:
        s = " ".join(body.split())
        return s[:n] + ("…" if len(s) > n else "")

    # Default typed-zone authority weights (anti-burial).
    # PT-02 (s05): curated_weight=2.0 / meetings_damp=0.55 is the CV-SELECTED
    # candidate from a 5x4=20-point (curated boost x "40 Meetings" damp) grid,
    # chosen by stratified 5-fold CV on train+dev ONLY (H34) — see
    # `eval/pt_zonefix_sweep.py` and `docs/eval-bench/pt-fix.md`. All 5 outer
    # folds independently selected this SAME point (stable, not noise). It
    # replaces the pre-S05 1.35-only default, which was provably a no-op on
    # the migrated index (see `_resolve_zone` docstring — the prior needs
    # BOTH the source_zone fix AND a weight strong enough to move fused RRF
    # ranks once alive). Damping "40 Meetings" (rather than a bigger uniform
    # curated boost) is what avoids trading away monolingual_pt / multi_hop,
    # per `docs/eval-bench/pt-diagnosis.md` §3 E5's own warning.
    _DEFAULT_ZONE_WEIGHTS = {
        "10 People": 2.0, "20 Companies": 2.0, "30 Projects": 2.0,
        "60 Concepts": 2.0, "70 Decisions": 2.0,
        "40 Meetings": 0.55,
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

    def _resolve_zone(self, zone_col: str, path: str) -> str:
        """Anti-burial authority KEY for a note (PT-02, s05).

        The live migrated index flattens every Johnny-Decimal zone to
        ``brain``/``raw`` in the ``notes.zone`` column (People pages land in
        ``brain/areas/`` next to Companies; meeting transcripts land in
        ``raw/``). ``_zone_weight`` is keyed on the ORIGINAL zone names
        (``"10 People"``, ``"40 Meetings"``, ...), so on the flattened column
        it was a no-op — see `docs/eval-bench/pt-diagnosis.md` root cause 1.

        This is a RETRIEVAL-TIME-ONLY fix (H23/H11 reversibility gate): the
        migration tool (`tools/apply_live_migration.py`) already writes the
        original zone into each migrated note's frontmatter as
        ``source_zone:`` alongside ``source_path:``. Rather than re-indexing
        to carry that field into the SQLite schema, we read it straight off
        the note's file (identified by the already-indexed ``path`` column)
        at query time — no index/schema/vector change, fully reversible by
        deleting this method and the call site. Brain-native notes created
        after the migration have no ``source_zone`` (they were never
        Johnny-Decimal); those fall back to the flattened ``zone`` column
        unchanged, so this fix only ever *adds* signal, never removes it.

        Only the frontmatter block (first ~2 KB) is read, not the full note
        body — cheap even for large meeting transcripts. Results are cached
        per ``(path, mtime)`` for the life of the index object so a query
        that touches the same candidate note twice (or a session with many
        queries) does not re-read the file repeatedly; a changed mtime
        invalidates the cache entry automatically.

        Kill switch: ``BRAIN_ZONE_SOURCE_MODE=column`` disables this and
        restores the pre-fix behaviour (flattened column only) for rollback
        without a code change.
        """
        if os.environ.get("BRAIN_ZONE_SOURCE_MODE", "auto").strip().lower() == "column":
            return zone_col
        cache = getattr(self, "_source_zone_cache", None)
        if cache is None:
            cache = {}
            self._source_zone_cache = cache
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            return zone_col
        key = (path, mtime_ns)
        if key in cache:
            return cache[key] or zone_col
        source_zone: str | None = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(2048)
            if head.startswith("---"):
                end = head.find("---", 3)
                block = head[3:end] if end != -1 else head[3:]
                meta = frontmatter.parse(block)
                sz = meta.get("source_zone")
                if isinstance(sz, str) and sz.strip():
                    source_zone = sz.strip()
        except OSError:
            source_zone = None
        cache[key] = source_zone
        return source_zone or zone_col

    # Near-duplicate transcript suppression (PT-02, s05 — diagnosis cause #3).
    # OFF BY DEFAULT (`_DEFAULT_DEDUP_THRESHOLD = None`). The s05 CV sweep
    # (`eval/pt_dedup_sweep.py`, train+dev, H34) found the lever adds EXACTLY
    # ZERO incremental Recall@10 on top of the zone-authority fix — every one
    # of 5 CV folds selected "disabled" — because (a) the 0.55x zone damp
    # already demotes the flooding transcript zone, and (b) the residual PT→EN
    # failures are e5-small embedder-floor golds at note-rank 100-250, beyond
    # the candidate pool no top-k reordering can reach (diagnosis E4/E6, em-01
    # scope). Shipping it ON by default would add latency + risk for no proven
    # value, so it stays off. The machinery + `BRAIN_DEDUP_THRESHOLD` (float in
    # (0,1)) / `BRAIN_DEDUP_SCOPE=all|transcript` knobs are retained + tested
    # for a future corpus / post-embedder-swap re-eval. See
    # `docs/eval-bench/pt-fix.md`.
    _DEFAULT_DEDUP_THRESHOLD: float | None = None
    _TRANSCRIPT_ZONES = frozenset({"40 Meetings", "raw"})

    def _dedup_params(self) -> tuple[float | None, str]:
        thr = self._DEFAULT_DEDUP_THRESHOLD
        raw = os.environ.get("BRAIN_DEDUP_THRESHOLD")
        if raw is not None:
            try:
                thr = float(raw)
            except ValueError:
                pass
        scope = os.environ.get("BRAIN_DEDUP_SCOPE", "transcript").strip().lower()
        return thr, scope

    def _suppress_near_dups(
        self,
        ordered: list[int],
        best_chunk_rowid: dict[int, int],
        zmap: dict[int, str],
        col_zone: dict[int, str],
        in_lex: set[int],
    ) -> list[int]:
        """Retrieval-time near-duplicate SUPPRESSION (H11/H23 — never an
        index-time deletion; suppressed notes are DEMOTED to the tail, so they
        still surface at a larger k and nothing is removed from the index).

        Root cause #3 of `docs/eval-bench/pt-diagnosis.md`: meeting transcripts
        are near-duplicative (6,752 chunk pairs >=0.80 cosine; 3,988 >=0.97) and
        monopolise the top-k (100% of the failing cross-lingual top-10),
        crowding out the single canonical curated note. This pass walks the
        fused ranking best-first and, when a TRANSCRIPT-zone candidate's
        representative (best-chunk) vector is >= the threshold cosine to an
        already-KEPT candidate's vector, defers it — keeping the cluster's
        highest-ranked representative but freeing the slots its near-clones
        would occupy. It is deliberately CONSERVATIVE (diagnosis §5 F3 risk):
          * only transcript-zone candidates are eligible for suppression
            (scope=transcript, the default) — a curated note is never
            suppressed, so a genuinely-relevant canonical hit cannot be lost;
          * the FIRST (highest-ranked) member of any near-dup cluster always
            survives, so a relevant transcript still surfaces (mono-PT uses
            transcript golds — diagnosis E3b);
          * lexical ("both"/exact) hits are never suppressed;
          * a candidate with no dense best-chunk vector (lexical-only) is never
            suppressed and is not usable as a suppressor reference.
        """
        thr, scope = self._dedup_params()
        if thr is None or not (0.0 < thr < 1.0) or len(ordered) <= 1:
            return ordered
        from .vectors import cosine

        want = [
            best_chunk_rowid[rid] for rid in ordered if rid in best_chunk_rowid
        ]
        vecs_by_chunk = self.backend.get_vectors(self.conn, want) if want else {}

        def _vec(rid: int) -> list[float] | None:
            cr = best_chunk_rowid.get(rid)
            return vecs_by_chunk.get(cr) if cr is not None else None

        def _is_transcript(rid: int) -> bool:
            return (zmap.get(rid, "") in self._TRANSCRIPT_ZONES
                    or col_zone.get(rid, "") in self._TRANSCRIPT_ZONES)

        kept: list[int] = []
        kept_vecs: list[list[float]] = []
        deferred: list[int] = []
        for rid in ordered:
            v = _vec(rid)
            eligible = (
                v is not None
                and rid not in in_lex
                and (scope == "all" or _is_transcript(rid))
            )
            if eligible and any(cosine(v, kv) >= thr for kv in kept_vecs):
                deferred.append(rid)
                continue
            kept.append(rid)
            if v is not None:
                kept_vecs.append(v)
        return kept + deferred

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

    def _dense_ranked(
        self, query: str, n: int
    ) -> tuple[list[int], dict[int, str], dict[int, int]]:
        """Dense (vector) ranked note rowids best-first + best chunk text per note
        + best chunk ROWID per note (the last enables retrieval-time near-dup
        suppression to fetch each note's representative vector without
        re-embedding).

        Embeds the query LAZILY here (with the canonical ``query:`` prefix) — the
        only place a query embedding is computed, so lexical-only tools never pay
        the embed cost. Goes through the ``VectorBackend`` ADAPTER (CORE-01); it
        never depends on sqlite-vec directly, so brute-force is identical."""
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
        return order, best_chunk_text, best_chunk_rowid

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
        dense, best_chunk_text, best_chunk_rowid = self._dense_ranked(query, n)

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
        zmap: dict[int, str] = {}
        col_zone: dict[int, str] = {}
        rdate: dict[int, str] = {}
        if scores:
            rids = tuple(scores)
            # FALSE POSITIVE (scanner: string-built SQL / hardcoded_sql_expressions):
            # `qmarks` interpolates only literal "?" placeholder characters (one
            # per element of `rids`) -- the VALUES themselves are bound as query
            # params (`rids`, passed separately below), never string-formatted
            # into the SQL text. See docs/SECURITY_NOTES.md.
            qmarks = ",".join("?" * len(rids))
            # Valid-time fallback: effective_date → document_date → created,
            # the same chain bases-query uses (§2/ADR-0003 Ruling 2).
            date_expr = ("COALESCE(NULLIF(effective_date,''), "
                         "NULLIF(document_date,''), created)")
            for r, z, p, d in self.conn.execute(
                f"SELECT rowid, zone, path, {date_expr} FROM notes "  # nosec B608
                f"WHERE rowid IN ({qmarks})", rids
            ):
                rid = int(r)
                col_zone[rid] = z or ""
                zmap[rid] = self._resolve_zone(z or "", p or "")
                rdate[rid] = d or ""
            scope = os.environ.get("BRAIN_ZONE_SCOPE", "semantic_only").strip().lower()
            for rid in scores:
                if scope == "semantic_only" and rid in in_lex:
                    continue  # exact-match hit — authority prior does not apply
                scores[rid] *= self._zone_weight(zmap.get(rid, ""))

            # Recency prior (RET-07). The RRF fusion above is time-blind: a stale
            # version of a document outranks its current successor purely on text
            # similarity, so a "latest developments" query grounds on months-old
            # material. A gentle, multiplicative staleness penalty (≤1.0, so the
            # fused score stays under the RRF ceiling like the zone prior) makes
            # the more recent of two topically-similar hits win — without
            # reconciling score scales (same reason RRF uses ranks). Neutral
            # (×1.0) for undated notes, so nothing is penalised for lacking a date.
            # Knobs: BRAIN_RECENCY_WEIGHT (0 disables), BRAIN_RECENCY_HALFLIFE_DAYS.
            # ponytail: always-on gentle prior; query-intent weighting (heavier
            # decay for "latest"/"as of <date>" queries) is the upgrade path if
            # near-ties still slip through.
            rweight = _env_float("BRAIN_RECENCY_WEIGHT", 0.25)
            if rweight > 0:
                rhalf = _env_float("BRAIN_RECENCY_HALFLIFE_DAYS", 180.0)
                today = _today()
                for rid in scores:
                    scores[rid] *= _recency_factor(
                        rdate.get(rid, ""), today, rweight, rhalf)

        def _source(rid: int) -> str:
            if rid in in_lex and rid in in_dense:
                return "both"
            return "lexical" if rid in in_lex else "semantic"

        ordered = sorted(scores, key=lambda r: (-scores[r], r))
        ordered = self._suppress_near_dups(
            ordered, best_chunk_rowid, zmap, col_zone, in_lex)

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
                    is_latest_version=row.get("is_latest_version", ""),
                )
            )

        if rerank and hits:
            hits = self._apply_rerank(query, hits, reranker, rerank_top)
        return hits[:k]

    # -- multi-hop graph-augmented retrieval (RET-06) --------------------
    def _link_graph_cached(self) -> Any:
        from .graph import build_graph
        if self._link_graph is None:
            self._link_graph = build_graph(self.conn)
        return self._link_graph

    def _entity_lexicon_cached(self) -> Any:
        from .multihop import EntityLexicon
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
        from .multihop import graph_augmented_ranking

        lexicon = self._entity_lexicon_cached()
        mentions = lexicon.mentions(query)
        from .multihop import is_multihop_shaped

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
        )

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
        first matching line as the snippet and a match count.

        Bounded against ReDoS / resource exhaustion (RET-04 hardening):
        ``pattern`` is length-capped (:data:`MAX_GREP_PATTERN_LEN`) before
        compilation, and every match is wall-clock-bounded
        (:data:`GREP_REGEX_TIMEOUT_S`) when the optional `regex` engine is
        installed — see :func:`_grep_bounded_search`.
        """
        if len(pattern) > MAX_GREP_PATTERN_LEN:
            raise GrepPatternError(
                f"grep pattern too long ({len(pattern)} chars; max "
                f"{MAX_GREP_PATTERN_LEN}) — refusing to compile "
                "(ReDoS / resource-exhaustion guard)"
            )
        # M-1: without the `regex` engine, a user-supplied --regex pattern has
        # NO wall-clock bound (stdlib `re` can hang on catastrophic backtracking
        # even under MAX_GREP_PATTERN_LEN, e.g. `(a+)+$`). Refuse explicit regex
        # mode outright on the minimal build rather than silently degrading to
        # an unbounded engine (VM-reachable surface).
        if regex and not _GREP_HAS_TIMEOUT:
            raise GrepPatternError(
                "grep --regex requires the 'regex' engine (pip install "
                "'profile-a-brain[index]') for a bounded match timeout; "
                "the minimal build has no ReDoS-safe regex path"
            )
        _re = _grep_engine  # the timeout-capable `regex` engine when available, else stdlib re

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
            matches = [ln for ln in body.splitlines()
                       if any(_grep_bounded_search(x, ln) for x in rxs)]
            if not matches:
                continue
            distinct = (
                sum(1 for x in rxs if any(_grep_bounded_search(x, ln) for ln in matches))
                if multi else 1
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
        latest_only: bool = False,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Structured frontmatter query (an Obsidian-Bases-style view) over the
        indexed columns — NO embedding. Filters are exact-match on
        id/title/type/classification/zone/path; unknown keys are ignored. Returns
        note-shaped dicts for the CLI egress gate.

        TMP-02 temporal views (ADR-0003 Ruling 2/8 — the Latest Only / As Of
        Bases):

        - ``latest_only``: excludes any note explicitly retired
          (``is_latest_version: false``). A note that never entered a
          supersession chain has no opinion here and is included (it IS the
          current — only — version of itself).
        - ``as_of``: an ISO date; returns notes valid AT that date under
          valid-time semantics — ``effective_date`` if present, else
          ``document_date``, else ``created`` (fallback chain, per the ADR) —
          excluding anything not yet effective by that date or already
          superseded by then. Composable with ``latest_only`` (as-of naturally
          admits a since-superseded note back in, latest_only would then
          exclude it again — apply them together only if that's the intent;
          the CLI keeps them as independent flags).
        """
        cols = {"id", "title", "type", "classification", "zone", "path",
                "created", "updated"}
        filters = filters or {}
        where, params = [], []
        # FALSE POSITIVE (scanner: string-built SQL / hardcoded_sql_expressions):
        # `key` / `order_col` are only ever interpolated after an explicit
        # `in cols` allowlist check against the fixed column set above -- an
        # unrecognised key/order_by is dropped/defaulted, never reaches the SQL
        # text. Every VALUE (`val`, `k`) is a bound param, never interpolated.
        # See docs/SECURITY_NOTES.md.
        for key, val in filters.items():
            if key in cols:
                where.append(f"{key} = ?")  # nosec B608 - key is allowlisted above
                params.append(val)
        if latest_only:
            where.append("is_latest_version IS NOT 'false'")
        if as_of:
            where.append(
                "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) <= ?"
            )
            params.append(as_of)
            where.append("(superseded_date IS NULL OR superseded_date = '' OR superseded_date > ?)")
            params.append(as_of)
        order_col = order_by if order_by in cols else "updated"
        sql = (
            "SELECT id,title,classification,zone,path,type,updated,is_latest_version FROM notes"
            + (" WHERE " + " AND ".join(where) if where else "")
            + f" ORDER BY {order_col} DESC, id ASC LIMIT ?"  # nosec B608 - order_col is allowlisted above
        )
        params.append(k)
        rows = self.conn.execute(sql, params).fetchall()
        keys = ["id", "title", "classification", "zone", "path", "type", "updated",
                "is_latest_version"]
        return [dict(zip(keys, r)) for r in rows]

    def graph_expand(
        self, seeds: list[str], *, depth: int = 2, k: int = 10, use_ppr: bool = True,
        extra_edges: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """On-demand wikilink-BFS + PPR expansion (RET-03). DISCOVERY-ONLY — the
        derived graph is never authoritative; results carry that flag.
        ``extra_edges`` (GRF-01, optional) folds graphify's INFERRED edges in."""
        from .graph import graph_expand as _expand

        return _expand(self.conn, seeds, depth=depth, k=k, use_ppr=use_ppr,
                        extra_edges=extra_edges)

    def get(self, note_id: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT id,title,type,classification,zone,path,created,updated,sha256,body,"
            "is_latest_version,superseded_by,previous_version,superseded_date"
            " FROM notes WHERE id=?",
            (note_id,),
        ).fetchone()
        if not r:
            return None
        keys = ["id", "title", "type", "classification", "zone", "path",
                "created", "updated", "sha256", "body",
                "is_latest_version", "superseded_by", "previous_version", "superseded_date"]
        return dict(zip(keys, r))

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path,updated FROM notes "
            "ORDER BY updated DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ["id", "title", "classification", "zone", "path", "updated"]
        return [dict(zip(keys, r)) for r in rows]

    # -- maintenance: near-dup scan (G1) + unclassified lint --------------
    def near_dup(self, *, min_score: float = 0.95, k: int = 5) -> list[dict[str, Any]]:
        """Corpus-wide near-duplicate scan (brain-cli-gaps.md G1).

        Repoints the old SC-cosine integrity-scan §A directly onto the brain
        vector backend — no MCP round-trip, no raw pairwise O(n^2) python cosine
        matrix. For EVERY note, probe the backend ANN index with that note's own
        representative (first) chunk vector to NOMINATE its ``k`` nearest
        neighbours, then score each nominated pair by **true cosine** between the
        two notes' own vectors. Cost is O(n) batched embeds + O(n) backend
        searches (each sub-linear under sqlite-vec, linear-but-cheap under the
        brute-force fallback) — NOT the naive O(n^2) the gap doc flagged as the
        heavy path.

        BACKEND-INDEPENDENT THRESHOLD (the load-bearing detail): the backend's
        own ``search`` SCORE is metric-dependent — brute-force returns cosine,
        but sqlite-vec's ``vec0`` returns an L2-derived ``1/(1+d)`` similarity on
        a DIFFERENT scale (the same near-dup pair scores ~0.96 cosine vs ~0.79
        under vec0). A ``min_score`` like 0.95 would mean two different things
        across backends. So we use ``search`` ONLY to nominate candidates and
        recompute the actual pair score as cosine over the embedder vectors
        (which are L2-normalised), making ``min_score`` mean the same cosine
        threshold on EVERY backend.

        UNFILTERED (note-shaped, by id) — the CLI egress-gates BOTH members of
        every pair before surfacing (G1's explicit requirement)."""
        from .chunk import Chunk
        from .vectors import cosine

        c = self.conn
        rows = c.execute(
            "SELECT n.rowid, n.id, n.title, n.zone, c.heading, c.lang, c.text "
            "FROM notes n JOIN chunks c ON c.note_rowid = n.rowid AND c.ordinal = 0"
        ).fetchall()
        if len(rows) < 2:
            return []
        rowid_to_id = {int(r[0]): r[1] for r in rows}
        # Re-derive the EXACT representation each chunk was STORED with — the
        # contextual-prefix + chunk text ``embed_input`` (IDX-02) — so the probe
        # vector is apples-to-apples comparable to the stored passage vectors.
        # Probing with the bare chunk text alone (no prefix) under-measures
        # similarity by a wide margin (the prefix tokens dominate a short
        # chunk's bag-of-tokens) and uses symmetric is_query=False ("passage:")
        # encoding throughout — near-dup is a passage<->passage comparison, not
        # a query->passage retrieval, so the asymmetric query prefix a
        # search()-style probe would use is the wrong encoding here.
        texts = [
            Chunk(ordinal=0, heading=r[4] or "", text=r[6] or "", lang=r[5] or "en")
            .embed_input(r[2] or r[1], r[3] or "", "")
            for r in rows
        ]
        vecs = self.embedder.embed_batch(texts, is_query=False)
        vec_by_note: dict[int, list[float]] = {
            int(r[0]): v for r, v in zip(rows, vecs)
        }
        # chunk rowid (ordinal=0) -> owning note rowid, for mapping search hits back.
        chunk_to_note: dict[int, int] = {
            int(crid): int(nrid)
            for nrid, crid in c.execute(
                "SELECT note_rowid, rowid FROM chunks WHERE ordinal = 0"
            ).fetchall()
        }
        best: dict[tuple[str, str], float] = {}
        for (note_rowid, *_rest), vec in zip(rows, vecs):
            for hit_chunk_rowid, _backend_score in self.backend.search(c, vec, k + 1):
                other_rowid = chunk_to_note.get(int(hit_chunk_rowid))
                if other_rowid is None or other_rowid == note_rowid:
                    continue
                # Recompute the pair score as TRUE cosine (backend-independent).
                score = cosine(vec, vec_by_note[other_rowid])
                if score < min_score:
                    continue
                a, b = rowid_to_id[note_rowid], rowid_to_id[other_rowid]
                key = (a, b) if a < b else (b, a)
                if score > best.get(key, -1.0):
                    best[key] = score

        out: list[dict[str, Any]] = []
        for (a_id, b_id), score in best.items():
            a_row, b_row = self._note_row(self._rowid_of(a_id)), self._note_row(self._rowid_of(b_id))
            if not a_row or not b_row:
                continue
            out.append({
                "a": {"id": a_row["id"], "title": a_row["title"],
                      "classification": a_row["classification"], "zone": a_row["zone"],
                      "path": a_row["path"]},
                "b": {"id": b_row["id"], "title": b_row["title"],
                      "classification": b_row["classification"], "zone": b_row["zone"],
                      "path": b_row["path"]},
                "score": round(score, 6),
            })
        out.sort(key=lambda d: -d["score"])
        return out

    def stale_wikilink_targets(self) -> list[dict[str, Any]]:
        """Wikilinks whose target vanished or moved to ``archive/`` (AUT-02,
        curation Sunday fold). Reuses the ``graph`` module's derived graph —
        DISCOVERY-ONLY, UNFILTERED (the CLI egress-gates before surfacing)."""
        from .graph import stale_wikilink_targets as _stale

        return _stale(self.conn)

    def revisit_sample(self, *, today: Any = None, k: int = 10) -> list[dict[str, Any]]:
        """Staleness revisit sample ranked by age x whole-corpus PageRank
        centrality (AUT-02, curation Sunday fold). UNFILTERED — the CLI
        egress-gates before surfacing."""
        import datetime as _dt

        from .graph import revisit_sample as _revisit

        return _revisit(self.conn, today or _dt.date.today(), k=k)

    def unclassified_notes(self, *, k: int = 100) -> list[dict[str, Any]]:
        """Notes whose ``classification`` is missing/empty or not a recognised
        tier — the curation-lint default-deny finding (no wikilink-graph orphan
        detection here; that stays vault-overlay tooling, see G4 / task-disposition
        row 4). UNFILTERED — note-shaped for the CLI egress gate."""
        from . import classification as cls

        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path FROM notes ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            if cls.is_default_denied(r[2]):
                out.append({"id": r[0], "title": r[1], "classification": r[2],
                            "zone": r[3], "path": r[4]})
        return out[:k]

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
