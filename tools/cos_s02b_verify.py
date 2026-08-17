#!/usr/bin/env python3
"""The DETERMINISTIC verdict on the s02b attended chips-only run.

WHY THIS EXISTS. `/plan-execute`'s `require_evidence` checks only that the
declared evidence paths exist and are non-empty (`verify.py:373-398`) — it
never reads a verdict. So without this tool s02b's outcome would be unchecked
prose: exactly the shape of the `check_self_eval` defect one level up, where a
control compared ids and never read the answers.

It re-derives every s02b assertion and all four non-vacuity floors from the
run's own artifacts, through the SAME joins the E-check answering and the
before/after truth table use (`brain.cos_echecks`) — never a second artifact
model of the same night.

    tools/cos_s02b_verify.py --handoff _evidence/cosv7/s02b-run-selection.json

Exit 0 only when every assertion PASSES and every floor is met. Any failure,
any UNPROVEN floor, and any MISSING INPUT exits non-zero: a validator that
silently reads fewer inputs than its assertions require is the same failure as
a checker that reads ids instead of answers.

PREFLIGHT IT BEFORE THE MUTATION. `--preflight` runs every input-presence and
tooling check WITHOUT requiring a completed run, so the first fixed-argv
execution does not happen after the owner's mailbox has already changed.

ponytail: one file, one argv, no config — the handoff names the run and the
run's own artifacts answer everything else.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import cos, cos_chips, cos_echecks, cos_runverify   # noqa: E402

FOUR_CHIPS = set(cos_chips.CHIPS)
READ_NOISE_SIGNAL = "read-noise-bucket"

#: Every input this tool reads, by the key it is reported under. A gate whose
#: input set is implicit is a gate that can quietly read fewer things than its
#: assertions need — so the set is DECLARED, reported, and each one fails
#: CLOSED when absent.
INPUT_KEYS = ("handoff", "ingestion_ledger", "undo_ledger", "run_manifest",
              "nightly_report", "validity_recorded", "validity_prerecord",
              "truth_table", "plan", "dry_run")


class Result:
    """Assertions and floors, each with its own verdict and its evidence."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, kind: str, name: str, ok: bool | None, detail: str) -> None:
        self.rows.append({"kind": kind, "name": name,
                          "verdict": "PASS" if ok else
                                     ("UNPROVEN" if ok is None else "FAIL"),
                          "detail": detail})

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["verdict"] != "PASS"]


def _json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(p: Path) -> list[dict[str, Any]] | None:
    try:
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
                if x.strip()]
    except (OSError, ValueError):
        return None


def resolve_inputs(handoff_path: Path, vault: Path) -> dict[str, Any]:
    """Locate every input and say plainly which ones are missing."""
    ho = _json(handoff_path)
    inputs: dict[str, Any] = {k: None for k in INPUT_KEYS}
    paths: dict[str, str] = {"handoff": str(handoff_path)}
    inputs["handoff"] = ho
    rid = str((ho or {}).get("real_run_id") or "")
    ev_dir = handoff_path.parent
    inputs["truth_table"] = _json(ev_dir / "s02-truth-table.json")
    paths["truth_table"] = str(ev_dir / "s02-truth-table.json")
    inputs["validity_prerecord"] = _json(ev_dir / "s02b-validity-prerecord.json")
    paths["validity_prerecord"] = str(ev_dir / "s02b-validity-prerecord.json")
    if not rid:
        return {"inputs": inputs, "paths": paths, "run_id": "", "vault": vault}
    ops = cos.run_ops_dir(vault)
    p = ops / f"_cos_ingestion_ledger_{rid}.jsonl"
    inputs["ingestion_ledger"] = _jsonl(p); paths["ingestion_ledger"] = str(p)
    # THE REAL LEDGER, NEVER THE DRYRUN TWIN. `cos_mutate` writes a dry pass to
    # `_cos_undo_DRYRUN_<run>.jsonl` and a real pass to `_cos_undo_ledger_…`,
    # and this vault holds run ids that have ONLY the dry file — so the
    # ambiguity exists WITHIN one run id as well as across runs.
    p = ops / f"_cos_undo_ledger_{rid}.jsonl"
    inputs["undo_ledger"] = _jsonl(p); paths["undo_ledger"] = str(p)
    p = ops / f"_cos_nightly_{rid}.md"
    try:
        inputs["nightly_report"] = p.read_text(encoding="utf-8")
    except OSError:
        inputs["nightly_report"] = None
    paths["nightly_report"] = str(p)
    inputs["run_manifest"] = cos.run_manifest(vault, rid)
    paths["run_manifest"] = str(cos.run_manifest_path(vault, rid))
    p = cos.run_validity_path(vault, rid)
    inputs["validity_recorded"] = _json(p); paths["validity_recorded"] = str(p)
    binding = _json(cos.run_plan_binding_path(vault, rid))
    plan_p = Path(str(binding["plan"])) if isinstance(binding, dict) \
        and binding.get("plan") else None
    inputs["plan"] = _json(plan_p) if plan_p else None
    paths["plan"] = str(plan_p) if plan_p else "<no plan binding>"
    dry_p = plan_p.parent / "dry-run.json" if plan_p else None
    inputs["dry_run"] = _json(dry_p) if dry_p else None
    paths["dry_run"] = str(dry_p) if dry_p else "<no plan binding>"
    return {"inputs": inputs, "paths": paths, "run_id": rid, "vault": vault,
            "binding": binding}


