"""Validate a draft's declared in-place update of an existing note (UPD-01).

The trust rule, in one sentence: the untrusted leg may rewrite ONLY notes the
untrusted lane itself authored (``provenance.trust: untrusted``), only under
``brain/`` (raw/ is append-only), and only against the exact version it read
(``base_sha256`` — the sha256 that ``brain get`` returned). Everything else
fails closed with a reason that names the host-side way forward. The base-hash
rule is also the lost-update guard: a draft staged from a stale read refuses
instead of silently clobbering a newer write.

Split out of :mod:`brain.draft_drain` at the file-size ratchet; the drain owns
the pipeline, this module owns only the update lane's validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .notes import note_from_text

#: Transport-only frontmatter keys of the update lane (UPD-01, 2026-08-30).
#: They declare intent to the DRAIN and are dropped before signing — a
#: committed note never carries them.
UPDATE_TRANSPORT_KEYS = ("updates", "base_sha256")


@dataclass
class UpdateVerdict:
    """Either an accepted target ``rel_path`` or a fail-closed skip reason."""

    rel_path: str = ""
    reason: str = ""


def _target_rel_path(core: Any, note: Any, note_id: str, promotion_path) -> str:
    """Resolve the LIVE note's own VAULT-RELATIVE path.

    The index row wins (it stores the note's path, possibly absolute), disk is
    the fallback. A path that does not resolve inside the vault returns ""
    (fail closed — the caller reports the target as missing).
    """
    try:
        row = core.index.get(note_id)
    except Exception:
        row = None
    raw_path = str((row or {}).get("path") or "")
    if not raw_path:
        fallback, _ = promotion_path(note, note_id)
        return fallback if (core.vault / fallback).exists() else ""
    p = Path(raw_path)
    if not p.is_absolute():
        p = core.vault / p
    try:
        return p.resolve().relative_to(core.vault.resolve()).as_posix()
    except ValueError:
        return ""


def validate_update(
    core: Any, note: Any, note_id: str, *, approved: bool, promotion_path,
) -> UpdateVerdict:
    """Check every UPD-01 guard; see the module docstring for the trust rule."""
    if approved:
        return UpdateVerdict(
            reason="update-refused: the approved queue does not carry updates")
    declared = str(note.meta.get("updates") or "").strip()
    if declared != note_id:
        return UpdateVerdict(
            reason=f"update-id-mismatch: updates {declared!r} but the draft id "
                   f"is {note_id!r}")
    rel_path = _target_rel_path(core, note, note_id, promotion_path)
    if not rel_path or not (core.vault / rel_path).exists():
        return UpdateVerdict(
            reason=f"update-target-missing: {note_id!r} has no live note on disk")
    if not rel_path.startswith("brain/"):
        return UpdateVerdict(
            reason=f"update-refused-immutable-zone: {rel_path!r} is not under "
                   "brain/ (raw/ is append-only)")
    try:
        current_text = (core.vault / rel_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return UpdateVerdict(
            reason=f"update-target-unreadable: {type(exc).__name__}")
    current = note_from_text(current_text, core.vault / rel_path, core.vault)
    if current is None:
        return UpdateVerdict(
            reason=f"update-target-unparseable: {rel_path!r} has no frontmatter")
    if str(current.meta.get("provenance.trust") or "").strip() != "untrusted":
        return UpdateVerdict(
            reason=f"update-needs-owner: {note_id!r} is host-authored — apply "
                   "it on the host with `brain write`")
    # No `or ""`: the frontmatter parser turns an all-digit scalar into an
    # int, and 0 is falsy — an all-zero hash must fail as STALE, not missing.
    raw_base = note.meta.get("base_sha256")
    base_sha = "" if raw_base is None else str(raw_base).strip().lower()
    if not base_sha:
        return UpdateVerdict(
            reason="update-missing-base: declare base_sha256 (the sha256 "
                   "`brain get` returned for the version you read)")
    if base_sha != current.sha256:
        return UpdateVerdict(
            reason=f"stale-base: {note_id!r} changed since the draft's "
                   "base_sha256 — re-read the note and re-stage")
    return UpdateVerdict(rel_path=rel_path)
