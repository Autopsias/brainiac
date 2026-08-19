"""Category-stamp, ingestion-ledger, body-pass, and body-order checks."""
from __future__ import annotations

from typing import Any

from . import cos

def check_category_stamp(vault, run_id: str,
                         rows: list[dict[str, Any]]) -> dict[str, Any]:
    """(b3) The rule-1¾ stamp, scored against the owner's OWN parsed taxonomy.

    WHY THIS EXISTS (measured 2026-08-10, against the live, present and
    parseable ``overlay/cos/ingest.md``). Rule 1¾ was not being applied at all:

    * runs 103, 106 and 108 wrote **zero** ``never-category`` rows,
    * run 103 stamped ``category: null`` on **all 118** of its rows — running
      as though the feature were OFF while the taxonomy sat on disk,
    * runs 105/106/108 stamped ``internal-coordination`` on **exactly 100 of
      115** rows each, a blanket default rather than a per-thread judgment.

    The consequence is not cosmetic: ``never`` material was OPENED — 11 of run
    103's 19 opens and 3 of run 108's — spending a budget the cap owed to
    actionable mail, and then folded into the same ``no-substance`` bucket.

    Everything here is threshold-free EXCEPT the blanket-default bar, which is
    calibrated off the same corpus (see ``_CATEGORY_DOMINANCE_MAX_SHARE``). The
    dominant share is reported on EVERY verdict, pass included, so drift is
    visible before it is a failure — the discipline v5.53 gave the recovered
    mismatch count.

    Scored only when the taxonomy is ACTIVE. Absent or unparseable is the
    documented feature-OFF state (``cos.ingest_taxonomy``), and a run cannot be
    failed for a rule that was not in force.
    """
    try:
        taxonomy = cos.ingest_taxonomy(vault)
    except Exception as exc:                     # pragma: no cover - defensive
        return _row("category_stamp", INCONCLUSIVE,
                    f"the owner's ingest taxonomy could not be read ({exc}), "
                    "so the rule-1¾ stamp could not be scored",
                    reexecuted=False)
    if taxonomy.get("mode") != "active":
        return _row("category_stamp", PASS,
                    f"the ingest taxonomy is {taxonomy.get('mode')!r} — rule "
                    "1¾ is not in force, so `category: null` on every row is "
                    "the documented shape and there is nothing to score",
                    reexecuted=True)

    rules = taxonomy.get("rules") or {}
    never = {cid for cid, r in rules.items()
             if str((r or {}).get("disposition") or "").strip().lower() == "never"}
    scored = [r for r in rows if r.get("disposition") != _MARKER_DISPOSITION]
    if not scored:
        return _row("category_stamp", PASS,
                    "no in-scope ingestion rows to stamp", reexecuted=True)

    stamped = [r for r in scored if r.get("category") is not None]
    problems: list[str] = []

    if not stamped:
        problems.append(
            f"all {len(scored)} in-scope row(s) carry `category: null` while "
            f"the owner's taxonomy is ACTIVE and defines {len(rules)} "
            "categor(ies) — `null` is legal ONLY when the overlay is absent or "
            "unparseable, and this run behaved as though the feature were off "
            "(run 103's shape: 118 of 118)")

    undefined, not_excluded, wrong_reason = _category_stamp_counts(
        stamped, scored, rules, never)
    problems += _category_stamp_problems(undefined, not_excluded,
                                         wrong_reason, never)

    top, top_n, share = _category_dominance(stamped, scored)
    dominance = _category_dominance_problem(top, top_n, share, len(scored))
    if dominance:
        problems.append(dominance)

    detail_tail = (f"; dominant category {top!r} at {top_n}/{len(scored)} "
                   f"({share:.0%})")
    if problems:
        return _row("category_stamp", FAIL,
                    f"{len(scored)} in-scope ingestion row(s): "
                    + "; ".join(problems) + " (E29(e); SKILL.md Phase 1.6 "
                    "rule 1¾)" + detail_tail,
                    reexecuted=True)
    return _row("category_stamp", PASS,
                f"{len(scored)} in-scope row(s) stamped against the owner's "
                f"active taxonomy: every id defined, every `never` category "
                f"excluded before its body was opened{detail_tail}",
                reexecuted=True)


