"""FIX-03 — feed sub-floor stubs and mechanical quarantine drops back into the
ingest retry lane (26dbd97), instead of letting them sit as findings.

Split out of :mod:`brain.remediation_branches` purely to keep that file
under the file-size ratchet — no behaviour change. See that module's
docstring for the shared provenance rules the FIX-01/FIX-02 branches obey;
``extract_retry``'s writes land in ``inbox/``, the drop zone, never in the
audited ``vault/brain``/``vault/raw`` zone, so it does not go through
``audited_write`` at all — see :func:`plan_extract_retry`'s own docstring.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from . import frontmatter as fm
from .remediation_folds import (
    BranchOutcome, EXTRACT_RETRY, EXTRACT_RETRY_MAX_ATTEMPTS, Intent,
    KEY_SUBFLOOR, RETRY_COPY_TO_INBOX,
)


def quarantine_retry_targets(vault: Path) -> dict[str, list[str]]:
    """``{"quarantine:<reason>": [vault-relative paths]}`` for every reason in
    ``remediation.MECHANICAL_QUARANTINE_REASONS`` whose quarantine directory
    currently holds files.

    Reads the SAME tree ``maintenance_retention.quarantine_triage_summary``
    reads, never a second scan of the ingest pipeline's own bookkeeping —
    and the reason vocabulary comes from the registry's explicit allow-list,
    never re-derived here (AGENTS.md capture rule 6)."""
    from . import remediation as _rem

    out: dict[str, list[str]] = {}
    qdir = Path(vault) / "inbox" / "_quarantine"
    if not qdir.is_dir():
        return out
    for reason in _rem.MECHANICAL_QUARANTINE_REASONS:
        reason_dir = qdir / reason
        if not reason_dir.is_dir():
            continue
        files = sorted(
            str(p.relative_to(vault)) for p in reason_dir.iterdir()
            if p.is_file() and not p.name.endswith(".reason.txt"))
        if files:
            out[f"quarantine:{reason}"] = files
    return out


def subfloor_retry_targets(core: Any) -> list[tuple[str, Path]]:
    """``[(note id, resolved archived-original path)]`` for every sub-floor
    family member worth retrying.

    Two narrowing checks, both there so this never force-feeds a note that
    is not actually a failed extraction: the body must carry
    ``NO_TEXT_MARKER`` — the SAME check ``ingest.pipeline_duplicates.
    prior_extraction_failed`` makes, so a short-but-real note the family
    shape happens to catch is never mistaken for a stub — and its
    ``origin:`` frontmatter must resolve to a file that still exists (a
    ``verbal``/``url`` origin has no archived binary to retry).

    Reads ``invariants.subfloor_families()["members"]`` — the SAME
    population the metric itself reports, never re-derived."""
    from . import invariants as _inv
    from .ingest.handlers.base import NO_TEXT_MARKER

    vault = Path(core.vault)
    metric = _inv.subfloor_families(core.index.conn)
    out: list[tuple[str, Path]] = []
    for nid in sorted(metric.get("members") or []):
        note_path = vault / "raw" / f"{nid}.md"
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if NO_TEXT_MARKER not in text:
            continue
        try:
            meta, _body = fm.parse_text(text)
        except ValueError:
            continue
        origin = str(meta.get("origin") or "").strip()
        if not origin:
            continue
        original = vault / origin
        if original.is_file():
            out.append((nid, original))
    return out


def _attempt_entry(row: dict[str, Any], content_hash: str) -> dict[str, Any]:
    """The mutable per-CONTENT-HASH attempt record inside the branch's own
    host-private row (grill ruling 2026-08-20: keyed by content, never by
    path — a re-ingested file with different bytes is a new target, and the
    same bytes never retry past the bound).

    ``count_last_day`` is ``remediation_state.once_per_day``'s OWN marker
    name for the ``"count"`` counter (``f"{counter}_last_day"``) — read here
    only to PEEK whether today's retry has already been claimed, never
    written directly; the claim itself happens once, in
    ``folds.remediation.apply_extract_retry_intents``, through the shared
    primitive (audit A's class guard)."""
    attempts = row.get("retry_attempts")
    if not isinstance(attempts, dict):
        attempts = {}
        row["retry_attempts"] = attempts
    entry = attempts.get(content_hash)
    if not isinstance(entry, dict):
        entry = {"count": 0}
        attempts[content_hash] = entry
    return entry


def _retry_already_resolved(vault: Path, content_hash: str) -> bool:
    """Whether a PRIOR retry of these exact bytes already succeeded.

    A retried copy lands in ``inbox/`` and is ingested through the ordinary
    pipeline, which re-keys the ingest manifest's ``content_hash -> note id``
    entry to the NEW note (``pipeline_stages.py``'s finalize step) — the same
    entry the original stub/quarantine used to point at. So once that entry
    names a note whose body no longer carries ``NO_TEXT_MARKER``, the
    document is reachable and this target is DONE — without anything here
    ever touching the stale stub note or the leftover quarantined file (which
    stay exactly where they were; nothing in this branch deletes or moves
    them). Without this check a target that succeeded on attempt 1 would
    still read as a target forever (the stub/quarantine leftover never goes
    away on its own) and hit the exhaustion bound regardless of the retry
    having worked."""
    from .ingest.handlers.base import NO_TEXT_MARKER
    from .ingest.pipeline import _load_manifest

    manifest = _load_manifest(vault)
    note_id = manifest.get(content_hash)
    if not note_id:
        return False
    try:
        text = (vault / "raw" / f"{note_id}.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return NO_TEXT_MARKER not in text


def plan_extract_retry(
    core: Any, cap: int, row: dict[str, Any], today: datetime.date,
) -> tuple[BranchOutcome, list[Intent]]:
    """Sub-floor stubs and mechanical quarantine drops, bounded per target.

    A target's identity is the sha256 of the bytes being retried (the
    quarantined file, or the archived original for a sub-floor stub) —
    ``_attempt_entry`` above. Past ``EXTRACT_RETRY_MAX_ATTEMPTS`` a target is
    EXCLUDED from ``out.targets``/``out.by_key`` — never counted toward this
    branch's remaining/convergence — and instead recorded in
    ``out.exhausted`` under its own ``quarantine-exhausted:*`` owner key
    (s01-review finding: one file that can never converge must not read as
    the whole branch being broken). A target a PRIOR retry already resolved
    (``_retry_already_resolved``) is dropped silently — resolved, not
    exhausted.

    Writes land in ``inbox/`` — the drop zone, not the audited vault zone —
    so this branch never touches ``audited_write``/``core.write_note`` at
    all; ``folds.remediation.apply_extract_retry_intents`` does the actual
    copy."""
    from .snapshot import _sha256_file

    out = BranchOutcome(EXTRACT_RETRY, KEY_SUBFLOOR)
    vault = Path(core.vault)
    today_iso = today.isoformat()
    # (registry key, target identity, bytes to retry, owner exception key)
    candidates: list[tuple[str, str, Path, str]] = []
    for key, files in quarantine_retry_targets(vault).items():
        reason = key.split(":", 1)[1]
        for rel in files:
            candidates.append(
                (key, rel, vault / rel, f"quarantine-exhausted:{reason}"))
    for nid, original in subfloor_retry_targets(core):
        candidates.append(
            (KEY_SUBFLOOR, f"raw/{nid}.md", original, "quarantine-exhausted:subfloor"))

    by_key: dict[str, list[str]] = {}
    exhausted: dict[str, list[str]] = {}
    intents: list[Intent] = []
    for key, target, source, exhaust_key in candidates:
        try:
            content_hash = _sha256_file(source)
        except OSError as exc:
            out.skipped.append({"target": target, "reason": f"unreadable: {exc}"})
            continue
        if _retry_already_resolved(vault, content_hash):
            # A prior retry already produced a real note over these bytes;
            # the stub/quarantine leftover is not deleted, but it is no
            # longer a target — resolved, not exhausted, not retried again.
            continue
        entry = _attempt_entry(row, content_hash)
        count = int(entry.get("count", 0) or 0)
        if count >= EXTRACT_RETRY_MAX_ATTEMPTS:
            exhausted.setdefault(exhaust_key, []).append(target)
            continue
        out.targets.append(target)
        by_key.setdefault(key, []).append(target)
        if entry.get("count_last_day") == today_iso:
            out.skipped.append({"target": target,
                                "reason": "already retried today; waiting for tomorrow "
                                          "(one retry per target per day — same-day "
                                          "re-offers collide on the ingest note id)"})
            continue
        if len(intents) < cap:
            intents.append(Intent(
                target, RETRY_COPY_TO_INBOX,
                f"retry {count + 1}/{EXTRACT_RETRY_MAX_ATTEMPTS}: re-drop into "
                "inbox/ for re-ingestion", content=str(source)))
    out.by_key = by_key
    out.exhausted = exhausted
    return out, intents
