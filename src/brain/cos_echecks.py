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


# ---------------------------------------------------------------------------
# the executing tree, and its capability digest
# ---------------------------------------------------------------------------
def _slice(text: str, begin: str, end: str) -> str | None:
    i = text.find(begin)
    if i < 0:
        return None
    j = text.find(end, i)
    return None if j < 0 else text[i:j + len(end)]


def capability_digest(tools: Path | None = None) -> str | None:
    """sha256 over the capability set the model leg runs under, or ``None``
    when the executing tree is not on disk to hash.

    THE BOUNDARY IS THE CAPABILITY SET, NOT THE FENCE (DOCTRINE §2.8). The
    `⟦UNTRUSTED DATA⟧` wrapper is a mitigation — LLMail-Inject solved
    Spotlighting, Prompt Shields, TaskTracker and an LLM judge, together — so
    what E10 asserts is that the tool grant, the mutation allowlist and the
    zero-send denylist are the ones the run began with, byte for byte.
    """
    tools = tools or cos_runverify.tools_dir()
    if tools is None:
        return None
    h = hashlib.sha256()
    for name, begin, end in CAPABILITY_BLOCKS:
        p = tools / name
        try:
            block = _slice(p.read_text(encoding="utf-8"), begin, end)
        except OSError:
            return None
        if block is None:
            return None
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(block.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def git_state(repo: Path | None = None) -> dict[str, Any]:
    """The commit that is about to run, and whether the tree was clean.

    THE RUN MANIFEST RECORDED NO GIT STATE AT ALL, so "MODEL_TOOLS were
    unchanged at the commit that actually ran" was unprovable after the fact:
    inspecting today's constants proves what exists while you verify, never
    what mutated the mailbox. One field and one flag close that; a claim
    written by hand into a handoff file cannot.
    """
    repo = repo or (cos_runverify.tools_dir() or Path.cwd()).parent
    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(repo), *args],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {"commit": head,
            "clean": (status == "") if status is not None else None}


# ---------------------------------------------------------------------------
# the run's artifacts, read ONCE
# ---------------------------------------------------------------------------
def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_run(vault, run_id: str) -> dict[str, Any]:
    """Every artifact the ten checks read, gathered once and named honestly.

    An artifact that is absent is ``None`` here and the check that needs it
    says so in its own answer — never a silent zero, which is the shape that
    lets a check pass because its input was empty.
    """
    ops = cos.run_ops_dir(vault)
    ledger = cos_runverify.ledger_rows(vault, run_id)
    undo = cos_runverify._read_jsonl(ops / f"_cos_undo_ledger_{run_id}.jsonl")
    drafts = cos_runverify._read_jsonl(
        ops / f"_cos_drafts_pending_{run_id}.jsonl")
    binding = _json(cos.run_plan_binding_path(vault, run_id))
    plan = _json(Path(str(binding.get("plan")))) if isinstance(binding, dict) \
        and binding.get("plan") else None
    ev = Path(str(binding["plan"])).parent if isinstance(binding, dict) \
        and binding.get("plan") else None
    stamps = None
    for name in ("categories-bound.json", "categories.json"):
        raw = _json(ev / name) if ev else None
        if isinstance(raw, list):
            stamps = {str(r.get("conversation_id")): r.get("category")
                      for r in raw if isinstance(r, dict)}
            break
        if isinstance(raw, dict):
            stamps = {str(k): v for k, v in raw.items()}
            break
    return {
        "vault": Path(str(vault)),
        "category_stamps": stamps,
        "ops": ops,
        "ledger": ledger,
        "undo": undo,
        "drafts": drafts,
        "binding": binding,
        "plan": plan,
        "evidence_dir": ev,
        "apply": _json(ev / "apply.json") if ev else None,
        "dry_run": _json(ev / "dry-run.json") if ev else None,
        "judgment": _json(ev / "judgment.json") if ev else None,
        "sent_baseline": _json(ops / f"_cos_sent_baseline_{run_id}.json"),
        "grounding": _json(grounding_path(vault, run_id)),
        # THE DELIVERY JOIN (grounding design D2a). A separate artifact from the
        # declaration on purpose: the declaration is the fetcher's word about
        # itself, and this is what the composed prompt actually carried.
        "grounding_join": _json(ev / "grounding-join.json") if ev else None,
        "manifest": cos.run_manifest(vault, run_id) or {},
    }


def grounding_path(vault, run_id: str) -> Path:
    """Where the run DECLARES its grounding state (DOCTRINE §8.2 E10).

    A missing file is a FAIL, not an ungrounded night: an ungrounded night is a
    thing the run SAYS, never a thing an absent file implies.
    """
    return cos.run_ops_dir(vault) / f"_cos_grounding_{run_id}.json"


