"""Pre-flight safety and staging for `brain init --import-from` (ONB-01)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import config


# --------------------------------------------------------------------------
# ONB-01: brain init --full --import-from <dir> -- guided first ingest
# --------------------------------------------------------------------------
# Stages an external folder (e.g. an existing Obsidian vault) into
# vault/inbox/ and drives the STANDARD host ingest drain
# (brain.ingest.pipeline.run_ingest via BrainCore.ingest_dropzone) -- reuses
# the existing pipeline verbatim, never forks it. Host-only: refused
# (role_forbidden) before any filesystem side effect -- ingest_dropzone would
# refuse a VM leg anyway (BrainCore._require_host), but the check here runs
# BEFORE even the read-only dry-run scan, so a VM leg never touches the
# import folder at all.
#
# [HARDENED:codex] import safety: realpath-resolved overlap check in BOTH
# directions, symlinks never followed, a dry-run manifest gate (file count +
# bytes + per-extension breakdown) that requires explicit confirmation before
# anything is staged, and a default file-count/byte-size cap.
DEFAULT_IMPORT_FILE_CAP = 5000
DEFAULT_IMPORT_BYTES_CAP = 500 * 1024 * 1024


class ImportSafetyError(ValueError):
    """``--import-from`` failed a pre-flight safety check; nothing was staged."""


def _realpath(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(str(Path(p).expanduser())))


def validate_import_overlap(
    import_dir: str | os.PathLike[str], vault: str | os.PathLike[str] | None,
) -> None:
    """Reject either direction of overlap between ``import_dir`` and the
    resolved vault root.

    - ``import_dir`` inside (or equal to) the vault: would re-ingest the
      vault's own content (including its own ``inbox/``).
    - the vault inside ``import_dir``: the self-copy bomb -- the moment
      staging starts writing into ``vault/inbox/``, that new content becomes
      part of the very traversal source being walked.
    """
    imp = _realpath(import_dir)
    vlt = _realpath(config.vault_root(vault, allow_missing=True))
    if not imp.is_dir():
        raise ImportSafetyError(f"--import-from {imp} is not a directory")
    try:
        imp.relative_to(vlt)
    except ValueError:
        pass
    else:
        raise ImportSafetyError(
            f"--import-from {imp} is inside (or equal to) the vault {vlt}; refusing")
    try:
        vlt.relative_to(imp)
    except ValueError:
        pass
    else:
        raise ImportSafetyError(
            f"the vault {vlt} is inside --import-from {imp}; refusing -- this "
            "is the self-copy bomb (vault/inbox/ would become part of the "
            "traversal source once staging starts writing into it)")


def scan_import_dir(import_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only walk of ``import_dir``: never follows a symlinked file or
    directory (HARDENED:codex). Returns a dry-run manifest -- file count,
    total bytes, per-extension breakdown -- plus the internal file list
    ``stage_import_files`` consumes to actually copy."""
    imp = Path(import_dir)
    files: list[tuple[Path, int]] = []
    total_bytes = 0
    by_extension: dict[str, int] = {}
    for root, dirnames, filenames in os.walk(imp, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [d for d in dirnames if not (root_path / d).is_symlink()]
        for name in filenames:
            fp = root_path / name
            if fp.is_symlink():
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            rel = fp.relative_to(imp)
            files.append((rel, size))
            total_bytes += size
            ext = fp.suffix.lower() or "(none)"
            by_extension[ext] = by_extension.get(ext, 0) + 1
    return {
        "import_dir": str(imp), "file_count": len(files), "total_bytes": total_bytes,
        "by_extension": by_extension, "_files": files,
    }


def check_import_caps(
    manifest: dict[str, Any], *, force: bool,
    file_cap: int | None = None, bytes_cap: int | None = None,
) -> None:
    # Resolved from the PARENT module namespace AT CALL TIME (not as bound
    # default values) so a caller (or a test) can monkeypatch
    # brain.init.DEFAULT_IMPORT_FILE_CAP/DEFAULT_IMPORT_BYTES_CAP and have it
    # take effect — the caps are defined here but the facade contract (tests
    # patch the parent) is what decides, same as before the size-ratchet split.
    if file_cap is None:
        file_cap = _init.DEFAULT_IMPORT_FILE_CAP
    if bytes_cap is None:
        bytes_cap = _init.DEFAULT_IMPORT_BYTES_CAP
    if force:
        return
    if manifest["file_count"] > file_cap:
        raise ImportSafetyError(
            f"{manifest['file_count']} files exceeds the default cap ({file_cap}); "
            "pass --import-force to override")
    if manifest["total_bytes"] > bytes_cap:
        raise ImportSafetyError(
            f"{manifest['total_bytes']} bytes exceeds the default cap ({bytes_cap}); "
            "pass --import-force to override")


def build_import_dry_run(
    import_from: str | os.PathLike[str], vault: str | os.PathLike[str] | None,
    *, force: bool = False,
) -> dict[str, Any]:
    """Pre-flight: overlap + symlink-safe scan + cap check. Pure read-only
    filesystem inspection -- never stages or ingests anything."""
    validate_import_overlap(import_from, vault)
    manifest = scan_import_dir(import_from)
    check_import_caps(manifest, force=force)
    return manifest


def _flatten_relpath(rel: Path) -> str:
    """The ingest drain only scans the inbox ROOT (never recurses), so a
    nested import (e.g. an Obsidian vault's subfolders) is flattened into one
    filename per file -- joined with '__' so the original path stays visible
    and collisions across sibling subfolders are vanishingly unlikely (the
    dest-uniquification below is the actual guarantee)."""
    return "__".join(rel.parts) if len(rel.parts) > 1 else rel.parts[0]


def _unique_inbox_dest(inbox: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = inbox / name
    i = 0
    while dest.exists():
        i += 1
        dest = inbox / f"{stem}.{i}{suffix}"
    return dest


def stage_import_files(
    manifest: dict[str, Any], import_from: str | os.PathLike[str],
    vault: str | os.PathLike[str] | None,
) -> list[str]:
    """Copy (never move) every file the dry-run manifest found into
    ``vault/inbox/``. The user's original folder is never touched."""
    v = config.vault_root(vault, allow_missing=True)
    imp = Path(import_from)
    inbox = v / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for rel, _size in manifest["_files"]:
        src = imp / rel
        if src.is_symlink():
            continue
        dest = _unique_inbox_dest(inbox, _flatten_relpath(rel))
        shutil.copy2(src, dest)
        staged.append(dest.name)
    return staged


def stage_and_ingest_import(
    import_from: str | os.PathLike[str], vault: str | os.PathLike[str] | None,
    role: str, *, force: bool = False,
) -> dict[str, Any]:
    """Stage ``import_from`` into ``vault/inbox/`` then run the STANDARD host
    ingest drain (``BrainCore.ingest_dropzone`` -> ``ingest.pipeline.run_ingest``).

    Refused on ``role != host`` BEFORE any filesystem side effect --
    ``ingest_dropzone`` would refuse a VM leg on its own
    (``BrainCore._require_host``), but that only fires after staging already
    copied bytes into ``inbox/``; this check runs first so a VM leg never
    touches the import folder or the vault at all (same fail-closed shape as
    every other host-broker verb).
    """
    if role != config.ROLE_HOST:
        raise PermissionError(
            f"role={role!r} may not import + ingest a folder; this is a "
            "host-broker privilege (the VM leg is read + draft only). "
            "Run on the host.")
    manifest = build_import_dry_run(import_from, vault, force=force)
    staged = stage_import_files(manifest, import_from, vault)

    from .core import BrainCore

    core = BrainCore(vault=vault, role=role)
    ingest_report = core.ingest_dropzone()
    return {
        "import_dir": manifest["import_dir"], "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"], "by_extension": manifest["by_extension"],
        "staged": len(staged), "ingest": ingest_report,
    }


def render_import_dry_run(manifest: dict[str, Any]) -> str:
    lines = [
        f"import dry-run: {manifest['import_dir']}",
        f"  {manifest['file_count']} file(s), {manifest['total_bytes']} bytes total",
    ]
    for ext, n in sorted(manifest["by_extension"].items()):
        lines.append(f"    {ext}: {n}")
    lines.append("re-run with --yes to stage into vault/inbox/ and run the ingest drain")
    return "\n".join(lines)

# Parent-namespace bind, deferred past this module's own defs.
from . import init as _init  # noqa: E402

