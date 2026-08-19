"""The E6-E10 run-integrity checks and the grounding-delivery helpers."""
from __future__ import annotations

import re
from typing import Any, Callable

from . import cos
from . import cos_echecks_delivery as delivery

_TERMINAL = ("reconciled", "confirmed", "excluded", "stopped", "skipped")


def _e6(run: dict[str, Any]) -> dict[str, Any]:
    plan = run["plan"] if isinstance(run["plan"], dict) else None
    binding = run["binding"] if isinstance(run["binding"], dict) else None
    disp = dispatched(run["undo"])
    planned = list((plan or {}).get("mutations") or [])
    of = "row(s) in the frozen plan"
    stopped = ((run["apply"] or {}).get("stopped")
               if isinstance(run["apply"], dict) else None)
    if not disp and not planned and stopped:
        return _answer(6, NA, 0, of,
                       f"the apply lane never ran ({str(stopped)[:120]})")
    if binding is None or plan is None:
        return _answer(6, FAIL, len(planned), of,
                       "this run recorded mutations with no readable plan "
                       "binding or frozen plan — an absent binding on a run "
                       "that mutated is a FAIL, not a quiet night")
    problems = []
    if binding.get("source") != "frozen":
        problems.append(f"binding source is {binding.get('source')!r}, not "
                        "`frozen`")
    if binding.get("plan_digest") != plan.get("plan_digest"):
        problems.append("the binding's digest does not match the plan it names")
    if binding.get("planned") != len(planned):
        problems.append(f"the binding claims {binding.get('planned')} planned "
                        f"row(s), the plan carries {len(planned)}")
    keys = {(m.get("conversation_id"), m.get("verb")) for m in planned}
    states = {}
    for r in run["undo"]:
        states.setdefault((r.get("conversation_id"), r.get("verb")), set()
                          ).add(str(r.get("state")))
    unterminated = [k for k in keys
                    if not (states.get(k, set()) & set(_TERMINAL))]
    strangers = sorted({str(r.get("conversation_id_digest") or "?")
                        for r in disp
                        if (r.get("conversation_id"), r.get("verb")) not in keys})
    if unterminated:
        problems.append(f"{len(unterminated)} planned row(s) reached no "
                        "terminal state")
    if strangers:
        problems.append(f"{len(strangers)} dispatched row(s) name a "
                        f"conversation the frozen plan never did: {strangers[:6]}")
    if problems:
        return _answer(6, FAIL, len(planned), of, "; ".join(problems))
    return _answer(6, PASS, len(planned), of,
                   f"the binding re-hashes to the frozen plan "
                   f"({str(plan.get('plan_digest'))[:12]}…), all {len(planned)} "
                   f"planned row(s) reached a terminal state, and no ledger row "
                   "names a conversation the plan did not")