def check_inputs(res: Result, ctx: dict[str, Any]) -> bool:
    missing = [k for k in INPUT_KEYS if ctx["inputs"].get(k) in (None, [])]
    ho = ctx["inputs"]["handoff"]
    if not isinstance(ho, dict):
        res.add("input", "handoff", False,
                f"{ctx['paths']['handoff']} is missing or not a JSON object — "
                "the run id is never inferred from current-run.json, mtimes, "
                "filenames or narration")
        return False
    if ho.get("mode") != "real-run-complete" or not ho.get("real_run_id"):
        res.add("input", "handoff", False,
                f"handoff mode is {ho.get('mode')!r} with real_run_id "
                f"{ho.get('real_run_id')!r} — dispatch is legitimate ONLY on "
                "`real-run-complete` with a non-empty run id")
        return False
    if missing:
        res.add("input", "input-set", False,
                "these required inputs are missing or empty: "
                + ", ".join(f"{k} ({ctx['paths'].get(k, '?')})"
                            for k in missing))
        return False
    res.add("input", "input-set", True,
            f"all {len(INPUT_KEYS)} declared inputs present for "
            f"{ctx['run_id']}")
    return True


# ---------------------------------------------------------------------------
# the six assertions
# ---------------------------------------------------------------------------
def assertions(res: Result, ctx: dict[str, Any]) -> dict[str, Any]:
    vault, rid = ctx["vault"], ctx["run_id"]
    ledger = ctx["inputs"]["ingestion_ledger"]
    run = cos_echecks.load_run(vault, rid)
    chips = cos_echecks.chip_join(vault, rid, run)
    archives = cos_echecks.archive_join(vault, rid, run)

    # (1) every chip is one of the four AND the (bucket, tier) matrix fired
    bad = [c for c in chips if c["chip"] not in FOUR_CHIPS
           or c["chip"] != c["expected_chip"]]
    res.add("assertion", "1-four-chips-and-the-matrix", not bad and bool(chips),
            f"{len(chips)} dispatched chip(s); "
            + ("no chip write disagrees with the four-chip (bucket, tier) "
               "matrix" if not bad else
               f"{len(bad)} disagree: "
               + str([(c['digest'], c['verdict'], c['judged_tier'], c['chip'],
                       c['expected_chip']) for c in bad[:6]]))
            + ("" if chips else " — ZERO chips is a zero denominator, not a pass"))

    # (2) every archive READ + bucket noise, and the widening actually shipped
    bad = [a for a in archives
           if a["verdict"] != "noise" or a["read_state"] != "read"
           or a["judged_tier"] in ("P0", "P1")
           or a["noise_signal"] not in cos_echecks.ARCHIVING_SIGNALS]
    res.add("assertion", "2a-archive-eligibility", not bad and bool(archives),
            f"{len(archives)} archived thread(s); "
            + ("every one was READ, sits in bucket `noise`, is not P0/P1 and "
               "cites a recognized typed signal" if not bad else
               f"{len(bad)} breach it: "
               + str([(a['digest'], a['verdict'], a['read_state'],
                       a['judged_tier'], a['noise_signal']) for a in bad[:6]])))

    # THE LABEL IS NOT THE PROOF. A recurring sender with >=3 rows tonight can
    # carry the new signal while archiving nothing new, so the new-signal rows
    # are joined against the s02 TRUTH TABLE — the independent statement of
    # which rows the OLD rules would not have archived.
    tt = ctx["inputs"]["truth_table"]
    newly = set(tt.get("newly_archived") or []) if isinstance(tt, dict) else set()
    new_signal = [a for a in archives if a["noise_signal"] == READ_NOISE_SIGNAL]
    only_new = [a for a in new_signal if a["digest"] in newly]
    res.add("assertion", "2b-widening-shipped", bool(only_new),
            f"{len(new_signal)} archived row(s) carry `{READ_NOISE_SIGNAL}`, of "
            f"which {len(only_new)} are named by the s02 truth table as rows "
            f"the OLD rules would NOT have archived "
            f"({sorted(a['digest'] for a in only_new)[:6]}). Zero such rows "
            "means the widening did not ship, which is a miss, not a pass")

    # …and the new signal's VALIDATOR BRANCH must read a field a PRODUCER
    # writes — `automated-mail-marker` was retired at run 127 because its
    # branch validated against `ctx["automated_marker"]`, which nothing wrote.
    judge = cos_echecks._cos_judge()
    branch_ok = False
    if judge is not None:
        rule = judge.RULES.get("triage.noise_signal_required")
        v = {"auto_archive": True, "bucket": "noise", "tier": "P3",
             "noise_signal": READ_NOISE_SIGNAL}
        refuses = rule.check(v, {"read_state": "unread"}) if rule else "no rule"
        accepts = rule.check(v, {"read_state": "read"}) if rule else "no rule"
        produced = any(r.get("read_state") for r in (ledger or []))
        branch_ok = bool(refuses) and accepts is None and produced
    res.add("assertion", "2c-validator-branch-reads-a-produced-field", branch_ok,
            "the `triage.noise_signal_required` branch for "
            f"`{READ_NOISE_SIGNAL}` refuses on `read_state != read`, accepts on "
            "`read`, and `read_state` is a field the driver actually writes on "
            "the ingestion ledger" if branch_ok else
            "the branch does not discriminate on a field some producer writes "
            "— the run-127 shape")

    # (3) no unread thread was archived (and none mutated at all)
    verdicts = cos_echecks.by_conversation(ledger or [])
    touched_unread = [r.get("conversation_id_digest")
                      for r in cos_echecks.dispatched(run["undo"])
                      if verdicts.get(r.get("conversation_id"), {})
                      .get("read_state") != "read"]
    res.add("assertion", "3-unread-shield", not touched_unread,
            "no dispatched mutation names a thread the ingestion ledger reports "
            "as anything but READ" if not touched_unread else
            f"{len(touched_unread)} mutated thread(s) were not READ: "
            f"{touched_unread[:6]}")

    # (4) the run-integrity bar, from the RECORDED verdict
    rec = ctx["inputs"]["validity_recorded"]
    pre = ctx["inputs"]["validity_prerecord"]
    bar_ok, bar_why = _integrity_bar(rec)
    res.add("assertion", "4-run-integrity-bar", bar_ok, bar_why)
    same, why = _prerecord_matches(pre, rec)
    res.add("assertion", "4b-record-matches-the-pre-record-read", same, why)

    # (5) the verifier can actually FAIL — probed, not inspected
    probe_ok, probe_why = _probe_self_eval(vault, rid,
                                           ctx["inputs"]["nightly_report"])
    res.add("assertion", "5-self-eval-can-fail", probe_ok, probe_why)

    # (6) run-bound capability set, and MANAGED_CHIPS grew by exactly one
    man = ctx["inputs"]["run_manifest"] or {}
    ho = ctx["inputs"]["handoff"]
    host_commit, host_clean = man.get("git_commit"), man.get("git_clean")
    frozen = man.get("capability_digest")
    now = cos_echecks.capability_digest()
    problems = []
    if not host_commit:
        problems.append("the run manifest records no host commit, so today's "
                        "constants cannot be read as last night's")
    if host_clean is not True:
        problems.append(f"the tree was not clean when the run fired "
                        f"(git_clean={host_clean!r})")
    if ho.get("validated_commit") != host_commit:
        problems.append(f"the handoff CLAIMS commit "
                        f"{str(ho.get('validated_commit'))[:12]} while the HOST "
                        f"recorded {str(host_commit)[:12]}")
    if ho.get("worktree_clean") is not host_clean:
        problems.append("the handoff's clean flag disagrees with the host's")
    if not frozen or frozen != now:
        problems.append(f"the capability digest the manifest froze "
                        f"({str(frozen)[:12]}) is not the one this tree hashes "
                        f"to ({str(now)[:12]})")
    if len(cos_chips.CHIPS) != 4:
        problems.append(f"MANAGED_CHIPS holds {len(cos_chips.CHIPS)} name(s), "
                        "not the four the doctrine names")
    res.add("assertion", "6-run-bound-capability-set", not problems,
            "; ".join(problems) if problems else
            f"the capability set at the HOST-RECORDED commit "
            f"{str(host_commit)[:12]} (clean tree) is byte-identical to the "
            f"digest the manifest froze, and MANAGED_CHIPS holds exactly the "
            f"four names")

    # the blast cap — count the APPLIED archives against what the owner set
    cap = ho.get("owner_archive_cap")
    if not isinstance(cap, int) or isinstance(cap, bool):
        res.add("assertion", "cap-honoured", None,
                f"owner_archive_cap is {cap!r} — UNRECORDED, so the abort rule "
                "is unenforceable and cannot be verified")
    else:
        res.add("assertion", "cap-honoured", len(archives) <= cap,
                f"{len(archives)} archive(s) applied against an owner cap of "
                f"{cap}")
    plan = ctx["inputs"]["plan"] or {}
    dry = ctx["inputs"]["dry_run"] or {}
    binding = ctx.get("binding") or {}
    digest_ok = bool(plan.get("plan_digest")) and \
        plan.get("plan_digest") == dry.get("plan_digest") == \
        binding.get("plan_digest")
    res.add("assertion", "cap-plan-identity", digest_ok,
            f"the applied plan's digest {str(plan.get('plan_digest'))[:16]} is "
            f"the digest its OWN rehearsal and binding named (within one run — "
            f"a cross-run comparison is structurally impossible, `plan_digest` "
            f"hashes the run id)" if digest_ok else
            f"plan {str(plan.get('plan_digest'))[:12]}, rehearsal "
            f"{str(dry.get('plan_digest'))[:12]}, binding "
            f"{str(binding.get('plan_digest'))[:12]} do not agree")
    return {"chips": chips, "archives": archives, "run": run}


