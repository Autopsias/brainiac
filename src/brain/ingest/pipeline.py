"""Expose the stable ingest orchestration surface."""
from __future__ import annotations

import datetime as _dt
import errno
import os
import re
from pathlib import Path
from typing import Any

from . import handlers as H
from . import pipeline_files as files
from . import tierguard as TG
from ..audit import AuditError

INBOX_DIRNAME = "inbox"
PROCESSING_DIRNAME = "_processing"
QUARANTINE_DIRNAME = "_quarantine"
DUPLICATE_DIRNAME = "_duplicate"
OPERATIONAL_DIRNAME = "_operational"
MANIFEST_RELPATH = ("ingest-manifest.json",)
FAILURES_RELPATH = ("ingest-failures.json",)

MAX_INGEST_FAILURES = 3
STALE_PROCESSING_SECONDS = 15 * 60
MAX_INGEST_BYTES = 200 * 1024 * 1024
MAX_NESTED_DEPTH = 3
MAX_TOTAL_NESTED_BYTES = 500 * 1024 * 1024
MAX_TOTAL_NESTED_ITEMS = 1000

_SYSTEMIC_OSERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EROFS})


def _is_systemic_error(exc: BaseException) -> bool:
    """Return whether an exception represents a batch-wide host outage."""
    if isinstance(exc, AuditError):
        return True
    return isinstance(exc, OSError) and exc.errno in _SYSTEMIC_OSERRNOS


def inbox_dir(vault: Path) -> Path:
    return vault / INBOX_DIRNAME


# Compatibility aliases: callers and tests import these hardened helpers from
# ``brain.ingest.pipeline``. Their implementations live with the filesystem
# transitions, while the public/private import surface stays stable.
_manifest_path = files.manifest_path
_load_manifest = files.load_manifest
_save_manifest = files.save_manifest
_failures_path = files.failures_path
_load_failures = files.load_failures
_save_failures = files.save_failures
_sha256_bytes = files.sha256_bytes
_content_key = files.content_key
_slugify_stem = files.slugify_stem
_sanitize_archive_name = files.sanitize_archive_name
_move = files.move_path
_claim = files.claim_path
_unique_dest = files.unique_destination
_sweep_stale_processing = files.sweep_stale_processing
_create_exclusive_or_collision = files.create_exclusive_or_collision
_scratch_dir = files.scratch_directory
_extract_verified = files.extract_verified_buffer
_existing_note_classification = files.existing_note_classification
_set_aside_operational = files.set_aside_operational
_quarantine = files.quarantine_claim


def _yaml_dq_escape(value: str) -> str:
    """Escape a value for a YAML double-quoted scalar."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_frontmatter(meta: dict[str, Any], body: str) -> str:
    """Serialize ingest frontmatter with the established hardened escaping."""
    from .. import frontmatter as fm

    lines = ["---"]
    for key, raw_value in meta.items():
        value = raw_value
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, str):
            value = H.strip_control_chars(value)
            if key.startswith("provenance."):
                lines.append(f"{key}: {fm.yaml_scalar(value)}")
            elif any(char in value for char in ':#"\\'):
                lines.append(f'{key}: "{_yaml_dq_escape(value)}"')
            else:
                lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def capability_report() -> dict[str, dict]:
    return H.capability_report()


def run_ingest(core: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Drain the inbox through the ordered host-side admission stages."""
    from .pipeline_run import run_ingest as run

    return run(core, dry_run=dry_run)


def _released_without_anchor(vault: Path, manifest: dict[str, str]) -> set[str]:
    """Compatibility entry point for the fail-closed released-byte set."""
    from .pipeline_run import released_without_anchor

    return released_without_anchor(vault, manifest)


