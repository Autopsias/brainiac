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

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
COUNTERS = ("drafts_created", "marked", "archived", "ingestion_candidates")

# A re-verification of an EARLIER run's draft is not a creation (4¾(a)).
NOT_A_CREATION = {"existing-draft-visible", "draft-expired", "draft-discarded"}

# SKILL.md v5.36 (ING-05) Disposition 4¾(e): the four ingestion/attachment
# fields are REQUIRED on every appended row. `ingestion_candidates` was being
# emitted until run 41 and then simply stopped, and nothing noticed for 15
# runs — a counter no rule requires is a counter that can vanish.
REQUIRED_INGESTION_FIELDS = (
    "ingestion_in_scope", "ingestion_candidates", "ingestion_held",
    "attachment_lane",
)
ATTACHMENT_LANES = {
    "downloads-mounted", "blocked-no-downloads-mount", "not-exercised",
}


def _rows(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():  # an absent ledger is zero rows, not a crash
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _date_of(path: Path) -> str | None:
    m = DATE_RE.search(path.name)
    return m.group(1) if m else None


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


LEDGERS = (
    ("_cos_drafts_ledger_*.jsonl", "drafts_created", counts_draft),
    ("_cos_chip_ledger_*.jsonl", "marked", counts_mark),
    ("_cos_archive_ledger_*.jsonl", "archived", counts_archive),
    ("_cos_ingestion_ledger_*.jsonl", "ingestion_candidates", counts_ingestion),
)


def reconcile(ops_dir: Path) -> dict:
    """{date: {counter: {"reported": int, "ledgered": int, "shortfall": int}}}."""
    reported: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTERS, 0))
    metrics = ops_dir / "_cos_metrics.jsonl"
    if metrics.exists():
        for row in _rows(metrics):
            date = row.get("date")
            if not date:
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


# -- run-obligation guard (ROL-02 / E22 precedent) ---------------------------
# On/off probe only. The AUTHORITATIVE parser is `brain.overlay.parse_ingest`;
# this deliberately re-derives nothing but "is any lane open at all", because a
# second full parser here is exactly the drift the taxonomy spec warns about.
_RULE_RE = re.compile(r"^\s*-\s*([a-z0-9][a-z0-9-]*)\s*:\s*(always|propose|never)\b",
                      re.MULTILINE)


def ingest_lane_open(vault: Path) -> tuple[bool, str, float | None]:
    """(enabled, reason, mtime). Absent file ⇒ the whole category feature is OFF."""
    path = vault / "overlay" / "cos" / "ingest.md"
    if not path.exists():
        return False, f"no {path} — category feature is OFF, guard not applicable", None
    mtime = path.stat().st_mtime
    rules = _RULE_RE.findall(path.read_text(encoding="utf-8"))
    open_rules = [r for r, d in rules if d != "never"]
    if not open_rules:
        return False, f"{len(rules)} rule(s) parsed, all `never` — no lane is open", mtime
    return True, f"{len(open_rules)}/{len(rules)} rule(s) open (non-`never`)", mtime


def _pre_contract(ops: Path, run_tag: str) -> tuple[dict, str]:
    for name in (f"cos_contract_pre_{run_tag}.json", f"_cos_contract_pre_{run_tag}.json"):
        p = ops / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(data, dict):
                return data, name
    return {}, ""


def mail_leg_enumerated(ops: Path, run_tag: str) -> tuple[int, str]:
    """Threads the mail leg actually enumerated for this run, from its own
    PRE-contract (the run's authoritative record) with the metrics row as the
    fallback. 0 ⇒ the mail leg was not live, so a zero funnel is honest."""
    data, name = _pre_contract(ops, run_tag)
    if isinstance(data.get("enumerated"), list):
        return len(data["enumerated"]), name
    run = run_tag.rsplit("run", 1)[-1]
    date = (DATE_RE.search(run_tag) or [None])[0]
    for row in _rows(ops / "_cos_metrics.jsonl"):
        if row.get("date") == date and str(row.get("run")) == run:
            return int(row.get("mail_triaged") or 0), "_cos_metrics.jsonl"
    return 0, "no PRE-contract and no metrics row found"