def _e7(run: dict[str, Any]) -> dict[str, Any]:
    ledger = run["ledger"]
    of = "enumerated in-scope conversation(s)"
    judgment = run["judgment"] if isinstance(run["judgment"], dict) else None
    reported = ((judgment or {}).get("run_facts") or {}).get("category_gate")
    if not ledger:
        return _answer(7, NA, 0, of,
                       "the night stopped before enumeration, so there was no "
                       "gate to arm")
    if not isinstance(reported, dict):
        return _answer(7, FAIL, len(ledger), of,
                       "the run reported no `run_facts.category_gate`, so there "
                       "is nothing to recompute it against")
    # THE DRIVER'S OWN PREDICATE, RE-RUN — never a second spelling of it. Two
    # spellings is the exact defect `category_gate_state` was written to end:
    # the driver armed on `categories is not None` and the judge on
    # `categories`, so one run reported `armed` from one leg and `not-run` from
    # the other. A check that re-derives the state with its own arithmetic
    # would be a THIRD.
    stamps = run.get("category_stamps")
    driver = _cos_driver()
    if driver is None or stamps is None:
        return _answer(7, FAIL, len(ledger), of,
                       "the category answer this run bound, or the driver that "
                       "owns the gate predicate, is not on disk — so the "
                       "reported state cannot be recomputed, and an "
                       "unverifiable claim is not a pass")
    taxonomy = (cos.ingest_taxonomy(vault_of(run)) or {}).get("rules") or {}
    recomputed = driver.category_gate_state(
        stamps, (r["conversation_id"] for r in ledger), taxonomy)
    differs = [k for k in ("state", "in_scope", "unstamped_in_scope",
                           "undefined_ids")
               if reported.get(k) != recomputed.get(k)]
    if differs:
        return _answer(7, FAIL, len(ledger), of,
                       f"the reported category gate disagrees with the "
                       f"recomputation on {differs}: reported "
                       f"{ {k: reported.get(k) for k in differs} }, recomputed "
                       f"{ {k: recomputed.get(k) for k in differs} }")
    excluded = sum(1 for r in ledger if r.get("category_gate_excluded"))
    return _answer(7, PASS, len(ledger), of,
                   f"the reported state `{reported.get('state')}` equals the "
                   f"state recomputed host-side by the driver's own predicate "
                   f"over this run's enumeration, stamps and taxonomy "
                   f"({recomputed.get('unstamped_in_scope')} unstamped, "
                   f"{len(recomputed.get('undefined_ids') or [])} "
                   f"taxonomy-undefined, {excluded} excluded before the draw)")


def _e8(run: dict[str, Any]) -> dict[str, Any]:
    from . import cos_runverify as rv                            # noqa: PLC0415
    opened = [r for r in run["ledger"] if r.get("body_opened")]
    of = "row(s) with `body_opened: true`"
    if not opened:
        return _answer(8, NA, 0, of, "this run opened no body")
    allowed = rv._HELD_REASONS | rv._HOST_HELD_REASONS
    silent = [r for r in opened
              if r.get("disposition") in (None, "")
              and not r.get("candidate_count")]
    bad = [str(r.get("held_reason")) for r in opened
           if r.get("disposition") not in ("candidate", None, "")
           and str(r.get("held_reason") or "") not in allowed]
    if silent or bad:
        return _answer(8, FAIL, len(opened), of,
                       (f"{len(silent)} body-opened row(s) carry a null "
                        "disposition — answered by silence; " if silent else "")
                       + (f"held_reason(s) outside the managed set: "
                          f"{sorted(set(bad))[:8]}" if bad else ""))
    return _answer(8, PASS, len(opened), of,
                   f"all {len(opened)} body-opened row(s) carry a candidate or "
                   "a disposition with a `held_reason` from the managed set")


def _e9(run: dict[str, Any]) -> dict[str, Any]:
    scope = [r for r in run["ledger"] if in_scope(r)]
    of = "in-scope row(s) (`act`, plus `read` at P0/P1)"
    if not scope:
        return _answer(9, NA, 0, of,
                       "no row reached the Phase-1.6 in-scope population")
    undisposed = [r for r in scope if not r.get("disposition")]
    judgment = run["judgment"] if isinstance(run["judgment"], dict) else None
    cov = (judgment or {}).get("model_coverage") or {}
    floor = (judgment or {}).get("model_coverage_floor")
    fraction = cov.get("fraction")
    problems = []
    if undisposed:
        problems.append(f"{len(undisposed)} in-scope row(s) carry no disposition")
    if not isinstance(fraction, (int, float)):
        problems.append("the run recorded no model coverage to score")
    elif isinstance(floor, (int, float)) and fraction < floor:
        problems.append(f"model coverage {fraction:.4f} is below the floor "
                        f"{floor} this run recorded")
    elif not isinstance(floor, (int, float)):
        problems.append("the run recorded no coverage FLOOR, so `at or above "
                        "the floor it recorded` cannot be scored")
    if problems:
        return _answer(9, FAIL, len(scope), of, "; ".join(problems))
    return _answer(9, PASS, len(scope), of,
                   f"all {len(scope)} in-scope row(s) carry a disposition and "
                   f"model coverage {fraction:.4f} is at or above the recorded "
                   f"floor {floor}")