def _integrity_bar(rec: Any) -> tuple[bool, str]:
    if not isinstance(rec, dict):
        return False, "no recorded validity verdict for this run"
    verdict = rec.get("verdict")
    checks = ((rec.get("detail") or {}).get("checks")
              if isinstance(rec.get("detail"), dict) else None) \
        or rec.get("checks") or []
    non_pass = [c.get("check") for c in checks if c.get("status") != "pass"]
    self_eval = next((c for c in checks if c.get("check") == "self_eval"), None)
    if (self_eval or {}).get("status") != "pass":
        return False, (f"self_eval is {(self_eval or {}).get('status')!r}, not "
                       "pass — the owner-restated bar requires it")
    if verdict == "VALID" and not non_pass:
        return True, "VALID with every control PASS"
    if verdict == "VALID_DEGRADED" and non_pass == ["candidate_stamps"]:
        return True, ("VALID_DEGRADED whose ONLY degraded control is "
                      "`candidate_stamps`, the one documented-inapplicable "
                      "control")
    return False, (f"verdict {verdict!r} with non-PASS control(s) {non_pass} — "
                   "the bar is VALID, or VALID_DEGRADED degraded ONLY on "
                   "`candidate_stamps`, with self_eval PASS")


def _prerecord_matches(pre: Any, rec: Any) -> tuple[bool, str]:
    if not isinstance(pre, dict) or not isinstance(rec, dict):
        return False, "the pre-record read or the recorded verdict is missing"
    rc = ((rec.get("detail") or {}).get("checks")
          if isinstance(rec.get("detail"), dict) else None) or rec.get("checks")
    pc = pre.get("checks")
    if pre.get("verdict") != rec.get("verdict"):
        return False, (f"the pre-record read said {pre.get('verdict')!r}, the "
                       f"recorded file says {rec.get('verdict')!r}")
    if pc != rc:
        return False, "the checks array changed between the read and the record"
    ts_pre, ts_rec = pre.get("recorded"), rec.get("recorded")
    if ts_pre and ts_rec and str(ts_rec) <= str(ts_pre):
        return False, (f"the recorded timestamp {ts_rec} is not newer than the "
                       f"pre-record read {ts_pre}")
    return True, ("the recorded verdict and full checks array equal the "
                  "pre-record read, and only the timestamp moved")


