"""Path + location policy for the brain engine.

The derived SQLite index lives under a per-user **application-data** directory,
NOT under Documents/Desktop (Windows Controlled-Folder-Access protected paths) —
CORE-01 hardening. The index is derived and disposable; delete-and-rebuild from
`vault/` is always safe, so its exact location is policy, not truth.

Resolution order for the index directory:
  1. ``$BRAIN_INDEX_DIR``                  (explicit override; tests use this)
  2. Windows  : ``%LOCALAPPDATA%\\profile-a-brain``
  3. macOS    : ``~/Library/Application Support/profile-a-brain``
  4. Linux/*  : ``$XDG_DATA_HOME/profile-a-brain`` or ``~/.local/share/...``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "profile-a-brain"
INDEX_FILENAME = "index.sqlite"

# Host/VM trust split (S06). The HOST broker is the sole writer (signs the audit
# chain, mutates the index, publishes snapshots). The Cowork Linux VM is a
# READ + DRAFT surface only — it may never write notes, open the index in
# WAL/write mode, or resolve a signing key. Role is resolved from $BRAIN_ROLE,
# default "host". See AGENTS.md §6 + docs/cowork-windows-install.md.
ROLE_HOST = "host"
ROLE_VM = "vm"


def role(explicit: str | None = None) -> str:
    """Resolve the trust role: explicit arg > ``$BRAIN_ROLE`` > ``host``."""
    val = (explicit or os.environ.get("BRAIN_ROLE") or ROLE_HOST).strip().lower()
    return ROLE_VM if val == ROLE_VM else ROLE_HOST


def index_dir() -> Path:
    """Return the per-user app-data directory that holds the derived index.

    Never returns a Controlled-Folder-Access path (Documents/Desktop/Pictures).
    """
    override = os.environ.get("BRAIN_INDEX_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    # Linux / BSD / Cowork VM
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def index_path() -> Path:
    """Absolute path to the single SQLite index file."""
    return index_dir() / INDEX_FILENAME


def ensure_index_dir() -> Path:
    d = index_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# file permission policy (hardening pass)
# --------------------------------------------------------------------------
# The derived SQLite index and the published read-only snapshot can carry note
# bodies up to and including Secret-tier content (the classification gate is an
# egress *decision*, not containment -- see docs/operations/egress-provider-
# posture.md §2). Neither must ever be left world-readable. The snapshot was
# previously chmod'd 0o444 (read-only, but readable by every local account on a
# shared/multi-user machine); the index inherited whatever the process umask
# happened to be (often 0o644 on a typical single-user default). Both are now
# tightened to owner-only immediately after creation, regardless of umask.
SECURE_FILE_MODE = 0o600  # owner rw only; use 0o640 if a deployment intentionally
                           # shares index/snapshot files with a trusted local group


def secure_file_permissions(path: "os.PathLike[str] | str", mode: int = SECURE_FILE_MODE) -> None:
    """Best-effort tighten ``path`` to ``mode`` (default owner-only 0600).

    Never raises: a chmod call that fails (unsupported filesystem, Windows ACL
    semantics where POSIX mode bits are only partially honored, a race where the
    file vanished) must not break index/snapshot creation -- it degrades to
    "as restrictive as the platform default allowed", not a crash.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def vault_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the vault root: explicit arg > ``$BRAIN_VAULT`` > CWD/vault."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAIN_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "vault").resolve()


# --------------------------------------------------------------------------
# workspace runtime locations (S06 — Cowork-Windows workspace-install path)
# --------------------------------------------------------------------------
# The Cowork Linux VM mounts ONLY the workspace and sees ``vault/.brain/``. The
# runtime dir holds the per-arch ``brain`` binary, the bundled ``model.onnx``,
# the read-only published ``snapshot/`` the VM reads, and the writable
# ``capture-inbox/`` the VM drops drafts into. All four resolve from env first so
# a workspace install can point them at a workspace-root ``.brain/`` if desired;
# the default keeps everything under the gitignored ``vault/.brain/`` (spec §2),
# which ``notes.scan_vault`` already excludes from indexing.
def brain_runtime_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    override = os.environ.get("BRAIN_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    return vault_root(vault) / ".brain"


def snapshot_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Dir holding the read-only published snapshot (DB + manifest)."""
    override = os.environ.get("BRAIN_SNAPSHOT_DIR")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "snapshot"


def snapshot_db_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Absolute path to the read-only snapshot DB the VM ``brain`` reads."""
    from .snapshot import SNAPSHOT_DB

    return snapshot_dir(vault) / SNAPSHOT_DB


def capture_inbox_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Writable dir the VM drops capture drafts into (host drains it on invoke).

    Lives under ``.brain/`` so it is host-visible on the shared mount AND
    excluded from ``scan_vault`` — a draft is never auto-indexed; only the host
    promotes it (sign + index) via drain-on-invoke.
    """
    override = os.environ.get("BRAIN_CAPTURE_INBOX")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "capture-inbox"
