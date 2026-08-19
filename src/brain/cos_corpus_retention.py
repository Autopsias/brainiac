"""Corpus retention: automatic whole-run pruning and the status summary."""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from . import config, cos

def _keep_days(days: Any) -> int:
    """A retention window, or a refusal. A window below 1 puts the cutoff in
    the future and deletes runs that have not expired — a knob held the wrong
    way must not be a delete-everything button."""
    try:
        keep = int(days)
    except (TypeError, ValueError):
        raise CorpusRefused(f"retention window must be a whole number of "
                            f"days, not {days!r}") from None
    if not 1 <= keep <= 36500:
        raise CorpusRefused(
            f"retention window must be between 1 and 36500 days, not {keep}. "
            f"A window below 1 puts the cutoff in the future and deletes runs "
            f"that have not expired.")
    return keep


def retention_days() -> int:
    """The configured window. An unset variable takes the default; a set but
    unusable one REFUSES rather than clamping — ``BRAIN_COS_CORPUS_DAYS=0``
    reads like "off" and would otherwise silently become "keep one day"."""
    raw = os.environ.get(RETENTION_DAYS_ENV)
    return DEFAULT_RETENTION_DAYS if raw is None else _keep_days(raw)


def _run_date(run_id: str) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(run_id[:10])
    except ValueError:
        return None


def prune(vault, *, now: _dt.datetime | None = None,
          days: int | None = None) -> dict[str, Any]:
    """Delete WHOLE expired corpus files. Never a row inside one.

    A partially pruned corpus would silently change a replay's denominator —
    the same shape as a run reporting reads it never performed — so the unit of
    deletion is the file and there is no per-row path at all.

    Age comes from the run id in the FILENAME, not from mtime: the run id is
    host-assigned at launch and cannot drift, while an mtime is rewritten by
    any tool that touches the file. A name that is not a run id is left alone
    and REPORTED, never deleted on a guess.

    An UNCLOSED corpus is held, never deleted: unlinking a file a writer still
    holds open leaves that writer appending to a detached inode, so the bytes
    vanish at close and the corpus silently lost rows.

    THE CALLER IS THE NIGHTLY (``BrainCore.maintain``'s daily retention block,
    beside the duplicate and query-log prunes), so "for how long" is enforced by
    the schedule rather than by an operator remembering. It stays callable by
    hand — the fold adds a caller, not a gate. **The nightly passes no ``now=``
    on purpose**: this is a destructive window, and taking the cutoff from
    ``brain maintain --date`` made exercising the date gate with a future date
    delete unexpired mail bodies.

    ``errors`` IS THE DIFFERENCE BETWEEN "nothing expired" AND "the delete
    failed". An unreadable directory or a refused unlink used to return the
    same success-shaped result as a clean scan, so the caller stamped
    "retention ran here" over expired MNPI bodies still on disk. Failures are
    reported separately from ``unrecognized`` (a name this fold does not
    understand, which is not damage) so the caller can withhold that claim.

    ponytail: plain unlink by pathname, no inode re-check. Racing it needs
    local code execution, which could read the plaintext file anyway.
    """
    keep = _keep_days(days if days is not None else retention_days())
    today = (now or cos.utcnow()).date()
    cutoff = today - _dt.timedelta(days=keep)
    out: dict[str, Any] = {"retention_days": keep, "cutoff": cutoff.isoformat(),
                           "pruned": [], "kept": 0, "unrecognized": [],
                           "held": [], "errors": []}
    try:
        entries = sorted(corpus_root(vault).iterdir())
    except FileNotFoundError:
        return out  # no corpus directory: nothing is at rest here to expire
    except (OSError, config.HostPathUnsafe) as exc:
        out["errors"].append(
            f"the corpus directory could not be scanned: "
            f"{type(exc).__name__}: {exc}")
        return out
    for p in entries:
        if not p.is_file():
            continue
        rid = p.name[:-len(_SUFFIX)] if p.name.endswith(_SUFFIX) else ""
        day = _run_date(rid) if cos.RUN_ID_RE.match(rid) else None
        if day is None:
            out["unrecognized"].append(p.name)
            continue
        if day < cutoff:
            if not is_closed(vault, rid):
                out["held"].append(f"{rid}: never closed")
                continue
            try:
                p.unlink()
            except OSError as exc:
                out["errors"].append(
                    f"{p.name}: {type(exc).__name__}: {exc}")
                continue
            out["pruned"].append(rid)
        else:
            out["kept"] += 1
    return out


def last_scheduled_prune(vault=None) -> str | None:
    """The date the nightly fold last pruned this host's corpora, or ``None``.

    Read from ``maintain-state.json``, not inferred from the code being
    present: on a host where the nightly has never run, retention is not in
    force no matter what this engine ships, and status must say so.
    """
    try:
        state = json.loads(
            config.maintain_state_path(vault).read_text(encoding="utf-8"))
        marker = state.get(PRUNE_MARKER)
        return str(marker["last_run"]) if isinstance(marker, dict) else None
    except Exception:  # noqa: BLE001 — absent/unreadable state means "never"
        return None


def corpus_summary(vault=None) -> dict[str, Any]:
    """What ``brain status`` reports about the corpus on this host.

    Unfiltered MNPI mail bodies are the one thing under the index dir nothing
    else reports — an operator repointing ``$BRAIN_INDEX_DIR`` or uninstalling
    has to be able to see how much is on disk, how old the oldest night is, and
    whether anything is actually deleting it here.
    """
    pruned_on = last_scheduled_prune(vault)
    out: dict[str, Any] = {"runs": 0, "bytes": 0, "oldest_run": None,
                           "oldest_days": None, "unclosed": 0,
                           "pruned_by_a_scheduled_fold": pruned_on is not None,
                           "last_scheduled_prune": pruned_on,
                           "retention_days": DEFAULT_RETENTION_DAYS}
    try:
        out["retention_days"] = retention_days()
        root = corpus_root(vault)
        runs = list_runs(vault)
        out["runs"] = len(runs)
        out["bytes"] = sum((root / f"{r}{_SUFFIX}").stat().st_size
                           for r in runs)
        out["oldest_run"] = runs[0] if runs else None
        if runs:
            day = _run_date(runs[0])
            if day is not None:
                out["oldest_days"] = (cos.utcnow().date() - day).days
        out["unclosed"] = sum(1 for r in runs if not is_closed(vault, r))
    except Exception as exc:  # noqa: BLE001 — status must never crash on this
        out["error"] = str(exc)
    return out

# Parent-namespace binds, deferred past this module's own defs.
from .cos_corpus import (  # noqa: E402
    DEFAULT_RETENTION_DAYS as DEFAULT_RETENTION_DAYS,
    PRUNE_MARKER as PRUNE_MARKER,
    RETENTION_DAYS_ENV as RETENTION_DAYS_ENV,
    CorpusRefused as CorpusRefused,
    _SUFFIX as _SUFFIX,
    corpus_root as corpus_root,
    is_closed as is_closed,
    list_runs as list_runs,
)
