"""The mutation policy constants of `cos_mutate` — permitted and refused action sets, folders, draft forms (batch-2 drain).

Moved verbatim out of `cos_mutate`; the parent re-exports every name, and the
siblings whose moved code reads them (the evidence builder, the undo ledger)
import them here rather than from the parent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cos_reconcile_metrics import MUTATION_VERBS  # noqa: E402


#: `--allow-draft-resume` is refused without this. See `DRAFT_RESUME_POLICY`.
DRAFT_RESUME_POLICY = (
    "EXCLUDED from autonomous resume. A draft lost to a dropped response is "
    "reconcilable in principle — this run embeds a machine signature in the "
    "body and the reconciliation query joins the Drafts folder on conversation "
    "id and confirms that signature, which is proven end to end under fault "
    "injection (tests/js/cos_mutate_page.test.mjs). What is NOT proven is that "
    "a live Drafts enumeration returns ConversationId on this build: no live "
    "mutation ran in the session that wrote this module. Until one does, a "
    "draft in state `sent` escalates to manual resolution instead of being "
    "re-created, because a duplicated reply draft in the owner's mailbox is a "
    "worse outcome than a line in a report.")


PERMITTED_ACTIONS = ("MoveItem", "UpdateItem", "CreateItem",
                     "ApplyConversationAction")

#: `ApplyConversationAction` is admitted by its ACTION VALUE, never by the verb.
#: Measured 2026-08-11 on the live mailbox: this build archives with
#: `Action: "Move"` and writes a chip with `Action: "UpdateAlwaysCategorizeRule"`
#: — one verb, and per EWS the same verb also carries `Delete`, `SetReadState`
#: and `AlwaysDelete`. Admitting the verb would admit deleting the owner's mail.
#: `UpdateAlwaysCategorizeRule` joins it by OWNER RULING 2026-08-11 — the chip
#: lane ships on this build's real behaviour. It still writes a standing rule,
#: which every chip's run-report line says, and removal stays a human action
#: until a clear shape is captured.
PERMITTED_CONVERSATION_ACTIONS = ("Move", "UpdateAlwaysCategorizeRule")

#: REFUSED, and this one is a decision rather than an omission: it does not
#: write a per-conversation label, it leaves a STANDING RULE that categorises
#: future messages in the thread. The chip lane is specified as a reversible
#: label and removing a chip does not remove a rule, so the chip lane stays
#: closed until a per-item categorize shape is captured or the owner restates
#: what that lane may do.
REFUSED_CONVERSATION_ACTIONS = ("Delete", "SetReadState", "AlwaysDelete",
                                "AlwaysMove")
PERMITTED_FOLDERS = ("archive", "inbox")
DRAFT_FOLDER = "drafts"
SAVE_ONLY = "SaveOnly"

CANARY_MAX_AGE_DAYS = 30


#: The draft prompt's own two-value `form` vocabulary (`cos_judge.py:1066`,
#: `"form": "standard|acknowledge-late"`). Anything else is a model that
#: answered outside its vocabulary, and it is projected — never interpolated.

#: The live half of a night's mutation proof is the MAIN SESSION's (owner
#: ruling 2026-08-08); this is the marker the evidence block writes for it.
PENDING = "pending-main-session"


DEFAULT_CAPS = {v: None for v in MUTATION_VERBS}

#: The recency window, in days. A mutation on a thread older than this is out of
#: scope — "the last couple of weeks", not the whole mailbox. `None` lifts it
#: (historic / "do it in all"). Env: `$BRAIN_COS_SINCE_DAYS`.
DEFAULT_SINCE_DAYS = int(os.environ.get("BRAIN_COS_SINCE_DAYS", "14") or "14")

#: The state machine. `intent` is on disk before the call; `sent` means the
#: request left and its outcome is not yet known; `confirmed` means the server
#: answered NoError; `reconciled` means a RE-READ found the effect.
STATES = ("intent", "sent", "confirmed", "reconciled",
          "aborted-not-applied", "verification-failed", "unknown")
TERMINAL = ("reconciled", "aborted-not-applied", "verification-failed", "unknown")


#: The FOUR managed priority chips (DOCTRINE v7 §4 — `P3 · Read` is additive;
#: the three older names are never renamed, recoloured or reused, because an
#: Outlook category name is immutable once created). A category write may only
#: add or remove one of these; every other category on the item is preserved.
#:
#: A LITERAL, and duplicated in `tools/cos_mutate_page.js` on purpose: the page
#: half is injected as source and cannot import. `tests/test_cos_mutate.py`
#: pins BOTH halves against this tuple, so the two cannot drift — and adding a
#: name here WIDENS the mutation surface `isManaged()` guards, which is why the
#: pin is explicit rather than derived.
MANAGED_CHIPS = ("P0 · Now", "P1 · Today", "P2 · This week", "P3 · Read")

#: (bucket, tier) -> chip. IMPORTED, never restated: `brain.cos_chips.CHIP_FOR`
#: is the one definition (DOCTRINE v7 §4.1). It replaced a tier-only
#: `CHIP_FOR_TIER`, which structurally could not express `read`/P2 →
#: `P3 · Read` beside `act`/P2 → `P2 · This week` and had no answer at all for
#: `act`/P3.


#: The FOUR managed priority chips (DOCTRINE v7 §4 — `P3 · Read` is additive;
#: the three older names are never renamed, recoloured or reused, because an
#: Outlook category name is immutable once created). A category write may only
#: add or remove one of these; every other category on the item is preserved.
#:
#: A LITERAL, and duplicated in `tools/cos_mutate_page.js` on purpose: the page
#: half is injected as source and cannot import. `tests/test_cos_mutate.py`
#: pins BOTH halves against this tuple, so the two cannot drift — and adding a
#: name here WIDENS the mutation surface `isManaged()` guards, which is why the
#: pin is explicit rather than derived.
MANAGED_CHIPS = ("P0 · Now", "P1 · Today", "P2 · This week", "P3 · Read")

#: Which chip the cap should spend a slot on first.
CHIP_RANK = {MANAGED_CHIPS[0]: 0, MANAGED_CHIPS[1]: 1, MANAGED_CHIPS[2]: 2,
             MANAGED_CHIPS[3]: 3}


#: HOW MANY ROWS MAY VANISH before a night is an enumeration failure rather
#: than a busy mailbox (review 2026-08-12). A thread moving mid-run is ordinary
#: and one or two a night is expected; a quarter of the plan disappearing is
#: the browser reading a starved folder, and carrying on would apply the night
#: to whatever fraction of the mailbox happened to render. The cap is a
#: FRACTION of the plan with an absolute floor, so a 4-row hand-run is not
#: tripped by its first skip and a 200-row night is not allowed 50. Tripping it
#: STOPS the pass exactly like a verification failure: everything before it is
#: applied and verified, nothing after it runs.
ABSENT_SKIP_FRACTION = 0.25



ABSENT_SKIP_FLOOR = 5



#: `receipts` is the one NESTED value, and key closure cannot bound a nested
#: free value — so it gets a shape rule. These are the keys the page actually
#: emits (`cos_mutate_page.js:1902` draft, `:1939` archive); nothing else.
RECEIPT_KEYS = frozenset({
    "is_draft", "signature_present", "send_attempted",
    "moved_item_resolves", "source_folder", "source_absent",
    "source_enumeration_complete", "source_enumeration_terminated",
    "source_items_seen", "source_total_in_view",
    "deleted_items_absent", "deleted_items_enumeration_complete",
    # the marker a refused post-dispatch row is rewritten with
    "refused",
})



#: Permission for the artifacts this tool writes under `<vault>/cos-ops` and
#: into the run's evidence directory. `cos._write_atomic` defaults to 0o600
#: (host-private), which these were NOT before round 7 — they were plain
#: umask-derived 0o644 — and a hardening pass that also silently narrows a
#: permission is two changes wearing one commit. 0o644 keeps them exactly as
#: they are today; the plan binding, which really is host-private, takes the
#: helper's own default.
_OPS_MODE = 0o644
