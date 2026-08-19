"""Capture lifecycle methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    KeyUnavailable,
    Path,
    _contained_in,
    _stamp_draft_frontmatter,
    classification,
    config,
    frontmatter,
    os,
    safe_slug,
    sha256_text,
    vault_writer_lock,
)
from ._durability import (
    _write_note_durable,
)


class _CoreCaptureMixin:
    """Capture lifecycle methods for BrainCore."""

    def capture_inbox_dir(self) -> Path:
        return config.capture_inbox_dir(self.vault)
    def draft_capture(
        self, content: str, *, ident: str | None = None, is_source: bool = False
    ) -> dict[str, Any]:
        """Stage a candidate note as a plain DRAFT — the ONE write a VM leg may do.

        This is the VM-side capture verb (AGENTS.md §5/§6). It writes a plain
        Markdown file into the writable ``capture-inbox/`` on the shared mount and
        stamps ``status: draft`` + ``provenance.trust: untrusted``. It NEVER:
        signs the audit chain, opens the index, writes WAL, or resolves a signing
        key. The draft is NOT authoritative and is NOT surfaced by ``search``
        until the HOST drains it (drain-on-invoke -> sign + index + snapshot).

        Available on BOTH legs (host + VM) — it is the only quasi-write a VM holds.
        """
        meta, _body = frontmatter.parse_text(content)
        note_id = ident or (str(meta.get("id")) if meta and meta.get("id") else None)
        if not note_id:
            # deterministic fallback id from content hash
            note_id = "draft-" + sha256_text(content)[:12]
        # C-1 trust boundary: the id comes from --id or untrusted YAML and
        # becomes a path — refuse anything but a bare slug (fail closed).
        note_id = safe_slug(note_id)
        staged = _stamp_draft_frontmatter(content, note_id, is_source)
        inbox = self.capture_inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / f"{note_id}.md"
        # Belt over the slug check: the resolved target (symlinks followed)
        # must stay inside the inbox.
        if not _contained_in(target, inbox):
            raise ValueError(f"draft target escapes capture inbox: {note_id!r}")
        target.write_text(staged, encoding="utf-8")
        return {
            "draft": str(target),
            "id": note_id,
            "signed": False,
            "indexed": False,
            "authoritative": False,
            "note": "draft staged; host drain-on-invoke will sign + index + snapshot",
        }
    def rebuild(self, *, json_mode: bool = False) -> dict[str, Any]:
        self._require_host("rebuild the index")
        with vault_writer_lock(self.vault, verb="rebuild"):
            res = self.index.rebuild(self.vault, json_mode=json_mode)
        # INT-01 durability: the index dir is a disposable cache EXCEPT for the
        # approved queue, which is the only copy of owner-accepted content until
        # it is signed. Rebuild guidance ("just delete it and rebuild") is
        # exactly the habit that would destroy it, so never let it be silent.
        try:
            from .. import cos as _cos_q

            waiting = len(_cos_q.approved_pending(self.vault))
            # INT-04: the SECOND non-disposable item in this dir. An armed
            # acceptance anchor is the only thing holding its inbox file at the
            # email-derived MNPI floor; lose it and the file ingests at
            # `Internal`, silently. Same warning surface, same reason.
            anchors = _cos_q.attachment_anchors_awaiting_drain(self.vault)
        except Exception:  # noqa: BLE001 — never fail a rebuild on the check
            waiting = anchors = 0
        if waiting or anchors:
            # A `progress_note` here was a TTY-gated whisper: a headless
            # launchd rebuild — the exact context that would then delete the
            # dir — saw nothing at all. Put it in the RESULT, where every
            # caller (JSON, human, scheduled) actually reads it.
            parts = []
            if waiting:
                res["approved_awaiting_signature"] = waiting
                parts.append(f"{waiting} owner-approved item(s) wait in "
                             f"{_cos_q.approved_queue_dir(self.vault)}")
            if anchors:
                res["attachment_anchors_awaiting_drain"] = anchors
                parts.append(f"{anchors} accepted-attachment anchor(s) wait in "
                             f"{_cos_q.attachment_anchor_dir(self.vault)}")
            res["warning"] = (
                " and ".join(parts) + " — NOT rebuildable from vault/. Run "
                "`brain sync` to drain them before deleting or repointing the "
                "index dir.")
            from ..progress import progress_note

            progress_note("WARNING: " + res["warning"], json_mode=json_mode,
                          verb="rebuild")
        return res
    def embedder_pending(self) -> bool:
        """True when the index's stored dense vectors were built with a
        DIFFERENT embedder than the one the live runtime would use now (S02/
        CS-01) — e.g. a cold-start install built the index with the offline
        ``hash`` placeholder to avoid a network model download. Read-only,
        cheap (no download): :meth:`BrainIndex.model_matches` only compares
        recorded meta strings against the constructed (not yet loaded)
        embedder's ``model_id``/``dim``."""
        return not self.index.model_matches()
    def warmup(self, *, json_mode: bool = False) -> dict[str, Any]:
        """HOST-ONLY (S02/CS-01): resolve + download the live auto-embedder's
        model weights now, instead of on the first real semantic search.

        huggingface_hub prints its own progress bar to stderr during the
        download (never stdout — keeps ``--json`` output parseable) and
        already file-locks the blob it is writing
        (``huggingface_hub.file_download.WeakFileLock``), so a concurrent
        warmup / first-search / nightly-maintenance embed racing on the same
        cache directory cannot corrupt it — see the closeout note; no extra
        locking is added here.

        Does NOT rebuild the index. If the index was built with a placeholder
        embedder (``embedder_pending()`` was True), run `brain sync` (or
        `brain rebuild`) afterward — `BrainIndex.sync`'s existing model-
        mismatch guard will do a full, now-offline (model already cached)
        re-embed automatically."""
        self._require_host("warm up the embedding model (download)")
        import os
        import time

        from ..embed import get_embedder, model_cache_ready
        from ..progress import progress_note

        embedder = get_embedder(os.environ.get("BRAIN_EMBEDDER", "auto"))
        was_cached = model_cache_ready(embedder)
        # OB-02: begin/end lines only -- hf_hub prints its own download bar to
        # stderr during the load below (core.py, embed.py), so we narrate
        # start/finish around it rather than duplicating its per-file progress.
        progress_note(f"warmup: resolving {embedder.model_id}"
                       f"{' (cached)' if was_cached else ' (downloading)'}...",
                       json_mode=json_mode, verb="warmup")
        t0 = time.monotonic()
        embedder.embed("warmup")  # triggers the real load/download if needed
        elapsed = time.monotonic() - t0
        # The RERANKER too, since 0.20.1: reranking is default-on (BR-03), and
        # a search is no longer allowed to download its weights mid-query — it
        # degrades to the unreranked order instead. Warmup is now the one place
        # those weights are fetched, so warming only the embedder would leave a
        # user permanently unreranked without ever saying why.
        from ..rerank import warm_reranker_weights

        rerank_info: dict[str, Any]
        try:
            progress_note("warmup: resolving the reranker...",
                          json_mode=json_mode, verb="warmup")
            rerank_info = dict(warm_reranker_weights())
        except Exception as exc:
            # Never fail the embedder warm because the optional precision
            # booster could not be fetched — report it and move on.
            rerank_info = {"downloaded": False, "cached": False,
                           "error": f"{type(exc).__name__}: {exc}"}
        progress_note(f"warmup: ready in {elapsed:.1f}s", json_mode=json_mode, verb="warmup")
        return {
            "model_id": embedder.model_id,
            "already_cached": bool(was_cached),
            "elapsed_s": round(elapsed, 2),
            "reranker": rerank_info,
        }
    def drafts_dir(self) -> Path:
        return self.vault / ".brain" / "drafts"
    def _draft_sources(self) -> list[Path]:
        """Both draft drop locations, drained on the host: the legacy
        ``.brain/drafts/`` and the VM-facing ``capture-inbox/``."""
        dirs = [self.drafts_dir(), self.capture_inbox_dir()]
        seen: set[str] = set()
        out: list[Path] = []
        for d in dirs:
            key = str(d.resolve()) if d.exists() else str(d)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out
    def _drain_sources(self) -> tuple[list[tuple[Path, bool, Any]],
                                      list[dict[str, str]]]:
        """``[(dir, is_approved_queue, pubkey)]`` in drain order, plus refusals.

        The broker's HOST-ONLY approved queue is drained FIRST, deliberately: a
        VM draft-capture under the same id would otherwise be signed ahead of
        the bytes the owner approved, and the owner's copy would then lose to
        the duplicate-id guard.

        The verification key is resolved ONCE, here. If it cannot be resolved —
        locked keychain, scheduler running as another user, missing
        ``cryptography``, a rotated key — the queue is not drained AT ALL and
        says ``no-signing-key (fail-closed)``, exactly like the ordinary draft
        path. Verifying per item instead would turn every key outage into a
        pile of security-worded refusals over perfectly good owner-approved
        work."""
        from .. import cos as _cos_mod

        out: list[tuple[Path, bool, Any]] = []
        refusals: list[dict[str, str]] = []
        try:
            queue = _cos_mod.approved_queue_root(self.vault)
            out.append((queue, True, _cos_mod.approved_verify_key(self.vault)))
        except _cos_mod.ApprovedQueueUnsafe as exc:
            refusals.append({"draft": "(approved queue)", "source": "approved-queue",
                             "reason": f"not drained (fail-closed): {exc}"})
        except _cos_mod.ApprovedKeyUnavailable as exc:
            refusals.append({"draft": "(approved queue)", "source": "approved-queue",
                             "reason": f"no-signing-key (fail-closed): the approved "
                                       f"queue was left untouched ({exc})"})
        out += [(d, False, None) for d in self._draft_sources()]
        return out, refusals
    def ingest_dropzone(self, *, dry_run: bool = False) -> dict[str, Any]:
        """HOST-only: drain ``<vault>/inbox/`` (ADR-0003 Ruling 1 / ING-01).

        Refused on the VM leg BEFORE any filesystem side effect (no key
        lookup, no processing-dir claim, no archive/WAL write) — the same
        fail-closed shape as ``drain_drafts``/``write_note``. Idempotent and
        cheap when the inbox is empty or absent (a directory listing)."""
        self._require_host("ingest the drop zone")
        from ..ingest.pipeline import run_ingest

        return run_ingest(self, dry_run=dry_run)
    def ingest_transcript(
        self, path: str | Path, *, origin: str, language: str | None = None,
        document_date: str | None = None, classification: str = "Internal",
    ) -> dict[str, Any]:
        """HOST-only: promote one transcript ``.md`` file into ``vault/raw/``
        with explicit provenance (ADR-0003 Ruling 1 companion / ING-04).

        ``origin`` is the source audio/video file path, or the literal
        string ``"verbal"`` — the generic drop-zone (``ingest_dropzone``)
        cannot express this fact on its own (its own ``origin`` always points
        at an archived COPY of the dropped file). Refused on the VM leg
        BEFORE any filesystem side effect, same fail-closed shape as
        ``ingest_dropzone``/``write_note``."""
        self._require_host("ingest a transcript")
        from ..ingest.transcript import ingest_transcript as _ingest_transcript

        return _ingest_transcript(
            self, path, origin=origin, language=language,
            document_date=document_date, classification=classification,
        )
    def sync(self, *, drain: bool = True, publish: bool = False,
             json_mode: bool = False) -> dict[str, Any]:
        """Incremental index reconcile (IDX-03), draining capture drafts AND
        the ingestion drop zone first.

        HOST-broker only (it mutates the index). ``drain`` runs the host capture
        drain + inbox ingest drain before reconciling (ADR-0003 Ruling 1
        amendment: the ingest drain fires on every host ``sync``, not only the
        nightly `maintain` floor); ``publish`` additionally republishes the
        read-only snapshot so a VM session's next read sees the just-committed
        note (closing the capture loop). Set ``drain=False`` only for a host
        read-only reconcile."""
        self._require_host("sync (mutate) the index")
        with vault_writer_lock(self.vault, verb="sync"):
            drain_res = self.drain_drafts() if drain else {"promoted": 0, "skipped": 0, "drain": "off"}
            if drain:
                try:
                    ingest_res = self.ingest_dropzone()
                except Exception as exc:
                    # C2: run_ingest's own per-file retry/quarantine machinery
                    # isolates a single poison file WITHOUT raising, but this is
                    # the last-resort backstop for anything that still escapes it
                    # (e.g. a manifest/failures-file I/O error). ingest_dropzone
                    # ran BEFORE index.sync with no try/except, so any escaping
                    # exception aborted index reconciliation and snapshot
                    # publication on every subsequent sync — one bad drop must
                    # never abort index maintenance.
                    ingest_res = {"processed": [], "error": f"{type(exc).__name__}: {exc}"}
            else:
                ingest_res = {"processed": [], "reason": "drain-off"}
            idx_res = self.index.sync(self.vault, json_mode=json_mode)
            idx_res["drain"] = drain_res
            idx_res["ingest"] = ingest_res
            if publish:
                idx_res["snapshot"] = self.publish_snapshot()
            return idx_res
    def publish_snapshot(self, dest: str | Path | None = None) -> dict[str, Any]:
        """Publish a read-only, generation-stamped snapshot of the authoritative
        host index (atomic). The VM mounts this read-only; it never writes the
        authoritative DB. HOST-broker only."""
        self._require_host("publish a snapshot")
        from ..snapshot import publish_snapshot as _publish

        dest_dir = Path(dest) if dest else config.snapshot_dir(self.vault)
        with vault_writer_lock(self.vault, verb="snapshot"):
            return _publish(self.index.db_path, dest_dir).to_dict()
    def restore_index_from_snapshot(
        self, *, force: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Fast index recovery: replace the live index with the published snapshot.

        The snapshot is a complete, read-consistent copy of the authoritative
        index, so restoring from it is O(seconds) — the safe alternative to a
        full re-embed ``rebuild`` when the live index is corrupt or empty (e.g.
        an interrupted rebuild left a half-written DB). HOST-broker only.

        Guards: refuses a missing/empty/unreadable snapshot; refuses to clobber a
        live index that holds MORE notes than the snapshot (the snapshot is
        older — ``sync``/``rebuild`` instead) unless ``force``; backs up the
        current index (reversible ``.pre-restore-*.bak``) before overwriting; and
        verifies the note count post-restore.
        """
        self._require_host("restore the index from a snapshot")
        with vault_writer_lock(self.vault, verb="restore-index"):
            return self._restore_index_from_snapshot_locked(force=force, dry_run=dry_run)
    def _restore_index_from_snapshot_locked(
        self, *, force: bool, dry_run: bool
    ) -> dict[str, Any]:
        import datetime as _dt
        import shutil as _sh
        import sqlite3 as _sq

        idx = config.index_path(self.vault)
        snap = config.snapshot_db_path(self.vault)

        def _count(p: Path):
            if not p.exists():
                return None  # absent
            try:
                c = _sq.connect(f"file:{p}?mode=ro", uri=True)
                try:
                    return int(c.execute("SELECT count(*) FROM notes").fetchone()[0])
                finally:
                    c.close()
            except Exception:
                return -1  # present but unreadable/corrupt

        snap_n = _count(snap)
        if snap_n is None:
            raise FileNotFoundError(f"no snapshot to restore from: {snap}")
        if snap_n <= 0:
            raise ValueError(
                f"snapshot has {snap_n} notes — refusing to restore an empty/corrupt "
                f"snapshot ({snap})")
        live_n = _count(idx)

        if live_n is not None and live_n > snap_n and not force:
            raise ValueError(
                f"live index has {live_n} notes but the snapshot has only {snap_n} — "
                f"restoring would LOSE {live_n - snap_n} note(s). The snapshot is older; "
                f"run `brain sync`/`rebuild` instead, or pass --force to override.")

        plan: dict[str, Any] = {
            "index": str(idx), "snapshot": str(snap),
            "snapshot_notes": snap_n, "live_notes_before": live_n,
        }
        if dry_run:
            plan["dry_run"] = True
            return plan

        config.ensure_index_dir(self.vault)
        backup = None
        if idx.exists():
            stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            backup = idx.with_name(idx.name + f".pre-restore-{stamp}.bak")
            _sh.move(str(idx), str(backup))
        for suf in ("-wal", "-shm"):  # stale sqlite sidecars would mask the copy
            side = idx.with_name(idx.name + suf)
            if side.exists():
                side.unlink()
        _sh.copy2(str(snap), str(idx))

        live_after = _count(idx)
        if live_after != snap_n:
            raise RuntimeError(
                f"post-restore verification failed: index has {live_after} notes, "
                f"expected {snap_n} (backup preserved at {backup})")
        plan.update({"restored": True, "live_notes_after": live_after,
                     "backup": str(backup) if backup else None})
        return plan
    def _count_pending_drafts(self) -> int:
        """Everything waiting for the drain — the stalled-drain tripwire reads
        this. It must include the approved queue (INT-01): owner-approved,
        unsigned content is precisely what a stalled drain must not hide."""
        n = 0
        for ddir in self._draft_sources():
            if ddir.is_dir():
                n += len(list(ddir.glob("*.md")))
        if self.role == config.ROLE_HOST:
            from .. import cos as _cos_q

            n += len(_cos_q.approved_pending(self.vault))
        return n
    def write_note(
        self, rel_path: str, content: str, reason: str = "", *,
        subtree: str | None = None,
    ) -> dict[str, Any]:
        """Write a note to the vault and append a signed audit-chain entry.

        Fails closed in BOTH directions:
        - if no signing key resolves (KeyUnavailable), nothing is written;
        - the chain records the write ATTEMPT first, then the OUTCOME. If the
          file write raises after signing (disk full, permission), a compensating
          ``write_failed`` entry is appended so the chain never claims a write
          that didn't land (F-06). The original exception is re-raised.

        Containment (C-2): the RESOLVED target (symlinks followed) must stay
        inside the vault, and — when ``subtree`` is given (e.g. ``"raw"`` or
        ``"brain/resources"`` on the drain/capture paths) — inside that
        SPECIFIC subtree, so a traversal-laden rel_path can never earn an
        Ed25519 signature over an overwrite elsewhere. Refused BEFORE signing.

        HOST-broker only: refused on the VM leg BEFORE any signing-key
        resolution (the VM never holds the audit key).

        DURABLE (ENF-01, adversarial review round 3, 2026-08-10). The file
        write used to be a plain ``Path.write_text``: not atomic, and nothing
        fsynced. ``supersede``/``unsupersede`` then unlinked their crash
        journal — with a *directory* fsync — the moment both calls returned, so
        a power loss could persist the journal's deletion while losing or
        tearing a note write, leaving a signed one-sided chain with no
        recovery record. ``_write_note_durable`` fsyncs the content and the
        parent directory entry before returning, so "``write_note`` returned"
        now means "these bytes survive a power loss" — which is the only thing
        that makes clearing the journal afterwards safe.
        """
        self._require_host("write notes (sign + commit)")
        target = self.vault / rel_path
        if not _contained_in(target, self.vault):
            raise ValueError(f"write target escapes vault: {rel_path}")
        if subtree is not None and not _contained_in(target, self.vault / subtree):
            raise ValueError(f"write target escapes {subtree!r} subtree: {rel_path}")
        target = target.resolve()
        # Append the signed audit entry FIRST; if signing fails, nothing is written.
        content_sha = sha256_text(content)
        try:
            entry = self.audit.append(
                verb="write", path=rel_path,
                reason=reason or f"write_note {rel_path}",
                content_sha256=content_sha,
            )
        except KeyUnavailable:
            raise  # fail closed — no unsigned writes
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._write_note_durable(target, content)
        except Exception as exc:
            # The signed "write" entry is already in the chain; record the failure
            # so verify-audit shows the attempt did not complete.
            try:
                self.audit.append(
                    verb="write_failed", path=rel_path,
                    reason=f"file write failed after signing: {type(exc).__name__}: {exc}",
                )
            except KeyUnavailable:
                pass  # key vanished mid-op; the original error is what matters
            raise
        return {"written": str(target), "audit": entry}
