"""EXC-01 — retire a staged cross-tier pair whose two notes now share a tier.

Split out of :mod:`brain.remediation_exceptions` to keep that module inside its
file-size bound; re-exported there, so ``remediation_exceptions.retire_converged``
and ``CONVERGED_STATE`` stay the names every caller and test uses.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from . import inbox as _inbox
from .remediation_answers import unanswerable_pair, unraisable
from .remediation_exceptions_store import read_pending, retire as _retire

#: A pending pair whose two notes now carry ONE tier. Not ANSWERED, so
#: `decided_pair_keys` never counts it and a pair that splits again re-stages.
CONVERGED_STATE = "converged"


def retire_converged(core: Any, today: datetime.date) -> list[str]:
    """Retire every pending pair whose two notes NOW carry the same tier.

    ``low_tier``/``high_tier`` are stored at STAGING. When the lower copy is
    raised by any other route — a bulk raise, the ingest tier guard, a hand
    edit — the exposure is closed, but ``retire_unanswerable`` reads the STORED
    tiers, so the pair stayed pending and rotated back into the owner's queue as
    a question about an exposure that no longer exists (measured 2026-09-15:
    329 of 331 pending pairs already same-tier on disk).

    Tiers are read from the index — the same source the detectors stage from,
    so this can never disagree with what would re-stage the pair. An
    unreadable row or an unraisable tier retires nothing (the unlabelled case
    belongs to ``retire_unanswerable``). Its open question closes through the
    expiry path, exactly as there."""
    vault = Path(core.vault)
    done: list[str] = []
    closing: set[str] = set()
    for meta in read_pending(vault):
        tiers = []
        for side in ("low_id", "high_id"):
            try:
                row = core.index.get(str(meta.get(side))) or {}
            except Exception:  # noqa: BLE001 — an unreadable index retires nothing
                row = {}
            tiers.append(row.get("classification"))
        # Through the lane's one tier rule, never `classification` directly:
        # a missing label is not a tier two notes can "share".
        if any(t is None or unraisable(t) for t in tiers):
            continue
        why = unanswerable_pair(tiers[0], tiers[1])
        if not why:
            continue                        # still a real cross-tier exposure
        if _retire(vault, meta, CONVERGED_STATE, at=today.isoformat(),
                   reason=f"current tiers {tiers[0]} and {tiers[1]}: {why}"):
            closing.add(str(meta["question_key"]))
            done.append(str(meta["id"]))
    if closing:
        entries, closed = _inbox.expire_questions(
            core._read_inbox(), closing, expired=today.isoformat())
        if closed:
            core._write_inbox(entries)
    return done
