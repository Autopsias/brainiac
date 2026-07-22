"""ING-01 orchestrator: dispatcher + quarantine + immutable archival + audited
promotion. Pure(ish) — the only I/O is the drop zone / vault filesystem and
(non-dry-run) ``BrainCore.write_note``. Never called with role=vm: BrainCore
refuses ``ingest_dropzone`` via ``_require_host`` BEFORE this module is even
imported, so a VM leg has zero side effects here (S06 hard guarantee, mirrors
``write_note``/``drain_drafts``).

HARDENED (codex + grill):
  - concurrency: each drop-zone file is CLAIMED via an atomic ``os.rename``
    into ``inbox/_processing/`` before extraction, so a manual ``brain
    ingest`` and the scheduled ``maintain`` drain can never double-process
    the same file (the loser's rename raises ``OSError`` and it's skipped).
  - immutability-safe writes: the archived original and the ``raw/`` source
    are CREATE-EXCLUSIVE — same sha256 at the target = idempotent no-op,
    different sha256 = quarantine as a collision (never silently overwritten).
  - duplicate-content idempotency: a manifest keyed by the ORIGINAL file's
    sha256 makes re-ingesting identical bytes a no-op (moved to
    ``inbox/_duplicate/`` with a report line, never re-signed).
  - quality gate: nothing reaches ``write_note`` unless the handler's
    extraction passed its density/encryption/size gates (see
    ``handlers.base.density_gate`` + each handler's own guards).
"""
from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import handlers as H
from ..audit import AuditError
from ..notes import safe_slug

INBOX_DIRNAME = "inbox"
PROCESSING_DIRNAME = "_processing"
QUARANTINE_DIRNAME = "_quarantine"
DUPLICATE_DIRNAME = "_duplicate"
MANIFEST_RELPATH = ("ingest-manifest.json",)  # under .brain/
FAILURES_RELPATH = ("ingest-failures.json",)  # under .brain/ — per-file retry counter (C2)

# C2: a file that deterministically fails processing must not be retried
# forever (that would starve every later-alphabetical candidate's chance to
# ever be reported as "quarantined" and clutter the inbox root indefinitely
# with one file bouncing back and forth). After this many failed attempts
# (across separate run_ingest calls) it is quarantined instead of retried.
MAX_INGEST_FAILURES = 3

# B1: infra-wide outages (no signing key resolved; disk full/read-only fs)
# are NOT a per-file defect — they hit every remaining candidate in the batch
# identically. Counting one against the per-file poison counter (and
# eventually quarantining a perfectly good file) mistakes an outage for a
# poison file; see _is_systemic_error below.
_SYSTEMIC_OSERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EROFS})


def _is_systemic_error(exc: BaseException) -> bool:
    """True for a batch-wide outage (signing-key unavailable, or an OSError
    whose errno says disk-full/quota/read-only-fs) as opposed to a per-file
    extraction/content defect. ``AuditError`` covers ``KeyUnavailable`` and
    any sibling audit/signing failure (e.g. the 'cryptography' package
    missing) — anything raised by ``core.write_note``'s signing step."""
    if isinstance(exc, AuditError):
        return True
    if isinstance(exc, OSError) and exc.errno in _SYSTEMIC_OSERRNOS:
        return True
    return False


# HARDENED (code-review rework): a claim (rename into _processing/) followed
# by a crash before the file is unlinked/moved out would strand it forever —
# run_ingest only scans the inbox ROOT. Files older than this are swept back
# to the inbox root at the start of the NEXT run_ingest call (crash backstop).
# Files younger than this are left alone: they may belong to another process
# that is CURRENTLY (and legitimately) extracting them — sweeping those would
# break the atomic-claim guarantee. The per-file try/except below (moves the
# claim back to inbox on any in-process exception) is the primary mechanism;
# this sweep only catches a hard crash (killed process, power loss) that never
# got to run its except-clause.
STALE_PROCESSING_SECONDS = 15 * 60

# Cap checked via stat() BEFORE any read_bytes() of the claimed file, so a
# pathological multi-GB drop never loads fully into memory before rejection.
# Matches the largest per-handler cap (pdf.py); handlers still enforce their
# own (possibly smaller) cap on top of this.
MAX_INGEST_BYTES = 200 * 1024 * 1024

