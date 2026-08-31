"""The one scanner for "a note body is recoverable from inside this folder".

Split out of :mod:`brain.cowork_staging` on 2026-08-29 along the real seam: this
module ANSWERS the question, ``cowork_staging`` DECIDES what to do about the
answer (refuse a staging call) and :mod:`brain.doctor_mount_leak` reports it as a
recurring check. One canonical import path, no re-export shim.

Seven artefact classes, because a check that looks only for a vault TREE stays
green next to an 824 MB ``snapshot/index.snapshot.sqlite``, and a check that
looks only under ``brain/`` and ``raw/`` stays green next to 46 note-shaped
Markdown files in a folder called ``migration/``. Both were MEASURED on the
live reference mount, not imagined:

- ``vault_tree`` --- a note zone (``brain/``/``raw/``) holding at least one ``.md``
- ``snapshot`` --- a published read-only snapshot database
- ``derived_index`` --- any sqlite database with a note ``body`` column
- ``staged_original`` --- a document staged out of ``vault/raw/originals``
- ``escaping_symlink`` --- a link out of the workspace whose target yields bodies
- ``note_body_dump`` --- note-shaped Markdown ANYWHERE, judged on frontmatter
- ``deliverables_shelf`` --- the ADR-0010 shelf of archived originals

``snapshot`` and ``derived_index`` are told apart by PATH, not shape --- the same
query proves both carry bodies, so a renamed snapshot is still caught.

**Why the last two exist, measured 2026-08-29 on the live reference mount.** With
five classes the scanner returned three findings, all of which the cutover MOVES.
So the moment it ran the check would have gone GREEN while
``<workspace>/migration/`` (46 of 46 ``.md`` with vault note frontmatter) and
``<workspace>/brain-deliverables/`` (83 MB, header declaring "Highest
classification here: MNPI") sat on the mount, readable with ``cat``. An all-clear
reachable while the data is still there is the failure this plan was hardened
against.

**On leases.** There is no lease mechanism to be active or abandoned: s05b was
retired by owner ruling 2026-08-27 and s05 built the DECISION half only. So every
staged original inside a workspace IS abandoned --- nothing owns it, nothing will
reclaim it. If leases are ever built, the active/abandoned split belongs here.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .cowork_leak_artifact import Artifact
from .cowork_leak_notes import _is_note_file, _scan_note_dumps
from .cowork_leak_databases import (
    _classify_db,
    _looks_like_sqlite,
    _sqlite_body_rows,
    _sqlite_candidates,
)

__all__ = ["Artifact", "recoverable_artifacts"]

# THE WALK IS UNBOUNDED AND UNPRUNED, and that is a correctness decision with a
# measurement behind it. Until 2026-08-29 this module capped the walk at depth 6
# and refused to descend into ``model/vendor/bin/engine/__pycache__/.git/
# node_modules``, both justified as cost control. The adversarial round proved
# both were free all-clears: a note placed at depth 7, or anywhere under a
# directory someone named ``vendor``, returned NO FINDINGS while ``cat`` still
# printed the body. A gate you evade by choosing a directory name is not a gate.
#
# The cost that justified them does not exist. MEASURED on the live 3.8 GB
# reference mount, 2026-08-29, same process, same cache state:
#
#     pruned + capped at depth 6 : 1,962 dirs,  9,718 files, 0.48s
#     uncapped, still pruned     : 1,964 dirs,  9,719 files, 0.20s
#     UNCAPPED AND UNPRUNED      : 3,011 dirs, 19,325 files, 0.35s
#
# Half the mount was never being looked at, and looking at all of it was faster
# than the bookkeeping that avoided it.

def _walk(root: Path):
    """Yield (dirpath, dirnames, filenames) for EVERY directory under ``root``.

    No depth cap and no prune list --- see the constants above for the measured
    reason. ``followlinks=False`` still applies: a symlinked directory is not
    descended here, it is handled by :func:`_scan_symlinks`, which recurses the
    whole scanner into the target instead.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        yield Path(dirpath), dirnames, filenames


