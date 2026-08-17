#!/usr/bin/env python3
"""The COS mutation module — guards kept, clicks gone (MUT-01, S04).

WHAT THIS IS. The host half of the three mailbox mutations a COS night makes:
archive, priority-chip write, and reply-draft SAVE. It owns the ledger, the
caps, the kill switch and the state machine; `tools/cos_mutate_page.js` owns the
payloads and the auth, because auth may not leave the page. Neither half can act
without the other, which is the containment.

WHY IT IS A SEPARATE FILE FROM `cos_driver.py`. The read driver's proven
property is that no mutation verb appears anywhere in it or in its page half,
and `tests/test_cos_driver.py` asserts that mechanically. Bolting mutations onto
it would have deleted a proof to satisfy a filename. The mutation module keeps
its own, different property — every outgoing payload is validated at RUNTIME
against an allowlist — and shares the read driver's transport, staging and
bridge rather than its guarantees.

THE ORDER IS THE SAFETY. Per mutation, in this order and no other:

  1. resolve the target by CONVERSATION ID (ids rotate; a list-view ItemId is a
     session handle, never an identity — v4.7);
  2. write the undo row, state `intent`, WITH the before-image, BEFORE the call;
  3. re-fetch the ChangeKey immediately before dispatch, validate the payload
     against the allowlist, dispatch;
  4. verify by RE-READ, and treat a response that changed nothing as a
     verification FAILURE rather than a success;
  5. record `reconciled`, or STOP the run.

RESUME, NEVER RESTART (G7). Caps count what is applied THIS CHECKPOINT, read
off the undo ledger and not off a process counter, and a mutation whose ledger
row is already terminal is skipped. A half-applied batch is a legitimate state
and the evidence says exactly which rows are applied, which are rolled back and
which are unknown — "unknown" is an honest value, a silent second pass is not.

THE LIVE PASS IS THE MAIN SESSION'S (owner ruling 2026-08-08). A dispatched
subagent is classifier-refused on live mailbox mutation and returns zero writes.
`--dry-run` exists so the live pass can be inspected before it is run: it walks
the whole thing against the live mailbox read-only and stops one line before
`fetch`, printing exactly what would be sent.

    python3 tools/cos_mutate.py plan     --vault <v> --run-id <r>
    python3 tools/cos_mutate.py dry-run  --vault <v> --run-id <r> --tab-id <id>
    python3 tools/cos_mutate.py apply    --vault <v> --run-id <r> --tab-id <id>
    python3 tools/cos_mutate.py canary   --vault <v> --run-id <r> --tab-id <id> \\
                                         --canary-convid <id>
    python3 tools/cos_mutate.py capture-shapes --vault <v> --tab-id <id>
    python3 tools/cos_mutate.py selfcheck
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_driver as drv                                          # noqa: E402
from brain import cos_chips as chips                              # noqa: E402
# The undo ledger's counting definition. It MOVED to `cos_reconcile_metrics`
# (s10, 2026-08-16) and is imported back here under its original names, so every
# existing caller of `cos_mutate.applied_counts` / `MUTATION_VERBS` /
# `APPLIED_STATES` / `VERB_COUNTER` is unchanged. It had to move because the
# ledger↔metrics join needs the SAME definition and this module is deliberately
# absent from the engine asset mirror — see the note beside it over there.
# Imported EAGERLY and unguarded on purpose: the cap accounting is built on it,
# and a missing counter must stop the apply, never silently count zero.
from cos_reconcile_metrics import (                                # noqa: E402
    APPLIED_STATES, MUTATION_VERBS, VERB_COUNTER, applied_counts,
)

HERE = Path(__file__).resolve().parent
PAGE_JS = HERE / "cos_mutate_page.js"
HOOK_JS = HERE / "cos_capture_hook.js"

#: The mutation lane this module elects. E17 canaries are per-lane and a canary
#: for a different lane does NOT satisfy the gate.
MUTATION_LANE = "rest"
PRIMITIVE = {"archive": "rest-conversation-move",
             "categorize": "rest-categorize",
             "draft": "rest-create-draft"}

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
chip_for = chips.chip_for

#: Which chip the cap should spend a slot on first.
CHIP_RANK = {MANAGED_CHIPS[0]: 0, MANAGED_CHIPS[1]: 1, MANAGED_CHIPS[2]: 2,
             MANAGED_CHIPS[3]: 3}

#: The three mutation verbs, in one place, for iterating the counters —
#: imported above from `cos_reconcile_metrics` beside `applied_counts`.

#: THE TYPED FACT that says THE TARGET WAS NOT THERE, nothing was dispatched,
#: and the night should carry on. They are skips wearing a failure word — the
#: page half returns `verification: "verified-failed"` for them because it has
#: one word for "no" — so the host discriminates on `dispatched` and on this
#: flag, never on the word (run 125, 2026-08-12).
#:
#: IT USED TO BE A LIST OF OUTCOME STRINGS, and that is exactly how it failed
#: (review 2026-08-12). The list held `target-not-found` and
#: `source-thread-not-found` — the categorize and draft lanes' words — while
#: the ARCHIVE lane mints its own, `already-absent-from-<folder>`. So no number
#: of vanished archive targets ever matched, none of them reached the skip cap,
#: and a night that found nothing to archive reported completion. A list two
#: producers must both remember to join is the defect; a flag every absence
#: site sets cannot be forgotten by a lane that does not appear in it.
#: `tests/test_cos_mutate.py::test_the_absent_target_flag_is_the_page_halfs_own`
#: reads the page half and asserts every skip site sets THIS key.
ABSENT_TARGET_FLAG = "absent_target"

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


def absent_skip_cap(planned: int) -> int:
    """The ceiling on conclusive-absence skips for a plan of `planned` rows."""
    return max(ABSENT_SKIP_FLOOR, int(planned * ABSENT_SKIP_FRACTION))


#: NO ARTIFICIAL NUMERIC CAP (owner ruling 2026-08-11: "the content and emails
#: and context should drive that, not pre-established artificial limits"). A cap
#: of `None` is unlimited; the SCOPE is what bounds a night now — `since_days`
#: below, plus per-lane self-exclusion (an archived thread leaves the inbox, a
#: chipped thread is skipped, a thread with a draft is skipped). A number can
#: still be passed per verb for a deliberately small hand-run.
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

#: `APPLIED_STATES` and `VERB_COUNTER` are imported at the top of this file from
#: `cos_reconcile_metrics`. ONE definition, because THREE sides now read it: the
#: apply WRITES these counters from what it dispatched,
#: `cos_runverify.check_metrics_row` RECOUNTS them from the same ledger, and the
#: ledger↔metrics join re-counts them per date. Two spellings of one mapping is
#: how the row and the recount drift apart, which is the defect this closes.

# --- the undo ledger's CLOSED FIELD SET (grounding design D14, sink 11) -----
#: `_cos_undo_ledger_<run>.jsonl` is written at APPLY, long after grounding
#: exists, so the ordering argument that covers the capture corpus is FALSE for
#: it. What covers it is that no key here is free text a model could author —
#: and that argument only holds if it is enforced where the row is SERIALIZED.
#: `_undo_row` ends with `row.update(extra)` and the apply path serializes
#: `dict(intent, …)` merges anyway, so a rule pinned to `_undo_row` constrains
#: nothing that reaches disk. It is enforced in `UndoLedger.append`.
#:
#: THE BOUND IS A SUBSET, NOT AN EQUALITY, and that is a decision rather than a
#: slip (design revision 4, carried finding 3). Exact key-set equality is
#: unsatisfiable as the record stated it: the write-ahead `intent` row
#: serializes 24 keys against this 28-key set, and the four merge keys do not
#: exist yet when it is written. The UPPER bound — nothing outside this set may
#: be serialized — is the half that closes the sink, and it is unambiguous. The
#: lower bound is deliberately not asserted: filling absent keys with `None`
#: would make every intent row claim a `receipts` it does not have.
LEDGER_ROW_KEYS = frozenset({
    # the 23 `_undo_row` names
    "idempotency_key", "conversation_id", "conversation_id_digest", "verb",
    "state", "reason", "account", "message_id", "key_scheme", "thread_id",
    "mutation_lane", "original_folder", "destination_folder", "action_ts",
    "primitive", "connector_result", "verification", "chip", "mode",
    "before_image", "item_id_at_resolve", "changekey_refetched_at", "run",
    # the stamp `append` itself adds
    "ts",
    # and exactly the four the apply/unchip merges add
    "new_item_id", "dispatched", "receipts", "observed_after",
})

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


def _fold(value: Any) -> str:
    """NFC + casefold, for the ONE comparison in the receipts rule.

    THE PAGE DERIVES THE FOLDER NAMES ITSELF and the ledger hard-codes them:
    `prepareArchive` computes `var source = "inbox"` (lowercase,
    `cos_mutate_page.js:1496-1497`) while `_undo_row` writes
    `"original_folder": "Inbox"`. Exact string equality matches NEITHER, so the
    rule as first written refused the archive lane's own receipts on the HAPPY
    PATH and would have stopped an applying night on its first archive. Both
    specified tests were known negatives, so neither could catch it: the rule
    had no known positive. It has one now
    (`test_a_real_archive_receipts_payload_passes_unchanged`).
    """
    return unicodedata.normalize("NFC", str(value)).casefold()


def receipts_shape_ok(receipts: Any, row: dict[str, Any]) -> bool:
    """`None`, or a flat mapping of known keys whose values are `bool`, `int`,
    `None`, or a string equal — NFC + casefold — to this row's own
    `original_folder` or `destination_folder`. That leaves no string a model
    could author."""
    if receipts is None:
        return True
    if not isinstance(receipts, dict):
        return False
    # An ABSENT folder is not a match target: `{""}` would let an
    # attacker-authored empty string through on a row that names no folder.
    folders = {_fold(v) for v in (row.get("original_folder"),
                                  row.get("destination_folder")) if v}
    for key, value in receipts.items():
        if key not in RECEIPT_KEYS:
            return False
        if isinstance(value, bool) or value is None or isinstance(value, int):
            continue
        if isinstance(value, str) and _fold(value) in folders:
            continue
        if key == "refused" and value == "shape":
            continue
        return False
    return True

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

# --- denylist: named here so a payload carrying one is REFUSED --------------
# These literals exist in this file only to be rejected. `tests/test_cos_mutate.py`
# reads this block by its markers, so the audit can tell a denial from a use.
BANNED_ACTIONS = ("SendItem", "DeleteItem", "MarkAsJunk", "MarkAllItemsAsRead",
                  "EmptyFolder", "ExportItems", "UploadItems", "CreateAttachment")
BANNED_DISPOSITIONS = ("SendOnly", "SendAndSaveCopy", "SendToNone",
                       "SendOnlyToAll", "SendOnlyToChanged",
                       "SendToAllAndSaveCopy", "SendToChangedAndSaveCopy")
# --- end denylist ------------------------------------------------------------

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
DRAFT_FORMS = ("standard", "acknowledge-late")


def draft_form(value: Any) -> str:
    """`value` projected onto `DRAFT_FORMS`, else `unspecified`."""
    v = str(value or "").strip().casefold()
    return v if v in DRAFT_FORMS else "unspecified"


class MutationStop(drv.DriverStop):
    """A condition the mutation module refuses to run past."""


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ts(dt: _dt.datetime | None = None) -> str:
    return (dt or _utcnow()).astimezone(_dt.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def short(value: str) -> str:
    return drv.short(value)


# ---------------------------------------------------------------------------
# preflight: the vault, the kill switch, the canary
# ---------------------------------------------------------------------------
def assert_vault(vault: Path) -> dict[str, Any]:
    """G0. Every COS artifact lives in the deployment's own vault, and this
    repository has no `vault/cos-ops/` at all — an unqualified relative path
    resolves to nothing here, and a `brain cos-*` command run with the wrong CWD
    writes a stray run into the wrong vault (measured: a phantom run-999
    manifest). So the root is ASSERTED, with the numbers it was asserted on."""
    from brain import cos                                        # noqa: PLC0415

    if not vault or not vault.is_dir():
        raise MutationStop(
            f"$BRAIN_VAULT / --vault names no directory ({vault!r}). Set it to "
            "the COS deployment's vault before anything else.")
    ops = cos.run_ops_dir(vault)
    if not ops.is_dir():
        raise MutationStop(f"{ops} does not exist — this is not a COS vault")
    ledgers = sorted(p.name for p in ops.glob("_cos_ingestion_ledger_*.jsonl"))
    if not ledgers:
        raise MutationStop(
            f"{ops} holds no ingestion ledger at all — a mutation pass has "
            "nothing to act on and this is almost certainly the wrong vault")
    return {
        "BRAIN_VAULT": str(vault),
        "raw_source_count_at_preflight": len(list((vault / "raw").rglob("*.md"))),
        "cos_ops_exists": True,
        "ingestion_ledgers_observed": ledgers[-4:],
        "asserted_before_any_browser_action": True,
    }


def kill_switch(vault: Path) -> dict[str, Any]:
    """`overlay/cos/auto-archive.md`. Absent ⇒ enabled; unparseable ⇒ DISABLED.

    The overlay is the owner's lever and it is read literally, never inferred.
    """
    path = vault / "overlay" / "cos" / "auto-archive.md"
    if not path.exists():
        return {"enabled": True, "source": str(path), "state": "absent-defaults-on"}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"enabled": False, "source": str(path),
                "state": f"unreadable ({exc}) — a kill switch that cannot be "
                         "read is OFF"}
    enabled = True
    cap = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("enabled:"):
            enabled = s.split(":", 1)[1].strip().lower() == "true"
        elif s.startswith("cap:"):
            try:
                cap = int(s.split(":", 1)[1].strip())
            except ValueError:
                cap = None
    return {"enabled": enabled, "cap": cap, "source": str(path), "state": "read"}


def stop_file(vault: Path, run_id: str) -> Path:
    from brain import cos                                        # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_mutation_stop_{run_id}"


def stopped(vault: Path, run_id: str) -> bool:
    """The emergency brake, re-read BETWEEN every mutation. `touch` the file and
    the pass stops after the mutation in flight, not at the end of the batch."""
    return stop_file(vault, run_id).exists()


def canary_status(vault: Path, *, lane: str = MUTATION_LANE,
                  now: _dt.datetime | None = None) -> dict[str, Any]:
    """E17, evaluated rather than assumed.

    Three clauses, all of them binding: the record must be FOR THIS LANE, at
    most 30 days old, and carry per-step verification RECEIPTS — "a canary file
    lacking per-step verification receipts is a FAIL (a written file is not a
    run drill)". The live `rest` record fails the receipts clause today, which
    is why this returns a structure rather than a boolean.
    """
    from brain import cos                                        # noqa: PLC0415

    path = cos.run_ops_dir(vault) / "_cos_undo_canary.json"
    out = {"lane": lane, "path": str(path), "lane_match": False,
           "canary_tested_utc": None, "canary_age_days": None,
           "receipts_present": False, "idempotent_replay": None, "valid": False,
           "why": ""}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["why"] = f"no readable canary record at {path} ({exc})"
        return out
    # A legacy flat pre-v5.7 file reads as the `rest` lane's record.
    record = (doc.get("lanes") or {}).get(lane)
    if record is None and lane == "rest" and doc.get("tested"):
        record = doc
    if record is None:
        out["why"] = f"the canary file carries no record for the {lane!r} lane"
        return out
    out["lane_match"] = True
    out["canary_tested_utc"] = record.get("tested")
    out["idempotent_replay"] = record.get("idempotent_replay")
    receipts = record.get("receipts")
    out["receipts_present"] = bool(receipts) and isinstance(receipts, dict)
    try:
        tested = _dt.datetime.fromisoformat(
            str(record.get("tested")).replace("Z", "+00:00"))
        age = ((now or _utcnow()) - tested).total_seconds() / 86400.0
        out["canary_age_days"] = round(age, 2)
    except (TypeError, ValueError):
        out["why"] = "the canary record carries no parseable `tested` timestamp"
        return out
    reasons = []
    if out["canary_age_days"] > CANARY_MAX_AGE_DAYS:
        reasons.append(f"the drill is {out['canary_age_days']:.0f} days old "
                       f"(max {CANARY_MAX_AGE_DAYS})")
    if not out["receipts_present"]:
        reasons.append("the record carries no per-step verification receipts — "
                       "a written file is not a run drill")
    if str(record.get("idempotent_replay")) != "confirmed":
        reasons.append("idempotent_replay is not `confirmed`")
    out["valid"] = not reasons
    out["why"] = "; ".join(reasons) if reasons else "lane, age and receipts all hold"
    return out


# ---------------------------------------------------------------------------
# the plan: what the JUDGE decided, re-screened mechanically
# ---------------------------------------------------------------------------
def draft_signature(run_id: str, conv_id: str) -> str:
    """The machine signature embedded in every draft this run saves.

    It is what makes a lost `CreateItem` response reconcilable: the Drafts
    folder is joined on conversation id and the match is CONFIRMED by this
    string, so "did the server accept it" has an answer that does not depend on
    a response we never received.
    """
    return f"[cos:{run_id}:{short(conv_id)[:12]}]"


def _ledger_path(vault: Path, run_id: str) -> Path:
    from brain import cos                                        # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def _within_window(received: Any, since_days: int | None,
                   now: _dt.datetime) -> bool:
    """Is this thread inside the recency window? Unknown date ⇒ IN.

    `since_days is None` is the historic / "do it in all" mode — everything is
    in scope. A thread whose `received` cannot be parsed is kept rather than
    dropped: the per-lane guards already vetted it, and a scope filter should
    never be the thing that silently discards a real action on a parse slip.
    """
    if since_days is None:
        return True
    try:
        when = _dt.datetime.fromisoformat(str(received))
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return (now - when).total_seconds() <= since_days * 86400


def build_plan(vault: Path, run_id: str, *, caps: dict[str, int] | None = None,
               applied: dict[str, int] | None = None,
               skip_keys: set[str] | None = None,
               since_days: int | None = DEFAULT_SINCE_DAYS) -> dict[str, Any]:
    """Turn the JUDGED ledger into a mutation plan — and re-screen it in CODE.

    The judge decides WHAT (`auto_archive`, `hold_category`, the draft text);
    this re-applies the mechanical screens that are facts rather than opinions,
    so a judgment defect cannot reach the mailbox through a slot the driver
    never re-checked. Every exclusion is recorded with its reason: a plan that
    silently drops rows is how a cap becomes invisible.
    """
    caps = dict(DEFAULT_CAPS, **(caps or {}))
    applied = applied or {}
    rows = _read_jsonl(_ledger_path(vault, run_id))
    if not rows:
        raise MutationStop(
            f"no ingestion ledger for {run_id} — the judgment pass has to have "
            "run before there is anything to execute")
    drafts = _read_jsonl(
        _ledger_path(vault, run_id).with_name(f"_cos_drafts_pending_{run_id}.jsonl"))

    planned: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    def exclude(cid: str, verb: str, why: str) -> None:
        excluded.append({"conversation_id": short(cid), "verb": verb, "reason": why})

    for row in rows:
        cid = row["conversation_id"]
        if row.get("auto_archive") is True:
            if row.get("read_state") != "read":
                exclude(cid, "archive", "read_state is not `read` — an unread "
                                        "row is untouchable by any lane")
            elif row.get("tier") in ("P0", "P1"):
                exclude(cid, "archive", f"tier {row.get('tier')} is hard-excluded "
                                        "from auto-archive under every lane")
            elif row.get("judgment_pending"):
                exclude(cid, "archive", "the row carries no verdict")
            else:
                planned.append({
                    "verb": "archive", "conversation_id": cid,
                    "reason": f"auto-archive: {row.get('verdict')}/"
                              f"{row.get('tier') or 'untiered'}/"
                              f"{row.get('noise_signal') or 'signal-unnamed'}",
                    "tier": row.get("tier"), "read_state": row.get("read_state"),
                    "received": row.get("received"),
                })
        # THE CHIP COMES FROM THE (bucket, tier) MATRIX, not from `hold_category`
        # (which is the judge's hold-REASON vocabulary — `Held · ask` and its
        # siblings — and could never match a managed chip name; wired to it, the
        # lane was unfireable by construction), and not from the tier ALONE
        # (DOCTRINE v7 §4.1: `read`/P2 → `P3 · Read` while `act`/P2 →
        # `P2 · This week`, same tier, different chip). `verdict` is the bucket
        # `apply_judgment` wrote; `judged_tier` is the tier. The lane is
        # ADD-ONLY: it puts a missing chip on, and never touches a thread that
        # already carries one, because clearing a chip is a shape this build has
        # not accepted from us.
        chip = chip_for(row.get("verdict"), row.get("judged_tier"))
        if chip:
            if chip not in MANAGED_CHIPS:
                exclude(cid, "categorize", f"{chip!r} is not a managed priority "
                                           "chip; only the four may be written")
            elif row.get("judgment_pending"):
                exclude(cid, "categorize", "the row carries no verdict")
            elif row.get("tier"):
                exclude(cid, "categorize",
                        f"the thread already carries a {row.get('tier')} chip and "
                        "this lane is ADD-ONLY — it cannot clear or replace one")
            else:
                planned.append({"verb": "categorize", "conversation_id": cid,
                                "chip": chip, "mode": "add",
                                "received": row.get("received"),
                                "reason": f"chip: judged "
                                          f"{row.get('verdict')}/"
                                          f"{row.get('judged_tier')} on a "
                                          "thread carrying none"})

    ledger_ids = {r["conversation_id"] for r in rows}
    received_by_cid = {r["conversation_id"]: r.get("received") for r in rows}
    for d in drafts:
        cid = d.get("conversation_id")
        if d.get("saved_to_mailbox"):
            continue
        if cid not in ledger_ids:
            exclude(str(cid), "draft", "the draft names a conversation this run "
                                       "never enumerated")
        elif not str(d.get("text") or "").strip():
            exclude(cid, "draft", "the draft carries no text")
        else:
            # PLACEHOLDERS ARE REQUIRED, NOT A DEFECT. `draft.placeholder_honesty`
            # (cos_judge) refuses an UNGROUNDED draft that carries NO
            # `[owner: confirm …]` marker — "that is an invented fact in the
            # owner's voice". A screen that excluded placeholder-carrying drafts
            # would have blocked the entire draft lane: all 10 of run 115's
            # drafts carry one, measured 2026-08-10. The draft is saved unsent
            # with its markers visible, which is exactly what they are for.
            planned.append({"verb": "draft", "conversation_id": cid,
                            "text": d["text"],
                            "placeholders": d.get("placeholders") or [],
                            "recipient_scope": d.get("recipient_scope"),
                            "signature": draft_signature(run_id, cid),
                            "received": received_by_cid.get(cid),
                            # PROJECTED, NOT INTERPOLATED (design revision 4,
                            # carried finding 2). `form` is MODEL-AUTHORED and
                            # survives D14's projection; `draft.stale_ask_form`
                            # fires only on stale asks, so `--judge` does not
                            # bound it. `reason` reaches the undo ledger, whose
                            # closed field set argues that no value on the row
                            # is free text — an f-string over an unprojected
                            # model string would falsify that in one line.
                            "reason": f"reply draft ({draft_form(d.get('form'))})"})

    # WORST-FIRST INSIDE EACH VERB, then the cap. A cap that spends its five
    # chip slots on P2 threads while P1 threads sit unchipped is the same defect
    # as a body-open cap that draws P3 rows first: the selection decides what the
    # run is worth, and it is invisible once the cap has bitten. Enumeration
    # order (newest first) breaks the tie, so the sort stays stable.
    planned.sort(key=lambda m: CHIP_RANK.get(m.get("chip") or "", 9))

    # THE RECENCY WINDOW is the real bound now, not a count. A thread older than
    # `since_days` is out of scope — recorded, not silently dropped — unless the
    # window was lifted (`--all`, historic). This is what replaces the numeric
    # cap: the mailbox and its dates decide the size of a night, not a number.
    now = _utcnow()
    windowed: list[dict[str, Any]] = []
    for m in planned:
        if _within_window(m.get("received"), since_days, now):
            windowed.append(m)
        else:
            exclude(m["conversation_id"], m["verb"],
                    f"outside the {since_days}-day window (received "
                    f"{str(m.get('received'))[:10]}) — run historic/--all to "
                    "include it")
    planned = windowed

    # A mutation this checkpoint ALREADY carried out is dropped BEFORE the cap,
    # never counted through it twice. It still counts in `applied`, so the blast
    # radius is unchanged — what changes is that the remaining slots go to work
    # that has not happened yet. Measured on run 118: a resumed pass planned its
    # five chips, three of them already applied, and delivered nothing.
    if skip_keys:
        fresh = []
        for m in planned:
            if f"{m['conversation_id']}|{m['verb']}" in skip_keys:
                exclude(m["conversation_id"], m["verb"],
                        "already carried out this checkpoint — it counts against "
                        "the cap in `applied`, not as a slot to spend again")
            else:
                fresh.append(m)
        planned = fresh

    # Caps bite LAST, over the re-screened plan, and count what is already
    # applied this checkpoint.
    kept: list[dict[str, Any]] = []
    counts = {v: applied.get(v, 0) for v in MUTATION_VERBS}
    for m in planned:
        verb = m["verb"]
        # `caps[verb] is None` is UNLIMITED — the default. A number still bounds
        # a deliberately small hand-run.
        if caps[verb] is not None and counts[verb] >= caps[verb]:
            exclude(m["conversation_id"], verb,
                    f"per-run cap reached ({caps[verb]}, {applied.get(verb, 0)} "
                    "already applied this checkpoint)")
            continue
        counts[verb] += 1
        kept.append(m)

    return {"run_id": run_id, "mutations": kept, "excluded": excluded,
            "caps": caps, "applied_before": applied, "since_days": since_days,
            "plan_digest": plan_digest(kept, run_id),
            "planned_by_verb": {v: sum(1 for m in kept if m["verb"] == v)
                                for v in MUTATION_VERBS}}


# ---------------------------------------------------------------------------
# ONE FROZEN PLAN (review 2026-08-13, round 2, K1 — Codex CRITICAL + Claude)
# ---------------------------------------------------------------------------
# THREE INDEPENDENT PLANS USED TO EXIST. `cos_mutate.py plan` built one and
# wrote `plan.json`; `dry_run()` called `build_plan()` AGAIN and rehearsed
# whatever it got; `apply_pass()` called it a THIRD time and dispatched whatever
# THAT got. The validated artifact was never consumed by anything. Codex probed
# it end to end: a P1/add plan matched against a P3/remove rehearsal returned
# `ok: true`, because the rehearsal gate compares only `(verb,
# conversation_id)` — two rows can name the same thread and the same verb while
# carrying a different chip, a different mode, or entirely different DRAFT TEXT
# destined for the owner's real Drafts folder.
#
# The three builds PREDATE s08. What s08 added is a gate that now CLAIMS the
# rehearsal covers the plan — so the claim outran the mechanism, which is worse
# than no claim.
#
# THE FIX IS IDENTITY, NOT MORE MATCHING. One plan is built ONCE, digested over
# its whole payload, and written to disk; the rehearsal names that digest; the
# apply refuses to run unless the plan it reads still hashes to the digest the
# rehearsal named. A payload that changed after the rehearsal — one character of
# draft text, a chip swapped, a row added — no longer hashes the same and is
# REFUSED, not applied.
def plan_digest(mutations: list[dict[str, Any]], run_id: Any) -> str:
    """The identity of a plan: sha256 over its run id AND its WHOLE mutation list.

    Over the whole row, deliberately. `(verb, conversation_id)` is what the
    rehearsal gate matches on and is exactly what K1 showed to be insufficient:
    the draft lane's payload is free text a human may send verbatim, and two
    rows agreeing on verb and thread can disagree about every word of it.

    THE RUN ID IS INSIDE THE HASH, and `run_id` is a REQUIRED argument
    (review 2026-08-13, round 5, C1 — consensus CRITICAL). It used to be a
    sibling key of `plan_digest` in the written document, covered by nothing:
    both review lanes deleted it from an OLD run's plan and applied that plan
    under a NEW run whose UndoLedger is empty, so `done_keys` skipped nothing
    and every archive, chip and draft in it was dispatched a second time —
    around the lane lock, which is keyed on the run id and therefore hands a
    different lock to a different run. A digest that does not cover the run id
    is the identity of a PAYLOAD, and this artifact's job is the identity of a
    payload FOR A RUN. Deleting or editing the key now changes the hash, so
    `load_frozen_plan` refuses the document before anything reads its rows.

    No default. A default of `None` would let a caller silently reproduce the
    exact hole this closes, and there is no site in this file that legitimately
    digests a plan without knowing which run it belongs to.

    Canonical serialization (`sort_keys`, no whitespace) so that key order,
    indentation and a re-serialization round trip cannot change the identity of
    a plan that did not change.
    """
    return hashlib.sha256(json.dumps(
        {"run_id": run_id, "mutations": mutations}, sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def load_frozen_plan(path: Path) -> dict[str, Any]:
    """Read a written plan and REFUSE it unless it still hashes to its stamp.

    This is the whole point of the artifact: a plan that was edited, truncated,
    or replaced between the rehearsal and the apply is not the plan that was
    rehearsed, and the only safe reading of one is to dispatch nothing.

    The stamp covers `run_id` as well as `mutations` (C1), so a plan whose run
    id was DELETED to make it apply under another run no longer hashes to its
    own stamp and stops here, before `_frozen_todo`'s run check even runs.
    """
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MutationStop(
            f"the frozen plan at {path} could not be read ({exc}) — an "
            "unreadable plan is not an empty one, and nothing is dispatched "
            "against a plan this lane never saw") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("mutations"), list):
        raise MutationStop(
            f"the frozen plan at {path} carries no `mutations` list — the key "
            "it is read by is part of the contract, and a missing one used to "
            "read as an empty plan")
    stamped = doc.get("plan_digest")
    if not isinstance(stamped, str) or not stamped.strip():
        raise MutationStop(
            f"the frozen plan at {path} carries no `plan_digest`. An unstamped "
            "plan cannot be bound to the rehearsal that validated it, which is "
            "the whole reason this artifact is written to disk")
    actual = plan_digest(doc["mutations"], doc.get("run_id"))
    if actual != stamped:
        raise MutationStop(
            f"the frozen plan at {path} does not hash to its own stamp "
            f"(stamped {stamped[:16]}…, computed {actual[:16]}…) — its payload "
            "changed after it was written, so it is not the plan anything "
            "rehearsed and nothing is dispatched")
    return doc


def plan_binding_path(vault: Path, run_id: str) -> Path:
    """Where the apply records WHICH plan it dispatched, for the validator.

    HOST-PRIVATE, beside the run manifest and validity verdicts (review
    2026-08-13, round 6, H-forge). It used to live in `run_ops_dir` = the
    `cos-ops` directory, which `cos.run_ops_dir` itself documents as
    "VM-writable by nature" — and `check_plan_binding` read `source` straight
    out of it, so a VM (or a self-planned `apply_pass` rebuild) could write
    `{"source":"frozen"}` there and launder a clean K1 verdict, no digest, no
    plan, no rehearsal. A run-validity control cannot take an unauthenticated
    assertion from the VM-writable ops zone. `runs_dir` is the same location
    the manifest the validator already trusts lives in, and it IS reachable by
    `cos_runverify` (it reads the manifest and the validity verdict from there
    on every pass).

    Round 6 said that location was "never VM-writable" and it was not: until
    gap-05 (2026-08-16) `runs_dir` resolved to `<vault>/.brain/cos/host/runs`,
    host-private by CONTRACT (AGENTS.md §9) and inside the VirtioFS workspace
    by FILESYSTEM. It now resolves through `config.cos_run_records_dir`, off
    the mount beside the writer lock and the supersede journal, so the sentence
    the whole control rests on is finally true.

    The NAME lives in `cos.run_plan_binding_path` (round 7) — writer and reader
    held the same literal twice, so the round-6 move had to be made in two
    files. This wrapper adds only the directory the writer needs.
    """
    from brain import cos                                        # noqa: PLC0415

    p = cos.run_plan_binding_path(vault, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


#: Permission for the artifacts this tool writes under `<vault>/cos-ops` and
#: into the run's evidence directory. `cos._write_atomic` defaults to 0o600
#: (host-private), which these were NOT before round 7 — they were plain
#: umask-derived 0o644 — and a hardening pass that also silently narrows a
#: permission is two changes wearing one commit. 0o644 keeps them exactly as
#: they are today; the plan binding, which really is host-private, takes the
#: helper's own default.
_OPS_MODE = 0o644


def _write_text_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Publish `text` at `path` atomically and never through a symlink.

    EVERY write in this file goes through here (review 2026-08-13, round 7).
    `tests/test_cos_pathguard.py::test_no_raw_write_remains_on_a_mount_path`
    scans this module now, for the reason it already scanned `cos.py`: most of
    these paths are under `<vault>/cos-ops`, which is VM-writable by nature, so
    a plain `write_text` at a predictable name FOLLOWS a symlink an attacker
    pre-created there and the host truncates whatever it points at. The shapes
    file, the dry-run ledger and the canary all sat at exactly such names.
    """
    from brain import cos                                        # noqa: PLC0415

    cos._write_atomic(path, text.encode("utf-8"),
                      **({} if mode is None else {"mode": mode}))


