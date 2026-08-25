"""DLV-04 — the shelf fold's core: ONE census, and the copies it implies.

The fold mixin that fires this on every nightly lives in ``folds/deliverables.py``;
the host-private ledger, the containment rule and the filesystem primitives live
in ``deliverables_ledger``. Everything here is testable without a ``BrainCore``.

**The census is enumerated from FRONTMATTER, never from the index.** The obvious
read — ``bases_query(..., latest_only=True)`` — defaults to ``k=50`` behind a
hard SQL ``LIMIT`` (``core/_retrieval.py:84``, ``index/_tools.py:148``), so
"every deliverable" silently truncates at fifty and an acceptance check running
the same command falsely passes. It also cannot filter on ``deliverable`` at all
(the column allowlist is fixed at ``index/_tools.py:126-127``). Markdown + YAML
is the source of truth and the index a derived cache, so ``scan_vault`` +
frontmatter is both the more correct read and the one that cannot truncate or
read zero mid-rebuild. :func:`census` is the SINGLE public enumeration — the
fold, the CLI (``brain shelf census``) and the acceptance check all call it, so
no capped surface exists anywhere in the chain. Measured on a synthetic
4,400-note vault: ~1.2 s warm, ~10.4 s cold — comfortably cheap at the hourly
cadence, where the umbrella's own sync has already warmed the page cache.

**CRASH SAFETY: the ledger is published BEFORE the copies, as a superset.** The
pre-copy write records ``prior u desired``, carrying the outgoing hash of any
entry whose bytes are about to change. That single property is the whole
recovery story: whatever moment the process dies, the ledger on disk already
claims every path the run could have touched, and recognises both the old and
the new bytes at each one. The next run therefore finishes the job instead of
seeing its own half-written output as unknown files and refusing forever. The
post-copy write narrows the ledger back to what actually landed.

**Nothing here deletes.** Planning and applying the copies — and the
never-delete rule, the blast-radius cap and the divergence refusal that ride on
them — live in ``deliverables_materialize``.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import classification as CLS
from . import config, deliverables_ledger as L
from . import deliverables_materialize as _mat
from . import deliverables_shelf as shelf_mod
from . import notes as _notes

#: Where a deliverable with no ``project:`` lands.
UNGROUPED = "ungrouped"

# Re-exported so a caller (the fold, a test, the CLI) reaches ONE spelling of
# each shelf constant rather than importing two modules to say one thing.
DIR_MODE = L.DIR_MODE
FILE_MODE = L.FILE_MODE
PREVIOUS_DIRNAME = L.PREVIOUS_DIRNAME
MANIFEST_FILENAME = L.MANIFEST_FILENAME
README_FILENAME = L.README_FILENAME
LEDGER_VERSION = L.LEDGER_VERSION
MAX_MOVES_ENV = _mat.MAX_MOVES_ENV
HELD_BY_CAP = _mat.HELD_BY_CAP
HELD_BY_EMPTY_CENSUS = _mat.HELD_BY_EMPTY_CENSUS
move_cap = _mat.move_cap
ledger_path = L.ledger_path
load_ledger = L.load_ledger
safe_relpath = L.safe_relpath

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# the census — the one public enumeration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Deliverable:
    """One marked, non-retired deliverable and the bytes the shelf would show."""

    note_id: str
    title: str
    project: str
    project_slug: str
    tier: str
    payload: str          # absolute path to the bytes, "" when unresolved
    payload_kind: str     # "original" | "note" | "unresolved"
    is_latest_version: str
    note_path: str
    updated: str = ""
    problem: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_slug(value: object) -> str:
    """Fold a free-text project name to one path-safe folder name.

    Accents are folded to ASCII FIRST (NFKD, combining marks dropped) rather
    than punched out as separators — this vault's project names are accented,
    and the plain-regex form turns one word into a hyphenated pair while
    leaving its unaccented spelling a different folder entirely.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _SLUG_STRIP.sub("-", text.strip().lower()).strip("-") or UNGROUPED


def _is_marked(note: Any) -> bool:
    return _notes._bitemporal_bool(note.meta.get("deliverable")) == "true"


def _contained_file(candidate: Path, root: Path) -> Path | None:
    """``candidate`` as a regular, non-symlinked file inside ``root``, or None."""
    try:
        resolved = candidate.resolve(strict=False)
        base = root.resolve(strict=False)
    except OSError:
        return None
    if base not in resolved.parents:
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    return candidate


