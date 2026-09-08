"""One ledger row for one enumerated conversation — the driver's row builder.

Split out of `cos_driver_accounting` on 2026-09-02 for the same reason
`cos_mutate_plan_noise` was split out of its parent: that module's 500-LOC
bound. The function is lifted WHOLE and nothing in it changed; the parent
re-imports the name, so every caller and every test still reaches it at
`cos_driver_accounting._ledger_row`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_draw import _tier, _tier_source  # noqa: E402
from cos_driver_transport import (  # noqa: E402
    BODY_BUDGET, BODY_OPEN_CAP, READ_LANE)


def _ledger_row(c: dict[str, Any], *, bodies: dict[str, Any],
                opened_seq: dict[str, int], run_id: str,
                bundle_version: str, rules_version: str, enumerated_at: str,
                gate_excluded: set[str] | frozenset[str], cap: int | None,
                read_never: bool = False,
                never_ids: set[str] | frozenset[str] = frozenset()
                ) -> dict[str, Any]:
    """One ledger row for one enumerated conversation, computed from the
    capture and nothing else — see `build_accounting` for why every judgment
    slot below is `None`."""
    # At CALL time: a module-level import would cycle with the parent.
    from cos_driver_accounting import (  # noqa: PLC0415
        body_open_fields, carries_ingest_mark)
    cid = c["convId"]
    b = bodies.get(cid)
    opened = cid in opened_seq
    return {
        "run": run_id,
        "run_profile": "full",
        "conversation_id": cid,
        "message_id": c.get("itemId"),
        "received": c.get("received"),
        "read_state": "read" if c.get("isRead") else "unread",
        # ONE DRAFT PER THREAD (FIX-01): a THREAD-level fact from the
        # read pass's drafts census (`cos_driver_page.js` enumerates the
        # Drafts folder and marks every inbox conversation carrying one),
        # so the planner can refuse to draft a thread that already has a
        # draft. `False` — never `None` — when no census ran: the planner
        # reads absent and no-draft the same way, and a nullable field
        # would only invite a third spelling.
        "isDraft": c.get("isDraft") is True,
        "read_lane": READ_LANE,
        "tier": _tier(c.get("categories")),
        "tier_source": _tier_source(c.get("categories")),
        "carries_ingest_mark": carries_ingest_mark(c.get("categories")),
        "body_opened": opened,
        # A FACT ABOUT THE PASS, not a judgment about the mail: this row was
        # held out of the rule-1½ draw because the category batch stamped it
        # with an id the owner's taxonomy dispositions `never`. The judgment
        # is the CATEGORY, and it is the model's; what the driver records is
        # that the body was consequently never opened. `cos_judge`'s
        # `mechanical_disposition` reads this to write rule 1¾'s pairing
        # (`no-substance` / `never-category`) without asking the model to
        # re-decide something already on disk.
        "category_gate_excluded": cid in gate_excluded,
        # THE GATE STATES ITS OWN INPUT (ruling 2026-09-02): this night opened
        # `never` bodies on purpose, and nothing else on the row separates that
        # from the rule-1¾ leak the verifier catches (run 103 opened 11 with the
        # gate unarmed). `never_category` is the TAXONOMY's answer;
        # `category_gate_excluded` above is what the DRAW did about it. One
        # field said both until the ruling — see `_apply_read_never`.
        "read_never_categories": bool(read_never),
        "never_category": cid in never_ids,
        # What the open did, and why it was refused — see `body_open_fields`.
        **body_open_fields(b, opened, opened_seq.get(cid)),
        "body_budget": BODY_BUDGET,
        # THE CAP THE RUN USED (FIX-02, batch-4 reporting defect): the row
        # carried the module CONSTANT, so run188 — launched uncapped and
        # opening 123 bodies — recorded `staging_cap: 20` beside a number
        # that cannot fit under it. `None` (a replay of a night that never
        # recorded one) keeps the constant those nights actually wrote.
        "staging_cap": BODY_OPEN_CAP if cap is None else int(cap),
        # DERIVED FROM THE CAPTURE, never the constant "not-exercised"
        # (FIX-02): a row whose opened body carries attachment parts DID
        # see the file lane. Still one of the closed vocabulary's three
        # words; the per-run COUNTS live on the metrics row
        # (`cos_driver_night_records.stamp_attachment_lane`).
        "attachment_lane": ("downloads-mounted"
                            if opened and (b or {}).get("attachments")
                            else "not-exercised"),
        # THE MISSING PRODUCER. `cos_ingest_bridge_content._attachment_names`
        # has read `row["attachments"]` since the file lane shipped and
        # NOTHING has ever written it, so every candidate the live taxonomy
        # routes to a file-carrying lane (`regulatory-filing` -> attachment,
        # `market-digest` / `system-notification` -> both) quarantines
        # `attachment-names-missing`. The names come off the SAME
        # `AllProperties` GetItem the body pass already pays for — see
        # `attachmentsOf` in `cos_driver_page.js`. Empty on a row whose body
        # did not open: a refused open is not evidence of no attachment.
        "attachments": list((b or {}).get("attachments") or []) if opened else [],
        # An ABSENT list under `HasAttachments: true` is a different fact
        # from an empty one, and the ledger must not spell them the same
        # way. `item_keys` is the page's own witness of which properties the
        # build actually returned.
        "attachments_withheld": bool(opened and (b or {}).get("item_keys")),
        "send_attempted": False,
        "extraction_rules_version": rules_version,
        "bundle_version": bundle_version,
        "ts": enumerated_at,
        # --- judgment slots, owned by s03 and left EMPTY on purpose -------
        "verdict": None,
        "category": None,
        "disposition": None,
        "held_reason": None,
        "dedup_check": None,
        "candidate_count": 0,
        "proposal_id": None,
        "content_sha256": None,
        "judgment_pending": True,
    }
