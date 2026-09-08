"""Verdict rollups over recent runs: pending sweep, stalls, alerts, hot entry."""
from __future__ import annotations

import datetime as _dt
import os
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


def carries_unjudged_work(vault, run_id: str) -> bool:
    """Did this run write an ingestion ledger with something in it?

    (2026-09-06, RUN-01) THE FACT THAT STOPS THE CLOCK. 2026-08-29-run207 wrote
    246 ingestion-ledger rows, then no metrics row and no validity verdict —
    a night's work on disk, uncounted and unjudged. `stalled_runs` DID report
    it, for three days, and then `STALLED_LOOKBACK_DAYS` aged the finding out
    and nothing anywhere has said so since. Ageing out is right for a run that
    produced NOTHING: an aborted night is over and re-reporting it forever is
    noise. It is wrong for a run that produced WORK, because the work does not
    age out — it is still on disk, still uncounted, still unjudged.

    A file read of one `stat`, never a parse: the question is whether the run
    left work behind, and a zero-byte or absent ledger answers it.
    """
    path = (cos.run_ops_dir(vault)
            / f"_cos_ingestion_ledger_{run_id}.jsonl")
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


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
        # THE DATE FLOOR APPLIES TO A RUN THAT LEFT NOTHING BEHIND (RUN-01).
        # See `carries_unjudged_work`: a run holding an ingestion ledger stays
        # reportable until a verdict is recorded over it, however old it is.
        work = carries_unjudged_work(vault, run_id)
        if not work and run_id[:10] < oldest.isoformat():
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
                    "artifacts": len(files), "missing": done["missing"],
                    "unjudged_work": work,
                    "metrics_row": (cos.run_ops_dir(vault)
                                    / f"_cos_metrics_row_{run_id}.json").exists()})
    return out


def _install_age(directory, now: _dt.datetime) -> dict[str, Any]:
    """How old the sheets directory itself is, when no sheet has ever landed.

    ``nights`` is a THRESHOLD, not an elapsed count, so it must never be
    reported as one: on 2026-08-26 it read "MISSED 2 night(s)" over a directory
    29 minutes old. The installer creates this directory, so its mtime dates
    the install and is the only elapsed time this pass can measure.

    DAYS, not hours: ``alerts.vault_alerts`` pins ``now`` to 12:00 UTC of the
    current date, so an hours figure reads 0.0 for anything installed after
    noon. A day count is honest under that pinned clock and is the same unit
    as ``nights``.
    """
    try:
        made = _dt.datetime.fromtimestamp(directory.stat().st_mtime,
                                          tz=_dt.timezone.utc).date()
    except OSError:                                        # pragma: no cover
        return {"installed_date": None, "installed_days": None}
    return {"installed_date": made.isoformat(),
            "installed_days": max(0, (now.date() - made).days)}


def _heartbeat_text(result: dict[str, Any], *, directory, configured: int,
                    latest: str | None, stat_errors: int) -> str:
    """The firing line. Two states, two sentences — a lane that has never run
    once has not MISSED anything, and must not borrow the threshold's number.
    """
    tail = (f"the out-of-band directory is {directory} — check the 02:00 "
            "launchd job and its log")
    if latest is not None:
        return (f"COS sheet heartbeat MISSED {configured} night(s): newest "
                f"sheet is {result['age_hours']}h old ({latest}); {tail}")
    age = (f"{stat_errors} directory entr(y/ies) could not be examined"
           if stat_errors else "no sheet has ever been written")
    days = result.get("installed_days")
    when = ("" if days is None else
            "; the directory was created today" if days == 0 else
            f"; the directory was created {days} day(s) ago "
            f"({result['installed_date']})")
    return f"COS sheet heartbeat has NEVER FIRED: {age}{when}; {tail}"


