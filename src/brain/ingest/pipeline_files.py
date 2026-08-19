"""Implement ingest filesystem transitions."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


def manifest_path(vault: Path) -> Path:
    from .. import config
    from . import pipeline as facade

    return config.brain_runtime_dir(vault) / facade.MANIFEST_RELPATH[0]


def load_manifest(vault: Path) -> dict[str, str]:
    path = manifest_path(vault)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(vault: Path, manifest: dict[str, str]) -> None:
    path = manifest_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def failures_path(vault: Path) -> Path:
    from .. import config
    from . import pipeline as facade

    return config.brain_runtime_dir(vault) / facade.FAILURES_RELPATH[0]


def load_failures(vault: Path) -> dict[str, int]:
    path = failures_path(vault)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_failures(vault: Path, failures: dict[str, int]) -> None:
    path = failures_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(failures, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_key(path: Path) -> str:
    """Return the stable content key for retry accounting across renames."""
    return sha256_bytes(path.read_bytes())


_SLUG_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


def slugify_stem(stem: str) -> str:
    cleaned = _SLUG_SANITIZE.sub("-", stem).strip("-").lower()
    return cleaned or "file"


_ARCHIVE_NAME_SANITIZE = re.compile(r'[\x00-\x1f\x7f:"\\]')


def sanitize_archive_name(name: str) -> str:
    """Return a filename safe for immutable storage and YAML provenance."""
    cleaned = _ARCHIVE_NAME_SANITIZE.sub("_", name)
    return cleaned or "file"


def move_path(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError:
        shutil.move(str(src), str(dest))


def claim_path(path: Path, processing_dir: Path) -> Path | None:
    """Atomically claim a drop, returning ``None`` when another worker won."""
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
    try:
        os.utime(dest, None)
    except OSError:
        pass
    return dest


def unique_destination(target_dir: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = target_dir / name
    i = 0
    while dest.exists():
        i += 1
        dest = target_dir / f"{stem}.{i}{suffix}"
    return dest


def sweep_stale_processing(
    processing_dir: Path,
    inbox: Path,
    *,
    vault: Path,
    quarantine_dir: Path,
    failures: dict[str, int],
) -> None:
    """Return abandoned claims to the inbox or quarantine repeated crashes."""
    from . import pipeline as facade

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
        if age < facade.STALE_PROCESSING_SECONDS:
            continue
        try:
            key = content_key(stuck)
        except OSError:
            key = stuck.name
        count = failures.get(key, 0) + 1
        failures[key] = count
        touched = True
        if count >= facade.MAX_INGEST_FAILURES:
            quarantine_claim(
                stuck,
                quarantine_dir,
                "repeated_ingest_failure",
                [
                    f"swept from stale {facade.PROCESSING_DIRNAME}/ {count} time(s) — "
                    "process likely died mid-extraction (crash-death is "
                    "indistinguishable from poison after N attempts); giving up"
                ],
            )
            failures.pop(key, None)
        else:
            move_path(stuck, unique_destination(inbox, stuck.name))
    if touched:
        save_failures(vault, failures)


def create_exclusive_or_collision(
    dest: Path,
    data: bytes,
    known_sha: str | None = None,
) -> str:
    """Create ``dest`` without replacement, reporting idempotency or collision."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = dest.read_bytes()
        if existing == data:
            return "idempotent"
        data_sha = known_sha if known_sha is not None else sha256_bytes(data)
        return "idempotent" if sha256_bytes(existing) == data_sha else "collision"
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return "written"


def scratch_directory(vault: Path) -> Path:
    """Return a scratch directory proven outside every VM-visible root."""
    from .. import config

    try:
        return config.proven_off_mount(Path(tempfile.gettempdir()), vault,
                                       what="ingest scratch dir")
    except config.HostPathUnsafe:
        directory = config.proven_off_mount(
            config.host_private_base() / "ingest-scratch",
            vault,
            what="ingest scratch dir (fallback)",
        )
        directory.mkdir(parents=True, exist_ok=True)
        config.secure_file_permissions(directory, 0o700)
        return directory


def extract_verified_buffer(
    handler: Any,
    data: bytes,
    suffix: str,
    vault: Path,
) -> Any:
    """Run a handler over a host-private copy of the verified bytes."""
    fd, tmp = tempfile.mkstemp(
        suffix=suffix or "",
        prefix="brain-ingest-",
        dir=scratch_directory(vault),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return handler.extract(Path(tmp))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def existing_note_classification(vault: Path, existing_id: str) -> str | None:
    """Return the tier of an already-ingested raw note for safe reporting."""
    note_path = vault / "raw" / f"{existing_id}.md"
    if not note_path.is_file():
        return None
    from .. import frontmatter as fm

    try:
        meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    value = meta.get("classification")
    return str(value) if value else None


def set_aside_operational(claimed: Path, inbox: Path, declared: str) -> None:
    """Consume an operational artifact without admitting it as knowledge."""
    from . import pipeline as facade

    dest_dir = inbox / facade.OPERATIONAL_DIRNAME / declared
    dest = unique_destination(dest_dir, claimed.name)
    move_path(claimed, dest)
    (dest_dir / f"{dest.name}.reason.txt").write_text(
        f"skipped_reason: operational_artifact:{declared}\n"
        "- this document declares its own operational type; it is the vault's\n"
        "  output, not knowledge about the business\n"
        "- set BRAIN_INGEST_ALLOW_OPERATIONAL=1 and re-drop it to override\n",
        encoding="utf-8",
    )


def quarantine_claim(
    claimed: Path,
    quarantine_dir: Path,
    reason: str,
    warnings: list[str],
) -> None:
    """Move a failed claim into a collision-safe quarantine sink."""
    dest_dir = quarantine_dir / reason
    dest = unique_destination(dest_dir, claimed.name)
    move_path(claimed, dest)
    report_lines = [f"quarantine_reason: {reason}"] + [f"- {w}" for w in warnings]
    (dest_dir / f"{dest.name}.reason.txt").write_text(
        "\n".join(report_lines) + "\n",
        encoding="utf-8",
    )
