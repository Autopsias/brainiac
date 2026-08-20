"""Self-eval report, repairs, metrics-row, mutation-counter, and vocabulary checks."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from . import cos
from . import cos_runverify_stamps as stamps

#: "## 🧪 Run-integrity — E-checks (16/30 passed, 1 repair round)".
_REPAIR_HEADER_RE = re.compile(r"(\d+)\s+repair\s+rounds?\b", re.IGNORECASE)
#: v5.59's Repairs section, and the bullets under it.
_REPAIRS_SECTION_RE = re.compile(
    r"^#{2,4}\s*(?:\S+\s+)?REPAIRS\b[^\n]*\n(.*?)(?=^#{1,4}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL)
_BULLET_RE = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)


def check_repairs(vault, run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """(a2) A repair round is ITEMISED, not just counted.

    WHY THIS EXISTS (measured, four consecutive nights). A run that finds its
    own artifact wrong repairs it in flight and prints a number — and that
    number is the only trace, so the repair reaches nobody and the same defect
    returns:

    * run 105: "Repair round 1 corrected the ingestion held counter and
      normalized all four never-category rows" — the counter rule it worked
      out that night ("`ingestion_held` must include both explicit held and
      no-substance rows") was never written down, and run 108 reproduced the
      identical error three nights later,
    * run 75 and run 106 both print "**0 repair rounds**" in the header of a
      document whose body describes counter repairs ("metrics append succeeded
      after counter repair"; "ledger counters reconcile after two counter-only
      repairs") — the count is prose, so it can contradict the page it sits on,
    * run 104: "1 placement repair" — no artifact anywhere says what was
      placed, or where,
    * run 108: "Body-open sequence is contiguous 1-19 after a bookkeeping
      repair" — a `body_open_seq` renumber is a LEDGER edit, and
      `check_body_order` then scored the repaired sequence as if the run had
      drawn it that way.

    So the count is RECOUNTED from the list, exactly as every other counter in
    this file is recounted from its ledger. What each repair touched has to be
    written down for the list to exist, and that written line is the thing
    that can become doctrine.

    VERSION-GATED, like `body_open_seq`: a bundle before v5.59 was never told
    to write the section, and retro-FAILing forty nights on a heading their
    doctrine never named is the wolf-cry this file refuses elsewhere. Those
    runs DEGRADE with the contradiction named, which is how run 75 and run 106
    read today.
    """
    ops = cos.run_ops_dir(vault)
    report = ops / f"_cos_nightly_{run_id}.md"
    if not report.is_file():
        report = ops / f"_cos_run_report_{run_id}.md"
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        return _row("repairs", INCONCLUSIVE,
                    f"no readable run report for {run_id} ({exc}) — the host "
                    "cannot tell what this run repaired",
                    reexecuted=False)

    gated = _bundle_at_least(str(manifest.get("bundle_version") or ""), (5, 59))
    declared = _REPAIR_HEADER_RE.search(text)
    section = _REPAIRS_SECTION_RE.search(text)
    itemised = len(_BULLET_RE.findall(section.group(1))) if section else None

    if declared is None:
        if not gated:
            # NON-WOLF-CRY: a pre-v5.59 bundle was never asked for the count,
            # and a run that claims nothing has made no claim to contradict.
            # The DEGRADED path below is for the runs that DID claim a repair
            # and left no record of it — 104, 105 and 108, not forty nights.
            return _row("repairs", PASS,
                        f"{report.name} states no repair-round count, and the "
                        f"bundle that ran ({manifest.get('bundle_version')}) "
                        "predates v5.59's Repairs section — nothing is claimed "
                        "here, so nothing is contradicted",
                        reexecuted=False)
        return _row("repairs", FAIL,
                    f"{report.name} states no repair-round count at all, so a "
                    "repair this run made to its own artifacts would leave no "
                    "trace. v5.59 requires the count in the run-integrity "
                    "header and one line per repair under `## 🔧 Repairs`",
                    reexecuted=False)
    n = int(declared.group(1))
    if itemised is None:
        if n == 0:
            return _row("repairs", PASS,
                        f"{report.name} declares 0 repair rounds and lists "
                        "none — nothing was repaired in flight",
                        reexecuted=True)
        return _row("repairs", DEGRADED if not gated else FAIL,
                    f"{report.name} declares {n} repair round(s) and carries no "
                    "`## 🔧 Repairs` section — the count is the only record, so "
                    "what was repaired, in which artifact, is unrecoverable "
                    "(run 104's 'placement repair' is this exact shape)",
                    reexecuted=True)
    if itemised != n:
        return _row("repairs", DEGRADED if not gated else FAIL,
                    f"{report.name} declares {n} repair round(s) but its "
                    f"`## 🔧 Repairs` section itemises {itemised} — a count "
                    "that disagrees with the list beneath it is the run-75 / "
                    "run-106 shape ('0 repair rounds' in the header of a page "
                    "describing counter repairs)",
                    reexecuted=True)
    return _row("repairs", PASS,
                f"{n} repair round(s) declared and {itemised} itemised in "
                f"{report.name} — the count survives a recount from its own list",
                reexecuted=True)


def expected_check_count(manifest: dict[str, Any]) -> tuple[int | None, str]:
    """How many E-checks the bundle THAT RAN defines — or why we cannot know.

    Since MAN-01 the count is FROZEN INTO THE MANIFEST at launch, from the
    bytes that were about to run, so it survives the bundle shipping a new
    version. It has to be: the fallback below is digest-verified against a file
    that has ALWAYS changed by validation time, so before the freeze this
    answered ``None`` on every real run — runs 101-106 each scored
    ``degraded`` here, which meant a run reporting ZERO of its 30 checks and a
    run reporting all 30 scored identically. Re-deriving from TODAY's file
    would score the run against a bundle it never executed, so the fallback
    stays digest-verified and stays honest about failing.
    """
    frozen = manifest.get("expected_echecks")
    if isinstance(frozen, int) and not isinstance(frozen, bool) and frozen > 0:
        return frozen, (f"{frozen} check(s) frozen into the run manifest at "
                        f"launch from the bundle that ran "
                        f"({manifest.get('bundle_version')})")
    path = manifest.get("skill_path")
    want = str(manifest.get("skill_sha256") or "")
    if not path:
        return None, "the run manifest names no skill path"
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, (f"the bundle that ran ({p}) is no longer on disk, so its "
                      "check set cannot be counted")
    got = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if want and got != want:
        return None, (f"{p} no longer hashes to the digest the manifest froze "
                      f"({want[:12]}… vs {got[:12]}…) — those bytes are gone, so "
                      "the count this run owed cannot be re-derived")
    n = len({int(m) for m in _SKILL_ECHECK_RE.findall(text)})
    if not n:
        return None, f"{p} defines no `- **E<n>** ·` self-eval checks to count"
    return n, f"{n} check(s) defined by {p.name} @ {got[:12]}…"


def check_metrics_row(vault, run_id: str, manifest: dict[str, Any],
                      rows: list[dict[str, Any]], recon: Any) -> dict[str, Any]:
    """(b) The metrics row exists, carries its required fields + host stamps,
    and its ingestion counters SURVIVE A RECOUNT from the run's own ledger."""
    row = metrics_row(vault, run_id)
    if row is None:
        side = cos.run_ops_dir(vault) / f"_cos_metrics_row_{run_id}.json"
        hint = (f" (a per-run side file {side.name} exists but was never "
                "appended — the appended row is the row of record)"
                if side.exists() else "")
        return _row("metrics_row", FAIL,
                    f"no row for {run_id} in _cos_metrics.jsonl{hint}",
                    reexecuted=True)

    # (v5.62, REP-02) A rerun under the same manifest may append a second row —
    # the ledger is append-only and stays that way — but it must SAY which row
    # it retires. Two silent rows for one key is not history, it is two answers
    # with no rule for choosing, and "the last one" would then mean "whichever
    # was written most recently", which is a habit and not a record.
    history = metrics_rows(vault, run_id)
    if len(history) > 1:
        seen: list[str] = []
        for later in history[1:]:
            names = str(later.get(_SUPERSEDES) or "").strip()
            if not names or names not in seen + [str(history[0].get("run_ts"))]:
                return _row("metrics_row", FAIL,
                            f"{len(history)} rows for {run_id} in "
                            "_cos_metrics.jsonl and one of them declares no "
                            f"`{_SUPERSEDES}` naming an earlier row's `run_ts` "
                            "— the ledger is append-only, so a corrected rerun "
                            "APPENDS a row that says what it replaces; two "
                            "undeclared rows for one run leave every counter "
                            "with two answers and no rule (REP-02)",
                            reexecuted=True)
            seen.append(str(later.get("run_ts")))
    superseded = (f" (row of record is the LATEST of {len(history)}; "
                  f"{len(history) - 1} superseded by a corrected rerun, kept "
                  "in place)" if len(history) > 1 else "")

    if recon is not None:
        try:
            # body_pass=False: this scores HISTORY. Every row before the v5.49
            # bump legitimately predates `body_open_cap`/`body_open_actual`/
            # `body_budget`, and retro-FAILing those nights on a field their
            # bundle never named is a wolf-cry. `check_body_open_count` already
            # carries the right answer for a counter that predates its check.
            recon._require_ingestion_fields(row, body_pass=False)
        except ValueError as exc:
            return _row("metrics_row", FAIL, str(exc), reexecuted=True)

    counted = ledger_counts(rows)
    disagree = [f"{k}: row says {row.get(k)!r}, the ledger counts {v}"
                for k, v in counted.items() if int(row.get(k) or 0) != v]
    if disagree:
        return _row("metrics_row", FAIL,
                    "the metrics row disagrees with a host RECOUNT of this "
                    "run's own ingestion ledger — " + "; ".join(disagree),
                    reexecuted=True)

    mutation, dispatched = check_mutation_counters(vault, run_id, row)
    if mutation is not None:
        return mutation

    # `frozen`, not `stamps`: this module imports `cos_runverify_stamps as
    # stamps`, and a local of that name hid the module for the rest of the scope.
    frozen = {"bundle_version": manifest.get("bundle_version"),
              "extraction_rules_version": manifest.get("extraction_rules_version"),
              "skill_sha256": manifest.get("skill_sha256")}
    wrong = [f"{k}: row says {row.get(k)!r}, the manifest froze {v!r}"
             for k, v in frozen.items()
             if row.get(k) is not None and str(row.get(k)) != str(v)]
    if wrong:
        return _row("metrics_row", FAIL,
                    "the metrics row CONTRADICTS the run manifest — "
                    + "; ".join(wrong)
                    + ". The host record wins; investigate which bundle ran",
                    reexecuted=True)
    absent = [k for k in frozen if row.get(k) is None]
    if absent:
        return _row("metrics_row", DEGRADED,
                    f"counters recount clean against the ledger, but the row "
                    f"carries no host-derived {', '.join(absent)} — it predates "
                    "STA-01's host stamps, so what produced it is not provable "
                    "from the row itself",
                    reexecuted=True)
    return _row("metrics_row", PASS,
                "present, all four Phase-1.6 fields, host stamps match the run "
                "manifest, and both recounts hold — all three ingestion "
                "counters against the ingestion ledger and all three mutation "
                f"counters against the undo ledger ({dispatched})"
                + superseded,
                reexecuted=True)


def check_mutation_counters(vault, run_id: str, row: dict[str, Any]
                            ) -> tuple[dict[str, Any] | None, str]:
    """The four MUTATION counters, recounted from the undo ledger.

    ``(problem_row_or_None, the recount as text)`` — the denominator rides back
    with the verdict so the PASS can NAME what it recounted against. A control
    that reports "the recount holds" without its numbers is unauditable, and it
    is what let the all-zero row read as agreement for eleven archives.

    WHY THIS EXISTS (measured, run 145, 2026-08-16). ``check_metrics_row``
    recounted the three INGESTION counters and nothing recounted the four
    mutation ones, so run 145's row of record read
    ``marked 0, archived 0, captured 0, drafts_created 0,
    mutation_lane "none-read-only"`` after 11 archives, 3 chips and 2 drafts —
    and PASSED. That is not a cosmetic wrong number. ``mutation_counts()`` is
    the corroboration ``unledgered_mutations`` and ``check_plan_binding`` use to
    tell a REMOVED artifact from a quiet night: with the counters stuck at zero,
    a VM that deleted this run's undo ledger bought a PASS from
    ``check_plan_binding`` — "this run dispatched no mutation" — on a night that
    archived eleven threads. The counters ARE the anti-vacuity input, so the one
    number nothing recounted was the number every absence argument rested on.

    TWO OUTCOMES, AND THE SPLIT IS THE POINT.

    * The row reports counters that CONTRADICT the ledger — a number was written
      and it is wrong — is a FAIL, on the same terms as the ingestion recount
      directly above.
    * The row's mutation counters are ALL ZERO beside an undo ledger that
      records dispatches: the counters were never written at all. That is
      INCONCLUSIVE, not FAIL. It is the pre-s08 vintage (the apply did not
      update the row) and it is also what a truncated apply leaves behind, and
      the artifacts cannot tell those apart from tampering. Scoring it PASS is
      the vacuous instrument; scoring it FAIL asserts a cause the evidence does
      not carry. It is not a pass, and it says why.

    Recounted through ``cos_mutate.dispatched_counters`` — IMPORTED, never
    restated. The apply writes the counters through the same map and the same
    per-key ledger fold, so the writer and the recount cannot drift into two
    notions of "one mutation" (a ledger row is a state TRANSITION; a mutation is
    a key).
    """
    mutate = _cos_mutate()
    if mutate is None:
        # `checkers` already scores the absent toolchain as INCONCLUSIVE
        return None, "toolchain absent, not recounted"
    did = mutate.dispatched_counters(vault, run_id)
    dispatched = ", ".join(f"{k}={v}" for k, v in sorted(did.items()))
    disagree = [f"{k}: row says {row.get(k)!r}, the undo ledger counts {v}"
                for k, v in sorted(did.items()) if int(row.get(k) or 0) != v]
    if not disagree:
        return None, dispatched
    reported = [int(row.get(k) or 0) for k in mutate.VERB_COUNTER.values()]
    lane = str(row.get("mutation_lane") or "")
    if not any(reported):
        return _row("metrics_row", INCONCLUSIVE,
                    "this run's metrics row records ZERO mutations of every "
                    f"kind (mutation_lane {lane!r}) while its own undo ledger "
                    "records " + ", ".join(disagree)
                    + ". The row of record was never updated by the apply, so "
                      "the counters every absence argument corroborates against "
                      "— `unledgered_mutations`, `check_plan_binding` — are "
                      "disarmed, and the host cannot tell an un-updated row "
                      "from a removed one. An instrument that cannot fail is "
                      "not a pass",
                    reexecuted=True), dispatched
    return _row("metrics_row", FAIL,
                "the metrics row disagrees with a host RECOUNT of this run's "
                "own undo ledger — " + "; ".join(disagree)
                + ". Both numbers were written; they cannot both be what "
                  "reached the mailbox",
                reexecuted=True), dispatched


def check_ledger_vocabulary(run_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """(b2) The ingestion ledger uses the CLOSED vocabulary E29(b) names.

    WHY THIS EXISTS. E29(b) has said "a `held_reason` from the managed set"
    since v5.36, and `disposition` has been a three-word enum since rule 8 was
    written — but nothing host-side ever checked either, so every run coined
    its own words (see `_HELD_REASONS`). That is not a tidiness problem, it is
    how the counters and the other checks go quietly wrong:

    * run 106 disposed 15 rows `no-new-substance`; they left `ingestion_held`
      and were accounted nowhere,
    * run 108 wrote its 19 substance verdicts as
      `held_reason: "no-substance-or-already-represented"`, and
      `check_body_pass` — which keys on the word — passed reporting that the
      ledger contained no substance verdict at all,
    * run 105 noticed its own drift and *hand-normalized four ledger rows
      mid-run* ("normalized all four never-category rows to
      `disposition: no-substance` with `held_reason: never-category`"), which
      is a LEDGER edit: precisely what E29(c) forbids, done because no gate
      caught the drift at the point it was written.

    Scored on every bundle. Unlike `body_open_seq` this needs no new field —
    both keys have been REQUIRED since ING-05, so a run of any vintage owed
    them, and the vocabulary they are drawn from has never changed.
    """
    bad_disp, bad_reason, bad_dedup, missing_reason = _vocabulary_counts(rows)
    problems = _vocabulary_problems(bad_disp, bad_reason, bad_dedup,
                                    missing_reason)
    if problems:
        return _row("ledger_vocabulary", FAIL,
                    f"{len(rows)} ingestion ledger row(s): " + "; ".join(problems)
                    + ". These words DEFINE the counters and select the rows "
                      "every other Phase-1.6 check scores, so an invented one "
                      "does not read as a variant — it reads as absence "
                      "(E29(b); SKILL.md Phase 1.6 rules 1½/1¾/6/8)",
                    reexecuted=True)
    return _row("ledger_vocabulary", PASS,
                f"all {len(rows)} ingestion ledger row(s) carry a rule-8 "
                "disposition, every non-candidate row a `held_reason` from "
                "the managed set, and every `dedup_check` one of rule 5's "
                "three words",
                reexecuted=True)

# Parent/IO binds, deferred past this module's own defs.
from .cos_runverify import (  # noqa: E402
    DEGRADED as DEGRADED,
    FAIL as FAIL,
    INCONCLUSIVE as INCONCLUSIVE,
    PASS as PASS,
    _SKILL_ECHECK_RE as _SKILL_ECHECK_RE,
    _bundle_at_least as _bundle_at_least,
    _row as _row,
    _vocabulary_counts as _vocabulary_counts,
    _vocabulary_problems as _vocabulary_problems,
)
from .cos_runverify_io import (  # noqa: E402
    _SUPERSEDES as _SUPERSEDES,
    ledger_counts as ledger_counts,
    metrics_row as metrics_row,
    metrics_rows as metrics_rows,
)
from .cos_runverify_contract import _cos_mutate as _cos_mutate  # noqa: E402
