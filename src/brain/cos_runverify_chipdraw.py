"""Sub-steps of the run validator's CHIP RE-EVALUATION DRAW check (s16).

One function per E-check sub-step of ``check_chip_reeval_draw`` (c7): the
draw/held/never-stamped slices, the census-degraded branch, the epoch-0
verdict and the E26(j) denominator recount. The check function itself stays
in :mod:`brain.cos_runverify` with an unchanged signature; this module never
imports it.
"""
from __future__ import annotations

import re
from typing import Any

from .cos_runverify_checks import DEGRADED, FAIL, PASS, _row

#: The chip-ledger dispositions that mark a Phase-1.5f re-evaluation (see
#: ``_is_reeval_row`` — Phase-1.5d RE-LEVEL rows carry neither).
_REEVAL_DISPOSITIONS = {"reevaluated", "held"}


# -- c7 chip re-evaluation draw ------------------------------------------------

def _is_reeval_row(row: dict[str, Any]) -> bool:
    """Is this chip-ledger row a Phase-1.5f re-evaluation?

    ``_cos_chip_ledger_*`` also carries Phase-1.5d RE-LEVEL rows (run 72 wrote
    38, run 74 fifty), which are not draws from the cycling queue and must not
    be counted as one. A 1.5f row is the one that carries a re-eval disposition
    or a ``last_reeval`` stamp field.
    """
    return (str(row.get("disposition") or "") in _REEVAL_DISPOSITIONS
            or "last_reeval" in row or "previous_last_reeval" in row)


def _bundle_at_least(bundle: str, want: tuple[int, int]) -> bool:
    """Does the manifest's ``bundle_version`` claim ``want`` or later?

    The same version gate ``_declares_v551`` applies to a ledger row, read off
    the MANIFEST instead — a chip-reeval row carries no ``bundle_version``, and
    the manifest is the artifact that froze which doctrine actually ran.
    """
    m = re.search(r"v?(\d+)\.(\d+)", str(bundle or ""))
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= want


def _denominator_row(drawn_rows: list[dict[str, Any]], recount: int,
                     bundle: str, detail: str) -> dict[str, Any]:
    """E26(j): the run STATES the population it drew from, and the host recounts it.

    A denominator nobody can recompute is how ``33`` survived three runs while
    the real population was 287: it was reported in prose, in a self-eval the
    run wrote about itself, and nothing on the host could disagree with it.
    """
    reported = {r["cycling_population"] for r in drawn_rows
                if isinstance(r.get("cycling_population"), int)
                and not isinstance(r.get("cycling_population"), bool)}
    if not reported:
        if _bundle_at_least(bundle, (5, 54)):
            return _row("chip_reeval_draw", FAIL,
                        f"{detail} — but this run's bundle ({bundle}) carries "
                        "E26(j) and its chip ledger states no "
                        "`cycling_population`. A denominator that cannot be "
                        "recomputed from the vault's own ledgers is a FAIL: "
                        f"the host recounts {recount}, and runs 103 and 104 "
                        "both reported 33",
                        reexecuted=True)
        return _row("chip_reeval_draw", DEGRADED,
                    f"{detail}. The run states no `cycling_population`, so the "
                    "denominator it reported in prose cannot be checked — its "
                    f"bundle ({bundle or 'unknown'}) predates E26(j), and a run "
                    "is not retro-failed against a field its own doctrine never "
                    f"named. The host's own recount is {recount}",
                    reexecuted=True)
    if len(reported) > 1:
        return _row("chip_reeval_draw", FAIL,
                    f"{detail} — but the chip ledger states "
                    f"{len(reported)} different `cycling_population` values "
                    f"({sorted(reported)}); one draw has one denominator",
                    reexecuted=True)
    stated = reported.pop()
    if stated != recount:
        return _row("chip_reeval_draw", FAIL,
                    f"{detail} — but the run states it drew from "
                    f"{stated} conversation(s) and the host recounts "
                    f"{recount} from this run's own hold ledger. A denominator "
                    "that does not survive a recount is E26(j)'s whole point. "
                    "THE DEFINITION, and it is the only one: the population is "
                    "the count of DISTINCT `conversation_id` in this run's own "
                    "`_cos_hold_ledger_<run_id>.jsonl` (union the threads this "
                    "phase drew, which the ledger already contains). NO ROW IS "
                    "FILTERED OUT — not by `held_category`, and above all not "
                    "by `held_reason`: a thread held because tonight's browser "
                    "broke is a held thread. Measured, run 109 "
                    "(2026-08-10): 301 hold rows, of which 51 carried "
                    "`safety-hold: body-pass-visibility-control-unavailable` "
                    "and were dropped from the count, giving the 250 the run "
                    "reported against the host's 301",
                    reexecuted=True)
    source = ""
    for r in drawn_rows:
        source = str(r.get("cycling_population_source") or "").strip()
        if source:
            break
    if not source:
        if _bundle_at_least(bundle, (5, 54)):
            return _row("chip_reeval_draw", FAIL,
                        f"{detail}, and the stated `cycling_population` "
                        f"{stated} survives the recount — but no row names "
                        "`cycling_population_source`, so HOW the set was "
                        "enumerated is unrecorded, which is the half of E26(j) "
                        "that catches a right number derived the wrong way",
                        reexecuted=True)
        return _row("chip_reeval_draw", DEGRADED,
                    f"{detail}, and the stated `cycling_population` {stated} "
                    "survives the recount, but the run names no "
                    "`cycling_population_source`",
                    reexecuted=True)
    return _row("chip_reeval_draw", PASS,
                f"{detail}. Stated `cycling_population` {stated} survives the "
                f"host recount, derived from {source!r}",
                reexecuted=True)


