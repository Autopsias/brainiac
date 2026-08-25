#!/usr/bin/env python3
"""The `cos-backfill-batch-report` gate: every backfill batch report is machine-parseable and its numbers are inside their bounds.

MOVED OUT OF `.claude/eval-gates.json` (2026-08-23). It lived there as a 40-line
inline `python3 -c` string, which nothing could test and no diff could read. The
accepted-deviations allowlist below is why it had to move: an exemption
mechanism you cannot test is how a gate quietly stops gating.

THE GATE GLOBS EVERY REPORT, and that is deliberate — a later batch must not be
able to regress an earlier one — but it means one old deviation fails the gate
for every session that follows. Batches 1 and 3 carry five such deviations,
each REAL and each already written down in the report it belongs to. The owner
accepted them on 2026-08-23 so s07/s08 could proceed.

THE ALLOWLIST IS NARROW ON PURPOSE, in both directions:

* it matches ONE exact (report, failure) pair — never a report, never a
  substring, never a rule id — so a NEW failure in an exempted report still
  fails the gate;
* a listed pair that matches NOTHING is itself a FAILURE. An exemption that has
  stopped being read is an exemption nobody can see expire, and the report it
  points at has changed under it.
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

REPORTS = "_evidence/backfill/batch-*-report.md"
ALLOWLIST = Path("_evidence/backfill/accepted-deviations.json")

#: Tokens the backfill launch line must carry. A missing one means the run was
#: not the run the plan specified, whatever the report says it was. An entry
#: that is a TUPLE is one requirement with several accepted spellings.
#:
#: THE ARCHIVE BOUND HAS TWO SPELLINGS, and this list only knew the older one
#: (2026-08-24). `cos_nightly.sh` parses `--archive-cap=N` and `--approve-cap=N`
#: in ONE case arm: both set `ARCHIVE_CAP` and both pass
#: `--archive-abort-cap N` to the planner. `--approve-cap` adds exactly one
#: thing — `SESSION_APPROVED=1`, the owner ruling of 2026-08-22 that moved the
#: attended GO from a human at a TTY into the assistant session. So an attended
#: run performed the ruled way carries `--approve-cap=` and NEVER
#: `--archive-cap=`, and this gate failed it for using the only flag that
#: implements the ruling. The requirement is "the run carried an archive
#: bound"; both spellings satisfy it.
_REQUIRED_TOKENS = ("COS_INGEST_BRIDGE=1", "--all",
                    ("--archive-cap=", "--approve-cap="))

#: The body cap is a FLOOR, not a literal. This list required the exact string
#: `COS_BODY_CAP=60` until 2026-08-24, which made the plan's own planned value
#: unexecutable: at 60 the judge sees ~210 conversations and 59 opened bodies,
#: over-claims `aged-read-no-action` on threads it never read, and the judgment
#: validator ABORTS (run183, 59 of 218). Every run that reached the mailbox used
#: 131 and opened 123-124. The plan's stated reason for the token is the driver
#: default: "a command line that omits it runs at 20 bodies while s04 priced the
#: plan at ~60". So the requirement is "the cap was set, and not below what s04
#: priced" — the same widening, for the same reason, as the archive bound above.
#: Raising the cap is NOT raising the refusal threshold, which the plan forbids
#: and which nothing here permits.
_BODY_CAP_FLOOR = 60


def _fields(path: str, text: str, bad: list[str]) -> None:
    """Every field the s05 report shape promises, checked where it is stated."""
    def need(rx: str, label: str):
        m = re.search(rx, text, re.I | re.M)
        if not m:
            bad.append(f"{path}: missing/unparseable field -> {label}")
        return m

    need(r'^run_id:\s*\S+', 'run_id')
    m = need(r'^attended:\s*(\w+)', 'attended')
    if m and m.group(1).lower() != 'true':
        bad.append(f"{path}: attended is {m.group(1)}, not true - "
                   "the run had no typed GO")
    m = need(r'^command:\s*(.+)$', 'command')
    if m:
        for tok in _REQUIRED_TOKENS:
            spellings = tok if isinstance(tok, tuple) else (tok,)
            if not any(s in m.group(1) for s in spellings):
                bad.append(f"{path}: command line is missing "
                           f"{' or '.join(spellings)}")
        m2 = re.search(r'COS_BODY_CAP=(\d+)', m.group(1))
        if not m2:
            bad.append(f"{path}: command line sets no COS_BODY_CAP, so the run "
                       f"used the driver default of 20 bodies")
        elif int(m2.group(1)) < _BODY_CAP_FLOOR:
            bad.append(f"{path}: COS_BODY_CAP={m2.group(1)} is below the "
                       f"floor of {_BODY_CAP_FLOOR} s04 priced the plan at")
    m = need(r'^move budget:\s*(\d+)\s*/\s*250', 'move budget')
    if m and int(m.group(1)) > 250:
        bad.append(f"{path}: move budget {m.group(1)} exceeds 250")
    for rx, label in ((r'^unreconciled sent rows:\s*(\d+)',
                       'unreconciled sent rows'),
                      (r'^cos\.approved_awaiting_signature:\s*(\d+)',
                       'cos.approved_awaiting_signature')):
        m = need(rx, label)
        if m and int(m.group(1)) != 0:
            bad.append(f"{path}: {label} is {m.group(1)}, must be 0")
    _guards(path, text, need, bad)
    need(r'^body cap:\s*effective\s*\d+', 'body cap')
    need(r'^draw-order:\s*\S', 'draw-order')
    need(r'^undo:\s*\S', 'undo command')
    need(r'^backlog-population residual:\s*\d+\s*\|\s*inflow:\s*\d+'
         r'\s*\|\s*unread-shield holds:\s*\d+', 'three-way residual split')


def _guards(path: str, text: str, need, bad: list[str]) -> None:
    """The pre-apply guards, restated 2026-08-23 (owner ruling "do this properly").

    (ii) was "body_opened true in THIS run" for every archive/categorize. That
    is a PROXY, and it fails two ways: the plan's own design chips in one pass
    and archives in a later one, and whenever mutations exceed the body cap the
    count cannot be zero by arithmetic (run157: 131 mutations, 60-body cap,
    floor of 71). It also duplicated guard (iii) bluntly. The invariant it was
    written for is: never mutate a thread whose substance NOBODY assessed —
    neither the owner (`read_state=read`) nor this run (`body_opened=true`).
    That is what `unassessed-vs-mutation` counts.
    """
    m = need(r'^pre-apply guards:\s*candidates-vs-archive\s*(\d+),'
             r'\s*unassessed-vs-mutation\s*(\d+),'
             r'\s*unread-vs-categorize\s*(\d+)', 'pre-apply guards')
    if m and any(int(x) != 0 for x in m.groups()):
        bad.append(f"{path}: a pre-apply guard count is non-zero "
                   f"{m.groups()} - a mutation was planned that the plan forbids")
    # A zero guard count over an EMPTY mutation set is not a pass — run177
    # scored 0/0/0 on a plan with no archive and no categorize rows at all.
    # Make the denominator say so.
    m2 = need(r'^mutations scored:\s*(\d+)', 'mutations scored')
    if m and m2 and int(m2.group(1)) == 0:
        bad.append(f"{path}: guards passed over ZERO mutations - an all-clear "
                   "that equals no input is not an all-clear")


def _apply_allowlist(bad: list[str], path: Path = None) -> list[str]:
    """Drop the accepted pairs; FAIL on any entry that matched nothing."""
    path = ALLOWLIST if path is None else path
    if not path.exists():
        return bad
    entries = json.loads(path.read_text(encoding="utf-8"))["accepted"]
    wanted = {f"{e['report']}: {e['failure']}": e for e in entries}
    remaining = [b for b in bad if b not in wanted]
    unused = [k for k in wanted if k not in bad]
    for k in unused:
        remaining.append(
            f"STALE EXEMPTION: {path} accepts {k!r} (on "
            f"{wanted[k]['accepted_on']}) but the gate no longer reports it - "
            "the report changed under the exemption, so the exemption is "
            "hiding nothing and must be removed")
    return remaining


def main(argv: list[str] | None = None) -> int:
    # The two paths are overridable so the gate is TESTABLE. Its real inputs
    # live under `_evidence/`, which is gitignored, so a test that could only
    # read the live directory would be a test of this machine.
    argv = list(sys.argv[1:] if argv is None else argv)
    reports = argv[0] if argv else REPORTS
    allowlist = Path(argv[1]) if len(argv) > 1 else ALLOWLIST
    files = sorted(glob.glob(reports))
    bad: list[str] = []
    if not files:
        bad.append("NO BATCH REPORT EXISTS - a gate whose all-clear equals "
                   "no input is not a gate")
    for f in files:
        _fields(f, Path(f).read_text(encoding="utf-8", errors="replace"), bad)
    exempted = len(bad)
    bad = _apply_allowlist(bad, allowlist)
    print(f"checked {len(files)} report(s); "
          f"{exempted - len(bad)} accepted deviation(s) exempted")
    for b in bad:
        print("FAIL:", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
