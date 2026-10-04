"""PAR-01 auto-PARA: file brain/ notes into their PARA zone by METADATA.

Two deliberately small rules:

- ``type: project``          -> ``brain/projects/``
- ``is_latest_version: false`` (retired by a supersession chain)
                              -> ``brain/archive/``

Generated views (``type: index``/``moc``) and everything else stay where
they are. Moves are by-id-safe: wikilinks target ids, not paths.

PLAN FROM THE INDEX, APPLY UNDER THE LOCK (2026-09-27). Walking and reading
every brain/ note took 6.2-6.9 s warm and 14.8 s cold over 3,438 notes on
the live vault, every hour, and opened whatever sat at a note path. So
``auto_para_plan`` asks the index — one query over the ``type`` and
``is_latest_version`` columns — and reads no note file. A note the index
does not know yet waits one run. ``auto_para_apply`` runs under the writer
lock, immediately before the maintain run's first sync, and treats the DISK
as the truth: it re-reads each planned file (regular files only) and skips
any note that is gone or no longer qualifies. An empty plan touches nothing.

THE MOVE IS AUDITED (2026-08-18) AND NEVER LAUNDERS (2026-09-27). The chain
is keyed on PATH, so a bare rename broke a signed note both ways
(``content_drift`` missing at the old path, ``unsigned_notes`` at the new).
Apply signs the destination BEFORE the move, and signs only bytes the chain
already signed at the old path: an edited or never-signed note is left where
it is for ``content_drift`` / ``unsigned_notes`` to report, never re-signed
into a clean one. A key failure SKIPS the move (fail closed). The move is
``link`` then ``unlink``, which refuses to overwrite a file that appeared at
the destination; any failure after signing records ``write_failed``, so no
signed entry is left for a path that does not exist.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

_SKIP_NAMES = ("backlinks.md", "catalog.md", "index.md")


def _zone_for(ntype: str, retired: bool) -> str | None:
    if ntype in ("index", "moc"):
        return None
    return "archive" if retired else "projects" if ntype == "project" else None


def _rel(raw: str, vault: Path) -> str | None:
    p = Path(raw)
    if not p.is_absolute():
        return p.as_posix()
    for root in (vault, vault.resolve()):
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            continue
    return None


def auto_para_plan(vault: Path, conn: Any) -> dict[str, Any]:
    """Read-only: the moves the INDEX says are due. Opens no note file."""
    moves: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT id, path, type, is_latest_version FROM notes "
        "WHERE type = 'project' OR is_latest_version = 'false'").fetchall()
    for nid, raw_path, ntype, latest in rows:
        rel = _rel(str(raw_path or ""), vault)
        if not rel or not rel.startswith("brain/"):
            continue
        parts = rel.split("/")
        zone = _zone_for(str(ntype or ""), str(latest or "") == "false")
        if zone is None or parts[-1] in _SKIP_NAMES or parts[-2] == zone:
            continue
        moves.append({"rel": rel, "zone": zone, "id": str(nid)})
    moves.sort(key=lambda m: m["rel"])
    return {"moves": moves, "errors": []}


def _destination(path: Path) -> tuple[str | None, str]:
    """``(dest_zone or None, note_id)`` for one note, read from disk NOW.
    Anything but a regular file (a FIFO, a symlink, a directory) is None."""
    from . import frontmatter as fm

    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None, path.stem
    except FileNotFoundError:
        return None, path.stem
    meta, _ = fm.parse_text(path.read_text(encoding="utf-8"))
    note_id = str(meta.get("id") or path.stem)
    retired = str(meta.get("is_latest_version")).lower() == "false"
    zone = _zone_for(str(meta.get("type") or ""), retired)
    if zone is None or path.parent.name == zone:
        return None, note_id
    return zone, note_id


def _move(src: Path, dest: Path) -> None:
    """No-replace move: ``link`` fails with FileExistsError if ``dest``
    appeared since the check, where ``rename`` would silently overwrite it."""
    os.link(src, dest)
    try:
        src.unlink()
    except OSError:
        dest.unlink()
        raise


def _apply_one(vault: Path, move: dict[str, Any], audit: Any,
               signed: dict[str, str], report: dict[str, Any]) -> None:
    from .audit import KeyUnavailable
    from . import notes

    old_rel, zone = move["rel"], move["zone"]
    p = vault / old_rel
    still, note_id = _destination(p)
    if still != zone:
        report["skipped_changed"].append({"id": note_id, "file": old_rel})
        return
    dest = vault / "brain" / zone / p.name
    new_rel = dest.relative_to(vault).as_posix()
    if dest.exists():
        report["errors"].append({"file": old_rel, "error": f"collision at {new_rel}"})
        return
    if audit is None:
        report["skipped_unsigned"].append(
            {"id": note_id, "reason": "no audit chain — refusing to move "
                                      "a signed note to an unsigned path"})
        return
    # RAW BYTES (M-7), hashed ONCE: the digest compared is the digest signed.
    sha = notes.sha256_file(p)
    if signed.get(old_rel) != sha:
        why = "never signed" if old_rel not in signed else "edited since signing"
        report["skipped_unverified"].append({"id": note_id, "reason": why})
        return
    dest.parent.mkdir(parents=True, exist_ok=True)  # BEFORE signing
    try:
        audit.append(verb="write", path=new_rel,
                     reason=f"auto-para: filed {note_id} into {zone}/",
                     content_sha256=sha)
    except KeyUnavailable as exc:
        report["skipped_unsigned"].append({"id": note_id, "reason": str(exc)})
        return
    try:
        _move(p, dest)
    except OSError as exc:
        audit.append(verb="write_failed", path=new_rel,
                     reason=f"auto-para move failed: {type(exc).__name__}: {exc}")
        report["errors"].append({"file": old_rel, "error": str(exc)})
        return
    # Retire the old path so content_drift stops reporting it `missing`.
    audit.append(verb="delete", path=old_rel,
                 reason=f"auto-para: {note_id} moved to {new_rel}")
    report["moved"].append({"id": note_id, "to": f"brain/{zone}/"})


def auto_para_apply(vault: Path, plan: dict[str, Any],
                    audit: Any | None = None) -> dict[str, Any]:
    """Move the planned notes, re-verifying each on disk first. Run under
    the writer lock. One failing move never hides or blocks the others."""
    report: dict[str, Any] = {"moved": [], "errors": list(plan.get("errors", [])),
                              "skipped_unsigned": [], "skipped_changed": [],
                              "skipped_unverified": []}
    moves = plan.get("moves", [])
    if not moves:
        return report
    try:
        signed = audit.latest_signed_hashes() if audit is not None else {}
    except Exception as exc:  # noqa: BLE001 — unreadable chain: move nothing
        report["errors"].append({"file": None, "error": f"audit chain: {exc}"})
        signed = {}
    for move in moves:
        try:
            _apply_one(vault, move, audit, signed, report)
        except Exception as exc:  # noqa: BLE001 — one move, not the batch
            report["errors"].append({"file": move.get("rel"),
                                     "error": f"{type(exc).__name__}: {exc}"})
    return report


def auto_para(vault: Path, audit: Any | None = None, *, conn: Any) -> dict[str, Any]:
    """Plan from the index, then apply, in one call."""
    return auto_para_apply(vault, auto_para_plan(vault, conn), audit)
