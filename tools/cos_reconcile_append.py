"""The append lane of `cos_reconcile_metrics` — the write-time gates on a metrics row (batch-2 drain).

The required-field/recount/stamp gates, the `SUPERSEDES` spelling and
`append_metric` moved verbatim out of `cos_reconcile_metrics` and re-imported
by it, so `recon._require_ingestion_fields(...)` (the way
`src/brain/cos_runverify_metrics.py` calls history mode) and
`append_metric` keep their original module path.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_reconcile_rows import _rows  # noqa: E402

# SKILL.md v5.36 (ING-05) Disposition 4¾(e): the four ingestion/attachment
# fields are REQUIRED on every appended row. `ingestion_candidates` was being
# emitted until run 41 and then simply stopped, and nothing noticed for 15
# runs — a counter no rule requires is a counter that can vanish.
REQUIRED_INGESTION_FIELDS = (
    "ingestion_in_scope", "ingestion_candidates", "ingestion_held",
    "attachment_lane",
)

# SKILL.md v5.49 (EXT-07) Disposition 4¾(e): the three BODY-PASS fields are
# required on the same terms, for the identical reason one layer down.
# `body_open_cap`/`body_open_actual` were emitted by runs 61-68 only because a
# run invented them; runs 69-100 stopped, and `cos_runverify` —
# `check_body_open_count`, built to recount `body_open_actual` against the
# ledger — has returned DEGRADED on every night since.
#
# They are a SEPARATE tuple because the two callers ask different questions.
# `--append` is a WRITE-TIME gate on a run happening now: it refuses all seven.
# `cos_runverify.check_metrics_row` SCORES HISTORY, and every row before this
# bump legitimately predates these three — retro-FAILing 40-odd nights on a
# field their bundle never named is a wolf-cry, and the engine already has the
# right answer for a counter that predates its check: DEGRADED, in
# `check_body_open_count`.
REQUIRED_BODY_PASS_FIELDS = (
    "body_open_cap", "body_open_actual", "body_budget",
)
ATTACHMENT_LANES = {
    "downloads-mounted", "blocked-no-downloads-mount", "not-exercised",
}


def _require_ingestion_fields(row: dict, *, body_pass: bool = False) -> None:
    """Refuse an append that drops a Phase-1.6 counter (SKILL.md v5.36 4¾(e),
    extended v5.49 4¾(e) to the three body-pass fields).

    Fails CLOSED and names the field: the run's repair is to read its own
    ingestion ledger, not to lower a number. `null` counts as absent — the
    defect being fixed is a counter quietly going away, and a null is exactly
    that with extra steps.

    ``body_pass`` DEFAULTS OFF, and that direction is deliberate: the WRITE
    path (`append_metric`) opts INTO the stricter check, while every reader —
    including a PINNED engine whose `cos_runverify` predates this signature and
    calls with no keyword at all — gets history mode. The other default would
    have made an old engine + this new tools/ copy retro-FAIL 40 nights on a
    field their bundle never named. See REQUIRED_BODY_PASS_FIELDS."""
    required = REQUIRED_INGESTION_FIELDS + (
        REQUIRED_BODY_PASS_FIELDS if body_pass else ())
    missing = [f for f in required if row.get(f) is None]
    if missing:
        raise ValueError(
            "metrics row is missing required Phase-1.6 field(s): "
            + ", ".join(missing)
            + " — count them from tonight's _cos_ingestion_ledger_<date>-run<N>"
              ".jsonl (SKILL.md v5.36/v5.49 Disposition 4¾(e), self-eval E29)")
    ints = ["ingestion_in_scope", "ingestion_candidates", "ingestion_held"]
    if body_pass:
        ints += ["body_open_cap", "body_open_actual"]
    for f in ints:
        if isinstance(row[f], bool) or not isinstance(row[f], int) or row[f] < 0:
            raise ValueError(f"`{f}` must be a non-negative integer, got {row[f]!r}")
    if body_pass and not str(row["body_budget"]).strip():
        raise ValueError(
            "`body_budget` must name the budget the opens were read to "
            "(e.g. '4000 extracted characters' or '6000 raw page fallback') — "
            "two nights read to different budgets are two different instruments")
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
    re-executes them.

    AN ABSENT LEDGER IS NOT A PASS WHEN THE ROW CLAIMS WORK (measured, run
    108, 2026-08-09). This used to return silently on a missing ledger, on the
    reasoning that a silent Phase 1.6 is E29(a)'s business and this function
    refuses disagreement, never absence. Run 108 appended its metrics row at
    23:26:32 and wrote its ingestion ledger at 23:32:47 — six minutes later —
    so the gate that exists to catch exactly this row's error had nothing to
    compare against and let `ingestion_held: 96` through against a ledger that
    counts 115. A counter that CANNOT be recounted is not a counter that
    reconciles. So: a row claiming in-scope ingestion work is refused until
    the ledger it was supposedly counted from exists. A row claiming none is
    still left to the observation guard, which is the check that knows whether
    the night owed any.
    """
    from brain import cos_runverify                          # noqa: PLC0415

    ledger = ops_dir / f"_cos_ingestion_ledger_{row['date']}-run{row['run']}.jsonl"
    if not ledger.is_file():
        if int(row.get("ingestion_in_scope") or 0) > 0:
            raise ValueError(
                f"metrics row reports ingestion_in_scope="
                f"{row['ingestion_in_scope']} but {ledger.name} does not exist, "
                "so the counters it claims cannot be recounted from anything. "
                "Phase 1.6 rule 8 writes the ledger FIRST and the row is "
                "counted from it (SKILL.md E29(c)) — write the ledger, then "
                "append the row")
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


