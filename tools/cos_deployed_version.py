#!/usr/bin/env python3
"""Read the chief-of-staff bundle version back out of the deployment — LANE-AWARE.

WHY THIS EXISTS. The calibration pin is gated on guard condition 4, a plain
string equality, so a pin ahead of what actually runs silently freezes every
gated phase (`archived: 0`, every E-check green — runs 37 and 55). A pin move
must be gated on READING the version out of the deployment, not on trusting a
claim that a bundle was uploaded.

WHY IT IS LANE-AWARE. "The deployment" is not one thing. There are two
surfaces that can execute the COS nightly, and they hold DIFFERENT versions:

  codex-automation  A Codex automation whose prompt names a SKILL.md path
                    verbatim ("Read and execute <path> end to end"). That file
                    IS what runs. This is the live lane as of 2026-07-26.
  cowork-desktop    A bundle uploaded into Claude Desktop's session skill
                    store via the owner-only "Save skill" click.

On 2026-07-31 this tool read ONLY the Desktop store while the Codex lane was
the execution path, and answered `MISMATCH … Do NOT move the calibration pin`
against a perfectly healthy v5.38 deployment. Acting on that would have caused
the exact freeze the message warns about. A readback pointed at the wrong
surface is worse than none: it manufactures the wrong remediation with
confidence. So this tool now REFUSES TO ANSWER rather than guess.

Sources, by authority:

  RUN-REPORT   `<vault>/cos-ops/_cos_nightly_*.md` — the running skill states
               its own version. AUTHORITATIVE and lane-independent: the bundle
               that wrote the line is the bundle that ran. Retrospective — it
               only exists after a run.
  DEPLOYMENT   whichever lane is resolved (below). Immediate, but prospective:
               it says what the NEXT run will load.
  OTHER        the non-executing surface. Always reported, NEVER counted — a
               version sitting there satisfies no expectation and contradicts
               none.

Lane resolution, in order:
  1. `--lane {codex-automation,cowork-desktop}` — the operator asserts it.
  2. An ACTIVE Codex automation naming an existing chief-of-staff SKILL.md.
     That is a directive, not an artifact: it says what will be executed.
  3. Otherwise REFUSE (exit 2). A Desktop skill-store entry proves an upload
     happened once; it never proves that store runs tonight.

    python3 tools/cos_deployed_version.py <vault>
    python3 tools/cos_deployed_version.py --expect "chief-of-staff v5.38" <vault>
    python3 tools/cos_deployed_version.py --lane cowork-desktop <vault>

Exit 0 = lane resolved and (with --expect) the version matched.
Exit 1 = lane resolved, no counted source reports the expected version.
Exit 2 = usage, no readable source, the lane could not be resolved, or the
         asserted surface is UNSUPPORTED — i.e. the tool declines to answer
         rather than guess.

THE COWORK-DESKTOP SURFACE IS RETIRED (DEP-03, 2026-08-01). While an ACTIVE
Codex automation executes a different version, `--lane cowork-desktop` returns
UNSUPPORTED instead of a version: the store does not run the nightly, and the
two false freeze alarms both came from reading it as if it did. Documenting
that in prose does not retire it — the refusal does. Re-upload the current
bundle in Claude Desktop and the surface answers again (see
`brain.cos_deploy.cowork_support`).

ponytail: reports what each source says and never reconciles them into one
invented "the" version — a disagreement between a lane and the last run is
real information, not noise to average away.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# ONE definition of the lane rules, shared with `brain.cos`'s run manifest —
# see src/brain/cos_deploy.py for why a second copy is the drift this tool
# exists to prevent.
from brain import cos_deploy  # noqa: E402
from brain.cos_deploy import (  # noqa: E402
    LANE_CODEX, LANE_COWORK, LANES,
    _mtime, cowork_support, from_codex_automations, from_skill_store, resolve_lane,
)

# The running skill names itself in its own report. Two wordings are in the
# field and both are the SAME claim — "what I am":
#   run 55: "Classifier pin: v5.34; running skill: v5.35 — archive freeze active"
#   run 57: "calibration pin v5.36 vs active skill v5.37 froze archive/chip ..."
# Match both rather than one, or a real freeze reads as "states no version".
RUN_SKILL_RE = re.compile(
    r"\b(?:running|active)\s+skill:?\s*(v[0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)

__all__ = ["LANE_CODEX", "LANE_COWORK", "LANES", "from_codex_automations",
           "from_skill_store", "resolve_lane", "from_run_reports", "report", "main"]


def from_run_reports(vault: Path, limit: int = 6) -> list[dict]:
    """Newest-first, what the last few runs said they were running."""
    ops = vault / "cos-ops"
    reports = sorted(ops.glob("_cos_nightly_*.md"), key=lambda p: p.stat().st_mtime,
                     reverse=True)[:limit]
    out = []
    for p in reports:
        text = p.read_text(encoding="utf-8", errors="replace")
        m = RUN_SKILL_RE.search(text)
        out.append({
            "source": "run-report",
            "authority": "authoritative",
            "path": p.name,
            "mtime": _mtime(p),
            # A run that does not state its version is itself a finding: the
            # line is only emitted while a freeze is active, so absence means
            # "no freeze reported", not "no version".
            "version": f"chief-of-staff {m.group(1)}" if m else None,
            "states_version": bool(m),
        })
    return out


def report(vault: Path, *, lane: str | None = None) -> dict:
    codex = from_codex_automations()
    store = from_skill_store()
    resolved, why = resolve_lane(lane, codex)
    by_lane = {LANE_CODEX: codex, LANE_COWORK: store}
    deployed = by_lane.get(resolved, []) if resolved else []
    # DEP-03: the Desktop store is a RETIRED version source while the Codex
    # lane executes something else. Asserting it with --lane no longer buys an
    # answer — the refusal IS the retirement (prose is not).
    support = (cowork_support(store, codex) if resolved == LANE_COWORK
               else {"supported": True, "reason": ""})
    if not support["supported"]:
        deployed = [{**e, "authority": "unsupported-surface"} for e in deployed]
    # Reported, never counted. This is the whole fix: a version sitting on a
    # surface that does not execute must not satisfy or refute anything.
    other = [{**e, "authority": "not-the-execution-path"}
             for lane_name, entries in by_lane.items() if lane_name != resolved
             for e in entries]
    runs = from_run_reports(vault)
    counted = list(deployed) if support["supported"] else []
    versions = {r["version"] for r in counted + runs if r.get("version")}
    return {
        "vault": str(vault),
        "lane": resolved,
        "lane_reason": why,
        "lane_supported": support["supported"],
        "lane_unsupported_reason": "" if support["supported"] else support["reason"],
        "deployed": deployed,
        "run_reports": runs,
        "other_surfaces": other,
        "versions_seen": sorted(versions),
        "newest_run_stating_version": next(
            (r for r in runs if r.get("states_version")), None),
        "newest_deployed": counted[0] if counted else None,
    }


def _print_text(res: dict) -> None:
    print(f"deployed-version readback — {res['vault']}\n")
    print(f"LANE: {res['lane'] or '(unresolved)'} — {res['lane_reason']}\n")
    if not res.get("lane_supported", True):
        print(f"SURFACE UNSUPPORTED — {res['lane_unsupported_reason']}\n")
    print("DEPLOYMENT (what the NEXT run will load):"
          if res.get("lane_supported", True) else
          "WHAT THIS RETIRED SURFACE HOLDS (reported, NEVER an answer):")
    for r in res["deployed"] or [{"mtime": "-", "version": None,
                                  "path": "(none on this lane)",
                                  "extraction_rules_version": None}]:
        print(f"  {r.get('mtime') or '-'}  {r.get('version') or '(unversioned)'}"
              f"  ext={r.get('extraction_rules_version') or '-'}  {r['path']}")
    print("\nRUN REPORTS (authoritative: the bundle that wrote the line ran):")
    for r in res["run_reports"]:
        print(f"  {r['mtime']}  {r['version'] or '(states no version)'}  {r['path']}")
    print("\nOTHER SURFACES (reported, NEVER counted — these do not execute):")
    for r in res["other_surfaces"] or [{"mtime": "-", "version": None,
                                        "path": "(none)"}]:
        print(f"  {r.get('mtime') or '-'}  {r.get('version') or '(unversioned)'}"
              f"  {r['path']}")
    print(f"\nversions counted: {', '.join(res['versions_seen']) or '(none)'}")




def _selfcheck() -> None:
    """assert-based, on the two failures that have actually happened."""
    saved = (cos_deploy.CODEX_AUTOMATIONS, cos_deploy.DESKTOP_SESSIONS)
    try:
        _selfcheck_body()
    finally:  # never leave the module pointed at a deleted temp dir
        cos_deploy.CODEX_AUTOMATIONS, cos_deploy.DESKTOP_SESSIONS = saved
    print("selfcheck OK — lane resolved or refused; stale surfaces never count")


def _selfcheck_body() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vault = root / "vault"
        (vault / "cos-ops").mkdir(parents=True)
        (vault / "cos-ops" / "_cos_nightly_2026-07-29-run55.md").write_text(
            "Classifier pin: v5.34; running skill: v5.35 — archive freeze active\n")

        # A Desktop store that is NOT the execution path.
        store = root / "store" / "s1" / "x" / "skills" / "chief-of-staff"
        store.mkdir(parents=True)
        (store / "SKILL.md").write_text('kernel_version: "chief-of-staff v5.29"\n')

        # With no automation at all, the lane is unresolvable -> REFUSE.
        cos_deploy.CODEX_AUTOMATIONS = root / "none"
        cos_deploy.DESKTOP_SESSIONS = root / "store"
        assert main(["x", str(vault)]) == 2, "answered without knowing the lane"

        # The 2026-07-31 false positive: an ACTIVE automation names a mirror at
        # v5.38 while the store still holds v5.29. --expect v5.38 must pass.
        mirror = root / "repo" / "chief-of-staff"
        mirror.mkdir(parents=True)
        (mirror / "SKILL.md").write_text('kernel_version: "chief-of-staff v5.38"\n')
        auto = root / "autos" / "cos"
        auto.mkdir(parents=True)
        (auto / "automation.toml").write_text(
            'status = "ACTIVE"\nprompt = "Read and execute\\n'
            f'{mirror / "SKILL.md"}\\nend to end."\n')
        cos_deploy.CODEX_AUTOMATIONS = root / "autos"
        assert main(["x", "--expect", "chief-of-staff v5.38", str(vault)]) == 0, \
            "the live Codex lane's own version was refused"
        # ...and the stale store must NOT be able to satisfy an expectation.
        assert main(["x", "--expect", "chief-of-staff v5.29", str(vault)]) == 1, \
            "a non-executing surface satisfied --expect"
        # The run-report wording that only appears during a freeze still counts.
        (vault / "cos-ops" / "_cos_nightly_2026-07-30-run57.md").write_text(
            "- Degraded: calibration pin v5.36 vs active skill v5.37 froze "
            "archive/chip reevaluation.\n")
        assert main(["x", "--expect", "chief-of-staff v5.37", str(vault)]) == 0
        assert report(vault)["newest_run_stating_version"]["version"] == \
            "chief-of-staff v5.37"

        # DEP-03: asserting the retired Desktop surface buys UNSUPPORTED, not
        # a version — and not a --expect verdict in either direction.
        assert main(["x", "--lane", LANE_COWORK, str(vault)]) == 2
        assert main(["x", "--lane", LANE_COWORK, "--expect",
                     "chief-of-staff v5.29", str(vault)]) == 2
        assert report(vault, lane=LANE_COWORK)["newest_deployed"] is None
        # ...and re-uploading the current bundle makes it answerable again.
        (store / "SKILL.md").write_text('kernel_version: "chief-of-staff v5.38"\n')
        assert main(["x", "--lane", LANE_COWORK, "--expect",
                     "chief-of-staff v5.38", str(vault)]) == 0


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.cos_deployed_version", sys.modules[__name__])
from tools.cos_deployed_steps import main  # noqa: E402

if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        raise SystemExit(main(sys.argv))
