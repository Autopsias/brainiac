"""Sub-steps of the run validator's LEDGER-SHAPE checks (s16 extraction).

One function per E-check sub-step for the four checks that score the
ingestion ledger's own shape — ``check_ledger_vocabulary`` (b2),
``check_category_stamp`` (b3), ``check_body_pass`` (c2) and
``check_body_order`` (c4). The check functions themselves stay in
:mod:`brain.cos_runverify` with unchanged signatures (every caller and the
doctrine text pinned in the chief-of-staff fixtures name them there); what
lives here is the per-clause recounting each of them dispatches to.

This module also owns the ONE definition of the check-row shape (``_row``,
the four verdict states) and the shared ledger vocabularies, so the sibling
:mod:`brain.cos_runverify_identity` and :mod:`brain.cos_runverify` itself
import them from here — nothing is defined twice.

Import direction is one-way and acyclic: ``cos_runverify → identity →
checks``; this module imports neither of them.
"""
from __future__ import annotations

import re
from typing import Any

# -- check-row states ---------------------------------------------------------
#: the control ran, was re-executable where it mattered, and holds
PASS = "pass"
#: the control holds, but something about it could NOT be re-executed host-side
#: — or the run degraded and reported that degradation correctly
DEGRADED = "degraded"
#: the control ran and does not hold
FAIL = "fail"
#: the control could not be evaluated at all
INCONCLUSIVE = "inconclusive"


def _row(name: str, status: str, detail: str, *, reexecuted: bool) -> dict[str, Any]:
    return {"check": name, "status": status, "reexecuted": reexecuted,
            "detail": detail}


#: ledger dispositions that mean "this thread was in Phase-1.6 scope"
_HELD_DISPOSITIONS = {"held", "no-substance"}
_MARKER_DISPOSITION = "zero-eligible"

#: Phase-1.6 rule 8's CLOSED disposition vocabulary, plus the degrade marker.
#: Closed because the counters are defined by these words: an invented one
#: silently leaves its rows out of every total (run 106 wrote
#: `no-new-substance` on 15 rows and its `ingestion_held` legitimately read
#: 100 of 115 — 15 rows accounted nowhere, and nothing said so).
_LEDGER_DISPOSITIONS = {"candidate", *_HELD_DISPOSITIONS, _MARKER_DISPOSITION}

#: The managed `held_reason` set, verbatim from SKILL.md Phase 1.6 rule 1½'s
#: eight + rule 1¾'s `never-category` + rule 6's (dormant) `over-candidate-cap`.
#: E29(b) has required membership since v5.36 and NOTHING ever checked it, so
#: every run invented its own words: `browser-control-failure` (61),
#: `dedup-prior-proposal` (65), `corpus-closed-before-capture` (68), the whole
#: Phase-1.5 `Held · uncertain` vocabulary (73), `body-read-no-distinct-durable
#: -claim` + `target-not-found-timeout` + `capture-blocked-download-path`
#: (101), `unread-native-category-deferred` (103),
#: `no-substance-or-already-represented` (106, 108). Drift is not cosmetic:
#: `check_body_pass` keys on the WORD, so run 108's 19 substance verdicts
#: spelled `no-substance-or-already-represented` were invisible to the one
#: check written to score them, and it passed reporting "no `no-substance`
#: verdict in this run's ingestion ledger".
#: (v5.60) Two words the set genuinely lacked, named in SKILL.md rule 1½ first
#: and only then used, exactly as E29(b) requires. `pass-ended-by-identity-stop`
#: is the CASCADE (the pass ended on a mismatch and this thread was written out
#: behind it, `target_attempt: 0` — run 105 wrote 108 such rows as
#: `target-identity-mismatch`, which reads as 108 identity failures and is one
#: stop). `host-eval-timeout` is the INSTRUMENT (the host-side evaluation that
#: judges identity did not return within its bound — measured in daylight, one
#: navigation wedged Chrome's JS bridge for ~2 minutes and every read in that
#: window timed out; scoring that as a lane mismatch is an instrument failure
#: wearing a lane failure's word).
_HELD_REASONS = {
    "unread-read-state-invariant", "no-body-access-on-lane",
    "preview-insufficient", "over-cap", "no-substance", "browser-not-visible",
    "target-identity-mismatch", "target-identity-unconfirmed",
    "never-category", "over-candidate-cap",
    "pass-ended-by-identity-stop", "host-eval-timeout",
    # (v5.62) OWA refused the navigation — the bare shell, no conversation
    # opened, the pane never moved — AND the click fallback could not scroll the
    # row into the virtualized list to click it. The only refusal shape that
    # costs the run a body; a recovered one is an ordinary open.
    "navigation-refused-row-unreachable",
}

