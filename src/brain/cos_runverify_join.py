"""Corpus-join, body-open-count, and artifact-naming checks."""
from __future__ import annotations

import json
import re
import datetime as _dt
from typing import Any

from . import cos
from .cos_chips import p0_floor_refuses

def check_body_open_count(run_id: str, rows: list[dict[str, Any]],
                          row: dict[str, Any] | None) -> dict[str, Any]:
    """(c3) ``body_open_actual`` equals the opens the run's own ledger carries.

    WHY THIS EXISTS (measured by the S19 census, 2026-08-02). Every other
    Phase-1.6 counter is recounted against the ledger — `ingestion_in_scope`,
    `ingestion_candidates` and `ingestion_held` by ``check_metrics_row``, and
    the mutation counters by ``cos_reconcile_metrics``. ``body_open_actual`` is
    not, by either. It is the counter for the one phase that costs real work,
    and until now the run could put any number in it.

    Run 64 is the known positive already on disk: its row says
    ``body_open_actual: 0`` while its own ledger carries 4 rows stamped
    ``body_opened: true``. Those two artifacts contradict each other and no
    instrument looked. (In run 64 the row was the truthful side and the ledger
    the fabricated one — which is exactly why a DISAGREEMENT, not a direction,
    is what this reports.)

    Corpus false-positive rate: zero. Runs 61 and 63 — the only other runs that
    emit the field — agree exactly (5/5 and 68/68). Runs 57-60 predate it and
    are left alone: an absent counter is ``check_metrics_row``'s business
    through ``_require_ingestion_fields``, not a disagreement.
    """
    opened = sum(1 for r in rows if r.get("body_opened"))
    claimed = (row or {}).get("body_open_actual")
    if rows and not any("body_opened" in r for r in rows):
        return _row("body_open_count", DEGRADED,
                    "no row in this run's ingestion ledger carries a "
                    "`body_opened` stamp, so a claimed open count cannot be "
                    "recounted host-side — the bundle predates EXT-01, and a "
                    "zero that matches a ledger with nothing to count is not "
                    "agreement",
                    reexecuted=True)
    if claimed is None:
        return _row("body_open_count", DEGRADED,
                    f"this run's metrics row states no `body_open_actual`, so "
                    f"the {opened} open(s) its ledger carries cannot be joined "
                    "to a claim — the bundle predates the counter",
                    reexecuted=True)
    try:
        claimed_n = int(claimed)
    except (TypeError, ValueError):
        return _row("body_open_count", FAIL,
                    f"`body_open_actual` is {claimed!r}, which is not a count; "
                    f"the ledger carries {opened} open(s)",
                    reexecuted=True)
    if claimed_n != opened:
        return _row("body_open_count", FAIL,
                    f"the metrics row claims `body_open_actual: {claimed_n}` "
                    f"and the run's OWN ingestion ledger carries {opened} row(s) "
                    "stamped `body_opened: true`. One of the two artifacts is "
                    "describing a run that did not happen; the host cannot say "
                    "which, and does not have to — they cannot both be this "
                    "run",
                    reexecuted=True)
    return _row("body_open_count", PASS,
                f"`body_open_actual: {claimed_n}` survives a recount of the "
                f"run's own ingestion ledger ({len(rows)} row(s))",
                reexecuted=True)



#: (v7.3, AGED-01) The owner's floor, recounted a THIRD time. `0` is the live
#: setting (2026-07-26, re-affirmed 2026-07-31: "if it's read it's game") and
#: means NO AGE GATE — never coerce it back to a default by a falsy check.
_AGED_READ_SIGNAL = "aged-read-no-action"
_AGED_READ_MIN_DAYS = 0


