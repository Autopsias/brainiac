"""INS-01 — the HOST-side validator for a COS run's own artifacts.

WHY THIS EXISTS (measured, 2026-07-31). Run 59 skipped its ENTIRE self-eval:
zero E-check output across its 16 artifacts. E16 — the check written to catch a
candidate with no stamps — never executed, so the miss was invisible, and 8
unstamped candidates went on to be claimed. Doctrine cannot police itself: a
model working under a 3,566-line skill will sometimes skip a step, and the
report it writes about itself is the one artifact that cannot detect that.

So the check lives where it cannot be skipped: HOST-side, in the hourly broker
fold, over the run's artifacts as they sit on disk.

THREE THINGS MAKE THIS MORE THAN THE NEXT INSTRUMENT THAT CANNOT FAIL:

1. FOUR STATES, NOT PASS/FAIL. ``VALID`` / ``VALID_DEGRADED`` / ``INVALID`` /
   ``INCONCLUSIVE`` (the constants live in :mod:`brain.cos`, beside the claim
   gate that reads them). ``VALID_DEGRADED`` never collapses into an ordinary
   pass — it is the state for "correctly-reported degradation", and for a check
   the host could not re-execute. ``INCONCLUSIVE`` — the validator could not
   run — is surfaced as loudly as ``INVALID`` and blocks claiming just as hard,
   because a validator that could not run is not a validator that passed.

2. RE-EXECUTION, NOT MARKER-TRUST. Reading a self-eval block's shape proves
   only that a string was printed. Where a control is mechanically
   re-executable host-side, this RE-EXECUTES it and compares:

     * the OUTCOME CONTRACT verdict is recomputed with ``tools/cos_contract.py``
       from the run's own raw PRE/POST snapshots and ledgers, and compared to
       the block the run recorded;
     * the metrics row's three ingestion counters are RECOUNTED from the run's
       ingestion ledger;
     * the expected self-eval check count is re-derived from THE RUN MANIFEST'S
       skill digest — never from whatever ``SKILL.md`` happens to be deployed at
       validation time, which is the same claim-time-vs-production-time error
       the manifest exists to prevent;
     * the substance verdicts in the ingestion ledger are checked against the
       body reads they assert (``check_body_pass``) — run 64 reported a whole
       Phase 1.6 it never ran, by copying the previous run's ledger;
     * every evidence artifact is checked against the run id the HOST assigned
       (``check_artifact_naming``) — run 64 named its ledgers from the local
       clock, crossed midnight, and left two dated copies of each.

   Where re-execution is genuinely impossible (the run's skill bytes are gone,
   the checker is not installed beside the engine), the row says so and scores
   ``degraded`` — never ``pass``.

3. THE DEGRADE EXEMPTION IS CROSS-ARTIFACT, NOT A MARKER. Run 58 (2026-07-31)
   was a LEGITIMATE degrade: Outlook signed out, the mail leg correctly stopped,
   an honest ``zero-eligible`` ledger marker, and a contract that honestly
   FAILED. Scoring that as a validator failure is how a guard gets muted — which
   is exactly how E16 stayed trusted while vacuous. So a FAILED contract is
   exempt ONLY when the degrade is TOTAL and CONSISTENT across every artifact of
   the run, corroborated against host-observed state (how many candidates the
   host actually received from that run). The run writes the marker, so the
   marker alone proves nothing: a ``zero-eligible`` marker beside a populated
   ledger, non-zero counters, an enumerating mail leg, or candidates the host
   really received is an outright FAIL.

AND IT BLOCKS. The verdict is not a counter. ``brain.cos.claim_drops`` refuses
to bind a candidate whose producing run is not ``VALID``/``VALID_DEGRADED``
(STA-01/STA-02, s01), so an INVALID run's candidates are quarantined rather than
claimed — they never reach the owner batch, the evidence keys, or the signed
drain. A validator that only wrote counters would make "FAILED" cosmetic.

ONLY COMPLETED RUNS ARE SCORED. A run whose manifest-declared artifact set is
incomplete, or whose artifacts are still being written, is PENDING — no verdict
is recorded at all (and an unscored run is INCONCLUSIVE to the claim gate, so
its candidates wait rather than flow). Every verdict records the digest of the
inputs it was computed over, so a changed manifest or a substituted artifact
re-validates on the next pass instead of resting on a cached verdict.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import config, cos, cos_corpus, cos_deploy

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

#: A run must be quiet for this long before it is scored — a nightly writes its
#: ledgers, its report and its metrics row over ~30 minutes, and scoring a
#: half-written run is both a false-alarm generator and a substitution window.
QUIESCE_ENV = "BRAIN_COS_RUN_QUIESCE_SECONDS"
DEFAULT_QUIESCE_SECONDS = 900

#: How many of the newest runs one fold pass looks at. Bounded on purpose: the
#: fold fires hourly and a full re-scan of a year of manifests every hour buys
#: nothing — a run older than this has long since been scored, and its verdict
#: is re-checked only if its inputs change.
DEFAULT_RUN_WINDOW = 10

#: How long a run may sit PENDING before the silence is itself the finding.
#: ``alert`` reads recorded VERDICTS, and a run that never completes never gets
#: one — so the loudest failure this validator has is also the one it cannot
#: see. Measured twice: run 100 (2026-08-08) wrote its PRE snapshot under a
#: drifted name and is unscored to this day, and run 106 (2026-08-09) wrote its
#: report as ``_cos_brief_…md`` where the manifest declared ``_cos_nightly_…md``
#: — a full night of work, 30 self-eval results, and not one host check ever
#: executed on either. A nightly writes for ~30 minutes and the broker fold
#: fires hourly, so a run idle this long is stuck, not slow.
STALLED_PENDING_HOURS = 6

#: How far back the stalled scan looks — in DAYS, not in runs, and that is the
#: load-bearing part. ``alert``'s verdict window is the newest 5 runs, and this
#: deployment fired SIX runs on 2026-08-09 alone: a run idle for 6 hours would
#: have been pushed out of a 5-run window before it was ever old enough to
#: report, so a count-based window here is an instrument that cannot fire. A
#: date-based one is rate-independent, still bounded (a few dozen manifests),
#: and lets a stalled night age out rather than become permanent noise.
STALLED_LOOKBACK_DAYS = 3

#: E-check DEFINITIONS in a chief-of-staff SKILL.md (`- **E16** · …`). Defined
#: in :mod:`brain.cos_deploy` beside the other facts read off a bundle, so the
#: count the manifest FREEZES and the count derived here can never disagree.
_SKILL_ECHECK_RE = cos_deploy.ECHECK_RE
#: E-check RESULTS in a run report. Line-anchored and list-item-anchored on
#: purpose: an Outlook conversation id is base64 and full of `E8XWki0=`-shaped
#: noise, and a substring match over the report counts that as a self-eval.
_REPORT_ECHECK_RE = re.compile(
    r"^\s*[-*]\s*\*{0,2}E0?(\d{1,2})\b[^\n]*?\b(PASS|FAIL|N/?A)\b",
    re.MULTILINE | re.IGNORECASE)

#: placeholder category values a producer invents when the taxonomy did not
#: match (SKILL.md E16: the value is the real id or it is absent — never a
#: stand-in, because `unclassified` is the host's own never-graduable default)
_PLACEHOLDER_CATEGORIES = {"uncategorized", "unclassified", "none", "n/a",
                           "na", "unknown", "null", "-"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_NUMBER_RE = re.compile(r"run([0-9]+)")

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

#: (v5.62) The hold reasons that legitimately END the body pass, so the threads
#: behind them may wear `pass-ended-by-identity-stop`. A wrong conversation
#: opened (`target-identity-mismatch`) is the original stop; a host evaluation
#: that timed out instead of answering (`host-eval-timeout`, v5.60) learned
#: nothing about the conversation, and carrying on blind past a wedged bridge is
#: not a pass either. **A REFUSED navigation is deliberately NOT here** — it
#: opened nothing, moved nothing and touched nothing, so it holds its own thread
#: and the draw continues to the next row.
_PASS_STOPPING_REASONS = {"target-identity-mismatch", "host-eval-timeout"}

#: The one Phase-1.6 hold reason that can ONLY be reached by reading the body.
#: SKILL.md rule 1½ gives every "could not read it" case its own reason
#: (`preview-insufficient` for genuinely unread, `over-cap`,
#: `no-body-access-on-lane`, v5.40's `browser-not-visible`), so `no-substance`
#: asserts "I read this and there was nothing quotable in it".
_READ_IMPLYING_REASON = "no-substance"

#: Artifacts deliberately named for the MORNING the owner reads them, which is
#: the day AFTER a run that starts before midnight. Everything else a run
#: writes is EVIDENCE and carries the host-assigned run id's own date.
_MORNING_DATED_PREFIXES = ("_briefing_", "_decision_card_")


def _row(name: str, status: str, detail: str, *, reexecuted: bool) -> dict[str, Any]:
    return {"check": name, "status": status, "reexecuted": reexecuted,
            "detail": detail}


# -- the re-execution toolchain ------------------------------------------------
# `tools/cos_contract.py` (the OUTCOME CONTRACT checker) and
# `tools/cos_reconcile_metrics.py` (the ledger join + the observation guard)
# are the two controls this validator re-executes rather than reads. They are
# scripts, not package modules, so they are resolved from disk: the dev
# checkout's `tools/`, the copy mirrored into the wheel at `_assets/tools/`
# (tools/package_clients.py keeps it in lockstep), or an explicit override.
# Without them the two most load-bearing re-executions cannot run at all, which
# is INCONCLUSIVE — never a quiet pass.
TOOLS_DIR_ENV = "BRAIN_COS_TOOLS_DIR"


def tools_dir() -> Path | None:
    """Where ``cos_contract.py`` + ``cos_reconcile_metrics.py`` actually live."""
    here = Path(__file__).resolve()
    candidates = []
    override = os.environ.get(TOOLS_DIR_ENV)
    if override:
        candidates.append(Path(override).expanduser())
    # the dev checkout FIRST: `tools/` is the source of truth and `_assets/`
    # its mirror, so preferring the mirror in a checkout would silently run a
    # stale copy of the checker whenever the sync had not been re-run.
    candidates.append(here.parents[2] / "tools")           # dev checkout
    candidates.append(here.parent / "_assets" / "tools")   # installed wheel
    for d in candidates:
        if (d / "cos_contract.py").is_file() and (d / "cos_reconcile_metrics.py").is_file():
            return d
    return None


def _load_script(directory: Path, name: str):
    """Import a `tools/` SCRIPT by path, once, under its own module name."""
    want = directory / f"{name}.py"
    cached = sys.modules.get(name)
    # Re-load if some other caller (a test, a sibling tool) already imported a
    # DIFFERENT file under this bare name — the checker that gets re-executed
    # must be the one this validator resolved, not whatever won an import race.
    if cached is not None and Path(getattr(cached, "__file__", "")) == want:
        return cached
    spec = importlib.util.spec_from_file_location(name, want)
    if spec is None or spec.loader is None:                # pragma: no cover
        raise ImportError(f"cannot load {name} from {directory}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def checkers() -> tuple[Any, Any, str]:
    """``(cos_contract, cos_reconcile_metrics, reason)``; modules are ``None``
    when the toolchain is not on disk beside the engine."""
    d = tools_dir()
    if d is None:
        return None, None, (
            "neither the dev checkout's `tools/` nor the engine's bundled "
            "`_assets/tools/` carries cos_contract.py + cos_reconcile_metrics.py, "
            f"and ${TOOLS_DIR_ENV} names no directory that does — the host "
            "cannot RE-EXECUTE this run's own controls, so it cannot honestly "
            "score the run either")
    # `tools/cos_contract.py` imports `cos_reconcile_metrics` as a sibling.
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        recon = _load_script(d, "cos_reconcile_metrics")
        contract = _load_script(d, "cos_contract")
    except Exception as exc:                               # noqa: BLE001
        return None, None, f"the run checkers at {d} failed to load: {exc}"
    return contract, recon, f"re-executing the checkers at {d}"


# -- artifact observation (host-side, never the run's report of itself) --------

def run_artifacts(vault, run_id: str) -> list[Path]:
    """Every file in the run's ops dir whose NAME names this run.

    Name-scoped deliberately: ``_cos_metrics.jsonl`` is a shared append-only
    file every run touches, so folding its mtime into this run's quiescence
    would make each run look permanently in-flight."""
    ops = cos.run_ops_dir(vault)
    if not ops.is_dir():
        return []
    # `(?!\d)` is load-bearing: a bare substring match makes run 5 swallow every
    # artifact of runs 50-59, and the run that then looks incomplete (or
    # complete) is the wrong one.
    named = re.compile(re.escape(run_id) + r"(?!\d)")
    return sorted(p for p in ops.iterdir() if p.is_file() and named.search(p.name))


def inputs_digest(vault, run_id: str, manifest: dict[str, Any]) -> str:
    """A digest over EVERYTHING the verdict was computed from.

    Recorded with the verdict so a changed manifest, a late artifact or a
    swapped file re-validates on the next pass. A cached verdict over a partial
    artifact set is both a false-alarm generator and a substitution window."""
    h = hashlib.sha256()
    h.update(json.dumps(manifest, sort_keys=True).encode("utf-8"))
    for p in run_artifacts(vault, run_id):
        try:
            st = p.stat()
        except OSError:                                    # pragma: no cover
            continue
        h.update(f"\n{p.name}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))
    # `_cos_metrics.jsonl` is SHARED (every run appends to it), so it is not a
    # run-named artifact — but this run's row in it is an input to the verdict.
    # Without it, a row appended AFTER a first look would leave the earlier
    # "no metrics row" INVALID standing forever.
    h.update(b"\nmetrics:")
    h.update(json.dumps(metrics_row(vault, run_id), sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _quiesce_seconds(explicit: int | None) -> int:
    if explicit is not None:
        return max(0, int(explicit))
    try:
        return max(0, int(os.environ.get(QUIESCE_ENV, DEFAULT_QUIESCE_SECONDS)))
    except ValueError:
        return DEFAULT_QUIESCE_SECONDS


def completion(vault, run_id: str, manifest: dict[str, Any], *,
               now: _dt.datetime, quiesce: int) -> dict[str, Any]:
    """Is this run FINISHED? ``{"complete", "reason", "missing", "quiet_for"}``.

    The completion signal is HOST-OWNED end to end: the expected artifact set
    was frozen by the host in the run manifest at LAUNCH, and presence +
    quiescence are read off the filesystem. Nothing the run writes about itself
    participates."""
    ops = cos.run_ops_dir(vault)
    expected = [str(a) for a in (manifest.get("expected_artifacts") or [])]
    missing = [a for a in expected if not (ops / a).exists()]
    files = run_artifacts(vault, run_id)
    newest = max((p.stat().st_mtime for p in files), default=None)
    quiet_for = None if newest is None else (now.timestamp() - newest)
    if missing:
        return {"complete": False, "missing": missing, "quiet_for": quiet_for,
                "reason": (f"{len(missing)} of {len(expected)} manifest-declared "
                           f"artifact(s) not written yet ({', '.join(missing)})")}
    if quiet_for is not None and quiet_for < quiesce:
        return {"complete": False, "missing": [], "quiet_for": quiet_for,
                "reason": (f"the run's artifacts were last written "
                           f"{int(quiet_for)}s ago (< {quiesce}s quiesce window) "
                           "— scoring a half-written run is a false alarm")}
    return {"complete": True, "missing": [], "quiet_for": quiet_for,
            "reason": (f"all {len(expected)} manifest-declared artifact(s) "
                       "present and quiescent")}


# -- ledger / metrics reading --------------------------------------------------

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ledger_rows(vault, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl")


def ledger_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """The three Phase-1.6 counters, RECOUNTED from the ledger itself.

    ``ingestion_held`` is EVERY in-scope row that is not a staged candidate —
    `held`, `no-substance`, and anything else a run writes in that slot — so
    ``in_scope == candidates + held`` is an arithmetic identity rather than a
    coincidence of three independent sums.

    WHY IT IS SPELLED THIS WAY (measured three times). Run 64 reported
    ``ingestion_held: 11`` against 116 held/no-substance rows. Run 105 hit it
    again and *repaired the counter by hand mid-run*, reporting "ingestion_held
    must include both explicit held and no-substance rows" — and that repair
    reached no rule, so run 108 reproduced it exactly (row 96, ledger 115: the
    96 is the `held` rows alone). The membership test that made those three
    possible is gone: there is no set to be half-remembered any more, only
    "not a candidate". Run 106 shows the other end of the same defect — 15
    rows disposed `no-new-substance` fell out of the old set and out of every
    total at once.
    """
    dispositions = [str(r.get("disposition") or "") for r in rows]
    in_scope = [d for d in dispositions if d != _MARKER_DISPOSITION]
    candidates = sum(1 for d in in_scope if d == "candidate")
    return {
        "ingestion_in_scope": len(in_scope),
        "ingestion_candidates": candidates,
        "ingestion_held": len(in_scope) - candidates,
    }


def _run_number(run_id: str) -> str:
    m = _RUN_NUMBER_RE.search(run_id)
    return m.group(1) if m else run_id


#: (v5.62, REP-02) See ``tools/cos_reconcile_metrics.SUPERSEDES``. One spelling,
#: two sides: the writer refuses a second row for a key that does not declare
#: it, and the scorer reads the LAST row for the key while keeping the history.
_SUPERSEDES = "supersedes_run_ts"


def metrics_rows(vault, run_id: str) -> list[dict[str, Any]]:
    """EVERY row this run appended, in ledger order.

    Normally one. A run that safe-stopped, was corrected and RE-RAN under the
    same manifest has two — append-only is the design and it is right, so the
    rerun says which of them is current rather than editing either.
    """
    ops = cos.run_ops_dir(vault)
    want_run = _run_number(run_id)
    date = run_id[:10]
    return [row for row in _read_jsonl(ops / "_cos_metrics.jsonl")
            if row.get("date") == date
            and _run_number(str(row.get("run"))) == want_run]


def metrics_row(vault, run_id: str) -> dict[str, Any] | None:
    """This run's row of record in ``_cos_metrics.jsonl``.

    (v5.62) THE LAST ONE, not the first. Measured, run 111: the first attempt
    safe-stopped on a stale banner and appended a row reading `mail_triaged: 0`;
    the corrected rerun enumerated 304/304 and wrote a 118-row ledger. Reading
    the FIRST row scores the retracted night and calls the real one a
    disagreement with its own ledger — which is exactly what happened. The
    history stays readable (``metrics_rows``) and ``check_metrics_row`` refuses
    a second row that does not declare what it supersedes, so "last" can never
    mean "whichever was appended most recently and said nothing about why".

    Falls back to the per-run ``_cos_metrics_row_<run>.json`` side file only to
    NAME the shortfall precisely — a side file is the run's draft; the appended
    row is what every counter and every reconciliation reads."""
    rows = metrics_rows(vault, run_id)
    return rows[-1] if rows else None


def host_received_candidates(vault, run_id: str) -> int:
    """How many candidates the HOST actually received from this run.

    Host-observed, and therefore the corroboration a degrade claim is checked
    against: the run controls its own ledger and its own markers, but not the
    sidecars the host wrote when it took delivery of a drop."""
    n = 0
    for m in cos._pending_metas(vault):
        if str(m.get("run_id") or "") == run_id:
            n += 1
    for q in cos.quarantined_claims(vault):
        if str(q.get("run_id") or "") == run_id:
            n += 1
    return n


# -- the individual controls ---------------------------------------------------

def check_self_eval(vault, run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """(a) The run reported its self-eval, and reported ALL of it.

    The expected count is re-derived from THE RUN MANIFEST'S skill digest, not
    from whatever ``SKILL.md`` is deployed now — the deployed bundle changes
    between a run and its validation (that is the whole reason the manifest is
    frozen at launch), and counting against the wrong bundle is the same
    claim-time-vs-production-time error the manifest exists to prevent.
    """
    report = cos.run_ops_dir(vault) / f"_cos_nightly_{run_id}.md"
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        return _row("self_eval", INCONCLUSIVE,
                    f"the run report {report.name} is unreadable ({exc}) — the "
                    "host cannot tell whether the self-eval ran",
                    reexecuted=False)
    found = {int(n) for n, _ in _REPORT_ECHECK_RE.findall(text)}

    expected, why = expected_check_count(manifest)
    if expected is None:
        return _row("self_eval", DEGRADED,
                    f"{len(found)} self-eval check result(s) in "
                    f"{report.name}, but the EXPECTED count is not derivable "
                    f"host-side: {why}. Presence alone proves a string was "
                    "printed, so this scores degraded, never valid",
                    reexecuted=False)
    if not found:
        return _row("self_eval", FAIL,
                    f"the run report {report.name} carries ZERO self-eval check "
                    f"results; the bundle the host froze for this run "
                    f"({manifest.get('bundle_version')}) defines {expected}. "
                    "The run skipped its entire self-eval — the exact run-59 "
                    "failure this validator exists to catch",
                    reexecuted=False)
    # The SET, not the count. Counting alone lets a run report E1-E28 plus an
    # invented E40 and score PASS with E29 never checked — the same
    # "the instrument cannot fail" shape this validator exists to catch, one
    # level up. Measured: run 60's report numbers its block E01-E29 with labels
    # that do not correspond to the doctrine's checks at all, and a count-only
    # test cannot see that. The ids are all this check can bind; it binds them.
    missing = sorted(set(range(1, expected + 1)) - found)
    if missing:
        return _row("self_eval", FAIL,
                    f"{report.name} reports {len(found)} of {expected} self-eval "
                    f"checks defined by the run's own bundle "
                    f"({manifest.get('bundle_version')}); "
                    f"missing E{', E'.join(str(m) for m in missing[:12])}"
                    + (" …" if len(missing) > 12 else ""),
                    reexecuted=False)
    return _row("self_eval", PASS,
                f"{len(found)} self-eval check result(s) reported, against "
                f"{why} — never against whatever SKILL.md is deployed now",
                reexecuted=False)


#: The self-eval header every run report carries, e.g.
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

    stamps = {"bundle_version": manifest.get("bundle_version"),
              "extraction_rules_version": manifest.get("extraction_rules_version"),
              "skill_sha256": manifest.get("skill_sha256")}
    wrong = [f"{k}: row says {row.get(k)!r}, the manifest froze {v!r}"
             for k, v in stamps.items()
             if row.get(k) is not None and str(row.get(k)) != str(v)]
    if wrong:
        return _row("metrics_row", FAIL,
                    "the metrics row CONTRADICTS the run manifest — "
                    + "; ".join(wrong)
                    + ". The host record wins; investigate which bundle ran",
                    reexecuted=True)
    absent = [k for k in stamps if row.get(k) is None]
    if absent:
        return _row("metrics_row", DEGRADED,
                    f"counters recount clean against the ledger, but the row "
                    f"carries no host-derived {', '.join(absent)} — it predates "
                    "STA-01's host stamps, so what produced it is not provable "
                    "from the row itself",
                    reexecuted=True)
    return _row("metrics_row", PASS,
                "present, all four Phase-1.6 fields, host stamps match the run "
                "manifest, and all three ingestion counters survive a recount "
                "from the run's ledger" + superseded,
                reexecuted=True)


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
        elif reason not in _HELD_REASONS:
            bad_reason[reason] = bad_reason.get(reason, 0) + 1

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

    counts: dict[str, int] = {}
    for r in stamped:
        cid = str(r.get("category")).strip()
        counts[cid] = counts.get(cid, 0) + 1
    top, top_n = (max(counts.items(), key=lambda kv: kv[1]) if counts
                  else ("<none>", 0))
    share = top_n / len(scored) if scored else 0.0
    if (len(scored) >= _CATEGORY_DOMINANCE_MIN_ROWS
            and share > _CATEGORY_DOMINANCE_MAX_SHARE):
        problems.append(
            f"one category ({top!r}) covers {top_n} of {len(scored)} in-scope "
            f"rows ({share:.0%}) — over the {_CATEGORY_DOMINANCE_MAX_SHARE:.0%} "
            "blanket-default bar. Every night that demonstrably APPLIED this "
            "taxonomy sits at 20-33%; every blanket-default night at 81-90%. "
            "If this night is honest the repair is the TAXONOMY (one id doing "
            "several ids' work), never this check")

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
    # substance verdicts because either one invalidates an open outright.
    #
    # (1) A `never` CATEGORY COSTS ZERO OPENS. Rule 1¾ excludes on the rule-1½
    #     DRAW, before the body is opened, so a `never` thread that was opened
    #     spent one of the twenty the cap owed to actionable mail — a FAIL even
    #     when the row is ledgered correctly afterwards. Measured: 11 of run
    #     103's 19 opens and 3 of run 108's went to `never` categories.
    # (2) AN EMPTY SHELL IS NOT A BODY. Rule 1½ step 4: an extraction at or
    #     below the 42-character bare-folder shell is a FAILED OPEN, and a row
    #     claiming `body_opened: true` over one claims a read that did not
    #     happen. Scored only where `body_chars` is present — a bundle that
    #     never named the field is never failed against it.
    never_opened = [r for r in rows
                    if r.get("body_opened")
                    and str(r.get("held_reason") or "") == "never-category"]
    if never_opened:
        return _row("body_pass", FAIL,
                    f"{len(never_opened)} row(s) carry `body_opened: true` "
                    "beside `held_reason: \"never-category\"` — rule 1¾ "
                    "excludes a `never` category on the rule-1½ DRAW, BEFORE "
                    "the body is opened, so each of these spent one of the "
                    "twenty opens the cap owed to actionable material. A "
                    "post-hoc exclusion recovers the doctrine and keeps the "
                    "cost (E29(e); measured: 11 of run 103's 19 opens)",
                    reexecuted=True)
    shells = [r for r in rows
              if r.get("body_opened")
              and isinstance(r.get("body_chars"), int)
              and not isinstance(r.get("body_chars"), bool)
              and r["body_chars"] <= _EMPTY_SHELL_CHARS]
    if shells:
        return _row("body_pass", FAIL,
                    f"{len(shells)} row(s) claim `body_opened: true` over an "
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
                    "`target-identity-mismatch`",
                    reexecuted=True)

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
    # NO `over-cap` row may outrank ANY row that was opened — compared against
    # the LOWEST-ranked body the pass actually read, never the highest. (The
    # first cut of this compared against the highest and scored run 102 clean:
    # its 3 starved P1 rows outrank the 3 P3 bodies it opened, but not the P0s
    # it also opened. The probe caught it; a positive-only test never would.)
    open_worst = max(_draw_rank(r) for r in opened)
    starved = [r for r in rows
               if str(r.get("held_reason") or "") == "over-cap"
               and _draw_rank(r) < open_worst]
    if starved:
        names = ["P0", "P1", "other"]
        best = min(_draw_rank(r) for r in starved)
        return _row("body_order", FAIL,
                    f"{len(starved)} in-scope row(s) finished `over-cap` at a "
                    f"HIGHER draw group than a body this run actually opened "
                    f"(the starved set reaches {names[best]}; the lowest group "
                    f"opened was {names[open_worst]}) — rule 1½ draws P0, then "
                    "P1, then the rest, so the cap must bite the LOWEST group "
                    "first. The night's reading budget went to the wrong end "
                    "of the queue",
                    reexecuted=True)

    # (i) the sequence half. `body_open_seq` (v5.51) is the ONLY witness: this
    # ledger holds one row per in-scope thread written in ENUMERATION order,
    # opened and unopened interleaved (run 63's opened rows are scattered the
    # length of its file), so line order is not a fallback in either direction.
    names = ["P0", "P1", "other"]
    seqs = [r.get("body_open_seq") for r in opened]
    if all(s is None for s in seqs):
        if _declares_v551(opened):
            return _row("body_order", FAIL,
                        f"none of this run's {len(opened)} opened rows carries "
                        "`body_open_seq` and its own `bundle_version` says "
                        "v5.51 or later, which requires it — the draw cannot "
                        "be recounted, and rule 8 puts the stamp on the same "
                        "footing as `body_opened`",
                        reexecuted=True)
        if len({_draw_rank(r) for r in opened}) == 1:
            return _row("body_order", PASS,
                        f"all {len(opened)} body open(s) sit in ONE draw group "
                        f"({names[_draw_rank(opened[0])]}) and no `over-cap` "
                        "row outranks them — there is no order here to get "
                        "wrong, stamp or no stamp",
                        reexecuted=True)
        # A pre-v5.51 ledger is not retro-failed against a field its own bundle
        # never named — but its line order is the only ordering signal it left,
        # so print it: a reader deciding whether to look harder should see it.
        seen = "".join(names[_draw_rank(r)][:2] for r in opened)
        return _row("body_order", DEGRADED,
                    f"none of this run's {len(opened)} opened rows carries "
                    "`body_open_seq`, so the order they were DRAWN in cannot "
                    "be recounted — the bundle predates v5.51 and this "
                    "ledger's line order is enumeration order, not open order. "
                    f"No `over-cap` row outranks an open. Line order was: {seen}",
                    reexecuted=True)
    if any(s is None for s in seqs):
        return _row("body_order", FAIL,
                    f"{sum(1 for s in seqs if s is None)} of this run's "
                    f"{len(opened)} opened rows carry no `body_open_seq` while "
                    "others do — a partially-stamped sequence cannot be "
                    "replayed and is not a witness to anything",
                    reexecuted=True)
    elif not all(isinstance(s, int) and not isinstance(s, bool) for s in seqs):
        return _row("body_order", FAIL,
                    "a `body_open_seq` in this run's ingestion ledger is not "
                    f"an integer position: {sorted(map(repr, seqs))[:5]}",
                    reexecuted=True)
    elif sorted(seqs) != list(range(1, len(seqs) + 1)):
        return _row("body_order", FAIL,
                    f"the {len(seqs)} `body_open_seq` value(s) are not a "
                    f"contiguous 1..{len(seqs)} — a gap or a repeat means the "
                    "draw cannot be replayed from the ledger "
                    f"(got {sorted(seqs)})",
                    reexecuted=True)
    ordered = sorted(opened, key=lambda r: r["body_open_seq"])
    ranks = [_draw_rank(r) for r in ordered]
    for i in range(1, len(ranks)):
        if ranks[i] < ranks[i - 1]:
            return _row("body_order", FAIL,
                        f"open #{i + 1} is {names[ranks[i]]} but open #{i} was "
                        f"{names[ranks[i - 1]]} — rule 1½ draws P0, then P1, "
                        "then the rest, and no thread is opened while an "
                        "unopened in-scope thread of a higher group remains. "
                        "Observed `body_open_seq` order: "
                        + "".join(names[r][:2] for r in ranks),
                        reexecuted=True)
    return _row("body_order", PASS,
                f"{len(opened)} body open(s) drawn P0→P1→rest and recounted "
                "from `body_open_seq`; no `over-cap` row outranks an open",
                reexecuted=True)


#: The ONE category primitive that writes without selecting the row. Every
#: other one has to touch the row, and Outlook reads a native selection as an
#: open — SKILL.md v5.51, measured on run 102's SAP thread.
_NON_TOUCHING_CATEGORIZE_PRIMITIVE = "rest-categorize"
_UNREAD_DEFER_REASON = "unread-native-category-deferred"


def action_rows(vault, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(cos.run_ops_dir(vault) / f"_cos_action_ledger_{run_id}.jsonl")


#: metrics-row counters that only a MUTATION can make non-zero
_MUTATION_COUNTERS = ("archived", "marked", "drafts_created", "captured")


def unledgered_mutations(vault, run_id: str, rows: list[dict[str, Any]]) -> str:
    """Did this run mutate the mailbox with NO action ledger to check? (why, or "")

    ``check_unread_touch`` and ``check_target_identity`` both re-execute over
    the action ledger, and both read an EMPTY one as "nothing acted, so nothing
    could act wrongly" — which is true only when the run really did nothing.
    Measured, run 106 (2026-08-09): no ``_cos_action_ledger_…jsonl`` was written
    at all, while the run's own metrics row records 2 verified archives and its
    own report names FIVE unrecovered identity mismatches. Both controls
    returned PASS on 0 rows — two instruments that could not fail, on the one
    night that most needed them.

    So the absence is corroborated against the run's OWN counters, the same
    cross-artifact discipline ``degrade_evidence`` uses: an action ledger that
    exists and simply carries no `categorize` row is an ordinary night and stays
    a PASS.
    """
    if rows:
        return ""
    row = metrics_row(vault, run_id) or {}
    did = {}
    for k in _MUTATION_COUNTERS:
        v = row.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) > 0:
            did[k] = int(v)
    if not did:
        return ""
    return (f"this run's _cos_action_ledger_{run_id}.jsonl is absent or empty, "
            f"but its own metrics row records "
            + ", ".join(f"{k}={v}" for k, v in sorted(did.items()))
            + ". The ledger this control re-executes over does not exist on a "
              "run that mutated the mailbox — read as 'nothing acted', that is "
              "an instrument which cannot fail, so it is INCONCLUSIVE and not a "
              "pass (E1 already makes the missing ledger a FAIL of the run)")


def check_unread_touch(run_id: str, rows: list[dict[str, Any]],
                       unledgered: str = "") -> dict[str, Any]:
    """(c5) No category was written onto a row the run had screened UNREAD.

    WHY THIS EXISTS (measured, run 102, 2026-08-09). The run applied
    ``Held · deadline`` through the native lane to a thread whose own action
    row reads ``unread_before: true``. Its immediate re-read said the row was
    still unread (``unread_immediate_after: true``) and the final census said it
    was read (``unread_final_after: false``) — the flip is ASYNCHRONOUS, so the
    post-write re-read is not evidence and the only honest moment to look is
    before. ``unread-touch`` is a Layer-2 hard deny, so this single defect
    failed E1, E12 and E27 at once, and the run correctly refused to "repair" it
    by marking the row unread again (that would be a second forbidden
    mutation).

    The conservative branch v5.51 requires instead is a DEFERRAL — no category,
    a held row carrying ``held_reason: "unread-native-category-deferred"``, and
    a count in the report — so the check reports those beside the verdict: a
    deferral nobody counts is how the write creeps back.
    """
    cats = [r for r in rows if str(r.get("action") or "") == "categorize"]
    if not cats:
        if unledgered:
            return _row("unread_touch", INCONCLUSIVE, unledgered, reexecuted=True)
        return _row("unread_touch", PASS,
                    f"no `categorize` row in this run's action ledger "
                    f"({len(rows)} row(s)) — nothing could have touched an "
                    "unread row through a category write",
                    reexecuted=True)
    deferred = [r for r in cats
                if str(r.get("held_reason") or "") == _UNREAD_DEFER_REASON]
    executed = [r for r in cats if r not in deferred]
    touched = [r for r in executed
               if r.get("unread_before") is True
               and str(r.get("primitive") or "")
               != _NON_TOUCHING_CATEGORIZE_PRIMITIVE]
    if touched:
        prims = sorted({str(r.get("primitive") or "?") for r in touched})
        return _row("unread_touch", FAIL,
                    f"{len(touched)} of this run's {len(cats)} `categorize` "
                    f"row(s) wrote a category onto a row screened "
                    f"`unread_before: true` via {', '.join(prims)} — a native "
                    "category write must SELECT the row and Outlook reads that "
                    "as an open, so the write IS the unread-touch a Layer-2 "
                    "hard deny forbids (v5.51). The row is DEFERRED instead, "
                    f"ledgered `{_UNREAD_DEFER_REASON}`; it is never repaired "
                    "by marking the message unread again",
                    reexecuted=True)
    unstamped = [r for r in executed if "unread_before" not in r]
    if unstamped:
        return _row("unread_touch", DEGRADED,
                    f"{len(unstamped)} of this run's {len(cats)} `categorize` "
                    "row(s) carry no `unread_before`, so the read state at the "
                    "moment of the write cannot be recounted host-side — the "
                    "bundle predates v5.51, and a post-write re-read is not a "
                    "substitute",
                    reexecuted=True)
    return _row("unread_touch", PASS,
                f"{len(executed)} category write(s), every one onto a row "
                f"screened read (or via `{_NON_TOUCHING_CATEGORIZE_PRIMITIVE}`, "
                f"which does not touch it); {len(deferred)} unread row(s) "
                "deferred and ledgered",
                reexecuted=True)


#: An identity assertion is any action row carrying BOTH id fields — an
#: EXCLUSION list, deliberately, because the event names drift between runs
#: (run 102 writes `native-ui-liveness` for run 104's `liveness-preflight`) and
#: an inclusion list would let a renamed event escape the check unnoticed,
#: which is the vacuous pass this validator exists to prevent. Excluded, both
#: measured on run 104's real ledger: `mutation-stop` RE-STATES the mismatch
#: pair as its stop record (it is not a second mismatch), and `attachment-lane`
#: reuses the same two fields for a DOWNLOAD PATH
#: (`target_intended: "BRAIN_COS_DOWNLOADS_DIR"`). Reading either as a per-row
#: conversation action reports three mismatches where the run made one. A
#: FUTURE event that reuses the fields for something that is not a conversation
#: fails loudly here rather than passing silently — the right direction.
_NON_IDENTITY_EVENTS = ("mutation-stop", "attachment-lane")


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


def check_target_identity(run_id: str, rows: list[dict[str, Any]],
                          unledgered: str = "") -> dict[str, Any]:
    """(c6) Every identity mismatch was GUARDED — detected, recovered, inert.

    WHY THIS EXISTS (owner ruling 2026-08-09, on run 104). The safety property
    is *"no wrong action ever happens"*, not *"no mismatch ever occurs"*. On a
    virtualized ~300-row list the measured mismatch rate is about one open in
    twenty, so E30's old bar of ZERO mismatches demanded luck and punished the
    guard for working: run 104 detected its mismatch, recovered on the one
    bounded re-target, mutated nothing — and scored FAIL.

    v5.53 fails only an UNGUARDED mismatch. That loosening is exactly the kind
    of bar a run grades itself against and can now claim its way past, so the
    claim is RECOUNTED here from the action ledger and never read out of the
    run's prose. A mismatch fails when it MUTATED anything, went UNDETECTED, or
    was NOT RECOVERED — the one bounded re-target failed, was not taken, did not
    differ (E30(e)), or ran past its bound. Recovery must be PROVEN by the
    fields E30(a)/(e) already oblige: `target_produced_pre` on the mismatch row,
    a re-target that NAMES what it changed, and `target_produced ==
    target_intended` on the attempt that landed.

    The recovered count is in the detail on EVERY verdict, pass included — a
    rising mismatch rate that "recovered" absorbs into silence is the same
    disappearance `ingestion_candidates` made at run 41.

    (v5.62) AND A REFUSED NAVIGATION IS NOT ONE OF THEM. Measured, run 111
    (2026-08-10), with the lane's own in-run control CLEAN on the same tab the
    same night (12/12, 0 mismatches): the priority draw met four navigations OWA
    simply refused — every one `url_has_id: false`, `body_chars: 42`,
    `ready_state: complete`, no produced id — and each was scored
    `target-identity-mismatch`, which ended the pass and cascaded 111 rows into
    `pass-ended-by-identity-stop`. Nothing wrong opened; NOTHING opened. So the
    shape is separated here and scored on its own terms, and the separation is
    RECOUNTED from the fields v5.60 already obliges rather than taken from a
    word the run chose — a run cannot relabel a wrong-conversation landing as a
    refusal, because a refusal has no produced id at all (`_is_refusal`).
    """
    asserted = [r for r in rows
                if "target_intended" in r and "target_produced" in r
                and r.get("event") not in _NON_IDENTITY_EVENTS]
    if not asserted:
        if unledgered:
            return _row("target_identity", INCONCLUSIVE, unledgered,
                        reexecuted=True)
        return _row("target_identity", PASS,
                    f"no per-row identity assertion in this run's action "
                    f"ledger ({len(rows)} row(s)) — nothing acted on a row, so "
                    "nothing could act on the wrong one; recovered "
                    "mismatches: 0",
                    reexecuted=True)

    differing = [r for r in asserted
                 if r.get("target_produced") != r.get("target_intended")]
    refused = [r for r in differing if _is_refusal(r)]
    # (v5.62) The click fallback that could not SCROLL its row into the
    # virtualized list never clicked anything, so it has no produced surface to
    # judge — it is the named hold, not a mismatch. It still cannot hide one:
    # producing ANY id means something opened, and the word is then a forgery
    # (caught here and, on the ledger side, by `check_open_instrumentation`).
    unreachable = [r for r in differing
                   if str(r.get("held_reason") or "")
                   == "navigation-refused-row-unreachable"]
    forged = [r for r in unreachable if r.get("target_produced")]
    if forged:
        return _row("target_identity", FAIL,
                    f"{len(forged)} action row(s) carry "
                    "`navigation-refused-row-unreachable` while producing a "
                    "conversation id — a row the fallback never reached cannot "
                    "have produced anything, and an id it DID produce that is "
                    "not the intended one is `target-identity-mismatch`",
                    reexecuted=True)
    mismatched = [r for r in differing
                  if not _is_refusal(r) and r not in unreachable]

    # (v5.62) THE REFUSALS ARE SCORED FIRST, AND ON THEIR OWN OBLIGATION: the
    # ONE bounded re-target is still owed, and on a refusal it is the CLICK
    # path — which is exactly what run 111 never took. All four of its refusals
    # died at `target_attempt: 1`, because a refused navigation leaves the tab
    # on the bare shell with a dozen rows rendered from the TOP of the folder
    # while a priority row is the OLDEST mail in it, so `row-not-rendered` was
    # the honest answer to a fallback that never scrolled. A refusal is only
    # inert if it was RECOVERED or HELD BY NAME; unaccounted, it is a body the
    # run silently did not read.
    refusal_problems = _refusal_followups(asserted, refused)
    if refusal_problems:
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

    if not mismatched:
        return _row("target_identity", PASS,
                    f"{len(asserted)} per-row action(s), every one produced the "
                    f"id it intended or was a navigation OWA REFUSED that took "
                    f"its bounded click re-target; recovered mismatches: 0; "
                    f"navigations refused: {len(refused)}",
                    reexecuted=True)

    # (i) DETECTED. The ledger says the ids differ; the run must say so too. A
    # row asserting `identity_verified: true` over a differing pair is a
    # mismatch nobody saw — and it is also the shape a re-target takes when it
    # claims success without having produced the id it intended.
    undetected = [r for r in mismatched if r.get("identity_verified") is not False]
    if undetected:
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

    # (ii) The mismatch row carries `target_produced_pre` (E30(a), v5.50):
    # without it "never moved" and "moved to the wrong row" are the same record.
    no_pre = [r for r in mismatched if "target_produced_pre" not in r]
    if no_pre:
        return _row("target_identity", FAIL,
                    f"{len(no_pre)} of this run's {len(mismatched)} mismatch "
                    "row(s) carry no `target_produced_pre`, so the ledger "
                    "cannot say whether the action moved the surface to the "
                    "wrong conversation or never moved it at all (E30(a), "
                    "v5.50) — recovery cannot be PROVEN from a record that "
                    "incomplete",
                    reexecuted=True)

    # (iii) INERT. Nothing mutated at or after the first mismatch (E30(b)).
    order, by_ts = _ordered(rows)
    positions = {id(r): i for i, (_, r) in enumerate(order)}
    first_at = min(positions[id(r)] for r in mismatched)
    mutated = [r for _, r in order[first_at:] if r.get("mutation") is True]
    if mutated:
        return _row("target_identity", FAIL,
                    f"{len(mutated)} row(s) carry `mutation: true` at or after "
                    "this run's first identity mismatch — the first mismatch "
                    "ends every mutation leg for the run (E30(b)), and a "
                    "mutation after it is an automatic FAIL, never a "
                    "repair-and-continue. Ordering read from "
                    + ("timestamps" if by_ts else
                       "LEDGER POSITION (not every row carries a `ts`)"),
                    reexecuted=True)

    # (iv) RECOVERED, once, and differently. One bounded re-target per mismatched
    # target: it names what it changed, clicks a different point where both
    # attempts recorded one, and produces the id it intended.
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
    if problems:
        return _row("target_identity", FAIL,
                    f"{len(problems)} of this run's {len(mismatched)} identity "
                    "mismatch(es) were NOT recovered on the one bounded "
                    "re-target: " + "; ".join(sorted(set(problems))[:3])
                    + f". recovered mismatches: {recovered}",
                    reexecuted=True)

    return _row("target_identity", PASS,
                f"{len(asserted)} per-row action(s); navigations refused: "
                f"{len(refused)}; recovered mismatches: "
                f"{recovered} — each DETECTED (`identity_verified: false` over "
                "a `target_produced_pre` pair), each recovered by ONE bounded "
                "re-target that named its change and produced the id it "
                "intended, and zero mutation at or after the stop. Fail-closed "
                "action held; the mismatch is counted, not absorbed",
                reexecuted=True)


#: (v5.60, INS-02) What a v5.60 attempt row owes, per attempt and EVEN WHEN THE
#: ATTEMPT FAILED. `open_method`/`open_url` because run 106 landed every one of
#: its twenty opens on attempt 2 while recording neither, which makes that night
#: unscoreable outright. `eval_ms` because a wedged host bridge and a wrong
#: conversation currently arrive as the same word. The four page facts because
#: "the page never loaded", "the list rendered nothing", "the body never
#: arrived" and "the URL lost its /id/" are four different defects. `hour` and
#: `display_state` because the night fires at an hour daylight never tests, on a
#: machine whose screen state daylight never has. `hold_status` READ FROM THE
#: STATUS FILE because a hold that has lost its tab keeps reporting `holding`.
_ATTEMPT_FIELDS = ("open_method", "eval_ms", "ready_state", "rendered_rows",
                   "body_chars", "url_has_id", "hour", "display_state",
                   "hold_status", "hold_status_source")

#: The in-run control: the SAME fixed daylight burst, re-run inside the night on
#: the same lane. v5.57 made the rehearsal re-anchor to the TOP of the folder
#: while a night draws by PRIORITY across ~115 rows, so the rehearsal and the
#: night have never sampled the same population — which is how four successive
#: fixes each scored 20/20 in daylight while the night kept failing.
_CONTROL_ARTIFACT = "_cos_lane_control_{run_id}.json"


def check_open_instrumentation(vault, run_id: str,
                               ledger: list[dict[str, Any]],
                               acts: list[dict[str, Any]]) -> dict[str, Any]:
    """(c7) The night can be told apart from its instrument.

    TWO THINGS, and only the first is version-gated.

    (1) THE MISLABEL, scored on every bundle that wrote the field. A row whose
    own ``target_attempt`` is 0 was never opened, so it cannot carry
    ``target-identity-mismatch`` — that reason asserts an open happened and
    produced the wrong id. Measured, run 105 (2026-08-09): **108 rows labelled
    ``target-identity-mismatch``, every one carrying ``target_attempt: 0`` and
    ``target_produced: null``**. Read as written that is 108 identity failures;
    it is ONE stop and 108 threads written out behind it, and the v5.48 stop
    clause told the run to write exactly that. The word is now
    ``pass-ended-by-identity-stop``. This is scored off the run's OWN field, so
    no bundle is judged against a field it never named — runs 103, 106 and 108
    pass unchanged, their mismatch rows all carrying ``target_attempt: 2``.

    (2) THE PER-ATTEMPT INSTRUMENTATION and the IN-RUN CONTROL, on v5.60+ only.
    Item B does not close from artifacts: the derivation is correct, page-1
    membership predicts nothing, 26 of 26 neutral daylight opens landed at the
    night's own cadence — and run 108's probe log records ~84% first-attempt
    failure. One transient mode WAS caught in daylight: a navigation wedged
    Chrome's JS bridge for ~2 minutes, and a run whose identity read times out
    in that window records a mismatch. The control is what decides it: if the
    control also fails it is the LANE, if the control passes while the priority
    draw fails it is the DRAW.
    """
    problems: list[str] = []

    mislabelled = [r for r in ledger
                   if str(r.get("held_reason") or "") == "target-identity-mismatch"
                   and isinstance(r.get("target_attempt"), int)
                   and not isinstance(r.get("target_attempt"), bool)
                   and r["target_attempt"] < 1]
    if mislabelled:
        problems.append(
            f"{len(mislabelled)} ledger row(s) carry `target-identity-mismatch` "
            "with their own `target_attempt: 0` — never opened, so nothing "
            "produced the wrong id. That is the pass-ended cascade wearing a "
            "mismatch's word (run 105 wrote 108 of them, and the night read as "
            "108 identity failures); its reason is "
            "`pass-ended-by-identity-stop`")

    # (v5.62) THE REFUSAL WORD, SCORED IN BOTH DIRECTIONS.
    #
    # (a) It may only sit on a row that really was refused — recounted from the
    #     page facts (`_is_refusal`), never from the word. Without this the new
    #     word is a way to launder a wrong-conversation landing out of the
    #     mutation stop, which is the one thing this split must never buy.
    forged = [r for r in ledger
              if str(r.get("held_reason") or "") == "navigation-refused-row-unreachable"
              and not _is_refusal(r)]
    if forged:
        problems.append(
            f"{len(forged)} ledger row(s) carry "
            "`navigation-refused-row-unreachable` without the page facts a "
            "refusal is defined by (`open_method: \"navigate\"`, no produced "
            f"id, `url_has_id: false`, `body_chars` <= {_EMPTY_SHELL_CHARS}). "
            "A landing that produced ANY id opened something; if it was not "
            "the intended conversation that is `target-identity-mismatch`, "
            "with the mutation stop and everything else it carries")
    # (b) AND A REFUSAL MAY NOT END THE PASS. The stop exists for a wrong
    #     conversation being opened; a refusal opened none, so the cascade word
    #     needs a TRUE mismatch behind it. Measured run 111: four refusals, and
    #     111 rows written out `pass-ended-by-identity-stop` behind them — a
    #     whole night's reading lost to a stop nothing had triggered.
    cascade = [r for r in ledger
               if str(r.get("held_reason") or "") == "pass-ended-by-identity-stop"]
    real_stop = any(str(r.get("held_reason") or "") in _PASS_STOPPING_REASONS
                    for r in ledger) or any(
        r.get("target_produced") != r.get("target_intended") and not _is_refusal(r)
        for r in acts
        if "target_intended" in r and "target_produced" in r
        and r.get("event") not in _NON_IDENTITY_EVENTS)
    if cascade and not real_stop:
        problems.append(
            f"{len(cascade)} ledger row(s) carry `pass-ended-by-identity-stop` "
            "while nothing in this run records a cause that ENDS a pass "
            f"({'/'.join(sorted(_PASS_STOPPING_REASONS))}, or an action row "
            "whose produced id differs from the intended one) — so the pass "
            "ended on a REFUSAL, which opened no conversation, moved no pane "
            "and touched nothing. A refusal holds its own thread and the pass "
            "carries on (measured run 111: 4 refusals, 111 rows written out "
            "behind a stop nothing triggered)")

    gated = _declares(ledger, (5, 60)) or _declares(acts, (5, 60))
    if gated:
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

    if problems:
        return _row("open_instrumentation", FAIL,
                    f"{len(ledger)} ledger row(s), {len(acts)} action row(s): "
                    + "; ".join(problems[:4])
                    + " (E30(g)/(h); SKILL.md A MISMATCH STOPS THE LINE)",
                    reexecuted=True)
    if not gated:
        return _row("open_instrumentation", PASS,
                    f"no row of this run claims v5.60, so the per-attempt "
                    "instrumentation and the in-run control are not owed; no "
                    f"mismatch reason sits on a `target_attempt: 0` row "
                    f"({len(ledger)} ledger row(s))",
                    reexecuted=True)
    return _row("open_instrumentation", PASS,
                "every attempt row carries its method, URL, evaluation "
                "duration, page facts, hour, display state and a hold status "
                "read from the status file; the in-run control is on disk; and "
                "no mismatch reason sits on a row that was never attempted",
                reexecuted=True)


# -- Phase 1.5f: the cycling set, recounted ------------------------------------

#: Dispositions a Phase-1.5f row carries. A `held` row is IN the batch and
#: deliberately carries NO stamp (E26 v5.13: an unscreened chip must come back
#: to the front of the queue), so it is drawn-but-unstamped, never both.
_REEVAL_DISPOSITIONS = {"reevaluated", "held"}


def _is_reeval_row(row: dict[str, Any]) -> bool:
    """Is this chip-ledger row a Phase-1.5f re-evaluation?

    ``_cos_chip_ledger_*`` also carries Phase-1.5d RE-LEVEL rows (run 72 wrote
    38, run 74 fifty), which are not draws from the cycling queue and must not
    be counted as one. A 1.5f row is the one that carries a re-eval disposition
    or a ``last_reeval`` stamp field.
    """
    return (str(row.get("disposition") or "") in _REEVAL_DISPOSITIONS
            or "last_reeval" in row or "previous_last_reeval" in row)


def chip_rows(vault, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(cos.run_ops_dir(vault) / f"_cos_chip_ledger_{run_id}.jsonl")


def hold_rows(vault, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(cos.run_ops_dir(vault) / f"_cos_hold_ledger_{run_id}.jsonl")


def _written_before(name: str, run_id: str) -> bool:
    """Was ``name`` written by a run that preceded ``run_id``?

    Run NUMBER first (they ascend, and three runs share 2026-08-09), the date in
    the filename only when one side carries no run number — a ledger a LATER run
    wrote must never contribute a stamp to an earlier run's recount, or
    re-scoring run 102 today would grade it against run 104's work.
    """
    a, b = _RUN_NUMBER_RE.search(name), _RUN_NUMBER_RE.search(run_id)
    if a and b:
        return int(a.group(1)) < int(b.group(1))
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return bool(m) and m.group(1) < run_id[:10]


def prior_reeval_stamps(vault, run_id: str) -> dict[str, str]:
    """The stamp of record per conversation, from THIS VAULT'S OWN ledgers.

    E26(a)/(j): the ordering is computed from the vault's chip ledgers, and a
    conversation's stamp is the LATEST ``last_reeval`` any earlier run wrote for
    it. A conversation with no row here has never been re-evaluated and sorts at
    epoch 0 — ahead of every dated entry.
    """
    ops = cos.run_ops_dir(vault)
    out: dict[str, str] = {}
    for path in sorted(ops.glob("_cos_chip_ledger_*.jsonl")) + \
            sorted(ops.glob("_chip_reeval_*.jsonl")):
        if run_id in path.name or not _written_before(path.name, run_id):
            continue
        for r in _read_jsonl(path):
            if not _is_reeval_row(r):
                continue
            cid = str(r.get("conversation_id") or "")
            stamp = str(r.get("last_reeval") or "")
            if cid and stamp and stamp > out.get(cid, ""):
                out[cid] = stamp
    return out


def _bundle_at_least(bundle: str, want: tuple[int, int]) -> bool:
    """Does the manifest's ``bundle_version`` claim ``want`` or later?

    The same version gate ``_declares_v551`` applies to a ledger row, read off
    the MANIFEST instead — a chip-reeval row carries no ``bundle_version``, and
    the manifest is the artifact that froze which doctrine actually ran.
    """
    m = re.search(r"v?(\d+)\.(\d+)", str(bundle or ""))
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= want


def _denominator_row(drawn_rows: list[dict[str, Any]], recount: int,
                     bundle: str, detail: str) -> dict[str, Any]:
    """E26(j): the run STATES the population it drew from, and the host recounts it.

    A denominator nobody can recompute is how ``33`` survived three runs while
    the real population was 287: it was reported in prose, in a self-eval the
    run wrote about itself, and nothing on the host could disagree with it.
    """
    reported = {r["cycling_population"] for r in drawn_rows
                if isinstance(r.get("cycling_population"), int)
                and not isinstance(r.get("cycling_population"), bool)}
    if not reported:
        if _bundle_at_least(bundle, (5, 54)):
            return _row("chip_reeval_draw", FAIL,
                        f"{detail} — but this run's bundle ({bundle}) carries "
                        "E26(j) and its chip ledger states no "
                        "`cycling_population`. A denominator that cannot be "
                        "recomputed from the vault's own ledgers is a FAIL: "
                        f"the host recounts {recount}, and runs 103 and 104 "
                        "both reported 33",
                        reexecuted=True)
        return _row("chip_reeval_draw", DEGRADED,
                    f"{detail}. The run states no `cycling_population`, so the "
                    "denominator it reported in prose cannot be checked — its "
                    f"bundle ({bundle or 'unknown'}) predates E26(j), and a run "
                    "is not retro-failed against a field its own doctrine never "
                    f"named. The host's own recount is {recount}",
                    reexecuted=True)
    if len(reported) > 1:
        return _row("chip_reeval_draw", FAIL,
                    f"{detail} — but the chip ledger states "
                    f"{len(reported)} different `cycling_population` values "
                    f"({sorted(reported)}); one draw has one denominator",
                    reexecuted=True)
    stated = reported.pop()
    if stated != recount:
        return _row("chip_reeval_draw", FAIL,
                    f"{detail} — but the run states it drew from "
                    f"{stated} conversation(s) and the host recounts "
                    f"{recount} from this run's own hold ledger. A denominator "
                    "that does not survive a recount is E26(j)'s whole point. "
                    "THE DEFINITION, and it is the only one: the population is "
                    "the count of DISTINCT `conversation_id` in this run's own "
                    "`_cos_hold_ledger_<run_id>.jsonl` (union the threads this "
                    "phase drew, which the ledger already contains). NO ROW IS "
                    "FILTERED OUT — not by `held_category`, and above all not "
                    "by `held_reason`: a thread held because tonight's browser "
                    "broke is a held thread. Measured, run 109 "
                    "(2026-08-10): 301 hold rows, of which 51 carried "
                    "`safety-hold: body-pass-visibility-control-unavailable` "
                    "and were dropped from the count, giving the 250 the run "
                    "reported against the host's 301",
                    reexecuted=True)
    source = ""
    for r in drawn_rows:
        source = str(r.get("cycling_population_source") or "").strip()
        if source:
            break
    if not source:
        if _bundle_at_least(bundle, (5, 54)):
            return _row("chip_reeval_draw", FAIL,
                        f"{detail}, and the stated `cycling_population` "
                        f"{stated} survives the recount — but no row names "
                        "`cycling_population_source`, so HOW the set was "
                        "enumerated is unrecorded, which is the half of E26(j) "
                        "that catches a right number derived the wrong way",
                        reexecuted=True)
        return _row("chip_reeval_draw", DEGRADED,
                    f"{detail}, and the stated `cycling_population` {stated} "
                    "survives the recount, but the run names no "
                    "`cycling_population_source`",
                    reexecuted=True)
    return _row("chip_reeval_draw", PASS,
                f"{detail}. Stated `cycling_population` {stated} survives the "
                f"host recount, derived from {source!r}",
                reexecuted=True)


def check_chip_reeval_draw(run_id: str, batch: list[dict[str, Any]],
                           held: list[dict[str, Any]], prior: dict[str, str],
                           *, bundle: str = "") -> dict[str, Any]:
    """(c7) The chip re-evaluation batch IS the head of the cycling queue.

    WHY THIS EXISTS (measured, runs 100/103/104, 2026-08-08..09). E26(a) has
    required an oldest-``last_reeval``-first draw since v5.5 and never once got
    one. Run 104 re-evaluated the IDENTICAL twenty conversations run 102 had
    evaluated nine hours earlier, while 234 held-and-chipped conversations had
    NEVER been stamped at all and so, under E26(a)'s epoch-0 rule, owned every
    slot in that batch. Same shape on runs 100 and 103. Three occurrences.

    THE DEFECT IS THE POPULATION, NOT THE COMPARATOR — which is why this
    recounts the SET and not just the order. Both runs 103 and 104 reported the
    denominator ``33``, and 33 is exactly the number of DISTINCT CONVERSATIONS
    THE PHASE HAD ALREADY EVALUATED (``|run100 ∪ run102|`` = ``|run102 ∪
    run103|`` = 33, the same set both times). Enumerating candidates from the
    ``last_reeval`` STAMPS is self-referential: a never-stamped thread has no
    stamp row to find, so it can never enter the list, rule 1's epoch-0 clause
    becomes unreachable, and the queue ping-pongs forever among the threads it
    has already drawn. Six runs, 120 stamp events, 53 distinct conversations,
    and the backlog RTG-01 exists to drain untouched.

    So the population is the run's OWN held-and-chipped census (tonight's
    ``_cos_hold_ledger_``), never the stamp file, and this check FAILS a batch
    that is not that population's head. The run's prose is not an input.

    WHAT IT DOES NOT CLAIM. Which of the never-stamped threads a cold start
    picked is NOT recountable here — the hold ledger carries no ``received``, so
    E26(a)'s oldest-``received``-then-``conversation_id`` tiebreak has no source
    on this surface. The set-level invariant is what is decidable and it is what
    every measured failure violates: while never-stamped threads remain, a
    stamped one may not be drawn.
    """
    drawn_rows = [r for r in batch if _is_reeval_row(r)]
    drawn = list(dict.fromkeys(str(r.get("conversation_id") or "")
                               for r in drawn_rows if r.get("conversation_id")))
    if not drawn:
        return _row("chip_reeval_draw", PASS,
                    f"no Phase-1.5f row in this run's chip ledger "
                    f"({len(batch)} row(s)) — no draw to recount (a phase that "
                    "owed a batch and wrote none is E26's run-obligation, "
                    "scored on the self-eval, not here)",
                    reexecuted=True)

    held_ids = {str(r.get("conversation_id") or "") for r in held}
    held_ids.discard("")
    missing = [c for c in drawn if c not in held_ids]
    if missing:
        # ponytail: the hold ledger is the only chipped census a run writes.
        # When it does not even contain the batch it is not one, and a
        # population recounted from it would be fiction. The OUTCOME CONTRACT
        # is what catches a hold ledger that under-reports (archive : hold :
        # drafted must equal the enumeration), so this degrades rather than
        # inventing a second census.
        return _row("chip_reeval_draw", DEGRADED,
                    f"this run's hold ledger ({len(held_ids)} conversation(s)) "
                    f"does not contain {len(missing)} of the {len(drawn)} "
                    "thread(s) the chip ledger says were re-evaluated, so it is "
                    "not a census of the chipped set and the cycling "
                    "POPULATION cannot be recounted from it — the draw is taken "
                    "on the run's word",
                    reexecuted=False)

    population = held_ids | set(drawn)
    never = [c for c in population if c not in prior]
    stamped_drawn = [c for c in drawn if c in prior]

    if len(never) >= len(drawn):
        if stamped_drawn:
            oldest = min(prior[c] for c in stamped_drawn)
            return _row("chip_reeval_draw", FAIL,
                        f"{len(stamped_drawn)} of the {len(drawn)} thread(s) "
                        f"drawn had ALREADY been re-evaluated (oldest such "
                        f"stamp {oldest}) while {len(never)} of the "
                        f"{len(population)} held-and-chipped conversation(s) "
                        "have NEVER been stamped — a never-reeval'd thread "
                        "sorts at epoch 0 and owns every slot in this batch "
                        "(E26(a)/(j)). This is the run-104 shape: the "
                        "population was enumerated from the `last_reeval` "
                        "stamps, which only the already-drawn threads have, so "
                        "the queue re-draws its own head and the backlog never "
                        "cycles",
                        reexecuted=True)
        # Say which it is, rather than asserting the ceiling from memory: run
        # 109's hold ledger carries `received` on all 301 rows, and its report
        # still claimed "the hold ledger lacks the received evidence" because
        # THIS STRING said so unconditionally.
        tiebreak = ("(Which unstamped threads were picked is not recountable: "
                    "this run's hold ledger carries no `received` for E26(a)'s "
                    "cold-start tiebreak)"
                    if not any(r.get("received") for r in held) else
                    "(This run's hold ledger DOES carry `received`, so E26(a)'s "
                    "cold-start tiebreak is recountable from it — no check "
                    "scores it yet, and no run may claim the evidence is "
                    "missing)")
        return _denominator_row(
            drawn_rows, len(population), bundle,
            f"{len(drawn)} thread(s) drawn, every one never previously "
            f"re-evaluated, from a population of {len(population)} "
            f"held-and-chipped conversation(s) of which {len(never)} are "
            f"unstamped — the epoch-0 head. {tiebreak}")

    left_behind = [c for c in never if c not in drawn]
    if left_behind:
        return _row("chip_reeval_draw", FAIL,
                    f"{len(left_behind)} never-re-evaluated conversation(s) "
                    f"were left behind by a batch of {len(drawn)} that had room "
                    f"for them — every one of the {len(never)} unstamped "
                    f"thread(s) in this run's population of {len(population)} "
                    "sorts at epoch 0, ahead of any dated stamp (E26(a)/(j))",
                    reexecuted=True)

    # Fewer unstamped threads than slots: the remainder goes to the OLDEST
    # stamps. Ties are inclusive — a whole cohort shares one stamp, and picking
    # any member of it is a correct draw.
    needed = len(drawn) - len(never)
    oldest_first = sorted(prior[c] for c in population if c in prior)
    cutoff = oldest_first[needed - 1]
    late = [c for c in stamped_drawn if prior[c] > cutoff]
    if late:
        return _row("chip_reeval_draw", FAIL,
                    f"{len(late)} thread(s) in this batch carry a "
                    f"`last_reeval` NEWER than the {needed}th-oldest stamp in "
                    f"the population ({cutoff}) — the batch is not the head of "
                    "the queue, and the threads it skipped stay skipped "
                    "(E26(a)/(j))",
                    reexecuted=True)

    return _denominator_row(
        drawn_rows, len(population), bundle,
        f"{len(drawn)} thread(s) drawn from a population of "
        f"{len(population)} held-and-chipped conversation(s): all "
        f"{len(never)} never-stamped thread(s) first, the remaining "
        f"{needed} slot(s) filled from stamps no newer than {cutoff}")


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


def _capture_was_live_by(vault, run_id: str) -> str | None:
    """The oldest corpus this host still holds, when it is no NEWER than
    ``run_id`` — or ``None``.

    One artifact, two exclusions. A corpus dated on or before this run's date
    proves capture was ALREADY RUNNING on this host by then (so the run neither
    predates capture nor ran a bundle that cannot write one), and it proves
    retention has not reached that date (whole files expire oldest-first by the
    date in the name, so the witness would have been deleted before this run's
    corpus). With no corpus on disk at all there is no witness and nothing is
    concluded — the host genuinely cannot tell, which is the degraded case.
    """
    runs = cos_corpus.list_runs(vault)                  # oldest first, [] on error
    oldest = runs[0] if runs else ""
    return oldest if oldest and oldest[:10] <= run_id[:10] else None


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
    try:
        exists = cos_corpus.corpus_path(vault, run_id).exists()
    except cos_corpus.CorpusHostOnly as exc:
        return _row("corpus_join", INCONCLUSIVE, str(exc), reexecuted=False)
    except config.HostPathUnsafe as exc:
        return _row("corpus_join", INCONCLUSIVE,
                    f"the capture corpus location could not be proven safe, "
                    f"so the host cannot tell whether one exists for this "
                    f"run: {exc}", reexecuted=False)
    if not exists:
        opened = sum(1 for r in rows if r.get("body_opened"))
        witness = _capture_was_live_by(vault, run_id) if opened else None
        if witness:
            return _row("corpus_join", FAIL,
                        f"{opened} ledger row(s) claim `body_opened: true` and "
                        f"this run wrote NO capture corpus at all. None of the "
                        f"three innocent causes applies: this host was already "
                        f"capturing by this run's date — it still holds the "
                        f"corpus for {witness} — so the run neither predates "
                        f"capture nor ran a bundle that does not write one, and "
                        f"retention has not reached this date either, or "
                        f"{witness} would have gone first. A body claimed open "
                        f"with nothing captured is a read asserted by the "
                        f"ledger and by nothing else — the run-64 shape",
                        reexecuted=True)
        return _row("corpus_join", DEGRADED,
                    "not applicable — no capture corpus on disk for this run. "
                    "It either predates corpus capture (s06, 2026-08-02), ran "
                    "a bundle that does not write one, or is older than the "
                    "capture-corpus retention window ($BRAIN_COS_CORPUS_DAYS, "
                    "default 30 days) and the nightly deleted it. So the "
                    "ledger-corpus join could not be re-executed; this is "
                    "never scored as a failure on that account alone",
                    reexecuted=False)

    corpus_rows = cos_corpus.read_corpus(vault, run_id)
    captured: set[str] = set()
    bodied: set[str] = set()
    for r in corpus_rows:
        cid = str(r.get("conversation_id") or "").strip()
        if not cid:
            continue
        captured.add(cid)
        if str(r.get("text") or "").strip():
            bodied.add(cid)

    in_scope = [r for r in rows
               if str(r.get("disposition") or "") != _MARKER_DISPOSITION]
    missing = [str(r.get("conversation_id") or "").strip() or "<no conversation_id>"
              for r in in_scope
              if str(r.get("conversation_id") or "").strip() not in captured]
    if missing:
        return _row("corpus_join", FAIL,
                    f"{len(missing)} of {len(in_scope)} in-scope ledger row(s) "
                    "carry a verdict for a thread this run's own capture "
                    "corpus never recorded: "
                    + ", ".join(m[:32] for m in missing[:5])
                    + (" …" if len(missing) > 5 else "")
                    + ". The ledger and the corpus are one record joined on "
                      "`conversation_id` (SKILL.md rule 8); a row on one side "
                      "with nothing on the other is not a coherent pair",
                    reexecuted=True)

    opened_no_text = [str(r.get("conversation_id") or "").strip() for r in rows
                      if r.get("body_opened")
                      and str(r.get("conversation_id") or "").strip() not in bodied]
    if opened_no_text:
        return _row("corpus_join", FAIL,
                    f"{len(opened_no_text)} ledger row(s) claim `body_opened: "
                    "true` for a thread whose corpus row carries no text: "
                    + ", ".join(m[:32] for m in opened_no_text[:5])
                    + (" …" if len(opened_no_text) > 5 else "")
                    + ". A body claimed open is a body the corpus should hold "
                      "the text of",
                    reexecuted=True)

    return _row("corpus_join", PASS,
                f"{len(in_scope)} in-scope ledger row(s) all resolve to a "
                f"thread the corpus captured, and every `body_opened: true` "
                f"row's thread carries corpus text ({len(bodied)} of "
                f"{len(corpus_rows)} corpus row(s) bodied)",
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


def check_candidate_stamps(run_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """E16's stamp clause, checked where v5.39 put it: the LEDGER.

    The host joins a claimed drop back to its producing run by proposal id AND
    full content digest, so a candidate row with no id, a duplicate id, a
    malformed digest or an invented category is a candidate the host either
    cannot attribute or must refuse."""
    candidates = [r for r in rows if r.get("disposition") == "candidate"]
    if not candidates:
        return _row("candidate_stamps", PASS,
                    "no candidate rows in this run's ledger — nothing to stamp",
                    reexecuted=True)
    problems: list[str] = []
    seen: dict[str, int] = {}
    no_digest = 0
    for r in candidates:
        pid = str(r.get("proposal_id") or r.get("id") or "").strip()
        if not pid:
            problems.append("a candidate row carries no proposal_id — the host "
                            "cannot attribute the drop it names")
            continue
        seen[pid] = seen.get(pid, 0) + 1
        digest = str(r.get("content_sha256") or r.get("sha256") or "").strip().lower()
        if not digest:
            no_digest += 1
        elif not _SHA256_RE.match(digest):
            problems.append(f"{pid}: content_sha256 {digest[:16]!r} is not a "
                            "sha256 — a hand-computed hash proves nothing about "
                            "the staged bytes")
        cat = str(r.get("category") or "").strip().lower()
        if cat in _PLACEHOLDER_CATEGORIES:
            problems.append(f"{pid}: category {cat!r} is an invented placeholder "
                            "— the value is the owner's real taxonomy id, or the "
                            "key is absent")
    problems.extend(f"{pid}: {n} candidate rows claim the same proposal id"
                    for pid, n in sorted(seen.items()) if n > 1)
    if problems:
        return _row("candidate_stamps", FAIL,
                    f"{len(candidates)} candidate row(s): " + "; ".join(problems[:8])
                    + (" …" if len(problems) > 8 else ""),
                    reexecuted=True)
    if no_digest:
        return _row("candidate_stamps", DEGRADED,
                    f"{no_digest} of {len(candidates)} candidate row(s) carry no "
                    "content digest, so the host cannot join them to the bytes "
                    "that were dropped — those candidates quarantine at claim "
                    "time rather than bind",
                    reexecuted=True)
    return _row("candidate_stamps", PASS,
                f"{len(candidates)} candidate row(s), each with a unique "
                "proposal id, a sha256 content digest and a non-placeholder "
                "category",
                reexecuted=True)


def degrade_evidence(vault, run_id: str, block: dict[str, Any] | None,
                     row: dict[str, Any] | None, rows: list[dict[str, Any]],
                     recon: Any) -> dict[str, Any]:
    """Is this run a TOTAL, INTERNALLY CONSISTENT degrade — or a run pretending?

    Every signal here except the mail-leg enumeration comes from a different
    artifact, and ``host_received`` comes from the HOST's own records. A degrade
    that is real shows up in all of them at once; a marker copied onto a run
    that did substantive work contradicts the rest."""
    ops = cos.run_ops_dir(vault)
    enumerated = (recon.mail_leg_enumerated(ops, run_id)[0]
                  if recon is not None else
                  len((block or {}).get("enumerated") or []))
    marker = [r for r in rows if r.get("disposition") == _MARKER_DISPOSITION]
    counted = ledger_counts(rows)
    reported = {k: int((row or {}).get(k) or 0) for k in counted}
    host_received = host_received_candidates(vault, run_id)

    total = (enumerated == 0 and host_received == 0
             and all(v == 0 for v in counted.values())
             and all(v == 0 for v in reported.values()))

    inconsistencies: list[str] = []
    if marker:
        if counted["ingestion_candidates"] or counted["ingestion_in_scope"]:
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside "
                f"{counted['ingestion_in_scope']} in-scope row(s) and "
                f"{counted['ingestion_candidates']} staged candidate(s) in the "
                "SAME ledger")
        if any(v for v in reported.values()):
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside a metrics "
                f"row reporting {reported} ingestion work")
        if enumerated:
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside a mail leg "
                f"that enumerated {enumerated} thread(s)")
        if host_received:
            inconsistencies.append(
                f"a `{_MARKER_DISPOSITION}` degrade marker sits beside "
                f"{host_received} candidate(s) the HOST actually received from "
                "this run")
    return {"total": total, "inconsistencies": inconsistencies,
            "enumerated": enumerated, "host_received": host_received,
            "ledger_counts": counted, "reported_counts": reported,
            "marker_rows": len(marker)}


def check_degrade_consistency(evidence: dict[str, Any]) -> dict[str, Any]:
    if evidence["inconsistencies"]:
        return _row("degrade_consistency", FAIL,
                    "the run claims a degrade its own artifacts contradict — "
                    + "; ".join(evidence["inconsistencies"]),
                    reexecuted=True)
    if evidence["marker_rows"] and evidence["total"]:
        return _row("degrade_consistency", PASS,
                    "the degrade is TOTAL and consistent across every artifact "
                    "(0 enumerated, 0 ledgered, 0 reported, 0 candidates "
                    "received by the host)",
                    reexecuted=True)
    return _row("degrade_consistency", PASS,
                "no degrade marker to corroborate", reexecuted=True)


def check_contract(vault, run_id: str, contract: Any, recon: Any,
                   evidence: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
    """(d) The contract verdict is present, checker-produced, and RE-DERIVABLE.

    This is the one control that fully re-executes: the same deterministic
    checker, over the same raw PRE/POST snapshots and ledgers the run handed it,
    compared against the block the run recorded. A block that does not survive
    re-execution was not produced by the checker over those artifacts, whatever
    `verdict_source` it carries."""
    ops = cos.run_ops_dir(vault)
    block_path = ops / f"cos_contract_block_{run_id}.json"
    block = _load_json(block_path)
    if not isinstance(block, dict):
        return _row("contract", FAIL,
                    f"no readable OUTCOME CONTRACT block at {block_path.name} — "
                    "the run recorded no verdict for the host to check",
                    reexecuted=False), None
    if str(block.get("verdict")) not in ("PASS", "FAILED"):
        return _row("contract", FAIL,
                    f"the contract block carries verdict {block.get('verdict')!r}, "
                    "which the checker never emits",
                    reexecuted=False), block
    source = str(block.get("verdict_source") or "")
    if not source.startswith("tools/cos_contract.py@"):
        return _row("contract", FAIL,
                    f"the contract block's verdict_source is {source!r} — a "
                    "hand-composed verdict, not one the checker produced",
                    reexecuted=False), block
    if contract is None:
        return _row("contract", INCONCLUSIVE,
                    "the OUTCOME CONTRACT checker is not available host-side, so "
                    f"the recorded {block.get('verdict')} could not be "
                    "re-derived from the raw artifacts",
                    reexecuted=False), block

    pre = _load_json(ops / f"cos_contract_pre_{run_id}.json")
    post = _load_json(ops / f"cos_contract_post_{run_id}.json")
    if not isinstance(pre, dict) or not isinstance(post, dict):
        return _row("contract", DEGRADED,
                    "the run's raw PRE/POST snapshots are not both readable, so "
                    f"the recorded {block.get('verdict')} cannot be re-executed "
                    "— it is taken on the run's word",
                    reexecuted=False), block
    profile = (pre.get("run_profile") or block.get("run_profile") or "full")
    try:
        recomputed = contract.evaluate(pre, post, ops, _run_number(run_id), profile)
    except Exception as exc:                               # noqa: BLE001
        return _row("contract", FAIL,
                    f"the run's own contract inputs do not survive the checker "
                    f"({type(exc).__name__}: {exc}) — a block claiming "
                    f"{block.get('verdict')} over inputs the checker refuses is "
                    "not a verdict",
                    reexecuted=True), block

    if (recomputed["verdict"] != block.get("verdict")
            or sorted(recomputed["verdict_reasons"])
            != sorted(block.get("verdict_reasons") or [])):
        return _row("contract", FAIL,
                    f"re-executing the checker over this run's raw artifacts "
                    f"yields {recomputed['verdict']} "
                    f"({', '.join(recomputed['verdict_reasons']) or 'no clauses'}) "
                    f"but the recorded block says {block.get('verdict')} "
                    f"({', '.join(block.get('verdict_reasons') or []) or 'no clauses'})",
                    reexecuted=True), block

    if recomputed["verdict"] == "PASS":
        return _row("contract", PASS,
                    "the recorded PASS is reproduced exactly by re-executing the "
                    "checker over the run's raw PRE/POST snapshots and ledgers",
                    reexecuted=True), block
    clauses = ", ".join(recomputed["verdict_reasons"]) or "no clauses"
    if evidence["total"]:
        return _row("contract", DEGRADED,
                    f"the contract honestly FAILED ({clauses}) and the degrade is "
                    "TOTAL and consistent across every artifact — correctly "
                    "reported degradation, not a validator failure",
                    reexecuted=True), block
    return _row("contract", FAIL,
                f"the contract FAILED ({clauses}) on a run that did substantive "
                f"work — {evidence['enumerated']} thread(s) enumerated, "
                f"{evidence['ledger_counts']['ingestion_candidates']} candidate(s) "
                f"staged, {evidence['host_received']} received by the host. A "
                "failed contract is exempt only when the degrade is total",
                reexecuted=True), block


# -- the verdict ---------------------------------------------------------------

def _verdict_from(checks: list[dict[str, Any]]) -> tuple[str, str]:
    states = [c["status"] for c in checks]
    if FAIL in states:
        bad = [c for c in checks if c["status"] == FAIL]
        return cos.RUN_INVALID, "; ".join(
            f"{c['check']}: {c['detail']}" for c in bad)
    if INCONCLUSIVE in states:
        bad = [c for c in checks if c["status"] == INCONCLUSIVE]
        return cos.RUN_INCONCLUSIVE, "; ".join(
            f"{c['check']}: {c['detail']}" for c in bad)
    if DEGRADED in states:
        bad = [c for c in checks if c["status"] == DEGRADED]
        return cos.RUN_VALID_DEGRADED, "; ".join(
            f"{c['check']}: {c['detail']}" for c in bad)
    return cos.RUN_VALID, "every control re-executed clean"


def verify_run(vault, run_id: str, *, now: _dt.datetime | None = None,
               quiesce_seconds: int | None = None) -> dict[str, Any]:
    """Score ONE run against its own artifacts. Never writes anything."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    quiesce = _quiesce_seconds(quiesce_seconds)
    out: dict[str, Any] = {"run_id": run_id, "verdict": None, "state": "pending",
                           "reason": "", "checks": [], "inputs_digest": None}

    manifest = cos.run_manifest(vault, run_id)
    if manifest is None:
        # NAME WHICH OF THE TWO IT IS. A malformed id (`106` for
        # `2026-08-09-run106`) resolves to no manifest path at all, and
        # reporting that as "no host run manifest" sends the reader looking for
        # a missing file that is sitting right there — run 106's own report
        # carries exactly that line while its manifest existed the whole time.
        try:
            cos.checked_run_id(run_id)
            why = ("no host run manifest for this run — the host never "
                   "recorded what was supposed to run, so it cannot "
                   "check whether the run did it")
            short = "no host run manifest"
        except ValueError as exc:
            why = (f"{exc}. That is a MALFORMED run id, NOT a missing "
                   "manifest: pass the full host-assigned id, which the host "
                   "publishes at .brain/cos/shared/current-run.json")
            short = "malformed run id (not a missing manifest)"
        out.update(state="scored", verdict=cos.RUN_INCONCLUSIVE, reason=why,
                   checks=[_row("completion", INCONCLUSIVE, short,
                                reexecuted=True)])
        return out
    out["inputs_digest"] = inputs_digest(vault, run_id, manifest)

    ops = cos.run_ops_dir(vault)
    if not ops.is_dir():
        out.update(state="scored", verdict=cos.RUN_INCONCLUSIVE,
                   reason=(f"the run ops dir {ops} does not exist — the "
                           "validator could not run, which is not the same as "
                           "the run passing"),
                   checks=[_row("completion", INCONCLUSIVE,
                                f"no run ops dir at {ops}", reexecuted=True)])
        return out

    done = completion(vault, run_id, manifest, now=now, quiesce=quiesce)
    if not done["complete"]:
        out["reason"] = done["reason"]
        out["checks"] = [_row("completion", INCONCLUSIVE, done["reason"],
                              reexecuted=True)]
        return out                                   # PENDING: no verdict at all

    contract_mod, recon, tools_reason = checkers()
    checks = [_row("completion", PASS, done["reason"], reexecuted=True)]
    if contract_mod is None:
        checks.append(_row("checkers", INCONCLUSIVE, tools_reason,
                           reexecuted=False))

    rows = ledger_rows(vault, run_id)
    row = metrics_row(vault, run_id)
    block = _load_json(ops / f"cos_contract_block_{run_id}.json")
    evidence = degrade_evidence(vault, run_id,
                                block if isinstance(block, dict) else None,
                                row, rows, recon)

    checks.append(check_self_eval(vault, run_id, manifest))
    checks.append(check_repairs(vault, run_id, manifest))
    checks.append(check_metrics_row(vault, run_id, manifest, rows, recon))
    checks.append(check_ledger_vocabulary(run_id, rows))
    checks.append(check_category_stamp(vault, run_id, rows))
    checks.append(check_ingestion_ledger(vault, run_id, rows, recon))
    checks.append(check_body_pass(run_id, rows))
    checks.append(check_body_order(run_id, rows))
    checks.append(check_body_open_count(run_id, rows, row))
    acts = action_rows(vault, run_id)
    # Corroborated ONCE and handed to both controls: an empty action ledger is
    # "nothing acted" only if the run's own counters agree (run 106 did not).
    unledgered = unledgered_mutations(vault, run_id, acts)
    checks.append(check_unread_touch(run_id, acts, unledgered))
    checks.append(check_target_identity(run_id, acts, unledgered))
    checks.append(check_open_instrumentation(vault, run_id, rows, acts))
    checks.append(check_chip_reeval_draw(
        run_id, chip_rows(vault, run_id), hold_rows(vault, run_id),
        prior_reeval_stamps(vault, run_id),
        bundle=str(manifest.get("bundle_version") or "")))
    checks.append(check_corpus_join(vault, run_id, rows))
    checks.append(check_candidate_stamps(run_id, rows))
    checks.append(check_artifact_naming(vault, run_id))
    checks.append(check_degrade_consistency(evidence))
    contract_row, _ = check_contract(vault, run_id, contract_mod, recon, evidence)
    checks.append(contract_row)

    verdict, reason = _verdict_from(checks)
    out.update(state="scored", verdict=verdict, reason=reason, checks=checks,
               degrade=evidence)
    return out