def _has_text(zone: Path) -> bool:
    """True if ``zone`` holds at least one note-bearing file at any depth.

    Short-circuits on the first hit: a note zone with 4,131 notes costs the same
    as one with a single note.

    ANY Markdown counts, frontmatter or not --- inside a directory the vault
    itself calls ``brain``/``raw``, a body with its frontmatter stripped is
    still a body. A ``.txt`` has to prove itself, because "leave a README saying
    the vault moved" is a real thing an operator does in an emptied zone and it
    must not read as a leak.
    """
    for dirpath, _dirnames, filenames in _walk(zone):
        for name in filenames:
            suffix = Path(name).suffix.lower()
            if suffix in (".md", ".markdown"):
                return True
            if suffix == ".txt" and _is_note_file(dirpath / name):
                return True
    return False


def _zone_yields_bodies(zone: Path) -> bool:
    """Is this directory called ``brain``/``raw`` actually a vault note zone?

    Any Markdown or text under it counts --- deliberately the LOOSE rule, and
    that is load-bearing: a raw body dumped there with its frontmatter stripped
    is still a leak, and ``tests/test_direct_file_read_relocated.py`` pins that
    this scanner and a plain content search can never disagree about a marker.

    The one exclusion is a PYTHON PACKAGE. ``vault/.brain/engine/brain`` is the
    engine's own source tree; it is a directory called ``brain``, it ships
    Markdown (``_assets/templates/*.md``), and the moment the walk stopped
    pruning ``engine/`` (see the constants above) it became a finding on BOTH
    live workspaces. ``__init__.py`` is a fact about the directory's CONTENT,
    not a path or a name someone can choose, and no vault note zone has ever
    contained one. A row an operator learns to ignore is the same as no row, and
    this module has already paid that price once (see
    :func:`_yields_note_bodies`).
    """
    if (zone / "__init__.py").exists():
        return False
    return _has_text(zone)

_NOTE_ZONES = ("brain", "raw")


def _yields_note_bodies(target: Path, visited: set[Path],
                        deadline: float | None = None) -> bool:
    """True if an ordinary read of ``target`` recovers a NOTE body.

    Deliberately the SAME proof the other classes use, not "the target holds a
    ``.md`` file". MEASURED on the live mount 2026-08-29: the looser rule
    reported 10 findings on ``<workspace>/.agents/skills/*``, symlinks into
    this repo's own skill definitions --- Markdown, but not vault content. Ten
    false positives out of thirteen findings is a row an operator learns to
    ignore, which is the same as not having one.

    A DIRECTORY target is handed to the whole scanner recursively, not to a
    reduced version of it. The reduced version was the defect: it looked only
    for directories NAMED ``brain``/``raw`` and for databases, so a symlink to
    an ordinary directory holding note-shaped Markdown reported clean while
    ``cat`` through the link printed the body (adversarial round, 2026-08-29).
    ``visited`` carries the resolved roots already being scanned, so a symlink
    loop terminates instead of recursing forever.
    """
    if target.is_file():
        if _is_note_file(target):
            return True
        return bool(_sqlite_body_rows(target))
    if not target.is_dir():
        return False
    return bool(_recoverable(target, visited, deadline))


def _scan_note_zones(here: Path, dirnames: list[str]) -> list[Artifact]:
    """(1) a note zone: ``brain/`` or ``raw/`` holding at least one ``.md``."""
    out = []
    for zone_name in _NOTE_ZONES:
        if zone_name not in dirnames:
            continue
        zone = here / zone_name
        # A SYMLINKED note zone is reported by :func:`_scan_symlinks` instead:
        # same finding, but the remediation is "remove the link", not "delete
        # the tree" --- and the tree may be the real vault.
        if zone.is_symlink() or not _zone_yields_bodies(zone):
            continue
        out.append(Artifact("vault_tree", zone,
                            "note zone: holds .md note bodies readable by path, "
                            "by glob and by content search"))
    return out