def sheet_heartbeat(vault, *, now: _dt.datetime | None = None,
                    nights: int | None = None) -> dict[str, Any]:
    """Out-of-band proof that the owner-facing sheets lane is still firing.

    This intentionally never reads a run record or COS ledger. A dead nightly
    cannot report its own death through an artifact it would have had to write;
    the newest filesystem mtime under ``feedback.sheets_dir`` is the independent
    signal. Configuration is read in this pass, at the moment it is used.
    """
    from .cos import feedback                                  # noqa: PLC0415

    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    configured = (int(os.environ.get("BRAIN_COS_SHEET_HEARTBEAT_NIGHTS", "2"))
                  if nights is None else int(nights))
    configured = max(1, configured)
    directory = feedback.sheets_dir(vault)
    # An absent directory means this COS schedule has not been installed for
    # the vault yet. The installer creates it before launchd is ever loaded;
    # from that point onward an EMPTY directory is the required known-positive
    # alarm, including a job that dies before its first ledger write.
    if not directory.is_dir():
        return {
            "firing": False,
            "nights": configured,
            "sheets_dir": str(directory),
            "latest_sheet_mtime": None,
            "age_hours": None,
            "state": "not-configured",
        }
    newest: float | None = None
    try:
        paths = list(directory.iterdir())
    except OSError as exc:
        return {
            "firing": True,
            "nights": configured,
            "sheets_dir": str(directory),
            "latest_sheet_mtime": None,
            "age_hours": None,
            "state": "unreadable",
            "text": (f"COS sheet heartbeat cannot read {directory} "
                     f"({type(exc).__name__}: {exc}) — check the 02:00 "
                     "launchd job and directory permissions"),
        }
    stat_errors = 0
    for path in paths:
        try:
            # s07's sheet contract is `<date>.html`. Finder metadata, editor
            # swaps, or a launchd log copied into this directory are not proof
            # that the owner-facing sheet lane published anything.
            if path.is_file() and path.suffix.lower() == ".html":
                newest = max(newest or 0.0, path.stat().st_mtime)
        except OSError:
            stat_errors += 1
            continue
    limit_seconds = configured * 24 * 60 * 60
    age_seconds = None if newest is None else max(0.0, now.timestamp() - newest)
    firing = age_seconds is None or age_seconds >= limit_seconds
    latest = (_dt.datetime.fromtimestamp(newest, tz=_dt.timezone.utc).isoformat()
              if newest is not None else None)
    result: dict[str, Any] = {
        "firing": firing,
        "nights": configured,
        "sheets_dir": str(directory),
        "latest_sheet_mtime": latest,
        "age_hours": (None if age_seconds is None
                      else round(age_seconds / 3600.0, 1)),
        "unreadable_entries": stat_errors,
    }
    if newest is None:
        result.update(_install_age(directory, now))
    if firing:
        result["text"] = _heartbeat_text(result, directory=directory,
                                         configured=configured, latest=latest,
                                         stat_errors=stat_errors)
    return result


def alert(vault, *, window: int = 5,
          now: _dt.datetime | None = None) -> dict[str, Any]:
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
        # ...AND WHICH OF THEM LEFT WORK BEHIND (RUN-01). A stalled run with an
        # ingestion ledger is not merely unscored: its rows were never counted
        # into `_cos_metrics.jsonl` and never judged, and no later fold will do
        # either. Named separately because the repair differs — if the run's
        # declared artifacts can no longer be written, the honest close is to
        # RECORD a verdict for it (`cos.record_run_validity`, INCONCLUSIVE with
        # the reason), which is also what makes this line go quiet.
        work = [s for s in stalled if s.get("unjudged_work")]
        if work:
            out["unjudged_work_runs"] = work
            out["unjudged_work_text"] = (
                f"{len(work)} of them wrote an INGESTION LEDGER and no verdict "
                f"({', '.join(s['run_id'] for s in work)}) — that work is on "
                "disk, uncounted and unjudged, and no fold will pick it up. "
                "Re-write the missing declared artifacts if the night can "
                "still produce them, otherwise record the run INCONCLUSIVE "
                "naming why, which is the only thing that closes it.")
    heartbeat = sheet_heartbeat(vault, now=now)
    out["sheet_heartbeat"] = heartbeat
    if heartbeat.get("firing"):
        out["sheet_heartbeat_text"] = heartbeat["text"]
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
