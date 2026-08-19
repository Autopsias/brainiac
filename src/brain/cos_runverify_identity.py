"""Sub-steps of the run validator's IDENTITY checks (s16 extraction).

One function per E-check sub-step for ``check_target_identity`` (c6) and
``check_open_instrumentation`` (c7), plus the small refusal/ordering
predicates those clauses share. The check functions themselves stay in
:mod:`brain.cos_runverify` with unchanged signatures; what lives here is the
per-clause recounting each of them dispatches to.

Import direction is one-way and acyclic: ``cos_runverify → identity →
checks``; this module imports the shared row shape and vocabularies from
:mod:`brain.cos_runverify_checks` and never from ``cos_runverify`` itself.
"""
from __future__ import annotations

from typing import Any

from . import cos
from .cos_runverify_checks import FAIL, _EMPTY_SHELL_CHARS, _row

#: Events that end the action ledger without ever acting on a row — a stop
#: record and the attachment lane's own events are not identity assertions.
#: A mismatch row discovered under EITHER would be real, so they are excluded
#: from the asserted set by NAME here and re-checked by clause below; if that
#: ever fails loudly here rather than passing silently — the right direction.
_NON_IDENTITY_EVENTS = ("mutation-stop", "attachment-lane")

#: (v5.62) The hold reasons that legitimately END the body pass, so the threads
#: behind them may wear `pass-ended-by-identity-stop`. A wrong conversation
#: opened (`target-identity-mismatch`) is the original stop; a host evaluation
#: that timed out instead of answering (`host-eval-timeout`, v5.60) learned
#: nothing about the conversation, and carrying on blind past a wedged bridge is
#: not a pass either. **A REFUSED navigation is deliberately NOT here** — it
#: opened nothing, moved nothing and touched nothing, so it holds its own thread
#: and the draw continues to the next row.
_PASS_STOPPING_REASONS = {"target-identity-mismatch", "host-eval-timeout"}

#: The per-attempt fields v5.60 obliges on EVERY attempt, failed ones included.
#: The full list with its rationale lives beside the check that scores it
#: (``check_open_instrumentation`` in :mod:`brain.cos_runverify`); the
#: `_CONTROL_ARTIFACT` name below is the in-run control's file name.
_ATTEMPT_FIELDS = ("open_method", "eval_ms", "ready_state", "rendered_rows",
                   "body_chars", "url_has_id", "hour", "display_state",
                   "hold_status", "hold_status_source")

#: The in-run control: the SAME fixed daylight burst, re-run inside the night on
#: the same lane. v5.57 made the rehearsal re-anchor to the TOP of the folder
#: while a night draws by PRIORITY across ~115 rows, so the rehearsal and the
#: night have never sampled the same population — which is how four successive
#: fixes each scored 20/20 in daylight while the night kept failing.
_CONTROL_ARTIFACT = "_cos_lane_control_{run_id}.json"


def _ordered(rows: list[dict[str, Any]]) -> tuple[list[tuple[Any, dict[str, Any]]], bool]:
    """The ledger in the only order it can honestly be read, and whether that
    order is the timestamps'.

    Run 104's action ledger is NOT written in timestamp order (its stop record
    carries 08:54:01 at line 13 while line 21 carries 08:53:30), so ledger
    position and clock disagree and neither is reliable alone. Timestamps win
    when EVERY row carries one; otherwise position is all there is, and the
    caller says so rather than implying a precision it does not have.
    """
    by_ts = all(str(r.get("ts") or "") for r in rows)
    key = (lambda i_r: str(i_r[1].get("ts"))) if by_ts else (lambda i_r: i_r[0])
    return sorted(enumerate(rows), key=key), by_ts


def _repeated_action(retry: dict[str, Any], first: dict[str, Any]) -> str:
    """Did the one bounded re-target repeat the attempt that just failed (E30(e))?

    Identified by what the action actually DID, per primitive — a click by the
    point it clicked, a v5.55 deep-link open by the URL it navigated to. Until
    the body pass could only click, `point` was the whole answer; a re-target
    that re-navigates to one URL is run 101's defect one primitive over, and a
    check that knows only about points cannot see it. Returns the phrase naming
    what repeated, or "" when the two attempts genuinely differed.

    An attempt that carries NEITHER field never acted (`row-not-rendered`), and
    two of those are not one action taken twice — so they never fingerprint.
    """
    point, url = retry.get("point"), retry.get("open_url")
    if point and point == first.get("point"):
        return f"clicked the SAME point {point}"
    if url and url == first.get("open_url"):
        return f"navigated to the SAME URL {url}"
    return ""


