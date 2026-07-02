"""Encrypted off-device backup + restore (SEC-03).

The off-device backup is the ONE place encryption genuinely matters (design v5
§3.3): the at-rest baseline stays FDE-only, but a copy that leaves the machine
loses FDE's protection, so the backup is encrypted regardless of the dormant
at-rest flag (brain.encryption.is_enabled()).

What is backed up: the **Markdown truth** under ``vault/`` (the second brain).
The derived ``.brain/`` index is deliberately EXCLUDED — it is disposable and
rebuildable from Markdown, so backing it up wastes space and risks shipping a
stale/locked SQLite WAL off-device. ``.git/`` is excluded too.

Format: a gzip tar of the vault, then AES-256-GCM-encrypted into one
``<name>.tar.gz.enc`` token (brain.encryption.encrypt_bytes(force=True)), plus a
sidecar ``<name>.manifest.json`` recording counts + the sha256 of the plaintext
archive so a restore can prove byte-identity. Fails closed: if no encryption key
resolves, the backup refuses (never writes a plaintext copy off-device).
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import encryption as enc

EXCLUDE_DIRS = {".brain", ".git", "__pycache__", ".pytest_cache"}


@dataclass
class BackupManifest:
    schema_version: str
    created_iso: str
    source_vault: str
    archive: str
    encrypted: bool
    files: int
    plaintext_sha256: str
    plaintext_bytes: int
    ciphertext_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _included(path: Path, vault: Path) -> bool:
    rel = path.relative_to(vault)
    return not any(part in EXCLUDE_DIRS for part in rel.parts)


def _build_tar_gz(vault: Path) -> tuple[bytes, int]:
    """Deterministic-ish gzip tar of vault Markdown truth. Returns (bytes, file_count)."""
    buf = io.BytesIO()
    count = 0
    # mtime fixed so the gzip header is stable across runs of the same content.
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(vault.rglob("*")):
            if p.is_file() and _included(p, vault):
                tar.add(p, arcname=str(p.relative_to(vault)))
                count += 1
    return buf.getvalue(), count


def create_backup(vault: Path, dest_dir: Path, *, name: str | None = None,
                  encrypt: bool = True) -> BackupManifest:
    """Create an encrypted off-device backup of ``vault`` into ``dest_dir``.

    ``encrypt=True`` (default) forces AES-256-GCM encryption (fails closed with
    no key). ``encrypt=False`` is for the rare in-VM / already-encrypted-target
    case and writes a plaintext ``.tar.gz`` with a loud manifest flag — discouraged
    for anything leaving the machine.
    """
    vault = Path(vault)
    dest_dir = Path(dest_dir)
    if not vault.is_dir():
        raise FileNotFoundError(f"vault not found: {vault}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    plaintext, file_count = _build_tar_gz(vault)
    pt_sha = hashlib.sha256(plaintext).hexdigest()
    stamp = name or f"brain-backup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"

    if encrypt:
        token = enc.encrypt_bytes(plaintext, force=True)  # force: off-device => encrypt
        archive = dest_dir / f"{stamp}.tar.gz.enc"
        archive.write_bytes(token)
        ct_bytes = len(token)
    else:
        archive = dest_dir / f"{stamp}.tar.gz"
        archive.write_bytes(plaintext)
        ct_bytes = len(plaintext)

    manifest = BackupManifest(
        schema_version="brain-backup-v1",
        created_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        source_vault=str(vault.resolve()),
        archive=str(archive),
        encrypted=encrypt,
        files=file_count,
        plaintext_sha256=pt_sha,
        plaintext_bytes=len(plaintext),
        ciphertext_bytes=ct_bytes,
    )
    (dest_dir / f"{stamp}.manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
    return manifest


def restore_backup(archive: Path, dest_dir: Path) -> dict[str, Any]:
    """Restore a backup archive into ``dest_dir`` (decrypting if needed).

    Returns a verdict with the restored file count and the plaintext sha256 (so a
    caller can assert byte-identity against the backup manifest). Authenticated
    decryption — a tampered ``.enc`` raises rather than restoring garbage.
    """
    archive = Path(archive)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    blob = archive.read_bytes()

    if archive.suffix == ".enc" or blob.startswith(enc.MAGIC):
        plaintext = enc.decrypt_bytes(blob)
        encrypted = True
    else:
        plaintext, encrypted = blob, False

    restored = 0
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
        for member in tar.getmembers():
            # Guard against path traversal in archive member names.
            target = (dest_dir / member.name).resolve()
            if dest_dir.resolve() not in target.parents and target != dest_dir.resolve():
                raise ValueError(f"archive member escapes dest: {member.name}")
        tar.extractall(dest_dir)  # noqa: S202 - members validated above
        restored = sum(1 for m in tar.getmembers() if m.isfile())

    return {
        "restored": True,
        "files": restored,
        "encrypted": encrypted,
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
        "dest": str(dest_dir),
    }
