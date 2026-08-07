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

from . import config, cos, cos_corpus

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

#: E-check DEFINITIONS in a chief-of-staff SKILL.md (`- **E16** · …`).
_SKILL_ECHECK_RE = re.compile(r"^- \*\*E(\d{1,2})\*\*\s*·", re.MULTILINE)
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
    """The three Phase-1.6 counters, RECOUNTED from the ledger itself."""
    dispositions = [str(r.get("disposition") or "") for r in rows]
    return {
        "ingestion_in_scope": sum(1 for d in dispositions if d != _MARKER_DISPOSITION),
        "ingestion_candidates": sum(1 for d in dispositions if d == "candidate"),
        "ingestion_held": sum(1 for d in dispositions if d in _HELD_DISPOSITIONS),
    }


def _run_number(run_id: str) -> str:
    m = _RUN_NUMBER_RE.search(run_id)
    return m.group(1) if m else run_id


def metrics_row(vault, run_id: str) -> dict[str, Any] | None:
    """This run's row in ``_cos_metrics.jsonl`` (the row of record).

    Falls back to the per-run ``_cos_metrics_row_<run>.json`` side file only to
    NAME the shortfall precisely — a side file is the run's draft; the appended
    row is what every counter and every reconciliation reads."""
    ops = cos.run_ops_dir(vault)
    want_run = _run_number(run_id)
    date = run_id[:10]
    for row in _read_jsonl(ops / "_cos_metrics.jsonl"):
        if row.get("date") == date and _run_number(str(row.get("run"))) == want_run:
            return row
    return None


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
                f"{expected} derived from the run manifest's skill digest",
                reexecuted=False)


def expected_check_count(manifest: dict[str, Any]) -> tuple[int | None, str]:
    """How many E-checks the bundle THAT RAN defines — or why we cannot know.

    Digest-verified: the manifest names a path AND the sha256 of the bytes that
    executed. If the file has since changed (it always has, once a skill ships
    a new version) the count is UNDERIVABLE, and saying so is the honest
    answer — re-deriving it from today's file would score the run against a
    bundle it never executed."""
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
    if recon is not None:
        try:
            recon._require_ingestion_fields(row)
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
                "from the run's ledger",
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
    """
    substance = [r for r in rows
                 if str(r.get("held_reason") or "") == _READ_IMPLYING_REASON]
    opened = sum(1 for r in rows if r.get("body_opened"))
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
        out.update(state="scored", verdict=cos.RUN_INCONCLUSIVE,
                   reason=("no host run manifest for this run — the host never "
                           "recorded what was supposed to run, so it cannot "
                           "check whether the run did it"),
                   checks=[_row("completion", INCONCLUSIVE,
                                "no host run manifest", reexecuted=True)])
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
    checks.append(check_metrics_row(vault, run_id, manifest, rows, recon))
    checks.append(check_ingestion_ledger(vault, run_id, rows, recon))
    checks.append(check_body_pass(run_id, rows))
    checks.append(check_body_open_count(run_id, rows, row))
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
    "run_artifacts", "verify_pending_runs", "verify_run",
]
