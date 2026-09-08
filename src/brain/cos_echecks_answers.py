"""The E1-E10 run-integrity checks and their shared helpers."""
from __future__ import annotations

import re as re
from typing import Any, Callable

from . import cos as cos, cos_chips
from . import cos_echecks_delivery as delivery  # noqa: F401

# ---------------------------------------------------------------------------
# the ten checks
# ---------------------------------------------------------------------------
def _answer(cid: int, result: str, denominator: int, of: str, detail: str
            ) -> dict[str, Any]:
    if result == NA and cid in NEVER_NA:
        result = FAIL
        detail = (f"answered N/A, which E{cid} may never be — its denominator "
                  f"({of}) exists on every run. " + detail)
    if result == NA and denominator:
        result = FAIL
        detail = (f"answered N/A over a NON-ZERO denominator ({denominator} "
                  f"{of}) — N/A is legal only against a machine-derived zero. "
                  + detail)
    return {"id": cid, "result": result, "denominator": denominator,
            "denominator_of": of, "detail": detail}


def _e1(run: dict[str, Any]) -> dict[str, Any]:
    disp = dispatched(run["undo"])
    baseline = run["sent_baseline"]
    items = len(baseline.get("items") or []) if isinstance(baseline, dict) else 0
    of = "dispatched mutation row(s), against the read leg's sent baseline"
    problems = []
    if not isinstance(baseline, dict):
        problems.append("the sent baseline is missing or unreadable")
    bad = sorted({str(r.get("primitive")) for r in disp
                  if str(r.get("primitive")) not in PERMITTED_PRIMITIVES})
    if bad:
        problems.append(f"primitive(s) outside the permitted set: {bad}")
    sends = [r for r in run["undo"]
             if ((r.get("receipts") or {}).get("send_attempted") is True
                 or r.get("send_attempted") is True)]
    if sends:
        problems.append(f"{len(sends)} row(s) record a send attempt")
    frozen = (run["manifest"] or {}).get("capability_digest")
    now = capability_digest()
    if not frozen:
        problems.append("the run manifest froze no capability digest, so the "
                        "permitted and banned sets this run began with cannot "
                        "be re-derived")
    elif now is None:
        problems.append("the executing tree is not on disk to re-hash the "
                        "permitted and banned sets against")
    elif now != frozen:
        problems.append(f"the capability set CHANGED during the run "
                        f"({frozen[:12]}… → {now[:12]}…)")
    detail = (f"{len(disp)} dispatched mutation(s), all on the permitted "
              f"primitives; no send attempted; sent baseline present "
              f"({items} item(s) in its window); capability set byte-identical "
              f"to the digest the manifest froze")
    return _answer(1, FAIL if problems else PASS, len(disp) + items, of,
                   "; ".join(problems) if problems else detail)


def _e2(run: dict[str, Any]) -> dict[str, Any]:
    disp = dispatched(run["undo"])
    verdicts = by_conversation(run["ledger"])
    of = "dispatched mutation row(s)"
    absent = sorted({str(r.get("conversation_id_digest") or "?") for r in disp
                     if r.get("conversation_id") not in verdicts})
    unread = sorted({str(r.get("conversation_id_digest") or "?") for r in disp
                     if verdicts.get(r.get("conversation_id"), {})
                     .get("read_state") == "unread"})
    if not disp:
        return _answer(2, NA, 0, of,
                       "this run dispatched no mutation, so no thread was "
                       "screened for read state")
    if absent or unread:
        return _answer(2, FAIL, len(disp), of,
                       (f"{len(absent)} mutated thread(s) absent from the "
                        f"ingestion ledger {absent[:6]} — absence is a FAIL, "
                        "never an excuse; " if absent else "")
                       + (f"{len(unread)} mutated thread(s) carry "
                          f"`read_state: unread` {unread[:6]}" if unread else ""))
    return _answer(2, PASS, len(disp), of,
                   f"every one of {len(disp)} mutated thread(s) joins the "
                   "ingestion ledger and was screened READ before the mutation")