def check_ingestion_ledger(vault, run_id: str, rows: list[dict[str, Any]],
                           recon: Any) -> dict[str, Any]:
    """(c) On a mail-live night the ingestion ledger exists and is not vacuous.

    Applicability is DELEGATED to ``tools/cos_reconcile_metrics.observation_guard``
    — the lane-off, lane-opened-mid-run and mail-not-live false-alarm classes
    are already worked out there, and a second copy of them here would drift."""
    if recon is None:
        return _row("ingestion_ledger", INCONCLUSIVE,
                    "the observation guard is not available host-side, so the "
                    "run-obligation check could not be evaluated",
                    reexecuted=False)
    ops = cos.run_ops_dir(vault)
    guard = recon.observation_guard(ops, run_id)
    verdict = guard.get("verdict")
    enumerated, source = recon.mail_leg_enumerated(ops, run_id)
    ledger = ops / f"_cos_ingestion_ledger_{run_id}.jsonl"
    if enumerated > 0 and not rows:
        return _row("ingestion_ledger", FAIL,
                    f"the mail leg enumerated {enumerated} thread(s) ({source}) "
                    f"but {ledger.name} carries no rows at all — a silent "
                    "Phase 1.6 is never 'not exercised'",
                    reexecuted=True)
    if verdict == "FAIL":
        return _row("ingestion_ledger", FAIL, guard.get("reason", ""),
                    reexecuted=True)
    if verdict == "PENDING":
        return _row("ingestion_ledger", INCONCLUSIVE, guard.get("reason", ""),
                    reexecuted=True)
    return _row("ingestion_ledger", PASS,
                f"observation guard: {verdict} — {guard.get('reason', '')}",
                reexecuted=True)


