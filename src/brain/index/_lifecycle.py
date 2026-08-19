"""Index rebuild lifecycle methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _LifecycleMixin:
    """Index rebuild lifecycle methods."""

    def _staging_paths(self) -> tuple[Path, Path]:
        """Stable (no-pid) staging DB path + its advisory JSON manifest path.

        RB-02: the pid-suffixed name from s03 let a killed rebuild's staging
        DB survive on disk, but no SUCCESSOR process could ever find it (its
        own pid differs). A stable name is required for resume to locate
        prior work."""
        tmp_path = self.db_path.with_name(self.db_path.name + ".rebuild.tmp")
        manifest_path = self.db_path.with_name(self.db_path.name + ".rebuild.tmp.manifest.json")
        return tmp_path, manifest_path

    @staticmethod
    def _vault_fingerprint(plans: "list[_NotePlan]") -> str:
        """Cheap content fingerprint of the FULL planned note set.

        Used to detect "the vault changed between the killed attempt and this
        one" -- in which case resume must NOT continue (a stale partial next
        to a changed vault could skip notes that changed, or miss deletes).
        Built from (path, content_hash) pairs, which ``_plan_note`` already
        computed for every note as part of planning -- no extra scan."""
        return _LifecycleMixin._vault_fingerprint_projection(
            (p.row["path"], p.row["content_hash"]) for p in plans
        )

    @staticmethod
    def _vault_fingerprint_projection(
        pairs: "Iterable[tuple[str, str]]",
    ) -> str:
        """Hash the final sorted ``(path, content_hash)`` index projection.

        Rebuild already derives its resumability fingerprint from this exact
        projection.  Incremental sync uses the same helper *inside its write
        transaction*, so query replay can distinguish a genuinely unchanged
        vault from a ranking/configuration change without mistaking a VM
        snapshot generation or wall-clock timestamp for content identity.
        """
        parts = sorted(f"{path}\x00{content_hash or ''}" for path, content_hash in pairs)
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()

    def rebuild(self, vault: Path, *, json_mode: bool = False) -> dict[str, Any]:
        """Rebuild the entire index from vault/ — crash-safe AND resumable.

        Builds into a TEMP db file and atomically swaps it into place on
        success (same posture as ``snapshot.publish_snapshot``), so a killed
        rebuild leaves the prior live index untouched. Before the s03 fix,
        ``_create_schema`` dropped the live tables FIRST and inserts landed at
        the end — a kill mid-rebuild left the live index wiped (field report
        2026-07-16: a killed rebuild on a 2,312-note vault left 0 notes / 0
        chunks).

        RB-02 (docs/adr/0007): a killed rebuild's staging DB is now a
        deliberate, named, resumable artifact instead of being unlinked on
        every non-success path. See ``_try_resume`` / ``_rebuild_impl`` /
        ``_finalize_rebuild`` for the resume + durability protocol. The
        invariant that matters is unchanged and, if anything, stronger: the
        LIVE index is NEVER replaced by anything but a fully-committed,
        fully-checkpointed staging DB.

        In-memory DBs (``:memory:``, test-only) build in place — there is
        nothing to swap, and therefore nothing to resume."""
        self._title_phrase_records_cache = None
        self._identity_owner_cache.clear()
        self._title_phrase_match_cache.clear()
        self._literal_text_cache.clear()
        if self.db_path == Path(":memory:"):
            result = self._rebuild_impl(vault, json_mode=json_mode)
            result["languages"] = self._refresh_language_census()
            return result

        tmp_path, manifest_path = self._staging_paths()
        orig_db_path = self.db_path

        resume_state = self._try_resume(tmp_path, manifest_path)

        self.close()
        self.db_path = tmp_path
        try:
            # NOTE: this path is taken even when ``resume_state.finished`` is
            # already true (all batches committed by a prior attempt, but the
            # checkpoint+swap never happened -- most likely
            # wal_checkpoint(TRUNCATE) was blocked by a reader, item 1/ruling
            # 4). ``_rebuild_impl`` re-validates the vault fingerprint either
            # way and, when it still matches, its batch loop skips every
            # already-committed batch (``resume_from_batch`` covers all of
            # them) -- so this is a cheap no-op re-plan, not a re-embed, and
            # we still get fresh re-validation instead of trusting a stale
            # "finished" marker against a vault that may have changed since.
            result = self._rebuild_impl(vault, resume=resume_state, json_mode=json_mode)
            self._finalize_rebuild(tmp_path, manifest_path, orig_db_path)
        except BaseException:
            preserve = self._partial_is_resumable(tmp_path)
            self.close()
            self.db_path = orig_db_path
            if not preserve:
                self._discard_staging(tmp_path, manifest_path)
            raise
        self.close()
        self.db_path = orig_db_path
        # The just-replaced live DB has no matching sidecars of its own yet
        # (the temp ones were checkpointed away in ``_finalize_rebuild``) —
        # drop any stale leftovers from the PRIOR live DB so the next open
        # starts clean.
        for suffix in ("-wal", "-shm"):
            Path(str(orig_db_path) + suffix).unlink(missing_ok=True)
        result["db"] = str(self.db_path)
        result["languages"] = self._refresh_language_census()
        return result

    def _discard_staging(self, tmp_path: Path, manifest_path: Path) -> None:
        for p in (tmp_path, Path(str(tmp_path) + "-wal"), Path(str(tmp_path) + "-shm"), manifest_path):
            p.unlink(missing_ok=True)

    def _partial_is_resumable(self, tmp_path: Path) -> bool:
        """Best-effort check (item 4/ADR ruling 6): preserve a killed staging
        DB only when it is a genuine, committed, in-progress partial — never
        when validation of what IS there is uncertain (atomic-discard, item
        7/ruling 7: when in doubt, throw it away)."""
        if not tmp_path.is_file():
            return False
        try:
            conn = sqlite3.connect(str(tmp_path))
            try:
                conn.execute("PRAGMA journal_mode=WAL")  # forces WAL recovery on open
                row = conn.execute(
                    "SELECT v FROM meta WHERE k='finished'"
                ).fetchone()
                # Preserve BOTH a mid-progress partial (finished=false) and a
                # fully-committed-but-unswapped one (finished=true, e.g. the
                # checkpoint was blocked by a reader) -- both are legitimate
                # resumable staging artifacts, never a leak.
                if row is None or row[0] not in ("false", "true"):
                    return False
                batches = conn.execute(
                    "SELECT v FROM meta WHERE k='committed_batches'"
                ).fetchone()
                return bool(batches) and int(batches[0]) >= 1
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def _try_resume(self, tmp_path: Path, manifest_path: Path) -> "_ResumeState | None":
        """Validate a surviving staging DB against EVERY persisted invariant
        (item 2/ruling 5) and, only on a full match, return the state needed
        to continue it. Any mismatch, or any uncertainty at all, discards the
        partial and returns ``None`` -- a clean rebuild from scratch (item
        2/ruling 7: prefer atomic discard over surgical repair)."""
        if not tmp_path.is_file():
            self._discard_staging(tmp_path, manifest_path)
            return None
        # Cheap pre-flight via the advisory manifest, BEFORE opening the DB.
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._discard_staging(tmp_path, manifest_path)
            return None
        expected = {
            "schema_version": str(SCHEMA_VERSION),
            "index_format_version": str(INDEX_FORMAT_VERSION),
            "vector_backend": self.backend.name,
            "embed_model": self.embedder.model_id,
            "embed_dim": str(self.embedder.dim),
        }
        if any(manifest.get(k) != v for k, v in expected.items()):
            self._discard_staging(tmp_path, manifest_path)
            return None
        # Authoritative check: the meta table INSIDE the staging DB, reopened
        # through SQLite's own WAL recovery (item 3/ruling 3) before we trust
        # anything it reports.
        try:
            conn = sqlite3.connect(str(tmp_path))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
                finished_marker = meta.get("finished")
                if finished_marker not in ("false", "true"):
                    self._discard_staging(tmp_path, manifest_path)
                    return None
                if any(meta.get(k) != v for k, v in expected.items()):
                    self._discard_staging(tmp_path, manifest_path)
                    return None
                committed_batches = int(meta.get("committed_batches", "0") or "0")
                if committed_batches < 1:
                    self._discard_staging(tmp_path, manifest_path)
                    return None
                committed_notes = int(
                    conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                )
                max_chunk_rowid = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM chunks"
                ).fetchone()[0]
                vault_fingerprint = meta.get("vault_fingerprint")
                notes_per_batch = meta.get("notes_per_batch")
            finally:
                conn.close()
        except sqlite3.Error:
            self._discard_staging(tmp_path, manifest_path)
            return None
        if not vault_fingerprint or not notes_per_batch:
            self._discard_staging(tmp_path, manifest_path)
            return None
        return _ResumeState(
            committed_batches=committed_batches,
            committed_notes=committed_notes,
            start_chunk_rowid=int(max_chunk_rowid) + 1,
            vault_fingerprint=vault_fingerprint,
            notes_per_batch=int(notes_per_batch),
            finished=(finished_marker == "true"),
        )

    def _finalize_rebuild(self, tmp_path: Path, manifest_path: Path, orig_db_path: Path) -> None:
        """The durability protocol (item 1/ruling 4), IN THIS EXACT ORDER:

        commit final batch + meta row (done inside ``_rebuild_impl``'s last
        batch write) -> reopen + validate the meta row -> checkpoint the WAL
        -> CONSUME the checkpoint result and refuse the swap unless it fully
        truncated -> fsync the staging DB -> fsync its parent dir ->
        ``os.replace`` -> fsync the parent dir again."""
        self.close()
        conn = sqlite3.connect(str(tmp_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # reopen through WAL recovery
            row = conn.execute("SELECT v FROM meta WHERE k='finished'").fetchone()
            if row is None or row[0] != "true":
                raise RuntimeError(
                    "rebuild refused to swap: staging DB meta row does not say "
                    "finished=true (a genuinely-finished rebuild always sets it "
                    "in the same transaction as its final batch)"
                )
            busy, log_frames, checkpointed_frames = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if busy or log_frames or checkpointed_frames < 0:
                raise RuntimeError(
                    f"rebuild refused to swap: wal_checkpoint(TRUNCATE) did not "
                    f"fully truncate (busy={busy}, log_frames={log_frames}) -- a "
                    f"reader is very likely still holding a transaction open "
                    f"against the staging DB; the final committed pages would "
                    f"still live only in its WAL, so promoting it now could "
                    f"install a silently-incomplete index"
                )
        finally:
            conn.close()
        # O_RDWR, not O_RDONLY: Windows' FlushFileBuffers requires a handle
        # opened for WRITE and fails the whole rebuild with [Errno 9] on a
        # read-only one, after every note has already been indexed.
        fd = os.open(str(tmp_path), os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name == "posix":
            dir_fd = os.open(str(tmp_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
                os.replace(tmp_path, orig_db_path)
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        else:
            # ponytail: Windows cannot open a directory as a file descriptor,
            # so the rename record cannot be fsynced. os.replace is still
            # atomic on NTFS -- only the post-crash durability window for the
            # rename itself widens, never the index contents (fsynced above).
            os.replace(tmp_path, orig_db_path)
        for p in (Path(str(tmp_path) + "-wal"), Path(str(tmp_path) + "-shm"), manifest_path):
            p.unlink(missing_ok=True)

    def _rebuild_impl(
        self, vault: Path, *, resume: "_ResumeState | None" = None,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """Run the resumable three-pass rebuild through its stage module."""
        from ..index_stages.rebuild import rebuild_index

        return rebuild_index(
            self,
            vault,
            resume=resume,
            json_mode=json_mode,
            schema_version=SCHEMA_VERSION,
            format_version=INDEX_FORMAT_VERSION,
            retry=self._write_retry(),
        )

    def sync(self, vault: Path, *, json_mode: bool = False) -> dict[str, Any]:
        """Incrementally reconcile the index through one CC-01 transaction."""
        from ..index_stages.sync import sync_index

        return sync_index(
            self, vault, json_mode=json_mode, retry=self._write_retry()
        )

