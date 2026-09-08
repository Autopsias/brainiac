"""COS ingest-manifest claims."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_store import (
    _attachment_meta_path, _manifest_line_key, _sweep_claims_path, _sweep_max_bytes,
    _sweep_recency_seconds, _write_attachment_meta, attachment_quarantine_dir,
    ingest_manifest_dir,
)
from ._attachment_store import record_sweep_decline
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
    # AND OFF THE MOUNT WHEN IT IS A DECLINE. The row above is the sweep's
    # idempotency key and lives in the VM-writable drop tree; a `refused`/
    # `duplicate` word there SETTLES an attachment-carrying thread's ATT-03
    # gate, so it is recorded again where the untrusted leg cannot write it and
    # only the host record is read for that decision
    # (`_attachment_store.line_settlements`).
    if disposition.startswith(("refused", "duplicate")):
        record_sweep_decline(vault, key=key, disposition=disposition,
                             msg_key=str(entry.get("msg_key") or ""), now=now)
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
        # NO CLAIM, NO DECLINE — the same fail-CLOSED reading as the stale-mtime
        # refusal below, and for a sharper reason (adversarial review,
        # 2026-09-05). THIS BRANCH NEVER TOUCHES THE FILESYSTEM: it is decided
        # from the manifest entry's own `filename`, and the manifest lives under
        # `drop_dir`, which the untrusted leg writes — so a host decline here let
        # ONE appended line settle a thread whose attachment was never offered,
        # chipping it `Ingested` with its bytes never fetched. The other three
        # declines each require a real file the HOST's fetch lane placed in
        # `downloads` (host home, off the mount), so they still settle. Full
        # reasoning and the probe: `docs/cos-ops.md` §6f, defect 4.
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
        # STATED CEILING (review 2026-09-05): this refusal writes NO claims row
        # and NO host decline, so the manifest line stays `unclaimed` forever
        # and — under ATT-03 — holds its thread out of the ingestion chip for
        # good. That is deliberate and it is the fail-CLOSED reading: the file
        # on disk is a pre-existing host file the VM manifest cannot claim, so
        # the vault genuinely does NOT have those bytes and must not read as if
        # it did. Recording a `refused: stale` disposition here would settle the
        # thread on the strength of the sweep having been too slow, which is the
        # 596-lost-candidates shape (ATT-02) with a new name. The real cure is
        # for the bytes to be re-fetched — which the next night does whenever
        # the thread is still in the mailbox — not for the gate to relent.
        _report_unmatched(report, [filename],
                          f"not a fresh download: host mtime is {age / 3600.0:.1f}h old "
                          f"(recency window {_sweep_recency_seconds() // 3600}h) — a pre-existing host "
                          "file the VM manifest cannot claim")
        return None
    expected_size = entry.get("approx_size_bytes")
    tolerance = max(expected_size * INGEST_SWEEP_SIZE_TOLERANCE, INGEST_SWEEP_SIZE_FLOOR) if isinstance(expected_size, int) else 0
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
    # ...AND THE SAME BOUND IN THE OTHER DIRECTION. Until 2026-08-23 this check
    # only refused a file OLDER than its manifest line, so a line could claim a
    # file downloaded 40 days after it was written. `_sweep_manifest_lines`
    # walks manifests in FILENAME sort order, which is chronological, so the
    # OLDEST line matching a name claims first. Measured on a live vault:
    # a 2026-07-14 line carrying `attachment_filename` for a PDF — no
    # `approx_size_bytes`, so no size check could stop it — was first in line to
    # claim the same-named PDF that run 178 fetched over its own envelope, which
    # would have anchored it `unclassified` with NO provenance while run 178's
    # own line, carrying the conversation id, sender and `working-draft`, went
    # unmatched. A line and its file belong to ONE download episode; the recency
    # window is already this design's name for that episode.
    # MEASURED AGAINST THE SWEEP'S OWN CLOCK, not the raw mtime. The sweep reads
    # the downloads directory AT `now`, so a file cannot have been fetched after
    # that instant — an mtime past it is a clock artifact, never evidence. The
    # sibling freshness check above already bounds mtime to `[now - 6h, ...)`;
    # clamping here bounds it from the other side so both read the same clock.
    # Without this, 49 tests broke: they inject a frozen `now` while writing
    # their files at the real one, so every same-episode pair measured weeks
    # apart. The refusal it exists for is UNAFFECTED — a live sweep's `now` is
    # the real clock, so run 178's 965h gap still refuses.
    fetched_at = min(stat.st_mtime, now.timestamp())
    if entry_time is not None and fetched_at > entry_time.timestamp() + _sweep_recency_seconds():
        age_hours = (fetched_at - entry_time.timestamp()) / 3600.0
        _report_unmatched(report, [filename],
                          f"fresh namesake: file mtime is {age_hours:.1f}h NEWER than the manifest's "
                          f"download ts {entry_time.isoformat()} (recency window "
                          f"{_sweep_recency_seconds() // 3600}h) — this file was fetched for a "
                          "different, later manifest line")
        return None
    return candidate, filename


def _attachment_metadata(entry: dict[str, Any], *, aid: str, file_sha: str, filename: str,
                         destination: Path, category: str, disposition: str, tier: str,
                         claim: dict[str, Any], line_key: str,
                         now: _dt.datetime) -> dict[str, Any]:
    """Build one attachment-quarantine sidecar.

    ``manifest_line_key`` is what makes a later WITHDRAWAL designate the line
    it settles rather than borrowing a name off the mount — see
    :func:`brain.cos.line_settlements_path`. It is written here, at claim
    time, because this is the only moment the host holds both the payload and
    the manifest entry it came from.
    """
    rules_version = entry.get("extraction_rules_version")
    return {
        "id": aid, "sha256": file_sha, "filename": filename, "path": str(destination),
        "lane": LANE_ATTACHMENT, "category": category, "disposition": disposition, "tier": tier,
        "rules_version": rules_version, "pattern": entry.get("pattern"),
        "bundle_version": entry.get("bundle_version"), "kind": "attachment",
        "msg_key": provenance.sanitize_value(entry.get("msg_key")), "provenance": claim,
        "manifest_line_key": str(line_key),
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
            category=category, disposition=disposition, tier=tier, claim=claim,
            line_key=key, now=now))
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


def _default_downloads_dir() -> tuple[Path | None, str]:
    """The engine's own staging directory, when the environment names none.

    `tools/cos_ctl.sh` defaults `$BRAIN_COS_DOWNLOADS_DIR` to
    `~/.brain/cos-downloads`, and `tools/cos_attachment_fetch.py` writes
    wherever that variable points — so on a host that never set it the fetch
    lands there and the sweep, which read nothing, reported itself disabled.
    This closes that case and only that case.

    IT IS NOT WHAT WENT WRONG ON THE REFERENCE HOST — the first version of this
    docstring said it was, and the review was right to refuse it. Re-measured
    2026-09-05 by reading every live plist: TWO launchd jobs set the variable
    to TWO directories. `com.brainiac.cos-nightly.plist` (the job that fetches)
    says `~/.brain/cos-downloads`, which held 79 files; the maintain job
    `com.brainiac.nightly.<id>.plist` (the job that sweeps) says a second,
    unrelated staging directory outside the engine default
    (`.../<some-workspace>/_cos_downloads`), which held 0 with an mtime of
    2026-09-01. Neither is unset and neither is wrong alone, so nothing
    reported a fault. A configured value still wins here unconditionally; what
    answers the real defect is `_misconfigured_staging`, which makes the sweep
    SAY the configured directory is empty of the files the manifest names while
    the engine default holds them.

    It answers `None` when the directory is absent — a host with no attachment
    lane has nothing to sweep and must not have one invented for it — and the
    refusals that matter are re-applied by the caller either way: a symlink or
    a resolved `~/Downloads` is still refused, whatever named it.
    """
    d = Path(DEFAULT_INGEST_SWEEP_DOWNLOADS_DIR).expanduser()
    try:
        if not d.is_dir():
            return None, "absent"
    except OSError:
        return None, "absent"
    return d, "engine-default"


def _misconfigured_staging(report: dict[str, Any], downloads: Path,
                           source: str) -> None:
    """Name a CONFIGURED staging directory that has none of the manifest's
    files while the engine's own default has them. NEVER swaps to it.

    THE FAILURE THIS EXISTS FOR IS SILENT AND LASTED FOUR DAYS. The sweep read
    a real, configured, EMPTY directory and reported a perfectly ordinary zero:
    `moved: []`, `refused: []`, no `disabled_reason`, nothing to read as a
    fault. Meanwhile the fetch lane, which takes its directory from a different
    environment, was filling `~/.brain/cos-downloads` — 79 files by 2026-09-05,
    5 sweep passes that day, 0 claims since 2026-09-01.

    It REPORTS rather than repairs, and that is deliberate. Silently sweeping a
    directory the operator did not name would make the configured value a
    suggestion, and the whole reason this lane refuses `~/Downloads` is that
    WHICH directory is swept is a security decision. Repointing the launchd job
    is the owner's action; the engine may not write one.

    Cheap by construction: it runs only when the sweep moved nothing, and it
    stats the names the manifest already asked for.
    """
    if source != "configured" or report.get("moved"):
        return
    names = {str(n) for n in (report.get("unmatched") or []) if n}
    if not names:
        return
    default, _ = _default_downloads_dir()
    if default is None:
        return
    try:
        if default.resolve() == downloads.resolve():
            return
    except OSError:
        return
    found = sorted(n for n in names if (default / n).is_file())
    if not found:
        return
    report["misconfigured"] = {
        "configured": str(downloads),
        "engine_default": str(default),
        "manifest_names_found_in_default": len(found),
        "manifest_names_unmatched": len(names),
        "examples": found[:5],
        "detail": (
            f"{len(found)} of {len(names)} file(s) this run's ingest-manifest "
            f"names are in {default} but NOT in the configured "
            f"{INGEST_SWEEP_DOWNLOADS_ENV} ({downloads}). The fetch lane and "
            "the sweep are pointed at different directories. Nothing was "
            "swept from the default: repoint the job that runs `brain "
            "maintain`, or unset the variable so the engine default applies."),
    }


def ingest_sweep(vault, *, downloads_dir: Path | str | None = None,
                 dry_run: bool = False,
                 now: _dt.datetime | None = None) -> dict[str, Any]:
    """Claim fresh host downloads named by unclaimed ingest-manifest lines."""
    now = now or _utcnow()
    configured = downloads_dir or os.environ.get(INGEST_SWEEP_DOWNLOADS_ENV)
    downloads, source = ((Path(configured).expanduser(), "configured")
                         if configured else _default_downloads_dir())
    report = _sweep_report(downloads, dry_run)
    report["downloads_dir_source"] = source
    if downloads is None:
        report["disabled_reason"] = (
            f"set {INGEST_SWEEP_DOWNLOADS_ENV} to a dedicated host-only download staging directory; "
            f"shared ~/Downloads is never swept, and the engine's own default "
            f"({DEFAULT_INGEST_SWEEP_DOWNLOADS_DIR}) does not exist on this host")
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
    _misconfigured_staging(report, downloads, source)
    return report


__all__ = ["ingest_sweep", "_default_downloads_dir",
           "_misconfigured_staging"]
