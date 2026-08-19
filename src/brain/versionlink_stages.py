"""Version-link proposal stages."""
from __future__ import annotations

from typing import Any, Iterable

from .index import _boilerplate_patterns, _matches_boilerplate_pattern


def _direction_signals(old: Any, new: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    signals: dict[str, Any] = {}
    if not (old.valid_date and new.valid_date and new.valid_date > old.valid_date):
        return signals, {"verdict": "skip", "reason": "no strictly newer valid date",
                         "signals": signals}
    signals["newer_date"] = {"old": old.valid_date, "new": new.valid_date}
    return signals, None


def _add_family_signals(old: Any, new: Any, signals: dict[str, Any]) -> bool:
    shared = sorted(old.stems & new.stems)
    if shared:
        signals["name_family"] = shared[0]
    if old.prov.get("conversation_id") and (
            old.prov["conversation_id"] == new.prov.get("conversation_id")):
        signals["conversation"] = old.prov["conversation_id"]
    if old.prov.get("sender") and old.prov["sender"] == new.prov.get("sender"):
        signals["sender"] = old.prov["sender"]
    if signals.get("conversation") or signals.get("sender"):
        signals["family_class"] = _versionlink.FAMILY_EMAIL
    elif signals.get("name_family") and not (old.email_claimed or new.email_claimed):
        signals["family_class"] = _versionlink.FAMILY_NAME
    else:
        return False
    return True


def _marker_result(
    old: Any, new: Any, signals: dict[str, Any], similarity: float | None,
    threshold: float,
) -> tuple[bool, dict[str, Any] | None]:
    marker_ok = False
    if old.marker and new.marker:
        signals["version_markers"] = {"old": list(old.marker), "new": list(new.marker)}
        same_scale = old.marker[0] == new.marker[0]
        if same_scale and new.marker[1] > old.marker[1]:
            marker_ok = True
        elif signals.get("name_family") or (
                similarity is not None and similarity >= threshold):
            reason = (
                "version markers do not advance with the dates "
                f"({old.marker[0]}:{old.marker[1]} dated {old.valid_date} vs "
                f"{new.marker[0]}:{new.marker[1]} dated {new.valid_date})"
                if same_scale else
                "version markers are on different scales "
                f"({old.marker[0]} vs {new.marker[0]})")
            return False, {"verdict": "ambiguous", "reason": reason,
                           "signals": signals}
    if marker_ok:
        signals["version_advance"] = {"old": old.marker[1], "new": new.marker[1]}
    return marker_ok, None


def analyze(old: "NoteView", new: "NoteView", *, similarity: float | None,
            threshold: float) -> dict[str, Any]:
    """Verdict for ONE oriented pair (``new`` is the later-dated side).

    Returns ``{"verdict": "propose"|"ambiguous"|"skip", "reason": str,
    "signals": {...}}``. ``signals`` records WHICH signals fired and their
    values — evidence the owner sees, never a bare confidence number.
    """
    signals, early = _direction_signals(old, new)
    if early:
        return early
    if not _add_family_signals(old, new, signals):
        return {"verdict": "skip",
                "reason": "no HOST-VERIFIED shared conversation or sender, and "
                          "no shared document name on two notes without one",
                "signals": signals}
    marker_ok, early = _marker_result(old, new, signals, similarity, threshold)
    if early:
        return early
    if similarity is not None:
        signals["similarity"] = round(similarity, 6)
    near_dup = similarity is not None and similarity >= threshold
    if not (signals.get("name_family") or marker_ok):
        return {"verdict": "skip",
                "reason": "no name-identity signal (different documents in one "
                          "thread look exactly like this)", "signals": signals}
    if not (near_dup or marker_ok):
        return {"verdict": "skip",
                "reason": f"content similarity {similarity!r} below "
                          f"{threshold} and no advancing version marker",
                "signals": signals}
    if near_dup:
        signals["near_duplicate"] = {"score": round(similarity or 0.0, 6),
                                      "threshold": threshold}
    return {"verdict": "propose", "reason": "deduced from email context",
            "signals": signals}


def _has_recent_successor(core: Any, cutoff: str) -> bool:
    return bool(core.index.conn.execute(
        _versionlink._RECENT_SQL, (cutoff,)).fetchone()[0])


def _build_groups(notes: list["NoteView"]) -> dict[tuple[str, str], list[str]]:
    boilerplate = _boilerplate_patterns()
    groups: dict[tuple[str, str], list[str]] = {}
    for note in notes:
        if note.email_claimed:
            for field in ("conversation_id", "sender"):
                value = note.prov.get(field)
                if value:
                    groups.setdefault((field, value), []).append(note.id)
            continue
        if _matches_boilerplate_pattern(note.id, boilerplate):
            continue
        for stem in note.stems:
            groups.setdefault(("stem", stem), []).append(note.id)
    return groups


def _partner_ids(
    note: "NoteView", groups: dict[tuple[str, str], list[str]], by_id: dict[str, "NoteView"],
) -> list[str]:
    if note.email_claimed:
        keys = [("conversation_id", note.prov.get("conversation_id") or ""),
                ("sender", note.prov.get("sender") or "")]
    else:
        keys = [("stem", stem) for stem in sorted(note.stems)]
    partners: list[str] = []
    for key in keys:
        for partner_id in groups.get(key, ()):
            if partner_id != note.id and partner_id not in partners:
                partners.append(partner_id)
    partners.sort(key=lambda partner_id: (
        by_id[partner_id].valid_date, by_id[partner_id].commit_date, partner_id),
        reverse=True)
    return partners


def _skip_pair(old: "NoteView", new: "NoteView", proposed_ids: set[str]) -> bool:
    if old.retired or old.id == new.id:
        return True
    old_family = _versionlink.version_family_key(old.id)
    new_family = _versionlink.version_family_key(new.id)
    if old_family and new_family and old_family[0] == new_family[0]:
        return True
    if old.body_sha and old.body_sha == new.body_sha:
        return True
    if not (old.email_claimed or new.email_claimed) and old.untrusted != new.untrusted:
        return True
    first, second = (old, new) if old.valid_date <= new.valid_date else (new, old)
    return (first.id in proposed_ids or second.id in proposed_ids
            or second.has_predecessor)


def _append_verdict(
    report: dict[str, Any], verdict: dict[str, Any], first: "NoteView", second: "NoteView",
    key: str,
) -> None:
    if verdict["verdict"] == "propose":
        report["candidates"].append({
            "old_id": first.id, "new_id": second.id, "pair_key": key,
            "old_title": first.title, "new_title": second.title,
            "old_sha256": first.content_hash, "new_sha256": second.content_hash,
            "old_classification": first.classification,
            "new_classification": second.classification,
            "signals": verdict["signals"],
        })
    elif verdict["verdict"] == "ambiguous":
        report["ambiguous"].append({
            "old_id": first.id, "new_id": second.id, "pair_key": key,
            "reason": verdict["reason"], "signals": verdict["signals"],
        })


def _process_partner(
    core: Any, report: dict[str, Any], old: "NoteView", new: "NoteView",
    threshold: float, seen: set[str], proposed_ids: set[str],
    cache: dict[str, list[float]],
) -> bool:
    key = _versionlink.pair_key(old.id, new.id)
    if key in seen or _skip_pair(old, new, proposed_ids):
        return False
    if report["pairs_examined"] >= _versionlink.MAX_PAIRS:
        return True
    report["pairs_examined"] += 1
    seen.add(key)
    first, second = (old, new) if old.valid_date <= new.valid_date else (new, old)
    similarity = _versionlink._similarity(core.index, cache, first, second)
    verdict = analyze(first, second, similarity=similarity, threshold=threshold)
    _append_verdict(report, verdict, first, second, key)
    if verdict["verdict"] == "propose":
        proposed_ids.update({first.id, second.id})
    return False


def _generate_report(
    core: Any, notes: list["NoteView"], *, cutoff: str, threshold: float,
    exclude: Iterable[str], report: dict[str, Any],
) -> dict[str, Any]:
    by_id = {note.id: note for note in notes}
    groups = _build_groups(notes)
    cache: dict[str, list[float]] = {}
    seen: set[str] = set(exclude)
    proposed_ids: set[str] = set()
    for new in sorted(notes, key=lambda note: (note.commit_date, note.id), reverse=True):
        if new.commit_date < cutoff or new.retired:
            continue
        partners = _partner_ids(new, groups, by_id)
        for partner_id in partners[:_versionlink.MAX_PARTNERS]:
            if _process_partner(
                    core, report, by_id[partner_id], new, threshold, seen,
                    proposed_ids, cache):
                report["truncated"] = True
                break
        if report["truncated"]:
            break
    return report


def generate(core: Any, *, cutoff: str, exclude: Iterable[str] = (),
             threshold: float | None = None) -> dict[str, Any]:
    """Nominate version-link candidates over the in-scope corpus.

    ``cutoff`` is an ISO date: only notes committed on or after it are
    considered as SUCCESSORS (predecessors may be arbitrarily old). ``exclude``
    is the set of :func:`pair_key` values already decided (proposed, rejected,
    applied or declined) — never re-asked.

    Pure: reads the index + note files, writes nothing.
    """
    selected_threshold = (
        _versionlink.min_similarity() if threshold is None else threshold)
    report: dict[str, Any] = {"candidates": [], "ambiguous": [],
                              "pairs_examined": 0, "truncated": False}
    if not _has_recent_successor(core, cutoff):
        return report
    notes = _versionlink._load(core)
    return _generate_report(
        core, notes, cutoff=cutoff, threshold=selected_threshold,
        exclude=exclude, report=report)


from . import versionlink as _versionlink  # noqa: E402

NoteView = _versionlink.NoteView
