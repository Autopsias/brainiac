"""DLV-04 — the host-private shelf ledger, its containment rule, and the
filesystem primitives the fold writes through.

Split out of ``deliverables_sync`` at the 500-LOC file ratchet, along one real
seam: this module decides what the fold OWNS and how bytes reach the shelf;
``deliverables_sync`` decides what the shelf should CONTAIN.

**The ledger is deletion authority for a scheduled host process**, so it lives
host-private beside the index dir — the same base and the same reasoning as the
approved queue (INT-01), the attachment anchors (INT-04) and the writer lock
(INT-05). The shelf is a folder the owner can write to, which is the whole
point, so a manifest inside it cannot also be the record that decides what may
be moved: anything able to edit it could add a hand-dropped file's path and
current hash. A path absent from THIS record is an unknown file, whatever the
in-shelf ``shelf-manifest.json`` claims.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from . import config, deliverables_shelf as shelf_mod, lock as _lock
from . import snapshot as _snapshot

#: Bump when an entry's shape changes incompatibly. A ledger carrying an
#: unknown version owns nothing — fail closed, never guess.
LEDGER_VERSION = 1
LEDGER_PREFIX = "shelf-ledger"

#: Where a displaced copy is retired to, never deleted.
PREVIOUS_DIRNAME = "_previous"
#: The advisory view published INSIDE the shelf. Written, never read back.
MANIFEST_FILENAME = "shelf-manifest.json"
#: The owner-facing index published INSIDE the shelf, same posture.
README_FILENAME = "README.md"

#: ``_previous/`` is namespaced by RUN ID, not flat. A v1 and a v2 of one deck
#: retiring on different nights is the NORMAL case, and in a flat layout the
#: second lands on the first's name — recoverable only through a collision
#: suffix, which reads as a corrupted filename rather than as history. The run
#: id is UTC and second-resolution, and it doubles as the retirement DATE the
#: retention window is measured against: the alternative, mtime, records when
#: the bytes were COPIED to the shelf, not when they were moved aside.
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"

#: Owner-only, verified every run — not only at creation. A read-only probe
#: found sampled archived originals and their parents group/world-readable, and
#: reproducing that outside the vault would put Restricted/MNPI payloads where
#: another local account can read them.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: How many superseded hashes one path may still recognise as its own. Only a
#: crash mid-swap leaves any, and each further crash before a clean run adds
#: one, so the list is short by construction — the cap is a bound on a
#: pathological loop, not on ordinary operation.
MAX_PRIOR_SHAS = 4


def new_run_id(now: datetime.datetime | None = None) -> str:
    """This run's ``_previous/`` namespace. UTC, always."""
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime(
        RUN_ID_FORMAT)


def run_id_date(name: str) -> datetime.date | None:
    """The UTC date a ``_previous/<run-id>/`` directory was created, or None.

    None means "do not age this out": the shelf is a folder the owner can
    write to, so an unparseable directory name is either hand-made or damaged,
    and neither is a thing to start a retention clock on.
    """
    try:
        return datetime.datetime.strptime(name, RUN_ID_FORMAT).replace(
            tzinfo=datetime.timezone.utc).date()
    except (ValueError, TypeError):
        return None


def sha256_file(path: Path) -> str:
    """The vault's one chunked file digest, re-exported so this module's
    callers never grow a second implementation of it."""
    return _snapshot._sha256_file(path)


# ---------------------------------------------------------------------------
# ledger path containment
# ---------------------------------------------------------------------------

def _ancestors(target: Path, shelf: Path) -> Iterator[Path]:
    for parent in target.parents:
        if parent == shelf:
            return
        yield parent


