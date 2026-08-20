"""Workspace sweep into the inbox (WSP-01)."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import os
import time


# ---------------------------------------------------------------------------
# Workspace sweep (WSP-01, 2026-07-11). A live working folder (e.g. an
# an Obsidian workspace folder) accumulates hundreds of session artifacts
# with no lifecycle. The sweep gives them one: a SETTLED file (mtime older
# than the age gate — nobody is editing it any more) is MOVED into the
# vault's `inbox/`, where the standard ingest drain archives the original
# immutably under `raw/originals/`, signs + writes the `raw/` note (with a
# filename-derived `document_date`), the next sync embeds it, and the
# monthly graphify wires it into the discovery graph. Content already
# ingested dedups by content hash (parked in the inbox duplicate dir), so
# the sweep is idempotent and never double-ingests.
#
# Scope rules (deliberately dumb): TOP-LEVEL FILES ONLY — subdirectories are
# other systems' machine state (skill packages, archives, trust runs) and are
# never touched; dotfiles are skipped. Actively-edited files have a fresh
# mtime and are skipped by the age gate. Configuration: the sweep only runs
# when dirs are configured ($BRAIN_WORKSPACE_SWEEP_DIRS, os.pathsep-separated;
# $BRAIN_WORKSPACE_SWEEP_AGE_DAYS, default 14) — no config, no sweep.
# ---------------------------------------------------------------------------
WORKSPACE_SWEEP_DIRS_ENV = "BRAIN_WORKSPACE_SWEEP_DIRS"
WORKSPACE_SWEEP_AGE_ENV = "BRAIN_WORKSPACE_SWEEP_AGE_DAYS"
WORKSPACE_SWEEP_DEFAULT_AGE_DAYS = 14


def workspace_sweep_config() -> tuple[list[tuple[Path, int | None]], int]:
    """Configured sweep sources + default age gate. Empty list = disabled.

    Each $BRAIN_WORKSPACE_SWEEP_DIRS entry is ``path`` or ``path=N`` — the
    per-dir age override (2026-07-11, round-5 benchmark): a CAPTURE folder
    (an inbox / a meetings drop folder) holds FINAL documents that
    settle in a day, while a WORKING folder needs the long gate so
    in-progress files are never swept. One global age starved the capture
    folders by a week; ``path=1`` fixes that per source."""

    raw = os.environ.get(WORKSPACE_SWEEP_DIRS_ENV, "").strip()
    dirs: list[tuple[Path, int | None]] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path_part, sep, age_part = entry.rpartition("=")
        if sep and age_part.isdigit():
            # age 0 is legal: same-day capture sweep (a 15-minute
            # write-settle guard still applies inside sweep_workspace).
            dirs.append((Path(path_part).expanduser(), int(age_part)))
        else:
            dirs.append((Path(entry).expanduser(), None))
    try:
        age = int(os.environ.get(WORKSPACE_SWEEP_AGE_ENV, ""))
    except ValueError:
        age = WORKSPACE_SWEEP_DEFAULT_AGE_DAYS
    if age < 1:
        age = WORKSPACE_SWEEP_DEFAULT_AGE_DAYS
    return dirs, age


def sweep_workspace(
    dirs: list[Path] | list[tuple[Path, int | None]], inbox: Path, age_days: int,
    now: float | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Move settled top-level files from ``dirs`` into ``inbox``.

    ``dirs`` entries are ``Path`` (use the global ``age_days``) or
    ``(Path, age)`` tuples (per-dir override; ``None`` = global). Pure file
    motion — classification/signing/dedup all happen downstream in the
    ingest drain. Collisions uniquify (never clobber an inbox file).
    Returns an honest report; a missing dir is reported, never raised."""

    from .ingest.pipeline import _move, _unique_dest

    from .ingest.handlers import handler_for

    base_now = now if now is not None else time.time()
    report: dict[str, Any] = {
        "swept": [], "skipped_active": 0, "skipped_unsupported": 0,
        "missing_dirs": [], "errors": [],
        "age_days": age_days, "dry_run": dry_run,
    }
    for entry in dirs:
        d, dir_age = entry if isinstance(entry, tuple) else (entry, None)
        eff_age = dir_age if dir_age is not None else age_days
        # age 0 = capture-inbox mode: sweep same-day, but never a file
        # younger than 15 minutes (write-settle guard against partial copies).
        cutoff = base_now - (900.0 if eff_age == 0 else eff_age * 86400.0)
        if not d.is_dir():
            report["missing_dirs"].append(str(d))
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            if handler_for(p) is None:
                # Machine artifacts (.py, .json, .tsv, …) have no ingest
                # handler — sweeping them only detours through quarantine
                # (measured: 344 on the first real sweep). Leave them where
                # they live; the count keeps the skip honest.
                report["skipped_unsupported"] += 1
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError as exc:
                report["errors"].append({"file": str(p), "error": str(exc)})
                continue
            if mtime > cutoff:
                report["skipped_active"] += 1
                continue
            if dry_run:
                report["swept"].append({"file": str(p), "would_move": True})
                continue
            try:
                inbox.mkdir(parents=True, exist_ok=True)
                _move(p, _unique_dest(inbox, p.name))
                report["swept"].append({"file": str(p)})
            except OSError as exc:
                report["errors"].append({"file": str(p), "error": str(exc)})
    return report

# Cross-section binds, deferred past this module's own defs.