def mutation_lane_lock_path(vault: Path, run_id: str) -> Path:
    """Where the apply's exclusive lock lives — HOST-PRIVATE, off the mount.

    `config.host_lock_dir` is the same app-data directory the COS ledger
    appends and the index writer already lock in, and for the same reason
    (INT-05): a lock file on the VM-writable mount can be unlinked under a live
    holder and replaced at the same name, after which the next holder flocks a
    DIFFERENT inode and both run. Not being reachable is the only fix.

    Keyed on VAULT + RUN, so two different runs never wait on each other.

    THE PHYSICAL VAULT, NOT ITS SPELLING (review 2026-08-13, round 5, C-lock).
    `os.path.abspath` normalises `..` and the cwd and stops there: it never
    resolves a symlink, so the SAME vault reached through a link and through
    its real path produced two different key digests, two different lock files,
    and two applies of one run dispatching side by side — the exact double
    dispatch this lock exists to stop, defeated by how the path was typed.
    `Path.resolve()` answers with the physical path in both spellings.
    """
    from brain import config                                      # noqa: PLC0415

    key = hashlib.sha256(
        f"cos-apply|{Path(vault).resolve()}|{run_id}".encode("utf-8")
    ).hexdigest()[:16]
    return config.host_lock_dir(create=True) / f"{key}.lock"


def _mutation_lane_lock(vault: Path, run_id: str) -> Any:
    """The exclusive lock the APPLY holds for its WHOLE pass.

    Reused, not re-implemented: `brain.lock.writer_lock` is the same portable
    flock primitive the index writer already uses — kernel-released on
    crash/SIGKILL, so there is no stale-pidfile heuristic to get wrong.

    The digest binding above stops an apply consuming a payload nothing
    rehearsed; this stops TWO applies consuming the same rehearsed one at once.
    They are different failures: one dispatches the wrong thing, the other
    dispatches the right thing twice, and `done_keys` only skips a row whose
    undo entry is already on disk — which a concurrent pass has not written yet.
    """
    from brain import lock                                        # noqa: PLC0415

    return lock.writer_lock(mutation_lane_lock_path(vault, run_id),
                            verb=f"cos-apply {run_id}")