# S06 (ING-03): a handler (zip, eml) can return ``metadata["nested"]`` —
# member/attachment bytes that re-enter the SAME dispatcher as their own
# ingest candidates. Bounded on TWO axes so a nested-archive-of-archives
# ("zip bomb via nesting") can't blow past each handler's own per-level caps:
#   - MAX_NESTED_DEPTH: how many levels of re-entry (zip-in-zip, eml
#     attachment that is itself a zip, ...) before giving up.
#   - a shared per-top-level-candidate BUDGET (bytes + item count) threaded
#     through the WHOLE recursion tree, not just checked per level — each
#     handler already caps its own single-level ``nested`` list, but nesting
#     N archives each at their own cap multiplies past any single-level limit.
MAX_NESTED_DEPTH = 3
MAX_TOTAL_NESTED_BYTES = 500 * 1024 * 1024
MAX_TOTAL_NESTED_ITEMS = 1000


def inbox_dir(vault: Path) -> Path:
    return vault / INBOX_DIRNAME


def _manifest_path(vault: Path) -> Path:
    from .. import config

    return config.brain_runtime_dir(vault) / MANIFEST_RELPATH[0]


def _load_manifest(vault: Path) -> dict[str, str]:
    path = _manifest_path(vault)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_manifest(vault: Path, manifest: dict[str, str]) -> None:
    path = _manifest_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic on same filesystem


def _failures_path(vault: Path) -> Path:
    from .. import config

    return config.brain_runtime_dir(vault) / FAILURES_RELPATH[0]