_LINE_RE = re.compile(r"^(\s*[-*]\s*\*{0,2}E0?\d{1,2}\b[^\n]*?)\b(PASS|N/?A)\b",
                      re.MULTILINE)
_FAIL_LINE_RE = re.compile(r"^(\s*[-*]\s*\*{0,2}E0?\d{1,2}\b[^\n]*?)\bFAIL\b",
                           re.MULTILINE)


def _probe_self_eval(vault, rid: str, report_text: Any) -> tuple[bool, str]:
    """Feed the SHIPPED verifier an all-FAIL report and require it to fail.

    Existence of a parser or a test proves nothing — a skipped, uncollected or
    vacuous test satisfies existence, which is the defect class this whole plan
    exists to close. So the probe RUNS, against a scratch copy, and never
    against the real artifact.
    """
    if not isinstance(report_text, str) or not report_text.strip():
        return False, "the run report is unreadable, so the probe cannot run"
    manifest = cos.run_manifest(vault, rid) or {}
    original = report_text
    all_fail = _LINE_RE.sub(lambda m: m.group(1) + "FAIL", original)
    all_pass = _FAIL_LINE_RE.sub(lambda m: m.group(1) + "PASS", original)
    if all_fail == original and all_pass == original:
        return False, ("the report carries no E-check line to rewrite, so the "
                       "probe would be vacuous")
    dup = original.rstrip("\n") + "\n- **E1** · FAIL — a duplicated, "\
        "conflicting id\n"
    # NEVER the real artifact. s02b (2026-08-15) measured the earlier version
    # writing all three variants to `cos.run_ops_dir(vault)/_cos_nightly_<rid>.md`
    # — the canonical report — while this docstring promised a scratch copy. It
    # restored in a `finally`, but the vault is not a git repo, so a kill
    # mid-probe left the report permanently forged; the hourly re-scoring fold
    # could read an all-FAIL report as authoritative; and each write reset the
    # mtime, pushing the run back inside the 900s quiesce window (measured: a
    # verify immediately after refused with "last written 563s ago").
    with tempfile.TemporaryDirectory(prefix="cosv7-selfeval-probe-") as td:
        scratch = Path(td)
        target = cos.run_ops_dir(scratch)
        target.mkdir(parents=True, exist_ok=True)
        report = target / f"_cos_nightly_{rid}.md"

        def _score(text: str) -> str:
            report.write_text(text, encoding="utf-8")
            return cos_runverify.check_self_eval(scratch, rid, manifest)["status"]

        try:
            s_fail, s_dup, s_pass = (_score(all_fail), _score(dup),
                                     _score(all_pass))
        except OSError as exc:
            return False, f"the probe could not write its scratch report ({exc})"
    # The POSITIVE control is synthesized, not borrowed from the real report.
    # The earlier version required the REAL report to score `pass`, so on any
    # night that legitimately FAILED — exactly when the verdict matters most —
    # the probe reported "did not fail as designed" and voided assertion 4 on a
    # night when the verifier was working perfectly.
    ok = s_fail != "pass" and s_dup != "pass" and s_pass == "pass"
    return ok, (f"on a scratch vault: an all-FAIL report scores self_eval "
                f"{s_fail!r}, a duplicated conflicting id scores {s_dup!r}, and "
                f"an all-PASS variant of this same report scores {s_pass!r} — "
                f"the verifier decides on OUTCOMES, and the real artifact was "
                f"never written" if ok else
                f"THE PROBE DID NOT FAIL AS DESIGNED (all-FAIL {s_fail!r}, "
                f"duplicate {s_dup!r}, all-PASS {s_pass!r}); assertion 4 is VOID "
                f"whatever verdict it printed")