# ---------------------------------------------------------------------------
# the undo ledger and the state machine
# ---------------------------------------------------------------------------
class UndoLedger:
    """Append-only, one row per state TRANSITION, latest row per key wins.

    Append-only because the interesting question after a stop is not "what is
    the state" but "what happened, in order" — and a rewritten row cannot answer
    it. `conversation_id` + verb is the idempotency key, per v4.7: OWA ItemIds
    change when an item moves folders, so a move-time id is a session handle and
    never an identity.
    """

    def __init__(self, vault: Path, run_id: str) -> None:
        from brain import cos                                    # noqa: PLC0415
        self.path = cos.run_ops_dir(vault) / f"_cos_undo_ledger_{run_id}.jsonl"
        self.run_id = run_id
        #: post-dispatch rows whose content this ledger refused. Non-empty means
        #: the run must stop AFTER the row is on disk — `refused_ledger_keys` in
        #: the run facts is this list's length.
        self.refused_rows: list[str] = []

    def rows(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.path)

    def latest(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            out[row["idempotency_key"]] = row
        return out

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("state") not in STATES:
            raise MutationStop(f"{row.get('state')!r} is not a state this "
                               f"machine has ({', '.join(STATES)})")
        row = dict(row, run=self.run_id, ts=_ts())
        # --- the closed field set, enforced ON THE SERIALIZED ROW (D14/11) ---
        # THE FAILURE POSTURE DIFFERS BY POSITION, and that is deliberate:
        # never lose the record of a dispatched mutation. A write-ahead
        # `intent` row precedes its bridge call, so a violation there is a
        # programming error with nothing yet on the server — stop before
        # anything dispatches. On a POST-DISPATCH row the mutation has already
        # happened, so the row is written first with the offending content
        # replaced by a marker, and the run stops afterwards. A refused key is
        # never a silently written key.
        write_ahead = row.get("state") == "intent"
        unknown = sorted(set(row) - LEDGER_ROW_KEYS)
        bad_receipts = not receipts_shape_ok(row.get("receipts"), row)
        refusal = ""
        if unknown or bad_receipts:
            refusal = ((f"the undo ledger refuses key(s) {unknown}"
                        if unknown else "")
                       + ("; " if unknown and bad_receipts else "")
                       + ("the `receipts` value is not the page's shape"
                          if bad_receipts else ""))
            if write_ahead:
                raise MutationStop(
                    f"{refusal} — nothing has dispatched, so this row is "
                    "refused rather than written")
            self.refused_rows.append(refusal)
            row = {k: v for k, v in row.items() if k in LEDGER_ROW_KEYS}
            if bad_receipts:
                row["receipts"] = {"refused": "shape"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # NOT `path.open("a")` — that FOLLOWS a symlink at the final name, and
        # this ledger lives under `<vault>/cos-ops`, which is VM-writable by
        # nature (review 2026-08-13, round 7; the identical defect in
        # `cos._append_jsonl` was R5-1). `cos._open_append_nofollow` is the
        # engine's one sanctioned append: it creates EXCLUSIVELY when absent
        # and, when present, refuses a symlink and confirms the same regular
        # inode on the fd — no check-then-open window either way.
        # It is NOT `cos.append_jsonl` either: that helper does not fsync, and
        # this ledger is a WRITE-AHEAD record written before the bridge call it
        # describes. A lost intent row is a mutation on the server with nothing
        # on disk saying who made it — the run-106 shape. Its per-ledger lock is
        # not needed here: the apply holds the mutation-lane lock, so this file
        # has exactly one writer.
        from brain import cos                                    # noqa: PLC0415

        data = (json.dumps(row, ensure_ascii=False, sort_keys=True)
                + "\n").encode("utf-8")
        fd = cos._open_append_nofollow(self.path)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(f"append made no progress on {self.path.name}")
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if refusal:
            # THE ROW IS ON DISK. Only now does the run stop: the mutation this
            # row describes already reached the server, and losing its record is
            # the run-106 shape.
            raise MutationStop(
                f"{refusal} — the row was written with the offending content "
                "replaced by a marker, and this run stops here")
        return row

    def applied_counts(self) -> dict[str, int]:
        """What counts against the cap: anything that MIGHT have reached the
        server. `sent` counts — a mutation whose outcome is unknown has already
        spent its blast radius."""
        return applied_counts(self.rows())

    def unfinished(self) -> list[dict[str, Any]]:
        return [r for r in self.latest().values() if r["state"] not in TERMINAL]


# `applied_counts` is imported at the top of this file — see the note there.


def dispatched_counters(vault: Path, run_id: str) -> dict[str, int]:
    """This run's four mutation counters, RECOUNTED from its undo ledger.

    `captured` is not recounted and not returned — see `VERB_COUNTER`.
    """
    return {VERB_COUNTER[v]: n
            for v, n in applied_counts(UndoLedger(vault, run_id).rows()).items()
            if v in VERB_COUNTER}


def record_mutation_counters(vault: Path, run_id: str) -> dict[str, Any]:
    """Write what this apply DISPATCHED into the run's metrics row.

    WHY THIS EXISTS (measured, run 145, 2026-08-16). `cos_driver` appends the
    metrics row hours before the apply, with `archived/marked/drafts_created/
    captured` at 0 and `mutation_lane: "none-read-only"` — which is TRUE of the
    read lane and stays on the record forever, because nothing ever updated it.
    Run 145 archived 11 threads, chipped 3 and drafted 2, and its row of record
    still read all-zero. That is not only a wrong number: `mutation_counts()` is
    what `unledgered_mutations` and `check_plan_binding` corroborate a MISSING
    artifact against, so an all-zero row disarmed both — the anti-vacuity guards
    concluded the run had done nothing, on a night that applied 16 mutations.

    APPEND, NEVER EDIT (REP-01/REP-02, E29(c)). The row is a copy of the driver's
    own row with the counters and the lane corrected, declaring
    `supersedes_run_ts`; `append_metric` refuses anything else, re-verifies the
    ingestion recount and re-joins the host stamps, so this cannot smuggle a
    changed counter past them. Re-running it is a no-op ("unchanged").

    It writes NOTHING when the run dispatched nothing: a read-only night's row is
    already correct, and appending a superseding duplicate would make every
    quiet night look like a corrected rerun.
    """
    from brain import cos                                        # noqa: PLC0415

    ops = cos.run_ops_dir(vault)
    counters = dispatched_counters(vault, run_id)
    out: dict[str, Any] = {"counters": counters, "appended": "not-attempted"}
    recon = _reconcile_module()
    if recon is None:
        out["appended"] = "no-checkers"
        return out
    rows = [r for r in recon._rows(ops / "_cos_metrics.jsonl")
            if (r.get("date"), _run_suffix(str(r.get("run"))))
            == (run_id[:10], _run_suffix(run_id))]
    if not rows:
        out["appended"] = "no-driver-row"
        return out
    prior = rows[-1]
    lane = MUTATION_LANE if any(counters.values()) else prior.get("mutation_lane")
    row = {**prior, **counters, "mutation_lane": lane,
           "run_ts": _ts(), recon.SUPERSEDES: str(prior.get("run_ts"))}
    if all(int(prior.get(k) or 0) == v for k, v in counters.items()) \
            and prior.get("mutation_lane") == lane:
        out["appended"] = "unchanged"                # already correct; no-op
        return out
    out["appended"] = recon.append_metric(ops, row)
    out["supersedes"] = row[recon.SUPERSEDES]
    return out


def _run_suffix(value: str) -> str:
    m = re.search(r"run(\d+)$", str(value)) or re.search(r"^(\d+)$", str(value))
    return m.group(1) if m else str(value)


def _reconcile_module() -> Any:
    try:
        import cos_reconcile_metrics as recon                   # noqa: PLC0415
    except ImportError:
        return None
    return recon


def _undo_row(m: dict[str, Any], resolved: dict[str, Any], *, state: str,
              run_id: str, account: str, **extra: Any) -> dict[str, Any]:
    """The E17 field set, every field present, `null` a recorded value.

    `key_scheme: message-id` with the provider-immutable `InternetMessageId` is
    what E17 requires on the rest lane; `thread_id` (the conversation id) is
    what the UNDO actually keys on, per v4.7. Both are recorded because they
    answer different questions, and a rest-lane row carrying a `convid` key
    scheme would be an E17 mismatch.
    """
    imid = resolved.get("internet_message_id")
    row = {
        "idempotency_key": f"{m['conversation_id']}|{m['verb']}",
        "conversation_id": m["conversation_id"],
        "conversation_id_digest": short(m["conversation_id"]),
        "verb": m["verb"],
        "state": state,
        "reason": m.get("reason"),
        "account": account,
        "message_id": imid,
        "key_scheme": "message-id" if imid else "convid",
        "thread_id": m["conversation_id"],
        "mutation_lane": MUTATION_LANE,
        "original_folder": "Inbox",
        "destination_folder": ({"archive": "archive", "draft": DRAFT_FOLDER}
                               .get(m["verb"], "Inbox")),
        "action_ts": _ts(),
        "primitive": PRIMITIVE[m["verb"]],
        "connector_result": None,
        "verification": None,
        # WHICH CHIP, on the row. Reconciliation asks "is this chip on the
        # thread"; without the name it asked about `undefined` and answered NO —
        # so run 118 recorded `aborted-not-applied` for a chip the mailbox was
        # carrying. An undo needs the name too: removing "the chip" is not an
        # instruction anything can follow.
        "chip": m.get("chip"),
        "mode": m.get("mode"),
        "before_image": resolved.get("before_categories"),
        "item_id_at_resolve": resolved.get("item_id"),
        "changekey_refetched_at": resolved.get("changekey_refetched_at"),
        "run": run_id,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# the bridge: one operation per evaluation
# ---------------------------------------------------------------------------
IN_ID = "__cos_min"
OUT_ID = "__cos_mout"
SRC_ID = "__cos_msrc"


class Bridge:
    """Drive `cos_mutate_page.js` through the two hidden DOM nodes.

    Same transport discipline as the read driver — chunked writes, base64 reads,
    length-checked — because the same three silent transport bugs apply (a
    mangled non-ASCII eval, Trusted Types voiding a `<script>` write, and a
    surrogate pair split by a fixed-width slice).
    """

    def __init__(self, tab: drv.ChromeTab, *, poll_seconds: float = 1.5,
                 max_wait: float = 180.0) -> None:
        self.tab = tab
        self.seq = 0
        self.poll_seconds = poll_seconds
        self.max_wait = max_wait

    def stage(self) -> str:
        return drv.stage(self.tab, PAGE_JS, SRC_ID)

    def call(self, op: str, args: dict[str, Any] | None = None,
             max_wait: float | None = None) -> dict[str, Any]:
        self.seq += 1
        payload = json.dumps({"seq": self.seq, "op": op, "args": args or {}},
                             ensure_ascii=False)
        self.tab.js(drv._fresh_node(IN_ID))
        for off in range(0, len(payload), drv.CHUNK):
            self.tab.js(
                f"(function(){{document.getElementById({json.dumps(IN_ID)})"
                f".textContent+={json.dumps(payload[off:off + drv.CHUNK])};"
                f"return 'chunk';}})()")
        deadline = time.time() + (max_wait or self.max_wait)
        while time.time() < deadline:
            time.sleep(self.poll_seconds)
            st = self.tab.json(
                f"(function(){{var e=document.getElementById({json.dumps(OUT_ID)});"
                "var s=e?JSON.parse(e.textContent):{};"
                "return JSON.stringify({done:!!s.done,seq:s.seq||0,"
                "phase:s.phase||null,error:s.error||null,"
                "canary449:!!s.canary449,auth401:!!s.auth401});})()")
            if st.get("seq") == self.seq and st.get("done"):
                out = drv._read_out(self.tab, OUT_ID)
                if out.get("error"):
                    err = MutationStop(f"the in-page mutation driver failed in "
                                       f"{out.get('phase')!r}: {out['error']}")
                    err.canary449 = bool(out.get("canary449"))       # type: ignore[attr-defined]
                    err.auth401 = bool(out.get("auth401"))           # type: ignore[attr-defined]
                    err.mutation_in_flight = bool(out.get("mutation_in_flight"))  # type: ignore[attr-defined]
                    err.runtime = out.get("runtime")                 # type: ignore[attr-defined]
                    raise err
                return out
        raise MutationStop(f"the in-page mutation driver did not finish {op!r} "
                           f"within {max_wait or self.max_wait:.0f}s")


#: Where the staged hook source and its mirrored `stats()` live. Two more inert
#: divs on the same two-world bridge the driver already uses.
HOOK_SRC_ID = "__cos_hook_src"
HOOK_STAT_ID = "__cos_hookstat"

#: What a hook installed in the HOST's own world looks like from the host: it
#: reports `installed`, it answers `stats()`, and it captures nothing but its own
#: traffic. Measured 2026-08-10 on the live tab — `has_cosCap: true`,
#: `page_globals_seen: []`. So the host asserts its own BLINDNESS: if this
#: process can see `window.__cosCap`, the hook is in the isolated world.
WRONG_WORLD = (
    "the capture hook is in the host's ISOLATED world, not the page's MAIN "
    "world — `ChromeTab.js` (osascript) evaluates there, and a hook installed "
    "that way reports itself installed, answers stats(), and captures NOTHING "
    "of the app's traffic. Evaluate the staged bootstrap line through a "
    "MAIN-world surface (the browser extension) instead.")


def stage_hook(tab_id: int) -> dict[str, Any]:
    """Stage `cos_capture_hook.js` and return the ONE line to evaluate in MAIN.

    It deliberately does NOT evaluate it: this process cannot reach the page's
    world, and an install it performs itself is the silent failure above. The
    returned line also mirrors `stats()` into `#__cos_hookstat`, which is how
    the host reads a buffer it can never see directly.
    """
    tab = drv.ChromeTab(tab_id)
    drv.stage(tab, HOOK_JS, HOOK_SRC_ID)
    tab.js(drv._fresh_node(HOOK_STAT_ID))
    line = (f"(function(){{var r={drv.bootstrap_for(HOOK_SRC_ID)};"
            f"document.getElementById({json.dumps(HOOK_STAT_ID)}).textContent="
            f"JSON.stringify(window.__cosCap.stats());return r;}})()")
    return {"bootstrap": line, "source_node": HOOK_SRC_ID,
            "stats_node": HOOK_STAT_ID,
            "evaluate_this_in": "the page's MAIN world (browser extension)"}


def verify_capture_world(tab_id: int, *, require_boot: bool = True) -> dict[str, Any]:
    """Refuse to trust a capture buffer until both halves check out.

    Two-sided, because either side alone lies. The host asserting it CANNOT see
    the hook proves the world; the mirrored `stats()` proves the install caught
    BOOT rather than arriving after it.
    """
    tab = drv.ChromeTab(tab_id)
    if tab.js("String(typeof window.__cosCap)") != "undefined":
        raise MutationStop(WRONG_WORLD)
    raw = tab.js(f"(function(){{var e=document.getElementById("
                 f"{json.dumps(HOOK_STAT_ID)});return e?e.textContent:'';}})()")
    if not raw.strip():
        raise MutationStop(
            f"no hook stats at #{HOOK_STAT_ID} — the staged bootstrap line was "
            "never evaluated in the page's MAIN world. `stage_hook` prints it.")
    stats = json.loads(raw)
    # THE GATE IS THE SEED, NOT THE CLOCK. `document_start` was the gate until
    # 2026-08-11, when a hook installed at readyState `complete` captured a
    # `FindItem` with its `authorization` intact on the live tab. s03's "boot
    # only" reading held for a SETTLED tab; a tab still settling, or a list
    # gesture, can fire one later. So the timing is reported and the ENVELOPE
    # is what decides — a gate that refuses a usable seed is as wrong as one
    # that accepts a missing one.
    if require_boot and not stats.get("boot_finditem"):
        raise MutationStop(
            "the hook caught no `FindItem` envelope, so there is nothing to "
            "replay — every mutation resolves its item through that seed. "
            f"(installed at readyState {stats.get('installed_at_readystate')!r}; "
            "install at document_start, or re-load the tab with the hook "
            "staged, and let the list settle.)")
    return {"world": "main", "host_is_blind_to_the_buffer": True, **stats}


class CdpBridge:
    """The same page driver, driven over CDP instead of AppleScript.

    WHY A SECOND TRANSPORT. `Bridge` talks to the tab through `osascript`, which
    (a) evaluates in an ISOLATED world, so the page driver has to be booted by
    some other main-world surface, and (b) addresses "Google Chrome" by name —
    ambiguous the moment a second Chrome is running, which is exactly the setup
    the capture needs. CDP evaluates in the MAIN world by default and addresses
    one browser by port, so both problems disappear and the nightly can run this
    unattended. The PAGE PROTOCOL is unchanged: same two nodes, same ops, same
    validator — only the wire is different.
    """

    def __init__(self, port: int = 9222, max_wait: float = 180.0) -> None:
        self.port = port
        self.max_wait = max_wait
        self.seq = 0

    def _eval(self, expression: str) -> Any:
        import cos_cdp_capture as cdp                          # noqa: PLC0415
        return cdp.evaluate(expression, port=self.port)

    def stage(self) -> str:
        booted = self._eval(PAGE_JS.read_text(encoding="utf-8"))
        if "cos-mutate-page-loaded" not in str(booted):
            raise MutationStop(f"the page driver did not boot over CDP: {booted!r}")
        return str(booted)

    def call(self, op: str, args: dict[str, Any] | None = None,
             max_wait: float | None = None) -> dict[str, Any]:
        """One round trip, awaited INSIDE the page.

        The poll runs in the page rather than here, so a slow mutation is one
        awaited evaluate instead of a stream of transport calls — the pattern
        that wedged Chrome's evaluation bridge on run 112.
        """
        self.seq += 1
        payload = json.dumps({"seq": self.seq, "op": op, "args": args or {}},
                             ensure_ascii=False)
        wait_ms = int((max_wait or self.max_wait) * 1000)
        expr = f"""(async function(){{
          var IN={json.dumps(IN_ID)}, OUT={json.dumps(OUT_ID)};
          var e=document.getElementById(IN);
          if(!e){{e=document.createElement('div');e.hidden=true;e.id=IN;
                  document.documentElement.appendChild(e);}}
          e.textContent={json.dumps(payload)};
          var deadline=Date.now()+{wait_ms};
          while(Date.now()<deadline){{
            await new Promise(function(r){{setTimeout(r,300);}});
            var o=document.getElementById(OUT);
            if(!o) continue;
            var st;
            try {{ st=JSON.parse(o.textContent); }} catch(err) {{ continue; }}
            if(st.seq==={self.seq} && st.done) return JSON.stringify(st);
          }}
          return JSON.stringify({{timeout:true,seq:{self.seq}}});
        }})()"""
        out = json.loads(self._eval(expr))
        if out.get("timeout"):
            raise MutationStop(f"the in-page mutation driver did not finish {op!r} "
                               f"within {wait_ms / 1000:.0f}s")
        if out.get("error"):
            err = MutationStop(f"the in-page mutation driver failed in "
                               f"{out.get('phase')!r}: {out['error']}")
            err.canary449 = bool(out.get("canary449"))           # type: ignore[attr-defined]
            err.auth401 = bool(out.get("auth401"))               # type: ignore[attr-defined]
            err.mutation_in_flight = bool(out.get("mutation_in_flight"))  # type: ignore[attr-defined]
            raise err
        return out


def load_shapes(vault: Path) -> dict[str, Any]:
    """The APPROVED captured request shapes — structure and constants only.

    They come from a file the host holds, not from the live capture buffer:
    what may be replayed is a decision, and a decision read fresh off the page
    on every run is not a decision at all. `capture-shapes` is how the file is
    written, once, from an action the OWNER performed in the UI.
    """
    from brain import cos                                        # noqa: PLC0415
    path = cos.run_ops_dir(vault) / "_cos_mutation_shapes.json"
    if not path.exists():
        return {"shapes": {}, "path": str(path), "missing": True}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {"shapes": doc.get("shapes") or {}, "path": str(path),
            "captured_at": doc.get("captured_at"), "missing": False}


def _merge_shapes(existing: dict[str, Any] | None,
                  got: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a fresh capture into the stored shapes PER SUB-KEY, not per entry.

    One entry can now carry two captured variants of the same job — the chip's
    add (`skeleton`/`fingerprint`) and its remove (`skeleton_remove`/
    `fingerprint_remove`), same verb, same Action, two payloads the server
    accepted separately (FINDING 2026-08-12). A whole-entry `dict.update` would
    therefore DELETE the variant the newer capture happens not to contain: import
    a remove-only capture and the chip lane's add shape is gone, with nothing in
    the output saying so. Sub-key merge keeps both; a variant the new capture DOES
    carry still wins.
    """
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in (existing or {}).items()}
    for key, shape in (got or {}).items():
        kept = out.get(key)
        if isinstance(kept, dict) and isinstance(shape, dict):
            kept.update(shape)
        else:
            out[key] = shape
    return out


def _fingerprints(shapes: dict[str, Any]) -> dict[str, Any]:
    """What was stored, per job AND per variant — so an import that landed only
    the remove half is visible as such instead of reading like a no-op."""
    out: dict[str, Any] = {}
    for key, shape in shapes.items():
        out[key] = shape.get("fingerprint")
        if shape.get("fingerprint_remove"):
            out[key + " (remove)"] = shape["fingerprint_remove"]
    return out


# ---------------------------------------------------------------------------
# the passes
# ---------------------------------------------------------------------------
def _init_page(bridge: Bridge, shapes: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Stage the page driver, then REQUIRE that something else booted it in MAIN.

    It used to boot the driver itself with `tab.js`, which is the isolated world
    — the same silent failure as `WRONG_WORLD`, one layer up: the driver would
    load, answer the bridge, and be refused 401 on every request because the
    captured envelope lives in the other world.
    """
    bridge.stage()
    if bridge.tab.js("String(typeof window.__cosMut)") != "undefined":
        raise MutationStop(
            "the page driver is in the host's ISOLATED world. " + WRONG_WORLD)
    if bridge.tab.js(f"String(!!document.getElementById({json.dumps(OUT_ID)}))") != "true":
        raise MutationStop(
            "the page driver has not booted in the tab's MAIN world. Evaluate "
            f"this line there (browser extension), then retry:\n"
            f"  {drv.bootstrap_for(SRC_ID)}")
    return bridge.call("init", {"shapes": shapes, "signature": f"[cos:{run_id}:"})


def _bridge_for(tab_id: int | None, shapes: dict[str, Any], run_id: str,
                *, use_cdp: bool) -> Any:
    """CDP or AppleScript, one place, so no pass can pick a different one."""
    if use_cdp:
        bridge = CdpBridge()
        bridge.stage()
        bridge.call("init", {"shapes": shapes, "signature": f"[cos:{run_id}:"})
        return bridge
    bridge = Bridge(drv.ChromeTab(tab_id))
    _init_page(bridge, shapes, run_id)
    return bridge


def dry_run(vault: Path, run_id: str, tab_id: int, *,
            caps: dict[str, int] | None = None,
            since_days: int | None = DEFAULT_SINCE_DAYS,
            plan_path: Path | None = None,
            use_cdp: bool = False) -> dict[str, Any]:
    """The whole pass, READ-ONLY, stopping one line before `fetch`.

    Everything the live pass does except the dispatch: the same resolve, the
    same freshly re-fetched ChangeKey, the same builder, the same allowlist —
    and then the payload is printed instead of sent. The undo rows are written
    to a DRY-RUN ledger and never to the real one: a real undo row claiming a
    move that never happened is precisely the hazard the 449 rule exists to
    prevent, and a rehearsal must not be able to create one.

    `plan_path` REHEARSES A FROZEN PLAN (K1). Without it this built its own
    plan, so what it rehearsed was only coincidentally what the apply would
    dispatch. With it, the artifact on disk is the one thing rehearsed, and the
    digest it was written under is carried out to the report so the apply can
    refuse anything else.
    """
    root = assert_vault(vault)
    shapes = load_shapes(vault)
    ledger = UndoLedger(vault, run_id)
    plan = (load_frozen_plan(plan_path) if plan_path is not None
            else build_plan(vault, run_id, caps=caps, since_days=since_days,
                            applied=ledger.applied_counts()))
    bridge = _bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp)
    init = {"transport": "cdp" if use_cdp else "applescript"}

    out = []
    for m in plan["mutations"]:
        # A REHEARSAL REPORTS, IT DOES NOT ABORT. One lane whose approved shape
        # cannot be built (measured 2026-08-11: the captured CreateItem names no
        # SavedItemFolderId, and v4.7 forbids filling one in) used to take the
        # whole dry run with it — so the owner saw an error instead of the other
        # lanes' payloads, which is the opposite of what a dry run is for.
        try:
            out.append(bridge.call("dry", {"mutation": m})["out"])
        except MutationStop as exc:
            out.append({"verb": m["verb"],
                        "conversation_id": short(m["conversation_id"]),
                        "blocked": str(exc), "would_dispatch": False,
                        "dispatched": False})
    dry_ledger = ledger.path.with_name(ledger.path.name.replace(
        "_cos_undo_ledger_", "_cos_undo_DRYRUN_"))
    _write_text_atomic(dry_ledger, "".join(
        json.dumps(_undo_row(m, r.get("resolved") or {}, state="intent",
                             run_id=run_id, account=str(root["BRAIN_VAULT"]),
                             dry_run=True), sort_keys=True) + "\n"
        for m, r in zip(plan["mutations"], out)), mode=_OPS_MODE)
    return {"vault_root_asserted": root, "shapes": {k: v.get("fingerprint")
                                                    for k, v in shapes["shapes"].items()},
            "shapes_path": shapes["path"],
            "capture": (init.get("out") or {}).get("capture"),
            # THE ONE FIELD THE APPLY IS BOUND BY. Top-level rather than only
            # inside `plan`, because the rehearsal gate and the apply both read
            # this file for exactly this and nothing else about the plan.
            "plan_digest": plan.get("plan_digest"),
            "rehearsed_frozen_plan": str(plan_path) if plan_path else None,
            "plan": plan, "dry": out, "dry_run_ledger": str(dry_ledger),
            "e17": canary_status(vault), "kill_switch": kill_switch(vault),
            "dispatched": 0}


#: `dry_run` writes the raw conversation id on the rehearsed path (`dryOne`
#: returns `m.conversation_id`) and the 16-hex DIGEST on the blocked path
#: (`short(...)` in the `except`). One key, either spelling — and a digest is
#: what it looks like.
_DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")


def _rehearsal_key(verb: Any, conversation_id: Any) -> tuple[str, str]:
    cid = str(conversation_id or "")
    return (str(verb or ""), cid if _DIGEST_RE.match(cid) else short(cid))


#: What `dryOne` writes on the ONE path that can reach `would_dispatch: true`
#: (`tools/cos_mutate_page.js`, `if (!p.body) return out;` … `out.would_dispatch
#: = !reason`), as `(key, predicate, what it proves)`. Read off the producer,
#: and pinned against it by `test_the_dispatchable_row_fixture_matches_dryOne`,
#: which RUNS the shipped page module and compares its real row against this.
_DISPATCH_EVIDENCE: tuple[tuple[str, Any, str], ...] = (
    ("action", lambda v: isinstance(v, str) and v.strip(),
     "the request `prepare()` built"),
    ("allowlist", lambda v: v == "ACCEPTED",
     "the allowlist's ruling, which is ACCEPTED exactly when would_dispatch is "
     "true"),
    ("fingerprint", lambda v: isinstance(v, str) and v.strip(),
     "the fingerprint of the payload that was built"),
    ("payload_without_header", lambda v: isinstance(v, dict) and v,
     "the payload itself, minus the bearer envelope"),
)


def _dispatch_evidence_problems(i: int, r: dict[str, Any]) -> list[str]:
    """A row that CLAIMS it would dispatch, without the evidence dry validation
    ran (review 2026-08-13, round 4, Codex HIGH).

    `{"verb": "archive", "conversation_id": "c1", "would_dispatch": true,
    "dispatched": false}` matched a plan one-to-one and certified the night —
    no payload was built, no allowlist ruled, nothing was fingerprinted. It was
    probed `ok: true`. `would_dispatch` is only REACHABLE in the producer after
    all three, so a row claiming it without them did not come from the producer.
    """
    out: list[str] = []
    for key, ok, proves in _DISPATCH_EVIDENCE:
        if key not in r:
            out.append(
                f"rehearsal row {i} claims would_dispatch=True but carries no "
                f"{key!r} — {proves}. `dryOne` writes it on the same path that "
                "sets would_dispatch, so a row without it never ran the dry "
                "validation it is claiming the verdict of")
        elif not ok(r[key]):
            out.append(
                f"rehearsal row {i} claims would_dispatch=True and carries "
                f"{key}={r[key]!r}, which is not {proves}")
    return out


def _row_problems(rows: list[dict[str, Any]], *, kind: str) -> list[str]:
    """Every row that is not a row this gate may reason about, named.

    THE MATCH USED TO BE THE ONLY CHECK (review 2026-08-13, round 3, Codex
    HIGH). `sum(1 for r in dry_rows if r.get("would_dispatch"))` is a TRUTHINESS
    test, so a row carrying the STRING `"false"` counted as dispatchable; and a
    plan row of `{}` normalized to the key `("", "")` and matched a rehearsal
    row that was equally empty, so two pieces of garbage certified each other.
    Both were probed `ok: true`.

    So the shape is decided BEFORE the match, and a malformed row is neither a
    quiet night nor a blocked lane — it is an artifact this gate cannot read,
    which is the `rc 2` path.

    `would_dispatch` may be ABSENT, and that is production, not laxity:
    `dryOne` omits it on the skip path (`if (!p.body) return out;`) and only
    ever writes a real boolean otherwise, while `dry_run`'s `except
    MutationStop` branch writes `False` explicitly. Absent means "this row
    would not dispatch". An explicit `null` is NOT that absence (round 4): a
    key the producer wrote and left empty is a producer that answered nothing,
    which is malformed. A STRING there is neither, and is refused.

    Keys are checked by VALUE, not by whitelist: the page adds fields to a dry
    row as it learns to report more (`fingerprint`, `allowlist`,
    `payload_without_header`), and a key whitelist would turn every such
    addition into a stopped night for no safety gain.

    BUT A CLAIM OF `would_dispatch: True` CARRIES ITS EVIDENCE (review
    2026-08-13, round 4, Codex HIGH). A four-field row —
    `{verb, conversation_id, would_dispatch: true, dispatched: false}` — passed
    every check above and certified the night, while lacking every artifact
    that proves dry validation actually ran. It was probed `ok: true`. `dryOne`
    reaches `would_dispatch` only after `prepare()` returned a body and
    `validate()` ruled on it, and it writes the ruling out on the same three
    lines: `allowlist` (`"ACCEPTED"` exactly when `would_dispatch` is true),
    `fingerprint` (of the built payload) and `payload_without_header` (the
    payload itself, minus the bearer envelope). A row claiming the verdict
    without them did not come from that path. This is CONDITIONAL — nothing is
    required of a row that does not claim it would dispatch — so the skip path,
    the `MutationStop` path and every rejected row are untouched.
    """
    problems: list[str] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            problems.append(f"{kind} row {i} is a {type(r).__name__}, not an object")
            continue
        verb = r.get("verb")
        if not isinstance(verb, str) or verb not in MUTATION_VERBS:
            problems.append(
                f"{kind} row {i} carries verb {verb!r}, which is not one of "
                f"{list(MUTATION_VERBS)} — a row naming no verb matches "
                "whatever else names none")
        cid = r.get("conversation_id")
        if not isinstance(cid, str) or not cid.strip():
            problems.append(
                f"{kind} row {i} carries conversation_id {cid!r} — a row "
                "naming no thread is not a row about any thread")
        if kind != "rehearsal":
            continue
        has_wd = "would_dispatch" in r
        wd = r.get("would_dispatch")
        if has_wd and not (wd is True or wd is False):
            problems.append(
                f"rehearsal row {i} carries would_dispatch {wd!r} "
                f"({type(wd).__name__}), which is not a boolean — the string "
                '"false" is TRUTHY and used to count as dispatchable; an '
                "explicit null is a producer that wrote the key and answered "
                "nothing, which is not the ABSENCE `dryOne` leaves on its skip "
                "path")
        elif wd is True:
            problems += _dispatch_evidence_problems(i, r)
        if r.get("dispatched") is not False:
            problems.append(
                f"rehearsal row {i} reports dispatched={r.get('dispatched')!r}; "
                "a rehearsal dispatches nothing and both production paths say "
                "so with an explicit False")
    return problems


def rehearsal_verdict(plan_mutations: list[dict[str, Any]],
                      dry_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Did this rehearsal rehearse THIS plan, and can anything act?

    THE GATE USED TO FAIL OPEN (review 2026-08-13, round 2). The nightly read
    `dry-run.json` inside a command substitution under `set -u` only, so a
    parser failure did not stop the run and a MISSING `dry` key became an empty
    plan: rename the production key, or truncate the file, and `0 of N` read as
    `0 of 0` — a quiet night — and the run walked into REAL MUTATIONS with no
    valid rehearsal behind it. The test that blessed it fed `{"dry": []}` and
    asserted `reached the apply`, which is the same empty-input all-clear.

    So the plan and the rehearsal are matched ONE-TO-ONE on
    `(conversation_id, verb)` BEFORE `0 of 0` is told apart from `0 of N`. A
    rehearsal that does not cover the plan is not a quiet night and is not a
    blocked lane — it is a rehearsal that did not happen, and the only safe
    reading of one is to dispatch nothing.

    And BEFORE either, both sides are shape-checked (`_row_problems`): a row
    with no valid verb, no conversation id, a non-boolean `would_dispatch` or a
    `dispatched` that is not `False` is an artifact this gate cannot read.

    Returns `{"ok", "blocked", "reason", …}`: `ok` false with `blocked` false
    means the rehearsal could not be matched at all.
    """
    base0 = {"planned": len(plan_mutations), "rehearsed": len(dry_rows),
             "missing": [], "extra": []}
    problems = (_row_problems(plan_mutations, kind="plan")
                + _row_problems(dry_rows, kind="rehearsal"))
    if problems:
        return dict(base0, ok=False, blocked=False, problems=problems[:8],
                    reason=("the plan or the rehearsal carries "
                            f"{len(problems)} malformed row field(s), so the "
                            "two cannot be matched at all: "
                            + "; ".join(problems[:3])
                            + ". A row this gate cannot read is not a row it "
                              "may certify, and nothing is dispatched"))

    want: dict[tuple[str, str], int] = {}
    for m in plan_mutations:
        k = _rehearsal_key(m.get("verb"), m.get("conversation_id"))
        want[k] = want.get(k, 0) + 1
    got: dict[tuple[str, str], int] = {}
    for r in dry_rows:
        k = _rehearsal_key(r.get("verb"), r.get("conversation_id"))
        got[k] = got.get(k, 0) + 1

    missing: list[str] = []
    for (verb, cid), n in sorted(want.items()):
        missing += [f"{verb} {cid}"] * max(0, n - got.get((verb, cid), 0))
    extra: list[str] = []
    for (verb, cid), n in sorted(got.items()):
        extra += [f"{verb} {cid}"] * max(0, n - want.get((verb, cid), 0))
    base = {"planned": len(plan_mutations), "rehearsed": len(dry_rows),
            "missing": missing[:8], "extra": extra[:8]}
    if missing or extra:
        return dict(base, ok=False, blocked=False, reason=(
            f"the rehearsal does not cover the plan: {len(plan_mutations)} "
            f"planned mutation(s), {len(dry_rows)} rehearsed, "
            f"{len(missing)} planned row(s) never rehearsed"
            + (f" (e.g. {missing[0]})" if missing else "")
            + (f" and {len(extra)} rehearsed row(s) not in the plan" if extra else "")
            + ". A rehearsal that did not rehearse this plan proves nothing "
              "about it, so nothing is dispatched"))

    # IDENTITY, NOT TRUTHINESS. `_row_problems` has already refused anything
    # that is not a boolean or absent; this keeps the count honest even so.
    ok_rows = sum(1 for r in dry_rows if r.get("would_dispatch") is True)
    if dry_rows and not ok_rows:
        reasons: dict[str, int] = {}
        for r in dry_rows:
            why = str(r.get("blocked") or r.get("reason") or "no reason recorded")
            reasons[why] = reasons.get(why, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        return dict(base, ok=False, blocked=True, would_dispatch=0, reason=(
            "0 of %d planned mutation(s) would dispatch. %s" % (
                len(dry_rows), "; ".join(
                    "%d x %s" % (n, why.replace("\n", " ")[:220])
                    for why, n in top))))
    return dict(base, ok=True, blocked=False, would_dispatch=ok_rows, reason=(
        f"{ok_rows} of {len(dry_rows)} rehearsed mutation(s) would dispatch, "
        f"and every planned row was rehearsed"))


def rehearsal_gate(plan_path: Path, dry_path: Path) -> tuple[int, str]:
    """The nightly's gate as ONE command with an exit status.

    0 proceed · 15 the lane cannot act · 2 the rehearsal could not be validated.
    Both non-zero paths dispatch nothing; they differ only in the morning they
    describe.
    """
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        dry = json.loads(dry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 2, (f"the plan or the rehearsal could not be read ({exc}) — "
                   "an unreadable rehearsal is not an empty one")
    for name, doc, key in (("plan", plan, "mutations"), ("rehearsal", dry, "dry")):
        if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
            return 2, (f"the {name} carries no `{key}` list — the key it is read "
                       "by is part of the contract, and a missing one used to "
                       "read as an empty plan")
    # IDENTITY BEFORE COVERAGE (K1). The row-by-row match below can only see
    # `(verb, conversation_id)`, so it certifies a rehearsal of a DIFFERENT
    # plan whenever the two happen to name the same threads and verbs — Codex
    # probed a P1/add plan against a P3/remove rehearsal `ok: true`. The digest
    # is over the whole payload, including the draft text a human may send
    # verbatim, so it is checked FIRST and the match becomes the second belt.
    stamped = plan.get("plan_digest")
    if not isinstance(stamped, str) or not stamped.strip():
        return 2, ("the plan carries no `plan_digest`, so the rehearsal cannot "
                   "be bound to it and a rehearsal of some other plan would "
                   "certify this one")
    actual = plan_digest(plan["mutations"], plan.get("run_id"))
    if actual != stamped:
        return 2, (f"the plan does not hash to its own stamp (stamped "
                   f"{stamped[:16]}…, computed {actual[:16]}…) — its payload "
                   "changed after it was written, so nothing rehearsed it")
    if dry.get("plan_digest") != stamped:
        return 2, (f"the rehearsal names plan digest "
                   f"{str(dry.get('plan_digest'))[:16]}… and this plan is "
                   f"{stamped[:16]}…. It rehearsed a DIFFERENT plan, which "
                   "proves nothing about this one however well the rows match")
    v = rehearsal_verdict(plan["mutations"], dry["dry"])
    return (0 if v["ok"] else 15 if v["blocked"] else 2), v["reason"]


def _seed_probe(bridge: Any, since: str) -> dict[str, Any]:
    """Was a FRESHER authenticated envelope captured after `since`? MEASURED.

    The 401 stop line asserted "no fresher envelope had been captured by the
    time it failed" while nothing in the build read the capture buffer at 401
    time (review 2026-08-13, round 4). This asks the page, whose
    `cap.freshestSeed` is the same query `init` uses to pick its seed.

    GUARDED, like the reconcile beside it: the transport failure that produced
    the 401 is exactly what would kill this call too, and an unguarded raise
    here would erase the report of a whole night's applied mutations. A probe
    that could not run says so — `measured: False` — and the sentence below
    prints that rather than an absence it never observed.
    """
    try:
        out = bridge.call("seed_probe", {"since": since})["out"] or {}
    except Exception as exc:                                     # noqa: BLE001
        return {"measured": False, "fresher_seed": None,
                "why": f"the seed probe could not run: {str(exc)[:160]}"}
    if not isinstance(out, dict) or "fresher_seed" not in out:
        return {"measured": False, "fresher_seed": None,
                "why": f"the seed probe answered {str(out)[:120]!r}"}
    return {"measured": bool(out.get("measured")),
            "fresher_seed": out.get("fresher_seed"),
            "fresher_seed_at": out.get("fresher_seed_at"),
            "captured_finditem": out.get("captured_finditem"),
            "why": out.get("why")}


def _seed_probe_sentence(seed: dict[str, Any] | None) -> str:
    """What the probe MEASURED, in the operator's own line."""
    if not seed or not seed.get("measured"):
        why = (seed or {}).get("why") or "the probe did not run"
        return (f"Whether a fresher envelope was captured is UNMEASURED here "
                f"({why}).")
    if seed.get("fresher_seed"):
        return ("A fresher authenticated FindItem WAS captured after this pass "
                f"began (at {seed.get('fresher_seed_at')}), so re-seeding from "
                "the buffer would very likely recover — but this lane does not "
                "do that on its own.")
    return ("Measured at failure time: NO fresher authenticated FindItem had "
            f"been captured since this pass began "
            f"({seed.get('captured_finditem')} captured in total).")


def _frozen_todo(vault: Path, run_id: str, plan_path: Path,
                 rehearsal_path: Path | None,
                 done_keys: set[str]) -> dict[str, Any]:
    """The frozen plan, PROVEN to be the one the rehearsal validated (K1).

    Five refusals, and every one of them dispatches nothing:

    * the plan does not hash to its own stamp (`load_frozen_plan`) — which
      since C1 includes a plan whose `run_id` was deleted,
    * the plan names another run,
    * no rehearsal was named — an apply against a frozen plan with no proof it
      was ever rehearsed is the unrehearsed apply wearing the artifact's
      clothes,
    * the rehearsal names a DIFFERENT digest — it rehearsed some other plan,
      which is precisely the P1/add-against-P3/remove case Codex probed
      `ok: true`,
    * the rehearsal REHEARSED NOTHING (review 2026-08-13, round 5, C3).

    THE LAST ONE WAS A DOCSTRING, NOT A CHECK. This function claimed to refuse
    "an apply with no proof it was ever rehearsed" while comparing one digest
    STRING, and both review lanes walked straight through it: a rehearsal of
    `dry: []`, and one whose every row said `would_dispatch: false`, were both
    ACCEPTED for a plan with real rows. The shell gate (`rehearsal-gate`, exit
    15) caught them, so nothing shipped broken — but the shell is a different
    process from the thing that dispatches, and a guarantee stated by the
    dispatcher has to be enforced by the dispatcher. `rehearsal_verdict` is the
    SAME function the shell gate calls, imported here rather than restated:
    one definition of "did this rehearsal cover this plan", two callers.

    `done_keys` still bites afterwards: resume-never-restart is unchanged, and
    a row this checkpoint already carried out is dropped from the todo without
    changing the plan's identity — the digest is over what was PLANNED, never
    over what is left to do.
    """
    plan = load_frozen_plan(plan_path)
    # EXACT, AND NEVER NULL (review 2026-08-13, round 5, C1). This read
    # `not in (None, run_id)`, so a plan whose `run_id` had been DELETED was
    # accepted by whichever run happened to read it — which is the whole replay:
    # an old run's rows against a new run's empty ledger, every one of them
    # re-dispatched, and the lane lock (keyed on the run id) never even
    # contended. The digest now covers the key too, so this is the second belt;
    # it is written as an exact match because a guard that accepts an absence is
    # a guard with a hole in the shape of an absence.
    if plan.get("run_id") != run_id:
        raise MutationStop(
            f"the frozen plan at {plan_path} was built for run "
            f"{plan.get('run_id')!r}, not {run_id!r} — a plan applied to "
            "another run's ledger is a plan about other threads")
    if rehearsal_path is None:
        raise MutationStop(
            "an apply against a frozen plan must name the rehearsal that "
            "validated it (--rehearsal). A plan with no rehearsal behind it is "
            "the unrehearsed apply this artifact exists to prevent")
    try:
        reh = json.loads(Path(rehearsal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} could not be read ({exc}) — "
            "an unreadable rehearsal is not a passed one") from exc
    if not isinstance(reh, dict) or reh.get("plan_digest") != plan["plan_digest"]:
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} names plan digest "
            f"{(reh or {}).get('plan_digest')!r} and this plan is "
            f"{plan['plan_digest']!r}. The rehearsal validated a DIFFERENT "
            "plan, so it proves nothing about this one — a P1/add plan behind "
            "a P3/remove rehearsal is the exact shape this refuses, and it "
            "used to return ok")
    # THE REHEARSAL HAS TO HAVE REHEARSED SOMETHING (C3). Same predicate as the
    # shell gate, same reasons, and the `dry` key is read defensively for the
    # same reason `mutations` is: a missing list used to read as an empty plan.
    dry_rows = reh.get("dry")
    if not isinstance(dry_rows, list):
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} carries no `dry` list — the key "
            "it is read by is part of the contract, and a rehearsal with no "
            "rehearsed rows in it is not a rehearsal this apply may act on")
    verdict = rehearsal_verdict(plan["mutations"], dry_rows)
    if not verdict["ok"]:
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} names this plan's digest but "
            f"did not pass its own gate: {verdict['reason']}. A matching digest "
            "string is INTEGRITY — it says the two files describe one plan — "
            "and this apply also needs the rehearsal to have actually run")
    todo = [m for m in plan["mutations"]
            if f"{m.get('conversation_id')}|{m.get('verb')}" not in done_keys]
    return {"plan": plan, "todo": todo}


