"""Supersession transactions for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Path,
    SupersedePreconditionFailed,
    frontmatter,
    sha256_text,
    vault_writer_lock,
)


class _SupersessionTransactionMixin:
    """Supersession transactions for BrainCore."""

    def supersede(self, old_id: str, new_id: str, *, reason: str = "",
                  expect: dict[str, Any] | None = None) -> dict[str, Any]:
        """Retire ``old_id`` in favour of ``new_id`` — both sides of the version
        chain, written through the audited ``write_note`` path (ADR-0003 Ruling
        2/8). HOST-broker only.

        Refuses BEFORE any signing-key resolution / WAL / index mutation when:
        - ``role != host``;
        - either id does not resolve to an on-disk note, or ``old_id == new_id``;
        - ``old_id`` is already superseded (chain invariant: no re-superseding an
          already-superseded note);
        - ``new_id`` itself already carries ``is_latest_version: false`` (would
          make it a "latest" that is simultaneously retired — refuse creating a
          second latest);
        - the successor's OWN frontmatter has no explicit ``classification`` —
          per the ADR ruling, classification is NEVER inherited implicitly
          across a supersession.

        Atomicity: a pending-operation journal (host-private, off the Cowork
        mount — ``config.supersede_journal_path``) is written before either
        note write and cleared after both are DURABLY committed. A crash between the two signed writes leaves
        a journal that the NEXT ``supersede`` call rolls back (restores the old
        note, then proceeds) before doing anything else — never a signed
        half-chain (HARDENED:codex).

        CC-02 (finding 1, 2026-07-20 dedup batch): the ENTIRE critical section
        (journal recovery, both signed writes, the trailing reindex) runs under
        the SAME bounded single-writer lock ``sync``/``rebuild``/``snapshot``
        use — previously `supersede` wrote both notes completely unlocked and
        only its trailing ``self.sync()`` call ever touched the lock, so a
        concurrent long-running writer (e.g. the hourly sync) could leave a
        `supersede` call blocking silently for minutes with no bounded refusal.
        One acquisition here means a busy writer is named and refused within
        ``$BRAIN_WRITER_LOCK_SECONDS`` (default 30s) — never a multi-minute
        silent block — and the lock's re-entrant depth counter means the
        trailing ``self.sync()`` call's own acquisition is a same-process no-op,
        not a second wait.

        ``expect`` (HARDENED:codex-8) is an OPTIONAL precondition set a caller
        computed OUT of band — content hashes and chain-head values it saw when
        it decided this supersession was correct. Every key present is verified
        **inside this lock, before the first signed write**. A caller that
        re-checks its own preconditions and THEN calls in is TOCTOU: the
        nightly folds hold the same lock and can retire or rewrite either note
        in the gap between that check and this acquisition. Recognised keys —
        ``old_sha256``/``new_sha256`` (``sha256_text`` of the whole note file),
        ``old_superseded_by``, ``old_is_latest_version``,
        ``new_is_latest_version``, ``new_previous_version``. A mismatch raises
        :class:`SupersedePreconditionFailed` and nothing is written.
        """
        self._require_host("supersede notes (writes both sides of a version chain)")
        with vault_writer_lock(self.vault, verb="supersede"):
            return self._supersede_locked(old_id, new_id, reason=reason,
                                          expect=expect)
    @staticmethod
    def _check_supersede_expect(expect: dict[str, Any], *, old_id: str, new_id: str,
                                old_before: str, new_before: str,
                                old_meta: dict[str, Any],
                                new_meta: dict[str, Any]) -> None:
        """Verify a caller's out-of-band preconditions. Raises on ANY mismatch.

        The two content hashes alone are sufficient (frontmatter is inside the
        file, so any chain mutation moves the hash); the chain-head keys are
        kept because they name WHAT drifted, and "the pair was chained while
        the proposal waited" is the case an operator most needs spelled out.
        """
        actual: dict[str, Any] = {
            "old_sha256": sha256_text(old_before),
            "new_sha256": sha256_text(new_before),
            "old_superseded_by": str(old_meta.get("superseded_by") or "").strip(),
            "new_previous_version": str(new_meta.get("previous_version") or "").strip(),
            "old_is_latest_version": str(
                old_meta.get("is_latest_version", "")).strip().lower(),
            "new_is_latest_version": str(
                new_meta.get("is_latest_version", "")).strip().lower(),
        }
        for key, want in expect.items():
            if key not in actual:
                raise SupersedePreconditionFailed(
                    f"supersede: unknown precondition {key!r}")
            got = actual[key]
            if str(want).strip().lower() != str(got).strip().lower():
                raise SupersedePreconditionFailed(
                    f"supersede {old_id} -> {new_id}: precondition {key!r} "
                    f"drifted (expected {want!r}, found {got!r}) — the pair "
                    "changed after the decision was made; nothing was written")
    def _supersede_locked(self, old_id: str, new_id: str, *, reason: str = "",
                          expect: dict[str, Any] | None = None) -> dict[str, Any]:
        self._recover_pending_supersede()

        if old_id == new_id:
            raise ValueError("supersede: a note may not supersede itself")
        old_row = self.index.get(old_id)
        new_row = self.index.get(new_id)
        if not old_row:
            raise ValueError(f"supersede: old note not found: {old_id}")
        if not new_row:
            raise ValueError(f"supersede: new note not found: {new_id}")

        old_path, new_path = Path(old_row["path"]), Path(new_row["path"])
        old_before = old_path.read_text(encoding="utf-8")
        new_before = new_path.read_text(encoding="utf-8")
        old_meta, _ = frontmatter.parse_text(old_before)
        new_meta, _ = frontmatter.parse_text(new_before)

        # Caller preconditions FIRST: a drifted pair gets the precise "this
        # changed under you" error rather than a generic invariant failure.
        if expect:
            self._check_supersede_expect(
                expect, old_id=old_id, new_id=new_id, old_before=old_before,
                new_before=new_before, old_meta=old_meta, new_meta=new_meta)

        # -- chain invariants + classification ruling (refused before any write) --
        if old_meta.get("superseded_by") or str(old_meta.get("is_latest_version", "")).strip().lower() == "false":
            raise ValueError(f"supersede: {old_id!r} is already superseded — no re-superseding")
        if str(new_meta.get("is_latest_version", "")).strip().lower() == "false":
            raise ValueError(
                f"supersede: {new_id!r} is itself already retired "
                "(is_latest_version: false) — refusing to create a second latest"
            )
        if not str(new_meta.get("classification") or "").strip():
            raise ValueError(
                f"supersede: successor {new_id!r} has no explicit classification — "
                "classification is never inherited across a supersession (ADR-0003 Ruling 2b)"
            )

        import datetime as _dt

        today = _dt.date.today().isoformat()
        old_rel = old_path.relative_to(self.vault).as_posix()
        new_rel = new_path.relative_to(self.vault).as_posix()

        self._write_supersede_journal({
            "op": "supersede", "old_id": old_id, "new_id": new_id,
            "old_rel": old_rel, "new_rel": new_rel,
            "old_before": old_before, "new_before": new_before,
        })

        old_after = frontmatter.set_keys(old_before, {
            "superseded_by": new_id, "superseded_date": today, "is_latest_version": False,
        })
        new_after = frontmatter.set_keys(new_before, {
            "previous_version": old_id, "is_latest_version": True,
        })

        old_write = self.write_note(
            old_rel, old_after,
            reason=reason or f"supersede: {old_id} -> {new_id} (retiring {old_id})",
        )
        new_write = self.write_note(
            new_rel, new_after,
            reason=reason or f"supersede: {old_id} -> {new_id} (new head {new_id})",
        )
        self._clear_supersede_journal()

        sync_res = self.sync(drain=False)
        return {
            "old_id": old_id, "new_id": new_id,
            "old_write": old_write, "new_write": new_write,
            "reindexed": {"added": sync_res.get("added", 0), "updated": sync_res.get("updated", 0)},
        }
    def unsupersede(self, old_id: str, new_id: str, *, reason: str = "") -> dict[str, Any]:
        """Undo ONE supersession link ``old_id -> new_id``, both sides, through
        the audited ``write_note`` path. HOST-broker only.

        This exists because DDP-01's nightly auto-dedup could create a link
        nothing could undo. It auto-superseded on BODY identity, and an image
        whose OCR extracted to a 122-byte ``[no text detected]`` stub is
        byte-identical to every other such image — so part 1 of a deck retired
        part 2, and nine distinct QR codes became one version family. ENF-01's
        body floor stops NEW ones; those already written needed an audited
        inverse, and `supersede` deliberately refuses to re-supersede an
        already-superseded note, so there was no way back.

        Refuses BEFORE any write unless ``old_id`` actually claims to be
        retired in favour of ``new_id`` (``old.superseded_by == new_id``) —
        that claim is the thing being undone, and without it the caller is
        naming a link that does not exist.

        The successor's side is repaired opportunistically rather than
        demanded, because the chains most needing repair are the malformed
        ones. On the reference vault two notes both declared
        ``superseded_by: …-qr-qa`` while ``qr-qa`` named only one of them as
        its ``previous_version``; requiring reciprocity would have left the
        unreciprocated half permanently unfixable. So ``previous_version`` is
        dropped from ``new_id`` only when it names ``old_id``, and otherwise
        left alone and reported as ``new_previous_version_kept``.

        Both notes come out in their PRE-link shape: the three retirement keys
        are dropped from ``old_id`` (absence of ``is_latest_version`` reads as
        "not retired" per AGENTS.md §2, so it is dropped rather than flipped to
        true). ``new_id``'s own ``is_latest_version`` is left exactly as found
        — it may be the head of an unrelated chain this call has no business
        touching.

        Same single-writer lock and same crash journal as ``supersede`` (a
        crash between the two signed writes is rolled back by the same
        ``_recover_pending_supersede``).
        """
        self._require_host("unsupersede notes (writes both sides of a version chain)")
        with vault_writer_lock(self.vault, verb="unsupersede"):
            return self._unsupersede_locked(old_id, new_id, reason=reason)
    def _unsupersede_locked(self, old_id: str, new_id: str, *,
                            reason: str = "") -> dict[str, Any]:
        self._recover_pending_supersede()

        if old_id == new_id:
            raise ValueError("unsupersede: a note may not supersede itself")
        old_row = self.index.get(old_id)
        new_row = self.index.get(new_id)
        if not old_row:
            raise ValueError(f"unsupersede: old note not found: {old_id}")
        if not new_row:
            raise ValueError(f"unsupersede: new note not found: {new_id}")

        old_path, new_path = Path(old_row["path"]), Path(new_row["path"])
        old_before = old_path.read_text(encoding="utf-8")
        new_before = new_path.read_text(encoding="utf-8")
        old_meta, _ = frontmatter.parse_text(old_before)
        new_meta, _ = frontmatter.parse_text(new_before)

        # AGENTS.md §2 permits a bare id OR a `[[wikilink]]`, and `replaces` is
        # a documented alias of `previous_version` — `notes._bitemporal_link`
        # is the ONE normalization the index already applies to both. Comparing
        # raw frontmatter here refused `superseded_by: [[new]]` outright and
        # left a `replaces:` predecessor link standing after a "successful"
        # repair (adversarial review 2026-08-10).
        from ..notes import _bitemporal_link

        def _link(val: object) -> str:
            # A bare-date id (`superseded_by: 2026-05-27`) is parsed by YAML as
            # a date, not a string — those exist in the reference vault, so
            # stringify before normalizing rather than silently reading "".
            return _bitemporal_link(val if isinstance(val, str) or val is None
                                    else str(val))

        if _link(old_meta.get("superseded_by")) != new_id:
            raise ValueError(
                f"unsupersede: {old_id!r} is not superseded by {new_id!r} "
                f"(found {old_meta.get('superseded_by')!r}) — nothing written")
        #: whichever documented predecessor key(s) actually name old_id
        back_keys = tuple(k for k in ("previous_version", "replaces")
                          if _link(new_meta.get(k)) == old_id)
        reciprocal = bool(back_keys)

        old_rel = old_path.relative_to(self.vault).as_posix()
        new_rel = new_path.relative_to(self.vault).as_posix()

        self._write_supersede_journal({
            "op": "unsupersede", "old_id": old_id, "new_id": new_id,
            "old_rel": old_rel, "new_rel": new_rel,
            "old_before": old_before, "new_before": new_before,
        })

        old_after = frontmatter.drop_keys(old_before, self.SUPERSESSION_KEYS_OLD)

        why = reason or f"unsupersede: broke the {old_id} -> {new_id} link"
        old_write = self.write_note(old_rel, old_after, reason=f"{why} (restoring {old_id})")
        new_write = None
        if reciprocal:
            new_write = self.write_note(
                new_rel, frontmatter.drop_keys(new_before, back_keys),
                reason=f"{why} (clearing {new_id})")
        self._clear_supersede_journal()

        sync_res = self.sync(drain=False)
        kept = next((str(new_meta.get(k)) for k in ("previous_version", "replaces")
                     if not reciprocal and new_meta.get(k)), None)
        return {
            "old_id": old_id, "new_id": new_id,
            "old_write": old_write, "new_write": new_write,
            "cleared_keys": list(back_keys),
            "new_previous_version_kept": kept,
            "reindexed": {"added": sync_res.get("added", 0),
                          "updated": sync_res.get("updated", 0)},
        }
