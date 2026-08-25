"""DLV-04/DLV-10 — planning the shelf's copies and moves, then applying them.

Split out of ``deliverables_sync`` at the 500-LOC file ratchet, along the seam
DLV-10 introduced: that module decides what the shelf should CONTAIN, this one
decides what has to HAPPEN to each path and then does it. The two passes are
separate on purpose — a blast-radius cap you can only enforce half way through
a walk is not a cap, and the plan is the thing the cap is applied to.

THE TARGET AUDIT IS UNCONDITIONAL. The source fingerprint decides whether a
payload is re-copied; it never decides whether the target is LOOKED at. On an
unchanged vault every path takes the cheap path, so a skip that returned before
hashing the target would leave an edited, deleted or hand-replaced shelf file
undetected forever.

**Nothing here deletes.** A superseded, retired or displaced copy MOVES to
``<shelf>/_previous/<run-id>/<project>/``. This vault already retires by moving
(quarantine ``_resolved/``), and the move retires the irreversible half of every
guard at once: a stale census, a forged ledger row and an owner edit all cost a
recoverable move rather than a destroyed file.
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any

from . import deliverables_ledger as L, deliverables_readme as _readme

#: How many files one run may MOVE. Every move is recoverable — the copy goes
#: to ``_previous/``, never to /dev/null — but a recoverable action still needs
#: a blast radius: 1,300 files relocated in one run is an incident even when
#: nothing is lost, and the causes that would do it (a half-read vault, a
#: ledger read against the wrong shelf) all look like an ordinary run from
#: inside. Over the cap the run moves NOTHING and reports it.
MAX_MOVES_ENV = "BRAIN_SHELF_MAX_MOVES"
MOVE_CAP_FRACTION = 5          # a fifth of the ledger…
MIN_MOVE_CAP = 5               # …but never a cap so small a small shelf wedges


def move_cap(ledger_entries: int) -> int:
    """How many files this run may MOVE. ``$BRAIN_SHELF_MAX_MOVES`` overrides."""
    raw = os.environ.get(MAX_MOVES_ENV, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass          # a typo must not silently uncap the run
    return max(MIN_MOVE_CAP, ledger_entries // MOVE_CAP_FRACTION)


def materialize(
    root: Path, shelf: Path,
    wanted: dict[str, dict[str, Any]],
    prior: dict[str, dict[str, Any]],
    building: dict[str, dict[str, Any]],
    bad_perms: list[str],
    unhealthy: set[str],
    run_id: str,
    ambiguous: list[dict[str, Any]],
    empty_census: bool = False,
) -> dict[str, Any]:
    """Plan every action, hold the run's moves if it is refusing to make them,
    then apply what survived. Planning and applying are separate passes because
    a cap you can only enforce halfway through is not a cap."""
    plan = _plan_actions(shelf, wanted, prior, building, unhealthy)
    cap = move_cap(len(prior))
    moves = len(plan["replace"]) + len(plan["retire"])
    held = _hold_reason(moves, cap, empty_census, bool(prior))

    final: dict[str, dict[str, Any]] = {}
    displaced: list[dict[str, str]] = []
    copied = _apply_plan(shelf, wanted, prior, building, plan, final,
                         displaced, bad_perms, run_id, bool(held))

    L.store_ledger(root, shelf, final)
    if not L.secure(L.ledger_path(root), L.FILE_MODE):
        bad_perms.append(str(L.ledger_path(root)))
    L.publish_manifest(shelf, final, bad_perms)
    previous = L.previous_stats(shelf)
    _readme.publish(shelf, final, ambiguous, previous, bad_perms)
    known = {unicodedata.normalize("NFC", rel) for rel in final}
    unknown = [p.relative_to(shelf).as_posix() for p in L.shelf_files(shelf)
               if unicodedata.normalize("NFC", p.relative_to(shelf).as_posix())
               not in known]
    return {
        "copied": copied, "current": [rel for rel, _t in plan["current"]],
        "displaced": displaced, "diverged": plan["diverged"],
        "unknown": unknown, "previous": previous,
        "permission_failures": sorted(set(bad_perms)),
        "ledger_entries": len(final),
        "moves_planned": moves, "move_cap": cap, "moves_held": held,
    }


#: Why one run made no moves. "" is the ordinary case.
HELD_BY_CAP = "cap"
HELD_BY_EMPTY_CENSUS = "empty-census"


def _hold_reason(moves: int, cap: int, empty_census: bool, had_ledger: bool) -> str:
    """Whether this run may move anything, and if not, why not.

    **Zero deliverables over a non-empty ledger is the signature of a read that
    FAILED**, not of a vault that emptied: a half-mounted volume, a permission
    flip, a scan that raised and was swallowed. Acting on it would retire every
    live copy on the shelf in one run — recoverable, since retirement is a move,
    but exactly the incident the blast-radius rule exists to prevent.

    It holds the MOVES and not the RUN. The audit still has to happen: an
    edited or hand-dropped shelf file must still be found, the permissions must
    still be re-proved, and the README must still be republished. Skipping the
    whole run to be careful would turn one guard off by way of another.

    An EXPLICIT ``$BRAIN_SHELF_MAX_MOVES`` lifts the empty-census hold, because
    without an escape the guard wedges a small shelf permanently: a vault with
    one deliverable that is legitimately retired reads zero forever, and an
    alarm that can never be satisfied is one nobody reads. Setting the variable
    is the owner stating the blast radius they expect for that run.
    """
    if empty_census and had_ledger and not os.environ.get(MAX_MOVES_ENV, "").strip():
        return HELD_BY_EMPTY_CENSUS
    return HELD_BY_CAP if moves > cap else ""


def _plan_actions(
    shelf: Path, wanted: dict[str, dict[str, Any]],
    prior: dict[str, dict[str, Any]], building: dict[str, dict[str, Any]],
    unhealthy: set[str],
) -> dict[str, list[Any]]:
    """Decide what each path needs, WITHOUT touching one of them.

    THE TARGET AUDIT IS UNCONDITIONAL. The source fingerprint decides whether a
    payload is re-copied; it never decides whether the target is LOOKED at. A
    cheap-skip that returns before hashing the target would leave an edited,
    deleted or hand-replaced shelf file undetected forever, because on an
    unchanged vault that is every run.
    """
    out: dict[str, list[Any]] = {"fresh": [], "replace": [], "current": [],
                                 "diverged": [], "retire": [], "hold": []}
    for rel, entry in sorted(wanted.items()):
        target = L.safe_relpath(rel, shelf)
        if target is None:
            out["diverged"].append({"path": rel,
                                    "reason": "refused by path containment"})
            continue
        if not target.exists():
            out["fresh"].append((rel, target))
            continue
        on_disk = L.sha256_file(target)
        if on_disk == entry["sha256"]:
            out["current"].append((rel, target))
        elif rel not in prior or on_disk not in L.owned_shas(building[rel]):
            # An owner edit, or a hand-dropped file sitting exactly where this
            # deliverable wants to go. Touch NEITHER, and carry any existing
            # ledger row forward UNCHANGED so the divergence stays visible next
            # run instead of being blessed into the record.
            out["diverged"].append({"path": rel,
                                    "reason": "on-disk bytes are not the fold's"})
            out["hold"].append(rel)
        else:
            out["replace"].append((rel, target, on_disk))
    _plan_retirements(shelf, wanted, prior, unhealthy, out)
    return out


def _plan_retirements(
    shelf: Path, wanted: dict[str, dict[str, Any]],
    prior: dict[str, dict[str, Any]], unhealthy: set[str],
    out: dict[str, list[Any]],
) -> None:
    """Every copy the census no longer wants, and why some of them stay.

    ``unhealthy`` holds the notes whose payload this run could not read at all
    (gone mid-run, or an unresolved source anchor). They are still deliverables
    the census wants — the BYTES were unavailable, not the note — so their live
    copies stay put and their ledger rows carry forward unchanged.
    """
    for rel, entry in sorted(prior.items()):
        if rel in wanted:
            continue
        if entry.get("note_id") in unhealthy:
            out["hold"].append(rel)
            continue
        target = L.safe_relpath(rel, shelf)
        if target is None or not target.exists():
            continue
        if L.sha256_file(target) not in L.owned_shas(entry):
            out["diverged"].append({"path": rel,
                                    "reason": "on-disk bytes are not the fold's"})
            out["hold"].append(rel)
            continue
        out["retire"].append((rel, target, entry["sha256"]))


def _apply_plan(
    shelf: Path, wanted: dict[str, dict[str, Any]],
    prior: dict[str, dict[str, Any]], building: dict[str, dict[str, Any]],
    plan: dict[str, list[Any]], final: dict[str, dict[str, Any]],
    displaced: list[dict[str, str]], bad_perms: list[str], run_id: str,
    held: bool,
) -> list[str]:
    """Carry the plan out. Copies into a FREE path always run; the moves are
    the only thing a hold withholds, because the move is the destructive-
    looking half even when it destroys nothing."""
    copied: list[str] = []
    for rel in plan["hold"]:
        if rel in prior:
            final[rel] = prior[rel]
    for rel, target in plan["current"]:
        # A superseded hash is dropped HERE and not only on the copy path: the
        # `current` verdict means the swap this row was journalling is finished,
        # so carrying `prior_sha256` forward made a crash-era hash owned
        # forever — and the ledger then recognised those stale bytes, written
        # back by hand, as its own and replaced them instead of refusing.
        final[rel] = _settled(building[rel])
        if not L.secure(target, L.FILE_MODE):
            bad_perms.append(str(target))
    for rel, target in plan["fresh"]:
        _write(wanted[rel], target, rel, final, building, bad_perms)
        copied.append(rel)
    if held:
        for rel, *_rest in plan["replace"] + plan["retire"]:
            if rel in prior:
                final[rel] = prior[rel]
        return copied
    for rel, target, on_disk in plan["replace"]:
        # A new version of the SAME deliverable. Nothing on the shelf is ever
        # deleted, so the outgoing bytes MOVE to `_previous/` before the new
        # ones land — os.replace alone would destroy them in place.
        displaced.append({"path": rel,
                          "moved_to": L.displace(target, shelf, rel, on_disk,
                                                 run_id)})
        _write(wanted[rel], target, rel, final, building, bad_perms)
        copied.append(rel)
    for rel, target, sha in plan["retire"]:
        displaced.append({"path": rel,
                          "moved_to": L.displace(target, shelf, rel, sha, run_id)})
    return copied


def _settled(row: dict[str, Any]) -> dict[str, Any]:
    """One ledger row with its in-flight bookkeeping dropped."""
    return {k: v for k, v in row.items() if k != "prior_sha256"}


def _write(entry: dict[str, Any], target: Path, rel: str,
           final: dict[str, dict[str, Any]],
           building: dict[str, dict[str, Any]], bad_perms: list[str]) -> None:
    L.copy_into(Path(entry["payload"]), target)
    final[rel] = _settled(building[rel])
    if not L.secure(target, L.FILE_MODE):
        bad_perms.append(str(target))