def apply_pass(vault: Path, run_id: str, tab_id: int, *,
               caps: dict[str, int] | None = None,
               since_days: int | None = DEFAULT_SINCE_DAYS,
               allow_draft_resume: bool = False,
               plan_path: Path | None = None,
               rehearsal_path: Path | None = None,
               use_cdp: bool = False) -> dict[str, Any]:
    """The live mutating pass. Every guard is checked HERE, in code.

    `plan_path`/`rehearsal_path` are how the NIGHTLY runs it, and the CLI
    refuses `apply` without both (K1): the plan the apply dispatches is the
    frozen artifact the rehearsal validated, by digest, or nothing is
    dispatched. Omitting them builds a fresh plan — the pre-s09 behaviour, kept
    only for the in-process callers that construct their own ledger — and the
    report says so in `plan_binding` rather than leaving it to be inferred.
    """
    with _mutation_lane_lock(vault, run_id):
        # THE COUNTERS ARE WRITTEN ON EVERY EXIT, INCLUDING THE BAD ONES. A run
        # that stopped early, refused, or died mid-plan is exactly the run whose
        # metrics row must not still read `archived: 0` — everything dispatched
        # before the stop is dispatched, and it is what the anti-vacuity guards
        # corroborate a missing artifact against. It runs INSIDE the lane lock,
        # so it cannot race a concurrent pass on the same run.
        #
        # ponytail: a failure to WRITE the counters is swallowed rather than
        # masking the apply's own outcome — it fails CLOSED, because
        # `check_metrics_row` reads an all-zero row beside a non-empty undo
        # ledger as INCONCLUSIVE, never as a pass.
        try:
            out = _apply_pass_locked(
                vault, run_id, tab_id, caps=caps, since_days=since_days,
                allow_draft_resume=allow_draft_resume, plan_path=plan_path,
                rehearsal_path=rehearsal_path, use_cdp=use_cdp)
        except BaseException:
            try:
                record_mutation_counters(vault, run_id)
            except Exception:                                     # noqa: BLE001
                pass
            raise
        try:
            out["mutation_counters"] = record_mutation_counters(vault, run_id)
        except Exception as exc:                                  # noqa: BLE001
            out["mutation_counters"] = {"appended": "failed",
                                        "error": str(exc)[:300]}
        return out