#: (v5.62, REP-02) The field a RERUN's row uses to account for the row its own
#: earlier attempt left behind. `_cos_metrics.jsonl` is append-only by design and
#: that design is right — a metrics row is evidence, and evidence is not edited
#: in place (E29(c), REP-01). But a run that safe-stops early, is corrected, and
#: RE-RUNS under the SAME manifest then has two truths for one key and no way to
#: say which is current. Measured, run 111 (2026-08-10): the first attempt
#: safe-stopped on a stale sign-in banner and appended a row reading
#: `mail_triaged: 0`, `ingestion_in_scope: 0`; the rerun enumerated 304/304 and
#: wrote a 118-row ledger, and its row could not be appended at all — the append
#: guard correctly refused a conflicting row for the same key, so the night's
#: real counters exist only in a side file the verifier does not read.
#:
#: The rule that settles it: the rerun APPENDS ITS OWN ROW naming the `run_ts`
#: of the row it supersedes. Nothing is edited, nothing is deleted, the history
#: stays readable in order — and every counter reads the LATEST row for the run.
SUPERSEDES = "supersedes_run_ts"


def superseded_run_ts(rows: list[dict]) -> set[tuple[str, str, str]]:
    """(date, run, run_ts) of every row a LATER row for the same key retired."""
    return {(str(r.get("date")), str(r.get("run")), str(r.get(SUPERSEDES)))
            for r in rows if r.get(SUPERSEDES)}


def append_metric(ops_dir: Path, row: dict) -> str:
    """Append once by (date, run); refuse a different row for the same key.

    (v5.62) UNLESS IT DECLARES WHAT IT SUPERSEDES. A rerun under the same
    manifest may append a second row for the key when it names the `run_ts` of
    the row it replaces (`supersedes_run_ts`) and that row is actually there.
    The ledger stays append-only and the history stays intact; what changes is
    that the run can finally SAY which of its two rows is current, instead of
    being forced to choose between an editable ledger and a lost night.
    """
    if not isinstance(row, dict) or not row.get("date") or row.get("run") is None:
        raise ValueError("metrics row requires non-empty `date` and `run`")
    _require_ingestion_fields(row, body_pass=True)
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
    siblings = [r for r in (_rows(path) if path.exists() else [])
                if (r.get("date"), str(r.get("run"))) == (row["date"], row["run"])]
    supersedes = str(row.get(SUPERSEDES) or "").strip()
    for existing in siblings:
        if {**existing, "run": str(existing["run"])} == row:
            return "unchanged"
    if siblings and not supersedes:
        raise ValueError(
            f"conflicting metrics row for {(row['date'], row['run'])!r} — this "
            f"key already has {len(siblings)} row(s) and the ledger is "
            f"append-only. A RERUN under the same manifest appends its own row "
            f"carrying `{SUPERSEDES}: <the earlier row's run_ts>`; nothing here "
            "is ever edited or deleted (REP-01/REP-02, E29(c))")
    if supersedes:
        if not siblings:
            raise ValueError(
                f"metrics row declares {SUPERSEDES}={supersedes!r} but "
                f"{(row['date'], row['run'])!r} has no earlier row at all — a "
                "supersession with nothing to supersede is a claim about "
                "history that history does not carry")
        known = {str(r.get("run_ts")) for r in siblings}
        if supersedes not in known:
            raise ValueError(
                f"metrics row declares {SUPERSEDES}={supersedes!r}, which "
                f"matches no earlier row for {(row['date'], row['run'])!r} "
                f"(this key carries run_ts {sorted(known)}) — name the row you "
                "are replacing or the chain cannot be read")
        if str(row.get("run_ts") or "").strip() == supersedes:
            raise ValueError(
                f"metrics row supersedes its OWN run_ts ({supersedes!r}) — a "
                "row cannot replace itself, and the chain would not terminate")

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