def known_run_ids(vault) -> list[str]:
    """Every run the host has a manifest for, newest run number first."""
    d = cos.runs_dir(vault)
    if not d.is_dir():
        return []
    ids = [p.stem for p in d.glob("*.json")
           if not p.name.endswith(".validity.json") and cos.RUN_ID_RE.match(p.stem)]
    return sorted(ids, key=lambda r: (int(_run_number(r)), r), reverse=True)


def verify_pending_runs(vault, *, now: _dt.datetime | None = None,
                        window: int = DEFAULT_RUN_WINDOW,
                        quiesce_seconds: int | None = None) -> dict[str, Any]:
    """Score every recent run that has not been scored over ITS CURRENT inputs.

    Idempotent: a run whose recorded verdict was computed over the same input
    digest is skipped, so the hourly fold does no work on a settled night. A
    changed manifest or a changed/substituted artifact moves the digest and
    forces a re-score — a cached verdict is never allowed to outlive the
    artifacts it was computed over.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    report: dict[str, Any] = {"scored": [], "pending": [], "unchanged": [],
                              "invalid": [], "inconclusive": [], "errors": []}
    for run_id in known_run_ids(vault)[:max(0, int(window))]:
        try:
            res = verify_run(vault, run_id, now=now,
                             quiesce_seconds=quiesce_seconds)
        except Exception as exc:                           # noqa: BLE001
            report["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")
            continue
        if res["verdict"] is None:
            report["pending"].append({"run_id": run_id, "reason": res["reason"]})
            continue
        prior = cos.run_validity(vault, run_id)
        if (prior.get("recorded")
                and (prior.get("detail") or {}).get("inputs_digest")
                == res["inputs_digest"]
                and prior.get("verdict") == res["verdict"]):
            report["unchanged"].append(run_id)
        else:
            cos.record_run_validity(
                vault, run_id, res["verdict"], reason=res["reason"],
                detail={"inputs_digest": res["inputs_digest"],
                        "checks": res["checks"]},
                ts=cos._ts(now))
            report["scored"].append({"run_id": run_id, "verdict": res["verdict"],
                                     "reason": res["reason"]})
        if res["verdict"] == cos.RUN_INVALID:
            report["invalid"].append(run_id)
        elif res["verdict"] == cos.RUN_INCONCLUSIVE:
            report["inconclusive"].append(run_id)
    # Cumulative counters on the same surface as `unstamped_batched`, bumped
    # only on a TRANSITION (a newly-recorded verdict) — an hourly re-count of a
    # settled failure would bury the rate of new ones.
    fresh = [s for s in report["scored"]
             if s["verdict"] not in cos.CLAIMABLE_VERDICTS]
    if fresh:
        cos._bump_route_stats(
            vault, now=now,
            invalid_runs=sum(1 for s in fresh if s["verdict"] == cos.RUN_INVALID),
            inconclusive_runs=sum(1 for s in fresh
                                  if s["verdict"] == cos.RUN_INCONCLUSIVE))
    return report


def recent_verdicts(vault, *, window: int = 5) -> list[dict[str, Any]]:
    """The newest runs' recorded verdicts — what ``brain status`` reports."""
    return [dict(cos.run_validity(vault, rid), run_id=rid)
            for rid in known_run_ids(vault)[:max(0, int(window))]]