#: (2026-08-14, run 135) HOST-WRITTEN reasons for a row that reached the ledger
#: with NO USABLE MODEL VERDICT. Run 135 applied 41 of 41 mutations and still
#: scored INVALID: its 9 refused verdicts left their rows with `disposition:
#: null` and `held_reason: null`, outside rule 8's vocabulary — the funnel's own
#: "counted rather than hidden" rule held for the COUNTERS (`ledger_counts`
#: reads every non-candidate row as held) and failed for the WORD. This is the
#: same shape the comment above documents: a real outcome with no approved word
#: logs as ABSENCE, and a downstream gate reads absence as a fault.
#:
#: SEPARATE FROM `_HELD_REASONS` ON PURPOSE, and the separation is the point.
#: That set is printed INTO the batch prompt (`cos_judge.batch_prompts`) and
#: validated against the model's own answers, so a word in it is a word the
#: model may CLAIM. "the host refused my verdict" and "no verdict of mine
#: arrived" are not the model's statements to make — they are the host's record
#: of what the host did, written only by `apply_judgment`, and a model that
#: emits one is still refused by `triage.held_reason_vocabulary`.
_HOST_HELD_REASONS = {
    # a verdict ARRIVED and the host would not use it: `validate_verdict`
    # rejected it, or H3 dropped a conflicting duplicate pair.
    "judgment-refused",
    # no verdict arrived for this row at all — an unanswered row, or a chunk
    # whose model call produced nothing (the coverage floor scores that too).
    "unjudged",
}

#: (v5.60) Rule 5's CLOSED dedup vocabulary. Dedup has no drop path at all — a
#: near-duplicate yields `merge_candidate: <id>` INSTEAD OF a fresh `create`,
#: an inconclusive probe still stages — so a value here that reports a DROP is
#: asserting an authority rule 5 does not grant. Measured: run 106 wrote "brain
#: lexical probes; no novel durable candidate staged" into this slot on 15
#: rows, run 108 wrote "no novel durable candidate staged" into all 115 of its
#: rows, run 61 wrote the fused `inconclusive-vm-tier-clamp`. `not-run` is the
#: honest value on a row that never reached rule 5 (an unopened body, a capped
#: thread, a `never` category). ABSENT is legal — the key is optional.
_DEDUP_CHECKS = {"clean", "inconclusive", "not-run"}

#: (v5.60) SKILL.md rule 1½ step 4: the bare `<origin>/mail/` shell OWA drops a
#: tab to when a conversation will not deep-link is 42 characters, folder and id
#: gone. An extraction at or below it is a FAILED OPEN, not a short message —
#: run 108 appended two 42-character bodies to its corpus and gave both a
#: post-read `no-substance` verdict.
_EMPTY_SHELL_CHARS = 42

#: (v5.60) SKILL.md rule 1¾'s blanket-default bar. CALIBRATED, not guessed:
#: every night that demonstrably APPLIED the owner's taxonomy sits at a dominant
#: category share of 0.20-0.33 (runs 57, 59, 63, 64) and every blanket-default
#: night at 0.81-0.90 (runs 100, 101, 102, 104, 105, 106, 108), so 0.75 sits in
#: the middle of a gap half the scale wide. Only scored on a night with more
#: in-scope rows than the body-open cap, where a lopsided draw is not noise.
_CATEGORY_DOMINANCE_MAX_SHARE = 0.75
_CATEGORY_DOMINANCE_MIN_ROWS = 21