def _process_claimed(
    claimed: Path,
    orig_name: str,
    *,
    original_bytes: bytes,
    original_sha: str,
    core: Any,
    manifest: dict[str, str],
    guard: TG.CrossTierGuard,
    vault: Path,
    today: str,
    quarantine_dir: Path,
    duplicate_dir: Path,
    processing_dir: Path,
    report: dict[str, Any],
    depth: int = 0,
    budget: dict[str, int] | None = None,
    parent: str | None = None,
    prov: dict[str, Any] | None = None,
) -> None:
    """Preserve the historical one-claim promotion entry point."""
    from .pipeline_stages import ClaimRecord, DrainRecord, process_verified_claim

    drain = DrainRecord(
        core=core,
        vault=vault,
        inbox=quarantine_dir.parent,
        processing_dir=processing_dir,
        quarantine_dir=quarantine_dir,
        duplicate_dir=duplicate_dir,
        manifest=manifest,
        failures={},
        guard=guard,
        today=today,
        report=report,
    )
    record = ClaimRecord(
        drain=drain,
        path=claimed,
        orig_name=orig_name,
        claimed=claimed,
        original_bytes=original_bytes,
        original_sha=original_sha,
        provenance=prov,
        depth=depth,
        budget=budget if budget is not None else {"bytes": 0, "items": 0},
        parent=parent,
    )
    process_verified_claim(record)


def _process_nested(
    nested: list[dict[str, Any]],
    *,
    parent_slug: str,
    depth: int,
    budget: dict[str, int],
    core: Any,
    manifest: dict[str, str],
    guard: TG.CrossTierGuard,
    vault: Path,
    today: str,
    quarantine_dir: Path,
    duplicate_dir: Path,
    processing_dir: Path,
    report: dict[str, Any],
    prov: dict[str, Any] | None = None,
) -> None:
    """Preserve the historical nested-member admission entry point."""
    from .pipeline_nested import process_nested
    from .pipeline_stages import DrainRecord

    drain = DrainRecord(
        core=core,
        vault=vault,
        inbox=quarantine_dir.parent,
        processing_dir=processing_dir,
        quarantine_dir=quarantine_dir,
        duplicate_dir=duplicate_dir,
        manifest=manifest,
        failures={},
        guard=guard,
        today=today,
        report=report,
    )
    process_nested(
        nested,
        parent_slug=parent_slug,
        depth=depth,
        budget=budget,
        drain=drain,
        provenance=prov,
    )


_DOC_DATE_RES = (
    re.compile(r"^(\d{4})[-_. ](\d{1,2})[-_. ](\d{1,2})(?!\d)"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"^(\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?=\.[A-Za-z0-9]+$|$)"),
)


def _derive_document_date(name: str, today: str) -> str | None:
    """Derive a conservative document date from the original filename."""
    for expression in _DOC_DATE_RES:
        match = expression.search(name)
        if not match:
            continue
        year, month, day = (int(group) for group in match.groups())
        if year < 100:
            year += 2000
        try:
            derived = _dt.date(year, month, day)
        except ValueError:
            continue
        if year >= 1990 and derived <= _dt.date.fromisoformat(today):
            return derived.isoformat()
    return None


def _meta(
    slug: str,
    today: str,
    archive_path: Path,
    vault: Path,
    body_sha: str,
    prov: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build source frontmatter before the ENF-04 tier verdict."""
    from .. import provenance as provenance

    meta: dict[str, Any] = {
        "id": slug,
        "type": "source",
        "classification": "Internal",
        "captured": today,
        "origin": str(archive_path.relative_to(vault)),
        "sha256": body_sha,
        "immutable": True,
    }
    document_date = _derive_document_date(archive_path.name, today)
    if document_date and document_date != today:
        meta["document_date"] = document_date
    fields = provenance.claim_from(prov)
    if fields or str((prov or {}).get("lane") or "") == "attachment":
        host_verified = bool((prov or {}).get("verified"))
        tier, _reason = provenance.email_classification(
            vault,
            proposed=(prov or {}).get("classification"),
            category=(prov or {}).get("category"),
            verified_texts=(
                (
                    fields.get("subject", ""),
                    fields.get("sender", ""),
                    archive_path.name,
                )
                if host_verified
                else ()
            ),
        )
        meta["classification"] = tier
        meta.update(provenance.frontmatter_keys(fields, verified=host_verified))
    return meta


def _operational_type(markdown: str) -> str:
    """Return the operational source type declared in leading frontmatter."""
    if os.environ.get("BRAIN_INGEST_ALLOW_OPERATIONAL", "").strip() not in (
        "",
        "0",
        "false",
        "False",
    ):
        return ""
    from ..invariants import OPERATIONAL_SOURCE_TYPES, embedded_source_type

    declared = embedded_source_type(markdown)
    return declared if declared in OPERATIONAL_SOURCE_TYPES else ""
