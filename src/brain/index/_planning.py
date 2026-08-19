"""Index note planning methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _PlanningMixin:
    """Index note planning methods."""

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
        # ADR-0008: aliases are owner-curated identity metadata on brain notes
        # only. Validation rejects malformed values; indexing stays defensive so
        # a malformed foreign note cannot crash a rebuild or manufacture a raw
        # source identity.
        raw_aliases = note.meta.get("aliases") if note.zone == "brain" else []
        row["aliases"] = [
            alias for alias in raw_aliases
            if isinstance(raw_aliases, list) and isinstance(alias, str)
            and alias.strip() and normalize_identity(alias)
        ] if isinstance(raw_aliases, list) else []
        row["title_norm"] = normalize_identity(row["title"])
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
                row["id"] = f"{row['id']}__{hashlib.sha1(row['path'].encode()).hexdigest()[:8]}"  # nosec B303 B324  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
            seen.add(row["id"])
        chunks = chunk_text(note.body)
        if not chunks:
            # A note with an empty body still gets one chunk (the title) so it is
            # retrievable semantically.
            from ..chunk import Chunk, detect_language

            chunks = [Chunk(0, "", note.title or note.id, detect_language(note.title))]
        # UPG-04 Contextual Retrieval: generate a per-note doc-context once and
        # prepend it to every chunk. Inert (returns "") when no LLM is configured
        # ($BRAIN_CONTEXTUAL_LLM unset) — degrades cleanly to the S10 path.
        from ..context import doc_context as _doc_context

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
            " previous_version, title_norm) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan.note_rowid, row["id"], row["title"], row["type"], row["classification"],
                row["zone"], row["path"], row["created"], row["updated"],
                row["sha256"], row["content_hash"], row["body"],
                row.get("document_date", ""), row.get("effective_date", ""),
                row.get("superseded_date", ""), row.get("is_latest_version", ""),
                row.get("superseded_by", ""), row.get("previous_version", ""),
                row.get("title_norm", ""),
            ),
        )
        # Projection rows are written in the same transaction as the note,
        # title, chunks, and vectors. Duplicate aliases within one note are a
        # validator error, but INSERT OR IGNORE keeps a foreign malformed note
        # from taking down an otherwise recoverable rebuild.
        for alias in row.get("aliases", []):
            c.execute(
                "INSERT OR IGNORE INTO aliases(alias_norm, note_rowid) VALUES (?,?)",
                (normalize_identity(alias), plan.note_rowid),
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
        # Explicit lifecycle deletion is intentional: FK cascades may be off on
        # a caller's SQLite connection, and stale identity owners are unsafe.
        c.execute("DELETE FROM aliases WHERE note_rowid=?", (note_rowid,))
        c.execute("DELETE FROM notes WHERE rowid=?", (note_rowid,))