def _apply_pass_locked(vault: Path, run_id: str, tab_id: int, *,
                       caps: dict[str, int] | None = None,
                       since_days: int | None = DEFAULT_SINCE_DAYS,
                       allow_draft_resume: bool = False,
                       plan_path: Path | None = None,
                       rehearsal_path: Path | None = None,
                       use_cdp: bool = False) -> dict[str, Any]:
    root = assert_vault(vault)
    ks = kill_switch(vault)
    if not ks["enabled"]:
        raise MutationStop(f"the kill switch at {ks['source']} reads "
                           f"enabled: false ({ks['state']}) — no mutation runs")
    e17 = canary_status(vault)
    if not e17["valid"]:
        raise MutationStop(
            f"E17: the {MUTATION_LANE!r}-lane undo canary does not satisfy the "
            f"gate ({e17['why']}). Run the drill (`cos_mutate.py canary`) "
            "before any mutation — guard condition 5 exists to make an "
            "unverified undo path impossible to mutate through.")
    shapes = load_shapes(vault)
    if shapes["missing"] or not shapes["shapes"]:
        raise MutationStop(
            f"no approved mutation shapes at {shapes['path']}. A mutation "
            "request is a REPLAY of a shape the server already accepted for "
            "that verb (doctrine v4.7); there is no path that builds one.")

    ledger = UndoLedger(vault, run_id)
    unfinished = ledger.unfinished()
    done_keys = set(ledger.latest())
    if plan_path is not None:
        frozen = _frozen_todo(vault, run_id, plan_path, rehearsal_path, done_keys)
        plan, todo = frozen["plan"], frozen["todo"]
        binding = {"source": "frozen", "plan": str(plan_path),
                   "rehearsal": str(rehearsal_path),
                   "plan_digest": plan["plan_digest"],
                   "planned": len(plan["mutations"]),
                   "already_carried_out_this_checkpoint":
                       len(plan["mutations"]) - len(todo)}
    else:
        plan = build_plan(vault, run_id, caps=caps, since_days=since_days,
                          applied=ledger.applied_counts(), skip_keys=done_keys)
        todo = list(plan["mutations"])
        binding = {"source": "rebuilt-by-the-apply",
                   "plan_digest": plan["plan_digest"],
                   "why": "no --plan was given, so this pass planned for "
                          "itself and no rehearsal is bound to what it "
                          "dispatched. The CLI refuses this; only an "
                          "in-process caller reaches it"}
    # WRITTEN WHERE A VERDICT CAN READ IT (review 2026-08-13, round 5). This
    # value existed only in the `--out` report, which lives in the repo's
    # evidence directory — so `cos_runverify`, which scores a run from the
    # VAULT's artifacts, could never see it, and nothing anywhere failed on
    # `rebuilt-by-the-apply`. A field written and read by nothing is a comment
    # (`hardening-prose-is-not-a-mechanism`). `check_plan_binding` reads this
    # file and FAILS the run.
    _write_text_atomic(plan_binding_path(vault, run_id),
                       json.dumps(binding, indent=2, ensure_ascii=False) + "\n")

    bridge = _bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp)
    init = {"transport": "cdp" if use_cdp else "applescript"}
    account = str(root["BRAIN_VAULT"])
    # THE WINDOW THE 401 DIAGNOSTIC IS ABOUT: "fresher" means captured after
    # this pass started replaying the seed it was handed. The lane's own reads
    # go out through `cap.rawFetch` and are never captured, so anything the
    # buffer holds after this instant is the APP's own traffic — the only kind
    # a re-seed could use.
    pass_started_at = _ts()

    results: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    stop_reason = None
    absent: list[dict[str, Any]] = []
    absent_cap = absent_skip_cap(len(todo))

    # Resume before starting anything new: a row left mid-flight is the server's
    # question to answer, not ours to guess.
    for row in unfinished:
        if row["verb"] == "draft" and not allow_draft_resume:
            ledger.append(dict(row, state="unknown",
                               connector_result="manual-resolution-required",
                               verification=DRAFT_RESUME_POLICY))
            continue
        # A BRIDGE CALL OUTSIDE THE `try` IS A REPORT THAT NEVER GETS WRITTEN
        # (review 2026-08-12). Three of them sat outside — this resume
        # reconcile, the per-row `resolve` below, and the closing `state` — and
        # a timeout in any one raised straight past `apply_pass` into `main`,
        # whose stop handler writes a report hard-coding `results: []`. A night
        # that had applied fifty mutations then produced an artifact claiming
        # it had done nothing.
        try:
            rec = bridge.call("reconcile", {"mutation": {
                "verb": row["verb"], "conversation_id": row["conversation_id"],
                "chip": row.get("chip"), "mode": row.get("mode"),
                "signature": draft_signature(run_id,
                                             row["conversation_id"])}})["out"]
        except MutationStop as exc:
            ledger.append(dict(row, state="unknown",
                               connector_result="reconcile-unavailable",
                               verification=f"the resume reconcile could not "
                                            f"run: {str(exc)[:200]}"))
            stop_reason = (f"the resume reconcile for {row['verb']} on "
                           f"{short(row['conversation_id'])} failed: "
                           f"{str(exc)[:200]}")
            break
        ledger.append(dict(row,
                           state=("reconciled" if rec.get("applied")
                                  else "aborted-not-applied" if rec.get("conclusive")
                                  else "unknown"),
                           connector_result=rec.get("observed"),
                           verification=f"reconciled by re-read: {rec.get('query')}"))
        results.append({"resumed": True, **rec})

    for m in todo:
        # A resume that could not reconcile has already stopped the run.
        if stop_reason:
            break
        if stopped(vault, run_id):
            stop_reason = f"the stop file {stop_file(vault, run_id)} appeared"
            break
        try:
            resolved = bridge.call("resolve", {
                "conversation_id": m["conversation_id"],
                "folder": "inbox"})["out"]
        except MutationStop as exc:
            # Nothing was dispatched and no intent row was written, so there is
            # nothing to reconcile — but the report must still be the report of
            # everything this pass DID do, not an empty one written by `main`.
            #
            # A 401 HERE IS THE STALE BEARER, AND IT IS NAMED (run 130) — but
            # NOT as a recovery that was tried and failed (review 2026-08-13,
            # round 2). This used to read "one re-prime did not recover it",
            # asserting an attempt the page cannot make.
            #
            # AND NOT AS AN ABSOLUTE EITHER (round 3). The corrected text said
            # the app issues its FindItem "at boot only", which is what s03
            # concluded from a SETTLED tab and what the capture hook's own
            # header corrects: measured three times on 2026-08-11, a hook
            # installed at `readyState=complete` does capture an authenticated
            # `FindItem` once the tab becomes ACTIVE and the list settles.
            #
            # SO IT IS MEASURED NOW, NOT ASSERTED (round 4, Claude MEDIUM). The
            # corrected text still said "no fresher envelope had been captured
            # by the time it failed" while NOTHING in this build read the
            # capture buffer at 401 time — one unverified absolute swapped for
            # another. The page has the query (`cap.freshestSeed`), so the host
            # asks it, over the window that starts at this transition, and
            # prints the ANSWER. A probe that cannot run is reported as
            # unmeasured; it is never silently read as "none".
            auth401 = bool(getattr(exc, "auth401", False))
            seed = _seed_probe(bridge, pass_started_at) if auth401 else None
            if auth401:
                transitions.append({"at": _ts(), "reason": "http-401",
                                    "leg": "resolve",
                                    "recovery": "host-only-requires-reprepare",
                                    "fresher_seed": seed.get("fresher_seed"),
                                    "fresher_seed_measured": seed.get("measured"),
                                    "conversation_id": short(m["conversation_id"])})
            stop_reason = (f"the mailbox read before {m['verb']} on "
                           f"{short(m['conversation_id'])} failed: "
                           f"{str(exc)[:200]}"
                           + (" — the replayed envelope's bearer aged out "
                              "part-way through this pass. "
                              + _seed_probe_sentence(seed)
                              + " This lane does not retry a 401 "
                              "automatically: re-seeding is the host's call, "
                              "deliberately. So the lane stopped here; "
                              "everything logged above applied and verified. "
                              "Re-run the night — `cos_cdp_capture.py "
                              "--prepare` navigates and takes a fresh envelope"
                              if auth401 else ""))
            break
        # 2. THE UNDO ROW IS ON DISK BEFORE THE CALL.
        intent = _undo_row(m, resolved, state="intent", run_id=run_id,
                           account=account)
        ledger.append(intent)
        try:
            res = bridge.call("apply", {"mutation": m})["out"]
        except MutationStop as exc:
            in_flight = bool(getattr(exc, "mutation_in_flight", False))
            if getattr(exc, "canary449", False):
                transitions.append({"at": _ts(), "reason": "http-449",
                                    "mutation_in_flight": in_flight,
                                    "conversation_id": short(m["conversation_id"])})
            if getattr(exc, "auth401", False):
                transitions.append({"at": _ts(), "reason": "http-401",
                                    "leg": "apply",
                                    "recovery": "never-a-mutation-is-re-issued",
                                    "mutation_in_flight": in_flight,
                                    "conversation_id": short(m["conversation_id"])})
            # The response is lost, not the outcome: ASK THE SERVER.
            #
            # AND GUARD THE ASKING (review 2026-08-13, round 3). This is the
            # FOURTH bridge call — the round-2 fix guarded the three sitting
            # outside the `try` and missed the one inside its own `except`.
            # The transport death that raised `exc` is exactly the failure
            # most likely to kill this call too, and an unguarded raise here
            # propagated to `main`, whose stop handler writes `results: []`
            # over a night of applied mutations — the precise erased-report
            # failure the round-2 fix claimed to close.
            try:
                rec = bridge.call("reconcile", {"mutation": {
                    "verb": m["verb"], "conversation_id": m["conversation_id"],
                    "chip": m.get("chip"), "mode": m.get("mode"),
                    "signature": m.get("signature")}})["out"]
            except MutationStop as rexc:
                # Nothing is known: the mutation MAY have applied and the
                # re-read could not run. `unknown` is the only honest state,
                # and the report of everything BEFORE this row must survive.
                ledger.append(dict(intent, state="unknown",
                                   connector_result=str(exc)[:400],
                                   verification=f"the lost-response reconcile "
                                                f"could not run: "
                                                f"{str(rexc)[:200]}"))
                stop_reason = (f"{str(exc)[:200]} — and the reconcile after it "
                               f"also failed: {str(rexc)[:200]}")
                results.append({"conversation_id": short(m["conversation_id"]),
                                "verb": m["verb"], "stopped": True,
                                "reconciliation": None,
                                "reconcile_unavailable": str(rexc)[:200]})
                break
            ledger.append(dict(intent,
                               state=("reconciled" if rec.get("applied")
                                      else "aborted-not-applied"
                                      if rec.get("conclusive") else "unknown"),
                               connector_result=str(exc)[:400],
                               verification=f"reconciliation after a lost "
                                            f"response: {rec.get('observed')}"))
            if transitions:
                transitions[-1]["in_flight_449_outcome"] = (
                    "stopped-and-reconciled" if in_flight else "none-in-flight")
                transitions[-1]["reconciliation"] = rec
            stop_reason = str(exc)[:400]
            results.append({"conversation_id": short(m["conversation_id"]),
                            "verb": m["verb"], "stopped": True,
                            "reconciliation": rec})
            break

        state = res.get("state")
        # A CONCLUSIVE ABSENCE IS TERMINAL, AND IT IS NOT `sent` (review
        # 2026-08-12). The page's skip carries the state word it would have
        # used had it dispatched, and this row wrote that word to disk before
        # the skip rule below ever ran — so a chip that never left the machine
        # was recorded `sent`, and `unchip` selects on `sent`. The run did not
        # apply it, so the ledger says so: `aborted-not-applied`, which is
        # terminal, spends no cap, and is not eligible for a reversal.
        absent_skip = (res.get("dispatched") is False
                       and res.get(ABSENT_TARGET_FLAG) is True
                       and res.get("absence_conclusive") is True)
        if absent_skip:
            state = "aborted-not-applied"
        ledger.append(dict(intent, state=state if state in STATES else "unknown",
                           connector_result=res.get("response_code")
                           or res.get("outcome"),
                           verification=res.get("verification"),
                           new_item_id=res.get("new_item_id"),
                           # WHETHER ANYTHING LEFT THE MACHINE, on the row. The
                           # reversals (`undo`, `unchip`) may only touch what
                           # this run actually sent, and the ledger was the one
                           # place that fact was not written down.
                           dispatched=res.get("dispatched"),
                           receipts=res.get("receipts")))
        # `state` and not `res["state"]`: the page reports the word it would
        # have used had it dispatched, and the row on disk says what this run
        # actually did. Two records of one event may not disagree.
        results.append({**res, "state": state,
                        "conversation_id": short(m["conversation_id"])})
        if state != "reconciled":
            # A TARGET THAT ISN'T THERE IS NOT A FAILED MUTATION. The page half
            # labels "I looked in the folder and the thread was gone" with the
            # same `verified-failed` word it uses for "I sent a change and could
            # not confirm it", and this loop stopped the whole night on either.
            # Measured on run 125, the first unattended run: one thread had
            # moved between the plan and the apply, and the run halted at chip
            # 30 of 65 having archived nothing and drafted nothing — while its
            # last log line still said `done`. On a mailbox the owner is
            # actually using, a thread moving mid-run is ordinary, so that halt
            # would have fired most mornings.
            #
            # The discriminator is NOT the word, it is `dispatched` AND proof
            # that the folder was read to the end. Nothing left the machine, so
            # there is no outcome in doubt and nothing to reconcile — but only
            # if the thread was really gone. `dispatched: false` alone cannot
            # tell "it moved" from "I could not see it" (review 2026-08-12): a
            # truncated enumeration or a throttled GetItem produced the same
            # row, so a browser-side read failure could drop every row of a
            # night and still report a completed run. The page half now returns
            # `absence_conclusive` from the enumeration's own
            # last-item-in-range flag, and an INCONCLUSIVE absence keeps its own
            # outcome word and falls through to the stop below. A row that WAS
            # dispatched and did not reconcile still stops the run, on the
            # original reasoning and unchanged.
            if absent_skip:
                absent.append({"conversation_id": short(m["conversation_id"]),
                               "verb": m["verb"], "outcome": res.get("outcome"),
                               # The evidence the skip was decided on, so the
                               # report can be audited instead of believed.
                               "enumeration_terminated":
                                   res.get("enumeration_terminated"),
                               "enumeration_pages": res.get("enumeration_pages"),
                               "enumeration_folder":
                                   res.get("enumeration_folder")})
                if len(absent) > absent_cap:
                    stop_reason = (
                        f"{len(absent)} of {len(todo)} planned rows were absent "
                        f"from the mailbox, past this run's ceiling of "
                        f"{absent_cap} — that many threads do not move in one "
                        f"night, so this is the browser reading a starved "
                        f"folder and not a busy mailbox")
                    break
                continue
            stop_reason = (f"verification failed for {m['verb']} on "
                           f"{short(m['conversation_id'])}: "
                           f"{res.get('verification')}")
            break

    # TELEMETRY MAY NOT ERASE THE RECORD. This is the last of the three
    # unguarded bridge calls: it reports the page half's runtime counters, and
    # a timeout in it used to throw away the whole report of a completed night.
    try:
        runtime = bridge.call("state")["out"]
    except MutationStop as exc:
        runtime = {"unavailable": str(exc)[:200]}
    return {
        "vault_root_asserted": root, "kill_switch": ks, "e17": e17,
        "shapes_path": shapes["path"],
        "shape_fingerprints": {k: v.get("fingerprint")
                               for k, v in shapes["shapes"].items()},
        "capture": (init.get("out") or {}).get("capture"),
        # WHAT THIS PASS WAS BOUND BY (K1). A report that does not say whether
        # the plan it dispatched was the rehearsed artifact cannot be audited
        # for the one property the whole gate exists to give.
        "plan_binding": binding,
        "plan": plan, "results": results, "stopped": stop_reason,
        "skipped_absent": absent, "skipped_absent_cap": absent_cap,
        "http_449_transitions": transitions,
        "runtime": runtime,
        "ledger": str(ledger.path),
        "final_states": {k: v["state"] for k, v in ledger.latest().items()},
    }


