"""DLV-10 — the ``_previous/`` retention window, and why it may not delete a
sole copy.

The shelf never deletes: a superseded or retired copy MOVES to
``<shelf>/_previous/<run-id>/<project>/``. A bin that only ever grows is a real
cost, so there is a window — ``$BRAIN_SHELF_PREVIOUS_DAYS``, default 90 — after
which a retired copy is ELIGIBLE to go.

**Eligible is not the same as deleted, and that distinction is the whole
module.** An entry reaches ``_previous/`` when its note was superseded OR is
GONE, and a Markdown deliverable's payload is the note's own file — so for a
note deleted from the vault, the ``_previous/`` copy can be the LAST SURVIVING
BYTES. ``AGENTS.md`` reserves deleting a possibly-sole copy for the owner, and a
design that advertises itself as recoverable must not carry a timer that quietly
makes it not. So an aged entry is pruned ONLY when byte-identical content still
exists in the vault or on the live shelf; anything else is RETAINED past its
window and enqueued as one owner decision (``shelf:sole-copy``).

The age is read from the ``_previous/<run-id>/`` directory name, not from the
file's mtime: mtime records when the bytes were COPIED onto the shelf, which can
be years before they were moved aside. A run-id directory whose name does not
parse starts no clock at all — the shelf is a folder the owner can write to, and
an unrecognised name there is hand-made or damaged, neither of which is a thing
to age out.

It rides the existing retention fold rather than a new scheduled task
(``AGENTS.md`` §6: recurring vault work joins the umbrella).
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

from . import config, deliverables_ledger as L, deliverables_shelf as shelf_mod

PREVIOUS_DAYS_ENV = "BRAIN_SHELF_PREVIOUS_DAYS"
DEFAULT_PREVIOUS_DAYS = 90

#: Where a surviving twin may be found. Deliberately the two CONTENT zones and
#: not the whole vault: `.brain/` holds derived runtime copies, and counting one
#: of those as "the bytes still exist" would make the prune delete on the
#: strength of a cache.
_VAULT_ZONES = ("raw", "brain")


def window_days() -> int:
    raw = os.environ.get(PREVIOUS_DAYS_ENV, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_PREVIOUS_DAYS


def prune(vault: str | os.PathLike[str] | None = None,
          today: datetime.date | None = None) -> dict[str, Any]:
    """Delete aged ``_previous/`` copies that are provably not the last one.

    ``today`` is UTC by default and must stay UTC: the run-id namespaces it is
    compared against are stamped in UTC, and measuring a local date against a
    UTC one is a bug this repository has already shipped once.
    """
    root = config.vault_root(vault)
    resolved = shelf_mod.resolve(root)
    if not resolved.ok:
        assert resolved.refusal is not None
        return {"refused": True, "pruned": [], "retained": [],
                "files": 0, "bytes": 0, "window_days": window_days(),
                **resolved.refusal.as_action_required()}
    shelf = resolved.path
    assert shelf is not None
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = today - datetime.timedelta(days=window_days())

    aged = [path for path, run in L.previous_files(shelf)
            if (d := L.run_id_date(run)) is not None and d <= cutoff]
    pruned, retained = _dispose(root, shelf, aged)
    if pruned:
        _drop_empty_dirs(shelf)
    stats = L.previous_stats(shelf)
    return {"shelf": str(shelf), "window_days": window_days(),
            "pruned": pruned, "retained": retained, **stats}


def _dispose(root: Path, shelf: Path,
             aged: list[Path]) -> tuple[list[str], list[str]]:
    """Prune what has a surviving twin, retain what does not."""
    if not aged:
        return [], []
    survivors = _surviving_hashes(root, shelf, aged)
    pruned: list[str] = []
    retained: list[str] = []
    for path in aged:
        rel = path.relative_to(shelf).as_posix()
        try:
            digest = L.sha256_file(path)
        except OSError:
            retained.append(rel)      # unreadable is never a licence to delete
            continue
        if digest not in survivors:
            retained.append(rel)
            continue
        try:
            path.unlink()
            pruned.append(rel)
        except OSError:
            retained.append(rel)
    return pruned, retained


def _surviving_hashes(root: Path, shelf: Path, aged: list[Path]) -> set[str]:
    """Every hash that proves "these bytes still exist somewhere else".

    The live shelf is hashed from DISK rather than read out of the ledger: the
    ledger records what the fold last CLAIMED, and a claim is not a copy.

    The vault side is size-filtered first. Hashing every note and archived
    original nightly to age out a handful of retired copies would be a real
    cost for a lane that finds nothing at all for the first 90 days of a
    vault's life; only files whose size matches an aged candidate can possibly
    be byte-identical to one.
    """
    out: set[str] = set()
    for path in L.shelf_files(shelf):
        try:
            out.add(L.sha256_file(path))
        except OSError:
            pass
    sizes: set[int] = set()
    for path in aged:
        try:
            sizes.add(path.stat().st_size)
        except OSError:
            pass
    for zone in _VAULT_ZONES:
        for path in (root / zone).rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size not in sizes:
                    continue
                out.add(L.sha256_file(path))
            except OSError:
                continue
    return out


def _drop_empty_dirs(shelf: Path) -> None:
    """Remove the directory husks a prune leaves behind. Empty directories
    only — this never removes a file, and never ``_previous/`` itself."""
    root = shelf / L.PREVIOUS_DIRNAME
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            pass
