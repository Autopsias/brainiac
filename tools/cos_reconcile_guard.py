"""The run-obligation observation guard of `cos_reconcile_metrics` (ROL-02 / E22 precedent, batch-2 drain).

`ingest_lane_open`, `_pre_contract`, `mail_leg_enumerated`, `run_started_at`,
`run_complete` and `observation_guard` moved verbatim out of
`cos_reconcile_metrics` and re-imported by it, so `recon.observation_guard(...)`
on the loaded module — the way `src/brain/cos_runverify_ledger.py` calls it —
is unchanged.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_reconcile_rows import DATE_RE, _rows  # noqa: E402

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
