"""Verdict rollups over recent runs: pending sweep, stalls, alerts, hot entry."""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import cos
from .cos_runverify_io import DEFAULT_RUN_WINDOW, STALLED_LOOKBACK_DAYS, STALLED_PENDING_HOURS  # noqa: E402

def known_run_ids(vault) -> list[str]:
    """Every run the host has a manifest for, newest run number first.

    Carries the on-mount records forward first (gap-05): this is the enumerator
    the hourly fold and `brain cos-run-verify` start from, and on a host that
    has not written a manifest since the relocation it would otherwise report
    an empty history for runs whose manifests are sitting one directory away."""
    cos.migrate_run_records(vault)
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

# Parent/IO binds, deferred past this module's own defs.
from .cos_runverify import verify_run as verify_run  # noqa: E402
from .cos_runverify_io import (  # noqa: E402
    _run_number as _run_number,
    completion as completion,
    run_artifacts as run_artifacts,
)