def _load_failures(vault: Path) -> dict[str, int]:
    path = _failures_path(vault)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_failures(vault: Path, failures: dict[str, int]) -> None:
    path = _failures_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(failures, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_key(path: Path) -> str:
    """E5: the stable key for the per-file retry/failure counter. Keyed on
    the CONTENT hash rather than the filename — ``_claim`` disambiguates a
    same-named collision in ``_processing/`` (e.g. ``poison.1.pdf``), and a
    failed attempt is re-dropped into the inbox under THAT (possibly renamed)
    name. A name-keyed counter loses its accumulated count the moment the
    name changes; the sha256 of the bytes is stable across every rename."""
    return _sha256_bytes(path.read_bytes())


_SLUG_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


def _slugify_stem(stem: str) -> str:
    cleaned = _SLUG_SANITIZE.sub("-", stem).strip("-").lower()
    return cleaned or "file"


# C7/B2: chars that either break the hand-rolled double-quoted YAML wrapping
# in _build_frontmatter (`"`) or are unsafe/awkward across filesystems (`:`,
# `\`) — a hostile/careless original filename (e.g. `report:"final".pdf`)
# must never be able to bake malformed YAML into a signed, immutable raw/
# note. Also strip ALL control characters (0x00-0x1F incl. newline/tab, and
# DEL 0x7F) — an embedded literal newline flows into `origin:` as a bare
# unescaped scalar and corrupts the YAML just as surely as an unescaped quote.
_ARCHIVE_NAME_SANITIZE = re.compile(r'[\x00-\x1f\x7f:"\\]')


def _sanitize_archive_name(name: str) -> str:
    """Sanitize the archived-original's filename component (used for BOTH the
    on-disk archive path and the `origin:` frontmatter value it flows into)
    so it can never carry a character that would corrupt the signed
    frontmatter or misbehave on a legacy filesystem."""
    cleaned = _ARCHIVE_NAME_SANITIZE.sub("_", name)
    return cleaned or "file"


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError:
        shutil.move(str(src), str(dest))  # cross-device fallback


def _claim(path: Path, processing_dir: Path) -> Path | None:
    """Atomically claim ``path`` for this process. Returns the claimed path,
    or ``None`` if another process/thread claimed it first (or it vanished)."""
    processing_dir.mkdir(parents=True, exist_ok=True)
    dest = processing_dir / path.name
    i = 0
    while dest.exists():
        i += 1
        dest = processing_dir / f"{path.stem}.{i}{path.suffix}"
    try:
        os.rename(path, dest)
    except OSError:
        return None
    # C1: os.rename PRESERVES the source's mtime, so a file that was old
    # (e.g. downloaded/copied, keeping its original timestamp) looks
    # instantly "stale" to _sweep_stale_processing the moment it's claimed —
    # a concurrent drain would then sweep this LIVE claim back to the inbox.
    # Touch to now so the processing-dir entry's mtime measures CLAIM time,
    # not the source file's original timestamp.
    try:
        os.utime(dest, None)
    except OSError:
        pass  # non-fatal: worst case the staleness backstop is slightly off
    return dest


def _unique_dest(target_dir: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = target_dir / name
    i = 0
    while dest.exists():
        i += 1
        dest = target_dir / f"{stem}.{i}{suffix}"
    return dest


def _sweep_stale_processing(
    processing_dir: Path, inbox: Path, *,
    vault: Path, quarantine_dir: Path, failures: dict[str, int],
) -> None:
    """Crash backstop: rescue files stranded in ``_processing/`` by a process
    that claimed them and then died before finishing (see
    ``STALE_PROCESSING_SECONDS``). Only sweeps files older than the staleness
    threshold, so a concurrently-running claim by another live process is
    never touched.

    E3: a process death (OOM/segfault) mid-extraction is indistinguishable
    from a deterministically-poison file after enough attempts — without this,
    a file that reliably kills the process is swept back and reclaimed FOREVER
    (never counted, never quarantined), and every nightly ``maintain`` dies
    before ``index.sync``/publish ever runs. Count each sweep as a failed
    attempt against the SAME persisted counter the in-process per-file
    handler uses (keyed on content sha256, see E5/``_content_key``), so a
    crash-looping file quarantines after ``MAX_INGEST_FAILURES`` just like an
    in-process exception would."""
    if not processing_dir.is_dir():
        return
    now = _dt.datetime.now().timestamp()
    touched = False
    for stuck in list(processing_dir.iterdir()):
        if not stuck.is_file():
            continue
        try:
            age = now - stuck.stat().st_mtime
        except OSError:
            continue
        if age < STALE_PROCESSING_SECONDS:
            continue
        try:
            key = _content_key(stuck)
        except OSError:
            key = stuck.name  # unreadable; fall back to name-keying
        count = failures.get(key, 0) + 1
        failures[key] = count
        touched = True
        if count >= MAX_INGEST_FAILURES:
            reason = "repeated_ingest_failure"
            _quarantine(
                stuck, quarantine_dir, reason,
                [f"swept from stale {PROCESSING_DIRNAME}/ {count} time(s) — "
                 "process likely died mid-extraction (crash-death is "
                 "indistinguishable from poison after N attempts); giving up"],
            )
            failures.pop(key, None)
        else:
            _move(stuck, _unique_dest(inbox, stuck.name))
    if touched:
        _save_failures(vault, failures)


def _create_exclusive_or_collision(dest: Path, data: bytes, known_sha: str | None = None) -> str:
    """Write ``data`` to ``dest`` create-exclusive. Returns "written",
    "idempotent" (dest already holds identical bytes), or "collision" (dest
    holds DIFFERENT bytes — never overwritten).

    ``known_sha`` lets a caller that already hashed ``data`` (e.g. for the
    manifest content-key) skip re-hashing a potentially large buffer on the
    collision-check path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = dest.read_bytes()
        if existing == data:
            return "idempotent"
        data_sha = known_sha if known_sha is not None else _sha256_bytes(data)
        return "idempotent" if _sha256_bytes(existing) == data_sha else "collision"
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return "written"


def _yaml_dq_escape(s: str) -> str:
    """Escape a string for embedding inside a YAML double-quoted scalar."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_frontmatter(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            # S06 HARDENED (root-cause, not per-caller): strip raw control
            # chars (embedded newline/tab/...) from EVERY string value here,
            # in the ONE function every ingest caller routes through —
            # previously a value with a control char but none of `:#"\\`
            # skipped quoting entirely (see the C7 fix below) and could
            # inject a bogus line into signed, immutable frontmatter. This
            # covers both the drop-zone handlers' filenames AND
            # transcript.py's caller-supplied `origin`/`language` without
            # requiring each new caller to remember to pre-sanitize.
            v = H.strip_control_chars(v)
            if ":" in v or "#" in v or '"' in v or "\\" in v:
                # C7: previously wrapped in double quotes WITHOUT escaping an
                # embedded `"` — a value carrying one (e.g. origin embedding a
                # hostile original filename) baked malformed YAML into a signed,
                # immutable raw/ note that could never be fixed. Escape properly.
                lines.append(f'{k}: "{_yaml_dq_escape(v)}"')
            else:
                lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def capability_report() -> dict[str, dict]:
    return H.capability_report()


def run_ingest(core: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Drain the drop zone. ``core`` is a HOST-role ``BrainCore`` — the caller
    (``BrainCore.ingest_dropzone``) already enforced ``_require_host`` before
    calling in, so this function assumes host privileges are available."""
    vault = core.vault
    inbox = inbox_dir(vault)
    report: dict[str, Any] = {
        "processed": [], "quarantined": [], "duplicates": [], "skipped": [],
        "dry_run": dry_run,
    }
    if not inbox.is_dir():
        report["reason"] = "no-inbox-dir"
        return report

    processing_dir = inbox / PROCESSING_DIRNAME
    quarantine_dir = inbox / QUARANTINE_DIRNAME
    duplicate_dir = inbox / DUPLICATE_DIRNAME
    reserved = {PROCESSING_DIRNAME, QUARANTINE_DIRNAME, DUPLICATE_DIRNAME}

    # E6: loaded once, up-front, and threaded through the sweep below too, so
    # a single failures.json read/write pair covers both the crash-backstop
    # sweep and the main per-file loop in this run.
    failures = _load_failures(vault) if not dry_run else {}

    # Crash backstop: rescue anything a PRIOR run left stranded in
    # _processing/ (claimed, then the process died before finishing) so it
    # re-enters this run's candidate scan instead of being lost forever.
    if not dry_run:
        _sweep_stale_processing(
            processing_dir, inbox, vault=vault,
            quarantine_dir=quarantine_dir, failures=failures,
        )

    candidates = sorted(
        # `is_file()` FOLLOWS symlinks, so exclude symlinks explicitly: a
        # symlink dropped in the inbox would otherwise let ingestion read an
        # arbitrary file OUTSIDE the vault (and be a rename/stat/read TOCTOU
        # vector). Regular files only; symlinks are quarantined below.
        p for p in inbox.iterdir()
        if not p.is_symlink() and p.is_file()
        and not p.name.startswith(".") and p.parent.name not in reserved
    )
    symlinks = [p for p in inbox.iterdir()
                if p.is_symlink() and not p.name.startswith(".")]
    # Also skip anything whose top-level parent is a reserved dir (iterdir only
    # lists the inbox root, so this is defense-in-depth, not load-bearing).
    candidates = [p for p in candidates if p.name not in reserved]

    if dry_run:
        for link in symlinks:
            report["skipped"].append({"file": link.name, "reason": "symlink_rejected"})
        for path in candidates:
            handler = H.handler_for(path)
            if handler is None:
                report["skipped"].append({"file": path.name, "reason": "no_handler_for_extension"})
                continue
            if not handler.available():
                report["skipped"].append(
                    {"file": path.name, "reason": f"missing_dependency:{handler.dependency_name}"}
                )
                continue
            # C6: the stat()-based size gate was only applied on the
            # non-dry-run path — dry-run called handler.extract() directly,
            # and a handler like TextHandler does an unconditional
            # read_bytes(), so a pathological multi-GB drop got fully loaded
            # into memory even for a mere preview. Gate BEFORE extract here too.
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size > MAX_INGEST_BYTES:
                report["quarantined"].append({"file": path.name, "reason": "file_too_large"})
                continue
            result = handler.extract(path)
            if result.ok:
                report["processed"].append({"file": path.name, "would_write": True})
            else:
                report["quarantined"].append({"file": path.name, "reason": result.quarantine_reason})
        return report

    manifest = _load_manifest(vault)
    today = _dt.date.today().isoformat()

    # Quarantine rejected symlinks by moving the LINK itself (os.rename never
    # follows it), so a hostile inbox symlink is neither ingested nor left to
    # re-trigger every run — and its target is never opened.
    for link in symlinks:
        try:
            _quarantine(link, quarantine_dir, "symlink_rejected",
                        ["symlinks are not ingested (would read outside the vault)"])
            report["quarantined"].append({"file": link.name, "reason": "symlink_rejected"})
        except OSError:
            report["skipped"].append({"file": link.name, "reason": "symlink_rejected"})

    for path in candidates:
        claimed = _claim(path, processing_dir)
        if claimed is None:
            report["skipped"].append({"file": path.name, "reason": "claimed_elsewhere"})
            continue

        # Size gate BEFORE any read_bytes() of the claimed file, so a
        # pathological multi-GB drop never loads fully into memory before
        # rejection — checked here (ahead of the content-key read below) so
        # the gate still runs first even though _content_key now reads the
        # file too.
        try:
            size = claimed.stat().st_size
        except OSError:
            size = 0
        if size > MAX_INGEST_BYTES:
            reason = "file_too_large"
            _quarantine(claimed, quarantine_dir, reason,
                        [f"{size} bytes exceeds ingest cap {MAX_INGEST_BYTES}"])
            report["quarantined"].append({"file": claimed.name, "reason": reason})
            continue

        # E5: key the per-file retry counter on the CONTENT hash, not the
        # (possibly claim-collision-renamed / retry-renamed) filename.
        original_bytes = claimed.read_bytes()
        original_sha = _sha256_bytes(original_bytes)

        try:
            _process_claimed(
                claimed, path.name, original_bytes=original_bytes,
                original_sha=original_sha, core=core, manifest=manifest,
                vault=vault, today=today, quarantine_dir=quarantine_dir,
                duplicate_dir=duplicate_dir, processing_dir=processing_dir,
                report=report,
            )
        except Exception as exc:
            if _is_systemic_error(exc):
                # B1: KeyUnavailable / disk-full / read-only-fs is an OUTAGE,
                # not a per-file defect — it will hit every remaining
                # candidate in this batch identically. Leave the file for the
                # next drain untouched (no counter bump, no quarantine risk)
                # and stop this run's batch rather than burn through every
                # remaining candidate against the same wall, WITHOUT raising
                # (a systemic outage must never abort the surrounding sync).
                if claimed.exists():
                    _move(claimed, _unique_dest(inbox, claimed.name))
                report["skipped"].append({
                    "file": path.name,
                    "reason": f"systemic_error:{type(exc).__name__}: {exc}",
                })
                break
            # C2: a per-file exception used to be moved back to the inbox
            # AND re-raised, aborting the whole run_ingest call — one
            # deterministically-failing ("poison") file meant `brain sync`
            # exited nonzero on EVERY invocation, so the index never
            # reconciled and the snapshot never republished, forever. Now:
            # retry a few times (moved back to the inbox root, NOT raised, so
            # later candidates in THIS run still get processed), then
            # quarantine it once it's clearly not transient.
            count = failures.get(original_sha, 0) + 1
            failures[original_sha] = count
            _save_failures(vault, failures)
            if not claimed.exists():
                continue
            if count >= MAX_INGEST_FAILURES:
                reason = "repeated_ingest_failure"
                _quarantine(
                    claimed, quarantine_dir, reason,
                    [f"{type(exc).__name__}: {exc}", f"failed {count} time(s), giving up"],
                )
                report["quarantined"].append({"file": path.name, "reason": reason})
                failures.pop(original_sha, None)
                _save_failures(vault, failures)
            else:
                _move(claimed, _unique_dest(inbox, claimed.name))
                report["skipped"].append({
                    "file": path.name,
                    "reason": f"processing_error:{type(exc).__name__} (attempt {count}/{MAX_INGEST_FAILURES})",
                })
            continue
        else:
            # E6: clear the counter entry on success so a since-fixed/
            # renamed file doesn't carry a stale count into a later,
            # unrelated drop that happens to hash the same (vanishingly
            # unlikely, but the entry should never outlive its file anyway).
            if original_sha in failures:
                failures.pop(original_sha, None)
                _save_failures(vault, failures)

    return report


def _existing_note_classification(vault: Path, existing_id: str) -> str | None:
    """E4: the classification of an already-ingested ``raw/<id>.md`` note, so
    a duplicate-report entry can be routed through the same egress gate as
    ``processed`` (a duplicate's ``existing_id`` is a real note id — it must
    not leak an above-max-tier note's identity just because the CONTENT was a
    dedup hit rather than a fresh promotion)."""
    note_path = vault / "raw" / f"{existing_id}.md"
    if not note_path.is_file():
        return None
    from .. import frontmatter as fm

    try:
        meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    val = meta.get("classification")
    return str(val) if val else None


def _process_claimed(
    claimed: Path, orig_name: str, *, original_bytes: bytes, original_sha: str,
    core: Any, manifest: dict[str, str],
    vault: Path, today: str, quarantine_dir: Path, duplicate_dir: Path,
    processing_dir: Path, report: dict[str, Any],
    depth: int = 0, budget: dict[str, int] | None = None, parent: str | None = None,
) -> None:
    """Process one already-claimed file (in ``inbox/_processing/``) to
    completion: quarantine, duplicate, or promote. On success the claimed
    copy is always consumed (moved or unlinked). On any exception the caller
    (``run_ingest`` or, for a nested item, ``_process_nested``) moves the
    (still-existing) claim back to the inbox root / quarantines it so the
    next drain retries it — see the HARDENED note above.

    ``original_bytes``/``original_sha`` are precomputed by the caller (which
    needs the same content hash for its own retry-counter keying, E5) — read
    once, not twice.

    ``depth``/``budget``/``parent`` (S06, ING-03): non-default only when this
    call is a zip member or eml attachment re-entering the dispatcher (see
    ``_process_nested`` below) — all three are their defaults for every
    TOP-LEVEL inbox candidate. ``parent`` (the container's own note id) is
    stamped onto every report entry this call produces, so a nested item's
    provenance is traceable in the ingest report."""
    if budget is None:
        budget = {"bytes": 0, "items": 0}

    def _append(bucket: str, entry: dict[str, Any]) -> None:
        if parent is not None:
            entry["parent"] = parent
        report[bucket].append(entry)

    if original_sha in manifest:
        existing_id = manifest[original_sha]
        _move(claimed, duplicate_dir / claimed.name)
        (duplicate_dir / f"{claimed.name}.duplicate-of.txt").write_text(
            f"identical content already ingested as raw/{existing_id}.md\n",
            encoding="utf-8",
        )
        _append("duplicates", {
            "file": claimed.name, "existing_id": existing_id,
            "classification": _existing_note_classification(vault, existing_id),
        })
        return

    handler = H.handler_for(claimed)
    if handler is None:
        reason = "no_handler_for_extension"
        _quarantine(claimed, quarantine_dir, reason, [])
        _append("quarantined", {"file": claimed.name, "reason": reason})
        return
    if not handler.available():
        reason = f"missing_dependency:{handler.dependency_name}"
        _quarantine(claimed, quarantine_dir, reason, [])
        _append("quarantined", {"file": claimed.name, "reason": reason})
        return

    result = handler.extract(claimed)
    if not result.ok:
        _quarantine(claimed, quarantine_dir, result.quarantine_reason, result.warnings)
        _append("quarantined", {"file": claimed.name, "reason": result.quarantine_reason})
        return

    stem = _slugify_stem(claimed.stem)
    slug = safe_slug(f"{today}-{stem}")
    archive_subdir = vault / "raw" / "originals" / f"{today}-{stem}"
    # C7: the archived filename flows verbatim into the signed `origin:`
    # frontmatter value (and the filesystem path) — sanitize it so a hostile
    # or careless original name can't carry a quote/colon/backslash into
    # either.
    archive_path = archive_subdir / _sanitize_archive_name(claimed.name)

    arch_status = _create_exclusive_or_collision(archive_path, original_bytes, known_sha=original_sha)
    if arch_status == "collision":
        reason = "archive_collision"
        _quarantine(claimed, quarantine_dir, reason,
                    [f"archived-original target already holds different content: {archive_path}"])
        _append("quarantined", {"file": claimed.name, "reason": reason})
        return

    from .. import autolink

    linked_markdown, autolink_added = autolink.apply_autolinks(
        result.markdown, title=orig_name, vault=vault,
    )
    meta = _meta(slug, today, archive_path, vault, hashlib.sha256(linked_markdown.encode("utf-8")).hexdigest())
    body_sha = meta["sha256"]
    classification = meta["classification"]
    note_rel = f"raw/{slug}.md"
    note_path = vault / note_rel
    if note_path.exists():
        # Manifest miss but the target id already exists (e.g. a
        # hand-deleted/corrupted manifest) — defense in depth. Compare the
        # frontmatter `sha256:` of the existing note against this body's
        # hash rather than re-serialising: same body -> idempotent no-op,
        # different -> collision, never overwritten.
        from .. import frontmatter as fm

        existing_meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
        if str(existing_meta.get("sha256", "")) != body_sha:
            reason = "note_id_collision"
            _quarantine(claimed, quarantine_dir, reason,
                        [f"raw/{slug}.md already exists with different content"])
            _append("quarantined", {"file": claimed.name, "reason": reason})
            return
        manifest[original_sha] = slug
        _save_manifest(vault, manifest)
        claimed.unlink(missing_ok=True)
        existing_classification = existing_meta.get("classification")
        _append("duplicates", {
            "file": orig_name, "existing_id": slug,
            "classification": str(existing_classification) if existing_classification else None,
        })
        return

    content = _build_frontmatter(meta, linked_markdown)
    core.write_note(
        note_rel, content,
        reason=f"ingest {orig_name} -> raw/{slug}.md "
               f"(original archived at {archive_path.relative_to(vault)})",
        subtree="raw",
    )
    manifest[original_sha] = slug
    _save_manifest(vault, manifest)
    claimed.unlink(missing_ok=True)  # promoted; the processing copy is spent
    _append("processed", {
        "file": orig_name, "id": slug, "note": note_rel,
        "archived": str(archive_path.relative_to(vault)),
        "classification": classification,
        "warnings": result.warnings,
        "autolink_added": autolink_added,
    })

    # S06 (ING-03): zip members / eml attachments re-enter the SAME
    # dispatcher, one level deeper. Only on a FRESH promotion (never on a
    # duplicate/id-collision return above) — a duplicate top-level archive's
    # members were already fully expanded the first time it was ingested, so
    # re-walking them here would just re-report already-known duplicates for
    # no benefit (and risks re-running expensive recursion on every re-drop
    # of the same file).
    nested = result.metadata.get("nested") if isinstance(result.metadata, dict) else None
    if nested:
        _process_nested(
            nested, parent_slug=slug, depth=depth, budget=budget,
            core=core, manifest=manifest, vault=vault, today=today,
            quarantine_dir=quarantine_dir, duplicate_dir=duplicate_dir,
            processing_dir=processing_dir, report=report,
        )


def _process_nested(
    nested: list[dict], *, parent_slug: str, depth: int, budget: dict[str, int],
    core: Any, manifest: dict[str, str], vault: Path, today: str,
    quarantine_dir: Path, duplicate_dir: Path, processing_dir: Path,
    report: dict[str, Any],
) -> None:
    """Re-enter the dispatcher for each nested (name, data) item a handler
    returned (zip member / eml attachment). Bounded by ``MAX_NESTED_DEPTH``
    and by the shared ``budget`` (bytes + item count) across the WHOLE
    recursion tree for this one top-level candidate — see the module-level
    constants' docstring. A poison nested item is quarantined on its own;
    it never aborts its siblings or the parent's already-completed promotion."""
    if not nested:
        return
    if depth >= MAX_NESTED_DEPTH:
        for item in nested:
            report["skipped"].append({
                "file": H.strip_control_chars(item.get("name") or "?"),
                "reason": "nested_depth_exceeded", "parent": parent_slug,
            })
        return

    for idx, item in enumerate(nested):
        name = H.strip_control_chars(item.get("name") or f"member-{idx}")
        data = item.get("data", b"")
        if budget["items"] >= MAX_TOTAL_NESTED_ITEMS or budget["bytes"] + len(data) > MAX_TOTAL_NESTED_BYTES:
            report["quarantined"].append({
                "file": name, "reason": "nested_budget_exceeded", "parent": parent_slug,
            })
            continue
        budget["items"] += 1
        budget["bytes"] += len(data)

        # Member/attachment names NEVER become filesystem paths directly —
        # only a slugified BASENAME feeds a wholly synthetic temp filename.
        ext = Path(name).suffix.lower()
        safe_stem = _slugify_stem(Path(name).stem)
        synth_name = f"{parent_slug}-nested-{idx}-{safe_stem}{ext}"
        temp_path = _unique_dest(processing_dir, synth_name)
        try:
            processing_dir.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(data)
        except OSError as exc:
            report["skipped"].append({
                "file": name, "reason": f"nested_write_error:{type(exc).__name__}", "parent": parent_slug,
            })
            continue

        try:
            _process_claimed(
                temp_path, name, original_bytes=data, original_sha=_sha256_bytes(data),
                core=core, manifest=manifest, vault=vault, today=today,
                quarantine_dir=quarantine_dir, duplicate_dir=duplicate_dir,
                processing_dir=processing_dir, report=report,
                depth=depth + 1, budget=budget, parent=parent_slug,
            )
        except Exception as exc:
            if _is_systemic_error(exc):
                # A batch-wide outage (signing key vanished, disk full) hits
                # every remaining nested item (and every remaining top-level
                # candidate) identically — bubble it up so run_ingest's own
                # systemic handling takes over, rather than quarantining a
                # perfectly good nested item as if it were poison.
                raise
            # A per-item defect. Unlike top-level candidates, a nested item
            # has no stable identity across separate `brain sync` runs to
            # retry against (it is re-derived from its parent archive every
            # time) — quarantine it immediately, never abort its siblings.
            reason = "nested_processing_error"
            if temp_path.exists():
                _quarantine(temp_path, quarantine_dir, reason, [f"{type(exc).__name__}: {exc}"])
            report["quarantined"].append({"file": name, "reason": reason, "parent": parent_slug})


# Leading-date filename styles a dropped document commonly carries. Anchored
# at the start and followed by a non-digit so partial numbers (audit codes
# like "2024_011", MMYYYY stamps like "042022") never misparse into a date.
_DOC_DATE_RES = (
    re.compile(r"^(\d{4})[-_. ](\d{1,2})[-_. ](\d{1,2})(?!\d)"),  # 2026-03-25
    re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)"),                   # 20260325
    re.compile(r"^(\d{2})(\d{2})(\d{2})(?!\d)"),                   # 260325 (YYMMDD)
    # Embedded full-ISO fallback (workspace naming style:
    # "_scenario_board_2026-06-15_v16.md"). Only the unambiguous hyphenated
    # ISO form is accepted mid-name — never the digit-run styles, which would
    # false-positive on version/id numbers.
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
    # Trailing YYYYMMDD before the extension ("b2c-gp-analysis-20260331.md")
    # — end-anchored so a digit run mid-name never matches; the calendar +
    # range checks below still reject non-dates.
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?=\.[A-Za-z0-9]+$|$)"),
)


def _derive_document_date(name: str, today: str) -> str | None:
    """Best-effort ``document_date`` from a leading date in the ORIGINAL
    filename. Without it, a bulk re-ingestion of old documents ranks as the
    freshest content in the vault (recency keys on capture date) and "latest"
    queries ground on months-old material. Conservative by design: anything
    ambiguous, non-calendar, pre-1990 or in the future returns None — an
    undated source is NEUTRAL in recency ranking, a misdated one poisons it."""
    for rx in _DOC_DATE_RES:
        # search(), not match(): the first three patterns are ^-anchored (so
        # search behaves identically), the embedded-ISO fallback is not.
        m = rx.search(name)
        if not m:
            continue
        y, mo, d = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        try:
            dd = _dt.date(y, mo, d)
        except ValueError:
            continue
        if y < 1990 or dd > _dt.date.fromisoformat(today):
            continue
        return dd.isoformat()
    return None


def _meta(slug: str, today: str, archive_path: Path, vault: Path, body_sha: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": slug,
        "type": "source",
        "classification": "Internal",  # ADR-0003: unlabelled -> MNPI; ingest
                                        # DECLARES Internal explicitly, matching
                                        # capture.py's own missing-classification
                                        # default (never left unlabelled).
        "captured": today,
        "origin": str(archive_path.relative_to(vault)),
        "sha256": body_sha,
        "immutable": True,
    }
    doc_date = _derive_document_date(archive_path.name, today)
    if doc_date and doc_date != today:
        meta["document_date"] = doc_date
    return meta


def _quarantine(claimed: Path, quarantine_dir: Path, reason: str, warnings: list[str]) -> None:
    dest_dir = quarantine_dir / reason
    # C3: _move is an os.rename, which SILENTLY REPLACES a same-named file —
    # a second same-named corrupt drop would clobber the first quarantined
    # original (the only copy). Uniquify the destination like every other
    # sink (_claim, _sweep_stale_processing) does.
    dest = _unique_dest(dest_dir, claimed.name)
    _move(claimed, dest)
    report_lines = [f"quarantine_reason: {reason}"] + [f"- {w}" for w in warnings]
    (dest_dir / f"{dest.name}.reason.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