#: The one Phase-1.6 hold reason that can ONLY be reached by reading the body.
#: SKILL.md rule 1½ gives every "could not read it" case its own reason
#: (`preview-insufficient` for genuinely unread, `over-cap`,
#: `no-body-access-on-lane`, v5.40's `browser-not-visible`), so `no-substance`
#: asserts "I read this and there was nothing quotable in it".
_READ_IMPLYING_REASON = "no-substance"


#: Rule 1½'s draw groups, coarsest first. The body pass owes P0 before P1
#: before everything else in scope; inside a group the order is newest-first,
#: which the ledger does not witness (no `received` field) and this therefore
#: does not claim to check.
def _draw_rank(row: dict[str, Any]) -> int:
    tier = str(row.get("tier") or "").strip().upper()
    return {"P0": 0, "P1": 1}.get(tier, 2)


def _declares(rows: list[dict[str, Any]], want: tuple[int, int]) -> bool:
    """Does any row's own ``bundle_version`` claim ``want`` or later?

    The version gate s07-followup established: a row is never failed for a field
    the bundle that wrote it never named, and the row itself is what says which
    bundle that was.

    (v5.62) IT USED TO ANCHOR AT THE START OF THE STRING, AND THAT MADE EVERY
    GATE IT GUARDS UNFIREABLE ON THE ONE FORM RUNS ACTUALLY WRITE. Two spellings
    are live in the real ledgers — a bare ``"5.51"`` and the stamped
    ``"chief-of-staff v5.60"`` the host manifest carries — and ``re.match`` with
    ``^v?`` accepts only the first. Counted over every ingestion ledger this
    project holds: 782 rows in the bare form, and **234 rows spelling it
    ``chief-of-staff v5.60`` / ``v5.61``**, i.e. runs 110 and 111 — the very
    bundles that owed v5.60's per-attempt instrumentation and its obligatory
    in-run control. Both gates read False on them and neither could fail. A
    version gate that cannot recognise the version is the "check that returns
    clean because its input was empty" shape, one field over, so it is probed in
    BOTH spellings by ``test_declares_reads_the_stamped_bundle_string``.
    """
    for r in rows:
        raw = str(r.get("bundle_version") or "")
        m = re.search(r"v(\d+)\.(\d+)", raw) or re.match(r"(\d+)\.(\d+)", raw)
        if m and (int(m.group(1)), int(m.group(2))) >= want:
            return True
    return False


def _declares_v551(rows: list[dict[str, Any]]) -> bool:
    return _declares(rows, (5, 51))


# -- b2 ledger vocabulary ------------------------------------------------------

def _vocabulary_counts(rows: list[dict[str, Any]]) -> tuple[dict[str, int],
                                                            dict[str, int],
                                                            dict[str, int], int]:
    """Count every ledger row's disposition, dedup and hold-reason word.

    One pass, three tallies: dispositions outside rule 8's closed set,
    ``dedup_check`` values outside rule 5's, and ``held_reason`` words outside
    the managed set — plus the rows that carry no hold reason at all.
    """
    bad_disp: dict[str, int] = {}
    bad_reason: dict[str, int] = {}
    bad_dedup: dict[str, int] = {}
    missing_reason = 0
    for r in rows:
        disp = str(r.get("disposition") or "").strip()
        if disp not in _LEDGER_DISPOSITIONS:
            bad_disp[disp or "<absent>"] = bad_disp.get(disp or "<absent>", 0) + 1
        # (v5.60) The dedup slot is where run 106 and run 108 actually WROTE
        # the novelty verdict, so it is closed on the same terms as the other
        # two. ABSENT is legal; a present value must be one of rule 5's three.
        if "dedup_check" in r and r.get("dedup_check") is not None:
            dedup = str(r.get("dedup_check")).strip()
            if dedup not in _DEDUP_CHECKS:
                bad_dedup[dedup] = bad_dedup.get(dedup, 0) + 1
        if disp in ("candidate", _MARKER_DISPOSITION):
            continue
        reason = str(r.get("held_reason") or "").strip()
        if not reason:
            missing_reason += 1
        elif reason not in _HELD_REASONS | _HOST_HELD_REASONS:
            bad_reason[reason] = bad_reason.get(reason, 0) + 1
    return bad_disp, bad_reason, bad_dedup, missing_reason


