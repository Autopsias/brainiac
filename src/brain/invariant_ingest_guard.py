"""ENF-04 guard verdicts, read back off ``raw/`` (corpus invariant 5).

The registry stays in :mod:`brain.invariants`; this vertical slice owns the
frontmatter walk — the same split, and the same reason, as
``invariant_unsigned_notes``.

It also EXPOSES the population behind its own ``value`` (the ``unguarded``
key), so ``remediation_branches`` can drive the FIX-02 repair branch off this
definition instead of restating the walk, the frontmatter key, the status and
the pre-2026-08-17 back-compat rule. A hand-copied population is how two
surfaces end up silently disagreeing about their own number, which is exactly
what AGENTS.md legislates one shared definition against.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .invariant_shared import SAMPLE_CAP

def ingest_guard(vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """ENF-04 — the ingest guard's verdicts, read back off ``raw/``.

    ``value`` is the count of sources admitted while the guard was UNAVAILABLE
    (the only non-monotone, should-be-zero number here). Every other status is
    reported beside it, per leg for raises, so "0 raised" can never quietly
    mean "the guard never looked"."""
    from . import frontmatter as fm
    from .ingest.tierguard import (
        _NO_CORPUS_ERROR, GUARD_KEY, GUARD_LEG_KEY, GUARD_REASON_KEY,
        GUARD_STATUSES, NO_CORPUS, UNAVAILABLE,
    )

    by_status = {s: 0 for s in GUARD_STATUSES}
    by_leg: dict[str, int] = {}
    unstamped = 0
    unknown = 0
    sample: list[str] = []
    #: The POPULATION behind ``value`` — the vault-relative path of every
    #: source this walk counted as unguarded. Exposed rather than left implicit
    #: so ``remediation_folds`` can drive the repair branch off the SAME
    #: definition instead of restating the walk, the key, the status and the
    #: pre-2026-08-17 back-compat rule (adversarial review 2026-08-21: a
    #: hand-copied population is how two surfaces silently disagree about
    #: their own number).
    unguarded: list[str] = []
    raw = Path(vault) / "raw"
    total = 0
    for path in sorted(raw.glob("*.md")):
        total += 1
        try:
            meta, _body = fm.parse_text(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = str(meta.get(GUARD_KEY) or "").strip()
        if not status:
            # Ingested before ENF-04 shipped. Reported with its own number so
            # the denominator is honest — never folded into `clear`, which
            # would claim a check that never happened.
            unstamped += 1
            continue
        if status not in by_status:
            unknown += 1
            continue
        # Sources ingested before the 2026-08-17 split carry `unavailable`
        # even when the guard RAN against an empty corpus — its own reason
        # line says so, and `raw/` is immutable so the stamp cannot be
        # corrected in place. Read the recorded evidence rather than leaving a
        # permanent false alarm on every vault whose first document predates
        # the split.
        if status == "unavailable" and _NO_CORPUS_ERROR in str(
                meta.get(GUARD_REASON_KEY) or ""):
            status = NO_CORPUS
        by_status[status] += 1
        if status == UNAVAILABLE:
            unguarded.append(f"raw/{path.name}")
        if status == "raised":
            leg = str(meta.get(GUARD_LEG_KEY) or "unrecorded")
            by_leg[leg] = by_leg.get(leg, 0) + 1
            if len(sample) < cap:
                sample.append(f"{path.stem} -> {meta.get('classification')} ({leg})")
    return {
        "value": by_status[UNAVAILABLE],
        "unguarded": unguarded,
        "sources": total,
        "raised": by_status["raised"],
        "raised_by_leg": by_leg,
        "clear": by_status["clear"],
        "subfloor": by_status["subfloor"],
        # Reported beside `value`, never inside it: the guard looked and the
        # corpus held nothing comparable, so there was no higher-tier twin to
        # leak from. Never hidden — a leg that stops working must still show.
        "no_corpus": by_status[NO_CORPUS],
        "unstamped": unstamped,
        "unknown_status": unknown,
        "sample": sample,
    }