def short_chunks(join: dict[str, Any]) -> list[str]:
    """THE ONE delivery predicate: which chunks did not carry what they mapped.

    THREE COPIES OF THIS EXISTED, in three languages, and they already disagreed
    (review 2026-08-15): `tools/cos_batch_chunk.py` tested
    `map_bytes_found is not True` while this module and `tools/cos_nightly.sh`
    tested `is False` — a real semantic split on the null case, asserted by
    nothing. That is exactly what `cos_judge.batch_membership` states three
    modules over: *"it does not keep a second copy. Three copies is how a
    denominator drifts."*

    IT LIVES IN THE ENGINE, not in `tools/`, and that inverts the review's
    suggested direction on purpose. THE REASON RECORDED FOR IT WAS FALSE and is
    corrected here (review 2026-08-15): the round that moved it wrote *"an
    INSTALLED engine has no `tools/` tree to reach into"*, and this repository
    contradicts that three times — `core.py` and `doctor.py` both insert
    `repo_root / "tools"` on `sys.path` and import from it, and
    `src/brain/_assets/tools/` ships four tool modules inside the package
    precisely so an installed engine DOES have them.

    The true reason is simpler and does not depend on packaging at all: this
    predicate is a GATE concern. `_e10` is the only thing that can fail a night
    on it, `_e10` lives in this module, and a gate that has to reach outside its
    own module for the rule it enforces is a gate that can fail to find it. The
    producer (`tools/cos_batch_chunk.short_chunks`) delegates HERE, so there is
    one definition and the gate owns it. Direction of dependency is a
    consequence, not the argument.

    THE NULL DECISION, made once and documented here: `map_bytes_found: null` is
    the ABSENT-MAP case and is NOT short by this predicate. There are no bytes to
    look for, so the assertion has no subject — and nothing is lost by excluding
    it, because a chunk with no map contributes no `covered_ids`, so the coverage
    condition fails the night anyway, and with a reason an operator can act on
    ("delivered grounding for 3 of 27 required ids") rather than "chunk-01 is
    short".
    """
    return [c.get("chunk") for c in (join.get("chunks") or [])
            if c.get("missing") or c.get("missing_text") or c.get("unexpected")
            or c.get("map_bytes_found") is False
            or (c.get("text_found_in_prompt") or 0) < (c.get("with_text") or 0)]


def _exact_int(value: Any) -> int | None:
    """`value` if it is a REAL int, else None. `True` is not a coverage count —
    `isinstance(True, int)` is True in Python, and the review passed a join
    declaring `required_covered_by_chunks: true`."""
    return value if type(value) is int else None                 # noqa: E721


def _grounding_delivery(run: dict[str, Any]) -> tuple[list[str], str]:
    """What a `grounded` night has to prove BEYOND its own declaration (D2a/D5).

    Returns `(problems, substance)` — the second being the one SENTENCE FRAGMENT
    E10 puts on its PASS line. That is not decoration: `covered_with_content` and
    the per-leg `with_content` counts were both being WRITTEN and read by
    nothing, so a night where the vault contributed nothing scored GROUNDED
    identically to one where it contributed everything, and the only signal was
    a nightly log line nobody has to read (review 2026-08-15).

    IT IS A NUMBER, NOT A VERDICT. `grounded` still means what the owner ruled it
    means — "the vault knows nothing here" IS a grounded answer — so a zero here
    does not fail the night. It just stops being invisible on the artifact an
    operator actually reads.

    Two joins, and neither alone is enough. The DENOMINATOR join says `required`
    was not under-counted — the frozen set must be present in the batch files
    `cos_judge.py --batches` independently rendered. The DELIVERY join says every
    block the map claims actually reached a composed prompt. Without the first, a
    fetcher that under-required scores a clean `grounded`; without the second, a
    map can be produced, declared, and never reach the model — the chunker's
    `--grounding` argument forgotten, a chunk composed before its map existed —
    while every other gate still passes.

    An UNGROUNDED night is not put through this: it makes no claim to join.
    """
    j = run.get("grounding_join")
    if not isinstance(j, dict):
        # Same posture as the missing declaration: a claim with no join is not
        # a claim.
        return (["the night declares GROUNDED and `$EV/grounding-join.json` is "
                 "MISSING — a map that was produced is not a map that arrived"],
                "")
    # The join-scoring and the substance sentence live in
    # :mod:`brain.cos_echecks_delivery` (s18); this module's own helpers are
    # handed over so their definitions stay single.
    problems, union, with_content = delivery.join_problems(
        run, j, short_chunks=short_chunks, exact_int=_exact_int)
    substance = delivery.substance_sentence(run, union, with_content)
    return problems, substance


