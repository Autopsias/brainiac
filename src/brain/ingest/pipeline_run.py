"""Coordinate one inbox drain."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from . import handlers as H
from . import pipeline_stages as stages
from . import tierguard as TG


def run_ingest(core: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Drain the drop zone through the ordered secure stage pipeline."""
    from . import pipeline as facade

    vault = core.vault
    inbox = facade.inbox_dir(vault)
    report: dict[str, Any] = {
        "processed": [],
        "quarantined": [],
        "duplicates": [],
        "skipped": [],
        "dry_run": dry_run,
    }
    if not inbox.is_dir():
        report["reason"] = "no-inbox-dir"
        return report
    processing_dir = inbox / facade.PROCESSING_DIRNAME
    quarantine_dir = inbox / facade.QUARANTINE_DIRNAME
    duplicate_dir = inbox / facade.DUPLICATE_DIRNAME
    if dry_run:
        candidates, symlinks = _scan_candidates(inbox)
        return _dry_run_preview(vault, report, candidates, symlinks)
    failures = facade._load_failures(vault)
    manifest = facade._load_manifest(vault)
    drain = stages.DrainRecord(
        core=core,
        vault=vault,
        inbox=inbox,
        processing_dir=processing_dir,
        quarantine_dir=quarantine_dir,
        duplicate_dir=duplicate_dir,
        manifest=manifest,
        failures=failures,
        guard=TG.guard_for(core),
        today=_dt.date.today().isoformat(),
        report=report,
    )
    drain = stages.sweep_stage(drain)
    candidates, symlinks = _scan_candidates(inbox)
    if not _load_release_guard(drain, candidates):
        return report
    _quarantine_symlinks(drain, symlinks)
    for path in candidates:
        record = stages.ClaimRecord(drain=drain, path=path, orig_name=path.name)
        record = stages.claim_stage(record)
        record = stages.nofollow_read_stage(record)
        record = stages.acceptance_anchor_stage(record)
        if record.stop_drain:
            break
        if record.terminal:
            continue
        try:
            record = stages.process_verified_claim(record)
        except Exception as exc:
            if _handle_processing_exception(record, exc):
                break
            continue
        _finish_successful_attempt(record)
    report["tier_guard"] = drain.guard.counts.as_dict()
    return report


def _scan_candidates(inbox: Path) -> tuple[list[Path], list[Path]]:
    from . import pipeline as facade

    reserved = {
        facade.PROCESSING_DIRNAME,
        facade.QUARANTINE_DIRNAME,
        facade.DUPLICATE_DIRNAME,
    }
    candidates = sorted(
        path
        for path in inbox.iterdir()
        if not path.is_symlink()
        and path.is_file()
        and not path.name.startswith(".")
        and path.parent.name not in reserved
    )
    candidates = [path for path in candidates if path.name not in reserved]
    symlinks = [
        path
        for path in inbox.iterdir()
        if path.is_symlink() and not path.name.startswith(".")
    ]
    return candidates, symlinks


def _dry_run_preview(
    vault: Path,
    report: dict[str, Any],
    candidates: list[Path],
    symlinks: list[Path],
) -> dict[str, Any]:
    from .. import cos as COS
    from . import pipeline as facade

    for link in symlinks:
        report["skipped"].append({
            "file": link.name,
            "reason": "symlink_rejected",
        })
    for path in candidates:
        handler = H.handler_for(path)
        if handler is None:
            report["skipped"].append({
                "file": path.name,
                "reason": "no_handler_for_extension",
            })
            continue
        if not handler.available():
            report["skipped"].append({
                "file": path.name,
                "reason": f"missing_dependency:{handler.dependency_name}",
            })
            continue
        try:
            preview = COS.read_nofollow(path, max_bytes=facade.MAX_INGEST_BYTES)
        except COS.ApprovedTooLarge:
            report["quarantined"].append({
                "file": path.name,
                "reason": "file_too_large",
            })
            continue
        except COS.ApprovedRefused:
            report["skipped"].append({"file": path.name, "reason": "unreadable"})
            continue
        result = facade._extract_verified(handler, preview, path.suffix, vault)
        if result.ok:
            report["processed"].append({"file": path.name, "would_write": True})
        else:
            report["quarantined"].append({
                "file": path.name,
                "reason": result.quarantine_reason,
            })
    return report


