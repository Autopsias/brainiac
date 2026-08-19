"""Index schema methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SchemaMixin:
    """Index schema methods."""

    def _create_schema(self) -> None:
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

