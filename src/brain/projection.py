"""Workspace projection — the REAL containment control (consensus hardening).

The classification filter (brain.classification) only governs what the
cooperative `brain` CLI *prints*. It is NOT containment: any file-capable
harness can read the Markdown in ``vault/`` directly and see every tier. The
plan's consensus hardening requires a real control. We choose option (a):

    Project a workspace that physically EXCLUDES Restricted/MNPI (and anything
    above the cap, and all unlabelled/default-deny notes) for untrusted harnesses.

An untrusted harness (e.g. a Cowork VM leg) is given access ONLY to the
projected directory, never the full vault. Because the sensitive files are not
present in the projection, a direct file read on that surface cannot surface
them — proven by tests/test_direct_file_read.py.

This composes with the host/VM trust split (substrate-spec §4): the host owns
the full vault and the writer role; the VM sees only a projection.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import classification as cls
from .notes import scan_vault

# Marker file written into every projection root. `project_workspace` will only
# `rmtree` an existing destination that carries this marker (or is empty), so a
# fat-fingered `--dest ~/important` can never be recursively deleted.
PROJECTION_MARKER = ".brain-projection"


def _assert_safe_dest(vault: Path, dest: Path) -> None:
    """Refuse a destination that would recursively delete something valuable.

    `project_workspace` wipes `dest` before writing. Guard against the caller
    pointing it at the vault, an ancestor of the vault, the home dir or an
    ancestor of it, or the filesystem root — and against clobbering any
    pre-existing directory we did not create (no PROJECTION_MARKER)."""
    v = vault.resolve()
    d = dest.resolve()
    home = Path.home().resolve()
    if d == Path(d.anchor):
        raise ValueError(f"refusing to project onto the filesystem root: {d}")
    for label, protected in (("vault", v), ("home directory", home)):
        if d == protected or d in protected.parents:
            raise ValueError(
                f"refusing to project onto {label} or its ancestor ({d}) — "
                "recreating --dest from scratch would delete it")
    if v == d or v in d.parents:
        raise ValueError(
            f"refusing to project INTO the vault or a subdirectory of it ({d})")
    if d.exists():
        if any(d.iterdir()) and not (d / PROJECTION_MARKER).is_file():
            raise ValueError(
                f"--dest {d} is a non-empty directory not created by `brain "
                f"project` (no {PROJECTION_MARKER}); refusing to delete it. "
                "Pass an empty or new directory, or a prior projection.")


@dataclass
class ProjectionResult:
    dest: Path
    max_tier: str
    copied: int
    excluded: int
    excluded_unlabelled: int

    def to_dict(self) -> dict:
        return {
            "dest": str(self.dest),
            "max_tier": self.max_tier,
            "copied": self.copied,
            "excluded": self.excluded,
            "excluded_unlabelled": self.excluded_unlabelled,
        }


def project_workspace(vault: Path, dest: Path, max_tier: str = cls.DEFAULT_MAX_TIER) -> ProjectionResult:
    """Materialise a filtered copy of ``vault`` into ``dest``.

    Only notes whose effective classification rank <= ``max_tier`` are copied.
    Unlabelled/unrecognised notes are default-denied (treated as MNPI) and
    excluded unless ``max_tier == "MNPI"``. ``dest`` is recreated from scratch
    each call so it can never retain a previously-projected sensitive file.
    """
    vault = Path(vault)
    dest = Path(dest)
    flt = cls.ClassificationFilter(max_tier=max_tier)

    _assert_safe_dest(vault, dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / PROJECTION_MARKER).write_text("brain projection\n", encoding="utf-8")

    copied = excluded = excluded_unlabelled = 0
    for note in scan_vault(vault):
        if flt.allows(note.classification):
            rel = note.path.relative_to(vault)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(note.path.read_text(encoding="utf-8"), encoding="utf-8")
            copied += 1
        else:
            excluded += 1
            if cls.is_default_denied(note.classification):
                excluded_unlabelled += 1

    return ProjectionResult(
        dest=dest, max_tier=max_tier, copied=copied,
        excluded=excluded, excluded_unlabelled=excluded_unlabelled,
    )