def safe_relpath(rel: object, shelf: Path) -> Path | None:
    """One ledger key as an absolute path inside ``shelf``, or ``None``.

    The ledger is deletion authority for a scheduled host process, so this
    accepts ONLY a normalized relative path and refuses everything else:
    absolutes (POSIX or Windows-drive), ``..`` and ``.`` segments, embedded NULs
    or backslashes, a symlink anywhere along the chain, and any resolved target
    outside the shelf. ``core/_capture.py`` already requires resolved
    containment before the writer will sign anything; this is the same rule at
    the same strength.
    """
    text = unicodedata.normalize("NFC", str(rel or "")).strip()
    if not text or "\x00" in text or "\\" in text or text.startswith("/"):
        return None
    if len(text) > 1 and text[1] == ":":          # C:\… / c:/…
        return None
    parts = PurePosixPath(text).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    if PurePosixPath(text).as_posix() != text:
        # NORMALIZED form only. `PurePosixPath` silently drops "./" and collapses
        # "//", so comparing against its own round-trip is what makes "accept
        # only a normalized relative path" a rule rather than a description.
        return None
    target = shelf.joinpath(*parts)
    try:
        base = shelf.resolve(strict=False)
        resolved = target.resolve(strict=False)
    except OSError:
        return None
    if base not in resolved.parents:
        return None
    if target.is_symlink() or any(a.is_symlink() for a in _ancestors(target, shelf)):
        return None
    if target.exists() and not target.is_file():
        return None
    return target


# ---------------------------------------------------------------------------
# the host-private ledger
# ---------------------------------------------------------------------------

def ledger_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Host-private, off the mount — the base s02's binding registry proved."""
    return (shelf_mod.registry_dir(config.vault_root(vault))
            / f"{LEDGER_PREFIX}-{config.vault_slug8(vault)}.json")