def _is_refusal(row: dict[str, Any]) -> bool:
    """(v5.62) Did OWA REFUSE this navigation, rather than open the wrong thread?

    RECOUNTED FROM THE PAGE, never from a word the run chose. All four
    conditions, because each one alone is a different defect:

    * ``open_method: "navigate"`` — a click cannot be refused this way; it either
      moves the pane or it does not, and the reading-pane URL is app-produced.
    * NO PRODUCED ID. This is the anti-weakening condition and it is absolute:
      the moment the page yields ANY conversation id, something opened, and if
      it is not the intended one that is a `target-identity-mismatch` with every
      obligation that carries. A refusal is the absence of an open, not a
      gentler kind of wrong one.
    * ``url_has_id: false`` — the tab lost its `/id/` segment entirely.
    * ``body_chars`` at or below the 42-character bare-folder shell, READ AT THE
      MOMENT IDENTITY WAS JUDGED (v5.60 obliges exactly that). A shell-length
      page is the whole evidence that nothing was opened; a page with real text
      and no id is `no-id`, which stays a mismatch.

    All four are fields a v5.60 run already owes on EVERY attempt including the
    failed ones, so this needs no new field and cannot be asserted into being.
    """
    if row.get("open_method") != "navigate":
        return False
    if row.get("target_produced"):
        return False
    if row.get("url_has_id") is not False:
        return False
    body = row.get("body_chars")
    return (isinstance(body, int) and not isinstance(body, bool)
            and body <= _EMPTY_SHELL_CHARS)


def _refusal_followups(asserted: list[dict[str, Any]],
                       refused: list[dict[str, Any]]) -> list[str]:
    """Each refusal took its ONE bounded re-target, and it was the CLICK path.

    Held by name is an acceptable answer; silence is not. The re-target row
    either produced the intended id (the fallback reached the row and opened
    it) or records that the row could not be reached
    (``navigation-refused-row-unreachable``) — which is a counted hold, and the
    only refusal shape that costs the run a body.
    """
    problems: list[str] = []
    for first in refused:
        n = first.get("attempt")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            problems.append("a refusal row with no attempt number — the "
                            "re-target cannot be read off it (E30(a))")
            continue
        same = [r for r in asserted
                if str(r.get("target_intended")) == str(first.get("target_intended"))]
        retries = [r for r in same if r.get("attempt") == n + 1]
        if not retries:
            problems.append("a refusal whose bounded re-target was never taken "
                            "— run 111's shape exactly: the fallback had no "
                            "rendered row to click and the attempt simply ended")
            continue
        retry = retries[0]
        if retry.get("open_method") != "click":
            problems.append(
                "a refusal re-targeted by "
                f"{str(retry.get('open_method'))!r} rather than the CLICK path "
                "— re-navigating to the same URL repeats the attempt that was "
                "just refused (E30(e))")
        elif (retry.get("target_produced") != retry.get("target_intended")
                and str(retry.get("held_reason") or "")
                != "navigation-refused-row-unreachable"):
            problems.append(
                "a refusal whose click fallback neither produced the intended "
                "id nor recorded `navigation-refused-row-unreachable` — a "
                "fallback that failed for an unnamed reason is not a held row, "
                "it is an unaccounted body")
    return problems


# -- c6 target identity --------------------------------------------------------

