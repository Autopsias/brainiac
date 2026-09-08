"""Belt 2 of the READ-NOISE lane — RULE 1's substance gate, off the ledger row.

Split out of `cos_mutate_plan` on 2026-09-02 for the same reason
`cos_mutate_plan_aged` and `cos_mutate_plan_stale` were: that module's 500-LOC
bound. The rule is unchanged and the parent re-imports the name, so every
caller and every test still reaches it at `cos_mutate_plan.noise_read_refusal`.
"""
from __future__ import annotations

from typing import Any


def noise_read_refusal(row: dict[str, Any],
                       signed_ingested: frozenset[str] | set[str] = frozenset()
                       ) -> str | None:
    """(RULE 1, owner) Belt 2 of the READ-NOISE lane's substance gate.

    The HARD invariant — archive "always and only if everything was ingested
    back" — holds for EVERY archive, so read-noise carries the same substance
    screen aged-read does. A `read-noise-bucket` row archives ONLY when the
    ingest pass found nothing to vault (`no-substance`) OR the vault already
    holds a SIGNED copy of its substance (`signed_ingested`, SHIPPED
    2026-08-31); a `held`/`candidate` row whose substance is not yet signed
    WAITS. run213: held 2 rows the aged-read belt could not see.
    """
    if row.get("noise_signal") != "read-noise-bucket":
        return None
    disp = str(row.get("disposition") or "").strip().lower()
    cid = str(row.get("conversation_id") or "")
    if disp != "no-substance" and cid not in signed_ingested:
        return ("the ingest pass did not clear this read-noise thread as "
                f"`no-substance` (disposition={disp or 'unset'!r}) and no signed "
                "vault copy exists yet — Rule 1 archives a read thread only after "
                "its substance is vaulted, so it waits, never archived blind")
    return None