def _e3(run: dict[str, Any], vault, run_id: str) -> dict[str, Any]:
    rows = archive_join(vault, run_id, run)
    of = "archive row(s) in the undo ledger"
    if not rows:
        return _answer(3, NA, 0, of, "this run archived nothing")
    bad = []
    for r in rows:
        # (AGED-01, STALE-01) The legal BUCKET is looked up by the row's OWN
        # signal — `read` for the aged-read lane, `act` for the stale-act one,
        # `noise` for everything else — the same widening
        # `cos_judge_rules._r_floor` applies, kept in the same shape so the two
        # cannot drift apart silently. Per-row lookup is why adding a lane
        # cannot change what an existing signal means to this check.
        legal = ARCHIVE_BUCKETS.get(r["noise_signal"], DEFAULT_ARCHIVE_BUCKETS)
        if not r["in_ledger"]:
            bad.append(f"{r['digest']}:not-enumerated")
        elif r["verdict"] not in legal:
            bad.append(f"{r['digest']}:verdict={r['verdict']}")
        elif r["read_state"] != "read":
            bad.append(f"{r['digest']}:read_state={r['read_state']}")
        elif p0_floor_refuses(r["verdict"], r["judged_tier"]):
            bad.append(f"{r['digest']}:tier={r['judged_tier']}")
        elif r["noise_signal"] not in ARCHIVING_SIGNALS:
            bad.append(f"{r['digest']}:signal={r['noise_signal']}")
    if bad:
        return _answer(3, FAIL, len(rows), of,
                       f"{len(bad)} of {len(rows)} archived thread(s) breach "
                       f"the eligibility rule: {bad[:8]}")
    sig: dict[str, int] = {}
    for r in rows:
        sig[str(r["noise_signal"])] = sig.get(str(r["noise_signal"]), 0) + 1
    return _answer(3, PASS, len(rows), of,
                   f"all {len(rows)} archived thread(s) were READ, sit in the "
                   "bucket their own signal admits (`noise`, `read` on the "
                   "aged-read lane, `act` on the stale-act lane), clear the P0 "
                   f"blast floor and cite a recognized typed signal ({sig})")


def _e4_mark(r: dict[str, Any]) -> str | None:
    """The INGESTION MARK's own rule. It is a second axis, so the four-chip
    (bucket, tier) matrix does not apply to it — but "does not apply" must not
    mean "is not checked". A mark is legitimate when the vault SIGNED a note
    for this thread's own candidate (FIX-03: the content-hash join, not the
    bridge's old drop stamp — a stamp said "offered", and the chip claimed
    "ingested"), and when the thread did not already carry the mark
    (re-writing it is a no-op that spends a slot and an undo key).

    THE CANDIDATE NEED NOT BE TONIGHT'S (review 2026-08-25). Signing happens
    in a maintenance drain AFTER the night that offered the candidate, so a
    correct mark is nearly always catching up on an earlier run. `chip_join`
    answers `signed` with `cos.signed_ingested_catching_up`, the same call the
    planner chooses the mark with; asking the same-night question here would
    fail every legitimate mark this lane can produce."""
    if not r.get("signed"):
        return f"{r['digest']}:the-ingestion-mark-without-a-signed-note"
    if cos_chips.CHIP_INGESTED in r["before_image"]:
        return f"{r['digest']}:the-thread-already-carried-the-ingestion-mark"
    return None


def _e4(run: dict[str, Any], vault, run_id: str) -> dict[str, Any]:
    rows = chip_join(vault, run_id, run)
    of = "categorize row(s) in the undo ledger"
    if not rows:
        return _answer(4, NA, 0, of, "this run wrote no chip")
    managed = set(cos_chips.CHIPS)
    bad = []
    for r in rows:
        # TWO AXES, TWO RULES (owner ruling 2026-08-22). A priority chip ranks
        # urgency and answers to the matrix; the ingestion mark records a fact
        # and answers to the drop stamp. Judging the mark by the matrix failed
        # run169 for writing exactly what it was told to write.
        if r["chip"] == cos_chips.CHIP_INGESTED:
            problem = _e4_mark(r)
            if problem:
                bad.append(problem)
            continue
        if r["chip"] not in managed:
            bad.append(f"{r['digest']}:chip={r['chip']!r}-not-one-of-the-four")
        elif not r["in_ledger"]:
            bad.append(f"{r['digest']}:not-enumerated")
        elif r["expected_chip"] is None:
            bad.append(f"{r['digest']}:matrix-assigns-no-chip-to-"
                       f"{r['verdict']}/{r['judged_tier']}")
        elif r["chip"] != r["expected_chip"]:
            bad.append(f"{r['digest']}:{r['verdict']}/{r['judged_tier']}-wants-"
                       f"{r['expected_chip']!r}-got-{r['chip']!r}")
        elif [c for c in r["before_image"] if c in managed]:
            bad.append(f"{r['digest']}:the-thread-already-carried-a-managed-chip")
    if bad:
        return _answer(4, FAIL, len(rows), of,
                       f"{len(bad)} of {len(rows)} categorize write(s) breach "
                       f"their axis's rule — priority chips the four-chip "
                       f"(bucket, tier) matrix, the ingestion mark a SIGNED "
                       f"note for the thread's own candidate from any run "
                       f"still inside the catch-up window: {bad[:8]}")
    per: dict[str, int] = {}
    for r in rows:
        per[str(r["chip"])] = per.get(str(r["chip"]), 0) + 1
    marks = sum(1 for r in rows if r["chip"] == cos_chips.CHIP_INGESTED)
    return _answer(4, PASS, len(rows), of,
                   f"all {len(rows) - marks} priority chip(s) are one of the "
                   f"four managed names, match the (bucket, tier) matrix and "
                   f"landed on a bare thread; all {marks} ingestion mark(s) "
                   f"back a signed note for their own thread's candidate, "
                   f"from this run or one still inside the catch-up window "
                   f"({per})")


