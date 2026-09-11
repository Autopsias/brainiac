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
                title_norm TEXT NOT NULL,
                concealment TEXT,
                frontmatter TEXT
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
        self._concealment_column = True  # created above, no PRAGMA needed
        self._frontmatter_column = True  # ditto (PV-01)

    #: The one place the M-3b retrieval verdict's column name is written.
    CONCEALMENT_COL = "concealment"

    def _concealment_sql(self) -> str:
        """``concealment`` if this index carries the column, else ``''``.

        Every read of the verdict interpolates this ONE fragment, so an index
        that predates the column degrades to an empty string — which
        ``injection_fold.retrieval_verdict``'s vocabulary reads as
        ``unknown`` — instead of raising ``no such column`` out of ``get``.

        WHY A COLUMN AND NOT A SIDE TABLE. ``rebuild`` builds into a temp
        database and ``os.replace``s the whole FILE into position
        (``_lifecycle.rebuild``), so anything created outside
        :meth:`_create_schema` is destroyed on the next rebuild, silently. The
        column is IN the schema definition above, so it is rebuilt with it.

        WHY NOT A ``SCHEMA_VERSION`` BUMP. A bump makes ``_schema_ready``
        false, which escalates the next ``sync`` into a full re-index and
        re-embed of every registered vault — hours per vault, for one nullable
        column. Instead an existing v4 database is migrated in place by an
        idempotent ``ALTER TABLE ADD COLUMN``, which SQLite does as a metadata
        write. Existing rows get NULL and read ``unknown`` until the note is
        re-ingested; detection is forward-only, so that is the truth.

        A READ-ONLY connection (the VM leg's published snapshot) cannot ALTER.
        It gets the empty-string fragment and reports ``unknown`` — degraded,
        never broken — until the host republishes a snapshot built from a
        migrated index.
        """
        if self._concealment_column is None:
            cols = self._notes_columns()
            if not cols:
                # No `notes` table yet (a fresh file). Nothing to migrate and
                # nothing to cache — `_create_schema` sets the flag itself.
                return "''"
            if self.CONCEALMENT_COL not in cols and not self.read_only:
                try:
                    self.conn.execute(
                        f"ALTER TABLE notes ADD COLUMN {self.CONCEALMENT_COL} TEXT")
                    cols.add(self.CONCEALMENT_COL)
                except sqlite3.OperationalError:
                    # The ALTER lost. Re-READ rather than believe the exception:
                    # a concurrent migrator winning the race and a `database is
                    # locked` are the same error class, and only the PRAGMA can
                    # tell them apart (adversarial review B2, 2026-09-04).
                    cols = self._notes_columns()
                    if self.CONCEALMENT_COL not in cols:
                        # NOT cached. Caching `False` here made one transient
                        # lock permanent for the life of a long-running host
                        # process: every later read returned a false `unknown`.
                        # Un-cached, the next call retries the migration.
                        # `_write_planned` reads this same fragment and DROPS
                        # the column from its INSERT when it comes back `''`
                        # (C5, 2026-09-04) — until it did, a lost ALTER
                        # degraded the read and then aborted the next write
                        # with `no such column`.
                        return "''"
            self._concealment_column = self.CONCEALMENT_COL in cols
        return self.CONCEALMENT_COL if self._concealment_column else "''"

    #: The one place the PV-01 frontmatter column name is written.
    FRONTMATTER_COL = "frontmatter"

    def _frontmatter_sql(self) -> str:
        """``frontmatter`` if this index carries the column, else ``''``.

        Same degradation shape as :meth:`_concealment_sql`, and for the same
        reason — one interpolated fragment, two module literals, so a read
        against an index that predates the column returns an empty value
        instead of raising ``no such column`` out of ``get``.

        It does NOT migrate in place, and that is the difference. The column
        holds the parsed frontmatter, so an ``ALTER TABLE ADD COLUMN`` would
        leave every existing row NULL — a filter on ``provenance.sender``
        would then return nothing and read exactly like an answer. Filling it
        means re-reading every note, which is a rebuild. So PV-01 bumps
        :data:`SCHEMA_VERSION` instead and lets ``sync`` self-delegate to
        ``rebuild`` (``index_stages/sync._fallback_rebuild``).
        """
        if self._frontmatter_column is None:
            cols = self._notes_columns()
            if not cols:
                return "''"
            self._frontmatter_column = self.FRONTMATTER_COL in cols
        return self.FRONTMATTER_COL if self._frontmatter_column else "''"

    def _index_vault_root(self) -> str | None:
        """The vault root THIS INDEX was built from (meta key ``vault_root``,
        written by rebuild/sync), cached per connection. ``None`` on a
        pre-migration index that predates the write."""
        if not self._vault_root_meta_loaded:
            self._vault_root_meta = self.get_meta("vault_root")
            self._vault_root_meta_loaded = True
        return self._vault_root_meta

    def _vault_path(self, abs_path: str | None) -> str:
        """``abs_path`` relative to the vault root (SF-02) — LEXICAL string
        prefix-stripping against the root this index was built from, never
        against the reader's own environment. That distinction is the whole
        point: the Cowork VM reads a host-built snapshot through a mount path
        that differs from the host's own vault root, so resolving against
        ``config.vault_root()`` (the reader's env) returned the untouched
        host path on the one surface SF-02 exists for. Falls back to the
        env-based :func:`config.vault_relative_path` only when this index
        carries no stored root (built before this change). Never raises."""
        if not abs_path:
            return ""
        root = self._index_vault_root()
        if not root:
            return config.vault_relative_path(abs_path)
        s = str(abs_path)
        prefix = root if root.endswith("/") else root + "/"
        return s[len(prefix):] if s.startswith(prefix) else s

    def _notes_columns(self) -> set[str]:
        """Column names on ``notes``; empty if the table does not exist yet."""
        return {r[1] for r in self.conn.execute("PRAGMA table_info(notes)")}

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
        if k == "vault_root":
            self._vault_root_meta_loaded = False  # sync/rebuild changed it

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

