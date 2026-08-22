"""``brain update``'s update-state record + FIX-04 bounded retry.

Split out of :mod:`brain.update` purely to keep that file under the file-size
ratchet — no behaviour change. Every name here is re-exported from
``brain.update`` (see that module), so every existing
``brain.update.<name>`` caller and monkeypatch target is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .doctor import _compare


def update_state_path(brainiac_home: Path) -> Path:
    return brainiac_home / "update-state.json"


def read_update_state(brainiac_home: Path) -> Optional[dict]:
    try:
        import json
        return json.loads(update_state_path(brainiac_home).read_text(encoding="utf-8"))
    except Exception:
        return None


def failure_is_moot(state: Optional[dict], installed: Optional[str]) -> bool:
    """True when a recorded FAILED update targeted a version the machine has
    SINCE REACHED — the record describes a past state and must stop nagging.

    Nothing else clears a `failed` record: the availability check writes one
    only when an update IS available, so once the machine catches up (manually,
    or by a later version applying cleanly) the stale banner ran on in every
    session until the hook's 7-day freshness window expired. Judged on the
    DETERMINED comparison installed >= the version that failed — never on
    `available: False`, which also means "could not check".
    """
    target = (state or {}).get("latest")
    if not target or not installed:
        return False
    try:
        return _compare(str(installed), str(target)) >= 0
    except Exception:
        return False


def write_update_state(
    brainiac_home: Path, *, status: str, installed: Optional[str] = None,
    latest: Optional[str] = None, source: Optional[str] = None,
    at: Optional[str] = None, detail: str = "",
    attempts: int = 0, attempted_on: Optional[str] = None,
    escalated: bool = False,
) -> Path:
    """Persist the auto-update record the session-start hook reads (file-only,
    no engine call). ``status`` ∈ {available, applied, failed}.

    FIX-04 fields (``attempts``/``attempted_on``/``escalated``): the whole
    record is REWRITTEN on every call (never merged), so every caller passes
    its own retry bookkeeping explicitly rather than relying on a prior
    write's values surviving — a caller that forgot would silently reset the
    counter to 0 every hour."""
    import json
    p = update_state_path(brainiac_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "status": status, "installed": installed, "latest": latest,
        "source": source, "at": at, "detail": detail,
        "attempts": attempts, "attempted_on": attempted_on,
        "escalated": escalated,
    }), encoding="utf-8")
    return p


#: FIX-04 — bounded update retry. Matches the registry's ``escalate_after``
#: for ``update:available``/``update:failed`` (both -> ``update_retry``, 3) by
#: construction: "exhausted" and "would escalate the branch" are one number.
UPDATE_RETRY_ESCALATE_AFTER = 3


def retry_decision(previous: Optional[dict], today: Any) -> str:
    """``"retry" | "wait" | "escalate"`` for a SAME-version update that
    previously failed.

    Host-global, not per-vault (HARDENED:adv-2026-08-20 — update is one
    engine install shared by every vault on this host, so its retry state is
    ``update-state.json`` itself, never a per-vault row). At most one retry
    ATTEMPT per calendar day (``attempted_on``) — the hourly maintain umbrella
    would otherwise turn 3 attempts into 3 hours — and past
    ``UPDATE_RETRY_ESCALATE_AFTER`` attempts the version is ESCALATED: no more
    automatic retries until a newer version appears or an owner re-runs
    ``brain update`` by hand (which starts a fresh record via
    ``check_update_available``, not this counter)."""
    attempts = int((previous or {}).get("attempts", 0) or 0)
    if attempts >= UPDATE_RETRY_ESCALATE_AFTER:
        return "escalate"
    if (previous or {}).get("attempted_on") == today.isoformat():
        return "wait"
    return "retry"