def _vocabulary_problems(bad_disp: dict[str, int], bad_reason: dict[str, int],
                         bad_dedup: dict[str, int], missing_reason: int
                         ) -> list[str]:
    """Render the vocabulary tallies as the check's problem sentences."""
    problems: list[str] = []
    if bad_disp:
        problems.append(
            "disposition(s) outside rule 8's vocabulary "
            f"({'|'.join(sorted(_LEDGER_DISPOSITIONS))}): "
            + ", ".join(f"{v!r}×{n}" for v, n in sorted(bad_disp.items())))
    if missing_reason:
        problems.append(f"{missing_reason} non-candidate row(s) carry no "
                        "`held_reason` at all")
    if bad_reason:
        problems.append(
            "held_reason(s) outside the managed set: "
            + ", ".join(f"{v!r}×{n}" for v, n in sorted(bad_reason.items())))
    if bad_dedup:
        problems.append(
            "dedup_check value(s) outside rule 5's closed set "
            f"({'|'.join(sorted(_DEDUP_CHECKS))}): "
            + ", ".join(f"{v!r}×{n}" for v, n in sorted(bad_dedup.items()))
            + " — dedup has NO drop path (a near-duplicate is "
              "`merge_candidate`, an inconclusive probe still stages), so a "
              "value reporting a DROP asserts an authority rule 5 never granted")
    return problems


# -- b3 category stamp ---------------------------------------------------------

def _category_stamp_counts(stamped: list[dict[str, Any]],
                           scored: list[dict[str, Any]], rules: dict[str, Any],
                           never: set[str]) -> tuple[dict[str, int], int, int]:
    """Recount the rule-1¾ stamp: undefined ids, unexcluded `never`, both ways."""
    undefined: dict[str, int] = {}
    not_excluded = 0
    wrong_reason = 0
    for r in stamped:
        cid = str(r.get("category")).strip()
        if cid not in rules:
            undefined[cid] = undefined.get(cid, 0) + 1
            continue
        excluded = (str(r.get("held_reason") or "") == "never-category"
                    and r.get("disposition") == "no-substance"
                    and not r.get("body_opened"))
        if cid in never and not excluded:
            not_excluded += 1
    for r in scored:
        if str(r.get("held_reason") or "") != "never-category":
            continue
        cid = r.get("category")
        if cid is None or str(cid).strip() not in never:
            wrong_reason += 1
    return undefined, not_excluded, wrong_reason


def _category_stamp_problems(undefined: dict[str, int], not_excluded: int,
                             wrong_reason: int, never: set[str]) -> list[str]:
    """Render the stamp tallies as the check's problem sentences."""
    problems: list[str] = []
    if undefined:
        problems.append(
            "categor(ies) the parsed overlay does not define: "
            + ", ".join(f"{c!r}×{n}" for c, n in sorted(undefined.items()))
            + " — an id the owner never wrote is not a category, it is a guess")
    if not_excluded:
        problems.append(
            f"{not_excluded} row(s) stamped a `never` category "
            f"({'|'.join(sorted(never))}) and were NOT excluded — rule 1¾ owes "
            "each of them `disposition: no-substance`, `held_reason: "
            "never-category`, `body_opened: false`, or the exclusion is "
            "decorative")
    if wrong_reason:
        problems.append(
            f"{wrong_reason} row(s) ledgered `never-category` whose stamped "
            "category the taxonomy does not call `never` — the two slots agree "
            "in both directions or neither is evidence")
    return problems