# ---------------------------------------------------------------------------
# the canary drill (E17) — the undo path, exercised on ONE disposable row
# ---------------------------------------------------------------------------
CANARY_STEPS = ("chip_roundtrip", "archive", "undo", "replay")


#: Verification words only a DISPATCHED mutation that the server ANSWERED can
#: wear. They are the dispatch proof for a legacy row that predates the
#: `dispatched` field. `verified-failed` is deliberately NOT here: the absent
#: -target skips wear it too, so it proves nothing either way.
DISPATCH_PROVEN_BY = ("verified-archived", "verified-categorized",
                      "verified-draft-saved", "verified-failed-noop")

#: The three answers `_reversal_eligibility` can give. `manual` is the one that
#: did not exist before 2026-08-12, and its absence is what made a missing
#: field mean "yes" on one path and "not eligible" on another.
REVERSAL_YES, REVERSAL_NO, REVERSAL_MANUAL = "yes", "no", "manual"

#: States a reversal may CONSIDER. `intent` joined them in the review of
#: 2026-08-12: it is written BEFORE the call, so a death between dispatch and
#: result leaves a real mutation in it — and every reversal command targeted
#: only the states written AFTER a result, so no command could ever reach it.
#: Considering is not acting: `_reversal_eligibility` routes `intent` to
#: MANUAL resolution (round 3, 2026-08-13), so it is surfaced, never guessed.
REVERSIBLE_STATES = ("intent", "reconciled", "confirmed", "sent", "unknown")


def _reversal_eligibility(row: dict[str, Any]) -> str:
    """May a REVERSAL touch this row — and if it cannot tell, does it say so?

    A reversal is destructive on someone else's mailbox state: `unchip` takes a
    category off a live thread and `undo` moves a thread back into the Inbox.
    Both selected on the ledger's STATE, and two rows reach a reversible-looking
    state without anything ever leaving the machine — the "already applied"
    skip (`reconciled`, because the chip was already there when we looked) and
    an absent target. Reversing either removes a chip, or un-archives a thread,
    the OWNER put there.

    MISSING EVIDENCE WAS BEING READ BOTH WAYS (review 2026-08-12). A row with no
    `dispatched` field counted as "this run sent it" — so a legacy or resumed
    row with no dispatch evidence at all could remove a chip the owner set —
    while a row in `intent`, which is the one state that really might hold an
    unrecorded live mutation, was not selected by any reversal command. The same
    absence of proof meant yes in one place and not-eligible in the other.

    So there are THREE answers, not two:

    * `no`   — this run recorded that nothing left the machine (`dispatched:
      False`). Never touched.
    * `yes`  — this run recorded a dispatch, or a legacy row carries a
      verification word only an answered dispatch can wear.
    * `manual` — a row whose evidence cannot answer the question: a legacy row
      with no field and no proving word, or an `intent` row. It is REPORTED,
      not guessed at in either direction.

    `intent` briefly answered `yes` (2026-08-13, same day), on the argument
    that the reversal re-reads the mailbox before acting — but the re-read
    proves the thread's CURRENT state, not who caused it (review round 3).
    Crash after the write-ahead row but before dispatch, owner archives the
    thread by hand, undo runs: the "reversal" un-archives the OWNER's action —
    the same class as removing a chip the owner set. So `intent` is reachable
    (it lands in `needs_manual_resolution`, which the status page surfaces),
    and never auto-acted-on: the ledger cannot distinguish a death before
    dispatch from a death after it, and a human can.
    """
    if row.get("state") == "intent":
        return REVERSAL_MANUAL
    dispatched = row.get("dispatched")
    if dispatched is True:
        return REVERSAL_YES
    if dispatched is False:
        return REVERSAL_NO
    return (REVERSAL_YES if row.get("verification") in DISPATCH_PROVEN_BY
            else REVERSAL_MANUAL)


