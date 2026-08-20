#!/usr/bin/env python3
"""Join the COS ledgers to `_cos_metrics.jsonl` and fail on an under-report.

WHY THIS EXISTS (measured 2026-07-25 / 2026-07-21). The metrics row is the
instrument every "COS delivered nothing" conclusion rests on, and it was
lying. `_cos_drafts_ledger_2026-07-25-run34.jsonl` records one
`draft-saved-verified` reply draft; every 2026-07-25 metrics row reads
`drafts_created: 0` — because run 34 mutated and appended no row of its own,
and E10 only ever demanded that SOME row exist for the date. Same shape, far
larger, on 2026-07-21: 181 verified archives and 26 verified marks in the
ledgers against `archived: 0, marked: 0` in the one row for that date.

This implements the SKILL.md v5.27 Disposition step 4¾(c) target-day join as a
runnable check, so "a ledgered verified draft and a zero counter must never
coexist silently" is enforced by something that can actually fail, not only by
prose a model is asked to honour.

v5.36 (ING-05) extends the same join to Phase 1.6: `_cos_ingestion_ledger_*`
candidates reconcile against `ingestion_candidates`, and `--append` REFUSES a
row that omits any of the four required ingestion/attachment fields. Measured
2026-07-16..30: `ingestion_candidates` was emitted until run 41, then simply
stopped, and 15 runs passed self-eval anyway — a counter no rule requires is a
counter that can vanish.

    python3 tools/cos_reconcile_metrics.py <vault>/cos-ops
    python3 tools/cos_reconcile_metrics.py --json <vault>/cos-ops

Exit 0 = every date's reported counters cover its ledgers. Exit 1 = a
shortfall (ledgered > reported) on at least one date. Over-reporting is
listed too but is not, on its own, the defect this gate exists to catch.

`--observation-guard <date>-run<N>` is the SEPARATE run-obligation check for
the category-driven funnel (E22 precedent: a propose-only lane has shipped as
a silent no-op here before, and the reconcile join above cannot catch it —
0 ledgered against 0 reported reconciles perfectly).

    python3 tools/cos_reconcile_metrics.py --observation-guard 2026-07-30-run57 \
        <vault>/cos-ops

FAIL when all three hold: the ingest overlay is enabled with at least one
non-`never` rule, the mail leg actually enumerated threads that night, and the
run's ingestion ledger carries ZERO category-stamped candidate rows. That
combination is never a "quiet night" — it is the funnel dead at a stage no
counter reports. Exit 0 = PASS or NOT-APPLICABLE (with the reason named).

ponytail: deliberately CONSERVATIVE — a row is only counted as executed when
its verification says so, so the ledger side under-counts before it over-counts
and the gate never cries wolf. Unknown row shapes are ignored, not guessed.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# batch-2 drain: the three extracted sub-steps, moved verbatim and re-imported
# so every name below keeps its `cos_reconcile_metrics` module path — the
# engine re-executes this file (`cos_runverify_io.checkers`) from EITHER the
# checkout's tools/ or the bundled _assets/tools/, and `cos_contract`,
# `cos_mutate` and src/brain call these attributes off the loaded module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_reconcile_append import (  # noqa: E402,F401
    ATTACHMENT_LANES, REQUIRED_BODY_PASS_FIELDS, REQUIRED_INGESTION_FIELDS,
    SUPERSEDES, _require_ingestion_fields, _require_ingestion_recount,
    append_metric, host_stamps, superseded_run_ts)
from cos_reconcile_guard import (  # noqa: E402,F401
    _pre_contract, ingest_lane_open, mail_leg_enumerated, observation_guard,
    run_complete, run_started_at)
from cos_reconcile_rows import DATE_RE, _date_of, _rows  # noqa: E402,F401

COUNTERS = ("drafts_created", "marked", "archived", "ingestion_candidates",
            "ingestion_dropped")

# A re-verification of an EARLIER run's draft is not a creation (4¾(a)).
NOT_A_CREATION = {"existing-draft-visible", "draft-expired", "draft-discarded"}

# --- the undo ledger, counted ONCE for everybody (s10, 2026-08-16) -----------
#
# WHY THESE THREE LIVE HERE AND NOT IN `cos_mutate.py`, WHERE THEY WERE BORN.
# `applied_counts` is the one definition of "one mutation is a KEY, not a row"
# (s08). Two modules now need it: the apply, which WRITES the counters, and the
# join below, which RECOUNTS them. `cos_mutate.py` cannot be the shared home —
# it is deliberately NOT in the engine asset mirror (`package_clients.py`
# ENGINE_ASSET_FILES), so an installed engine resolving this file out of
# `_assets/tools/` would find no sibling to import and `cos_runverify.checkers()`
# would report the whole toolchain unloadable, i.e. every nightly INCONCLUSIVE.
# This module IS mirrored, so the definition lives here and `cos_mutate` imports
# it back. Nothing about the counting changed in the move; `tests/
# test_cos_mutate.py`'s existing key-derivation tests are the regression proof.
MUTATION_VERBS = ("archive", "categorize", "draft")

#: The states that say a mutation MIGHT have reached the server. `sent` counts —
#: a mutation whose outcome is unknown has already spent its blast radius.
APPLIED_STATES = ("sent", "confirmed", "reconciled", "unknown")

#: verb -> the metrics-row counter it moves. `captured` is deliberately absent:
#: no ledger records it, so nothing here may claim to recount it.
VERB_COUNTER = {"archive": "archived", "categorize": "marked",
                "draft": "drafts_created"}


def applied_counts(rows: list[dict]) -> dict[str, int]:
    """Per verb, how many of these undo-ledger rows might have reached the
    server — one count per idempotency KEY, never per row.

    The ledger is append-only with one row per state TRANSITION, so `intent`
    then `reconciled` for one archive is ONE mutation.

    The key is DERIVED from `(conversation_id, verb)` whenever the row carries a
    conversation id, and only then falls back to the row's own
    `idempotency_key`. The reason `check_plan_binding` already gives: the ledger
    lives in the VM-writable ops zone, and a row carrying SOMEONE ELSE'S key
    would otherwise collapse two mutations into one count. `_undo_row` always
    writes the conversation id, so deriving loses nothing on an honest row and
    refuses the forge. A row with NO conversation id keeps its declared key —
    and cannot hide behind that, because `check_plan_binding` joins on the same
    derivation and a keyless row matches no planned mutation at all.
    """
    latest: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("conversation_id")
        key = (f"{cid}|{row.get('verb')}" if cid
               else str(row.get("idempotency_key")))
        latest[key] = row
    counts = {v: 0 for v in MUTATION_VERBS}
    for row in latest.values():
        if row.get("state") in APPLIED_STATES and row.get("verb") in counts:
            counts[row["verb"]] += 1
    return counts


def _undo_counter(verb: str):
    """A `LEDGERS` counter that reads ONE verb out of an undo ledger."""
    def count(rows: list[dict]) -> int:
        return applied_counts(rows)[verb]
    count.__name__ = f"counts_undo_{verb}"
    return count


def counts_draft(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        status = r.get("status")
        if status in NOT_A_CREATION or r.get("operation") == "same-night-draft-verification":
            continue
        if status == "draft-saved-verified":
            n += 1
        elif r.get("action") == "draft-created" and r.get("verification"):
            n += 1
    return n


def counts_mark(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        status, ver = r.get("status"), r.get("verification")
        if status == "verified-marked":
            n += 1
        elif status == "verified" and r.get("operation") == "category-set-verification":
            n += 1
        elif isinstance(ver, str) and ver.startswith(("server-reread-confirmed", "PASS", "response-confirmed")):
            n += 1
    return n


def counts_archive(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        if r.get("operation") == "archive-summary":
            n += int(r.get("verified_archived") or 0)
            continue
        ver = r.get("verification")
        if ver in ("verified-archived", "response-confirmed") or isinstance(ver, dict):
            n += 1
    return n


def counts_ingestion(rows: list[dict]) -> int:
    """Phase 1.6 rule 8: one row per in-scope thread, `disposition` typed.
    Only a `candidate` row is a staged proposal — `held`/`no-substance` rows
    are the honest record that the phase RAN and found nothing quotable, and
    a lone `zero-eligible` marker counts zero."""
    return sum(1 for r in rows if r.get("disposition") == "candidate")


def counts_bridge_dropped(rows: list[dict]) -> int:
    """Staged candidates the ingest bridge turned into cos-propose drops
    (s03 ING-01). ZERO vs NO INPUT, per B3: a date with NO
    `_cos_ingest_bridge_*` ledger contributes NOTHING here — that is a night
    the bridge never ran on, never one that ran and dropped zero. Both
    readings are probed in tests/test_cos_ingest_bridge_accounting.py.

    DISTINCT conversations, because the bridge ledger is append-only across
    re-runs: an idempotent re-run appends `already-dropped` rows beside the
    first pass's `dropped` rows, and a raw row count would report one night's
    single drop as two. An `already-dropped` row counts only with
    `prior_drop: this-run` — under the no-self-heal contract (attempt 5) that
    is the only kind the bridge still writes; `earlier-run` rows exist in
    HISTORICAL ledgers from the withdrawn cross-run-skip design, were another
    night's delivery, and count nothing here.

    A ROW WITHOUT A CONVERSATION ID NEVER MERGES WITH ANOTHER (s03 finding 3):
    keying every id-less row on `str(cid or "")` collapsed all of them into
    ONE bucket, so `ledgered` under-counted and the shortfall the join exists
    to see read smaller than reality. An id-less row keys on its drop `ident`
    instead (stable across idempotent re-runs, so a re-run's this-run skip
    still dedups against its first pass), and a row with neither counts as
    itself — over-counting the ledgered side cries wolf, under-counting it
    hides a loss, and only one of those is safe."""
    keys: set = set()
    for i, r in enumerate(rows):
        if not (r.get("outcome") == "dropped"
                or (r.get("outcome") == "already-dropped"
                    and r.get("prior_drop") == "this-run")):
            continue
        cid = str(r.get("conversation_id") or "")
        ident = str(r.get("ident") or "")
        keys.add(("cid", cid) if cid else
                 ("ident", ident) if ident else ("row", i))
    return len(keys)


#: (glob, counter, row-counter). Several sources may feed ONE counter; the join
#: sums them per date.
#:
#: THE FIRST THREE HAVE NO v7 PRODUCER, AND ARE KEPT ANYWAY (s10, 2026-08-16).
#: `_cos_drafts_ledger_*`, `_cos_chip_ledger_*` and `_cos_archive_ledger_*` were
#: written by the MODEL leg of the pre-v7 browser-driven lane. Under v7 the model
#: legs run `--tools "Read,Glob"` with editing denied and cannot write a file at
#: all, so nothing has produced one since. They are NOT removed because their
#: input is not absent — 42 files of each are on disk and this join still reports
#: real shortfalls off them (measured 2026-08-16 against the reference vault:
#: 2026-08-01 drafts 12 ledgered / 2 reported, 2026-08-06 marks 38/0, 2026-08-07
#: marks 50/0). Deleting the globs would silence history.
#:
#: THE UNDO LEDGER IS THE v7 SOURCE, and it is what makes this join able to fail
#: again. Measured on the same day, BEFORE this line existed: 2026-08-16 read
#: `archived: reported 14, ledgered 0` and `drafts_created: reported 2,
#: ledgered 0` — the three globs above matched nothing, the ledgered side was 0,
#: and `shortfall = max(0, ledgered - reported)` is 0 whenever the ledgered side
#: is empty. An all-clear that equals no input is the failure mode this whole
#: file exists to catch, so the counter the v7 apply DOES write is now joined
#: too. One glob per counter because one undo ledger carries all three verbs.
LEDGERS = (
    ("_cos_drafts_ledger_*.jsonl", "drafts_created", counts_draft),
    ("_cos_chip_ledger_*.jsonl", "marked", counts_mark),
    ("_cos_archive_ledger_*.jsonl", "archived", counts_archive),
    ("_cos_ingestion_ledger_*.jsonl", "ingestion_candidates", counts_ingestion),
    ("_cos_ingest_bridge_*.jsonl", "ingestion_dropped", counts_bridge_dropped),
    ("_cos_undo_ledger_*.jsonl", "drafts_created", _undo_counter("draft")),
    ("_cos_undo_ledger_*.jsonl", "marked", _undo_counter("categorize")),
    ("_cos_undo_ledger_*.jsonl", "archived", _undo_counter("archive")),
)


def reconcile(ops_dir: Path) -> dict:
    """{date: {counter: {"reported": int, "ledgered": int, "shortfall": int}}}."""
    reported: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTERS, 0))
    metrics = ops_dir / "_cos_metrics.jsonl"
    if metrics.exists():
        rows = _rows(metrics)
        # (v5.62) A row a later row for the same key SUPERSEDES is history, not
        # a second night's work. Summing both would report a rerun's counters
        # twice and quietly widen the cover this join measures — the direction
        # that hides a shortfall rather than crying wolf.
        retired = superseded_run_ts(rows)
        for row in rows:
            date = row.get("date")
            if not date:
                continue
            if (str(date), str(row.get("run")), str(row.get("run_ts"))) in retired:
                continue
            for c in COUNTERS:
                reported[date][c] += int(row.get(c) or 0)

    ledgered: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTERS, 0))
    for pattern, counter, fn in LEDGERS:
        for path in sorted(ops_dir.glob(pattern)):
            date = _date_of(path)
            if date:
                ledgered[date][counter] += fn(_rows(path))

    out: dict[str, dict[str, dict[str, int]]] = {}
    for date in sorted(set(reported) | set(ledgered)):
        out[date] = {
            c: {
                "reported": reported[date][c],
                "ledgered": ledgered[date][c],
                "shortfall": max(0, ledgered[date][c] - reported[date][c]),
            }
            for c in COUNTERS
        }
    return out


def shortfalls(report: dict) -> list[tuple[str, str, int, int]]:
    return [
        (date, c, v["ledgered"], v["reported"])
        for date, per in report.items()
        for c, v in per.items()
        if v["shortfall"]
    ]


def _selfcheck() -> None:
    """Prove the observation guard can actually FAIL before it is trusted.

    A gate nobody has watched fail is not a gate — the whole reason this file
    exists is that E16 passed vacuously for 15 runs."""

    seed = ("---\nsetting: ingest\n---\n"
            "- contract-version: always | lane=both\n"
            "- market-digest: never | lane=both\n")
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        ops = vault / "cos-ops"
        ops.mkdir()
        (vault / "overlay" / "cos").mkdir(parents=True)
        tag = "2026-07-30-run99"
        pre = {"enumerated": ["a", "b", "c"]}
        (ops / f"cos_contract_pre_{tag}.json").write_text(json.dumps(pre))
        led = ops / f"_cos_ingestion_ledger_{tag}.jsonl"

        # 0. an IN-FLIGHT run (no nightly report yet) is PENDING, never FAIL
        (vault / "overlay" / "cos" / "ingest.md").write_text(seed)
        assert observation_guard(ops, tag)["verdict"] == "PENDING"
        assert observation_guard(ops, tag)["candidates"] == 0
        (ops / f"_cos_nightly_{tag}.md").write_text("# done\n")  # run reconciled
        (vault / "overlay" / "cos" / "ingest.md").unlink()

        # 1. lane OFF (no overlay) -> NOT-APPLICABLE, never a false alarm
        led.write_text("")
        assert observation_guard(ops, tag)["verdict"] == "NOT-APPLICABLE"

        # 2. KNOWN-POSITIVE: lane open + mail live + empty ledger -> must FAIL
        (vault / "overlay" / "cos" / "ingest.md").write_text(seed)
        bad = observation_guard(ops, tag)
        assert bad["verdict"] == "FAIL", f"guard did not fire: {bad}"

        # 3. still FAIL when candidates exist but carry NO category stamp —
        #    the exact shape a pre-v5.37 producer emits into a live lane
        led.write_text(json.dumps({"disposition": "candidate", "id": "x"}) + "\n")
        unstamped = observation_guard(ops, tag)
        assert unstamped["verdict"] == "FAIL", f"unstamped passed: {unstamped}"
        assert unstamped["candidates"] == 1
        assert unstamped["category_stamped_candidates"] == 0

        # 4. PASS only on a real category-stamped candidate
        led.write_text(json.dumps(
            {"disposition": "candidate", "id": "x", "category": "contract-version"}) + "\n")
        assert observation_guard(ops, tag)["verdict"] == "PASS"

        # 4b. a lane opened AFTER the run started cannot be blamed on the run
        pre_dated = dict(pre, enumerated_at="2000-01-01T00:00:00Z")
        (ops / f"cos_contract_pre_{tag}.json").write_text(json.dumps(pre_dated))
        led.write_text("")
        late = observation_guard(ops, tag)
        assert late["verdict"] == "NOT-APPLICABLE", f"cutover run blamed: {late}"
        assert late["lane_predates_run"] is False
        # ... but a run that started AFTER the lane opened is scored normally
        pre_now = dict(pre, enumerated_at=_dt.datetime.now(
            _dt.timezone.utc).isoformat().replace("+00:00", "Z"))
        (ops / f"cos_contract_pre_{tag}.json").write_text(json.dumps(pre_now))
        assert observation_guard(ops, tag)["verdict"] == "FAIL"
        (ops / f"cos_contract_pre_{tag}.json").write_text(json.dumps(pre))

        # 5. mail leg not live -> NOT-APPLICABLE (a zero funnel is honest)
        (ops / f"cos_contract_pre_{tag}.json").write_text(json.dumps({"enumerated": []}))
        led.write_text("")
        assert observation_guard(ops, tag)["verdict"] == "NOT-APPLICABLE"

        # 6. exit codes: FAIL is the only non-zero
        (ops / f"cos_contract_pre_{tag}.json").write_text(json.dumps(pre))
        assert main(["x", "--observation-guard", tag, str(ops)]) == 1
    print("selfcheck OK — the observation guard fires on a known-positive")


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.cos_reconcile_metrics", sys.modules[__name__])
from tools.cos_reconcile_steps import main  # noqa: E402

if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        raise SystemExit(main(sys.argv))