def load_ledger(vault: Path, shelf: Path) -> dict[str, dict[str, Any]]:
    """The paths this vault's fold last claimed on ``shelf``, containment-checked.

    Owns NOTHING on any mismatch — an unknown schema version, a different vault
    id, a different canonical vault path, a different shelf, an unreadable or
    non-JSON file. Owning nothing means the fold moves nothing and every file on
    the shelf reads as unknown, which is the fail-closed direction.
    """
    try:
        doc = json.loads(ledger_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != LEDGER_VERSION:
        return {}
    if doc.get("vault_id") != config.vault_id(vault, create=False):
        return {}
    if Path(str(doc.get("vault_path") or "")) != vault:
        return {}
    if Path(str(doc.get("shelf") or "")) != shelf:
        return {}
    raw = doc.get("entries")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for rel, entry in raw.items():
        if not isinstance(entry, dict) or not entry.get("sha256"):
            continue
        if safe_relpath(rel, shelf) is None:
            continue
        out[unicodedata.normalize("NFC", str(rel))] = entry
    return out


def store_ledger(vault: Path, shelf: Path, entries: dict[str, dict[str, Any]]) -> None:
    path = ledger_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(path.parent, DIR_MODE)
    doc = {
        "version": LEDGER_VERSION,
        "vault_id": config.vault_id(vault, create=False),
        "vault_path": str(vault),
        "shelf": str(shelf),
        "entries": entries,
    }
    tmp = _lock._atomic_temp_path(path)
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    config.secure_file_permissions(tmp, FILE_MODE)
    tmp.replace(path)



# ---------------------------------------------------------------------------
# filesystem primitives
# ---------------------------------------------------------------------------

def secure(path: Path, mode: int) -> bool:
    """Tighten to ``mode`` and PROVE it, every run. False when group/world bits survive."""
    config.secure_file_permissions(path, mode)
    try:
        return not (path.stat().st_mode & 0o077)
    except OSError:
        return False


def copy_into(src: Path, dst: Path) -> None:
    """Copy the BYTES — never the source mode.

    ``shutil.copyfile`` copies content only; ``copy``/``copy2`` would carry a
    group/world-readable archived original's permissions straight onto the shelf.
    Staged through an unpredictable sibling and committed with ``os.replace`` so
    a target holds either the old bytes or the new ones, never a torn file.
    """
    tmp = _lock._atomic_temp_path(dst)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def suffixed(name: str, note_id: str) -> str:
    """``report.pdf`` + ``x`` -> ``report (x).pdf``. Used for two different
    disambiguations — a basename collision inside one project group, and a
    second retirement of the same name into ``_previous/`` — so it lives beside
    the mover rather than being duplicated at each site."""
    stem, dot, ext = name.rpartition(".")
    return f"{stem} ({note_id}){dot}{ext}" if dot else f"{name} ({note_id})"


def displace(target: Path, shelf: Path, rel: str, sha: str, run_id: str) -> str:
    """Retire a copy the census no longer wants. A MOVE, never a delete.

    Namespaced by ``run_id`` (see :data:`RUN_ID_FORMAT`), so two versions of
    one deck retiring on two different nights cannot land on each other. The
    sha suffix below is the remaining belt: two runs inside ONE second share a
    namespace, and a run id supplied by a caller may repeat.
    """
    dest_dir = shelf / PREVIOUS_DIRNAME / run_id / PurePosixPath(rel).parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    for parent in [dest_dir, *_ancestors(dest_dir, shelf)]:
        secure(parent, DIR_MODE)
    dest = dest_dir / target.name
    if dest.exists():
        dest = dest_dir / suffixed(target.name, sha[:8])
    os.replace(target, dest)
    secure(dest, FILE_MODE)
    return str(dest)


def shelf_files(shelf: Path) -> Iterator[Path]:
    """Every regular file the fold could be responsible for. ``_previous/`` is
    the retirement bin and is deliberately outside the audit."""
    for path in sorted(shelf.rglob("*")):
        if PREVIOUS_DIRNAME in path.relative_to(shelf).parts:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.parent == shelf and path.name in (MANIFEST_FILENAME,
                                                  README_FILENAME):
            continue
        yield path



def owned_shas(entry: dict[str, Any]) -> set[str]:
    """Every hash that means "the fold wrote this" at that path.

    The current one, PLUS anything it was part-way through replacing when a
    crash interrupted a swap. It must be a list, not one slot: two crashes in a
    row over two new versions leave three possible on-disk states, and dropping
    the oldest makes the next run mistake its own half-written output for an
    owner edit — which it then refuses to touch, forever.
    """
    prior = entry.get("prior_sha256") or []
    return {s for s in [entry.get("sha256"), *prior] if s}


def claim(rel: str, entry: dict[str, Any],
          prior: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One pre-copy ledger row: the intended hash plus every superseded one."""
    superseded = sorted(owned_shas(prior.get(rel, {})) - {entry["sha256"]})
    return {
        "sha256": entry["sha256"], "note_id": entry["note_id"],
        "tier": entry["tier"], "project": entry["project_slug"],
        # Carried so the in-shelf README can be rendered from the LEDGER alone,
        # including the rows a run held back rather than re-copied. Additive:
        # `load_ledger` requires only `sha256`, so a row written before this
        # field existed still loads and simply renders without a title.
        "title": str(entry.get("title") or ""),
        "updated": str(entry.get("updated") or ""),
        **({"prior_sha256": superseded[:MAX_PRIOR_SHAS]} if superseded else {}),
    }


def publish_manifest(shelf: Path, final: dict[str, dict[str, Any]],
                     bad_perms: list[str]) -> None:
    """The in-shelf ADVISORY view. Written for a human; never read back for
    authority — the host-private ledger is the only record that decides
    anything, precisely because the owner can edit this one."""
    path = shelf / MANIFEST_FILENAME
    doc = {
        "advisory": True,
        "note": "Generated view. The authoritative record is host-private; "
                "editing this file changes nothing.",
        "entries": final,
        "count": len(final),
    }
    tmp = _lock._atomic_temp_path(path)
    try:
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    if not secure(path, FILE_MODE):
        bad_perms.append(str(path))


# ---------------------------------------------------------------------------
# `_previous/` — the retirement bin, counted but never emptied on a schedule
# ---------------------------------------------------------------------------

def previous_files(shelf: Path) -> Iterator[tuple[Path, str]]:
    """Every retired copy, paired with the RUN ID namespace it sits under.

    Deliberately separate from :func:`shelf_files`: that one enumerates what
    the fold may be responsible for, and a retired copy is by definition no
    longer ledger-owned. Reporting these as unknown files is the mistake this
    repository has already paid for once — 212 files parked under
    ``inbox/_quarantine/_resolved/`` made the quarantine trend alert fire
    nightly over content that was 99% already dispositioned.
    """
    root = shelf / PREVIOUS_DIRNAME
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if len(parts) < 2:
            continue          # a file dropped straight into `_previous/`
        yield path, parts[0]


def previous_stats(shelf: Path) -> dict[str, int]:
    """``{files, bytes}`` for ``_previous/``. Counted so a bin that never
    empties is at least VISIBLE in the fold result."""
    files = 0
    total = 0
    for path, _run in previous_files(shelf):
        files += 1
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return {"files": files, "bytes": total}