def declare_grounding(vault, run_id: str, *, state: str, reason: str = "",
                      required: list[str] | None = None,
                      covered: list[str] | None = None) -> Path:
    """Write that declaration. HOST-only, once per run, at launch."""
    if state not in ("grounded", "ungrounded"):
        raise EcheckError(f"grounding state {state!r} is neither `grounded` "
                          "nor `ungrounded` — an undeclared state is a FAIL")
    if state == "ungrounded" and not reason.strip():
        raise EcheckError("an UNGROUNDED night must say why — a bare state is "
                          "the absent file wearing a word")
    p = grounding_path(vault, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": run_id, "state": state,
                             "reason": reason,
                             "required": sorted(required or []),
                             "covered": sorted(covered or [])},
                            indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# joins the checks share (and `tools/cos_s02b_verify.py` reuses)
# ---------------------------------------------------------------------------
def dispatched(undo: list[dict[str, Any]], verb: str | None = None
               ) -> list[dict[str, Any]]:
    """Undo-ledger rows that actually reached the mailbox.

    `dispatched` — not the verification WORD. The page half returns
    `verification: "verified-failed"` for a target that simply was not there,
    because it has one word for "no" (run 125), so the host discriminates on
    the flag.
    """
    return [r for r in undo if r.get("dispatched") is True
            and (verb is None or r.get("verb") == verb)]


def by_conversation(ledger: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["conversation_id"]: r for r in ledger if r.get("conversation_id")}


def in_scope(row: dict[str, Any]) -> bool:
    """THE ONE Phase-1.6 eligibility definition — `act`, plus `read` at P0/P1.

    Shared by batching, grounding and E9's denominator. Three copies is how a
    denominator drifts (DOCTRINE §8.2 E9).
    """
    return (row.get("verdict") == "act"
            or (row.get("verdict") == "read"
                and row.get("judged_tier") in ("P0", "P1")))


def archive_join(vault, run_id: str, run: dict[str, Any] | None = None
                 ) -> list[dict[str, Any]]:
    """Every ARCHIVED thread this run dispatched, joined to its verdict row.

    ONE join, used by E3, by the before/after truth table and by the s02b
    gate — never three artifact models of the same night.
    """
    run = run or load_run(vault, run_id)
    verdicts = by_conversation(run["ledger"])
    out = []
    for r in dispatched(run["undo"], "archive"):
        cid = r.get("conversation_id")
        v = verdicts.get(cid) or {}
        out.append({
            "conversation_id": cid,
            "digest": r.get("conversation_id_digest")
            or hashlib.sha256(str(cid).encode()).hexdigest()[:16],
            "in_ledger": cid in verdicts,
            "verdict": v.get("verdict"),
            "judged_tier": v.get("judged_tier"),
            "read_state": v.get("read_state"),
            "noise_signal": v.get("noise_signal"),
            "state": r.get("state"),
        })
    return out


def chip_join(vault, run_id: str, run: dict[str, Any] | None = None
              ) -> list[dict[str, Any]]:
    """Every CHIP this run dispatched, joined to the verdict that chose it."""
    run = run or load_run(vault, run_id)
    verdicts = by_conversation(run["ledger"])
    out = []
    for r in dispatched(run["undo"], "categorize"):
        cid = r.get("conversation_id")
        v = verdicts.get(cid) or {}
        want = cos_chips.chip_for(v.get("verdict"), v.get("judged_tier"))
        out.append({
            "conversation_id": cid,
            "digest": r.get("conversation_id_digest")
            or hashlib.sha256(str(cid).encode()).hexdigest()[:16],
            "in_ledger": cid in verdicts,
            "chip": r.get("chip"),
            "expected_chip": want,
            "verdict": v.get("verdict"),
            "judged_tier": v.get("judged_tier"),
            "before_image": r.get("before_image") or [],
            "verification": r.get("verification"),
            "state": r.get("state"),
        })
    return out