def run_started_at(ops: Path, run_tag: str) -> float | None:
    """Epoch seconds of the run's own enumeration stamp, or None."""
    data, _ = _pre_contract(ops, run_tag)
    stamp = data.get("enumerated_at")
    if not isinstance(stamp, str):
        return None
    try:
        dt = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def run_complete(ops: Path, run_tag: str) -> tuple[bool, str]:
    """Has this run reached its own terminal artifacts?

    The ledgers are written at reconcile time, so an IN-FLIGHT run legitimately
    shows an empty ingestion ledger. Scoring that as FAIL would make the guard
    cry wolf on every night it is run early — and a guard that cries wolf gets
    ignored, which is how E16 ended up trusted while vacuous."""
    date = (DATE_RE.search(run_tag) or [None])[0]
    run = run_tag.rsplit("run", 1)[-1]
    for name in (f"_cos_nightly_{run_tag}.md", f"_cos_metrics_row_{run_tag}.json",
                 f"cos_metrics_row_{run_tag}.json"):
        if (ops / name).exists():
            return True, name
    for row in _rows(ops / "_cos_metrics.jsonl"):
        if row.get("date") == date and str(row.get("run")) == run:
            return True, "_cos_metrics.jsonl"
    return False, ("no nightly report, no per-run metrics row, and no "
                   "_cos_metrics.jsonl entry — the run has not reconciled yet")


def observation_guard(ops: Path, run_tag: str) -> dict:
    """The vacuous-pass guard. See the module docstring for the FAIL rule."""
    enabled, lane_reason, lane_mtime = ingest_lane_open(ops.parent)
    enumerated, mail_source = mail_leg_enumerated(ops, run_tag)
    started = run_started_at(ops, run_tag)
    # Phase 0 loads the overlay ONCE at run start, so a lane opened mid-run (or
    # after it) cannot have stamped that run's candidates. Scoring it as FAIL
    # would blame the rollout for its own cutover moment.
    lane_predates_run = not (enabled and lane_mtime and started and lane_mtime > started)
    ledger = ops / f"_cos_ingestion_ledger_{run_tag}.jsonl"
    rows = _rows(ledger)
    candidates = [r for r in rows if r.get("disposition") == "candidate"]
    stamped = [r for r in candidates if str(r.get("category") or "").strip()]
    complete, complete_reason = run_complete(ops, run_tag)
    out = {
        "run": run_tag,
        "run_complete": complete,
        "run_complete_evidence": complete_reason,
        "ingest_lane_enabled": enabled,
        "ingest_lane_reason": lane_reason,
        "lane_predates_run": lane_predates_run,
        "mail_enumerated": enumerated,
        "mail_source": mail_source,
        "ledger": ledger.name if ledger.exists() else None,
        "ledger_rows": len(rows),
        "candidates": len(candidates),
        "category_stamped_candidates": len(stamped),
    }
    if not complete:
        out["verdict"] = "PENDING"
        out["reason"] = (f"run {run_tag} has not reconciled yet ({complete_reason}) "
                         "— re-run this guard once the nightly report lands")
    elif stamped:
        # Checked BEFORE the not-applicable cases: they exist to suppress a
        # FAIL that would blame the run for conditions outside it, never to
        # hide a real pass. Evidence of the funnel working is always reportable.
        out["verdict"] = "PASS"
        out["reason"] = (f"{len(stamped)} category-stamped candidate(s) in "
                         f"{ledger.name}")
    elif not enabled:
        out["verdict"] = "NOT-APPLICABLE"
        out["reason"] = lane_reason
    elif not lane_predates_run:
        out["verdict"] = "NOT-APPLICABLE"
        out["reason"] = (
            f"the ingest lane was opened at "
            f"{_dt.datetime.fromtimestamp(lane_mtime).isoformat(timespec='seconds')}, "
            f"AFTER this run enumerated at "
            f"{_dt.datetime.fromtimestamp(started).isoformat(timespec='seconds')} — "
            "Phase 0 loads the overlay once at run start, so this run could not "
            "have stamped categories. Score the next run.")
    elif enumerated <= 0:
        out["verdict"] = "NOT-APPLICABLE"
        out["reason"] = (f"mail leg enumerated 0 threads ({mail_source}) — a zero "
                         "funnel is honest when there was nothing to funnel")
    else:
        out["verdict"] = "FAIL"
        out["reason"] = (
            f"ingest lane open ({lane_reason}) and the mail leg enumerated "
            f"{enumerated} thread(s) ({mail_source}), but the run ledger carries "
            f"ZERO category-stamped candidates "
            f"({len(candidates)} candidate row(s), {len(rows)} ledger row(s)). "
            "This is the funnel dead at a stage no counter reports — never a "
            "quiet night (E22 precedent).")
    return out