def _payload_of(note: Any, root: Path) -> tuple[Path | None, str, str | None, str]:
    """Resolve (payload, kind, source_tier, problem) for one deliverable.

    ``source_tier`` is ``None`` when no raw payload is involved at all, and the
    raw note's RAW classification value — possibly ``""`` — when one is. The
    distinction is load-bearing: an unlabelled raw note must fail closed to
    MNPI, and collapsing "no raw note" into the same empty string would fail it
    closed for every plain-note deliverable in the vault instead.

    The ``source:``-anchored ``raw/originals`` file when present, else the
    note's own ``.md``. The archived original is the thing an owner actually
    wants on the shelf — the note is the marker.
    """
    anchor = _notes._bitemporal_link(note.meta.get("source"))
    if not anchor.startswith("raw/"):
        return note.path, "note", None, ""
    raw_path = root / "raw" / f"{anchor[len('raw/'):]}.md"
    raw = _notes.load_note(raw_path, root) if raw_path.is_file() else None
    if raw is None:
        return note.path, "note", None, f"source {anchor} is not a readable raw note"
    origin = str(raw.meta.get("origin") or "").strip()
    if not origin:
        return note.path, "note", None, ""
    payload = _contained_file(root / origin, root)
    if payload is None:
        # Never silently fall back to the note here: the anchor NAMES a payload
        # and it is missing, outside the vault, or not a regular file. Shelving
        # the marker instead would look like success.
        return None, "unresolved", raw.classification, (
            f"archived original {origin} is missing, is not a regular file, "
            f"or resolves outside the vault")
    return payload, "original", raw.classification, ""


def _shelf_tier(note_tier: str, source_tier: str | None) -> str:
    """The HIGHER of the two, default-denied.

    The payload is the raw original, so an Internal note anchoring an MNPI
    original would otherwise put MNPI bytes on the shelf labelled Internal —
    inverting the one control that makes the label useful. ``CLS.rank`` already
    fails an unlabelled or unrecognised value to MNPI, so a malformed
    classification on either side lands at the most restrictive tier rather
    than being trusted.
    """
    ranks = [CLS.rank(note_tier)]
    if source_tier is not None:
        ranks.append(CLS.rank(source_tier))
    return CLS.TIERS[max(ranks)]