def archive_truth_table(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """BEFORE/AFTER over one run's real ledger: which rows the v7 widening
    newly makes archive-eligible, and which it takes away (never any).

    BEFORE is the shipped screen — a MODEL-claimed `auto_archive`, then the
    mechanical re-screens. AFTER adds the host's own
    `cos_judge.archive_eligibility`. A widening you cannot enumerate is a
    widening you cannot review, so this returns the rows, not a count.
    """
    judge = _cos_judge()
    if judge is None:
        raise EcheckError(
            "the mutation/judgment toolchain is not on disk beside the engine, "
            "so the AFTER column cannot be derived from the shipped rule — and "
            "a truth table with a guessed AFTER is worse than none")

    def screens(row: dict[str, Any]) -> str | None:
        if row.get("auto_archive") is not True:
            return "no archive claim"
        if row.get("judgment_pending"):
            return "no verdict"
        if row.get("read_state") != "read":
            return "unread — the shield"
        if row.get("tier") in ("P0", "P1"):
            return "carries a P0/P1 chip"
        if row.get("judged_tier") in ("P0", "P1"):
            return "judged P0/P1 — the blast floor"
        return None

    before, after = [], []
    for row in ledger:
        d = hashlib.sha256(str(row.get("conversation_id")).encode()).hexdigest()[:16]
        if screens(row) is None:
            before.append({"digest": d, "signal": row.get("noise_signal"),
                           "verdict": row.get("verdict"),
                           "judged_tier": row.get("judged_tier")})
        widened = dict(row)
        elig = judge.archive_eligibility(widened)
        if elig is not None:
            widened["auto_archive"], widened["noise_signal"] = elig
        if screens(widened) is None:
            after.append({"digest": d, "signal": widened.get("noise_signal"),
                          "verdict": widened.get("verdict"),
                          "judged_tier": widened.get("judged_tier")})
    b = {r["digest"] for r in before}
    a = {r["digest"] for r in after}
    return {"rows": len(ledger), "before": before, "after": after,
            "newly_archived": sorted(a - b), "no_longer_archived": sorted(b - a)}


# ---------------------------------------------------------------------------
# the ten checks
# ---------------------------------------------------------------------------
def _answer(cid: int, result: str, denominator: int, of: str, detail: str
            ) -> dict[str, Any]:
    if result == NA and cid in NEVER_NA:
        result = FAIL
        detail = (f"answered N/A, which E{cid} may never be — its denominator "
                  f"({of}) exists on every run. " + detail)
    if result == NA and denominator:
        result = FAIL
        detail = (f"answered N/A over a NON-ZERO denominator ({denominator} "
                  f"{of}) — N/A is legal only against a machine-derived zero. "
                  + detail)
    return {"id": cid, "result": result, "denominator": denominator,
            "denominator_of": of, "detail": detail}


def _e1(run: dict[str, Any]) -> dict[str, Any]:
    disp = dispatched(run["undo"])
    baseline = run["sent_baseline"]
    items = len(baseline.get("items") or []) if isinstance(baseline, dict) else 0
    of = "dispatched mutation row(s), against the read leg's sent baseline"
    problems = []
    if not isinstance(baseline, dict):
        problems.append("the sent baseline is missing or unreadable")
    bad = sorted({str(r.get("primitive")) for r in disp
                  if str(r.get("primitive")) not in PERMITTED_PRIMITIVES})
    if bad:
        problems.append(f"primitive(s) outside the permitted three: {bad}")
    sends = [r for r in run["undo"]
             if ((r.get("receipts") or {}).get("send_attempted") is True
                 or r.get("send_attempted") is True)]
    if sends:
        problems.append(f"{len(sends)} row(s) record a send attempt")
    frozen = (run["manifest"] or {}).get("capability_digest")
    now = capability_digest()
    if not frozen:
        problems.append("the run manifest froze no capability digest, so the "
                        "permitted and banned sets this run began with cannot "
                        "be re-derived")
    elif now is None:
        problems.append("the executing tree is not on disk to re-hash the "
                        "permitted and banned sets against")
    elif now != frozen:
        problems.append(f"the capability set CHANGED during the run "
                        f"({frozen[:12]}… → {now[:12]}…)")
    detail = (f"{len(disp)} dispatched mutation(s), all on the three permitted "
              f"primitives; no send attempted; sent baseline present "
              f"({items} item(s) in its window); capability set byte-identical "
              f"to the digest the manifest froze")
    return _answer(1, FAIL if problems else PASS, len(disp) + items, of,
                   "; ".join(problems) if problems else detail)


def _e2(run: dict[str, Any]) -> dict[str, Any]:
    disp = dispatched(run["undo"])
    verdicts = by_conversation(run["ledger"])
    of = "dispatched mutation row(s)"
    absent = sorted({str(r.get("conversation_id_digest") or "?") for r in disp
                     if r.get("conversation_id") not in verdicts})
    unread = sorted({str(r.get("conversation_id_digest") or "?") for r in disp
                     if verdicts.get(r.get("conversation_id"), {})
                     .get("read_state") == "unread"})
    if not disp:
        return _answer(2, NA, 0, of,
                       "this run dispatched no mutation, so no thread was "
                       "screened for read state")
    if absent or unread:
        return _answer(2, FAIL, len(disp), of,
                       (f"{len(absent)} mutated thread(s) absent from the "
                        f"ingestion ledger {absent[:6]} — absence is a FAIL, "
                        "never an excuse; " if absent else "")
                       + (f"{len(unread)} mutated thread(s) carry "
                          f"`read_state: unread` {unread[:6]}" if unread else ""))
    return _answer(2, PASS, len(disp), of,
                   f"every one of {len(disp)} mutated thread(s) joins the "
                   "ingestion ledger and was screened READ before the mutation")


def _e3(run: dict[str, Any], vault, run_id: str) -> dict[str, Any]:
    rows = archive_join(vault, run_id, run)
    of = "archive row(s) in the undo ledger"
    if not rows:
        return _answer(3, NA, 0, of, "this run archived nothing")
    bad = []
    for r in rows:
        if not r["in_ledger"]:
            bad.append(f"{r['digest']}:not-enumerated")
        elif r["verdict"] != "noise":
            bad.append(f"{r['digest']}:verdict={r['verdict']}")
        elif r["read_state"] != "read":
            bad.append(f"{r['digest']}:read_state={r['read_state']}")
        elif r["judged_tier"] in ("P0", "P1"):
            bad.append(f"{r['digest']}:tier={r['judged_tier']}")
        elif r["noise_signal"] not in ARCHIVING_SIGNALS:
            bad.append(f"{r['digest']}:signal={r['noise_signal']}")
    if bad:
        return _answer(3, FAIL, len(rows), of,
                       f"{len(bad)} of {len(rows)} archived thread(s) breach "
                       f"the eligibility rule: {bad[:8]}")
    sig: dict[str, int] = {}
    for r in rows:
        sig[str(r["noise_signal"])] = sig.get(str(r["noise_signal"]), 0) + 1
    return _answer(3, PASS, len(rows), of,
                   f"all {len(rows)} archived thread(s) were READ, sit in "
                   f"bucket `noise`, are not P0/P1 and cite a recognized typed "
                   f"signal ({sig})")


def _e4(run: dict[str, Any], vault, run_id: str) -> dict[str, Any]:
    rows = chip_join(vault, run_id, run)
    of = "categorize row(s) in the undo ledger"
    if not rows:
        return _answer(4, NA, 0, of, "this run wrote no chip")
    managed = set(cos_chips.CHIPS)
    bad = []
    for r in rows:
        if r["chip"] not in managed:
            bad.append(f"{r['digest']}:chip={r['chip']!r}-not-one-of-the-four")
        elif not r["in_ledger"]:
            bad.append(f"{r['digest']}:not-enumerated")
        elif r["expected_chip"] is None:
            bad.append(f"{r['digest']}:matrix-assigns-no-chip-to-"
                       f"{r['verdict']}/{r['judged_tier']}")
        elif r["chip"] != r["expected_chip"]:
            bad.append(f"{r['digest']}:{r['verdict']}/{r['judged_tier']}-wants-"
                       f"{r['expected_chip']!r}-got-{r['chip']!r}")
        elif [c for c in r["before_image"] if c in managed]:
            bad.append(f"{r['digest']}:the-thread-already-carried-a-managed-chip")
    if bad:
        return _answer(4, FAIL, len(rows), of,
                       f"{len(bad)} of {len(rows)} chip write(s) disagree with "
                       f"the four-chip (bucket, tier) matrix: {bad[:8]}")
    per: dict[str, int] = {}
    for r in rows:
        per[str(r["chip"])] = per.get(str(r["chip"]), 0) + 1
    return _answer(4, PASS, len(rows), of,
                   f"all {len(rows)} chip(s) are one of the four managed names, "
                   f"match the (bucket, tier) matrix and landed on a bare "
                   f"thread ({per})")


def _e5(run: dict[str, Any]) -> dict[str, Any]:
    rows = dispatched(run["undo"], "draft")
    of = "draft row(s) in the undo ledger"
    if not rows:
        return _answer(5, NA, 0, of, "this run produced no draft")
    scope = {d.get("conversation_id"): d.get("recipient_scope")
             for d in run["drafts"]}
    bad, seen, twice = [], set(), []
    for r in rows:
        cid = r.get("conversation_id")
        d = r.get("conversation_id_digest") or "?"
        if scope.get(cid) != "original-thread-only":
            bad.append(f"{d}:recipient_scope={scope.get(cid)!r}")
        if cid in seen:
            twice.append(str(d))
        seen.add(cid)
    if bad or twice:
        return _answer(5, FAIL, len(rows), of,
                       (f"{len(bad)} draft(s) name recipients beyond the "
                        f"original thread: {bad[:6]}; " if bad else "")
                       + (f"{len(twice)} conversation(s) were drafted twice: "
                          f"{twice[:6]}" if twice else ""))
    return _answer(5, PASS, len(rows), of,
                   f"all {len(rows)} draft(s) are scoped `original-thread-only` "
                   "and no conversation was drafted twice")


_TERMINAL = ("reconciled", "confirmed", "excluded", "stopped", "skipped")


def _e6(run: dict[str, Any]) -> dict[str, Any]:
    plan = run["plan"] if isinstance(run["plan"], dict) else None
    binding = run["binding"] if isinstance(run["binding"], dict) else None
    disp = dispatched(run["undo"])
    planned = list((plan or {}).get("mutations") or [])
    of = "row(s) in the frozen plan"
    stopped = ((run["apply"] or {}).get("stopped")
               if isinstance(run["apply"], dict) else None)
    if not disp and not planned and stopped:
        return _answer(6, NA, 0, of,
                       f"the apply lane never ran ({str(stopped)[:120]})")
    if binding is None or plan is None:
        return _answer(6, FAIL, len(planned), of,
                       "this run recorded mutations with no readable plan "
                       "binding or frozen plan — an absent binding on a run "
                       "that mutated is a FAIL, not a quiet night")
    problems = []
    if binding.get("source") != "frozen":
        problems.append(f"binding source is {binding.get('source')!r}, not "
                        "`frozen`")
    if binding.get("plan_digest") != plan.get("plan_digest"):
        problems.append("the binding's digest does not match the plan it names")
    if binding.get("planned") != len(planned):
        problems.append(f"the binding claims {binding.get('planned')} planned "
                        f"row(s), the plan carries {len(planned)}")
    keys = {(m.get("conversation_id"), m.get("verb")) for m in planned}
    states = {}
    for r in run["undo"]:
        states.setdefault((r.get("conversation_id"), r.get("verb")), set()
                          ).add(str(r.get("state")))
    unterminated = [k for k in keys
                    if not (states.get(k, set()) & set(_TERMINAL))]
    strangers = sorted({str(r.get("conversation_id_digest") or "?")
                        for r in disp
                        if (r.get("conversation_id"), r.get("verb")) not in keys})
    if unterminated:
        problems.append(f"{len(unterminated)} planned row(s) reached no "
                        "terminal state")
    if strangers:
        problems.append(f"{len(strangers)} dispatched row(s) name a "
                        f"conversation the frozen plan never did: {strangers[:6]}")
    if problems:
        return _answer(6, FAIL, len(planned), of, "; ".join(problems))
    return _answer(6, PASS, len(planned), of,
                   f"the binding re-hashes to the frozen plan "
                   f"({str(plan.get('plan_digest'))[:12]}…), all {len(planned)} "
                   f"planned row(s) reached a terminal state, and no ledger row "
                   "names a conversation the plan did not")


def _e7(run: dict[str, Any]) -> dict[str, Any]:
    ledger = run["ledger"]
    of = "enumerated in-scope conversation(s)"
    judgment = run["judgment"] if isinstance(run["judgment"], dict) else None
    reported = ((judgment or {}).get("run_facts") or {}).get("category_gate")
    if not ledger:
        return _answer(7, NA, 0, of,
                       "the night stopped before enumeration, so there was no "
                       "gate to arm")
    if not isinstance(reported, dict):
        return _answer(7, FAIL, len(ledger), of,
                       "the run reported no `run_facts.category_gate`, so there "
                       "is nothing to recompute it against")
    # THE DRIVER'S OWN PREDICATE, RE-RUN — never a second spelling of it. Two
    # spellings is the exact defect `category_gate_state` was written to end:
    # the driver armed on `categories is not None` and the judge on
    # `categories`, so one run reported `armed` from one leg and `not-run` from
    # the other. A check that re-derives the state with its own arithmetic
    # would be a THIRD.
    stamps = run.get("category_stamps")
    driver = _cos_driver()
    if driver is None or stamps is None:
        return _answer(7, FAIL, len(ledger), of,
                       "the category answer this run bound, or the driver that "
                       "owns the gate predicate, is not on disk — so the "
                       "reported state cannot be recomputed, and an "
                       "unverifiable claim is not a pass")
    taxonomy = (cos.ingest_taxonomy(vault_of(run)) or {}).get("rules") or {}
    recomputed = driver.category_gate_state(
        stamps, (r["conversation_id"] for r in ledger), taxonomy)
    differs = [k for k in ("state", "in_scope", "unstamped_in_scope",
                           "undefined_ids")
               if reported.get(k) != recomputed.get(k)]
    if differs:
        return _answer(7, FAIL, len(ledger), of,
                       f"the reported category gate disagrees with the "
                       f"recomputation on {differs}: reported "
                       f"{ {k: reported.get(k) for k in differs} }, recomputed "
                       f"{ {k: recomputed.get(k) for k in differs} }")
    excluded = sum(1 for r in ledger if r.get("category_gate_excluded"))
    return _answer(7, PASS, len(ledger), of,
                   f"the reported state `{reported.get('state')}` equals the "
                   f"state recomputed host-side by the driver's own predicate "
                   f"over this run's enumeration, stamps and taxonomy "
                   f"({recomputed.get('unstamped_in_scope')} unstamped, "
                   f"{len(recomputed.get('undefined_ids') or [])} "
                   f"taxonomy-undefined, {excluded} excluded before the draw)")


def _e8(run: dict[str, Any]) -> dict[str, Any]:
    from . import cos_runverify as rv                            # noqa: PLC0415
    opened = [r for r in run["ledger"] if r.get("body_opened")]
    of = "row(s) with `body_opened: true`"
    if not opened:
        return _answer(8, NA, 0, of, "this run opened no body")
    allowed = rv._HELD_REASONS | rv._HOST_HELD_REASONS
    silent = [r for r in opened
              if r.get("disposition") in (None, "")
              and not r.get("candidate_count")]
    bad = [str(r.get("held_reason")) for r in opened
           if r.get("disposition") not in ("candidate", None, "")
           and str(r.get("held_reason") or "") not in allowed]
    if silent or bad:
        return _answer(8, FAIL, len(opened), of,
                       (f"{len(silent)} body-opened row(s) carry a null "
                        "disposition — answered by silence; " if silent else "")
                       + (f"held_reason(s) outside the managed set: "
                          f"{sorted(set(bad))[:8]}" if bad else ""))
    return _answer(8, PASS, len(opened), of,
                   f"all {len(opened)} body-opened row(s) carry a candidate or "
                   "a disposition with a `held_reason` from the managed set")


def _e9(run: dict[str, Any]) -> dict[str, Any]:
    scope = [r for r in run["ledger"] if in_scope(r)]
    of = "in-scope row(s) (`act`, plus `read` at P0/P1)"
    if not scope:
        return _answer(9, NA, 0, of,
                       "no row reached the Phase-1.6 in-scope population")
    undisposed = [r for r in scope if not r.get("disposition")]
    judgment = run["judgment"] if isinstance(run["judgment"], dict) else None
    cov = (judgment or {}).get("model_coverage") or {}
    floor = (judgment or {}).get("model_coverage_floor")
    fraction = cov.get("fraction")
    problems = []
    if undisposed:
        problems.append(f"{len(undisposed)} in-scope row(s) carry no disposition")
    if not isinstance(fraction, (int, float)):
        problems.append("the run recorded no model coverage to score")
    elif isinstance(floor, (int, float)) and fraction < floor:
        problems.append(f"model coverage {fraction:.4f} is below the floor "
                        f"{floor} this run recorded")
    elif not isinstance(floor, (int, float)):
        problems.append("the run recorded no coverage FLOOR, so `at or above "
                        "the floor it recorded` cannot be scored")
    if problems:
        return _answer(9, FAIL, len(scope), of, "; ".join(problems))
    return _answer(9, PASS, len(scope), of,
                   f"all {len(scope)} in-scope row(s) carry a disposition and "
                   f"model coverage {fraction:.4f} is at or above the recorded "
                   f"floor {floor}")


def short_chunks(join: dict[str, Any]) -> list[str]:
    """THE ONE delivery predicate: which chunks did not carry what they mapped.

    THREE COPIES OF THIS EXISTED, in three languages, and they already disagreed
    (review 2026-08-15): `tools/cos_batch_chunk.py` tested
    `map_bytes_found is not True` while this module and `tools/cos_nightly.sh`
    tested `is False` — a real semantic split on the null case, asserted by
    nothing. That is exactly what `cos_judge.batch_membership` states three
    modules over: *"it does not keep a second copy. Three copies is how a
    denominator drifts."*

    IT LIVES IN THE ENGINE, not in `tools/`, and that inverts the review's
    suggested direction on purpose. THE REASON RECORDED FOR IT WAS FALSE and is
    corrected here (review 2026-08-15): the round that moved it wrote *"an
    INSTALLED engine has no `tools/` tree to reach into"*, and this repository
    contradicts that three times — `core.py` and `doctor.py` both insert
    `repo_root / "tools"` on `sys.path` and import from it, and
    `src/brain/_assets/tools/` ships four tool modules inside the package
    precisely so an installed engine DOES have them.

    The true reason is simpler and does not depend on packaging at all: this
    predicate is a GATE concern. `_e10` is the only thing that can fail a night
    on it, `_e10` lives in this module, and a gate that has to reach outside its
    own module for the rule it enforces is a gate that can fail to find it. The
    producer (`tools/cos_batch_chunk.short_chunks`) delegates HERE, so there is
    one definition and the gate owns it. Direction of dependency is a
    consequence, not the argument.

    THE NULL DECISION, made once and documented here: `map_bytes_found: null` is
    the ABSENT-MAP case and is NOT short by this predicate. There are no bytes to
    look for, so the assertion has no subject — and nothing is lost by excluding
    it, because a chunk with no map contributes no `covered_ids`, so the coverage
    condition fails the night anyway, and with a reason an operator can act on
    ("delivered grounding for 3 of 27 required ids") rather than "chunk-01 is
    short".
    """
    return [c.get("chunk") for c in (join.get("chunks") or [])
            if c.get("missing") or c.get("missing_text") or c.get("unexpected")
            or c.get("map_bytes_found") is False
            or (c.get("text_found_in_prompt") or 0) < (c.get("with_text") or 0)]


def _exact_int(value: Any) -> int | None:
    """`value` if it is a REAL int, else None. `True` is not a coverage count —
    `isinstance(True, int)` is True in Python, and the review passed a join
    declaring `required_covered_by_chunks: true`."""
    return value if type(value) is int else None                 # noqa: E721


def _grounding_delivery(run: dict[str, Any]) -> tuple[list[str], str]:
    """What a `grounded` night has to prove BEYOND its own declaration (D2a/D5).

    Returns `(problems, substance)` — the second being the one SENTENCE FRAGMENT
    E10 puts on its PASS line. That is not decoration: `covered_with_content` and
    the per-leg `with_content` counts were both being WRITTEN and read by
    nothing, so a night where the vault contributed nothing scored GROUNDED
    identically to one where it contributed everything, and the only signal was
    a nightly log line nobody has to read (review 2026-08-15).

    IT IS A NUMBER, NOT A VERDICT. `grounded` still means what the owner ruled it
    means — "the vault knows nothing here" IS a grounded answer — so a zero here
    does not fail the night. It just stops being invisible on the artifact an
    operator actually reads.

    Two joins, and neither alone is enough. The DENOMINATOR join says `required`
    was not under-counted — the frozen set must be present in the batch files
    `cos_judge.py --batches` independently rendered. The DELIVERY join says every
    block the map claims actually reached a composed prompt. Without the first, a
    fetcher that under-required scores a clean `grounded`; without the second, a
    map can be produced, declared, and never reach the model — the chunker's
    `--grounding` argument forgotten, a chunk composed before its map existed —
    while every other gate still passes.

    An UNGROUNDED night is not put through this: it makes no claim to join.
    """
    j = run.get("grounding_join")
    if not isinstance(j, dict):
        # Same posture as the missing declaration: a claim with no join is not
        # a claim.
        return (["the night declares GROUNDED and `$EV/grounding-join.json` is "
                 "MISSING — a map that was produced is not a map that arrived"],
                "")
    problems: list[str] = []
    chunks = j.get("chunks") or []
    # CONDITION 1 — the producer's own verdict. `ok` is written by
    # `cos_batch_chunk` and was READ BY NOTHING, so a join could declare itself
    # not-ok and still pass every condition below (review 2026-08-15). It is a
    # cheap, independent second opinion on the same file and it is now required
    # to be exactly `True`.
    if j.get("ok") is not True:
        problems.append(f"the grounding join's own `ok` is {j.get('ok')!r} — "
                        "the producer does not stand behind this delivery")
    # CONDITION 2 — composition. A map that exists and never reached the prompt.
    # ONE predicate, shared with the producer and the nightly's log line.
    bad = short_chunks(j)
    if bad:
        problems.append(f"{len(bad)} chunk(s) did not carry the grounding they "
                        f"were mapped: {bad[:6]}")
    # CONDITION 3 — the union covers the frozen required set. THE DENOMINATOR IS
    # THE DECLARATION'S, not the join's own: with `--grounding` forgotten the
    # chunker never reads a map, so its `required` is 0 and `0 >= 0` would pass
    # the very failure this condition exists for. The declaration is the
    # independently produced artifact, and it is what the union is scored
    # against.
    #
    # AND THE NUMERATOR IS RECOMPUTED FROM THE CHUNK RECORDS, not read off the
    # producer's `required_covered_by_chunks`. That field is the producer's own
    # word about itself, and the review passed a join declaring two covered ids
    # with `chunks: []` — zero problems returned. `covered_ids` per chunk is what
    # the map actually keyed; the union of those is the only honest numerator,
    # and the declared count must AGREE with it.
    g = run.get("grounding") if isinstance(run.get("grounding"), dict) else {}
    required_ids = {str(x) for x in (g.get("required") or [])}
    required = len(required_ids)
    union: set[str] = set()
    with_content: set[str] = set()
    for c in chunks:
        if not isinstance(c, dict):
            continue
        union |= {str(x) for x in (c.get("covered_ids") or [])}
        with_content |= {str(x) for x in (c.get("with_text_ids") or [])}
    uncovered = sorted(required_ids - union)
    if uncovered:
        problems.append(f"the chunks delivered grounding for "
                        f"{len(required_ids) - len(uncovered)} of the "
                        f"{required} required id(s), recomputed from the chunk "
                        f"records: {uncovered[:6]}")
    covered = _exact_int(j.get("required_covered_by_chunks"))
    if covered is None:
        problems.append("the grounding join records no coverage count as an "
                        f"integer ({j.get('required_covered_by_chunks')!r})")
    elif covered < required:
        problems.append(f"the chunks delivered grounding for {covered} of the "
                        f"{required} required id(s)")
    elif covered != len(union):
        problems.append(f"the grounding join DECLARES {covered} covered id(s) "
                        f"and its own chunk records carry {len(union)}")
    # THE DENOMINATOR. `required` must be a subset of the ids the rendered
    # batches carry — D13's guarantee, joined to the batches rather than assumed.
    orphans = j.get("required_not_in_batches") or []
    if orphans:
        problems.append(f"{len(orphans)} required id(s) are in no rendered "
                        f"batch: {orphans[:6]}")
    # THE PER-LEG DERIVATION. `required` is the UNSUBTRACTED union of the four
    # legs, so on a grounded night every leg's ungrounded count is zero BY
    # CONSTRUCTION. A non-zero one means the judged night and the declaration
    # disagree about the population, which is precisely what a state word hides.
    judgment = run.get("judgment") if isinstance(run.get("judgment"), dict) else {}
    legs = (((judgment or {}).get("run_facts") or {}).get("grounding")
            or {}).get("legs") or {}
    shortlegs = {leg: v.get("ungrounded") for leg, v in legs.items()
                 if isinstance(v, dict) and v.get("ungrounded")}
    if shortlegs:
        problems.append(f"the judged night reports ungrounded rows on a "
                        f"GROUNDED run: {shortlegs}")
    # THE SUBSTANCE SENTENCE. `with_content` per leg is what the judged night
    # recorded; `used_block_vocab_lower_bound` is how many of those rows' free
    # text carried a distinctive phrase from their block
    # (`cos_judge.grounding_facts`).
    #
    # IT IS RENDERED AS THE LOWER BOUND IT IS (review 2026-08-15). The
    # implementation says plainly "a LOWER BOUND on use, not a measure of it";
    # this line rendered a bare `(used N)`, and a bare number at the report
    # boundary reads as measured usage. THREE directions of error, all of them
    # downward: a leg that used a block and paraphrased it completely scores 0;
    # a leg that echoes one phrase without reasoning scores 1; and — the one the
    # docstring omitted — the PROJECTION REFUSES any row sharing a five-token run
    # with its block, so the most strongly grounded rows never reach this counter
    # at all. The two mechanisms read the same shingle space at widths 2 and 5
    # with opposite consequences, so `refused_grounding_overlap` belongs on this
    # same sentence: without it, a night that refused every quoting row is
    # indistinguishable from a night that ignored the vault.
    per_leg = ", ".join(
        f"{leg} {v.get('with_content', 0)}/{v.get('rows', 0)}"
        f" (used at least {v.get('used_block_vocab_lower_bound', 0)})"
        for leg, v in sorted(legs.items()) if isinstance(v, dict))
    projection = (((run.get("judgment") or {}).get("run_facts") or {})
                  .get("grounding") or {}).get("projection") or {}
    refused = projection.get("refused_grounding_overlap")
    n_refused = (sum(refused.values()) if isinstance(refused, dict)
                 else (refused or 0))
    substance = (f"{len(with_content)} of {len(union)} delivered id(s) carried "
                 f"vault content" + (f"; per leg with_content: {per_leg}"
                                     if per_leg else "")
                 + f"; {n_refused} row(s) refused for reproducing their block")
    return problems, substance


def _e10(run: dict[str, Any]) -> dict[str, Any]:
    of = "frozen capability digest (present on every run)"
    frozen = (run["manifest"] or {}).get("capability_digest")
    now = capability_digest()
    g = run["grounding"] if isinstance(run["grounding"], dict) else None
    problems = []
    substance = ""
    if not frozen:
        problems.append("the run manifest froze no capability digest")
    elif now is None:
        problems.append("the executing tree is not on disk to re-hash")
    elif now != frozen:
        problems.append(f"the capability set CHANGED ({frozen[:12]}… → "
                        f"{now[:12]}…)")
    if not isinstance(run["sent_baseline"], dict):
        problems.append("the sent baseline is missing")
    if [r for r in run["undo"]
            if (r.get("receipts") or {}).get("send_attempted") is True]:
        problems.append("a send was attempted")
    if dispatched(run["undo"]) and not isinstance(run["binding"], dict):
        problems.append("mutations were dispatched under no plan binding")
    if g is None:
        problems.append("`_cos_grounding_<run>.json` is MISSING — an ungrounded "
                        "night is a thing the run SAYS, never a thing an absent "
                        "file implies")
    elif g.get("state") == "ungrounded":
        if not str(g.get("reason") or "").strip():
            problems.append("the night declares UNGROUNDED with no reason")
    elif g.get("state") == "grounded":
        missing = sorted(set(g.get("required") or []) - set(g.get("covered") or []))
        if missing:
            problems.append(f"{len(missing)} required grounding id(s) are "
                            f"uncovered: {missing[:6]}")
        delivery, substance = _grounding_delivery(run)
        problems.extend(delivery)
    else:
        problems.append(f"grounding state {g.get('state')!r} is undeclared")
    if problems:
        return _answer(10, FAIL, 1, of, "; ".join(problems))
    return _answer(10, PASS, 1, of,
                   f"the capability set is byte-identical to the digest the "
                   f"manifest froze ({str(frozen)[:12]}…), no send was "
                   f"attempted, the plan binding is intact, and the run "
                   f"declares grounding `{g.get('state')}`"
                   + (f" ({g.get('reason')})" if g.get("state") == "ungrounded"
                      else "")
                   # THE SUBSTANCE NUMBER IS ON THE PASS LINE, so a night the
                   # vault contributed nothing to no longer reads identically to
                   # one it carried. It is reported, never gated.
                   + (f" — {substance}" if substance else ""))


#: id -> the function that answers it. TEN, CONTIGUOUS, and asserted against
#: the manifest's frozen count before anything is written.
CHECKS: dict[int, Callable[..., dict[str, Any]]] = {
    1: lambda run, vault, rid: _e1(run),
    2: lambda run, vault, rid: _e2(run),
    3: lambda run, vault, rid: _e3(run, vault, rid),
    4: lambda run, vault, rid: _e4(run, vault, rid),
    5: lambda run, vault, rid: _e5(run),
    6: lambda run, vault, rid: _e6(run),
    7: lambda run, vault, rid: _e7(run),
    8: lambda run, vault, rid: _e8(run),
    9: lambda run, vault, rid: _e9(run),
    10: lambda run, vault, rid: _e10(run),
}


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


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