def _load_release_guard(drain: stages.DrainRecord, candidates: list[Path]) -> bool:
    from .. import cos as COS

    try:
        drain.released_shas = released_without_anchor(drain.vault, drain.manifest)
    except COS.ReleaseRecordsUnreadable as exc:
        COS.log_defect(
            drain.vault,
            "attachment-release-records-unreadable",
            f"{exc} — no file ingested this run",
        )
        drain.report["skipped"].extend(
            {
                "file": path.name,
                "reason": f"systemic_error:{type(exc).__name__}: {exc}",
            }
            for path in candidates
        )
        return False
    return True


def _quarantine_symlinks(drain: stages.DrainRecord, symlinks: list[Path]) -> None:
    from . import pipeline as facade

    for link in symlinks:
        try:
            facade._quarantine(
                link,
                drain.quarantine_dir,
                "symlink_rejected",
                ["symlinks are not ingested (would read outside the vault)"],
            )
            drain.report["quarantined"].append({
                "file": link.name,
                "reason": "symlink_rejected",
            })
        except OSError:
            drain.report["skipped"].append({
                "file": link.name,
                "reason": "symlink_rejected",
            })


def _handle_processing_exception(record: stages.ClaimRecord, exc: Exception) -> bool:
    """Account for one failed attempt; return whether the drain must stop."""
    from . import pipeline as facade

    assert record.claimed is not None
    if facade._is_systemic_error(exc):
        if record.claimed.exists():
            facade._move(
                record.claimed,
                facade._unique_dest(record.drain.inbox, record.claimed.name),
            )
        record.append("skipped", {
            "file": record.path.name,
            "reason": f"systemic_error:{type(exc).__name__}: {exc}",
        })
        return True
    count = record.drain.failures.get(record.original_sha, 0) + 1
    record.drain.failures[record.original_sha] = count
    facade._save_failures(record.drain.vault, record.drain.failures)
    if not record.claimed.exists():
        return False
    if count >= facade.MAX_INGEST_FAILURES:
        reason = "repeated_ingest_failure"
        facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, [
            f"{type(exc).__name__}: {exc}",
            f"failed {count} time(s), giving up",
        ])
        record.append("quarantined", {"file": record.path.name, "reason": reason})
        record.drain.failures.pop(record.original_sha, None)
        facade._save_failures(record.drain.vault, record.drain.failures)
    else:
        facade._move(
            record.claimed,
            facade._unique_dest(record.drain.inbox, record.claimed.name),
        )
        record.append("skipped", {
            "file": record.path.name,
            "reason": (
                f"processing_error:{type(exc).__name__} "
                f"(attempt {count}/{facade.MAX_INGEST_FAILURES})"
            ),
        })
    return False


def _finish_successful_attempt(record: stages.ClaimRecord) -> None:
    from .. import cos as COS
    from . import pipeline as facade

    if record.original_sha in record.drain.failures:
        record.drain.failures.pop(record.original_sha, None)
        facade._save_failures(record.drain.vault, record.drain.failures)
    if record.anchor is not None:
        COS.clear_attachment_anchor(
            record.drain.vault,
            record.anchor.get("dest") or record.path,
            record.original_sha,
        )


_LEGACY_CLAIM_STORE = "ingest-provenance.json"


def released_without_anchor(vault: Path, manifest: dict[str, str]) -> set[str]:
    """Return released attachment hashes that no longer have a live anchor."""
    from .. import config
    from .. import cos as COS

    shas = {
        record["sha256"]
        for record in COS.attachment_release_records(vault)
        if record["sha256"] and record["sha256"] not in manifest
    }
    legacy = config.brain_runtime_dir(vault) / _LEGACY_CLAIM_STORE
    if legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise COS.ReleaseRecordsUnreadable(
                f"{_LEGACY_CLAIM_STORE}: {type(exc).__name__}: {exc}"
            ) from None
        if isinstance(data, dict):
            shas.update(key for key in data if key not in manifest)
        legacy.unlink(missing_ok=True)
    return shas