def _require_ingestion_fields(row: dict) -> None:
    """Refuse an append that drops a Phase-1.6 counter (SKILL.md v5.36 4¾(e)).

    Fails CLOSED and names the field: the run's repair is to read its own
    ingestion ledger, not to lower a number. `null` counts as absent — the
    defect being fixed is a counter quietly going away, and a null is exactly
    that with extra steps."""
    missing = [f for f in REQUIRED_INGESTION_FIELDS if row.get(f) is None]
    if missing:
        raise ValueError(
            "metrics row is missing required Phase-1.6 field(s): "
            + ", ".join(missing)
            + " — count them from tonight's _cos_ingestion_ledger_<date>-run<N>"
              ".jsonl (SKILL.md v5.36 Disposition 4¾(e), self-eval E29)")
    for f in ("ingestion_in_scope", "ingestion_candidates", "ingestion_held"):
        if isinstance(row[f], bool) or not isinstance(row[f], int) or row[f] < 0:
            raise ValueError(f"`{f}` must be a non-negative integer, got {row[f]!r}")
    if row["attachment_lane"] not in ATTACHMENT_LANES:
        raise ValueError(
            f"`attachment_lane` must be one of {sorted(ATTACHMENT_LANES)}, "
            f"got {row['attachment_lane']!r}")


def _require_ingestion_recount(ops_dir: Path, row: dict) -> None:
    """Refuse an append whose ingestion counters the run's OWN ledger denies.

    WHY (measured, run 64, 2026-08-02). The row said ``ingestion_held: 11``;
    the same run's ledger carries 116 rows disposed ``held``/``no-substance``,
    which is what SKILL.md defines ``ingestion_held`` to be. The row was
    appended anyway and the disagreement only surfaced hours later, in the host
    validator, as an INVALID verdict on a night that could no longer be redone.
    A counter is repaired AT THE COUNTER (E29(c)) — so refuse it at write time,
    when the run is still standing there and can recount.

    The recount is :func:`brain.cos_runverify.ledger_counts`, deliberately:
    one definition of these three counters, shared with the validator that
    re-executes them. A run with no ledger for its own id is E29(a)'s silent
    phase and is left to the observation guard — this function refuses
    disagreement, never absence.
    """
    from brain import cos_runverify                          # noqa: PLC0415

    ledger = ops_dir / f"_cos_ingestion_ledger_{row['date']}-run{row['run']}.jsonl"
    if not ledger.is_file():
        return
    counted = cos_runverify.ledger_counts(_rows(ledger))
    disagree = [f"`{k}`: row says {row.get(k)!r}, {ledger.name} counts {v}"
                for k, v in counted.items() if int(row.get(k) or 0) != v]
    if disagree:
        raise ValueError(
            "metrics row disagrees with a recount of this run's own ingestion "
            "ledger — " + "; ".join(disagree)
            + ". Repair the COUNTER, never the ledger (SKILL.md E29(c)); "
              "`ingestion_held` is every in-scope row disposed `held` OR "
              "`no-substance`, not only the `held` ones")


def host_stamps(ops_dir: Path, date: str, run: str) -> dict:
    """The version stamps for THIS run, read from the HOST's run manifest.

    STA-01: the metrics row's stamps come from the same host source as a
    candidate's. The producer used to copy them into the row, so a run could
    report a bundle it did not execute (or omit them entirely, as run 59 did on
    every one of its 8 candidates). The manifest was frozen at run LAUNCH, so
    it is the only record of what actually ran.
    """
    from brain import cos  # engine-side: ONE definition of the manifest layout

    run_id = f"{date}-run{run}"
    manifest = cos.run_manifest(ops_dir.parent, run_id)
    if manifest is None:
        raise ValueError(
            f"no host run manifest for {run_id} — `brain cos-run-begin` writes "
            "one at run launch, and without it the row's version stamps would "
            "be a producer claim about the producer (exactly what STA-01 "
            "removes). Begin the run through the host, then append the row")
    return {"bundle_version": manifest["bundle_version"],
            "extraction_rules_version": manifest["extraction_rules_version"],
            "skill_sha256": manifest["skill_sha256"]}