# ---------------------------------------------------------------------------
# the four non-vacuity floors
# ---------------------------------------------------------------------------
def floors(res: Result, ctx: dict[str, Any], data: dict[str, Any]) -> None:
    tt = ctx["inputs"]["truth_table"]
    newly = set(tt.get("newly_archived") or []) if isinstance(tt, dict) else set()
    archives, chips = data["archives"], data["chips"]

    i = [a for a in archives
         if a["noise_signal"] == READ_NOISE_SIGNAL and a["digest"] in newly]
    res.add("floor", "i-a-new-signal-archive-the-old-rules-would-not-have",
            bool(i) or None,
            f"{len(i)} archived thread(s) carried by the new signal that the "
            f"OLD rules would not have archived")

    ledger = ctx["inputs"]["ingestion_ledger"] or []
    mutated = {r.get("conversation_id")
               for r in cos_echecks.dispatched(data["run"]["undo"])}
    unread_untouched = [r for r in ledger
                        if r.get("read_state") == "unread"
                        and r.get("conversation_id") not in mutated]
    res.add("floor", "ii-an-unread-thread-present-and-untouched",
            bool(unread_untouched) or None,
            f"{len(unread_untouched)} unread thread(s) present in the "
            f"enumeration and named by no dispatched mutation")

    iii = [c for c in chips if c["chip"] == cos_chips.CHIP_P3
           and c["verdict"] == "read" and c["judged_tier"] in ("P2", "P3")]
    res.add("floor", "iii-a-P3-Read-chip-on-a-read-row", bool(iii) or None,
            f"{len(iii)} `P3 · Read` chip(s) whose ingestion join is "
            f"verdict=read with judged_tier in P2/P3")

    iv = [c for c in chips if c["chip"] == cos_chips.CHIP_P2
          and c["verdict"] == "act" and c["judged_tier"] == "P2"
          and str(c["state"]) == "reconciled"]
    res.add("floor", "iv-an-act-P2-row-kept-P2-This-week", bool(iv) or None,
            f"{len(iv)} dispatched-and-reconciled `P2 · This week` chip(s) whose "
            f"ingestion join is verdict=act with judged_tier=P2 — without this "
            f"the (bucket, tier) MATRIX is never exercised as a matrix")