def _chip_draw_state(batch: list[dict[str, Any]], held: list[dict[str, Any]],
                     prior: dict[str, str]) -> dict[str, Any]:
    """The drawn/held/never-stamped slices one draw is scored against."""
    drawn_rows = [r for r in batch if _is_reeval_row(r)]
    drawn = list(dict.fromkeys(str(r.get("conversation_id") or "")
                               for r in drawn_rows if r.get("conversation_id")))
    held_ids = {str(r.get("conversation_id") or "") for r in held}
    held_ids.discard("")
    missing = [c for c in drawn if c not in held_ids]
    population = held_ids | set(drawn)
    never = [c for c in population if c not in prior]
    stamped_drawn = [c for c in drawn if c in prior]
    return {"drawn_rows": drawn_rows, "drawn": drawn, "held_ids": held_ids,
            "missing": missing, "population": population, "never": never,
            "stamped_drawn": stamped_drawn}


def _chip_missing_census_row(state: dict[str, Any]) -> dict[str, Any] | None:
    """The hold ledger does not even contain the batch it is a census of."""
    missing, drawn, held_ids = state["missing"], state["drawn"], state["held_ids"]
    if not missing:
        return None
    # ponytail: the hold ledger is the only chipped census a run writes.
    # When it does not even contain the batch it is not one, and a
    # population recounted from it would be fiction. The OUTCOME CONTRACT
    # is what catches a hold ledger that under-reports (archive : hold :
    # drafted must equal the enumeration), so this degrades rather than
    # inventing a second census.
    return _row("chip_reeval_draw", DEGRADED,
                f"this run's hold ledger ({len(held_ids)} conversation(s)) "
                f"does not contain {len(missing)} of the {len(drawn)} "
                "thread(s) the chip ledger says were re-evaluated, so it is "
                "not a census of the chipped set and the cycling "
                "POPULATION cannot be recounted from it — the draw is taken "
                "on the run's word",
                reexecuted=False)