def append_metric(ops_dir: Path, row: dict) -> str:
    """Append once by (date, run); refuse a different row for the same key."""
    if not isinstance(row, dict) or not row.get("date") or row.get("run") is None:
        raise ValueError("metrics row requires non-empty `date` and `run`")
    _require_ingestion_fields(row)
    row = {**row, "run": str(row["run"])}
    _require_ingestion_recount(ops_dir, row)
    stamps = host_stamps(ops_dir, row["date"], row["run"])
    for key, host_value in stamps.items():
        claimed = row.get(key)
        # A row that CONTRADICTS the manifest is a finding, not a typo: it
        # means the executing bundle changed under the run, or the row belongs
        # to a different run. Refuse it rather than overwrite the evidence.
        if claimed is not None and str(claimed) != str(host_value):
            raise ValueError(
                f"metrics row claims {key}={claimed!r} but the run manifest for "
                f"{row['date']}-run{row['run']} says {host_value!r} — the host "
                "record wins; investigate which bundle actually ran")
    row = {**row, **stamps}
    path = ops_dir / "_cos_metrics.jsonl"
    for existing in _rows(path) if path.exists() else []:
        if (existing.get("date"), str(existing.get("run"))) != (
                row["date"], row["run"]):
            continue
        if {**existing, "run": str(existing["run"])} == row:
            return "unchanged"
        raise ValueError(
            f"conflicting metrics row for {(row['date'], row['run'])!r}")

    prior = path.read_text(encoding="utf-8") if path.exists() else ""
    payload = (prior.rstrip("\n") + ("\n" if prior else "")
               + json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    # The temp file used to be the fixed name `_cos_metrics.jsonl.tmp`, written
    # with `Path.write_text` (Codex cloud review, 2026-08-07). A symlink planted
    # at that predictable path is FOLLOWED by the open, so the write truncates
    # and overwrites whatever it points at -- outside the vault included -- and
    # the later `os.replace` only swaps the link, long after the damage. A COS
    # run writes here as a documented step, so nothing unusual has to happen.
    #
    # `mkstemp` is the stdlib answer to exactly this: a random name created
    # with O_CREAT|O_EXCL|O_NOFOLLOW-equivalent semantics (it refuses to open an
    # existing path at all), 0600, in the same directory so `os.replace` stays
    # atomic on one filesystem.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="._cos_metrics-",
                                    suffix=".jsonl.tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return "appended"


def main(argv: list[str]) -> int:
    if "--observation-guard" in argv:
        i = argv.index("--observation-guard")
        rest = [a for a in argv[i + 2:] if not a.startswith("--")]
        if i + 1 >= len(argv) or not rest:
            print("usage: cos_reconcile_metrics.py --observation-guard "
                  "<date>-run<N> <cos-ops dir>", file=sys.stderr)
            return 2
        ops = Path(rest[0]).expanduser().resolve()
        if not ops.is_dir():
            print(f"FAIL: no cos-ops dir at {ops}", file=sys.stderr)
            return 2
        res = observation_guard(ops, argv[i + 1])
        if "--json" in argv:
            print(json.dumps(res, indent=2))  # parseable: verdict is IN the object
        else:
            for k, v in res.items():
                print(f"  {k}: {v}")
            print(f"\n{res['verdict']}: {res['reason']}")
        return 1 if res["verdict"] == "FAIL" else 0

    append_path = None
    if "--append" in argv:
        i = argv.index("--append")
        if i + 1 >= len(argv):
            print("usage: cos_reconcile_metrics.py --append <row.json> <cos-ops dir>",
                  file=sys.stderr)
            return 2
        append_path = Path(argv[i + 1]).expanduser().resolve()
    args = [
        a for i, a in enumerate(argv[1:], 1)
        if not a.startswith("--")
        and not (append_path is not None and i == argv.index("--append") + 1)
    ]
    if not args:
        print("usage: cos_reconcile_metrics.py [--json] [--append <row.json>] "
              "<cos-ops dir>", file=sys.stderr)
        return 2
    ops = Path(args[0]).expanduser().resolve()
    if not ops.is_dir():
        print(f"FAIL: no cos-ops dir at {ops}", file=sys.stderr)
        return 2
    if append_path is not None:
        try:
            row = json.loads(append_path.read_text(encoding="utf-8"))
            result = append_metric(ops, row)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(f"{result}: metrics row {(row['date'], str(row['run']))!r}")
        return 0
    report = reconcile(ops)
    if "--json" in argv:
        print(json.dumps(report, indent=2))
    else:
        for date, per in report.items():
            bits = " ".join(
                f"{c}={v['ledgered']}/{v['reported']}" + ("!" if v["shortfall"] else "")
                for c, v in per.items()
            )
            print(f"{date}  (ledgered/reported)  {bits}")
    bad = shortfalls(report)
    if bad:
        print("\nUNDER-REPORTED — the ledgers record work no metrics row accounts for:")
        for date, c, led, rep in bad:
            print(f"  {date}: {c} ledgered {led}, reported {rep}")
        return 1
    print("\nOK: every date's counters cover its ledgers")
    return 0


def _selfcheck() -> None:
    """Prove the observation guard can actually FAIL before it is trusted.

    A gate nobody has watched fail is not a gate — the whole reason this file
    exists is that E16 passed vacuously for 15 runs."""
    import tempfile

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


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        raise SystemExit(main(sys.argv))