# ---------------------------------------------------------------------------
def run(handoff: Path, vault: Path, *, preflight: bool = False) -> dict[str, Any]:
    res = Result()
    ctx = resolve_inputs(handoff, vault)
    ok = check_inputs(res, ctx)
    if preflight:
        judge = cos_echecks._cos_judge()
        res.add("preflight", "toolchain", judge is not None,
                "the judgment toolchain loads, so the eligibility rule and the "
                "validator branch can be probed" if judge is not None else
                "tools/cos_judge.py is not loadable beside the engine")
        res.add("preflight", "capability-digest",
                cos_echecks.capability_digest() is not None,
                "the executing tree hashes to a capability digest")
        return {"rows": res.rows, "ok": not res.failed, "run_id": ctx["run_id"],
                "mode": "preflight", "inputs": ctx["paths"]}
    if ok:
        data = assertions(res, ctx)
        floors(res, ctx, data)
    return {"rows": res.rows, "ok": not res.failed, "run_id": ctx["run_id"],
            "mode": "verify", "inputs": ctx["paths"],
            "table_sha256": hashlib.sha256(
                json.dumps(res.rows, sort_keys=True).encode()).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--handoff", required=True, type=Path)
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--preflight", action="store_true",
                   help="check inputs and tooling WITHOUT requiring a completed "
                        "run — run this BEFORE the mutation, never after")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    import os                                                    # noqa: PLC0415
    vault = a.vault or Path(os.environ.get(
        "BRAIN_VAULT", str(Path.home() / "DeveloperFolder/Brainiac/vault"))
    ).expanduser()
    out = run(a.handoff, vault, preflight=a.preflight)
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for r in out["rows"]:
            print(f"{r['verdict']:<8} {r['kind']:<9} {r['name']}: {r['detail']}")
        print(f"\n{'PASS' if out['ok'] else 'FAIL'} — "
              f"{len([r for r in out['rows'] if r['verdict'] != 'PASS'])} of "
              f"{len(out['rows'])} row(s) not PASS ({out['mode']})")
    return 0 if out["ok"] else 1


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