def _applied_archive_cids(vault, run_id: str) -> set[str] | None:
    """The conversations this run ACTUALLY archived, off its undo ledger.

    `None` when the undo ledger cannot be read (no vault, missing file) — the
    caller then falls back to the judge's `auto_archive` flag, the STRICTER
    reading, so a run whose apply record is absent is judged as if every
    proposal landed.
    """
    if vault is None:
        return None
    try:
        from .cos.feedback_cli import applied_mutations         # noqa: PLC0415
        path = cos.run_ops_dir(vault) / f"_cos_undo_ledger_{run_id}.jsonl"
        rows = [json.loads(x) for x in
                path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:                                           # noqa: BLE001
        return None
    landed = applied_mutations(rows, verbs=("archive",))
    return {cid for (cid, _verb) in landed}


def _aged_read_bad(rows: list[dict[str, Any]], today,
                   signed_ingested: frozenset[str] | set[str] = frozenset(),
                   archived: set[str] | None = None
                   ) -> list[str]:
    """Every aged-read archive the ledger's OWN facts refuse.

    `signed_ingested` is the same substance-gate escape belt 2 takes (RULE 1):
    a thread whose substance the vault has already SIGNED has satisfied
    "ingested back" and may archive even when its disposition is not
    `no-substance`. The two belts carry the identical rule so they never
    disagree — a signed thread belt 2 archived must not read as `bad` here.

    `archived` is the set of conversations this run ACTUALLY archived on the
    mailbox. Belt 3 scores what LANDED — exactly as its docstring says, and as
    E3 already does off the undo ledger — never the judge's `auto_archive`
    PROPOSAL. That distinction is load-bearing: the two-run signed-ingest drain
    deliberately carries a held, not-yet-signed thread with `auto_archive: True`
    in run N while belt 2 HOLDS it, then archives it in run N+1 once its
    candidate is signed — so scoring the proposal fails every first drain pass.
    `None` means no dispatch record was available, so `auto_archive` is the
    fallback (the stricter reading).
    """
    bad = []
    for r in rows:
        cid_full = str(r.get("conversation_id") or "")
        landed = (r.get("auto_archive") if archived is None
                  else cid_full in archived)
        if r.get("noise_signal") != _AGED_READ_SIGNAL or not landed:
            continue
        cid = str(r.get("conversation_id"))[:16]
        # (RULE 1) THE SUBSTANCE-GATE BACKSTOP. Belt 2
        # (`cos_mutate_plan_aged.aged_read_refusal`) drops a held thread from
        # the plan; this soundness belt FAILS the night if one was archived
        # anyway, so the gate cannot silently regress. A read thread archives
        # ONLY after its substance is vaulted — either the ingest pass cleared
        # it as `no-substance`, OR the vault holds a SIGNED copy of it.
        disp = str(r.get("disposition") or "").strip().lower()
        if disp != "no-substance" and str(r.get("conversation_id") or "") not in signed_ingested:
            bad.append(f"{cid}: disposition {disp or 'unset'!r} — Rule 1 archives "
                       "a read thread only after its substance is vaulted; a "
                       "held or vault-worthy thread waits, never archived blind")
        elif r.get("read_state") != "read":
            bad.append(f"{cid}: read_state {r.get('read_state')!r}")
        elif p0_floor_refuses(r.get("verdict"), r.get("judged_tier")):
            bad.append(f"{cid}: tier {r.get('judged_tier')} on a "
                       f"`{r.get('verdict')}` verdict")
        elif not r.get("body_opened"):
            bad.append(f"{cid}: the body never opened, so the action screens "
                       "could not run")
        else:
            rec = str(r.get("received") or "")[:10]
            try:
                age = (today - _dt.date.fromisoformat(rec)).days
            except ValueError:
                bad.append(f"{cid}: unparseable received {rec!r}")
                continue
            if age < _AGED_READ_MIN_DAYS:
                bad.append(f"{cid}: {age} day(s) old")
    return bad


def check_aged_read_lane(run_id: str, rows: list[dict[str, Any]],
                         *, vault=None, today=None) -> dict[str, Any]:
    """(c8) The aged-read lane archived only what the owner's ruling allows.

    THE THIRD BELT, and it exists because the two before it see different
    evidence and neither is this one. `cos_judge_rules._aged_read_refusal`
    refuses the model's CLAIM off the batch context (the action screens live
    there and nowhere else); `cos_mutate_plan._aged_read_refusal` re-screens
    what the ledger row can prove before a single mutation is dispatched; this
    recounts the same row facts AFTER the fact, off the artifact alone, which
    is the only one of the three a later reader can reproduce.

    IT ALSO STATES THE COUNT WHEN THE COUNT IS ZERO, and that half is not
    decoration. The lane's proposing half is a model flag, so it can fail the
    way run 136's did — the model set `auto_archive: true` on ONE of 57
    eligible `noise` rows and the night read as delivered. A lane that fires on
    a handful of an obviously larger population is a finding, and a verdict
    line that says `0 aged-read archive(s)` is what makes it one instead of a
    silence.

    WHAT COUNTS AS AN ARCHIVE IS WHAT LANDED, not what the judge proposed. The
    substance gate scores the conversations the undo ledger records as actually
    archived (via `applied_mutations`), because the two-run signed-ingest drain
    leaves a held, not-yet-signed thread carrying `auto_archive: True` in run N
    while belt 2 holds it — scoring the proposal would fail the drain's every
    first pass. With no undo ledger to read (unit tests, or an apply record
    that never landed) the proposal flag is the stricter fallback.
    """
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    archived_cids = _applied_archive_cids(vault, run_id)
    claimed = [r for r in rows if r.get("noise_signal") == _AGED_READ_SIGNAL]
    archived = [r for r in claimed
                if (r.get("auto_archive") if archived_cids is None
                    else str(r.get("conversation_id") or "") in archived_cids)]
    # The signed-ingest escape (RULE 1), recomputed off the vault so this belt
    # sees the same signed set belt 2 did. No vault (unit tests) ⇒ empty set,
    # which is the stricter direction: only `no-substance` passes.
    signed = (cos.signed_ingested_catching_up(vault, run_id, rows)
              if vault is not None else set())
    bad = _aged_read_bad(rows, today, signed, archived_cids)
    # WHAT THE COUNT IS COUNTING (2026-09-04). With no undo ledger to read,
    # `archived` above holds the judge's PROPOSAL flag, not a dispatch — so a
    # run that dispatched nothing reported "N aged-read archive(s)" and read as
    # an exercised lane. The fallback is right (it is the stricter reading for
    # the refusals below); the SENTENCE was wrong, because it claimed more than
    # the evidence behind the number. No count changes here — only the word for
    # what it counts, and it is set ONCE for all three verdicts below, because
    # a second spelling is how two of them come to disagree.
    confirmed = archived_cids is not None
    noun = "archive(s)" if confirmed else "PROPOSED archive(s)"
    caveat = "" if confirmed else (
        " — and not one of them is confirmed dispatched: this run left no undo "
        "ledger to read, so the count above is the judge's proposal")
    if bad:
        return _row("aged_read_lane", FAIL,
                    f"{len(bad)} of {len(archived)} aged-read {noun} the "
                    "ledger's own facts refuse: " + "; ".join(bad[:4])
                    + " — the owner's 2026-07-26 ruling archives mail he has "
                      "READ, whose action screens actually RAN and found "
                      "nothing owed (AGED-01)",
                    reexecuted=True)
    if not archived:
        # AN ALL-CLEAR THAT EQUALS NO INPUT IS NOT AN ALL-CLEAR (DOCTRINE §8
        # r3). Nothing rode the lane, so nothing was verified — say that,
        # rather than reciting conditions over an empty set.
        return _row("aged_read_lane", PASS,
                    f"no row rode the aged-read lane tonight ({len(claimed)} "
                    f"claim(s) over {len(rows)} ledger row(s)), so this "
                    "control verified nothing — a persistently empty lane on a "
                    "backlog that qualifies is itself the finding (AGED-01)",
                    reexecuted=True)
    return _row("aged_read_lane", PASS,
                f"{len(archived)} aged-read {noun} from {len(claimed)} "
                f"claim(s) over {len(rows)} ledger row(s); every one is read, "
                f"at or past the owner's floor of {_AGED_READ_MIN_DAYS} day(s), "
                "above P1, and had its body opened so the action screens could "
                f"run{caveat}",
                reexecuted=True)


def check_corpus_join(vault, run_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """(g) The ledger's verdicts correspond to messages the CAPTURE CORPUS
    actually recorded — WIR-03, extending the guards that caught run 64.

    WHY THIS EXISTS. s06 wired ``cos-corpus-append``/``cos-corpus-close`` into
    Phase 1.6 (2026-08-02): the run now saves the message text it read beside
    the verdict it wrote about that text. The ledger ALONE is exactly the
    artifact run 64 proved can be fabricated — a prior run's ledger, filtered
    and rewritten, every stamp this validator already checks left intact
    (``check_body_pass``, ``check_body_open_count`` and ``candidate_stamps``
    all check the ledger against ITSELF). The corpus is a stronger thing to
    join it against: it carries the bytes the judgment was supposedly made
    from, not a second assertion about them.

    THE CHECK, and only this. Every in-scope ledger row (everything but the
    ``zero-eligible`` marker) must resolve to a thread the corpus captured —
    SKILL.md rule 8 states the two are written in lockstep, joined on
    ``conversation_id``. And every row claiming ``body_opened: true`` must
    resolve to a corpus row that actually carries text.

    NOT APPLICABLE when there is no corpus on disk AND no evidence one was
    owed. Corpus
    capture shipped in s06; every run before it — and any run whose bundle
    predates it — never wrote one. A THIRD cause since CAP-02: the corpus
    EXPIRED and the nightly retention fold deleted it, so re-verifying a run
    older than the window finds nothing where a corpus once was. Naming only
    the first two would make this instrument — whose whole job is telling a
    genuine run from a fabricated one — state a false account of a run's
    provenance. Scoring a run INVALID for a capability
    that did not exist when it ran would make the next operator disable this
    check rather than trust it, which is exactly how E16 stayed trusted while
    vacuous (module docstring). Scored ``degraded``: the join could not be
    RE-EXECUTED, the same reason every other unre-executable control here
    scores degraded rather than an unqualified pass.

    THE ONE CASE WHERE A MISSING CORPUS IS THE FINDING (run 68, 2026-08-03).
    That excuse held for every run until one SKIPPED a corpus it owed: run 68's
    ledger claimed three body opens, on a host already capturing, and scoring it
    "not applicable" said the same thing about it as about a run from before
    capture existed. So the three causes are now checked rather than recited —
    :func:`_capture_was_live_by` rules out all three from one artifact — and a
    run whose own ledger claims a body open with no corpus at all FAILS,
    matching what this check already does when the corpus exists and is empty.
    No new field, counter or registry: the ledger and the corpus directory
    already say it.

    WHAT THIS DOES NOT PROVE — say it plainly, per s19's census. Roughly 150
    asserted facts across this run are pure trust because the host has no
    independent channel to the mailbox. This check does not close that gap: a
    run that fabricated a corpus AND a matching ledger would still pass every
    clause below. It converts one TRUST row into a COHERENCE row — the two
    artifacts must agree with each other — and claims nothing stronger.
    """
    missing_row = _corpus_missing_row(vault, run_id, rows)
    if missing_row:
        return missing_row

    (missing, opened_no_text, in_scope_n, bodied_n, corpus_n
     ) = _corpus_join_problems(rows, vault, run_id)

    thread_row = _corpus_missing_thread_row(missing, in_scope_n)
    if thread_row:
        return thread_row

    no_text_row = _corpus_opened_no_text_row(opened_no_text)
    if no_text_row:
        return no_text_row

    return _row("corpus_join", PASS,
                f"{in_scope_n} in-scope ledger row(s) all resolve to a "
                f"thread the corpus captured, and every `body_opened: true` "
                f"row's thread carries corpus text ({bodied_n} of "
                f"{corpus_n} corpus row(s) bodied)",
                reexecuted=True)


def check_artifact_naming(vault, run_id: str) -> dict[str, Any]:
    """(f) Every EVIDENCE artifact carries the run id the HOST assigned.

    WHY THIS EXISTS (measured, run 64). ``cos-run-begin`` assigns the run id
    from the host clock in UTC; run 64 started 00:08 local / 23:08Z and named
    its ledgers from the LOCAL date, so the host froze ``2026-08-01-run64``
    while the run wrote ``…2026-08-02-run64…``. It noticed at metrics-append
    time (``host_stamps`` refused: no manifest for ``2026-08-02-run64``) and
    repaired with ``cp``, not ``mv`` — leaving byte-identical ledger PAIRS under
    two dates. ``cos_reconcile_metrics`` aggregates by date, so it counted the
    duplicates as extra work and reported a false UNDER-REPORTED.

    The morning brief and the decision card are deliberately dated for the
    morning they are READ, so they are excluded; everything else a run writes
    is evidence and belongs to exactly one run id.
    """
    ops = cos.run_ops_dir(vault)
    if not ops.is_dir():
        return _row("artifact_naming", INCONCLUSIVE,
                    f"no run ops dir at {ops}", reexecuted=False)
    want_date, want_run = run_id[:10], _run_number(run_id)
    dated = re.compile(r"(\d{4}-\d{2}-\d{2})-run(\d+)(?!\d)")
    strays = []
    for p in sorted(ops.iterdir()):
        if not p.is_file() or p.name.startswith(_MORNING_DATED_PREFIXES):
            continue
        m = dated.search(p.name)
        if m and m.group(2) == want_run and m.group(1) != want_date:
            strays.append(p.name)
    if strays:
        return _row("artifact_naming", FAIL,
                    f"{len(strays)} artifact(s) name run {want_run} under a "
                    f"date the host never assigned (the manifest froze "
                    f"{run_id}): " + ", ".join(strays[:6])
                    + (" …" if len(strays) > 6 else "")
                    + ". A run has ONE id; a second date prefix double-counts "
                      "every ledger in the metrics join",
                    reexecuted=True)
    return _row("artifact_naming", PASS,
                f"every evidence artifact naming run {want_run} carries the "
                f"host-assigned date {want_date}",
                reexecuted=True)


#: The stamps a candidate row carries ONLY because a drop produced them. Any
#: one of them present is evidence a proposal was dropped, whatever the flag
#: says — which is what makes `proposals_dropped: false` beside one a
#: CONTRADICTION rather than an inapplicability.

# Parent/IO binds, deferred past this module's own defs.
from .cos_runverify import (  # noqa: E402
    DEGRADED as DEGRADED,
    FAIL as FAIL,
    INCONCLUSIVE as INCONCLUSIVE,
    PASS as PASS,
    _MORNING_DATED_PREFIXES as _MORNING_DATED_PREFIXES,
    _corpus_join_problems as _corpus_join_problems,
    _corpus_missing_row as _corpus_missing_row,
    _corpus_missing_thread_row as _corpus_missing_thread_row,
    _corpus_opened_no_text_row as _corpus_opened_no_text_row,
    _row as _row,
)
from .cos_runverify_io import _run_number as _run_number  # noqa: E402
