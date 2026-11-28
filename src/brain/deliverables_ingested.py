"""F33 — what THIS HOST ingested through the deliverables lane, kept off the mount.

``<vault>/.brain/ingest-manifest.json`` maps sha256(original bytes) -> raw id and
sits on the mount a Cowork VM writes. The absorb driver used it as its only "is
this row already in the vault" test, so one forged line (``{"<sha of a file in the
owner's folder>": "<an existing note id>"}``) made the host SIGN an anchor for a
file it never ingested and report ``already_present``.

The absorb driver now reads that file NOWHERE. "Present" means exactly one thing:
the lane ingested these BYTES on this host. The record below (sha -> raw id) is
written by the lane's own stage hook, ``deliverables.anchor_for``, in-process,
right after the signed raw note landed and before the anchor write. It is keyed by
``record.original_sha``, the hash of the exact bytes the drain read, archived and
signed, and holds ``record.slug``, the final id; so a decoy dropped into another
lane folder, or a staged copy swapped for other bytes, is recorded under ITS OWN
sha and can never stand in for the owner's file. No manifest line can create an
entry, and no file name is ever trusted. The record hands an id back only while it
is a plain slug and ``raw/<id>.md`` is still a plain file under ``raw/``.

It lives beside the drop-lane journal, OFF the mount (the journal's own resolver
and app-data fallback, 0700 dir / 0600 file, atomic write). Nothing reads it from a
mount path: being unreachable from the VM is the control. A missing or corrupt
record reads as empty.

Owner-visible consequences, none silent (each is a visible row with a reason):

* The record starts EMPTY, so a file absorbed BEFORE it existed is not recognised.
  A re-run stages it, the drain's duplicate gate files the copy into
  ``inbox/_duplicate`` and the row reports ``ingested: False``,
  ``reason: "duplicates: <id>"`` and stays open. No anchor is written or
  re-ensured; the anchor the original absorption wrote is untouched.
* A file that entered the vault by an ORDINARY drop (no lane, no anchor) is not
  recognised either; it needs the batch stamp route (``deliverables_apply``).
* A lost manifest line no longer makes the driver say ``already_present``.

Renaming a file repairs none of these, because the drain keys on the bytes.

Crash window, stated exactly: the record exists before the anchor write and before
the driver sees the drain's report, so a kill inside the anchor write, or after the
drain returned, resumes as ``already_present``. The one sub-window left is between
the signed raw note write returning and the first statement of ``anchor_for`` (the
manifest save, the claim unlink, the guard admit); a kill there is not zero, and
falls back to the drain's own visible ``duplicates: <id>`` outcome.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config
from .atomic_temp import write_text_atomic
from .notes import safe_slug


def record_path(vault: Any) -> Path:
    """The host-private record, beside the drop-lane journal. Resolution only:
    ``remember`` creates it."""
    from .ingest import deliverables as DLV

    return DLV.journal_path(vault).with_name(f"ingested-{config.vault_slug8(vault)}.json")


def _slug(value: Any) -> str:
    """``value`` as a plain, path-safe note id, or ``""`` (a list, a number, a
    separator, ``..``, NUL, an absolute path or an oversized value is not one)."""
    if not isinstance(value, str):
        return ""
    try:
        return safe_slug(value)
    except ValueError:
        return ""


def _load(vault: Any) -> dict[str, Any]:
    try:
        data = json.loads(record_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(vault: Any, sha: str, note_id: str) -> None:
    """Record that the lane ingested the bytes hashing to ``sha`` as ``note_id``.

    Errors propagate, like ``journal_open`` writing into the same host-private
    directory in the same drain: a record that could not be written must stop the
    drain loudly, never leave a row that reads as not-ingested.

    shortcut: one entry per lane drop, never pruned, and the whole file is
    rewritten each time like the journal's; upgrade only if a vault holds enough
    deliverables for that rewrite to show."""
    path = record_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(path.parent, 0o700)
    write_text_atomic(path, json.dumps({**_load(vault), sha: note_id}, indent=2, sort_keys=True))
    config.secure_file_permissions(path, 0o600)


def present_id(vault: Any, sha: str) -> str:
    """The raw id the lane ingested these BYTES as on this host, or ``""`` — also
    when the record is missing or corrupt, the entry is not a plain slug, or its
    ``raw/<id>.md`` is gone, a link, or not under ``raw/``."""
    from .remediation_extract_retry import contained_file_under

    root = Path(vault).resolve()
    nid = _slug(_load(root).get(sha))
    if nid and contained_file_under(root / "raw" / f"{nid}.md", root / "raw") is not None:
        return nid
    return ""
