"""What a pass REPORTS: the exit codes, the tally, and the metrics row.

Moved verbatim out of `cos_ingest_bridge` (file-size ratchet split); every
comment travels with its code and no behaviour changes. The facade re-imports
every name, so `tools.cos_ingest_bridge` keeps exporting them.
Covered by the `tests/test_cos_ingest_bridge_*.py` slices.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

from brain import cos
from brain.notes import sha256_text

from tools.cos_ingest_bridge_store import _bridge_ident, _conv_key


EXIT_OK = 0
EXIT_BACKPRESSURE = 3
#: The hourly `brain-nightly` rebuild holds the same lock for up to 90 minutes.
#: That is CONTENTION, not a refused candidate, and it gets its own code so the
#: nightly reports it as what it is instead of dying 18 on an escaped traceback.
EXIT_WRITER_BUSY = 4
EXIT_REFUSED = 5


#: The run fails (exit 5) when this many DISTINCT conversations quarantined in
#: one pass. Default 5: the measured per-candidate anomalies of eight review
#: rounds were one or two threads a night, while a systemic cause (an
#: unreadable corpus, a lost manifest file) hits the whole candidate set — the
#: reference nights stage ~8+ candidates (run 59 staged 8) — so 5 lets the odd
#: thread through and still fails a night whose whole input is inconsistent.
QUARANTINE_MAX_ENV = "BRAIN_COS_BRIDGE_QUARANTINE_MAX"
DEFAULT_QUARANTINE_MAX = 5


def _quarantine_max() -> int:
    try:
        n = int(os.environ.get(QUARANTINE_MAX_ENV, "") or DEFAULT_QUARANTINE_MAX)
    except ValueError:
        return DEFAULT_QUARANTINE_MAX
    return n if n >= 1 else DEFAULT_QUARANTINE_MAX


def _recon():
    """``cos_reconcile_metrics`` (B3: the counter's one home), reached the same
    way as a script and as `tools.cos_ingest_bridge` — a bare import works only
    on the first, and silently lost the counter on every suite path."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import cos_reconcile_metrics                                  # noqa: PLC0415

    return cos_reconcile_metrics


def _finish_report(report: dict, outcomes: list[dict]) -> tuple[dict, int]:
    """Tally the pass and derive status + exit code, threshold included."""
    def _n(*kinds: str) -> int:
        return sum(1 for o in outcomes if o.get("outcome") in kinds)

    refused = [{"conversation_id": o["conversation_id"], "reason": o["reason"],
                "detail": o.get("detail", "")}
               for o in outcomes if o.get("outcome") == "refused"]
    quarantines = [{"conversation_id": o["conversation_id"],
                    "reason": o.get("reason", ""),
                    "detail": o.get("detail", "")}
                   for o in outcomes if o.get("outcome") == "quarantined"]
    # the threshold unit is the CONVERSATION — a duplicate row of a
    # quarantined conversation must not double it toward the limit
    q_keys = {_conv_key(str(o.get("conversation_id") or ""))
              for o in outcomes if o.get("outcome") == "quarantined"}

    qmax = _quarantine_max()
    report.update({
        "refused": refused, "dropped": _n("dropped", "dropped (dry-run)"),
        "never": _n("never"), "already_dropped": _n("already-dropped"),
        "quarantined": len(q_keys), "quarantines": quarantines,
        "quarantine_max": qmax,
        "manifest_lines": sum(len(o.get("manifest_lines") or [])
                              for o in outcomes)})
    if len(q_keys) >= qmax and q_keys:
        # MASS INCONSISTENCY IS A DIFFERENT EVENT from one odd thread: at or
        # over the threshold the run fails loudly (the nightly dies before the
        # mutation lane). Everything already settled this pass stays settled —
        # drops are real, stamped and idempotent.
        report["status"] = "quarantine-overflow"
        report["error"] = (f"{len(q_keys)} conversation(s) quarantined — at or "
                           f"over the {QUARANTINE_MAX_ENV} threshold ({qmax}); "
                           "the input is inconsistent at a scale one odd "
                           "thread cannot explain. The bridge refused to call "
                           "this night clean")
        return report, EXIT_REFUSED
    if refused:
        report["status"] = "refused"
        return report, EXIT_REFUSED
    report["status"] = "quarantined" if q_keys else "ok"
    return report, EXIT_OK


def _observed_dropped(vault, run_id: str, candidates: list[dict],
                      excluded: frozenset[str] | set[str] = frozenset(),
                      written: dict[str, dict] | None = None) -> int:
    """DISTINCT CONVERSATIONS among this run's candidates whose drop THIS
    PASS itself wrote, read back off disk against the sha `cos.propose`
    returned — held in this process's memory, so it is a host observation of
    a host action, never a record anything else could plant. Deliberately
    blind to the bridge ledger: this is the `reported` side of the reconcile
    join, and the join's `ledgered` side is `counts_bridge_dropped` over that
    ledger, so reading the count off the same file made `reported ==
    ledgered` by construction and the shortfall structurally zero. Two
    independent observations can genuinely disagree — a ledger row claiming a
    drop that is on no disk is exactly the shortfall the join must see.

    Since attempt 14 there is no delivered-resolution leg: every pass
    re-proposes, so a PRE-CLAIM idempotent re-run still observes N through
    its OWN writes — a truthful N is never superseded by a 0 there. A
    POST-CLAIM re-run is different since attempt 15: its re-drops are
    replay-quarantined (excluded below), it observes fewer, and the
    superseding row plus the reconcile shortfall NAME that — the same loud
    treatment a degraded (quarantining) re-run always got, pointing at the
    replay-rejected evidence. Any drop-dir file this pass did not write
    (planted, leftover, another pass's) counts nothing.

    The unit is the CONVERSATION because the ledgered side counts distinct
    conversations too (owner rule, attempt 5 #5). ``excluded`` is the
    conversation keys THIS PASS refused or quarantined: whatever file exists
    for one of those is the partial state that was parked, not a delivery of
    this pass — counting it pushed `reported` above `ledgered` and masked a
    genuinely lost drop behind `max(0, …)`."""
    written = written or {}
    seen: set[str] = set()
    for row in candidates:
        ident = _bridge_ident(run_id, str(row.get("conversation_id") or ""))
        key = ident.rsplit("-", 1)[-1]
        if key in excluded:
            continue
        w = written.get(key)
        if not (w and w.get("ident") == ident and w.get("sha256")):
            continue
        p = cos.proposal_drop_dir(vault) / f"{ident}.md"
        try:
            if (p.is_file() and sha256_text(p.read_text(encoding="utf-8"))
                    == w["sha256"]):
                seen.add(key)
        except OSError:
            pass
    return len(seen)


def _update_metrics(vault, run_id: str, *, now: _dt.datetime,
                    candidates: list[dict], excluded: set[str],
                    written: dict[str, dict] | None = None) -> dict:
    """Record `ingestion_dropped` on the run's metrics row (B3: defined in
    tools/cos_reconcile_metrics.py) by APPENDING a row naming the one it
    supersedes (REP-02). Best-effort: a missing prior row is the judgment leg's
    failure to surface, not this function's to synthesize.

    THE COUNT IS THIS RUN'S TOTAL, OBSERVED ON DISK (`_observed_dropped`), not
    what this invocation happened to drop and NOT what the bridge ledger says:
    the join counts the ledgered side off that ledger, so a reported side read
    from the same file could never disagree with it. A PRE-CLAIM idempotent
    re-run re-proposes and so still observes N through its own verified
    writes — a truthful N is never superseded by a 0 there. What this counter
    can detect: a bridge ledger asserting drops that do not exist
    (shortfall), and a pass whose candidates degraded — a quarantined re-run,
    including a POST-CLAIM one whose re-drops replay-quarantined (attempt
    15), observes fewer and the superseding row names it.
    """
    recon = _recon()
    ops = cos.run_ops_dir(vault)
    path = ops / "_cos_metrics.jsonl"
    if not path.is_file():
        return {"updated": False, "reason": "no metrics row to extend"}
    dropped = _observed_dropped(vault, run_id, candidates,
                                excluded=excluded, written=written)
    prior = recon._rows(path)
    day, n = run_id[:10], run_id.rsplit("run", 1)[-1]
    # by run_id, else by the (date, run) key the row itself carries
    mine = ([r for r in prior if r.get("run_id") == run_id]
            or [r for r in prior
                if str(r.get("date")) == day and str(r.get("run")) == n])
    if not mine:
        return {"updated": False, "reason": "no metrics row for this run"}
    # The metrics row is MOUNT-RESIDENT input (attempt 16, finding 6): a
    # non-numeric `ingestion_dropped` used to raise out of this best-effort
    # function AFTER the pass wrote its drops, killing the run as a false
    # "refused". A value that is not a number is simply not "already
    # recorded" — fall through and supersede it with the observed truth.
    try:
        recorded = int(mine[-1]["ingestion_dropped"] or 0)
    except (KeyError, TypeError, ValueError):
        recorded = None
    if recorded == dropped:
        # Nothing changed — a re-run does not need a superseding row, and
        # appending one every night would churn the file for no new fact.
        return {"updated": False, "reason": "already recorded",
                "ingestion_dropped": dropped}
    row = dict(mine[-1], ingestion_dropped=dropped)
    row["run_ts"] = (now.strftime("%Y-%m-%dT%H:%M:%S.")
                     + f"{now.microsecond // 1000:03d}Z")
    row[recon.SUPERSEDES] = str(mine[-1].get("run_ts"))
    try:
        return {"updated": True, "append": recon.append_metric(ops, row)}
    except ValueError as exc:
        return {"updated": False, "reason": str(exc)[:300]}