def _identity_partitions(rows: list[dict[str, Any]]
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                    list[dict[str, Any]], list[dict[str, Any]],
                                    list[dict[str, Any]]]:
    """Slice the action ledger into (asserted, refused, unreachable, forged,
    mismatched).

    (v5.62) The click fallback that could not SCROLL its row into the
    virtualized list never clicked anything, so it has no produced surface to
    judge — it is the named hold, not a mismatch. It still cannot hide one:
    producing ANY id means something opened, and the word is then a forgery
    (caught by the caller and, on the ledger side, by
    ``check_open_instrumentation``).
    """
    asserted = [r for r in rows
                if "target_intended" in r and "target_produced" in r
                and r.get("event") not in _NON_IDENTITY_EVENTS]
    differing = [r for r in asserted
                 if r.get("target_produced") != r.get("target_intended")]
    refused = [r for r in differing if _is_refusal(r)]
    unreachable = [r for r in differing
                   if str(r.get("held_reason") or "")
                   == "navigation-refused-row-unreachable"]
    forged = [r for r in unreachable if r.get("target_produced")]
    mismatched = [r for r in differing
                  if not _is_refusal(r) and r not in unreachable]
    return asserted, refused, unreachable, forged, mismatched


def _forged_unreachable_row(forged: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A row the fallback never reached cannot have produced anything."""
    if not forged:
        return None
    return _row("target_identity", FAIL,
                f"{len(forged)} action row(s) carry "
                "`navigation-refused-row-unreachable` while producing a "
                "conversation id — a row the fallback never reached cannot "
                "have produced anything, and an id it DID produce that is "
                "not the intended one is `target-identity-mismatch`",
                reexecuted=True)


def _refusal_problem_row(asserted: list[dict[str, Any]],
                         refused: list[dict[str, Any]]) -> dict[str, Any] | None:
    """THE REFUSALS ARE SCORED FIRST, AND ON THEIR OWN OBLIGATION: the ONE
    bounded re-target is still owed, and on a refusal it is the CLICK path —
    which is exactly what run 111 never took. All four of its refusals died at
    `target_attempt: 1`, because a refused navigation leaves the tab on the
    bare shell with a dozen rows rendered from the TOP of the folder while a
    priority row is the OLDEST mail in it, so `row-not-rendered` was the honest
    answer to a fallback that never scrolled. A refusal is only inert if it was
    RECOVERED or HELD BY NAME; unaccounted, it is a body the run silently did
    not read."""
    refusal_problems = _refusal_followups(asserted, refused)
    if not refusal_problems:
        return None
    return _row("target_identity", FAIL,
                f"{len(refusal_problems)} of this run's {len(refused)} "
                "REFUSED navigation(s) took no bounded re-target: "
                + "; ".join(sorted(set(refusal_problems))[:3])
                + ". A refusal is answered by the CLICK path, and the click "
                  "path must first SCROLL the row into the virtualized list "
                  "— a fallback that cannot reach its row is a second "
                  "refusal wearing the first one's cause (measured run 111: "
                  "4 refusals, all dead at attempt 1)",
                reexecuted=True)


def _undetected_problem_row(mismatched: list[dict[str, Any]]) -> dict[str, Any] | None:
    """(i) DETECTED. The ledger says the ids differ; the run must say so too. A
    row asserting `identity_verified: true` over a differing pair is a mismatch
    nobody saw — and it is also the shape a re-target takes when it claims
    success without having produced the id it intended."""
    undetected = [r for r in mismatched if r.get("identity_verified") is not False]
    if not undetected:
        return None
    return _row("target_identity", FAIL,
                f"{len(undetected)} of this run's {len(mismatched)} "
                "identity mismatch(es) are not marked detected "
                "(`identity_verified` is not false) — the produced id "
                "differs from the intended one and the row asserts the "
                "identity held. An UNDETECTED mismatch is what E30 exists "
                "for, and a re-target that claims success without "
                "`target_produced == target_intended` reads exactly like "
                "this",
                reexecuted=True)


def _missing_pre_problem_row(mismatched: list[dict[str, Any]]) -> dict[str, Any] | None:
    """(ii) The mismatch row carries `target_produced_pre` (E30(a), v5.50):
    without it "never moved" and "moved to the wrong row" are the same record."""
    no_pre = [r for r in mismatched if "target_produced_pre" not in r]
    if not no_pre:
        return None
    return _row("target_identity", FAIL,
                f"{len(no_pre)} of this run's {len(mismatched)} mismatch "
                "row(s) carry no `target_produced_pre`, so the ledger "
                "cannot say whether the action moved the surface to the "
                "wrong conversation or never moved it at all (E30(a), "
                "v5.50) — recovery cannot be PROVEN from a record that "
                "incomplete",
                reexecuted=True)


def _post_mismatch_mutation_row(rows: list[dict[str, Any]],
                                mismatched: list[dict[str, Any]]
                                ) -> dict[str, Any] | None:
    """(iii) INERT. Nothing mutated at or after the first mismatch (E30(b))."""
    order, by_ts = _ordered(rows)
    positions = {id(r): i for i, (_, r) in enumerate(order)}
    first_at = min(positions[id(r)] for r in mismatched)
    mutated = [r for _, r in order[first_at:] if r.get("mutation") is True]
    if not mutated:
        return None
    return _row("target_identity", FAIL,
                f"{len(mutated)} row(s) carry `mutation: true` at or after "
                "this run's first identity mismatch — the first mismatch "
                "ends every mutation leg for the run (E30(b)), and a "
                "mutation after it is an automatic FAIL, never a "
                "repair-and-continue. Ordering read from "
                + ("timestamps" if by_ts else
                   "LEDGER POSITION (not every row carries a `ts`)"),
                reexecuted=True)


def _recovery_problems(mismatched: list[dict[str, Any]],
                       asserted: list[dict[str, Any]]) -> tuple[list[str], int]:
    """(iv) RECOVERED, once, and differently — the problem list and the count.

    One bounded re-target per mismatched target: it names what it changed,
    clicks a different point where both attempts recorded one, and produces the
    id it intended.
    """
    problems: list[str] = []
    recovered = 0
    for first in mismatched:
        # Attempt-KEYED, never "every row on this convid": a later action on the
        # same conversation is not a third attempt at this open, and grouping by
        # id alone would read one as a breach of the bound (E30(a), v5.48).
        n = first.get("attempt")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            problems.append("a mismatch row with no attempt number — the "
                            "action-to-produced chain cannot be replayed, so "
                            "no recovery can be read off it (E30(a))")
            continue
        if n > 1:
            problems.append("a mismatch on the RE-TARGET itself: the one "
                            "bounded re-target never recovered it (this is run "
                            "101's and run 103's shape)")
            continue
        same = [r for r in asserted
                if str(r.get("target_intended")) == str(first.get("target_intended"))]
        if [r for r in same if isinstance(r.get("attempt"), int)
                and not isinstance(r.get("attempt"), bool) and r["attempt"] > n + 1]:
            problems.append("more than one re-target on a single open — the "
                            "re-target is ONE and bounded (E30)")
            continue
        retries = [r for r in same if r.get("attempt") == n + 1]
        if not retries:
            problems.append("a mismatch whose one bounded re-target was never "
                            "taken — a mismatch left unrecovered is a FAIL, "
                            "recovered or not is the whole distinction")
            continue
        retry = retries[0]
        if retry.get("target_produced") != retry.get("target_intended"):
            problems.append("a re-target that did not produce the id it "
                            "intended (this is run 101's and run 103's shape)")
        elif not str(retry.get("retarget_changed") or "").strip():
            problems.append("a re-target that names no change — a retry "
                            "identical to the attempt that just failed is not "
                            "a re-target, whatever it produced (E30(e))")
        elif repeated := _repeated_action(retry, first):
            problems.append(f"a re-target that {repeated} (E30(e))")
        else:
            recovered += 1
    return problems, recovered


# -- c7 open instrumentation ---------------------------------------------------

def _mislabel_problems(ledger: list[dict[str, Any]]) -> list[str]:
    """(1) THE MISLABEL. A row whose own ``target_attempt`` is 0 was never
    opened, so it cannot carry ``target-identity-mismatch``."""
    mislabelled = [r for r in ledger
                   if str(r.get("held_reason") or "") == "target-identity-mismatch"
                   and isinstance(r.get("target_attempt"), int)
                   and not isinstance(r.get("target_attempt"), bool)
                   and r["target_attempt"] < 1]
    if not mislabelled:
        return []
    return [f"{len(mislabelled)} ledger row(s) carry `target-identity-mismatch` "
            "with their own `target_attempt: 0` — never opened, so nothing "
            "produced the wrong id. That is the pass-ended cascade wearing a "
            "mismatch's word (run 105 wrote 108 of them, and the night read as "
            "108 identity failures); its reason is "
            "`pass-ended-by-identity-stop`"]


def _forged_refusal_problems(ledger: list[dict[str, Any]]) -> list[str]:
    """(v5.62) The refusal word may only sit on a row that really was refused —
    recounted from the page facts, never from the word. Without this the new
    word is a way to launder a wrong-conversation landing out of the mutation
    stop, which is the one thing this split must never buy."""
    forged = [r for r in ledger
              if str(r.get("held_reason") or "") == "navigation-refused-row-unreachable"
              and not _is_refusal(r)]
    if not forged:
        return []
    return [f"{len(forged)} ledger row(s) carry "
            "`navigation-refused-row-unreachable` without the page facts a "
            "refusal is defined by (`open_method: \"navigate\"`, no produced "
            f"id, `url_has_id: false`, `body_chars` <= {_EMPTY_SHELL_CHARS}). "
            "A landing that produced ANY id opened something; if it was not "
            "the intended conversation that is `target-identity-mismatch`, "
            "with the mutation stop and everything else it carries"]


def _cascade_problems(ledger: list[dict[str, Any]],
                      acts: list[dict[str, Any]]) -> list[str]:
    """A refusal may not end the pass. The stop exists for a wrong conversation
    being opened; a refusal opened none, so the cascade word needs a TRUE
    mismatch behind it. Measured run 111: four refusals, and 111 rows written
    out `pass-ended-by-identity-stop` behind them — a whole night's reading
    lost to a stop nothing had triggered."""
    cascade = [r for r in ledger
               if str(r.get("held_reason") or "") == "pass-ended-by-identity-stop"]
    real_stop = any(str(r.get("held_reason") or "") in _PASS_STOPPING_REASONS
                    for r in ledger) or any(
        r.get("target_produced") != r.get("target_intended") and not _is_refusal(r)
        for r in acts
        if "target_intended" in r and "target_produced" in r
        and r.get("event") not in _NON_IDENTITY_EVENTS)
    if not (cascade and not real_stop):
        return []
    return [f"{len(cascade)} ledger row(s) carry `pass-ended-by-identity-stop` "
            "while nothing in this run records a cause that ENDS a pass "
            f"({'/'.join(sorted(_PASS_STOPPING_REASONS))}, or an action row "
            "whose produced id differs from the intended one) — so the pass "
            "ended on a REFUSAL, which opened no conversation, moved no pane "
            "and touched nothing. A refusal holds its own thread and the pass "
            "carries on (measured run 111: 4 refusals, 111 rows written out "
            "behind a stop nothing triggered)"]


def _attempt_problems(vault, run_id: str,
                      acts: list[dict[str, Any]]) -> list[str]:
    """(2) THE PER-ATTEMPT INSTRUMENTATION and the IN-RUN CONTROL, on v5.60+."""
    problems: list[str] = []
    attempts = [r for r in acts
                if "target_intended" in r
                and r.get("event") not in _NON_IDENTITY_EVENTS]
    missing: dict[str, int] = {}
    for r in attempts:
        for f in _ATTEMPT_FIELDS:
            if r.get(f) is None:
                missing[f] = missing.get(f, 0) + 1
        if r.get("open_method") == "navigate" and not r.get("open_url"):
            missing["open_url"] = missing.get("open_url", 0) + 1
        if r.get("hold_status_source") not in (None, "status-file"):
            problems.append(
                "an attempt row whose `hold_status_source` is "
                f"{r['hold_status_source']!r} — the hold's status is READ "
                "FROM ITS FILE or it is not evidence: a hold that has lost "
                "its tab keeps reporting `holding`")
    if missing:
        problems.append(
            f"{len(attempts)} attempt row(s) missing per-attempt "
            "instrumentation: "
            + ", ".join(f"{f}×{n}" for f, n in sorted(missing.items()))
            + " — these are owed on EVERY attempt including the failed "
              "ones; run 106 is unscoreable for want of `open_method` and "
              "`open_url` alone")
    control = cos.run_ops_dir(vault) / _CONTROL_ARTIFACT.format(run_id=run_id)
    if attempts and not control.is_file():
        problems.append(
            f"no in-run control ({control.name}) beside {len(attempts)} "
            "open attempt(s) — the same fixed daylight burst re-run inside "
            "the night on the same lane is the ONE field that separates a "
            "lane fault from the priority draw, and without it this night "
            "cannot be scored either way (E30(g))")
    return problems
