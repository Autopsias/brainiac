"""COS ingest-manifest claims."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_store import (
    _attachment_meta_path, _manifest_line_key, _sweep_claims_path, _sweep_max_bytes,
    _sweep_recency_seconds, _write_attachment_meta, attachment_quarantine_dir,
    ingest_manifest_dir,
)
from ._criteria import evidence_unit_key
from ._guards import _safe_basename
from ._io import _append_jsonl, _read_jsonl
from ._layout import _env_days, _parse_ts, _ts, _utcnow
from ._learning_ledger import log_defect
from ._taxonomy import ingest_taxonomy, resolve_category


def _sweep_report(downloads_dir: Path | None, dry_run: bool) -> dict[str, Any]:
    """Create an ingest-sweep report."""
    return {"downloads_dir": str(downloads_dir) if downloads_dir else None, "dry_run": dry_run,
            "moved": [], "refused": [], "unmatched": [], "already_claimed": 0}


def _claim_manifest_line(vault, *, dry_run: bool, now: _dt.datetime,
                         claimed_keys: set[str], key: str, entry: dict[str, Any],
                         disposition: str, dest: str | None = None) -> None:
    """Record one consumed manifest line."""
    if dry_run:
        return
    record: dict[str, Any] = {
        "key": key, "msg_key": entry.get("msg_key"), "filename": entry.get("filename"),
        "disposition": disposition, "ts": _ts(now)}
    if dest:
        record["dest"] = dest
    _append_jsonl(_sweep_claims_path(vault), record, vault=vault)
    claimed_keys.add(key)


def _report_unmatched(report: dict[str, Any], names: list[str], reason: str) -> None:
    """Report manifest names without a usable file."""
    report["unmatched"].extend(names)
    report.setdefault("unmatched_reasons", []).extend(
        {"filename": name, "reason": reason} for name in names)


def _manifest_candidate(vault, downloads: Path, entry: dict[str, Any], *, now: _dt.datetime,
                        max_bytes: int, dry_run: bool, claimed_keys: set[str], key: str,
                        report: dict[str, Any]) -> tuple[Path, str] | None:
    """Validate one downloaded file against its manifest line."""
    names = [entry.get(field) for field in ("filename", "expected_filename", "attachment_filename")]
    names = [name for name in names if isinstance(name, str) and name.strip()]
    safe_names = [name for name in (_safe_basename(name) for name in names) if name]
    if not safe_names:
        _claim_manifest_line(vault, dry_run=dry_run, now=now, claimed_keys=claimed_keys, key=key,
                             entry=entry, disposition="refused: unsafe filename (basename only)")
        report["refused"].append({"filename": names[0] if names else None, "reason": "unsafe filename"})
        return None
    filename = next((name for name in safe_names if (downloads / name).exists()), None)
    if filename is None:
        _report_unmatched(report, safe_names, "not present in the downloads dir")
        return None
    candidate = downloads / filename
    if candidate.is_symlink() or not candidate.is_file():
        _claim_manifest_line(vault, dry_run=dry_run, now=now, claimed_keys=claimed_keys, key=key,
                             entry=entry, disposition="refused: symlink / not a regular file")
        report["refused"].append({"filename": filename, "reason": "symlink refused"})
        return None
    stat = candidate.stat()
    if stat.st_size > max_bytes:
        _claim_manifest_line(vault, dry_run=dry_run, now=now, claimed_keys=claimed_keys, key=key,
                             entry=entry, disposition=f"refused: size {stat.st_size} > cap {max_bytes}")
        report["refused"].append({"filename": filename, "reason": "over size cap"})
        return None
    age = now.timestamp() - stat.st_mtime
    if age > _sweep_recency_seconds():
        _report_unmatched(report, [filename],
                          f"not a fresh download: host mtime is {age / 3600.0:.1f}h old "
                          f"(recency window {_sweep_recency_seconds() // 3600}h) — a pre-existing host "
                          "file the VM manifest cannot claim")
        return None
    expected_size = entry.get("approx_size_bytes")
    tolerance = max(expected_size * INGEST_SWEEP_SIZE_TOLERANCE, 4096) if isinstance(expected_size, int) else 0
    if isinstance(expected_size, int) and expected_size > 0 and abs(stat.st_size - expected_size) > tolerance:
        _report_unmatched(report, [filename],
                          f"size mismatch: on disk {stat.st_size}B, manifest expects {expected_size}B "
                          f"(tolerance {int(tolerance)}B) — a DIFFERENT file of the same name")
        return None
    entry_time = _parse_ts(str(entry.get("ts", "")))
    if entry_time is not None and stat.st_mtime < entry_time.timestamp() - INGEST_SWEEP_SKEW_SECONDS:
        age_hours = (entry_time.timestamp() - stat.st_mtime) / 3600.0
        _report_unmatched(report, [filename],
                          f"stale namesake: file mtime is {age_hours:.1f}h OLDER than the manifest's "
                          f"download ts {entry_time.isoformat()} (skew allowance {INGEST_SWEEP_SKEW_SECONDS}s) "
                          "— the VM's download did not land; this is a pre-existing file with the same name")
        return None
    return candidate, filename


def _attachment_metadata(entry: dict[str, Any], *, aid: str, file_sha: str, filename: str,
                         destination: Path, category: str, disposition: str, tier: str,
                         claim: dict[str, Any], now: _dt.datetime) -> dict[str, Any]:
    """Build one attachment-quarantine sidecar."""
    rules_version = entry.get("extraction_rules_version")
    return {
        "id": aid, "sha256": file_sha, "filename": filename, "path": str(destination),
        "lane": LANE_ATTACHMENT, "category": category, "disposition": disposition, "tier": tier,
        "rules_version": rules_version, "pattern": entry.get("pattern"),
        "bundle_version": entry.get("bundle_version"), "kind": "attachment",
        "msg_key": provenance.sanitize_value(entry.get("msg_key")), "provenance": claim,
        "claimed": _ts(now),
        "ttl_expires": _ts(now + _dt.timedelta(days=_env_days(
            PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS))), "state": "pending",
        "evidence_unit": evidence_unit_key(category=category, lane=LANE_ATTACHMENT,
                                             rules_version=rules_version, body=file_sha),
        "evidence_lineage": None,
    }


def _quarantine_manifest_candidate(vault, candidate: Path, filename: str, entry: dict[str, Any], *,
                                   taxonomy: dict[str, Any], now: _dt.datetime, dry_run: bool,
                                   claimed_keys: set[str], key: str, report: dict[str, Any]) -> None:
    """Move one validated download into attachment quarantine."""
    claim = provenance.claim_from(entry.get("provenance"))
    category, disposition = resolve_category(vault, entry.get("category"), lane=LANE_ATTACHMENT,
                                             taxonomy=taxonomy)
    if disposition == DISPOSITION_NEVER:
        _claim_manifest_line(vault, dry_run=dry_run, now=now, claimed_keys=claimed_keys, key=key,
                             entry=entry, disposition=f"refused: never-ingest category {category}")
        log_defect(vault, "never-category-attachment", f"{filename}: category={category}", ts=_ts(now))
        report["refused"].append({"filename": filename, "reason": "never-ingest category"})
        return
    file_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    aid = safe_slug("att-" + file_sha[:12])
    if _attachment_meta_path(vault, aid).exists():
        _claim_manifest_line(vault, dry_run=dry_run, now=now, claimed_keys=claimed_keys, key=key,
                             entry=entry, disposition=f"duplicate: already quarantined as {aid}")
        report.setdefault("duplicates", []).append({"filename": filename, "id": aid})
        return
    quarantine = attachment_quarantine_dir(vault)
    destination = quarantine / f"{aid}{candidate.suffix}"
    tier, _ = provenance.email_classification(vault, proposed=entry.get("classification"), category=category)
    if not dry_run:
        quarantine.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(quarantine, 0o700)  # nosemgrep: insecure-file-permissions -- host-private quarantine
        except OSError:
            pass
        shutil.move(str(candidate), destination)
        _write_attachment_meta(vault, _attachment_metadata(
            entry, aid=aid, file_sha=file_sha, filename=filename, destination=destination,
            category=category, disposition=disposition, tier=tier, claim=claim, now=now))
    _claim_manifest_line(vault, dry_run=dry_run, now=now, claimed_keys=claimed_keys, key=key,
                         entry=entry, disposition="quarantined", dest=str(destination))
    report["moved"].append(provenance.scrub({
        "filename": filename, "dest": str(destination), "id": aid, "awaiting_verdict": True,
        "category": category, "msg_key": entry.get("msg_key"),
        **({"provenance": claim} if claim else {}),
    }))


def _sweep_manifest_lines(vault, manifests: Path, downloads: Path, *, taxonomy: dict[str, Any],
                          now: _dt.datetime, dry_run: bool, claimed_keys: set[str],
                          max_bytes: int, report: dict[str, Any]) -> None:
    """Claim eligible entries from ingest manifests."""
    for manifest in sorted(manifests.glob("manifest-*.jsonl")):
        if manifest.is_symlink() or not manifest.is_file():
            continue
        for entry in _read_jsonl(manifest):
            key = _manifest_line_key(entry)
            if key in claimed_keys:
                report["already_claimed"] += 1
                continue
            candidate = _manifest_candidate(
                vault, downloads, entry, now=now, max_bytes=max_bytes, dry_run=dry_run,
                claimed_keys=claimed_keys, key=key, report=report)
            if candidate is not None:
                path, filename = candidate
                _quarantine_manifest_candidate(
                    vault, path, filename, entry, taxonomy=taxonomy, now=now, dry_run=dry_run,
                    claimed_keys=claimed_keys, key=key, report=report)


def ingest_sweep(vault, *, downloads_dir: Path | str | None = None,
                 dry_run: bool = False,
                 now: _dt.datetime | None = None) -> dict[str, Any]:
    """Claim fresh host downloads named by unclaimed ingest-manifest lines."""
    now = now or _utcnow()
    configured = downloads_dir or os.environ.get(INGEST_SWEEP_DOWNLOADS_ENV)
    downloads = Path(configured).expanduser() if configured else None
    report = _sweep_report(downloads, dry_run)
    if downloads is None:
        report["disabled_reason"] = (
            f"set {INGEST_SWEEP_DOWNLOADS_ENV} to a dedicated host-only download staging directory; "
            "shared ~/Downloads is never swept")
        return report
    if downloads.is_symlink() or downloads.resolve() == (Path.home() / "Downloads").resolve():
        report["disabled_reason"] = "refusing shared or symlinked ~/Downloads; configure a dedicated host-only staging directory"
        return report
    downloads = downloads.resolve()
    report["downloads_dir"] = str(downloads)
    manifests = ingest_manifest_dir(vault)
    if not manifests.is_dir():
        return report
    claimed = {claim.get("key") for claim in _read_jsonl(_sweep_claims_path(vault))}
    _sweep_manifest_lines(
        vault, manifests, downloads, taxonomy=ingest_taxonomy(vault, log=True), now=now,
        dry_run=dry_run, claimed_keys=claimed, max_bytes=_sweep_max_bytes(), report=report)
    return report


__all__ = ["ingest_sweep"]