def _scan_databases(here: Path, filenames: list[str]) -> list[Artifact]:
    """(2)+(3) a snapshot or a derived index.

    Proven by QUERYING for bodies, so an unrelated sqlite file in the workspace
    -- a COS ledger, a browser profile -- is never reported.
    """
    out = []
    for db in _sqlite_candidates(here, filenames):
        rows = _sqlite_body_rows(db)
        if not rows:
            continue
        out.append(Artifact(_classify_db(db), db,
                            f"{rows} note rows carry a non-empty body; "
                            "`strings` recovers them without sqlite"))
    return out


def _scan_symlinks(root: Path, here: Path, names: list[str],
                   visited: set[Path],
                   deadline: float | None = None) -> list[Artifact]:
    """(4) a symlink OUT of the workspace whose target yields note bodies.

    The move is undone by one of these, and neither ``Path.rglob`` nor
    ``os.walk(followlinks=False)`` descends a symlinked directory -- so a scan
    that skipped this would report clean against a workspace one ``cat`` away
    from the corpus. s01 JOB 4 already flags the real one:
    ``<workspace>/AGENTS.md`` has to be retargeted when ``.brain`` moves.
    """
    out = []
    for name in names:
        link = here / name
        if not link.is_symlink():
            continue
        try:
            target = link.resolve(strict=True)
        except OSError:
            continue
        if root == target or root in target.parents:
            continue  # stays inside the workspace: not an escape
        if not _yields_note_bodies(target, visited, deadline):
            continue
        out.append(Artifact("escaping_symlink", link,
                            f"symlink out of the workspace to {target}, which "
                            "yields note bodies through an ordinary read"))
    return out


def _scan_staged_originals(here: Path, filenames: list[str]) -> list[Artifact]:
    """(5) an abandoned staged original.

    No lease mechanism exists (see the module docstring), so anything found
    here is abandoned by definition: nothing owns it, nothing reclaims it.
    """
    if here.name != "originals":
        return []
    n = sum(1 for f in filenames if not f.startswith("."))
    if not n:
        return []
    return [Artifact("staged_original", here,
                     f"{n} staged original document(s); no lease owns them "
                     "and no reaper reclaims them (s05b retired 2026-08-27)")]


# EVERY candidate file in a directory is inspected, not a sample of three. The
# sample was a free all-clear and it was not even deterministic: it took
# ``filenames[:3]`` in `os.walk` order, which is readdir order, so which three
# files decided the verdict depended on the filesystem. The adversarial round
# hid a note as the FOURTH `.md` in a directory and the scan reported clean.
# MEASURED warm on the live mount: reading 4 KiB of all 6,035 candidate files
# costs 0.95s, and the scan stops at the first hit in each directory, so a real
# dump costs one read.
#
# A note body is not always called `.md`: the 2026-08-29 round renamed one to
# `.txt` and it became invisible. That round was answered by ADDING `.txt` to a
# suffix list, which is the same gate one rename further out -- and the
# 2026-08-30 adversarial round walked straight through it with
# `backup/note.md.bak`, a file an editor or a pre-move backup produces without
# anyone intending anything. Reproduced: 0 artefacts from the scanner while
# `grep -r` printed the Restricted body.
#
# SO THE SUFFIX IS NO LONGER A GATE. It is only an ORDERING: the suffixes below
# are tried first because they are where a note usually is, and every other
# regular file is tried after. The discriminator is the content -- the file must
# OPEN with `---` and carry note frontmatter -- and content is the one property
# a rename cannot change.
#
# COST. The first read is 4 bytes, not 4 KiB: a file that does not open with
# `---` is rejected without a second syscall, and the sibling sqlite sniff
# already opens every file >=512 bytes for a 16-byte read. The full 4 KiB is
# read only for a file that already looks like frontmatter. Measured warm on the
# live mount before this change: 4 KiB from all 6,035 suffix-matching candidates
# cost 0.95s, and the scan still stops at the first hit in each directory.
_SHELF_DIR = "brain-deliverables"