def _category_dominance(stamped: list[dict[str, Any]], scored: list[dict[str, Any]]
                        ) -> tuple[str, int, float]:
    """The dominant stamped category, its count, and its share of the scored rows."""
    counts: dict[str, int] = {}
    for r in stamped:
        cid = str(r.get("category")).strip()
        counts[cid] = counts.get(cid, 0) + 1
    top, top_n = (max(counts.items(), key=lambda kv: kv[1]) if counts
                  else ("<none>", 0))
    share = top_n / len(scored) if scored else 0.0
    return top, top_n, share


def _category_dominance_problem(top: str, top_n: int, share: float,
                                scored_n: int) -> str | None:
    """The blanket-default bar, or None when the draw is inside it."""
    if (scored_n >= _CATEGORY_DOMINANCE_MIN_ROWS
            and share > _CATEGORY_DOMINANCE_MAX_SHARE):
        return (f"one category ({top!r}) covers {top_n} of {scored_n} in-scope "
                f"rows ({share:.0%}) — over the "
                f"{_CATEGORY_DOMINANCE_MAX_SHARE:.0%} "
                "blanket-default bar. Every night that demonstrably APPLIED this "
                "taxonomy sits at 20-33%; every blanket-default night at "
                "81-90%. If this night is honest the repair is the TAXONOMY "
                "(one id doing several ids' work), never this check")
    return None


# -- c2 body pass --------------------------------------------------------------

def _never_opened_problem(rows: list[dict[str, Any]]) -> str | None:
    """(v5.60) coherence rule 1: a `never` category costs ZERO opens."""
    never_opened = [r for r in rows
                    if r.get("body_opened")
                    and str(r.get("held_reason") or "") == "never-category"]
    if not never_opened:
        return None
    return (f"{len(never_opened)} row(s) carry `body_opened: true` "
            "beside `held_reason: \"never-category\"` — rule 1¾ "
            "excludes a `never` category on the rule-1½ DRAW, BEFORE "
            "the body is opened, so each of these spent one of the "
            "twenty opens the cap owed to actionable material. A "
            "post-hoc exclusion recovers the doctrine and keeps the "
            "cost (E29(e); measured: 11 of run 103's 19 opens)")


def _empty_shell_problem(rows: list[dict[str, Any]]) -> str | None:
    """(v5.60) coherence rule 2: an empty shell is not a body."""
    shells = [r for r in rows
              if r.get("body_opened")
              and isinstance(r.get("body_chars"), int)
              and not isinstance(r.get("body_chars"), bool)
              and r["body_chars"] <= _EMPTY_SHELL_CHARS]
    if not shells:
        return None
    return (f"{len(shells)} row(s) claim `body_opened: true` over an "
            f"extraction of at most {_EMPTY_SHELL_CHARS} characters — "
            "that is the bare `<origin>/mail/` shell v5.57 names "
            "(folder and id gone), so the open FAILED and the row "
            "records it as landed. Rule 1½ step 4: `body_opened: "
            "false`, no corpus row, and never a post-read verdict "
            "(measured: run 108 banked two 42-character bodies and "
            "judged both `no-substance`). (v5.62) The reason is "
            "`navigation-refused-row-unreachable` when the click "
            "fallback could not scroll the row into the list; a row "
            "the fallback DID reach is an ordinary open, and only a "
            "landing that produced a WRONG id is "
            "`target-identity-mismatch`")


# -- c4 body order -------------------------------------------------------------

def _starved_problem(rows: list[dict[str, Any]],
                     opened: list[dict[str, Any]]) -> str | None:
    """The field-free half: no `over-cap` row outranks a body actually opened.

    Compared against the LOWEST-ranked body the pass actually read, never the
    highest — the first cut of this compared against the highest and scored
    run 102 clean: its 3 starved P1 rows outrank the 3 P3 bodies it opened,
    but not the P0s it also opened. The probe caught it; a positive-only test
    never would.
    """
    open_worst = max(_draw_rank(r) for r in opened)
    starved = [r for r in rows
               if str(r.get("held_reason") or "") == "over-cap"
               and _draw_rank(r) < open_worst]
    if not starved:
        return None
    names = ["P0", "P1", "other"]
    best = min(_draw_rank(r) for r in starved)
    return (f"{len(starved)} in-scope row(s) finished `over-cap` at a "
            f"HIGHER draw group than a body this run actually opened "
            f"(the starved set reaches {names[best]}; the lowest group "
            f"opened was {names[open_worst]}) — rule 1½ draws P0, then "
            "P1, then the rest, so the cap must bite the LOWEST group "
            "first. The night's reading budget went to the wrong end "
            "of the queue")


