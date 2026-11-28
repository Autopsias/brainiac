"""Nightly duplicate-retirement fold."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Iterator

from .maintenance_organization import (
    _floor_bytes,
    autodedup_max_per_run,
    body_sha256,
    iter_wikilinks,
)

def auto_dedup_tier1(core: Any) -> dict[str, Any]:
    """DDP-01: auto-supersede sha256-identical-body duplicate PAIRS.

    Reads ``body`` straight off the already-synced ``notes`` index row (no
    second file read) — this fold runs right after ``sync`` in the same
    unconditional daily block VER-01 occupies, so the index already reflects
    the current on-disk state.

    A pair is a TIER-1 candidate only when ALL of:
    (a) both notes are LIVE (``is_latest_version`` not ``false``, neither
        already ``superseded_by`` something) and their normalized body
        sha256 matches;
    (b) their ``classification`` values are IDENTICAL — a classification
        mismatch is never automated, it is counted and left for a human;
    (c) same zone (``raw``/``brain``) and same ``type``;
    (d) NOT both ids matching a recurring-artifact id pattern (the same
        list ``index.near_dup`` uses for its boilerplate caveat) — two
        generated periodic artifacts (``daily-*``, transcripts, ...) can be
        byte-identical by template design without being real duplicates;
    (e0) BOTH bodies clear the ``$BRAIN_FAMILY_MIN_BODY`` floor (ENF-01,
        2026-08-10). Byte-identity is only evidence of "same document" when
        the bytes SAY something. A scanned image whose OCR extracted to the
        122-byte ``[no text detected]`` stub is byte-identical to every other
        such image, so this fold merged part 1 of a deck with part 2, and
        nine distinct QR codes into one version family, on the reference
        deployment. Same floor, same env var and same reasoning as the
        RANKING-time family collapse in ``index.py`` (``FAMILY_MIN_BODY``,
        ``_family_min_body``) — that guard already refuses to fold a family
        on a sub-floor body, and the accessor is imported rather than
        duplicated so the two sites can never drift apart. The floor is
        checked FIRST because a sub-floor pair is not evidence of anything:
        classifying it as a classification/trust mismatch would report a
        judgment call where there is no candidate at all. The audited undo
        for the links already written is ``core.unsupersede``.
    (e) their TRUST levels match (codex 2026-07-22): a note carrying
        ``status: draft`` or ``provenance.trust: untrusted`` (a drained VM/
        capture draft) must never automatically retire a trusted note —
        canonical selection keys on attacker-writable frontmatter dates, so
        an untrusted byte-duplicate with a future ``document_date`` would
        otherwise win. A trust mismatch is counted and left for a human,
        exactly like a classification mismatch. An unreadable/unparseable
        note fails closed as untrusted.
    (f) BOTH notes' CURRENT file bytes are the bytes the host audit chain last
        signed for their path (F44). Trust in (e) is read from the note's own
        frontmatter, which anything able to write the vault can author, so an
        unstamped planted copy of a trusted note reads as trusted. The chain is
        host-private and off the mount: a planted note has no entry, a
        hand-edited one a stale hash, a legacy entry with no content hash
        cannot vouch. Such a pair is counted in ``skipped_unverified`` and
        never retires or replaces anything, and the supersede is pinned to the
        verified bytes (``_HostVerified.supersede``), so a swap between the
        check and the signature is refused too, not signed. (e) is judged on
        those same verified bytes, not on a second read of the file.

    Canonical = newer by (document_date, else updated, else created); tie ->
    more backlinks (counted over this same body pass — no second vault
    walk); tie -> lexicographically last id. The older side is retired via
    the audited ``core.supersede`` (signed, reversible, invariant-checked).

    Bounded to ``autodedup_max_per_run()`` retirements; anything past the cap
    is left untouched and counted in ``truncated`` for the caller to log."""
    from .core import SupersedeBatch
    from .index import _boilerplate_patterns, _family_min_body

    live = _load_dedup_live_notes(core)
    # One writer lock and one reindex for the whole run, not a full-vault sync
    # per retired pair — see `SupersedeBatch`.
    with SupersedeBatch(core, "auto-dedup") as batch:
        return _run_dedup_groups(
            core,
            live,
            batch=batch,
            cap=autodedup_max_per_run(),
            floor=_family_min_body(),
            boilerplate_patterns=_boilerplate_patterns(),
        )


def _load_dedup_live_notes(core: Any) -> list[dict[str, Any]]:
    rows = core.index.conn.execute(
        "SELECT id, zone, type, classification, is_latest_version, superseded_by, "
        "body, path, COALESCE(NULLIF(document_date,''), NULLIF(updated,''), created) "
        "FROM notes"
    ).fetchall()
    live: list[dict[str, Any]] = []
    for nid, zone, ntype, cls, ilv, sup_by, body, path, date_key in rows:
        if sup_by or str(ilv or "").strip().lower() == "false":
            continue
        live.append({
            "id": str(nid), "zone": str(zone or ""), "type": str(ntype or ""),
            "classification": str(cls or ""), "date_key": str(date_key or ""),
            "path": str(path or ""),
            "body": body or "", "hash": body_sha256(body or ""),
        })
    return live


def _note_file(core: Any, note: dict[str, Any]) -> Path:
    path = Path(note["path"])
    return path if path.is_absolute() else Path(core.vault) / path


class _HostVerified:
    """F44: ``verified(note)`` is the note's pinned content hash when its CURRENT
    bytes are the bytes the host audit chain last signed for its path, else None.

    The chain is host-private (off the mount), so unlike frontmatter this is not
    something a vault writer can author: a planted note has no entry and an
    edited one a stale hash. A missing or unreadable chain verifies nothing.

    The pin is ``sha256_text`` of the note as ``Path.read_text`` sees it, the
    convention ``supersede(expect=...)`` compares. The pin AND the draft/untrusted
    decision (:meth:`untrusted`) are both derived from the SAME single read of the
    bytes matched against the chain (whose hashes are over raw bytes and differ
    for a CR/CRLF note; a CR-only swap in the window leaves the text supersede
    signs unchanged), so a swap between two reads cannot show the trust check
    one version of the note and the supersede another. :meth:`supersede` hands
    the pin to ``core.supersede``, which checks it inside the writer lock before
    the first signed write, so bytes swapped in after the check are refused,
    not signed."""

    def __init__(self, core: Any) -> None:
        self._core = core
        self._signed: dict[str, str] | None = None
        self._dirty: set[str] = set()
        self._pin: dict[str, str | None] = {}
        self._untrusted: dict[str, bool] = {}

    def note(self, nid: str) -> dict[str, str]:
        row = self._core.index.conn.execute(
            "SELECT path FROM notes WHERE id = ?", (nid,)).fetchone()
        return {"id": nid, "path": str(row[0] or "") if row else ""}

    def __call__(self, note: dict[str, Any]) -> str | None:
        nid = note["id"]
        if nid not in self._pin:
            if self._signed is None or nid in self._dirty:
                self._signed, self._dirty = self._read_chain(), set()
            self._pin[nid] = self._verify(note)
        return self._pin[nid]

    def pin(self, nid: str) -> str | None:
        """``self(note)`` for the note with this id."""
        return self(self.note(nid))

    def untrusted(self, note: dict[str, Any]) -> bool:
        """Marked draft/untrusted in the very bytes ``self(note)`` verified (or
        failed to): unreadable or unparseable reads as untrusted."""
        self(note)
        return self._untrusted[note["id"]]

    def _read_chain(self) -> dict[str, str]:
        try:
            return self._core.audit.latest_signed_hashes()
        except Exception:  # noqa: BLE001 — no/unreadable chain: fail closed
            return {}

    def _verify(self, note: dict[str, Any]) -> str | None:
        from .index_stages.sync import _chain_key
        from .notes import sha256_text

        nid, path = note["id"], _note_file(self._core, note)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):  # unreadable = fail closed
            self._untrusted[nid] = True
            return None
        text = text.replace("\r\n", "\n").replace("\r", "\n")  # as read_text folds them
        self._untrusted[nid] = _text_untrusted(text)
        signed = (self._signed or {}).get(_chain_key(str(path), self._core.vault))
        if signed != hashlib.sha256(raw).hexdigest():
            return None
        return sha256_text(text)

    def supersede(self, batch: Any, old_id: str, new_id: str, *, reason: str) -> dict[str, Any]:
        """``batch.supersede`` pinned to the verified bytes of BOTH notes."""
        expect = {"old_sha256": self.pin(old_id), "new_sha256": self.pin(new_id)}
        try:
            return batch.supersede(old_id, new_id, reason=reason, expect=expect)
        finally:  # supersede re-signs both sides: look at the chain afresh next time
            for nid in (old_id, new_id):
                self._pin.pop(nid, None)
                self._dirty.add(nid)


def _text_untrusted(text: str) -> bool:
    """The DDP-01/VER-01 trust rule over note text already read: marked draft or
    untrusted, and an unparseable frontmatter fails closed as untrusted."""
    from . import frontmatter as fm

    try:
        meta, _ = fm.parse_text(text)
    except Exception:  # noqa: BLE001 — unparseable = fail closed
        return True
    return (
        str(meta.get("status", "")).strip().lower() == "draft"
        or str(meta.get("provenance.trust", "")).strip().lower() == "untrusted"
    )


def _populate_dedup_backlinks(live: list[dict[str, Any]]) -> None:
    ids = {note["id"] for note in live}
    backlinks: dict[str, int] = {}
    for note in live:
        for match in iter_wikilinks(note["body"]):
            target = match.group(1).strip()
            if target in ids and target != note["id"]:
                backlinks[target] = backlinks.get(target, 0) + 1
    for note in live:
        note["backlinks"] = backlinks.get(note["id"], 0)


def _dedup_pair_outcome(
    first: dict[str, Any], second: dict[str, Any], *, floor: int,
    boilerplate_patterns: Any,
    is_untrusted: Callable[[dict[str, Any]], bool],
    is_verified: Callable[[dict[str, Any]], str | None],
) -> dict[str, Any] | None:
    from .index import _matches_boilerplate_pattern

    if first["zone"] != second["zone"] or first["type"] != second["type"]:
        return None
    shortest = min(_floor_bytes(first["body"]), _floor_bytes(second["body"]))
    if shortest < floor:
        return {"kind": "skipped_short_body",
                "record": {"a": first["id"], "b": second["id"],
                           "bytes": shortest, "floor": floor}}
    if first["classification"] != second["classification"]:
        return {"kind": "skipped_classification",
                "record": {"a": first["id"], "b": second["id"]}}
    first_pattern = _matches_boilerplate_pattern(first["id"], boilerplate_patterns)
    second_pattern = _matches_boilerplate_pattern(second["id"], boilerplate_patterns)
    if first_pattern and second_pattern:
        return {"kind": "skipped_recurring",
                "record": {"a": first["id"], "b": second["id"],
                           "pattern": first_pattern}}
    if is_untrusted(first) != is_untrusted(second):
        return {"kind": "skipped_trust",
                "record": {"a": first["id"], "b": second["id"]}}
    if not (is_verified(first) and is_verified(second)):
        return {"kind": "skipped_unverified",
                "record": {"a": first["id"], "b": second["id"]}}
    first_key = (first["date_key"], first["backlinks"], first["id"])
    second_key = (second["date_key"], second["backlinks"], second["id"])
    canonical, old = (first, second) if first_key >= second_key else (second, first)
    return {"kind": "candidate", "old": old, "new": canonical}


def _iter_dedup_pairs(
    members: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for index, first in enumerate(members):
        for second in members[index + 1:]:
            yield first, second


def _record_dedup_outcome(
    outcome: dict[str, Any], skipped: dict[str, list[dict[str, Any]]],
) -> bool:
    kind = outcome["kind"]
    if kind == "candidate":
        return True
    skipped[kind].append(outcome["record"])
    return False


def _apply_dedup_pair(
    batch: Any, outcome: dict[str, Any], verified: _HostVerified,
    skipped: dict[str, list[dict[str, Any]]],
) -> bool:
    from .core import SupersedeJournalUnreadable as _JournalUnreadable
    from .core import SupersedeNotDurable as _NotDurable
    from .core import SupersedePreconditionFailed as _Drifted

    try:
        verified.supersede(
            batch, outcome["old"]["id"], outcome["new"]["id"],
            reason="auto-dedup DDP-01 (sha256-identical, nightly self-organization)")
    except (_JournalUnreadable, _NotDurable):
        raise
    except _Drifted:  # a note changed after it was verified: never signed, counted
        skipped["skipped_unverified"].append(
            {"a": outcome["old"]["id"], "b": outcome["new"]["id"]})
        return False
    except Exception:  # noqa: BLE001 — one bad pair never aborts the fold
        return False
    return True


def _run_dedup_groups(
    core: Any, live: list[dict[str, Any]], *, batch: Any, cap: int, floor: int,
    boilerplate_patterns: Any,
) -> dict[str, Any]:
    _populate_dedup_backlinks(live)
    verified = _HostVerified(core)
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for note in live:
        by_hash.setdefault(note["hash"], []).append(note)
    skipped: dict[str, list[dict[str, Any]]] = {
        "skipped_short_body": [], "skipped_classification": [],
        "skipped_recurring": [], "skipped_trust": [], "skipped_unverified": [],
    }
    retired: list[dict[str, str]] = []
    retired_ids: set[str] = set()
    truncated = 0
    for digest in sorted(by_hash):
        members = sorted(by_hash[digest], key=lambda note: note["id"])
        if len(members) < 2:
            continue
        for first, second in _iter_dedup_pairs(members):
            if first["id"] in retired_ids or second["id"] in retired_ids:
                continue
            outcome = _dedup_pair_outcome(
                first, second, floor=floor,
                boilerplate_patterns=boilerplate_patterns,
                is_untrusted=verified.untrusted, is_verified=verified,
            )
            if outcome is None or not _record_dedup_outcome(outcome, skipped):
                continue
            if len(retired) >= cap:
                truncated += 1
                continue
            if _apply_dedup_pair(batch, outcome, verified, skipped):
                retired.append({"old": outcome["old"]["id"], "new": outcome["new"]["id"]})
                retired_ids.add(outcome["old"]["id"])
    return {"retired": retired, **skipped, "truncated": truncated,
            "cap": cap, "floor": floor}

