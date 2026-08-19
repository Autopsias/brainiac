"""Quarantine and duplicate-retention fold (CUT-02)."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import os
import time


# ---------------------------------------------------------------------------
# CUT-02 — quarantine & duplicate retention fold. The ingest pipeline
# (``ingest/pipeline.py``) parks two kinds of leftovers under ``inbox/`` that
# used to grow forever with nobody looking:
#   - ``_duplicate/`` — content that hashed identical to something already
#     promoted into ``raw/``. Safe to prune after a grace period, but ONLY
#     once the full provenance chain re-confirms the redundancy (see
#     ``_verify_duplicate_provenance`` — HARDENED:codex, a manifest hit alone
#     is not proof: the manifest's ``sha256`` key is the ORIGINAL file's hash,
#     but a raw note's own ``sha256:`` frontmatter is the EXTRACTED
#     MARKDOWN's hash, not the original's — the only thing that can prove
#     "this parked file's bytes truly live elsewhere" is the archived
#     original under ``raw/originals/`` the note's ``origin:`` points at).
#   - ``_quarantine/`` — content that could NOT be safely processed (bad
#     encoding, missing handler, collision, ...). NEVER auto-deleted: it may
#     be the only copy. Instead it gets a non-destructive monthly triage
#     summary queued to ``hot.md`` (see ``quarantine_triage_summary`` /
#     ``render_quarantine_summary_hot_entry`` / ``quarantine_summary_due``).
# ---------------------------------------------------------------------------
DUPLICATE_RETENTION_DAYS_ENV = "BRAIN_DUPLICATE_RETENTION_DAYS"
DEFAULT_DUPLICATE_RETENTION_DAYS = 30
# Mirrors the sidecar convention ``ingest/pipeline.py``'s ``_process_claimed``
# writes alongside every parked duplicate (``<file>.duplicate-of.txt``).
_DUPLICATE_SIDECAR_SUFFIX = ".duplicate-of.txt"


def _sha256_file(path: Path) -> str:
    """Streaming sha256 (reuses snapshot's chunked helper — review finding [5]:
    a parked duplicate AND its archived original are BOTH hashed per aged
    candidate, and raw/originals are exactly the large binaries — never load a
    whole PDF/video into memory)."""
    from .snapshot import _sha256_file as _stream_sha256_file

    return _stream_sha256_file(path)


def _verify_duplicate_provenance(dup_file: Path, vault: Path, manifest: dict[str, str]) -> tuple[bool, str]:
    """The FULL provenance chain a parked duplicate must clear before it is
    ever deleted (HARDENED:codex — the naive "a manifest entry exists" guard
    is insufficient, see the module docstring above):

    1. hash the PARKED file itself (its bytes are the original bytes, moved
       verbatim into ``_duplicate/`` — never re-encoded);
    2. that hash must be a key in the ingest manifest (``sha256(original) ->
       raw-note-id``);
    3. resolve the manifest's target ``raw/<id>.md`` note;
    4. read ITS ``origin:`` frontmatter — the archived-original's path;
    5. confirm that archived file still exists under ``raw/originals/`` AND
       still hashes to the SAME original sha.

    Only step 5 passing proves the parked bytes are truly redundant. Returns
    ``(True, "verified")`` on success, else ``(False, <specific reason>)`` —
    every failure reason is precise enough to explain a skip without
    re-deriving it, since a failed chain is reported, never silently dropped."""
    try:
        dup_sha = _sha256_file(dup_file)
    except OSError as exc:
        return False, f"parked file unreadable: {exc}"
    existing_id = manifest.get(dup_sha)
    if existing_id is None:
        return False, "no ingest-manifest entry for this file's content hash"
    note_path = vault / "raw" / f"{existing_id}.md"
    if not note_path.is_file():
        return False, f"manifest target raw/{existing_id}.md does not exist"
    from . import frontmatter as fm

    try:
        meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — review finding [3]: a malformed raw
        # note (invalid UTF-8 = UnicodeDecodeError, or a YAML parse error — both
        # ValueError, not OSError) must SKIP this one candidate, never propagate
        # and abort the whole fold (which would starve pruning of every other
        # verifiable duplicate). Fail-safe: an unverifiable note is never pruned.
        return False, f"could not read/parse raw/{existing_id}.md: {exc}"
    origin = meta.get("origin")
    if not origin:
        return False, f"raw/{existing_id}.md has no origin: provenance"
    archive_path = vault / str(origin)
    if not archive_path.is_file():
        return False, f"archived original '{origin}' is missing"
    try:
        archive_sha = _sha256_file(archive_path)
    except OSError as exc:
        return False, f"could not hash archived original: {exc}"
    if archive_sha != dup_sha:
        return False, "archived original's hash no longer matches the parked duplicate"
    return True, "verified"


def retention_fold(vault: Path, today: datetime.date, *, dry_run: bool = False) -> dict[str, Any]:
    """CUT-02: prune ``inbox/_duplicate/`` entries older than
    ``BRAIN_DUPLICATE_RETENTION_DAYS`` (default 30, mtime-based). Safe by
    construction ONLY because every candidate is re-verified through
    ``_verify_duplicate_provenance`` right before deletion — a manifest hit
    is necessary but not sufficient (see the module docstring); anything
    that fails the chain is left in place and reported, never deleted on an
    unverified chain. Deletes the parked file + its ``.duplicate-of.txt``
    sidecar together, as one lot. Never touches ``inbox/_quarantine/`` (see
    ``quarantine_triage_summary`` for that lifecycle instead)."""
    import os
    import time

    from .ingest.pipeline import _load_manifest as _load_ingest_manifest

    # Review finding [1]: a non-integer $BRAIN_DUPLICATE_RETENTION_DAYS must not
    # raise (which would block the whole fold and silently disable pruning) —
    # fall back to the default on any bad value.
    try:
        retention_days = int(os.environ.get(
            DUPLICATE_RETENTION_DAYS_ENV, DEFAULT_DUPLICATE_RETENTION_DAYS))
    except (TypeError, ValueError):
        retention_days = DEFAULT_DUPLICATE_RETENTION_DAYS
    dup_dir = vault / "inbox" / "_duplicate"
    report: dict[str, Any] = {
        "retention_days": retention_days, "dry_run": dry_run,
        "considered": 0, "not_due": 0, "pruned": [], "skipped": [],
    }
    if not dup_dir.is_dir():
        return report

    manifest = _load_ingest_manifest(vault)
    cutoff_ts = time.mktime(today.timetuple()) - retention_days * 86400.0

    for p in sorted(dup_dir.iterdir()):
        if not p.is_file() or p.name.endswith(_DUPLICATE_SIDECAR_SUFFIX):
            continue  # sidecars are handled alongside their primary file
        report["considered"] += 1
        try:
            mtime = p.stat().st_mtime
        except OSError as exc:
            report["skipped"].append({"file": p.name, "kind": "stat", "reason": f"stat failed: {exc}"})
            continue
        if mtime > cutoff_ts:
            report["not_due"] += 1
            continue
        # Per-candidate isolation (review finding [3]): any unexpected error in
        # the verify chain skips THIS candidate, never aborts the fold.
        try:
            ok, reason = _verify_duplicate_provenance(p, vault, manifest)
        except Exception as exc:  # noqa: BLE001 — defensive; verify is fail-safe
            report["skipped"].append(
                {"file": p.name, "kind": "provenance", "reason": f"verify error: {exc}"})
            continue
        if not ok:
            report["skipped"].append({"file": p.name, "kind": "provenance", "reason": reason})
            continue
        if dry_run:
            report["pruned"].append({"file": p.name, "would_delete": True})
            continue
        # Delete the primary FIRST (the data-safe step, since provenance
        # verified the bytes are archived). Review finding [2]: only a PRIMARY
        # delete failure is a "not pruned" skip — a sidecar-unlink failure AFTER
        # the primary is gone must NOT be misreported as a provenance failure
        # (the file IS pruned); record the orphaned sidecar separately instead.
        sidecar = dup_dir / f"{p.name}{_DUPLICATE_SIDECAR_SUFFIX}"
        try:
            p.unlink()
        except OSError as exc:
            report["skipped"].append(
                {"file": p.name, "kind": "delete", "reason": f"delete failed: {exc}"})
            continue
        entry: dict[str, Any] = {"file": p.name}
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            entry["orphaned_sidecar"] = f"{sidecar.name}: {exc}"
        report["pruned"].append(entry)
    return report


#: Quarantine reasons whose fix is an operator action on THIS host rather than
#: a judgement call about the file. Each maps to the exact remedy.
_QUARANTINE_REMEDY = {
    "pdf_no_text_layer":
        "this PDF is a SCAN. Install the local OCR engine "
        "(`brew install tesseract tesseract-lang` + `pip install pytesseract` "
        "into the engine's venv), then move the file back to `inbox/` and run "
        "`brain sync` — the ingest OCRs scanned pages once OCR is available",
    "empty_or_low_text_density":
        "extraction returned almost no text. Check the file opens, then move it "
        "back to `inbox/` and run `brain sync`",
    "pdf_encrypted":
        "the PDF is password-protected. Save an unlocked copy into `inbox/`",
}


def ingest_quarantine_findings(
    ingest_report: dict[str, Any], vault: Path
) -> list[dict[str, Any]]:
    """One ``action_required`` item per quarantine reason in ONE ingest run.

    A quarantined drop is a document the owner meant to ingest and did not
    get. Neither existing surface reports it in time: the ``quarantine``
    health-trend metric compares against a trailing median and returns early
    on a zero baseline (so a vault's FIRST quarantine — the one that matters
    most — is structurally unalertable), and the triage summary fires at most
    once per calendar month. This reports the event on the run it happens.
    Pure: shapes an already-computed report, reads no files."""
    from .maintenance_outcomes import action_required_item
    quarantined = ingest_report.get("quarantined") or []
    if not quarantined:
        return []
    by_reason: dict[str, list[str]] = {}
    for entry in quarantined:
        if isinstance(entry, dict):
            by_reason.setdefault(str(entry.get("reason", "unknown")), []).append(
                str(entry.get("file", "?")))
    findings = []
    for reason, files in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(sorted(files)[:5])
        more = f" (+{len(files) - 5} more)" if len(files) > 5 else ""
        item = action_required_item(
            f"{len(files)} dropped file(s) quarantined as `{reason}` and NOT "
            f"ingested: {shown}{more}",
            "a quarantined file never reaches `raw/` and is never retrievable — "
            "it is not in the vault, and nothing else reports this within the month",
            _QUARANTINE_REMEDY.get(
                reason, "inspect the file and its `.reason.txt` sidecar, fix the "
                        "cause, then move it back to `inbox/` and run `brain sync`"),
            str(vault / "inbox" / "_quarantine" / reason),
        )
        # Reaches `brain alerts` at session start, not just the maintain log —
        # a finding only a launchd log carries is a finding nobody reads.
        # Keyed per REASON so it dedups once a day, not once per file.
        item["notify_key"] = f"quarantine:{reason}"
        findings.append(item)
    return findings


def quarantine_summary_due(marker: dict[str, Any] | None, today: datetime.date) -> bool:
    """True when the monthly quarantine-triage summary is due: never fired,
    or last fired in an earlier calendar month than ``today`` — "due since
    last run" catch-up (mirrors every other monthly ``maintain`` branch),
    not a strict "only on the 1st" calendar check."""
    if not marker:
        return True
    return marker.get("last_month") != today.strftime("%Y-%m")


def quarantine_triage_summary(vault: Path, today: datetime.date | None = None) -> dict[str, Any]:
    """A non-destructive snapshot of ``inbox/_quarantine/`` — counts by
    reason (the reason subdirectory ``_quarantine()`` files each candidate
    into, see ``ingest/pipeline.py``'s ``_quarantine``), oldest item's age in
    days, and total size. Never deletes anything: a quarantined file may be
    the only copy of its content (that's exactly why it never reached
    ``raw/`` in the first place)."""
    import time

    qdir = vault / "inbox" / "_quarantine"
    result: dict[str, Any] = {
        "total": 0, "by_reason": {}, "oldest_age_days": None, "total_bytes": 0,
    }
    if not qdir.is_dir():
        return result

    now_ts = time.mktime(today.timetuple()) if today else time.time()
    oldest_mtime: float | None = None
    for reason_dir in sorted(p for p in qdir.iterdir() if p.is_dir()):
        count = 0
        for f in reason_dir.rglob("*"):
            if not f.is_file() or f.name.endswith(".reason.txt"):
                continue
            count += 1
            try:
                st = f.stat()
            except OSError:
                continue
            result["total_bytes"] += st.st_size
            if oldest_mtime is None or st.st_mtime < oldest_mtime:
                oldest_mtime = st.st_mtime
        if count:
            result["by_reason"][reason_dir.name] = count
            result["total"] += count
    if oldest_mtime is not None:
        result["oldest_age_days"] = max(0, int((now_ts - oldest_mtime) // 86400))
    return result


def render_quarantine_summary_hot_entry(summary: dict[str, Any], today: datetime.date) -> str:
    """The monthly quarantine-aging hot-queue entry — never silent, never a
    prompt to delete anything (see ``retention_fold``'s docstring for why
    quarantine is excluded from auto-pruning)."""
    lines = [f"## {today.isoformat()} — Monthly quarantine triage"]
    total = summary.get("total", 0)
    if not total:
        lines.append("- **Context:** `inbox/_quarantine/` is empty. Nothing to triage.")
        return "\n".join(lines) + "\n"
    size_mb = round(summary.get("total_bytes", 0) / (1024 * 1024), 1)
    lines.append(
        f"- **Context:** {total} quarantined file(s), {size_mb} MB total, "
        f"oldest {summary.get('oldest_age_days')} day(s) old."
    )
    for reason, count in sorted(summary.get("by_reason", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"  - `{reason}`: {count}")
    lines.append(
        "- **Owner input needed:** review `inbox/_quarantine/` and fix or "
        "discard each reason bucket by hand — quarantined files are NEVER "
        "auto-deleted; a file here may be the only copy of its content."
    )
    return "\n".join(lines) + "\n"

# Cross-section binds, deferred past this module's own defs.