def _sequence_verdict(opened: list[dict[str, Any]]
                      ) -> tuple[str, str, bool] | None:
    """The `body_open_seq` half: (state, detail, reexecuted), or None to pass.

    ``body_open_seq`` (v5.51) is the ONLY witness: this ledger holds one row
    per in-scope thread written in ENUMERATION order, opened and unopened
    interleaved (run 63's opened rows are scattered the length of its file),
    so line order is not a fallback in either direction.
    """
    names = ["P0", "P1", "other"]
    seqs = [r.get("body_open_seq") for r in opened]
    if all(s is None for s in seqs):
        if _declares_v551(opened):
            return (FAIL,
                    f"none of this run's {len(opened)} opened rows carries "
                    "`body_open_seq` and its own `bundle_version` says "
                    "v5.51 or later, which requires it — the draw cannot "
                    "be recounted, and rule 8 puts the stamp on the same "
                    "footing as `body_opened`", True)
        if len({_draw_rank(r) for r in opened}) == 1:
            return (PASS,
                    f"all {len(opened)} body open(s) sit in ONE draw group "
                    f"({names[_draw_rank(opened[0])]}) and no `over-cap` "
                    "row outranks them — there is no order here to get "
                    "wrong, stamp or no stamp", True)
        # A pre-v5.51 ledger is not retro-failed against a field its own bundle
        # never named — but its line order is the only ordering signal it left,
        # so print it: a reader deciding whether to look harder should see it.
        seen = "".join(names[_draw_rank(r)][:2] for r in opened)
        return (DEGRADED,
                f"none of this run's {len(opened)} opened rows carries "
                "`body_open_seq`, so the order they were DRAWN in cannot "
                "be recounted — the bundle predates v5.51 and this "
                "ledger's line order is enumeration order, not open order. "
                f"No `over-cap` row outranks an open. Line order was: {seen}",
                True)
    if any(s is None for s in seqs):
        return (FAIL,
                f"{sum(1 for s in seqs if s is None)} of this run's "
                f"{len(opened)} opened rows carry no `body_open_seq` while "
                "others do — a partially-stamped sequence cannot be "
                "replayed and is not a witness to anything", True)
    if not all(isinstance(s, int) and not isinstance(s, bool) for s in seqs):
        return (FAIL,
                "a `body_open_seq` in this run's ingestion ledger is not "
                f"an integer position: {sorted(map(repr, seqs))[:5]}", True)
    if sorted(seqs) != list(range(1, len(seqs) + 1)):
        return (FAIL,
                f"the {len(seqs)} `body_open_seq` value(s) are not a "
                f"contiguous 1..{len(seqs)} — a gap or a repeat means the "
                "draw cannot be replayed from the ledger "
                f"(got {sorted(seqs)})", True)
    ordered = sorted(opened, key=lambda r: r["body_open_seq"])
    ranks = [_draw_rank(r) for r in ordered]
    for i in range(1, len(ranks)):
        if ranks[i] < ranks[i - 1]:
            return (FAIL,
                    f"open #{i + 1} is {names[ranks[i]]} but open #{i} was "
                    f"{names[ranks[i - 1]]} — rule 1½ draws P0, then P1, "
                    "then the rest, and no thread is opened while an "
                    "unopened in-scope thread of a higher group remains. "
                    "Observed `body_open_seq` order: "
                    + "".join(names[r][:2] for r in ranks), True)
    return None