def _e10(run: dict[str, Any]) -> dict[str, Any]:
    of = "frozen capability digest (present on every run)"
    frozen = (run["manifest"] or {}).get("capability_digest")
    now = capability_digest()
    g = run["grounding"] if isinstance(run["grounding"], dict) else None
    problems = []
    substance = ""
    if not frozen:
        problems.append("the run manifest froze no capability digest")
    elif now is None:
        problems.append("the executing tree is not on disk to re-hash")
    elif now != frozen:
        problems.append(f"the capability set CHANGED ({frozen[:12]}… → "
                        f"{now[:12]}…)")
    if not isinstance(run["sent_baseline"], dict):
        problems.append("the sent baseline is missing")
    if [r for r in run["undo"]
            if (r.get("receipts") or {}).get("send_attempted") is True]:
        problems.append("a send was attempted")
    if dispatched(run["undo"]) and not isinstance(run["binding"], dict):
        problems.append("mutations were dispatched under no plan binding")
    if g is None:
        problems.append("`_cos_grounding_<run>.json` is MISSING — an ungrounded "
                        "night is a thing the run SAYS, never a thing an absent "
                        "file implies")
    elif g.get("state") == "ungrounded":
        if not str(g.get("reason") or "").strip():
            problems.append("the night declares UNGROUNDED with no reason")
    elif g.get("state") in ("grounded", "ungrounded"):
        clause, substance = delivery.grounding_clause(
            run, g, delivery_problems=_grounding_delivery)
        problems.extend(clause)
    else:
        problems.append(f"grounding state {g.get('state')!r} is undeclared")
    if problems:
        return _answer(10, FAIL, 1, of, "; ".join(problems))
    return _answer(10, PASS, 1, of,
                   f"the capability set is byte-identical to the digest the "
                   f"manifest froze ({str(frozen)[:12]}…), no send was "
                   f"attempted, the plan binding is intact, and the run "
                   f"declares grounding `{g.get('state')}`"
                   + (f" ({g.get('reason')})" if g.get("state") == "ungrounded"
                      else "")
                   # THE SUBSTANCE NUMBER IS ON THE PASS LINE, so a night the
                   # vault contributed nothing to no longer reads identically to
                   # one it carried. It is reported, never gated.
                   + (f" — {substance}" if substance else ""))


#: id -> the function that answers it. TEN, CONTIGUOUS, and asserted against
#: the manifest's frozen count before anything is written.

# Parent-namespace binds, deferred past this module's own defs.
from .cos_echecks import (  # noqa: E402
    ARCHIVING_SIGNALS as ARCHIVING_SIGNALS,
    _cos_driver as _cos_driver,
    EcheckError as EcheckError,
    FAIL as FAIL,
    NA as NA,
    NEVER_NA as NEVER_NA,
    PASS as PASS,
    PERMITTED_PRIMITIVES as PERMITTED_PRIMITIVES,
    vault_of as vault_of,
)
from .cos_echecks_answers import _answer as _answer  # noqa: E402
from .cos_echecks_runs import (  # noqa: E402
    archive_join as archive_join,
    capability_digest as capability_digest,
    by_conversation as by_conversation,
    chip_join as chip_join,
    dispatched as dispatched,
    in_scope as in_scope,
    load_run as load_run,
)
