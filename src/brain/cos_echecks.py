#!/usr/bin/env python3
"""The HOST answers DOCTRINE v7 §8.2's ten E-checks — from the run's artifacts.

WHY THIS EXISTS. Doctrine v1 carried 30 SELF-REPORTED checks: the run graded
its own homework and scored 27/27 for six consecutive nights while archiving
nothing for a week. v6.0 retired the list into code and overcorrected to zero
checks, which made `check_self_eval` structurally ungradeable. v7 puts ten back
and changes the thing that mattered — **who answers**. Every answer below is
computed here, by trusted host code, from the ingestion ledger, the undo
ledger, the frozen plan and its binding, the sent baseline, the grounding
declaration and the run manifest. **No E-check answer is ever a model claim.**

THE FORMAT IS LOAD-BEARING. One line per check in `_cos_nightly_<run>.md`::

    - **E<n>** · PASS|FAIL|N/A — <one-line derivation, with the denominator>

`cos_runverify._REPORT_ECHECK_RE` matches nothing without the literal verdict
token, so an honestly worded answer carrying no verdict word reads as a MISSING
check and fails the run.

THE DENOMINATOR IS PRINTED BECAUSE A CHECK SCORED ON A RUN THAT DID NOTHING IS
EVIDENCE OF NOTHING. `N/A` is legal here **only** against a denominator this
module derived as zero — and `check_self_eval` re-derives them rather than
believing the line. E1 and E10 can never be `N/A`: their denominators (the sent
baseline and the frozen capability digest) exist on every run.

ponytail: no framework, no registry — ten functions with one shape, one
renderer, one writer.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import cos, cos_chips, cos_runverify
from . import cos_echecks_delivery as delivery

PASS = "PASS"
FAIL = "FAIL"
NA = "N/A"

#: The checks whose denominator exists on EVERY run, so `N/A` is a lie there
#: whatever the arithmetic says (DOCTRINE §8.2 E1, E10).
NEVER_NA = (1, 10)

#: The one artifact that says what this run's model leg could reach. Frozen
#: into the run manifest at `cos-run-begin`; re-computed here against the
#: executing tree. The blocks are sliced by the SAME markers the source audits
#: in `tests/test_cos_mutate.py` use, so a capability change that dodges this
#: digest has to dodge those too.
CAPABILITY_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("cos_nightly.sh", "# --- BEGIN model tool gate ---",
     "# --- END model tool gate ---"),
    ("cos_mutate.py", "# --- denylist:", "# --- end denylist"),
    ("cos_mutate_page.js", "/* --- denylist", "/* --- end denylist"),
)

#: The signals that may JUSTIFY an auto-archive. `none` is a descriptive label
#: and `automated-mail-marker` was retired at run 127 (no typed field validates
#: it), so neither can carry a row into the archive lane.
ARCHIVING_SIGNALS = frozenset({"recurring-automated-sender", "read-noise-bucket"})

#: Mutation primitives this build may dispatch. Anything else in the ledger's
#: `primitive` column is an action the zero-send boundary never admitted.
PERMITTED_PRIMITIVES = frozenset({"rest-conversation-move", "rest-categorize",
                                  "rest-create-draft"})


class EcheckError(RuntimeError):
    """The E-check answering step refuses to write a report."""


def _cos_judge():
    """`tools/cos_judge.py`, loaded the way `cos_runverify` loads its checkers.

    The archive-eligibility RULE has one home — the judge, beside the rule
    registry DOCTRINE §3 quotes — and the truth table imports it rather than
    restating it. A second copy of "what may be archived" is one policy and one
    rumour, and the rumour is the one the mailbox obeys.
    """
    d = cos_runverify.tools_dir()
    if d is None:
        return None
    import sys                                                   # noqa: PLC0415
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        return cos_runverify._load_script(d, "cos_judge")
    except Exception:                                            # noqa: BLE001
        return None


def _cos_driver():
    """`tools/cos_driver.py` — the home of `category_gate_state`, E7's own
    predicate. Loaded, never re-implemented (see `_cos_judge`)."""
    d = cos_runverify.tools_dir()
    if d is None:
        return None
    import sys                                                   # noqa: PLC0415
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        return cos_runverify._load_script(d, "cos_driver")
    except Exception:                                            # noqa: BLE001
        return None


def vault_of(run: dict) -> Path:
    """The vault this artifact bundle came from — carried on the bundle so no
    check has to be handed it twice."""
    return run["vault"]


def derive(vault, run_id: str, run: dict[str, Any] | None = None
           ) -> list[dict[str, Any]]:
    """The ten answers, in id order. A check whose derivation RAISES answers
    FAIL naming the exception — never silently absent, because absence is what
    `check_self_eval` punishes and what a crashed derivation would look like."""
    run = run or load_run(vault, run_id)
    out = []
    for cid in sorted(CHECKS):
        try:
            out.append(CHECKS[cid](run, vault, run_id))
        except Exception as exc:                       # noqa: BLE001 fail closed
            out.append(_answer(cid, FAIL, 0, "unavailable",
                               f"the host derivation raised "
                               f"{type(exc).__name__}: {str(exc)[:160]}"))
    return out


def denominators(vault, run_id: str) -> dict[int, int]:
    """The host's own denominators, for corroborating an `N/A`.

    `check_self_eval` calls this rather than believing the number printed in
    the report: an `N/A` is legal only against a machine-derived ZERO, and the
    machine has to be the one that derives it.
    """
    return {a["id"]: int(a["denominator"]) for a in derive(vault, run_id)}


# ---------------------------------------------------------------------------
# rendering into the run report
# ---------------------------------------------------------------------------
SECTION_HEADING = "## 🧪 Run-integrity — E-checks"
_SECTION_RE = re.compile(
    r"^##\s*\S*\s*Run-integrity\b[^\n]*\n(?:.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL)


def render(answers: list[dict[str, Any]], *, repairs: int = 0) -> str:
    passed = sum(1 for a in answers if a["result"] == PASS)
    lines = [f"{SECTION_HEADING} ({passed}/{len(answers)} passed, "
             f"{repairs} repair rounds)", "",
             "Answered HOST-SIDE by `brain.cos_echecks` from this run's own "
             "artifacts (DOCTRINE v7 §8.1 rule 1). No line here is a model "
             "self-claim.", ""]
    for a in answers:
        lines.append(f"- **E{a['id']}** · {a['result']} — {a['detail']} "
                     f"[denominator: {a['denominator']} {a['denominator_of']}]")
    return "\n".join(lines) + "\n\n"


def write_report_section(vault, run_id: str,
                         answers: list[dict[str, Any]] | None = None
                         ) -> dict[str, Any]:
    """Replace the run report's E-check section with the host's answers.

    ASSERTS THE COUNT FIRST. `expected_echecks` is frozen from whatever
    `--skill-path` named at `cos-run-begin`, NOT from the doctrine by
    construction — a run stamped against the superseded `SKILL.md` freezes 30
    and then fails on every id §8 does not define. So a disagreement between
    the frozen count and the list this module answers stops the step LOUDLY
    rather than writing ten answers into a run that owes thirty.
    """
    manifest = cos.run_manifest(vault, run_id) or {}
    expected = manifest.get("expected_echecks")
    if not isinstance(expected, int) or isinstance(expected, bool) \
            or expected != len(CHECKS):
        raise EcheckError(
            f"run {run_id} froze `expected_echecks`={expected!r} at launch "
            f"(bundle {manifest.get('bundle_version')!r}, skill "
            f"{manifest.get('skill_path')!r}) but this host answers "
            f"{len(CHECKS)}. The night cannot be graded against a list it did "
            "not run under — re-begin the run against the doctrine that "
            "defines these checks.")
    answers = answers if answers is not None else derive(vault, run_id)
    report = cos.run_ops_dir(vault) / f"_cos_nightly_{run_id}.md"
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        raise EcheckError(f"no run report to answer into ({exc})") from exc
    block = render(answers)
    text, n = _SECTION_RE.subn(lambda _m: block, text, count=1)
    if not n:
        text = text.rstrip("\n") + "\n\n" + block
    report.write_text(text, encoding="utf-8")
    return {"run_id": run_id, "report": str(report), "answers": answers,
            "passed": sum(1 for a in answers if a["result"] == PASS),
            "failed": sorted(a["id"] for a in answers if a["result"] == FAIL),
            "expected_echecks": expected}


def main(argv: list[str] | None = None) -> int:
    import argparse                                              # noqa: PLC0415
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("vault")
    p.add_argument("--run-id", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--declare-grounding", choices=("grounded", "ungrounded"),
                   help="write the run's grounding declaration and exit")
    p.add_argument("--reason", default="")
    a = p.parse_args(argv)
    vault = Path(a.vault).expanduser()
    if a.declare_grounding:
        path = declare_grounding(vault, a.run_id, state=a.declare_grounding,
                                 reason=a.reason)
        print(json.dumps({"grounding": str(path), "state": a.declare_grounding})
              if a.json else f"grounding declared {a.declare_grounding}: {path}")
        return 0
    try:
        res = write_report_section(vault, a.run_id)
    except EcheckError as exc:
        print(f"E-check answering REFUSED: {exc}")
        return 3
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else
          f"E-checks answered for {a.run_id}: {res['passed']}/"
          f"{len(res['answers'])} PASS"
          + (f", FAILED {res['failed']}" if res["failed"] else ""))
    return 0



# The run-state loaders/joins live in cos_echecks_runs.py and the E1-E10
# checks in cos_echecks_answers.py since the 2026-08-16 size ratchet;
# re-exported so every `brain.cos_echecks.<name>` caller is unchanged.
from .cos_echecks_answers_2 import (  # noqa: E402,F401  (facade re-export)
    _TERMINAL as _TERMINAL,
    _e10 as _e10,
    _e6 as _e6,
    _e7 as _e7,
    _e8 as _e8,
    _e9 as _e9,
)
from .cos_echecks_answers import (  # noqa: E402,F401  (facade re-export)
    CHECKS as CHECKS,
    _answer as _answer,
    _e1 as _e1,
    _e2 as _e2,
    _e3 as _e3,
    _e4 as _e4,
    _e5 as _e5,
    _exact_int as _exact_int,
    _grounding_delivery as _grounding_delivery,
    short_chunks as short_chunks,
)
# (runs first: the answers module binds `_slice` etc. from this facade,
# which re-exports it from the runs module)
from .cos_echecks_runs import (  # noqa: E402,F401  (facade re-export)
    _json as _json,
    _slice as _slice,
    archive_join as archive_join,
    archive_truth_table as archive_truth_table,
    by_conversation as by_conversation,
    capability_digest as capability_digest,
    chip_join as chip_join,
    declare_grounding as declare_grounding,
    dispatched as dispatched,
    git_state as git_state,
    grounding_path as grounding_path,
    in_scope as in_scope,
    load_run as load_run,

)


# The ``__main__`` guard lives at the very END of the module, AFTER the
# facade re-exports above: under runpy (``python -m brain.<mod>``) the guard
# fires at its source position, and every facade name must already be bound
# by then (2026-08-16 size-ratchet fix).
if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
