#!/usr/bin/env python3
"""The ONE producer of a thread's readable newest-message text.

Its own module, and not a fourth function inside `cos_signals`, for two
reasons that both matter. `cos_signals` sits at the repository's 500-LOC
bound, so anything added there is paid for by deleting rationale that a later
reader needs; and `cos_signals_stale` — which imports `cos_signals` — is the
sibling that has to call this, so a home BELOW both of them is what keeps the
import graph a line rather than a ring.

WHY IT EXISTS AT ALL. `cos_signals_stale.stale_signals_for_row` used to
rebuild this pair itself and dropped the corpus row's `extraction.error`. On
one rights-protected row (`"Please confirm no later than 2026-08-01."`, run
date 2026-08-25) the parent reported `body_unreadable: True` /
`live_deadline: False` while the sibling reported `stale_deadline_passed:
True` — the exact pair `cos_judge_rules_stale._date_passed_refusal` accepts,
so an `act` thread was archivable on evidence the run itself had thrown away
(review 2026-08-25, finding 2). `cos_signals`'s own header promised the two
"cannot drift into disagreeing about the same sentence"; one producer is what
makes that true by construction rather than by care.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def readable_newest_text(row: dict[str, Any], corpus_row: dict[str, Any] | None
                         ) -> tuple[str, bool]:
    """``(newest message text, body_unreadable)`` for one thread.

    The extraction RESULT lives on the CORPUS row (the ledger records the
    verdict, the corpus records what the read actually returned). An unreadable
    body contributes NO content evidence — reading markers out of a UI
    placeholder would be reading the mail client, not the mail — so the text
    handed back is empty and every marker scan over it comes back False.
    """
    import cos_signals                                          # noqa: PLC0415
    prov = ((corpus_row or {}).get("provenance") or {})
    text = (corpus_row or {}).get("text") or ""
    ext = (corpus_row or {}).get("extraction")
    unreadable = cos_signals.body_unreadable(
        body_opened=bool(row.get("body_opened")), text=text,
        body_chars=int(row.get("body_chars") or 0), subject=prov.get("subject"),
        extraction_error=(ext or {}).get("error") if isinstance(ext, dict) else None)
    return ("" if unreadable else cos_signals.newest_message_text(text)), unreadable
