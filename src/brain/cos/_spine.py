"""COS spine operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import _env_days, _parse_ts, _ts
from ._priority import load_priority_overrides

def _is_keeper_counterparty(vault, counterparty: str | None) -> bool:
    if not counterparty:
        return False
    overrides = load_priority_overrides(vault)
    name = str(counterparty).lower()
    if overrides.get(name) == "high":
        return True
    # Override keys are NOTE-ID SLUGS (the only form _OVERRIDE_LINE_RE parses),
    # but a commitment's counterparty is a display name from mail — e.g. a name
    # like "Renée Dûval" could never equal "renee-duval", so keeper detection
    # silently never fired (found 2026-07-17, the day the first real roster was
    # written). Compare in slug space, accents folded.
    import unicodedata
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return overrides.get(slug) == "high"

def _spine_ingest_commitment(vault, meta: dict[str, Any], *, source_ref: str,
                             now: _dt.datetime) -> bool:
    """Record ONE accepted commitment candidate into the spine. Returns
    whether it also qualifies to be signed as a brain note (keeper)."""
    from .. import spine as spine_mod

    direction = meta.get("direction") or "owed_by_me"
    if direction not in spine_mod.DIRECTIONS:
        direction = "owed_by_me"
    counterparty = str(meta.get("counterparty") or meta.get("title") or "unknown")
    text = str(meta.get("text") or meta.get("title") or source_ref)
    due = meta.get("due")
    topic = meta.get("topic")
    spine_mod.record_event(vault, event="created", direction=direction,
                           counterparty=counterparty, text=text, topic=topic,
                           due=due, source_ref=source_ref, ts=_ts(now))
    due_dt = _parse_ts(due) if due else None
    horizon = _env_days(KEEPER_HORIZON_DAYS_ENV, DEFAULT_KEEPER_HORIZON_DAYS)
    horizon_ok = bool(due_dt and (due_dt - now).days >= horizon)
    return _is_keeper_counterparty(vault, counterparty) and horizon_ok

__all__ = ['_is_keeper_counterparty', '_spine_ingest_commitment']
