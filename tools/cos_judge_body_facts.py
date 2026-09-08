"""What the body-open facts say about WHY there is no body (ZERO-03).

A separate module because `cos_judge_grounding` sits at the file-size ratchet's
bound, and because this is a fact about the FETCH rather than about the
judgment — the same split `cos_driver_accounting.open_outcome` already makes.
"""
from __future__ import annotations

from typing import Any


def no_body_held_reason(row: dict[str, Any], default: str) -> str:
    """`server-returned-no-body` when the fallback ANSWERED and answered nothing.

    Seven threads have refused their body on every full run for weeks and worn
    `no-body-access-on-lane` for it. That word says the LANE failed to reach a
    body that is there — a machine failure the night should keep re-attempting.
    These rows are the opposite. The first shape (`AllProperties/Text`) answers
    HTTP 500; the fallback (`Default/HTML`) then answers `NoError`/200 with ZERO
    characters. A *successful* answer of nothing is the server reporting there
    is no message body to reach, and no future night changes it.

    READ FROM THE LEDGER, never reasoned about: runs 260, 262 and 263
    (2026-09-05) each carry these seven rows with exactly this error object.
    81dbd37f added `item_class`/`retry_item_class` to it to tell the two cases
    apart, and its answer is that NEITHER shape hands back an item — both keys
    are ABSENT on all seven, and present only on the eighth (shell) row, which
    does get one. So the three fetch facts below are the whole available answer,
    and they are enough to classify it.

    All three, never a subset: a retry that never ANSWERED (run 254's rows carry
    no `retry_code`) is a lane failure and keeps `default`.
    """
    err = row.get("body_open_error") or {}
    if (err.get("retry_code") == "NoError" and err.get("retry_status") == 200
            and err.get("retry_chars") == 0):
        return "server-returned-no-body"
    return default
