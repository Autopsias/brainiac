"""Row-stamping sub-steps of `cos_judge.apply_judgment`'s ledger write.

`apply_judgment` owns the loop, the counters and the pending count; this
module owns what happens to ONE row — the pending-row stamp (the rule-8 word
beside the counter, run 135) and the judged-row slot write (verdict slots,
carried keys, the host's archive-eligibility decision written last). Import
direction is one-way: this module imports nothing from its parent — the
archive-eligibility decider arrives as a callable so a test that patches it on
the parent is still honoured.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cos_judge_ingest  # noqa: E402  sibling, not the parent
from brain.cos_echecks import p0_floor_refuses  # noqa: E402  the ONE floor


def pending_row(row: dict[str, Any], refused: bool) -> dict[str, Any]:
    """One row no verdict reached, stamped so the ledger says which of the two
    ways that happened (`judgment-refused` vs `unjudged`)."""
    out = dict(row)
    # STAMPED HERE, not merely inherited. The driver already writes
    # `judgment_pending: true` on every row it emits, but relying on
    # that made `ingestion_pending` a property of the caller: any row
    # reaching this function without the stamp came out as judged-and-
    # held, which is the one reading a half-judged night must never
    # have. Setting it makes apply_judgment's docstring true of the output.
    out["judgment_pending"] = True
    # THE WORD, beside the counter (run 135). `held` is what
    # `ledger_counts` already reads this row as — every in-scope
    # non-candidate row is held — so stamping it moves no total; what
    # changes is that rule 8's slot now carries a word instead of a
    # null. The reason distinguishes the two ways a row gets here, and
    # both are HOST words the model cannot claim.
    out["disposition"] = "held"
    out["held_reason"] = "judgment-refused" if refused else "unjudged"
    # (INGEST-01) A row no verdict reached states its ingest lane too. It is
    # NOT relevant — nothing decided its substance — and saying so is the whole
    # point: an absent field is a row counted by nothing (f270700).
    out["ingest"] = dict(cos_judge_ingest.NOT_RELEVANT)
    return out


def judged_row(out: dict[str, Any], v: dict[str, Any],
               decide_eligibility: Callable[[dict[str, Any]],
                                            tuple[bool, str] | None]
               ) -> dict[str, Any]:
    """One row's validated verdict, written into the driver's null slots."""
    out["verdict"] = v.get("bucket")
    out["category"] = v.get("category")
    out["disposition"] = v.get("disposition")
    out["held_reason"] = v.get("held_reason")
    out["dedup_check"] = v.get("dedup_check")
    out["candidate_count"] = 1 if v.get("disposition") == "candidate" else 0
    # (INGEST-01) THE EXPLICIT FIELD, derived in `_prepared_verdict` and carried
    # here verbatim. It rides ABOVE the archive decision below on purpose: the
    # host's `auto_archive` widening runs last and must never be able to move
    # it, because an archived thread's substance is worth exactly what a held
    # thread's is.
    out["ingest"] = (dict(v["ingest"]) if isinstance(v.get("ingest"), dict)
                     else dict(cos_judge_ingest.NOT_RELEVANT))
    out["proposal_id"] = v.get("proposal_id")
    out["content_sha256"] = v.get("content_sha256")
    out["judgment_pending"] = False
    # The JUDGED tier, kept beside the chip-derived `tier` rather than over
    # it: `tier` answers "what chip is on this thread" (the archive screens
    # depend on that reading) and `judged_tier` answers "what the judge says
    # it is worth". The chip lane needs both — it writes a chip only where
    # the judge named a tier and the thread carries none.
    out["judged_tier"] = v.get("tier")
    # `proposals_dropped` rides here so the LEDGER carries it: it is what
    # tells `check_candidate_stamps` that this run's candidates name no
    # drop, and a control that cannot apply must be able to say so from the
    # artifact alone.
    for k in ("hold_category", "hold_verdict", "classification",
              "substance_kind", "dedup_kind", "merge_candidate",
              "evidence_span", "auto_archive", "noise_signal",
              # (STALE-01) `stale` rides to the LEDGER because three readers
              # need it there: `archive_eligibility` below, the planner's
              # second belt, and the brief's `stale, archived` list
              # (`cos_judge_brief.compose_brief`, whose `stale_archived` set
              # `cos_judge.judge_night` reads back off these very rows).
              "stale", "proposals_dropped",
              # THE THIRD OUTCOME (owner ruling 2026-08-28). The word the
              # drafting leg returned for a thread it would not draft, carried
              # to the LEDGER because that is what the MORNING SHEET reads
              # (`brain.cos.sheet._run_rows`). A word that stops at the brief
              # reaches the one artifact the owner does not open first.
              "needs_owner"):
        if v.get(k) is not None:
            out[k] = v[k]
    # THE ARCHIVE DECISION IS THE HOST'S (DOCTRINE v7 §4.2), written LAST
    # so it wins over the model's own `auto_archive`/`noise_signal` claim
    # on exactly the population the owner ruled on. It only ever ADDS
    # eligibility: a row it declines to mark keeps whatever the judge said
    # and whatever `validate_verdict` already accepted, so the older
    # `recurring-automated-sender` lane is untouched (and, being read
    # `noise` below P1, is a strict subset of this one anyway).
    elig = decide_eligibility(out)
    if elig is not None:
        out["auto_archive"], out["noise_signal"] = elig
    return out


# ---------------------------------------------------------------------------
# batch-2 drain: the apply pipeline moved verbatim out of `cos_judge` and is
# re-imported by it — `judge_night` calls `apply_judgment` through the
# parent's globals exactly as before.
# ---------------------------------------------------------------------------
import sys                                                    # noqa: E402
from pathlib import Path                                      # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_judge_rules import READ_NOISE_SIGNAL              # noqa: E402
from cos_judge_rules_stale import (  # noqa: E402
    STALE_ACT_SIGNAL, STALE_REASONS)


JUDGMENT_SLOTS = ("verdict", "category", "disposition", "held_reason",
                  "dedup_check", "candidate_count", "proposal_id",
                  "content_sha256")


def archive_eligibility(row: dict[str, Any]) -> tuple[bool, str] | None:
    """DOCTRINE v7 §4.2 — the HOST decides archive eligibility, from facts.

    Returns ``(True, <typed signal>)`` for a row a ruling makes
    archive-eligible — ``READ_NOISE_SIGNAL`` on the read/noise lane,
    ``STALE_ACT_SIGNAL`` on the stale-act lane (STALE-01) — or ``None`` when
    neither has anything to say (the row keeps whatever the judge claimed,
    validated as before).

    THE HOST IS THE PRODUCER, AND THAT IS THE POINT. Leaving the widening to a
    model flag would have shipped a policy the run can decline to apply: on run
    136 the model set `auto_archive: true` on ONE of 57 `noise` rows, so
    "archive-eligible = read + bucket noise" would have archived one thread and
    read as delivered. Both inputs here are already on the ledger row before
    any judgment: `verdict` is the bucket the judge chose (that IS the
    disposition the owner ruled on) and `read_state` is the driver's own
    enumeration of the mailbox.

    THREE FLOORS, NONE OF THEM RELAXED:
      * `read_state` must literally be `read` — the UNREAD SHIELD (§2.2);
      * `judged_tier` P0 is refused OUTSIDE the `read` bucket —
        `cos_echecks.p0_floor_refuses`, the one definition of the blast floor
        (owner ruling 2026-09-04: a thread with no action for him archives
        whatever its priority; a P0 still in `act` keeps the floor);
      * the row must carry a real verdict — an unjudged row is never eligible.
    `tools/cos_mutate.build_plan` re-screens all three INDEPENDENTLY off the
    same ledger (plus the observed-chip floor), so this is the first of two
    belts, never the only one.

    THE STALE-ACT LANE TAKES THE SAME THREE FLOORS AND ONE MORE: it fires only
    on the judge's own `stale` field, and the rule `triage.stale_evidence`
    already refused that claim at validation time against the host's ask
    detector, the host's deadline parse, the opened body, the spine and the
    host's draft census. That rule keys on THE FIELD, not on the model's
    `auto_archive` claim, and it has to: this function archives off the field
    alone, so a belt that only ran when the model asked to archive left the
    trigger unexamined (review 2026-08-25, finding 1). This function names the
    signal; it does not decide the staleness.
    """
    if row.get("judgment_pending"):
        return None
    if row.get("read_state") != "read":
        return None
    if p0_floor_refuses(row.get("verdict"), row.get("judged_tier")):
        return None
    # (STALE-01) THE SECOND LANE, and it sits inside the SAME three floors on
    # purpose — the unread shield, the P0 refusal and the real-verdict
    # requirement are what bound both. An `act` thread archives only on the
    # judge's own `stale` field, whose evidence `stale_act_refusal` already
    # refused off facts the model does not control; here the host merely names
    # the typed signal that carries it through the planner, E3 and the sheet.
    if row.get("verdict") == "act":
        st = row.get("stale")
        if isinstance(st, dict) and st.get("is_stale") is True \
                and st.get("reason") in STALE_REASONS:
            return True, STALE_ACT_SIGNAL
        return None
    if row.get("verdict") != "noise":
        return None
    return True, READ_NOISE_SIGNAL


def apply_judgment(rows: list[dict[str, Any]],
                   verdicts: dict[str, dict[str, Any]],
                   refused: set[str] | None = None) -> dict[str, Any]:
    """Write validated verdicts into the ledger's null slots.

    A row with no verdict keeps `judgment_pending: true` and is COUNTED as such.
    Half-judged is a legitimate state; a half-judged night that reads as a
    complete one is not.

    AND IT SAYS SO IN THE SLOT, not only in the counter (run 135). Such a row
    used to reach the ledger with `disposition: null` and `held_reason: null`:
    `ledger_counts` still counted it as held (every non-candidate row is), so
    the ARITHMETIC was honest while the WORD was absent — and
    `check_ledger_vocabulary` reads an absent word as an invented one and fails
    the whole run. Run 135 applied 41 of 41 mutations, reconciled every one, and
    scored INVALID on nine such rows. The row is now stamped `held` (the
    disposition `ledger_counts` already implied, so no counter moves) plus a
    HOST-ONLY reason naming which of the two things happened:

      `judgment-refused` — a verdict arrived and the host would not use it
                           (`validate_verdict` rejected it, or H3 dropped a
                           conflicting duplicate). `refused` carries those ids.
      `unjudged`         — no verdict arrived for this row at all.

    Both words live in `cos_runverify._HOST_HELD_REASONS`, deliberately NOT in
    the model-facing `_HELD_REASONS`: the model is never offered them and is
    still refused if it claims one.
    """
    refused = refused or set()
    judged = []
    pending = 0
    for row in rows:
        v = verdicts.get(row["conversation_id"])
        if not v:
            pending += 1
            judged.append(pending_row(
                row, row["conversation_id"] in refused))
            continue
        judged.append(judged_row(
            dict(row), v, archive_eligibility))
    # THE ONE DEFINITION, imported rather than restated. `ingestion_held` is
    # every in-scope row that is not a staged candidate — including a row no
    # verdict reached — so `in_scope == candidates + held` is an identity. A
    # second local copy of that arithmetic is what run 64, 105 and 108 each
    # got wrong, and what made run 121's judgment unappendable.
    from brain import cos_runverify                           # noqa: PLC0415
    counters = dict(cos_runverify.ledger_counts(judged))
    # Reported BESIDE the identity, never inside it: a half-judged night is a
    # legitimate state and must be legible as one, not inferred from a gap.
    counters["ingestion_pending"] = sum(1 for r in judged
                                        if r.get("judgment_pending"))
    return {"rows": judged, "counters": counters, "judgment_pending": pending}