def check_body_pass(run_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """(c2) The body pass that the substance verdicts claim actually RAN.

    WHY THIS EXISTS (measured, run 64, 2026-08-02). Run 64 wrote no Phase-1.6
    rows of its own: its ingestion ledger is run 63's ledger, filtered to run
    64's enumerated set, with ``run``/``ts`` rewritten, ``body_opened`` hand-set
    on three ids and every one of run 63's ``candidate`` rows rewritten to
    ``held``/``dedup-prior-proposal``. The funnel then reported 0 candidates
    from 116 in-scope threads and nothing could see it: the row count was right,
    every row carried a real category, and ``candidate_stamps`` passed
    VACUOUSLY because there were no candidates to stamp.

    What the copy could not fake is COHERENCE. ``no-substance`` is the one hold
    reason that means "I read the body and there was nothing quotable in it" —
    every genuinely-unreadable case has its own reason (rule 1½). So a
    ``no-substance`` row with ``body_opened: false`` is a substance verdict
    reached without the read that verdict asserts.

    Measured false-positive rate on the real corpus: ZERO. Runs 57-63 carry not
    one such row (run 63: all 60 ``no-substance`` rows ``body_opened: true``,
    68 opens total); run 64 carries 58.

    THE VERDICT IS READ OFF ``held_reason`` AND ONLY THERE, deliberately —
    ``disposition: "no-substance"`` cannot carry it, because E29(e) MANDATES
    that same disposition for the `never-category` exclusion, which is a
    taxonomy drop and owes no body read (run 104 carries 4 such rows). So the
    two slots say different things and only one of them asserts a read.

    That is also why this check went vacuous on run 108 (2026-08-09), which
    wrote its 19 substance verdicts as ``held_reason:
    "no-substance-or-already-represented"`` and got "no `no-substance` verdict
    in this run's ingestion ledger — nothing claims a read it did not make".
    The repair belongs at the invented word, not here: widening this check to
    read ``disposition`` too would FAIL every doctrine-conforming
    `never-category` row. ``check_ledger_vocabulary`` (v5.59) refuses a
    `held_reason` outside the managed set, which is what makes the one word
    this check keys on trustworthy.
    """
    substance = [r for r in rows
                 if str(r.get("held_reason") or "") == _READ_IMPLYING_REASON]
    opened = sum(1 for r in rows if r.get("body_opened"))

    # (v5.60) TWO COHERENCE RULES ON THE SAME PAIR OF SLOTS, scored before the
    # substance verdicts because either one invalidates an open outright:
    # (1) A `never` CATEGORY COSTS ZERO OPENS (rule 1¾ excludes on the
    #     rule-1½ DRAW, before the body is opened), and (2) AN EMPTY SHELL IS
    #     NOT A BODY (rule 1½ step 4: an extraction at or below the
    #     42-character bare-folder shell is a FAILED OPEN, scored only where
    #     `body_chars` is present — a bundle that never named the field is
    #     never failed against it). Both sub-steps live in
    #     :mod:`brain.cos_runverify_checks` with their measured histories.
    coherence = _never_opened_problem(rows) or _empty_shell_problem(rows)
    if coherence:
        return _row("body_pass", FAIL, coherence, reexecuted=True)

    if not substance:
        return _row("body_pass", PASS,
                    f"no `{_READ_IMPLYING_REASON}` verdict in this run's "
                    f"ingestion ledger ({len(rows)} row(s), {opened} body "
                    "open(s)) — nothing claims a read it did not make",
                    reexecuted=True)
    if not any("body_opened" in r for r in rows):
        return _row("body_pass", DEGRADED,
                    f"{len(substance)} row(s) assert `{_READ_IMPLYING_REASON}` "
                    "but no row in the ledger carries a `body_opened` stamp, so "
                    "the reads they claim cannot be recounted host-side — the "
                    "bundle predates EXT-01, and presence alone is not evidence",
                    reexecuted=True)
    unread = [r for r in substance if not r.get("body_opened")]
    if unread:
        return _row("body_pass", FAIL,
                    f"{len(unread)} of this run's ingestion rows are disposed "
                    f"`{_READ_IMPLYING_REASON}` with `body_opened: false` — a "
                    "substance verdict reached WITHOUT the body read it "
                    "asserts. Rule 1½ gives every unreadable case its own "
                    "reason (`preview-insufficient`, `over-cap`, "
                    "`no-body-access-on-lane`, `browser-not-visible`), so this "
                    f"is a body pass that did not run ({opened} open(s) across "
                    f"{len(rows)} row(s))",
                    reexecuted=True)
    return _row("body_pass", PASS,
                f"{opened} body open(s) across {len(rows)} row(s); every "
                f"`{_READ_IMPLYING_REASON}` verdict is backed by an actual read",
                reexecuted=True)


def check_body_order(run_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """(c4) The body pass drew P0 before P1 before the rest.

    WHY THIS EXISTS (measured, two consecutive nights). Run 102 (2026-08-09) had
    113 threads in scope and a cap of 20, and the first three bodies it opened
    were P3 ``act`` rows; its first P0 was the SEVENTH open. Its cap happened
    not to starve anything, so that harm was latent — but run 101, the night
    before, is the same defect realized: ALL TWENTY of its opens went to P3
    threads while every one of its 3 P0 and 14 P1 in-scope threads finished
    ``over-cap``. Run 102's own E29 caught the ordering ("P3-before-P0"); run
    101's did not catch the starvation at all, and this validator scored that
    night VALID_DEGRADED 11/11.

    TWO ASSERTIONS, DELIBERATELY UNEQUAL IN WHAT THEY NEED:

    * ``over-cap`` never outranks an open — the cap must bite the LOWEST draw
      group first. Field-free (it reads only ``tier`` and ``held_reason``, which
      every bundle since ING-05 writes), so it scores a run of ANY vintage. It
      fires on RUN 101, which read 20 P2/P3 bodies while 17 in-scope rows
      reaching up to P0 finished ``over-cap`` — a night this validator scored
      VALID 11/11 the morning after.
    * ``body_open_seq`` is contiguous and non-decreasing in rank. This needs the
      v5.51 field, and it is what catches RUN 102's shape. Run 102's own line
      order shows the defect plainly (``ot ot ot P1 P1 P1 P0 …``) but a
      pre-v5.51 ledger's line order is ENUMERATION order, so a run of that
      vintage DEGRADES here rather than being retro-FAILed against a field its
      own bundle never named.
    """
    opened = [r for r in rows if r.get("body_opened")]
    if not opened:
        return _row("body_order", PASS,
                    f"no body was opened in this run's ingestion ledger "
                    f"({len(rows)} row(s)) — no draw order to check",
                    reexecuted=True)

    # (ii) the field-free half: the cap must bite the LOWEST group first, so
    # NO `over-cap` row may outrank ANY row that was opened. (Compared against
    # the LOWEST-ranked body the pass actually read, never the highest — the
    # first cut compared against the highest and scored run 102 clean; the
    # probe caught it, a positive-only test never would.)
    starved = _starved_problem(rows, opened)
    if starved:
        return _row("body_order", FAIL, starved, reexecuted=True)

    # (i) the sequence half. `body_open_seq` (v5.51) is the ONLY witness: this
    # ledger holds one row per in-scope thread written in ENUMERATION order,
    # opened and unopened interleaved, so line order is not a fallback in
    # either direction.
    verdict = _sequence_verdict(opened)
    if verdict is not None:
        status, detail, reexecuted = verdict
        return _row("body_order", status, detail, reexecuted=reexecuted)
    return _row("body_order", PASS,
                f"{len(opened)} body open(s) drawn P0→P1→rest and recounted "
                "from `body_open_seq`; no `over-cap` row outranks an open",
                reexecuted=True)


#: The ONE category primitive that writes without selecting the row. Every
#: other one has to touch the row, and Outlook reads a native selection as an
#: open — SKILL.md v5.51, measured on run 102's SAP thread.

# Parent binds, deferred past this module's own defs.
from .cos_runverify import (  # noqa: E402
    DEGRADED as DEGRADED,
    FAIL as FAIL,
    INCONCLUSIVE as INCONCLUSIVE,
    PASS as PASS,
    _MARKER_DISPOSITION as _MARKER_DISPOSITION,
    _READ_IMPLYING_REASON as _READ_IMPLYING_REASON,
    _category_dominance as _category_dominance,
    _category_dominance_problem as _category_dominance_problem,
    _category_stamp_counts as _category_stamp_counts,
    _category_stamp_problems as _category_stamp_problems,
    _empty_shell_problem as _empty_shell_problem,
    _never_opened_problem as _never_opened_problem,
    _row as _row,
    _sequence_verdict as _sequence_verdict,
    _starved_problem as _starved_problem,
)