def _scan_deliverables_shelf(here: Path, dirnames: list[str]) -> list[Artifact]:
    """(7) the ADR-0010 deliverables shelf, whose payload is the originals."""
    if _SHELF_DIR not in dirnames:
        return []
    shelf = here / _SHELF_DIR
    n = sum(1 for _d, _n, files in _walk(shelf) for f in files if not f.startswith("."))
    if not n:
        return []
    return [Artifact("deliverables_shelf", shelf,
                     f"{n} generated file(s): the ADR-0010 shelf holds the "
                     "archived ORIGINAL behind each note, and its own header "
                     "declares the highest classification present")]


class ScanBudgetExceeded(Exception):
    """The walk ran out of its budget. Carries what was found so far: a partial
    scan that already found a leak is still a true finding; only an EMPTY
    partial scan is uninformative."""

    def __init__(self, found: list[Artifact], overshoot: float) -> None:
        super().__init__(f"scan exceeded its budget by {overshoot:.1f}s")
        self.found = found
        self.overshoot = overshoot


def recoverable_artifacts(workspace: str | os.PathLike[str],
                          budget_seconds: float | None = None) -> list[Artifact]:
    """Every artefact inside ``workspace`` from which a note body is recoverable.

    Empty list == a clean layout. This asserts on the ABSENCE OF THE DATA, not
    on the absence of a directory: a re-mounted vault, a republished snapshot or
    a rebuilt index each produce a finding on their own, and an empty
    ``vault/brain/`` directory left behind by the move does NOT -- so the check
    cannot be satisfied by tidying up directory names.

    ``budget_seconds`` bounds the walk, because this scanner is uncapped and
    unpruned by design (that is what closed the five measured bypasses) and so
    magic-sniffs every file: 19 325 of them on the live reference mount, on
    every ``maintain`` via ``run_doctor``. Measured 2026-08-29 --- unbudgeted it
    blew a 300 s pytest timeout there and failed six unrelated tests.

    Exceeding it RAISES rather than returning a short list: an unfinished scan
    must never be readable as an all-clear, which is the "clean because the
    input was empty" bug this module exists to prevent.
    """
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    return _recoverable(Path(workspace), set(), deadline)


def _recoverable(workspace: Path, visited: set[Path],
                 deadline: float | None = None) -> list[Artifact]:
    """:func:`recoverable_artifacts` plus the symlink-loop guard.

    ``visited`` holds the resolved roots already on the stack. An escaping
    symlink hands its target to this same function (see
    :func:`_yields_note_bodies`), so without it a link pointing back into a
    directory already being scanned recurses until the interpreter gives up.
    """
    if not workspace.is_dir():
        return []
    root = workspace.resolve()
    if root in visited:
        return []
    visited = visited | {root}
    found: list[Artifact] = []
    seen: set[Path] = set()
    # Note zones already reported, so `_scan_note_dumps` does not report each of
    # their subdirectories again. `os.walk` is top-down, so a zone is always
    # recorded here before the walk descends into it.
    covered: set[Path] = set()
    for here, dirnames, filenames in _walk(root):
        if deadline is not None and time.monotonic() > deadline:
            raise ScanBudgetExceeded(found, time.monotonic() - deadline)
        zones = _scan_note_zones(here, dirnames)
        covered.update(zone.path for zone in zones)
        try:
            here_found = (zones
                          + _scan_databases(here, filenames)
                          + _scan_symlinks(root, here,
                                           list(dirnames) + list(filenames),
                                           visited, deadline)
                          + _scan_staged_originals(here, filenames)
                          + _scan_note_dumps(root, here, filenames, covered)
                          + _scan_deliverables_shelf(here, dirnames))
        except ScanBudgetExceeded as exc:
            # A NESTED scan (a symlink hands its target back here) timed out;
            # its exception carries only the INNER findings, so letting it fly
            # discards this level's and turns a real leak into "could not tell".
            raise ScanBudgetExceeded(found + exc.found, exc.overshoot) from None
        for artifact in here_found:
            if artifact.path in seen:
                continue
            seen.add(artifact.path)
            found.append(artifact)
    return sorted(found, key=lambda a: (a.kind, str(a.path)))
