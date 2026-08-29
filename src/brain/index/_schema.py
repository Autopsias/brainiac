"""Index schema methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SchemaMixin:
    """Index schema methods."""

    def _create_schema(self) -> None:
        self._refuse_accidental_hash_stamp()
        c = self.conn
        c.execute("DROP TABLE IF EXISTS aliases")
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
                is_latest_version TEXT, superseded_by TEXT, previous_version TEXT,
                title_norm TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE aliases (
                alias_norm TEXT NOT NULL,
                note_rowid INTEGER NOT NULL,
                PRIMARY KEY (alias_norm, note_rowid),
                FOREIGN KEY (note_rowid) REFERENCES notes(rowid)
            )"""
        )
        c.execute("CREATE INDEX idx_aliases_lookup ON aliases(alias_norm)")
        c.execute("CREATE INDEX idx_notes_title_norm ON notes(title_norm)")
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

    def _refuse_accidental_hash_stamp(self) -> None:
        """Refuse to stamp `hash-v1` when nobody asked for the hash embedder.

        WHY BEFORE THE DROPs: this is the FIRST statement of `_create_schema`,
        which starts by dropping every table. A guard that ran later would
        destroy the index it is meant to protect.

        Field cost (2026-08-26): the COS lane ran under a python with no
        onnxruntime, so auto-selection degraded to HashEmbedder. Opening the
        index made `model_matches()` False, that forced a rebuild, and the
        rebuild stamped `hash-v1` over a real bge-m3 index. Semantic search ran
        on effectively random vectors for 36 minutes, and the next maintain run
        saw the mismatch and spent 9h45m rebuilding 2,959 notes correctly.

        A misconfigured LANE is what caused it, so the fix cannot live only in
        that lane's environment — any future lane can be misconfigured the same
        way. The index refuses the stamp itself.

        The escape hatch is the EXPLICIT one that already exists:
        `BRAIN_EMBEDDER=hash` (tests, CI, offline work) constructs HashEmbedder
        through the explicit factory, which leaves `implicit_fallback` False.
        """
        if not getattr(self.embedder, "implicit_fallback", False):
            return
        raise EmbedderUnavailable(
            f"refusing to (re)build {self.db_path} with the non-semantic "
            "HashEmbedder: no real semantic embedder was available and none "
            "was explicitly requested, so writing 'hash-v1' would poison an "
            "index other lanes read and force a full rebuild to undo. Fix the "
            "environment (install onnxruntime + tokenizers, or point this lane "
            "at the engine venv), or set BRAIN_EMBEDDER=hash if a hash index "
            "is genuinely what you want."
        )

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

