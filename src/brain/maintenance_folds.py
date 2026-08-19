"""Nightly duplicate-retirement fold."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

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

    Canonical = newer by (document_date, else updated, else created); tie ->
    more backlinks (counted over this same body pass — no second vault
    walk); tie -> lexicographically last id. The older side is retired via
    the audited ``core.supersede`` (signed, reversible, invariant-checked).

    Bounded to ``autodedup_max_per_run()`` retirements; anything past the cap
    is left untouched and counted in ``truncated`` for the caller to log."""
    from .index import _boilerplate_patterns, _family_min_body

    live = _load_dedup_live_notes(core)
    return _run_dedup_groups(
        core,
        live,
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


def _dedup_trust_checker(core: Any) -> Callable[[dict[str, Any]], bool]:
    from . import frontmatter as fm

    trust_cache: dict[str, bool] = {}

    def _untrusted(note: dict[str, Any]) -> bool:
        cached = trust_cache.get(note["id"])
        if cached is None:
            path = Path(note["path"])
            if not path.is_absolute():
                path = Path(core.vault) / path
            try:
                meta, _ = fm.parse_text(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — unreadable = fail closed
                cached = True
            else:
                cached = (
                    str(meta.get("status", "")).strip().lower() == "draft"
                    or str(meta.get("provenance.trust", "")).strip().lower() == "untrusted"
                )
            trust_cache[note["id"]] = cached
        return cached

    return _untrusted


def _populate_dedup_backlinks(live: list[dict[str, Any]]) -> None:
    ids = {note["id"] for note in live}
    backlinks: dict[str, int] = {}
    for note in live:
        for match in _WIKILINK_RE.finditer(note["body"]):
            target = match.group(1).strip()
            if target in ids and target != note["id"]:
                backlinks[target] = backlinks.get(target, 0) + 1
    for note in live:
        note["backlinks"] = backlinks.get(note["id"], 0)


def _dedup_pair_outcome(
    first: dict[str, Any], second: dict[str, Any], *, floor: int,
    boilerplate_patterns: Any,
    is_untrusted: Callable[[dict[str, Any]], bool],
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


def _apply_dedup_pair(core: Any, outcome: dict[str, Any]) -> bool:
    from .core import SupersedeJournalUnreadable as _JournalUnreadable
    from .core import SupersedeNotDurable as _NotDurable

    try:
        core.supersede(
            outcome["old"]["id"], outcome["new"]["id"],
            reason="auto-dedup DDP-01 (sha256-identical, nightly self-organization)")
    except (_JournalUnreadable, _NotDurable):
        raise
    except Exception:  # noqa: BLE001 — one bad pair never aborts the fold
        return False
    return True


def _run_dedup_groups(
    core: Any, live: list[dict[str, Any]], *, cap: int, floor: int,
    boilerplate_patterns: Any,
) -> dict[str, Any]:
    _populate_dedup_backlinks(live)
    is_untrusted = _dedup_trust_checker(core)
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for note in live:
        by_hash.setdefault(note["hash"], []).append(note)
    skipped: dict[str, list[dict[str, Any]]] = {
        "skipped_short_body": [], "skipped_classification": [],
        "skipped_recurring": [], "skipped_trust": [],
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
                is_untrusted=is_untrusted,
            )
            if outcome is None or not _record_dedup_outcome(outcome, skipped):
                continue
            if len(retired) >= cap:
                truncated += 1
                continue
            if _apply_dedup_pair(core, outcome):
                retired.append({"old": outcome["old"]["id"], "new": outcome["new"]["id"]})
                retired_ids.add(outcome["old"]["id"])
    return {"retired": retired, **skipped, "truncated": truncated,
            "cap": cap, "floor": floor}


from . import maintenance as _maintenance  # noqa: E402

_WIKILINK_RE = _maintenance._WIKILINK_RE
_floor_bytes = _maintenance._floor_bytes
autodedup_max_per_run = _maintenance.autodedup_max_per_run
body_sha256 = _maintenance.body_sha256
