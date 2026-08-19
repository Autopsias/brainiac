"""Category-gate evaluation for the COS driver's live night.

Two sub-steps of ``cos_driver._run_night``: binding the category stamps to
the enumeration they were judged on (with the exclusions, the starvation
interlock, the ungated shadow draw), then rendering the ``category_gate``
evidence block the nightly publishes. ``_run_night`` keeps its name and
module; every parent callable this needs (``bind_categories``,
``resolve_never``, ``body_draw``, ``starvation_stop``,
``category_gate_state``, the ``DriverStop`` class) arrives as a parameter —
this module never imports ``cos_driver``, so a monkeypatched parent
attribute keeps working.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def gate_scope_and_exclusions(
        vault: Path, convs: list[dict[str, Any]], cap: int,
        categories: dict[str, str] | None,
        prior_enumeration: list[dict[str, Any]] | None,
        exclude_convids: set[str] | None, *,
        bind_categories: Callable[..., dict[str, Any]],
        resolve_never: Callable[..., dict[str, Any]],
        body_draw: Callable[..., list[dict[str, Any]]],
        starvation_stop: Callable[..., list[dict[str, Any]] | str | None],
        category_gate_state: Callable[..., dict[str, Any]],
        driver_stop: type[Exception]) -> dict[str, Any]:
    """THE GATE, FED: bind the stamps, exclude the owner's `never` rows, and
    evaluate the interlock both globally and over the asked-about scope."""
    gate: dict[str, Any] = {}
    # THE STAMPS WERE JUDGED ON ANOTHER ENUMERATION, AND ARE BOUND TO IT
    # (review 2026-08-13, round 1, HIGH). `prior_enumeration` is the
    # `--enumerate-only` output the category batch was asked about; anything
    # the mailbox did since then — an arrival, a thread whose subject or read
    # state changed — is resolved by `bind_categories` before a stamp can
    # exclude a body. Without it, this pass applied stamps by id to a snapshot
    # the model never saw.
    binding: dict[str, Any] = {}
    gate_scope: list[str] = [c["convId"] for c in convs]
    if categories is not None:
        if prior_enumeration is None:
            raise driver_stop(
                "a category answer was supplied with no enumeration to bind it "
                "to. The stamps were judged on the `--enumerate-only` snapshot "
                "and this pass re-enumerates, so without that file a stamp "
                "would be applied by id to a mailbox the model never saw — an "
                "arrival would draw ungated and a changed thread would be "
                "excluded on obsolete data, with nothing to notice either")
        binding = bind_categories(categories, prior_enumeration, convs)
        gate_scope = binding["scope"]
        gate = resolve_never(vault, binding["honored"])
        exclude_convids = set(exclude_convids or ()) | gate["excluded"]
    excluded = frozenset(exclude_convids or ())
    # An id we were handed for a conversation this enumeration does not carry
    # excludes nothing; counting it would inflate the metric that proves the
    # gate works, which is the one number that must stay honest.
    in_scope_excluded = {c["convId"] for c in convs if c["convId"] in excluded}
    # THE DEGENERATE CASE, AND IT NEEDS NO THRESHOLD (review 2026-08-13, round
    # 1, HIGH). A category pass that stamped everything `never` would blind the
    # night, and the cited backstop — `_CATEGORY_DOMINANCE_MAX_SHARE`, 0.75 —
    # is evadable by construction: split the stamps across two `never` ids at
    # 50/50 and 100% of the inbox is excluded while no single category reaches
    # the bar. What SHARE is too much is an owner decision that wants data
    # nobody has yet (`_evidence/s09/excluded-share.json`); what is unambiguous
    # today is ZERO. A mailbox that would have drawn bodies and draws none
    # BECAUSE of the gate is a blinded night, and a blinded night must stop for
    # a human rather than proceed reporting a quiet one.
    #
    # Compared against the SAME `body_draw` with no exclusions, so the two
    # differ in exactly one input: any zero it reports is the gate's doing and
    # not an all-unread mailbox, which draws zero either way and is left alone.
    ungated_draw = body_draw(convs, cap)
    starved = starvation_stop(convs, cap, excluded)
    # AND WHAT THE INTERLOCK WOULD HAVE SAID ABOUT THE SCOPE IT WAS ASKED ABOUT
    # (review 2026-08-13, round 5 — the one place the two review lanes
    # disagreed, settled by probe in `_evidence/s09/arrivals-probe.txt`).
    #
    # Codex said an arrival flood fills the cap ungated and pushes
    # `starvation_stop` back to None; Claude said `body_draw` filters
    # `isRead is True` so a new arrival cannot enter the draw at all. The probe
    # says BOTH, on different arrivals: an UNREAD arrival is structurally
    # excluded and the interlock still fires; a READ one — the owner opening
    # mail on a phone during a 15-40 minute model call, or a thread re-entering
    # the window — enters the draw ungated AND silences the interlock.
    #
    # WHAT TO DO ABOUT THE ARRIVAL ITSELF IS THE OWNER'S CALL (defer it, judge
    # the delta, or stop) and is carded, not invented here. What is NOT a
    # policy question is whether the night can tell: a blinded mailbox that
    # drew one arrival looked exactly like a healthy one. So the interlock is
    # ALSO evaluated over `gate_scope` — the threads the model was actually
    # asked about — and the difference is reported. Reported, not policed:
    # nothing changes what the night does.
    scope_ids = set(gate_scope)
    starved_in_scope = starvation_stop(
        [c for c in convs if c["convId"] in scope_ids], cap, excluded)
    draw = body_draw(convs, cap, exclude=excluded)
    # WAT-01: the gate ships with the number that reveals it was never armed.
    # `state` comes from the ONE shared predicate the judge also calls, so the
    # two legs cannot disagree about the same run — and an empty answer reads
    # `not-run` on both, because it excluded nothing.
    # `defined_ids` is the owner's taxonomy as `resolve_never` read it — the
    # same load, not a second one — so `armed` is checked against what the
    # owner actually wrote rather than against "the string is non-empty".
    # SCOPED TO WHAT THE MODEL WAS ASKED ABOUT AND WHAT IS STILL HERE. Judging
    # coverage against THIS enumeration would read `not-run` on every real
    # night — one mail arriving during a 15-40 minute model call is enough —
    # so the gate would have reported "never ran" while still excluding on its
    # stamps. Arrivals are reported on their own line instead, which is the
    # honest shape: the gate ran, over the threads it was asked about.
    gate_state = category_gate_state(binding["honored"] if binding else categories,
                                     gate_scope, gate.get("defined_ids"))
    return {"excluded": excluded, "in_scope_excluded": in_scope_excluded,
            "draw": draw, "ungated_draw": ungated_draw, "starved": starved,
            "starved_in_scope": starved_in_scope, "binding": binding,
            "gate": gate, "gate_state": gate_state}


def gate_evidence_block(convs: list[dict[str, Any]],
                        gate: dict[str, Any], gate_state: dict[str, Any],
                        binding: dict[str, Any],
                        in_scope_excluded: set[str], excluded: frozenset[str],
                        starved: Any, starved_in_scope: Any,
                        ungated_draw: list[dict[str, Any]],
                        draw: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``category_gate`` block the nightly publishes, numbers first."""
    return {
        "excluded_before_draw": len(in_scope_excluded),
        "state": gate_state["state"],
        "state_why": gate_state["why"],
        "stamps_in_scope": gate_state["stamps_in_scope"],
        "categorised_in_scope": gate_state["categorised_in_scope"],
        "unstamped_in_scope": gate_state["unstamped_in_scope"],
        "undefined_ids": gate_state["undefined_ids"],
        "stamps_supplied": gate_state["stamps_supplied"],
        "categorised": gate.get("categorised", 0),
        "in_scope": len(convs),
        # REPORTED, NOT POLICED. A category pass that stamped everything
        # `never` would blind the night, and the temptation is a share
        # threshold here — but nothing has ever measured what share of this
        # mailbox a FULL pre-draw pass calls `never` (runs 126-130 stamped only
        # the ~20 rows their bodies reached), so any number would be invented.
        # The host already fails a blanket-default night on its own calibrated
        # bar (`_CATEGORY_DOMINANCE_MAX_SHARE` in `check_category_stamp`); this
        # is the number that lets the first live runs calibrate one honestly.
        "excluded_share": (round(len(in_scope_excluded) / len(convs), 4)
                           if convs else 0.0),
        # THE SNAPSHOT DELTA, ON THE REPORT. Absent these, a gate whose stamps
        # were judged on a different mailbox looks exactly like one that was
        # not — which is how this went unnoticed. `arrivals_ungated` is the
        # number that matters: threads no stamp covers, drawing freely.
        "arrivals_ungated": len(binding.get("arrivals") or ()),
        # THE INTERLOCK'S OWN BLIND SPOT, ON THE REPORT. True means the gate
        # excluded every thread it was ASKED about and the night proceeded only
        # because an arrival nothing judged filled the draw.
        "starvation_suppressed_by_arrivals": bool(starved_in_scope and not starved),
        "starvation_in_scope_why": starved_in_scope or None,
        # THE HONORED MAP ITSELF, ON THE REPORT (review 2026-08-13, round 5,
        # H4). The counts above said stamps had been dropped; the STAMPS were
        # never published, so the nightly had nothing to hand the judge except
        # the raw model answer — and the judge then re-applied the very stamps
        # this pass dropped as stale, reporting `armed` on a gate the driver
        # reported `not-run`, one run, two contradictory gate states. This is
        # the artifact the judge must read: the stamps that actually bound,
        # after the snapshot delta was resolved. `{}` when the gate did not run,
        # which is the same thing the counts say.
        "honored_stamps": dict(binding.get("honored") or {}),
        "stamps_dropped_as_stale": len(binding.get("stamps_dropped_as_stale") or ()),
        "rows_changed_since_the_stamp": len(binding.get("stale") or ()),
        "departed_since_the_stamp": len(binding.get("departed") or ()),
        "bound_to_prior_enumeration": bool(binding),
        # THE SHADOW NUMBER (item 2). What the draw would have been with the
        # gate off, beside what it was — so the first armed nights measure the
        # cost of the gate instead of an owner inventing a share threshold.
        "draw_ungated": len(ungated_draw),
        "draw_gated": len(draw),
        "opens_withheld_by_the_gate": len(ungated_draw) - len(draw),
        "never_ids": gate.get("never_ids", []),
        "taxonomy_mode": gate.get("mode"),
        "undefined_categories": gate.get("undefined_categories", {}),
        "excluded_ids_not_enumerated": len(excluded) - len(in_scope_excluded),
        "why": ("rule 1¾ excludes a `never`-category thread BEFORE its body is "
                "opened; the category is a judgment, so the caller supplies the "
                "stamps and the owner's taxonomy says which are `never`. "
                "`not-run` means every opened body was drawn without that gate "
                "and the run may have spent opens on excluded material — "
                "measured at 8 of 20 opens on runs 126, 129 and 130"),
    }