def undo_pass(vault: Path, run_id: str, tab_id: int | None, *,
              use_cdp: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Put a run's archives back in the Inbox — every one of them, by conversation.

    WHY IT EXISTS. The archive cap is what used to bound a bad night: three
    threads, and a human sees it before there is a fourth. The owner lifted that
    cap on 2026-08-11 ("be aggressive, do them all"), which is a legitimate call
    — an archive is reversible and the noise rules are narrow — but only while
    the reversal is ONE COMMAND rather than a morning of clicking. This is that
    command.

    It reads the run's own undo ledger, so it can only undo what that run
    recorded doing, and it keys on `conversation_id` exactly as doctrine v4.7
    requires (a move-time ItemId is a session handle, not an identity). Each
    restore is verified by re-reading, appended as its own `restore` row, and a
    thread already back in the Inbox is reported as such and NOT dispatched.
    """
    root = assert_vault(vault)
    shapes = load_shapes(vault)
    ledger = UndoLedger(vault, run_id)
    candidates = [r for r in ledger.latest().values()
                  if r["verb"] == "archive" and r["state"] in REVERSIBLE_STATES]
    already = {r["conversation_id"] for r in ledger.latest().values()
               if r["verb"] == "restore" and r["state"] == "reconciled"}
    candidates = [r for r in candidates if r["conversation_id"] not in already]
    targets = [r for r in candidates
               if _reversal_eligibility(r) == REVERSAL_YES]
    # NEVER GUESSED AT IN EITHER DIRECTION: a legacy row with no dispatch
    # evidence is reported for a human, not silently reversed and not silently
    # dropped (review 2026-08-12).
    manual = [short(r["conversation_id"]) for r in candidates
              if _reversal_eligibility(r) == REVERSAL_MANUAL]
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        return {"run_id": run_id, "restored": 0, "results": [],
                "needs_manual_resolution": manual,
                "why": "this run's ledger records no archive left to put back",
                "vault_root_asserted": root}

    # `_bridge_for` has already staged and initialised whichever transport this
    # is. The second `_init_page` call that used to be here reached for
    # `bridge.tab`, which the CDP transport does not have — so this raised
    # AttributeError before its first restore on the exact transport
    # `cos_ctl.sh undo` drives it with. The tests never saw it: they patch
    # `_init_page` out.
    bridge = _bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp)
    results: list[dict[str, Any]] = []
    for row in targets:
        m = {"verb": "archive", "conversation_id": row["conversation_id"],
             "restore": True}
        # ITS OWN KEY. Reusing the archive's would make the restore row SUPERSEDE
        # the archive in `latest()`, erasing the record of what the run did and
        # making the "already restored" check answer about the wrong thing.
        base = dict(row, verb="restore",
                    idempotency_key=f"{row['conversation_id']}|restore",
                    reason=f"undo of this run's archive ({run_id})",
                    action_ts=_ts())
        # THE INTENT ROW IS ON DISK BEFORE THE CALL — `apply_pass` step 2's rule,
        # and this lane dispatched first and recorded afterwards (review
        # 2026-08-12). A response lost after the server took the move left no
        # durable trace that this command had ever touched the thread.
        # `dispatched=None`, EXPLICITLY. `base` is a copy of the forward archive
        # row, which carries `dispatched: True` — so the reversal's write-ahead
        # row inherited a dispatch claim about a request this loop had not made
        # yet (review 2026-08-12). A reversal intent gets its own field.
        ledger.append(dict(base, state="intent", connector_result=None,
                           verification=None, dispatched=None))
        out = bridge.call("apply", {"mutation": m})["out"]
        applied = out.get("verification") in ("verified-archived",
                                              "response-confirmed")
        ledger.append(dict(base, state="reconciled" if applied else "sent",
                           connector_result=out.get("outcome"),
                           verification=out.get("verification"),
                           dispatched=out.get("dispatched")))
        results.append({"conversation_id": short(row["conversation_id"]),
                        "verification": out.get("verification"),
                        "outcome": out.get("outcome")})
    return {"run_id": run_id, "restored": sum(1 for r in results
                                              if r["verification"] in
                                              ("verified-archived",
                                               "response-confirmed")),
            "attempted": len(results), "results": results,
            "needs_manual_resolution": manual,
            "vault_root_asserted": root}


def unchip_pass(vault: Path, run_id: str, tab_id: int | None, *,
                use_cdp: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Take every managed chip this run put on back off — verified per thread.

    WHY IT EXISTS. The same argument as `undo_pass`, one lane over: the chip cap
    came off with the archive cap, and an uncapped write is only safe while its
    reversal is ONE COMMAND. Until 2026-08-12 the chip lane had no reversal at
    all — a reduced `Categories` updates the forward RULE and leaves the chip on
    the thread — so this rides the captured `CategoriesToRemove` shape instead.

    Same discipline as the undo: it reads only THIS run's ledger, keys on
    `conversation_id`, verifies each removal by re-reading, appends its own
    `unchip` row (never superseding the chip row it reverses), and skips a thread
    already unchipped. A chip row with no chip NAME is not guessed at — removing
    "the chip" is not an instruction anything can follow — it is reported and
    left for a human.
    """
    root = assert_vault(vault)
    shapes = load_shapes(vault)
    ledger = UndoLedger(vault, run_id)
    latest = ledger.latest()
    candidates = [r for r in latest.values()
                  if r["verb"] == "categorize" and r.get("mode") != "remove"
                  and r["state"] in REVERSIBLE_STATES]
    already = {r["conversation_id"] for r in latest.values()
               if r["verb"] == "unchip" and r["state"] == "reconciled"}
    candidates = [r for r in candidates if r["conversation_id"] not in already]
    targets = [r for r in candidates
               if _reversal_eligibility(r) == REVERSAL_YES]
    manual = [short(r["conversation_id"]) for r in candidates
              if _reversal_eligibility(r) == REVERSAL_MANUAL]
    unnamed = [r for r in targets if not r.get("chip")]
    targets = [r for r in targets if r.get("chip")]
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        return {"run_id": run_id, "unchipped": 0, "results": [],
                "no_chip_name_on_row": [short(r["conversation_id"])
                                        for r in unnamed],
                "needs_manual_resolution": manual,
                "why": "this run's ledger records no chip left to take off",
                "vault_root_asserted": root}

    bridge = _bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp)
    results: list[dict[str, Any]] = []
    for row in targets:
        m = {"verb": "categorize", "conversation_id": row["conversation_id"],
             "chip": row["chip"], "mode": "remove"}
        # ITS OWN KEY, for the reason `restore` has its own: reusing the chip's
        # would make the reversal supersede the record of what the run did.
        base = dict(row, verb="unchip",
                    idempotency_key=f"{row['conversation_id']}|unchip",
                    mode="remove",
                    reason=f"unchip of this run's chip ({run_id})",
                    action_ts=_ts())
        # WRITE-AHEAD, like the archive lane and for its reason (review
        # 2026-08-12): this loop dispatched a category removal and only then
        # recorded that it had, so a lost response left the chip gone from the
        # mailbox and nothing at all on disk saying who took it off.
        # Its own `dispatched`, for `undo_pass`'s reason one lane over.
        ledger.append(dict(base, state="intent", connector_result=None,
                           verification=None, observed_after=None,
                           dispatched=None))
        out = bridge.call("apply", {"mutation": m})["out"]
        applied = out.get("verification") in ("verified-categorized",
                                              "response-confirmed")
        ledger.append(dict(base, state="reconciled" if applied else "sent",
                           connector_result=out.get("outcome")
                           or out.get("response_code"),
                           verification=out.get("verification"),
                           observed_after=out.get("observed_after"),
                           dispatched=out.get("dispatched")))
        results.append({"conversation_id": short(row["conversation_id"]),
                        "chip": row["chip"],
                        "verification": out.get("verification"),
                        "observed_after": out.get("observed_after"),
                        "outcome": out.get("outcome")})
    return {"run_id": run_id,
            "unchipped": sum(1 for r in results
                             if r["verification"] in ("verified-categorized",
                                                      "response-confirmed")),
            "attempted": len(results), "results": results,
            "no_chip_name_on_row": [short(r["conversation_id"]) for r in unnamed],
            "needs_manual_resolution": manual,
            "vault_root_asserted": root}


def canary_drill(vault: Path, run_id: str, tab_id: int, conv_id: str,
                 *, use_cdp: bool = False) -> dict[str, Any]:
    """Archive one disposable row, undo it, replay the undo, round-trip a chip.

    Every step produces a RECEIPT read back from the server, and the canary file
    is written only when all four are present — "writing the canary file without
    executing every drill step on live rows is an E17 FAIL; the file asserts
    receipts, never bare fields".
    """
    root = assert_vault(vault)
    shapes = load_shapes(vault)
    if shapes["missing"]:
        raise MutationStop(f"no approved shapes at {shapes['path']}")
    bridge = _bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp)
    receipts: dict[str, Any] = {}

    before = bridge.call("resolve", {"conversation_id": conv_id,
                                     "folder": "inbox"})["out"]
    if not before.get("found"):
        raise MutationStop("the nominated canary conversation is not in the Inbox")
    receipts["row"] = {"conversation_id_digest": short(conv_id),
                       "members_in_inbox": before.get("members"),
                       "before_categories": before.get("before_categories")}

    # THE CHIP LANE IS CLOSED on this build (UpdateAlwaysCategorizeRule writes a
    # standing rule, not a reversible label), so there is no approved chip shape
    # to drill. Drilling it anyway would either fail the whole canary or, worse,
    # tempt a shape to be synthesized. The canary records WHICH lanes it drilled
    # instead of implying it drilled them all.
    chip_open = bool((shapes["shapes"] or {}).get("UpdateItem"))
    if not chip_open:
        receipts["chip_roundtrip"] = {
            "drilled": False,
            "why": "the chip lane is closed: this build writes a standing "
                   "always-categorize RULE, and no per-item categorize shape is "
                   "approved. E17 validity below covers the ARCHIVE lane only.",
            "set_preserved": True}
    chip = MANAGED_CHIPS[2]
    add = chip_open and bridge.call("apply", {"mutation": {"verb": "categorize", "chip": chip,
                                             "mode": "add",
                                             "conversation_id": conv_id}})["out"]
    if chip_open:
        remove = bridge.call("apply", {"mutation": {
            "verb": "categorize", "chip": chip, "mode": "remove",
            "conversation_id": conv_id}})["out"]
        receipts["chip_roundtrip"] = {
            "before": add.get("before_image"),
            "after_add": add.get("observed_after"),
            "after_remove": remove.get("observed_after"),
            "set_preserved": sorted(add.get("before_image") or [])
            == sorted(remove.get("observed_after") or []),
            "verification": [add.get("verification"), remove.get("verification")]}

    arch = bridge.call("apply", {"mutation": {"verb": "archive",
                                              "conversation_id": conv_id}})["out"]
    receipts["archive"] = {"verification": arch.get("verification"),
                           "receipts": arch.get("receipts")}

    undo = bridge.call("apply", {"mutation": {"verb": "archive",
                                              "conversation_id": conv_id,
                                              "restore": True}})["out"]
    receipts["undo"] = {"verification": undo.get("verification"),
                        "receipts": undo.get("receipts")}
    replay = bridge.call("apply", {"mutation": {"verb": "archive",
                                                "conversation_id": conv_id,
                                                "restore": True}})["out"]
    receipts["replay"] = {"result": replay.get("outcome") or replay.get("verification")}

    missing = [s for s in CANARY_STEPS if not receipts.get(s)]
    ok = (not missing
          and receipts["chip_roundtrip"]["set_preserved"]
          and arch.get("verification") == "verified-archived"
          and undo.get("verification") in ("verified-archived", "response-confirmed")
          and str(receipts["replay"]["result"]).find("already") != -1)
    if not ok:
        return {"written": False, "receipts": receipts, "missing_steps": missing,
                "why": "a drill step produced no verification receipt; the canary "
                       "file is NOT written (a written file is not a run drill)",
                "vault_root_asserted": root}

    from brain import cos                                        # noqa: PLC0415
    path = cos.run_ops_dir(vault) / "_cos_undo_canary.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    lanes = doc.setdefault("lanes", {})
    if "tested" in doc and "rest" not in lanes:
        lanes["rest"] = {k: v for k, v in doc.items() if k != "lanes"}
    lanes[MUTATION_LANE] = {
        "lanes_drilled": ["archive"] + (["categorize"] if chip_open else []),
        "tested": _ts(), "message_id": before.get("internet_message_id"),
        "key_scheme": "message-id" if before.get("internet_message_id") else "convid",
        "mutation_lane": MUTATION_LANE, "primitive": PRIMITIVE["archive"],
        "idempotent_replay": "confirmed", "operator": "owner",
        "toolset": "chrome-plugin, service.svc replay (cos_mutate_page.js)",
        "receipts": receipts,
    }
    _write_text_atomic(path,
                       json.dumps({k: v for k, v in doc.items() if k == "lanes"},
                                  indent=2, ensure_ascii=False) + "\n",
                       mode=_OPS_MODE)
    return {"written": True, "path": str(path), "receipts": receipts,
            "vault_root_asserted": root}


def capture_shapes(vault: Path, tab_id: int) -> dict[str, Any]:
    """Read the approved request shapes out of the page's capture buffer.

    The owner performs each action ONCE in the UI (archive a message, set a
    priority chip, save a reply draft) with the capture hook installed; this
    reads the resulting requests, scrubs every id, address, subject and body out
    of them in the page, and stores the structure. That stored structure is what
    every later mutation replays — which is what "replay, never synthesize"
    means in practice.
    """
    from brain import cos                                        # noqa: PLC0415
    root = assert_vault(vault)
    # The buffer it reads lives in the page's world, so prove the hook is there
    # first. A late install is legal here — the three mutation shapes are fired
    # by owner actions AFTER load — but a hook in the WRONG world would hand
    # back an empty capture that reads exactly like "the owner did nothing".
    world = verify_capture_world(tab_id, require_boot=False)
    bridge = Bridge(drv.ChromeTab(tab_id))
    bridge.stage()
    if bridge.tab.js("String(typeof window.__cosMut)") != "undefined":
        raise MutationStop("the page driver is in the host's ISOLATED world. "
                           + WRONG_WORLD)
    got = bridge.call("shapes")["out"]
    path = cos.run_ops_dir(vault) / "_cos_mutation_shapes.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    shapes = _merge_shapes(existing.get("shapes"), got.get("shapes"))
    _write_text_atomic(path,
                       json.dumps({"captured_at": _ts(), "lane": MUTATION_LANE,
                                   "shapes": shapes},
                                  indent=2, ensure_ascii=False) + "\n",
                       mode=_OPS_MODE)
    return {"path": str(path), "actions_present": sorted(shapes),
            "capture_world": world,
            "actions_missing": got.get("actions_missing"),
            "fingerprints": _fingerprints(shapes),
            "vault_root_asserted": root}


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
PENDING = "pending-main-session"


def shapes_from_capture(vault: Path, capture: Path, *, port: int = 9222) -> dict[str, Any]:
    """Turn a BROWSER-LEVEL capture into the approved shapes file.

    `capture_shapes` reads the page's own buffer, and on this build that buffer
    never contains a write: the mutation is issued by a dedicated blob worker
    (measured 2026-08-11, corroborated by outlook-tool#3). So the rows come from
    `tools/cos_cdp_capture.py` instead — but the SKELETON, the FINGERPRINT and
    the scrubbing are still computed by `cos_mutate_page.js` itself, in the page,
    so the host and the validator can never drift apart on what a shape is.
    """
    import urllib.parse                                        # noqa: PLC0415
    from brain import cos                                      # noqa: PLC0415
    import cos_cdp_capture as cdp                              # noqa: PLC0415

    root = assert_vault(vault)
    rows = []
    skipped = 0
    for line in capture.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            # A STREAMED capture can end mid-line (the recorder writes each row
            # as it arrives), and concatenating captures joins a partial tail to
            # the next file's head. One bad line is not a reason to lose every
            # good one — but it IS counted, because silently reading half a
            # capture is how a missing shape looks like a missing action.
            skipped += 1
            continue
        if not r.get("action"):
            continue
        raw = None
        for k, v in (r.get("headers") or {}).items():
            if k.lower() == "x-owa-urlpostdata":
                raw = urllib.parse.unquote(v)
        raw = raw or r.get("body")
        if not raw:
            continue
        if isinstance(raw, (dict, list)):
            # AN ALREADY-PARSED ROW. The recorder writes `body` as the raw
            # string, but the chip CLEAR fires from a `blob:` dedicated worker
            # and its payload was extracted into a derived capture carrying the
            # decoded object (FINDING 2026-08-12) — the only on-disk copy of the
            # remove shape. `json.loads` on a dict raises TypeError, not
            # ValueError, so without this the import does not skip the row: it
            # dies on it.
            parsed = raw
        else:
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
        rows.append({"action": r["action"], "ts": r.get("ts") or _ts(),
                     "status": r.get("status"), "parsed": parsed})
    # ONE EXAMPLE PER JOB. A whole capture is hundreds of rows and tens of
    # thousands of characters, and the CDP evaluate that carries it came back
    # TRUNCATED — a silently half-parsed payload is worse than a refusal. The
    # exporter only ever reads the first match per job anyway.
    seen: set[tuple[str, str, bool]] = set()
    trimmed = []
    for r in rows:
        conv = ""
        remove = False
        acts = ((r["parsed"].get("Body") or {}).get("ConversationActions") or [])
        if acts:
            conv = str((acts[0] or {}).get("Action") or "")
            # THE VARIANT IS PART OF THE IDENTITY. The chip's add and its remove
            # are the same action with two accepted payloads (FINDING
            # 2026-08-12); keying the trim on action alone would drop whichever
            # one the owner performed second, and the exporter would never see
            # it.
            remove = isinstance((acts[0] or {}).get("CategoriesToRemove"), list)
        key = (r["action"], conv, remove)
        if key in seen:
            continue
        seen.add(key)
        trimmed.append(r)
    rows = trimmed
    if not rows:
        raise MutationStop(f"{capture} holds no request with a readable payload")

    # STORED, THEN READ IN SLICES. A `Runtime.evaluate` result came back
    # TRUNCATED mid-string at ~634 characters (measured 2026-08-11) and the
    # only reason it was visible is that JSON refused to parse it — the same
    # class of silent transport truncation the DOM bridge already guards
    # against, so it gets the same treatment.
    expr = (PAGE_JS.read_text(encoding="utf-8")
            + ";window.__cosShapes=JSON.stringify("
            + "window.__cosMut.exportShapes({calls:"
            + json.dumps(rows, ensure_ascii=False)
            + ",body:function(c){return c.parsed;}}));window.__cosShapes.length;")
    total = int(cdp.evaluate(expr, port=port))
    chunk = 8000
    parts = [cdp.evaluate(f"window.__cosShapes.substr({off},{chunk})", port=port)
             for off in range(0, total, chunk)]
    raw = "".join(parts)
    if len(raw) != total:
        raise MutationStop(f"the shapes payload came back {len(raw)} of {total} "
                           "characters — a truncated shape is not a shape")
    got = json.loads(raw)

    path = cos.run_ops_dir(vault) / "_cos_mutation_shapes.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    shapes = _merge_shapes(existing.get("shapes"), got.get("shapes"))
    _write_text_atomic(path,
                       json.dumps({"captured_at": _ts(), "lane": MUTATION_LANE,
                                   "capture_point": "cdp-browser-level",
                                   "source_capture": str(capture),
                                   "shapes": shapes},
                                  indent=2, ensure_ascii=False) + "\n",
                       mode=_OPS_MODE)
    return {"path": str(path), "actions_present": sorted(shapes),
            "unparseable_capture_lines": skipped,
            "actions_missing": got.get("actions_missing"),
            "conversation_action": (shapes.get("ApplyConversationAction") or {})
                                   .get("conversation_action"),
            "destination_recorded": bool((shapes.get("ApplyConversationAction") or {})
                                         .get("destination_id")),
            "fingerprints": _fingerprints(shapes),
            "vault_root_asserted": root}