def _chip_epoch0_verdict(state: dict[str, Any], held: list[dict[str, Any]],
                         prior: dict[str, str], bundle: str) -> dict[str, Any]:
    """Never-stamped threads remain: they own every slot in the batch."""
    drawn_rows, drawn, population = (state["drawn_rows"], state["drawn"],
                                     state["population"])
    never, stamped_drawn = state["never"], state["stamped_drawn"]
    if stamped_drawn:
        oldest = min(prior[c] for c in stamped_drawn)
        return _row("chip_reeval_draw", FAIL,
                    f"{len(stamped_drawn)} of the {len(drawn)} thread(s) "
                    f"drawn had ALREADY been re-evaluated (oldest such "
                    f"stamp {oldest}) while {len(never)} of the "
                    f"{len(population)} held-and-chipped conversation(s) "
                    "have NEVER been stamped — a never-reeval'd thread "
                    "sorts at epoch 0 and owns every slot in this batch "
                    "(E26(a)/(j)). This is the run-104 shape: the "
                    "population was enumerated from the `last_reeval` "
                    "stamps, which only the already-drawn threads have, so "
                    "the queue re-draws its own head and the backlog never "
                    "cycles",
                    reexecuted=True)
    # Say which it is, rather than asserting the ceiling from memory: run
    # 109's hold ledger carries `received` on all 301 rows, and its report
    # still claimed "the hold ledger lacks the received evidence" because
    # THIS STRING said so unconditionally.
    tiebreak = ("(Which unstamped threads were picked is not recountable: "
                "this run's hold ledger carries no `received` for E26(a)'s "
                "cold-start tiebreak)"
                if not any(r.get("received") for r in held) else
                "(This run's hold ledger DOES carry `received`, so E26(a)'s "
                "cold-start tiebreak is recountable from it — no check "
                "scores it yet, and no run may claim the evidence is "
                "missing)")
    return _denominator_row(
        drawn_rows, len(population), bundle,
        f"{len(drawn)} thread(s) drawn, every one never previously "
        f"re-evaluated, from a population of {len(population)} "
        f"held-and-chipped conversation(s) of which {len(never)} are "
        f"unstamped — the epoch-0 head. {tiebreak}")


def _chip_left_behind_row(state: dict[str, Any]) -> dict[str, Any] | None:
    """Fewer unstamped than drawn, but some were still passed over."""
    never, drawn, population = state["never"], state["drawn"], state["population"]
    left_behind = [c for c in never if c not in drawn]
    if not left_behind:
        return None
    return _row("chip_reeval_draw", FAIL,
                f"{len(left_behind)} never-re-evaluated conversation(s) "
                f"were left behind by a batch of {len(drawn)} that had room "
                f"for them — every one of the {len(never)} unstamped "
                f"thread(s) in this run's population of {len(population)} "
                "sorts at epoch 0, ahead of any dated stamp (E26(a)/(j))",
                reexecuted=True)


def _chip_late_stamp_row(state: dict[str, Any],
                         prior: dict[str, str]) -> dict[str, Any] | None:
    """The remainder went to stamps no newer than the cutoff."""
    drawn, never, stamped_drawn = (state["drawn"], state["never"],
                                   state["stamped_drawn"])
    # Fewer unstamped threads than slots: the remainder goes to the OLDEST
    # stamps. Ties are inclusive — a whole cohort shares one stamp, and picking
    # any member of it is a correct draw.
    needed = len(drawn) - len(never)
    oldest_first = sorted(prior[c] for c in state["population"] if c in prior)
    cutoff = oldest_first[needed - 1]
    late = [c for c in stamped_drawn if prior[c] > cutoff]
    if not late:
        return None
    return _row("chip_reeval_draw", FAIL,
                f"{len(late)} thread(s) in this batch carry a "
                f"`last_reeval` NEWER than the {needed}th-oldest stamp in "
                f"the population ({cutoff}) — the batch is not the head of "
                "the queue, and the threads it skipped stay skipped "
                "(E26(a)/(j))",
                reexecuted=True)