def census(vault: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Every marked, non-retired deliverable in the vault. The ONE enumeration.

    "Latest" on this engine means "not explicitly retired", not "the newest of a
    family": supersession beyond ``…-vN`` is proposed and never applied
    (AGENTS.md), so two hand-saved versions of one deck are both latest and both
    reach the shelf. Reporting that ambiguity is the reporting fold's job; the
    census states the raw ``is_latest_version`` and does not guess.
    """
    root = config.vault_root(vault)
    entries: list[Deliverable] = []
    for note in _notes.scan_vault(root):
        if note.zone != "brain" or not _is_marked(note):
            continue
        if note.is_latest_version == "false":
            continue
        payload, kind, source_tier, problem = _payload_of(note, root)
        project = _notes._bitemporal_link(note.meta.get("project"))
        entries.append(Deliverable(
            note_id=note.id,
            title=note.title,
            project=project,
            project_slug=project_slug(project),
            tier=_shelf_tier(note.classification, source_tier),
            payload=str(payload) if payload is not None else "",
            payload_kind=kind,
            is_latest_version=note.is_latest_version,
            note_path=str(note.path),
            updated=note.updated,
            problem=problem,
        ))
    entries.sort(key=lambda e: (e.project_slug, e.note_id))
    return {
        "vault": str(root),
        "count": len(entries),
        "entries": [e.as_dict() for e in entries],
    }


# ---------------------------------------------------------------------------
# planning: census -> one target path each
# ---------------------------------------------------------------------------

def plan_targets(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Map each resolvable deliverable to ONE shelf-relative path.

    Returns ``(planned, ambiguous)``. Basename collisions inside one project
    group take a ``(<note-id>)`` suffix on ALL colliding members, so the result
    is deterministic and independent of scan order — the alternative, suffixing
    whichever one arrived second, makes a file's name depend on the order
    ``readdir`` happened to return.

    **The suffix is applied until the key is FREE, not once.** A single pass
    silently dropped a deliverable: a suffixed key ``alpha/report (two).pdf``
    could land on the plain key of a third note whose payload is literally
    named that, and the later assignment simply overwrote the earlier one in
    the dict — census 3, planned 2, and the lost note appeared in no report
    field at all. Note ids are unique, so re-suffixing terminates.

    ``ambiguous`` names every entry that needed a suffix, grouped by the plain
    path they collided on. That is the CUR-01 version-ambiguity surface as
    well: supersession beyond ``…-vN`` is proposed and never applied
    (AGENTS.md), so two hand-saved versions of one deck are both latest, both
    reach the shelf, and collide here. It is REPORTED — into the shelf README —
    and resolved nowhere: picking a winner would be a second, competing
    supersession mechanism.
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["payload_kind"] == "unresolved" or not entry["payload"]:
            continue
        name = unicodedata.normalize("NFC", Path(entry["payload"]).name)
        buckets.setdefault((entry["project_slug"], name), []).append(entry)

    planned: dict[str, dict[str, Any]] = {}
    for (slug, name), members in sorted(buckets.items()):
        for entry in sorted(members, key=lambda e: e["note_id"]):
            final = L.suffixed(name, entry["note_id"]) if len(members) > 1 else name
            key = f"{slug}/{final}"
            while key in planned:
                key = f"{slug}/{L.suffixed(PurePosixPath(key).name, entry['note_id'])}"
            planned[key] = entry
    return planned, _ambiguities(planned)


def _ambiguities(planned: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every entry whose shelf name is not simply its payload's name, grouped
    by the plain path they contended for. ONE derivation covers both the
    same-name-in-one-project family and the rarer suffix-lands-on-a-plain-name
    accident, so there is no second detector to keep in step with the first."""
    grouped: dict[str, list[str]] = {}
    for key, entry in planned.items():
        plain = (f"{entry['project_slug']}/"
                 f"{unicodedata.normalize('NFC', Path(entry['payload']).name)}")
        if key != plain:
            grouped.setdefault(plain, []).append(str(entry["note_id"]))
    return [{"name": name, "note_ids": sorted(ids)}
            for name, ids in sorted(grouped.items())]


# ---------------------------------------------------------------------------
# the fold body
# ---------------------------------------------------------------------------

def sync(vault: str | os.PathLike[str] | None = None,
         run_id: str | None = None) -> dict[str, Any]:
    """Materialize the shelf from the census, safely and recoverably.

    Never raises on a refused shelf path: the resolver's own action-required
    shape is returned verbatim (it already carries the ``notify_key`` that
    reaches ``brain alerts``), and NOTHING is written. There is no fallback path
    inside ``vault/`` — a shelf that cannot be placed safely is not placed.

    ``run_id`` names this run's ``_previous/`` namespace; it is generated when
    omitted, and exists as a parameter so a test can pin it.
    """
    root = config.vault_root(vault)
    resolved = shelf_mod.resolve(root)
    if not resolved.ok:
        assert resolved.refusal is not None
        return {"refused": True, **resolved.refusal.as_action_required()}
    shelf = resolved.path
    assert shelf is not None

    taken = census(root)
    prior = L.load_ledger(root, shelf)
    run_id = run_id or L.new_run_id()
    desired, ambiguous = plan_targets(taken["entries"])
    bad_perms = _prepare_shelf(shelf, desired)
    wanted, vanished = _hash_payloads(desired)

    # THE JOURNAL: publish the superset BEFORE touching a single file, carrying
    # the outgoing hash of anything whose bytes are about to change. A crash at
    # any point after this leaves a ledger that already claims every path the
    # run could have written and recognises both its old and its new bytes.
    building = {rel: L.claim(rel, entry, prior) for rel, entry in wanted.items()}
    for rel, entry in prior.items():
        building.setdefault(rel, entry)
    L.store_ledger(root, shelf, building)

    # A note whose payload could not be READ this run is not a note the census
    # dropped. Retiring its live copy on a transient OSError would empty the
    # shelf entry until the next nightly, so both classes are held back from
    # retirement and their ledger rows carry forward untouched.
    unresolved = [e["note_id"] for e in taken["entries"]
                  if e["payload_kind"] == "unresolved"]
    unhealthy = set(vanished) | set(unresolved)

    report = _mat.materialize(root, shelf, wanted, prior, building,
                              bad_perms, unhealthy, run_id, ambiguous,
                              empty_census=not taken["count"])
    report.update(shelf=str(shelf), census=taken["count"], vanished=vanished,
                  unresolved=unresolved, run_id=run_id, ambiguous=ambiguous)
    return report


def _prepare_shelf(shelf: Path, desired: dict[str, dict[str, Any]]) -> list[str]:
    """Create the shelf and its project groups, owner-only, and PROVE it."""
    bad_perms: list[str] = []
    shelf.mkdir(parents=True, exist_ok=True)
    if not L.secure(shelf, L.DIR_MODE):
        bad_perms.append(str(shelf))
    for slug in sorted({PurePosixPath(rel).parts[0] for rel in desired}):
        group = shelf / slug
        group.mkdir(parents=True, exist_ok=True)
        if not L.secure(group, L.DIR_MODE):
            bad_perms.append(str(group))
    return bad_perms


def _hash_payloads(
    desired: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Digest every payload, dropping any that raced away since the census.

    Skipping leaves the previous copy in place — it is simply not in ``wanted``,
    so it is never displaced either — and the next run picks it up.
    """
    wanted: dict[str, dict[str, Any]] = {}
    vanished: list[str] = []
    for rel, entry in desired.items():
        try:
            digest = L.sha256_file(Path(entry["payload"]))
        except OSError:
            vanished.append(entry["note_id"])
            continue
        wanted[rel] = dict(entry, sha256=digest)
    return wanted, vanished
