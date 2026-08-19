"""Tool loading, run artifacts, ledgers, metrics rows, and completion facts."""
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

from . import cos

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
    d = _rv.tools_dir()
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
    sidecars the host wrote when it took delivery of a drop.

    DELEGATED to `cos.run_proposal_drops` since s09 (K2), because the judge now
    derives `proposals_dropped` from the same fact and two spellings of one
    predicate is the defect that made `category_gate.state` report `armed` from
    one leg and `not-run` from the other on the same run."""
    return cos.run_proposal_drops(vault, run_id)

# Parent binds, deferred past this module's own defs. ``tools_dir`` is
# monkeypatched on the FACADE by tests, so its call sites here resolve through
# the parent namespace at call time.
from . import cos_runverify as _rv  # noqa: E402
from .cos_runverify import (  # noqa: E402
    DEFAULT_QUIESCE_SECONDS as DEFAULT_QUIESCE_SECONDS,
    QUIESCE_ENV as QUIESCE_ENV,
    TOOLS_DIR_ENV as TOOLS_DIR_ENV,
    _MARKER_DISPOSITION as _MARKER_DISPOSITION,
    _RUN_NUMBER_RE as _RUN_NUMBER_RE,
    _row as _row,
)