def stalled_runs(vault, *, days: int | None = None,
                 now: _dt.datetime | None = None,
                 hours: float | None = None) -> list[dict[str, Any]]:
    """Runs that WORKED and never completed — PENDING with nothing coming.

    Deliberately narrow, so it stays loud instead of becoming background noise:

    * a run with a recorded verdict is ``alert``'s business, not this one;
    * a manifest with NO artifacts naming it is an ABANDONED STAMP — the host
      re-ran ``cos-run-begin`` before the run started, which is ordinary
      (2026-08-09 stamped run107 at 18:20Z and nothing was ever written under
      that name; run 108 launched three hours later) and is not a stalled run.
      Run 106 is NOT one of these and this docstring said it was: it carries
      20 artifacts and is a genuine PENDING, held back only by the 6-hour
      idle floor below;
    * a run still writing, or complete-but-not-yet-scored, is simply in flight.

    What is left is the failure that has now happened twice: artifacts on disk,
    a manifest-declared name never written, and no verdict ever recorded.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    limit = float(STALLED_PENDING_HOURS if hours is None else hours) * 3600.0
    oldest = (now.date()
              - _dt.timedelta(days=max(0, int(STALLED_LOOKBACK_DAYS
                                              if days is None else days))))
    out: list[dict[str, Any]] = []
    for run_id in known_run_ids(vault):
        if run_id[:10] < oldest.isoformat():
            continue
        if cos.run_validity(vault, run_id).get("recorded"):
            continue
        manifest = cos.run_manifest(vault, run_id)
        if manifest is None:
            continue
        files = run_artifacts(vault, run_id)
        newest = None
        for p in files:
            try:
                newest = max(newest or 0.0, p.stat().st_mtime)
            except OSError:                                # pragma: no cover
                continue
        if newest is None:
            continue                       # abandoned stamp, not a stalled run
        done = completion(vault, run_id, manifest, now=now, quiesce=0)
        idle = now.timestamp() - newest
        if done["complete"] or idle < limit:
            continue
        out.append({"run_id": run_id, "idle_hours": round(idle / 3600.0, 1),
                    "artifacts": len(files), "missing": done["missing"]})
    return out


def alert(vault, *, window: int = 5) -> dict[str, Any]:
    """The loud surface: which recent runs are NOT claimable, and why.

    Same shape and same loudness as ``unstamped_batched`` — a run scored
    INVALID (or INCONCLUSIVE, which is not a softer state) that only showed up
    in a log would be exactly the silent instrument this validator replaces."""
    bad = [v for v in recent_verdicts(vault, window=window)
           if v.get("recorded") and v.get("verdict") not in cos.CLAIMABLE_VERDICTS]
    out: dict[str, Any] = {"runs_not_claimable": [
        {"run_id": v["run_id"], "verdict": v["verdict"],
         "reason": str(v.get("reason") or "")[:400]} for v in bad]}
    if bad:
        names = ", ".join(f"{v['run_id']} {v['verdict']}" for v in bad)
        out["run_validity_text"] = (
            f"{len(bad)} recent COS run(s) failed host validation ({names}) — "
            "their candidates are quarantined, never claimed; see "
            "`_cos_nightly_<run>.md` and the recorded reason in "
            f"{cos.runs_dir(vault)}/<run>.validity.json")
    # NOT `window`: see STALLED_LOOKBACK_DAYS — the verdict window is 5 runs and
    # this deployment fires six in a day, so a count-based scan here could never
    # fire at all.
    stalled = stalled_runs(vault)
    if stalled:
        out["stalled_runs"] = stalled
        names = ", ".join(f"{s['run_id']} (idle {s['idle_hours']}h, "
                          f"{s['artifacts']} artifact(s), missing "
                          f"{', '.join(s['missing'])})" for s in stalled)
        out["stalled_text"] = (
            f"{len(stalled)} COS run(s) did a night's work and never became "
            f"COMPLETE, so NOT ONE host check ever executed on them ({names}) "
            "— the run wrote an artifact under a name the host did not "
            "declare. The manifest's `expected_artifacts` is the list of names "
            "it owes (MAN-01); rename the artifact to the declared name and "
            "the next broker fold scores the night.")
    return out


def hot_entry(scored: list[dict[str, Any]], today: Any) -> str:
    """hot.md LOG entry for newly non-claimable runs (§9: a log, not a queue)."""
    lines = [f"## {today} — COS run(s) failed host validation"]
    lines.append(
        "- **Context:** the host validator scored these runs against their own "
        "artifacts and could not certify them. Their candidates are held in "
        "claim quarantine and are never bound, signed, or used as category "
        "evidence.")
    for s in scored[:5]:
        lines.append(f"  - `{s['run_id']}` — **{s['verdict']}**: "
                     f"{str(s.get('reason') or '')[:300]}")
    if len(scored) > 5:
        lines.append(f"  - … {len(scored) - 5} more")
    lines.append(
        "- **No owner action needed:** re-extract the content on a run that "
        "passes validation. Re-stamping the quarantined copies would launder "
        "the output of an uncontrolled run into the signed pipeline.")
    return "\n".join(lines) + "\n"


__all__ = [
    "PASS", "DEGRADED", "FAIL", "INCONCLUSIVE",
    "alert", "checkers", "completion", "expected_check_count", "hot_entry",
    "inputs_digest", "known_run_ids", "ledger_counts", "recent_verdicts",
    "run_artifacts", "stalled_runs", "verify_pending_runs", "verify_run",
]
