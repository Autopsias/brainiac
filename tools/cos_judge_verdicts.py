"""Verdict adjudication sub-steps of `cos_judge.judge_night`'s validation pass.

Two stages, moved whole out of :mod:`cos_judge`: the H3 duplicate grouping
that turns the raw verdict objects into one answer per conversation id, and
the per-row accept-or-refuse pass over the enumerated rows that runs the
mechanical bookkeeping, the pre-draw category stamp and the closed-vocabulary
validator. Import direction is one-way: this module imports nothing from its
parent — the validator, the screen order, the mechanical-disposition decider
and the id shortener all arrive as callables, so a test that patches them on
the parent is still honoured.
"""
from __future__ import annotations

import json
from typing import Any, Callable


def group_verdicts(verdicts: list[dict[str, Any]]
                   ) -> tuple[dict[str, dict[str, Any]], set[str], int, int]:
    """``(by_id, conflicted_cids, duplicate_conflicts, duplicate_reemissions)``.

    H3 (Claude HIGH). The multi-turn reassembly (STREAM-01) can legitimately
    re-emit a boundary object across a turn split, so a duplicate
    conversation_id is NOT grounds to refuse the night — that would re-break
    the very multi-turn fix. Group by cid: one object is normal; several EQUAL
    objects are a benign re-emission (keep one); several that CONFLICT are
    dropped ENTIRELY (cid left out of by_id, so the enumerated loop treats it as
    PENDING/unjudged — the fail-safe direction) and counted. Never last-wins: a
    silent last-wins would let a second, differing object overwrite the first.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for v in verdicts:
        cid = v.get("conversation_id")
        if cid is None:
            continue        # a verdict with no id cannot join an enumerated row
        grouped.setdefault(cid, []).append(v)
    by_id: dict[str, dict[str, Any]] = {}
    conflicted_cids: set[str] = set()
    duplicate_conflicts = 0
    duplicate_reemissions = 0
    for cid, objs in grouped.items():
        if len(objs) == 1:
            by_id[cid] = objs[0]
        # R2 (Codex re-review HIGH): compare TYPE-PRESERVING canonical JSON, not
        # `==`. Python holds `True == 1` and `1 == 1.0`, so two objects differing
        # only `auto_archive: true` vs `1` compare EQUAL under `==` and would be
        # called a benign re-emission — yet `cos_mutate` tests `is True`, so that
        # difference decides whether an archive is planned. Canonical JSON
        # ("true" != "1") keeps them a CONFLICT, which drops the cid, fail-safe.
        elif len({json.dumps(o, sort_keys=True) for o in objs}) == 1:
            by_id[cid] = objs[0]
            duplicate_reemissions += 1
        else:
            # Dropped: the cid is left out of by_id AND recorded here so the
            # enumerated loop can keep it PENDING even where a mechanical
            # disposition would otherwise rescue it (R2, Codex re-review).
            conflicted_cids.add(cid)
            duplicate_conflicts += 1
    return by_id, conflicted_cids, duplicate_conflicts, duplicate_reemissions


def _prepared_verdict(row, answer, cid, ctx, categories, taxonomy_ids, *,
                      mechanical, first_screen,
                      undefined_stamps) -> dict[str, Any]:
    """Stage ONE model answer into the verdict the validator will judge."""
    v = dict(answer)
    mech = mechanical(row)
    # ON THE VALUE, NOT THE KEY. A model that volunteers `"disposition":
    # null` on an unopened row it was never asked to stage used to SUPPRESS
    # the host's own bookkeeping (the key was present), and was then refused
    # for the null it had sent plus the `held_reason` the host would have
    # supplied. An explicit null is not an answer.
    if mech and v.get("disposition") is None:
        v.update(mech)
    # THE PRE-DRAW STAMP IS THE CATEGORY. Applied AFTER the empty check, so
    # it can never turn a row that no verdict reached — a legitimately
    # PENDING row — into a rejected one.
    # WHERE THE PRE-DRAW LEG ANSWERED, ITS ANSWER WINS: the body draw was
    # made on it, and a staging stamp that disagrees is a second answer to
    # a settled question. WHERE IT ANSWERED `null`, THE STAGING STAMP
    # STANDS — and that is deliberate, not a gap. A row the category leg
    # could not place from typed fields is a row the gate did not exclude,
    # so its body really was opened; if the body then shows it was a
    # `never` thread, `body_pass` reporting that open is the TRUTH about
    # the night. Wiping the stamp would hide a miss the gate should be
    # judged on, and would strand the model's `held_reason: never-category`
    # beside an empty category — an inconsistency of our own making.
    stamp = (categories or {}).get(cid)
    if stamp:
        if stamp in taxonomy_ids:
            v["category"] = stamp
        else:
            # An id the taxonomy does not define is dropped rather than
            # carried: the driver already refuses to exclude on one (an
            # unknown id is a guess), and carrying it here would reject the
            # row's whole verdict, triage included, for a slot the row can
            # legally leave empty. Both legs treat it as no stamp, and both
            # report it.
            undefined_stamps[stamp] = undefined_stamps.get(stamp, 0) + 1
    v["conversation_id"] = cid
    # `hold_category` is DERIVED, never accepted. Every screen in the order
    # is a fact this run recorded, so asking a judge to name the first one
    # that failed is asking it to guess at something code already knows —
    # and on run 120 it guessed wrong 29 times out of 45, which alone blew
    # the abort threshold and threw away a whole valid night's triage.
    if v.get("hold_verdict") or v.get("hold_category"):
        v["hold_category"] = first_screen(v, ctx)
    return v


def accept_verdicts(rows: list[dict[str, Any]],
                    by_id: dict[str, dict[str, Any]],
                    conflicted_cids: set[str],
                    ctx_by_id: dict[str, dict[str, Any]],
                    categories: dict[str, str] | None,
                    taxonomy_ids: set[str], *,
                    mechanical: Callable[[dict[str, Any]], dict[str, Any] | None],
                    first_screen: Callable[[dict[str, Any], dict[str, Any]],
                                           str | None],
                    validate: Callable[[dict[str, Any], dict[str, Any]],
                                      list[dict[str, str]]],
                    short_id: Callable[[str], str]
                    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]],
                               set[str], dict[str, int]]:
    """``(accepted, rejected, refused_cids, undefined_stamps)`` — the enumerated
    accept-or-refuse pass, one row at a time.

    Ids whose verdict ARRIVED and the host would not use it — a validator
    rejection, or an H3 conflicting-duplicate drop — are stamped
    `judgment-refused` rather than `unjudged`, because "the model never
    answered" and "the host threw the answer away" are different mornings and
    rule 8's slot should say which one happened (run 135).
    """
    accepted: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    refused_cids: set[str] = set(conflicted_cids)
    undefined_stamps: dict[str, int] = {}
    for row in rows:
        cid = row["conversation_id"]
        # R2 (Codex re-review): a cid whose duplicates CONFLICT is unresolved.
        # Leave it PENDING — never let `mechanical_disposition` (which fires for
        # an unopened row) rescue it into `accepted` and record the conflict as
        # judged. It stays in scope and is counted pending, the fail-safe read.
        if cid in conflicted_cids:
            continue
        # NO MODEL ANSWER IS NOT A VOCABULARY DISAGREEMENT (s09, 2026-08-16).
        # `mechanical_disposition` below manufactures a partial verdict for an
        # UNOPENED row out of driver facts alone. Applied to a cid the model
        # never answered, it turned a row with no verdict — which this loop
        # otherwise leaves PENDING two lines down — into a non-empty dict that
        # `triage.bucket_vocabulary` then refused for a bucket the host had
        # never claimed and the model had never sent. 7 of the 28 rejections
        # across runs 147+148 are exactly that, and every one of those cids is
        # absent from the run's own `verdicts.json`. It is a MODEL-COVERAGE
        # fact, it already has its own correctly-calibrated control
        # (`model_coverage` against `model_coverage_floor`, which aborts
        # read-only), and counting it a second time in the rate whose
        # documented meaning is "the prompt templates and the validator
        # disagree" both mis-names it and aborts the night on it. Such a row
        # stays PENDING and is stamped `unjudged` rather than
        # `judgment-refused` — which is what actually happened to it.
        answer = by_id.get(cid)
        if not answer:
            continue
        v = _prepared_verdict(
            row, answer, cid, ctx_by_id[cid], categories, taxonomy_ids,
            mechanical=mechanical, first_screen=first_screen,
            undefined_stamps=undefined_stamps)
        violations = validate(v, ctx_by_id[cid])
        if violations:
            # The FULL id is kept beside the shortened report id: `rejected` is
            # owner-facing evidence and stays short, but `apply_judgment` has to
            # join on the real conversation_id to stamp `judgment-refused`
            # rather than `unjudged` on this row (run 135).
            refused_cids.add(cid)
            rejected.append({"conversation_id": short_id(cid),
                             "violations": violations})
        else:
            accepted[cid] = v
    return accepted, rejected, refused_cids, undefined_stamps
