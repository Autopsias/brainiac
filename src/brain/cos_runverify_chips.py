"""Chip/hold ledger readers and the chip re-evaluation draw check."""
from __future__ import annotations

import re
from typing import Any

from . import cos

# -- Phase 1.5f: the cycling set, recounted ------------------------------------

#: Dispositions a Phase-1.5f row carries. A `held` row is IN the batch and
#: deliberately carries NO stamp (E26 v5.13: an unscreened chip must come back
#: to the front of the queue), so it is drawn-but-unstamped, never both.
def chip_rows(vault, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(cos.run_ops_dir(vault) / f"_cos_chip_ledger_{run_id}.jsonl")


def hold_rows(vault, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(cos.run_ops_dir(vault) / f"_cos_hold_ledger_{run_id}.jsonl")


def _written_before(name: str, run_id: str) -> bool:
    """Was ``name`` written by a run that preceded ``run_id``?

    Run NUMBER first (they ascend, and three runs share 2026-08-09), the date in
    the filename only when one side carries no run number — a ledger a LATER run
    wrote must never contribute a stamp to an earlier run's recount, or
    re-scoring run 102 today would grade it against run 104's work.
    """
    a, b = _RUN_NUMBER_RE.search(name), _RUN_NUMBER_RE.search(run_id)
    if a and b:
        return int(a.group(1)) < int(b.group(1))
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return bool(m) and m.group(1) < run_id[:10]


def prior_reeval_stamps(vault, run_id: str) -> dict[str, str]:
    """The stamp of record per conversation, from THIS VAULT'S OWN ledgers.

    E26(a)/(j): the ordering is computed from the vault's chip ledgers, and a
    conversation's stamp is the LATEST ``last_reeval`` any earlier run wrote for
    it. A conversation with no row here has never been re-evaluated and sorts at
    epoch 0 — ahead of every dated entry.
    """
    ops = cos.run_ops_dir(vault)
    out: dict[str, str] = {}
    for path in sorted(ops.glob("_cos_chip_ledger_*.jsonl")) + \
            sorted(ops.glob("_chip_reeval_*.jsonl")):
        if run_id in path.name or not _written_before(path.name, run_id):
            continue
        for r in _read_jsonl(path):
            if not _is_reeval_row(r):
                continue
            cid = str(r.get("conversation_id") or "")
            stamp = str(r.get("last_reeval") or "")
            if cid and stamp and stamp > out.get(cid, ""):
                out[cid] = stamp
    return out


def check_chip_reeval_draw(run_id: str, batch: list[dict[str, Any]],
                           held: list[dict[str, Any]], prior: dict[str, str],
                           *, bundle: str = "") -> dict[str, Any]:
    """(c7) The chip re-evaluation batch IS the head of the cycling queue.

    RETIRED from the scored bar (s08, 2026-08-16) — see ``RETIRED_CONTROLS``.

    WHY THIS EXISTS (measured, runs 100/103/104, 2026-08-08..09). E26(a) has
    required an oldest-``last_reeval``-first draw since v5.5 and never once got
    one. Run 104 re-evaluated the IDENTICAL twenty conversations run 102 had
    evaluated nine hours earlier, while 234 held-and-chipped conversations had
    NEVER been stamped at all and so, under E26(a)'s epoch-0 rule, owned every
    slot in that batch. Same shape on runs 100 and 103. Three occurrences.

    THE DEFECT IS THE POPULATION, NOT THE COMPARATOR — which is why this
    recounts the SET and not just the order. Both runs 103 and 104 reported the
    denominator ``33``, and 33 is exactly the number of DISTINCT CONVERSATIONS
    THE PHASE HAD ALREADY EVALUATED (``|run100 ∪ run102|`` = ``|run102 ∪
    run103|`` = 33, the same set both times). Enumerating candidates from the
    ``last_reeval`` STAMPS is self-referential: a never-stamped thread has no
    stamp row to find, so it can never enter the list, rule 1's epoch-0 clause
    becomes unreachable, and the queue ping-pongs forever among the threads it
    has already drawn. Six runs, 120 stamp events, 53 distinct conversations,
    and the backlog RTG-01 exists to drain untouched.

    So the population is the run's OWN held-and-chipped census (tonight's
    ``_cos_hold_ledger_``), never the stamp file, and this check FAILS a batch
    that is not that population's head. The run's prose is not an input.

    WHAT IT DOES NOT CLAIM. Which of the never-stamped threads a cold start
    picked is NOT recountable here — the hold ledger carries no ``received``, so
    E26(a)'s oldest-``received``-then-``conversation_id`` tiebreak has no source
    on this surface. The set-level invariant is what is decidable and it is what
    every measured failure violates: while never-stamped threads remain, a
    stamped one may not be drawn.
    """
    state = _chip_draw_state(batch, held, prior)
    drawn_rows, drawn = state["drawn_rows"], state["drawn"]
    if not drawn:
        return _row("chip_reeval_draw", PASS,
                    f"no Phase-1.5f row in this run's chip ledger "
                    f"({len(batch)} row(s)) — no draw to recount (a phase that "
                    "owed a batch and wrote none is E26's run-obligation, "
                    "scored on the self-eval, not here)",
                    reexecuted=True)

    missing_row = _chip_missing_census_row(state)
    if missing_row:
        return missing_row

    never = state["never"]
    population = state["population"]

    if len(never) >= len(drawn):
        return _chip_epoch0_verdict(state, held, prior, bundle)

    left_behind_row = _chip_left_behind_row(state)
    if left_behind_row:
        return left_behind_row

    # Fewer unstamped threads than slots: the remainder goes to the OLDEST
    # stamps. Ties are inclusive — a whole cohort shares one stamp, and picking
    # any member of it is a correct draw.
    late_row = _chip_late_stamp_row(state, prior)
    if late_row:
        return late_row

    needed = len(drawn) - len(never)
    return _denominator_row(
        drawn_rows, len(population), bundle,
        f"{len(drawn)} thread(s) drawn from a population of "
        f"{len(population)} held-and-chipped conversation(s): all "
        f"{len(never)} never-stamped thread(s) first, the remaining "
        f"{needed} slot(s) filled from stamps no newer than the cutoff")

# Parent/IO binds, deferred past this module's own defs.
from .cos_runverify import (  # noqa: E402
    DEGRADED as DEGRADED,
    FAIL as FAIL,
    INCONCLUSIVE as INCONCLUSIVE,
    PASS as PASS,
    _NON_IDENTITY_EVENTS as _NON_IDENTITY_EVENTS,
    _RUN_NUMBER_RE as _RUN_NUMBER_RE,
    _chip_draw_state as _chip_draw_state,
    _row as _row,
)
from .cos_runverify import (  # noqa: E402
    _attempt_problems as _attempt_problems,
    _cascade_problems as _cascade_problems,
    _chip_epoch0_verdict as _chip_epoch0_verdict,
    _chip_late_stamp_row as _chip_late_stamp_row,
    _chip_left_behind_row as _chip_left_behind_row,
    _chip_missing_census_row as _chip_missing_census_row,
    _declares as _declares,
    _denominator_row as _denominator_row,
    _forged_refusal_problems as _forged_refusal_problems,
    _forged_unreachable_row as _forged_unreachable_row,
    _identity_partitions as _identity_partitions,
    _is_reeval_row as _is_reeval_row,
    _mislabel_problems as _mislabel_problems,
    _missing_pre_problem_row as _missing_pre_problem_row,
    _post_mismatch_mutation_row as _post_mismatch_mutation_row,
    _recovery_problems as _recovery_problems,
    _refusal_problem_row as _refusal_problem_row,
    _undetected_problem_row as _undetected_problem_row,
)
from .cos_runverify_io import _read_jsonl as _read_jsonl  # noqa: E402