def _e5(run: dict[str, Any]) -> dict[str, Any]:
    rows = dispatched(run["undo"], "draft")
    of = "draft row(s) in the undo ledger"
    if not rows:
        return _answer(5, NA, 0, of, "this run produced no draft")
    scope = {d.get("conversation_id"): d.get("recipient_scope")
             for d in run["drafts"]}
    bad, seen, twice = [], set(), []
    for r in rows:
        cid = r.get("conversation_id")
        d = r.get("conversation_id_digest") or "?"
        if scope.get(cid) != "original-thread-only":
            bad.append(f"{d}:recipient_scope={scope.get(cid)!r}")
        if cid in seen:
            twice.append(str(d))
        seen.add(cid)
    if bad or twice:
        return _answer(5, FAIL, len(rows), of,
                       (f"{len(bad)} draft(s) name recipients beyond the "
                        f"original thread: {bad[:6]}; " if bad else "")
                       + (f"{len(twice)} conversation(s) were drafted twice: "
                          f"{twice[:6]}" if twice else ""))
    return _answer(5, PASS, len(rows), of,
                   f"all {len(rows)} draft(s) are scoped `original-thread-only` "
                   "and no conversation was drafted twice")


CHECKS: dict[int, Callable[..., dict[str, Any]]] = {
    1: lambda run, vault, rid: _e1(run),
    2: lambda run, vault, rid: _e2(run),
    3: lambda run, vault, rid: _e3(run, vault, rid),
    4: lambda run, vault, rid: _e4(run, vault, rid),
    5: lambda run, vault, rid: _e5(run),
    6: lambda run, vault, rid: _e6(run),
    7: lambda run, vault, rid: _e7(run),
    8: lambda run, vault, rid: _e8(run),
    9: lambda run, vault, rid: _e9(run),
    10: lambda run, vault, rid: _e10(run),
}

# Parent-namespace binds, deferred past this module's own defs (patching the
# facade keeps working: every name below is resolved here once at import, so
# tests that monkeypatch `cos_echecks.X` continue to control behaviour only
# where they did before — the parent re-exports these very objects).
from .cos_echecks import (  # noqa: E402
    AGED_READ_SIGNAL as AGED_READ_SIGNAL,
    ARCHIVE_BUCKETS as ARCHIVE_BUCKETS,
    ARCHIVING_SIGNALS as ARCHIVING_SIGNALS,
    DEFAULT_ARCHIVE_BUCKETS as DEFAULT_ARCHIVE_BUCKETS,
    STALE_ACT_SIGNAL as STALE_ACT_SIGNAL,
    EcheckError as EcheckError,
    FAIL as FAIL,
    NA as NA,
    NEVER_NA as NEVER_NA,
    PASS as PASS,
    PERMITTED_PRIMITIVES as PERMITTED_PRIMITIVES,
    p0_floor_refuses as p0_floor_refuses,
    _cos_driver as _cos_driver,
    vault_of as vault_of,
)
from .cos_echecks_runs import _slice as _slice  # noqa: E402
from .cos_echecks_runs import (  # noqa: E402
    archive_join as archive_join,
    archive_truth_table as archive_truth_table,
    by_conversation as by_conversation,
    capability_digest as capability_digest,
    chip_join as chip_join,
    dispatched as dispatched,
    in_scope as in_scope,
    load_run as load_run,
)

# The E6-E10 checks live in cos_echecks_answers_2.py (second size-ratchet
# cut); bound here so CHECKS below resolves them exactly as before.
from .cos_echecks_answers_2 import (  # noqa: E402,F401
    _TERMINAL as _TERMINAL,
    _e10 as _e10,
    _e6 as _e6,
    _e7 as _e7,
    _e8 as _e8,
    _e9 as _e9,
    _exact_int as _exact_int,
    _grounding_delivery as _grounding_delivery,
    short_chunks as short_chunks,
)

