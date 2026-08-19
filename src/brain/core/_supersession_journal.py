"""Supersession journal persistence for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Path,
    SupersedeJournalUnreadable,
    _contained_in,
    config,
    frontmatter,
    vault_writer_lock,
)
from ._durability import (
    _fsync_dir_strict,
    _mkdir_durable,
    _require_durable_replace,
    _write_atomic_durable,
)


class _SupersessionJournalMixin:
    """Supersession journal persistence for BrainCore."""

    def _supersede_journal_path(self) -> Path:
        """Host-private (ENF-01 round 3) — see ``config.supersede_journal_path``."""
        return config.supersede_journal_path(self.vault)
    @staticmethod
    def _supersede_journal_checksum(journal: dict[str, Any]) -> str:
        """sha256 over the journal's payload with ``checksum`` itself excluded.

        Canonical (``sort_keys``, no spaces) so the digest depends on the
        VALUES, not on dict ordering. This detects a torn or corrupted journal;
        it is not a tamper control — the journal lives off the mount now, and
        being unreachable is what makes it untamperable."""
        import hashlib
        import json

        payload = {k: v for k, v in journal.items() if k != "checksum"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")).hexdigest()
    def _write_supersede_journal(self, journal: dict[str, Any]) -> None:
        """Write the rollback record ONCE, atomically and durably, BEFORE
        either note is touched.

        It used to be written with ``Path.write_text`` and then REWRITTEN after
        the first signed write to advance a ``stage`` field. Both halves were
        wrong. ``write_text`` truncates in place, so a crash during the rewrite
        leaves a truncated file where the only copy of both pre-transaction
        images lived — after one note had already changed. And nothing fsynced,
        so a journal that reached only the page cache is a journal a power loss
        never had (adversarial review round 2, 2026-08-10: a probe with a
        one-sided chain and a truncated journal returned
        ``{'recovered': False, 'reason': 'unreadable journal, discarded'}``
        while the half-chain stayed on disk).

        The stage field is gone with the rewrite: the journal is unlinked only
        after the LAST write returns, so its mere existence already means
        "incomplete", and recovery restores whichever side actually moved. One
        write, no second chance to corrupt it.

        ``_write_atomic_durable`` (module level) does the O_EXCL + O_NOFOLLOW
        staging, short-write loop, fsync, replace and STRICT parent fsync. It
        used to be ``cos._write_atomic``; round 3 moved every note-and-journal
        write off that symbol because a COS test double for it was intercepting
        unrelated vault writes — see ``_write_atomic_durable``.

        It carries a version stamp and a checksum (round 3): recovery REWRITES
        both notes from what it reads here, so "readable JSON" is not a high
        enough bar to act on — see ``_recover_pending_supersede``.

        0o600: this file holds both notes' complete text, at whatever tier they
        carry. It is owner-only, in an owner-only directory, off the mount.

        Durability is CHECKED, not assumed (round 4). Where an atomic replace
        cannot be proven to reach the disk, ``_require_durable_replace``
        refuses the whole supersession by name before anything is signed, and
        ``_mkdir_durable`` anchors a freshly created journal directory in its
        own parent — otherwise the first transaction after creating the store
        writes a durable file into a directory entry that isn't."""
        import json

        self._require_durable_replace(f"journal a {journal.get('op')}")
        record = {"v": self._SUPERSEDE_JOURNAL_V, **journal}
        record["checksum"] = self._supersede_journal_checksum(record)
        path = self._supersede_journal_path()
        self._mkdir_durable(path.parent)
        config.secure_file_permissions(path.parent, 0o700)   # never raises
        _write_atomic_durable(path, json.dumps(record).encode("utf-8"),
                              mode=0o600)
    def _clear_supersede_journal(self) -> None:
        """Durably forget a FINISHED transaction. The unlink is fsynced for the
        same reason the write is: a directory entry that never reached the disk
        brings the journal back after a power loss, and recovery would then
        "roll back" a supersede that actually completed — both sides differ
        from their recorded ``*_before``, which is exactly the signature of an
        interrupted one.

        STRICTLY fsynced (round 3): ``cos._fsync_dir`` swallows the failure,
        and an unreported one is precisely the case that resurrects the
        journal."""
        path = self._supersede_journal_path()
        path.unlink(missing_ok=True)
        _fsync_dir_strict(path.parent)
    def _recover_pending_supersede(self) -> dict[str, Any] | None:
        """HOST-only. Roll an interrupted ``supersede``/``unsupersede`` back to
        its pre-transaction state — BOTH sides — and clear the journal, so a
        crash mid-transaction can never leave a signed half-chain. Runs at the
        top of every ``supersede``/``unsupersede`` call before any new write.

        The journal survives only when the transaction did NOT complete (it is
        unlinked after the last write returns), so "a journal exists" is the
        whole decision — no stage inference is needed and none is kept. The
        earlier version restored only ``old_before`` on ``stage ==
        "old_written"``, which was correct for a crash BETWEEN the two writes
        and wrong for a crash AFTER the second: the second note kept its new
        content while the first was rolled back, manufacturing exactly the
        one-sided chain ``unsupersede`` exists to repair (HIGH, adversarial
        review 2026-08-10 — crash injection after ``unsupersede``'s second
        signed write left ``old.superseded_by="new"`` with
        ``new.previous_version=None``).

        Each side is restored only when its on-disk content actually differs
        from the recorded ``*_before``, so recovery is idempotent and a
        re-crashed recovery simply resumes.

        **An unreadable journal FAILS CLOSED and is preserved.** It used to be
        deleted and reported as ``recovered: False`` — throwing away the only
        record of the pre-transaction bytes while the half-chain it described
        stayed on disk, and then letting the next call proceed on top of it.
        A journal this path cannot parse is the one case a human must see, so
        it raises and leaves the file where it is.

        **A PARTIAL journal is unreadable too** (round 3). It used to accept
        any non-empty SUBSET of sides, restore only those, and then unlink the
        file regardless — a probe with a journal carrying only ``old_rel`` /
        ``old_before`` returned ``{"restored":["old"], "journal_exists":false,
        "old_superseded_by":"new", "new_previous_version":null}``: it RECREATED
        the one-sided chain this whole guard exists to prevent, and destroyed
        the remaining evidence on the way out. Every field of both sides is
        now required, typed, distinct, vault-contained, id-consistent with the
        recorded pre-image, and covered by the checksum. Anything short of that
        raises and the journal stays.

        **The host-private path is the ONLY path read** (round 4). Recovery
        used to fall back to the pre-2026-08-10 on-mount location when this one
        held nothing, waiving the version stamp and checksum for it because the
        old format had neither. That made ``<runtime>/supersede-pending.json``
        — writable by the untrusted Cowork leg — a way to hand the host two
        arbitrary note bodies at an arbitrary classification and have the
        hourly ``maintain`` sign them. The migration was worth nothing (a
        journal exists only for the seconds a supersession is in flight, and
        none was pending anywhere) and cost a signed MNPI-to-Public downgrade,
        so the fallback is deleted rather than hardened."""
        path = self._supersede_journal_path()
        if not path.exists():
            return None
        import json

        def _unreadable(why: str) -> SupersedeJournalUnreadable:
            return SupersedeJournalUnreadable(
                f"supersede: the crash journal at {path} is unreadable ({why}) "
                f"— it is the ONLY record of the pre-transaction content of a "
                f"supersede/unsupersede that did not finish, so it has been "
                f"PRESERVED and nothing was written. Inspect the two notes it "
                f"names against the audit log, repair them by hand, then delete "
                f"the journal to unblock supersession.")

        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _unreadable(f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(journal, dict):
            raise _unreadable(f"not an object: {type(journal).__name__}")
        self._validate_supersede_journal(journal, unreadable=_unreadable)
        op = str(journal["op"])
        result: dict[str, Any] = {"recovered": True, "op": op, "restored": [],
                                  "journal": str(path)}
        for side in ("old", "new"):
            rel, before = journal[f"{side}_rel"], journal[f"{side}_before"]
            try:
                current = (self.vault / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
            if current == before:
                continue  # this side never moved (or was already restored)
            self.write_note(
                rel, before,
                reason=f"{op}-rollback: {journal.get('old_id')} -> "
                       f"{journal.get('new_id')} (interrupted mid-transaction, "
                       f"restoring {side})",
            )
            result["restored"].append(side)
        result["action"] = ("rolled_back_" + "_".join(result["restored"])
                            if result["restored"] else "nothing_to_roll_back")
        self._clear_supersede_journal()
        return result
    def _validate_supersede_journal(
        self, journal: dict[str, Any], *, unreadable: Any,
    ) -> None:
        """Raise unless ``journal`` describes a COMPLETE two-sided rollback.

        Recovery rewrites signed notes from these bytes, so every one of them
        is checked before a single write: the version stamp and checksum (a
        torn or edited record), both ids present and distinct, both relative
        paths present, distinct and resolving INSIDE the vault (a ``../``
        would make rollback an arbitrary audited overwrite), both pre-images
        present as strings, and each pre-image's own frontmatter ``id``
        matching the id the journal claims for that side — the cheap check
        that the two halves actually belong to the transaction named.

        There is no waiver and no exempt caller (round 4): the legacy on-mount
        reader that needed one is gone."""
        if journal.get("v") != self._SUPERSEDE_JOURNAL_V:
            raise unreadable(
                f"schema version {journal.get('v')!r}, expected "
                f"{self._SUPERSEDE_JOURNAL_V}")
        want = self._supersede_journal_checksum(journal)
        if journal.get("checksum") != want:
            raise unreadable(
                f"checksum mismatch (recorded {journal.get('checksum')!r}, "
                f"computed {want!r}) — the record is torn or was edited")
        if journal.get("op") not in ("supersede", "unsupersede"):
            raise unreadable(f"unknown op {journal.get('op')!r}")
        for key in ("old_id", "new_id", "old_rel", "new_rel",
                    "old_before", "new_before"):
            if not isinstance(journal.get(key), str) or not journal[key].strip():
                raise unreadable(f"missing or non-string {key!r}")
        if journal["old_id"] == journal["new_id"]:
            raise unreadable("both sides name the same id")
        if journal["old_rel"] == journal["new_rel"]:
            raise unreadable("both sides name the same path")
        for side in ("old", "new"):
            rel = journal[f"{side}_rel"]
            if not _contained_in(self.vault / rel, self.vault):
                raise unreadable(f"{side}_rel {rel!r} escapes the vault")
            meta, _ = frontmatter.parse_text(journal[f"{side}_before"])
            got = str(meta.get("id") or "").strip()
            if got != journal[f"{side}_id"]:
                raise unreadable(
                    f"{side}_before carries id {got!r}, but the journal names "
                    f"{journal[f'{side}_id']!r}")
    def recover_pending_supersede(self, *, dry_run: bool = False) -> dict[str, Any] | None:
        """Public preflight: roll back an interrupted supersession, under the
        same single-writer lock the write verbs take. ``None`` when nothing is
        pending; raises :class:`SupersedeJournalUnreadable` when a journal
        exists but cannot be acted on (fail closed, journal preserved).

        Called once at the top of ``maintain`` — see that docstring for why
        leaving it to ``supersede``'s own call site was not enough.

        ``dry_run`` REPORTS a pending journal without writing anything: a
        rollback is two signed note writes, which is exactly what --dry-run
        promises not to do."""
        self._require_host("recover a pending supersede journal (writes notes)")
        path = self._supersede_journal_path()
        if not path.exists():
            return None
        if dry_run:
            return {"recovered": False, "pending": True, "journal": str(path),
                    "action": "dry-run: not recovered"}
        with vault_writer_lock(self.vault, verb="supersede-recover"):
            return self._recover_pending_supersede()