def fault_injection_report() -> dict[str, Any]:
    """RUN the fault-injection suite and report what it returned.

    Not a claim typed into a file: this shells out to the node suite, which
    simulates a response lost AFTER the server accepted the write for every
    verb and asserts the reconciliation query finds the server-side effect.
    """
    script = Path(__file__).resolve().parents[1] / "tests" / "js" \
        / "cos_mutate_page.test.mjs"
    if not script.exists():
        return {"ran": False, "why": f"{script} is missing"}
    proc = subprocess.run(["node", str(script), "--json"], capture_output=True,
                          text=True, timeout=300)
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ran": False, "returncode": proc.returncode,
                "stderr": proc.stderr[-800:], "stdout": proc.stdout[-800:]}
    report["ran"] = True
    report["returncode"] = proc.returncode
    return report


def build_evidence(vault: Path | None, run_id: str | None, *,
                   live: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `mutation-proof` artifact. Live fields are NULL until a live pass
    fills them — an invented value is the failure this whole plan exists to
    prevent, and a null awaiting a real run is honest."""
    e17 = canary_status(vault) if vault else {}
    faults = fault_injection_report()
    plan: dict[str, Any] = {}
    if vault and run_id:
        try:
            p = build_plan(vault, run_id,
                           applied=UndoLedger(vault, run_id).applied_counts())
            plan = {"run_id": run_id, "planned_by_verb": p["planned_by_verb"],
                    "excluded": len(p["excluded"]),
                    "exclusion_reasons": sorted({e["reason"] for e in p["excluded"]}),
                    "computed_read_only": True}
        except MutationStop as exc:
            plan = {"run_id": run_id, "error": str(exc)}
    ev: dict[str, Any] = {
        "session": "s04", "item": "MUT-01",
        "produced_at": _ts(),
        "executor": "main-session (a dispatched subagent is classifier-refused "
                    "on live mailbox mutation — owner ruling 2026-08-08)",
        "build_half": "this artifact",
        "live_half": PENDING if not live else "recorded below",
        "vault_root_asserted": (live or {}).get("vault_root_asserted")
        or (assert_vault(vault) if vault else None),
        "e17": {
            "lane": MUTATION_LANE,
            "lane_match": e17.get("lane_match"),
            "canary_tested_utc": e17.get("canary_tested_utc"),
            "canary_age_days": e17.get("canary_age_days"),
            "receipts_present": e17.get("receipts_present"),
            "valid": e17.get("valid"),
            "why": e17.get("why"),
            "fresh_drill_this_session": None if not live else True,
            "note": ("the fresh rest-lane drill is a LIVE move-and-move-back and "
                     "is the main session's to run; the drill code path, its "
                     "receipt assembly and its refusal to write a receipt-less "
                     "file are built and tested here"),
        },
        "zero_send": {
            "sent_itemid_diff": (live or {}).get("sent_itemid_diff", PENDING),
            "runtime_allowlist_enforced": True,
            "permitted_actions": list(PERMITTED_ACTIONS),
            "disposition_asserted": SAVE_ONLY,
            "permitted_folders": list(PERMITTED_FOLDERS) + [DRAFT_FOLDER],
            "rejected_payloads": ((live or {}).get("runtime") or {})
            .get("rejected", []),
            "enforcement_point": ("tools/cos_mutate_page.js `send()` — the line "
                                  "before `fetch`, in the page's own world, over "
                                  "the payload that is actually about to leave"),
            "source_grep_is_the_second_belt_only": (
                "a source audit greps PYTHON; this engine replays CAPTURED "
                "payloads, so a fixture could carry a sending disposition with "
                "no literal in our source. The allowlist validates the OUTGOING "
                "payload against approved captured-request fingerprints, asserts "
                "the save-only disposition POSITIVELY, and rejects anything else "
                "before dispatch."),
        },
        # An EMPTY list would read as "the pass ran and mutated nothing", which
        # is a different claim from "the pass has not run". Say the second one.
        "mutations": live.get("results", []) if live else PENDING,
        "final_states": live.get("final_states", {}) if live else PENDING,
        "state_machine": list(STATES),
        "fault_injection": faults,
        "draft_autonomous_resume": DRAFT_RESUME_POLICY,
        "http_449_transitions": live.get("http_449_transitions", []) if live
        else PENDING,
        "dispatch_blocked_until_reprime": faults.get(
            "dispatch_blocked_until_reprime"),
        "in_flight_449_outcome": [t.get("in_flight_449_outcome")
                                  for t in (live or {}).get(
                                      "http_449_transitions", [])] or PENDING,
        "mutation_shapes_replayed_not_synthesized": {
            "enforced": True,
            "how": ("every payload is a clone of an APPROVED SKELETON captured "
                    "from the app's own request for that verb, with ids "
                    "substituted at known paths; a verb with no approved shape "
                    "cannot run, and a payload whose key-path fingerprint "
                    "differs from the approved shape is rejected before dispatch"),
            "shapes": (live or {}).get("shape_fingerprints", PENDING),
        },
        "caps": DEFAULT_CAPS,
        "scope": {"default_since_days": DEFAULT_SINCE_DAYS,
                  "note": "the recency window bounds a night; caps are unlimited "
                          "by default (owner ruling 2026-08-11)"},
        "plan_observed": plan,
        "kill_switch": (live or {}).get("kill_switch",
                                        kill_switch(vault) if vault else None),
        "stopped": (live or {}).get("stopped"),
        "seed_fix": {
            "measured_blocker": ("s03: 137 main-world fetch calls across a full "
                                 "310-row scroll, three sort changes, two folder "
                                 "switches and a cold reload, with ZERO "
                                 "action=FindItem — the app fires its FindItem "
                                 "during BOOT and the hook was installed after "
                                 "load, so run 116 died at `seed`"),
            "fix": "tools/cos_capture_hook.js, evaluated at document_start",
            "install": ("CDP Page.addScriptToEvaluateOnNewDocument, an extension "
                        "content script at run_at: document_start, or a "
                        "userscript at @run-at document-start — then RELOAD the "
                        "mail tab so the boot call is captured"),
            "self_reporting": ("`__cosCap.stats().document_start` is true only "
                               "when the hook was in place at readyState "
                               "`loading`; `boot_finditem` says whether a "
                               "replayable envelope actually exists"),
            "verified_against_the_live_page": PENDING,
        },
        "runbook_for_the_live_pass": [
            "export BRAIN_VAULT=$HOME/DeveloperFolder/Brainiac/vault",
            "install tools/cos_capture_hook.js at document_start, reload the "
            "mail tab, and confirm stats().boot_finditem is true",
            "owner performs ONE archive, ONE chip set and ONE draft-save in the "
            "UI, then: python3 tools/cos_mutate.py capture-shapes --tab-id <id>",
            "python3 tools/cos_mutate.py canary --run-id <run> --tab-id <id> "
            "--canary-convid <a disposable thread>   # the fresh E17 drill",
            "python3 tools/cos_mutate.py dry-run --run-id <run> --tab-id <id> "
            "   # read-only; inspect every payload before anything is sent",
            "python3 tools/cos_mutate.py apply --run-id <run> --tab-id <id> "
            "--cap-archive 3 --cap-categorize 5 --cap-draft 2",
            "re-run `evidence` with the live pass output to fill the null fields",
        ],
    }
    return ev


# ---------------------------------------------------------------------------
# selfcheck — the structural properties, provable with no mailbox
# ---------------------------------------------------------------------------
def selfcheck() -> int:
    import tempfile                                              # noqa: PLC0415

    # The state machine's vocabulary is closed.
    with tempfile.TemporaryDirectory() as d:
        vault = Path(d) / "vault"
        (vault / "cos-ops").mkdir(parents=True)
        os.environ.setdefault("BRAIN_VAULT", str(vault))
        led = UndoLedger.__new__(UndoLedger)
        led.path = vault / "cos-ops" / "_cos_undo_ledger_x.jsonl"
        led.run_id = "x"
        try:
            led.append({"idempotency_key": "a|archive", "verb": "archive",
                        "state": "probably-fine"})
            raise AssertionError("an invented state was accepted")
        except MutationStop:
            pass
        led.append({"idempotency_key": "a|archive", "verb": "archive",
                    "state": "intent"})
        led.append({"idempotency_key": "a|archive", "verb": "archive",
                    "state": "reconciled"})
        assert led.latest()["a|archive"]["state"] == "reconciled"
        assert led.applied_counts()["archive"] == 1
        assert led.unfinished() == []

    # A canary with no receipts is a FAIL, whatever else it carries.
    with tempfile.TemporaryDirectory() as d:
        vault = Path(d)
        (vault / "cos-ops").mkdir(parents=True)
        _write_text_atomic(vault / "cos-ops" / "_cos_undo_canary.json", json.dumps(
            {"lanes": {"rest": {"tested": _ts(), "idempotent_replay": "confirmed"}}}))
        st = canary_status(vault)
        assert st["lane_match"] and not st["valid"], st
        assert "receipts" in st["why"]

    print("cos_mutate selfcheck: OK")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=("plan", "dry-run", "rehearsal-gate",
                                       "apply", "undo",
                                       "unchip", "canary",
                                       "hook-stage", "hook-verify",
                                       "shapes-from-capture",
                                       "capture-shapes", "evidence", "selfcheck"))
    p.add_argument("--plan", type=Path, default=None,
                   help="the `plan` command's output. REQUIRED by `apply` and "
                        "read by `dry-run` and `rehearsal-gate`: one plan is "
                        "built once, frozen, and consumed by all three, so "
                        "what is dispatched is what was rehearsed")
    p.add_argument("--rehearsal", type=Path, default=None,
                   help="apply: the `dry-run` output that validated --plan. "
                        "Its `plan_digest` must equal the plan's or nothing "
                        "is dispatched")
    p.add_argument("--dry-run-json", type=Path, default=None,
                   help="rehearsal-gate: the `dry-run` command's output")
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--tab-id", type=int, default=None)
    p.add_argument("--canary-convid", default=None)
    p.add_argument("--undo-limit", type=int, default=None,
                   help="undo/unchip: reverse at most N of this run's archives "
                        "or chips")
    # Caps default to UNLIMITED (owner ruling: content, not a number, bounds a
    # night). A value <= 0 is read as unlimited too, so `--cap-archive 0` and an
    # absent flag mean the same thing. The scope is the recency window below.
    p.add_argument("--cap-archive", type=int, default=None)
    # THE ATTENDED CAP IS AN ABORT, NOT A TRUNCATION, and that difference is
    # the whole point (adversarial review 2026-08-14). `--cap-archive` above
    # EXCLUDES each row past the cap and lets the run proceed with the rest —
    # which is the right shape for a bounded hand-run and exactly the WRONG
    # shape for "stop and come back to the owner". An attended run that plans
    # more archives than the owner authorised must dispatch NOTHING and say how
    # many it wanted, never a truncated prefix that looks like consent.
    p.add_argument("--archive-abort-cap", type=int, default=None,
                   help="plan: REFUSE the whole plan (exit 4, nothing written) "
                        "when it would archive more than N threads. Attended "
                        "runs only; absent ⇒ no abort, and the scheduled "
                        "lane's behaviour is unchanged.")
    p.add_argument("--cap-categorize", type=int, default=None)
    p.add_argument("--cap-draft", type=int, default=None)
    p.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS,
                   help=f"only act on threads received within N days "
                        f"(default {DEFAULT_SINCE_DAYS}; $BRAIN_COS_SINCE_DAYS)")
    p.add_argument("--all", action="store_true",
                   help="historic: lift the recency window, act on the whole "
                        "mailbox regardless of age")
    p.add_argument("--no-require-boot", action="store_true",
                   help="hook-verify: accept a hook installed after load. Legal "
                        "for shape capture only — the mutation lane needs the "
                        "BOOT envelope and refuses without it.")
    p.add_argument("--allow-draft-resume", action="store_true",
                   help=DRAFT_RESUME_POLICY)
    p.add_argument("--cdp", action="store_true",
                   help="drive the page over CDP (main world, addresses one "
                        "browser by port) instead of AppleScript")
    p.add_argument("--capture", default=None,
                   help="a cos_cdp_capture jsonl to build the shapes from")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv[1:])

    if args.command == "selfcheck":
        return selfcheck()

    # NO VAULT, NO BROWSER, NO LEDGER — two files and an exit status. Placed
    # before the vault resolution below on purpose: the nightly gates REAL
    # MUTATIONS on this status, and a gate that needs more than the artifacts
    # it judges is a gate with more ways to fail open.
    if args.command == "rehearsal-gate":
        if args.plan is None or args.dry_run_json is None:
            print("rehearsal-gate needs --plan and --dry-run-json",
                  file=sys.stderr)
            return 2
        rc, reason = rehearsal_gate(args.plan, args.dry_run_json)
        print(reason)
        return rc

    vault = args.vault or Path(os.environ.get("BRAIN_VAULT", "")).expanduser()

    def _cap(v: int | None) -> int | None:
        return None if v is None or v <= 0 else v         # <= 0 ⇒ unlimited
    caps = {"archive": _cap(args.cap_archive),
            "categorize": _cap(args.cap_categorize),
            "draft": _cap(args.cap_draft)}
    since_days = None if args.all else args.since_days

    try:
        if args.command == "plan":
            out = build_plan(vault, args.run_id, caps=caps, since_days=since_days,
                             applied=UndoLedger(vault, args.run_id).applied_counts())
            # STAMPED INTO THE PLAN whether it fires or not, so the artifact the
            # owner approves records the bound he set — a cap recorded nowhere
            # is an abort rule nobody can prove was enforced.
            out["archive_abort_cap"] = args.archive_abort_cap
            want = out["planned_by_verb"]["archive"]
            if args.archive_abort_cap is not None \
                    and want > args.archive_abort_cap:
                print(f"REFUSING the whole mutation lane: this plan would "
                      f"archive {want} thread(s) and the attended cap is "
                      f"{args.archive_abort_cap}. Nothing was written and "
                      f"nothing will be dispatched — an attended cap STOPS the "
                      f"run and comes back to the owner; it never archives a "
                      f"truncated prefix of what he did not approve.",
                      file=sys.stderr)
                return 4
            out["vault_root_asserted"] = assert_vault(vault)
            out["e17"] = canary_status(vault)
            out["kill_switch"] = kill_switch(vault)
        elif args.command == "dry-run":
            out = dry_run(vault, args.run_id, args.tab_id, caps=caps,
                          since_days=since_days, plan_path=args.plan,
                          use_cdp=args.cdp)
        elif args.command == "apply":
            # THE CLI HAS NO UNREHEARSED APPLY (K1). Every production caller —
            # the nightly, and any hand-run resuming it — already has a
            # `plan.json` and a `dry-run.json` in the run's evidence directory,
            # so requiring them costs nothing legitimate and closes the one
            # door through which an unrehearsed payload could reach the
            # mailbox. There is deliberately NO override flag: a knob that
            # turns this off is the hole with a longer name.
            if args.plan is None or args.rehearsal is None:
                raise MutationStop(
                    "apply needs --plan and --rehearsal: the frozen plan and "
                    "the rehearsal that validated it. An apply that plans for "
                    "itself dispatches a payload nothing rehearsed — a P1/add "
                    "plan behind a P3/remove rehearsal used to return ok. Run "
                    "`plan` then `dry-run --plan …`, then apply against both")
            out = apply_pass(vault, args.run_id, args.tab_id, caps=caps,
                             since_days=since_days,
                             allow_draft_resume=args.allow_draft_resume,
                             plan_path=args.plan, rehearsal_path=args.rehearsal,
                             use_cdp=args.cdp)
        elif args.command == "undo":
            out = undo_pass(vault, args.run_id, args.tab_id, use_cdp=args.cdp,
                            limit=args.undo_limit)
        elif args.command == "unchip":
            out = unchip_pass(vault, args.run_id, args.tab_id, use_cdp=args.cdp,
                              limit=args.undo_limit)
        elif args.command == "canary":
            out = canary_drill(vault, args.run_id, args.tab_id,
                               args.canary_convid, use_cdp=args.cdp)
        elif args.command == "shapes-from-capture":
            out = shapes_from_capture(vault, Path(args.capture))
        elif args.command == "hook-stage":
            out = stage_hook(args.tab_id)
        elif args.command == "hook-verify":
            out = verify_capture_world(args.tab_id,
                                       require_boot=not args.no_require_boot)
        elif args.command == "capture-shapes":
            out = capture_shapes(vault, args.tab_id)
        else:
            out = build_evidence(vault if vault and vault.is_dir() else None,
                                 args.run_id)
    except MutationStop as exc:
        print(f"MUTATION STOP: {exc}", file=sys.stderr)
        # A STOP WRITES ITS REPORT TOO (review 2026-08-12). Every `MutationStop`
        # returned 3 having written no `--out` file at all, and `cos_nightly.sh`
        # reads that file to decide whether the night finished — so a kill
        # switch off, an invalid E17 canary or missing approved shapes left no
        # report, no `stopped` field, and a log line reading `done` at exit 0:
        # the exact run-125 symptom the stop detection was added to end. The
        # report is the operator-facing record of a refusal, not only of a
        # completed pass.
        #
        # `results: []` IS TRUE HERE, AND ONLY HERE (review 2026-08-12). It used
        # to be a claim this handler could not support: three bridge calls in
        # `apply_pass` sat outside its `try`, so a timeout in any of them threw
        # past the pass and a night that had applied fifty mutations wrote an
        # artifact asserting zero. Those three now stop the pass from INSIDE and
        # return its real report, so anything reaching here is a refusal that
        # happened before the pass could dispatch anything — kill switch off,
        # invalid E17 canary, missing shapes.
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(args.out, json.dumps(
                {"command": args.command, "run_id": args.run_id,
                 "stopped": str(exc), "stop_class": "mutation-stop",
                 "results": [], "skipped_absent": [], "at": _ts()},
                indent=2, ensure_ascii=False) + "\n", mode=_OPS_MODE)
        return 3

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(args.out,
                           json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                           mode=_OPS_MODE)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
