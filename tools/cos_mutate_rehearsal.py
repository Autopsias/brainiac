"""The rehearsal lane of `cos_mutate` — evidence problems, the gate, seed probes, the frozen todo

Moved verbatim out of `cos_mutate` (batch-2 drain); every name is re-imported
by the parent so its `cos_mutate` module path is unchanged.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_mutate_gates import MutationStop, short  # noqa: E402
from cos_mutate_plan import load_frozen_plan, plan_digest  # noqa: E402
from cos_reconcile_metrics import MUTATION_VERBS  # noqa: E402


def _rehearsal_key(verb: Any, conversation_id: Any) -> tuple[str, str]:
    cid = str(conversation_id or "")
    return (str(verb or ""), cid if _DIGEST_RE.match(cid) else short(cid))


#: What `dryOne` writes on the ONE path that can reach `would_dispatch: true`
#: (`tools/cos_mutate_page.js`, `if (!p.body) return out;` … `out.would_dispatch
#: = !reason`), as `(key, predicate, what it proves)`. Read off the producer,
#: and pinned against it by `test_the_dispatchable_row_fixture_matches_dryOne`,
#: which RUNS the shipped page module and compares its real row against this.
_DISPATCH_EVIDENCE: tuple[tuple[str, Any, str], ...] = (
    ("action", lambda v: isinstance(v, str) and v.strip(),
     "the request `prepare()` built"),
    ("allowlist", lambda v: v == "ACCEPTED",
     "the allowlist's ruling, which is ACCEPTED exactly when would_dispatch is "
     "true"),
    ("fingerprint", lambda v: isinstance(v, str) and v.strip(),
     "the fingerprint of the payload that was built"),
    ("payload_without_header", lambda v: isinstance(v, dict) and v,
     "the payload itself, minus the bearer envelope"),
)


def _dispatch_evidence_problems(i: int, r: dict[str, Any]) -> list[str]:
    """A row that CLAIMS it would dispatch, without the evidence dry validation
    ran (review 2026-08-13, round 4, Codex HIGH).

    `{"verb": "archive", "conversation_id": "c1", "would_dispatch": true,
    "dispatched": false}` matched a plan one-to-one and certified the night —
    no payload was built, no allowlist ruled, nothing was fingerprinted. It was
    probed `ok: true`. `would_dispatch` is only REACHABLE in the producer after
    all three, so a row claiming it without them did not come from the producer.
    """
    out: list[str] = []
    for key, ok, proves in _DISPATCH_EVIDENCE:
        if key not in r:
            out.append(
                f"rehearsal row {i} claims would_dispatch=True but carries no "
                f"{key!r} — {proves}. `dryOne` writes it on the same path that "
                "sets would_dispatch, so a row without it never ran the dry "
                "validation it is claiming the verdict of")
        elif not ok(r[key]):
            out.append(
                f"rehearsal row {i} claims would_dispatch=True and carries "
                f"{key}={r[key]!r}, which is not {proves}")
    return out


def _row_problems(rows: list[dict[str, Any]], *, kind: str) -> list[str]:
    """Every row that is not a row this gate may reason about, named.

    THE MATCH USED TO BE THE ONLY CHECK (review 2026-08-13, round 3, Codex
    HIGH). `sum(1 for r in dry_rows if r.get("would_dispatch"))` is a TRUTHINESS
    test, so a row carrying the STRING `"false"` counted as dispatchable; and a
    plan row of `{}` normalized to the key `("", "")` and matched a rehearsal
    row that was equally empty, so two pieces of garbage certified each other.
    Both were probed `ok: true`.

    So the shape is decided BEFORE the match, and a malformed row is neither a
    quiet night nor a blocked lane — it is an artifact this gate cannot read,
    which is the `rc 2` path.

    `would_dispatch` may be ABSENT, and that is production, not laxity:
    `dryOne` omits it on the skip path (`if (!p.body) return out;`) and only
    ever writes a real boolean otherwise, while `dry_run`'s `except
    MutationStop` branch writes `False` explicitly. Absent means "this row
    would not dispatch". An explicit `null` is NOT that absence (round 4): a
    key the producer wrote and left empty is a producer that answered nothing,
    which is malformed. A STRING there is neither, and is refused.

    Keys are checked by VALUE, not by whitelist: the page adds fields to a dry
    row as it learns to report more (`fingerprint`, `allowlist`,
    `payload_without_header`), and a key whitelist would turn every such
    addition into a stopped night for no safety gain.

    BUT A CLAIM OF `would_dispatch: True` CARRIES ITS EVIDENCE (review
    2026-08-13, round 4, Codex HIGH). A four-field row —
    `{verb, conversation_id, would_dispatch: true, dispatched: false}` — passed
    every check above and certified the night, while lacking every artifact
    that proves dry validation actually ran. It was probed `ok: true`. `dryOne`
    reaches `would_dispatch` only after `prepare()` returned a body and
    `validate()` ruled on it, and it writes the ruling out on the same three
    lines: `allowlist` (`"ACCEPTED"` exactly when `would_dispatch` is true),
    `fingerprint` (of the built payload) and `payload_without_header` (the
    payload itself, minus the bearer envelope). A row claiming the verdict
    without them did not come from that path. This is CONDITIONAL — nothing is
    required of a row that does not claim it would dispatch — so the skip path,
    the `MutationStop` path and every rejected row are untouched.
    """
    problems: list[str] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            problems.append(f"{kind} row {i} is a {type(r).__name__}, not an object")
            continue
        verb = r.get("verb")
        if not isinstance(verb, str) or verb not in MUTATION_VERBS:
            problems.append(
                f"{kind} row {i} carries verb {verb!r}, which is not one of "
                f"{list(MUTATION_VERBS)} — a row naming no verb matches "
                "whatever else names none")
        cid = r.get("conversation_id")
        if not isinstance(cid, str) or not cid.strip():
            problems.append(
                f"{kind} row {i} carries conversation_id {cid!r} — a row "
                "naming no thread is not a row about any thread")
        if kind != "rehearsal":
            continue
        has_wd = "would_dispatch" in r
        wd = r.get("would_dispatch")
        if has_wd and not (wd is True or wd is False):
            problems.append(
                f"rehearsal row {i} carries would_dispatch {wd!r} "
                f"({type(wd).__name__}), which is not a boolean — the string "
                '"false" is TRUTHY and used to count as dispatchable; an '
                "explicit null is a producer that wrote the key and answered "
                "nothing, which is not the ABSENCE `dryOne` leaves on its skip "
                "path")
        elif wd is True:
            problems += _dispatch_evidence_problems(i, r)
        if r.get("dispatched") is not False:
            problems.append(
                f"rehearsal row {i} reports dispatched={r.get('dispatched')!r}; "
                "a rehearsal dispatches nothing and both production paths say "
                "so with an explicit False")
    return problems


def rehearsal_verdict(plan_mutations: list[dict[str, Any]],
                      dry_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Did this rehearsal rehearse THIS plan, and can anything act?

    THE GATE USED TO FAIL OPEN (review 2026-08-13, round 2). The nightly read
    `dry-run.json` inside a command substitution under `set -u` only, so a
    parser failure did not stop the run and a MISSING `dry` key became an empty
    plan: rename the production key, or truncate the file, and `0 of N` read as
    `0 of 0` — a quiet night — and the run walked into REAL MUTATIONS with no
    valid rehearsal behind it. The test that blessed it fed `{"dry": []}` and
    asserted `reached the apply`, which is the same empty-input all-clear.

    So the plan and the rehearsal are matched ONE-TO-ONE on
    `(conversation_id, verb)` BEFORE `0 of 0` is told apart from `0 of N`. A
    rehearsal that does not cover the plan is not a quiet night and is not a
    blocked lane — it is a rehearsal that did not happen, and the only safe
    reading of one is to dispatch nothing.

    And BEFORE either, both sides are shape-checked (`_row_problems`): a row
    with no valid verb, no conversation id, a non-boolean `would_dispatch` or a
    `dispatched` that is not `False` is an artifact this gate cannot read.

    Returns `{"ok", "blocked", "reason", …}`: `ok` false with `blocked` false
    means the rehearsal could not be matched at all.
    """
    base0 = {"planned": len(plan_mutations), "rehearsed": len(dry_rows),
             "missing": [], "extra": []}
    problems = (_row_problems(plan_mutations, kind="plan")
                + _row_problems(dry_rows, kind="rehearsal"))
    if problems:
        return dict(base0, ok=False, blocked=False, problems=problems[:8],
                    reason=("the plan or the rehearsal carries "
                            f"{len(problems)} malformed row field(s), so the "
                            "two cannot be matched at all: "
                            + "; ".join(problems[:3])
                            + ". A row this gate cannot read is not a row it "
                              "may certify, and nothing is dispatched"))

    want: dict[tuple[str, str], int] = {}
    for m in plan_mutations:
        k = _rehearsal_key(m.get("verb"), m.get("conversation_id"))
        want[k] = want.get(k, 0) + 1
    got: dict[tuple[str, str], int] = {}
    for r in dry_rows:
        k = _rehearsal_key(r.get("verb"), r.get("conversation_id"))
        got[k] = got.get(k, 0) + 1

    missing: list[str] = []
    for (verb, cid), n in sorted(want.items()):
        missing += [f"{verb} {cid}"] * max(0, n - got.get((verb, cid), 0))
    extra: list[str] = []
    for (verb, cid), n in sorted(got.items()):
        extra += [f"{verb} {cid}"] * max(0, n - want.get((verb, cid), 0))
    base = {"planned": len(plan_mutations), "rehearsed": len(dry_rows),
            "missing": missing[:8], "extra": extra[:8]}
    if missing or extra:
        return dict(base, ok=False, blocked=False, reason=(
            f"the rehearsal does not cover the plan: {len(plan_mutations)} "
            f"planned mutation(s), {len(dry_rows)} rehearsed, "
            f"{len(missing)} planned row(s) never rehearsed"
            + (f" (e.g. {missing[0]})" if missing else "")
            + (f" and {len(extra)} rehearsed row(s) not in the plan" if extra else "")
            + ". A rehearsal that did not rehearse this plan proves nothing "
              "about it, so nothing is dispatched"))

    # IDENTITY, NOT TRUTHINESS. `_row_problems` has already refused anything
    # that is not a boolean or absent; this keeps the count honest even so.
    ok_rows = sum(1 for r in dry_rows if r.get("would_dispatch") is True)
    if dry_rows and not ok_rows:
        reasons: dict[str, int] = {}
        for r in dry_rows:
            why = str(r.get("blocked") or r.get("reason") or "no reason recorded")
            reasons[why] = reasons.get(why, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        return dict(base, ok=False, blocked=True, would_dispatch=0, reason=(
            "0 of %d planned mutation(s) would dispatch. %s" % (
                len(dry_rows), "; ".join(
                    "%d x %s" % (n, why.replace("\n", " ")[:220])
                    for why, n in top))))
    return dict(base, ok=True, blocked=False, would_dispatch=ok_rows, reason=(
        f"{ok_rows} of {len(dry_rows)} rehearsed mutation(s) would dispatch, "
        f"and every planned row was rehearsed"))


def rehearsal_gate(plan_path: Path, dry_path: Path) -> tuple[int, str]:
    """The nightly's gate as ONE command with an exit status.

    0 proceed · 15 the lane cannot act · 2 the rehearsal could not be validated.
    Both non-zero paths dispatch nothing; they differ only in the morning they
    describe.
    """
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        dry = json.loads(dry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 2, (f"the plan or the rehearsal could not be read ({exc}) — "
                   "an unreadable rehearsal is not an empty one")
    for name, doc, key in (("plan", plan, "mutations"), ("rehearsal", dry, "dry")):
        if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
            return 2, (f"the {name} carries no `{key}` list — the key it is read "
                       "by is part of the contract, and a missing one used to "
                       "read as an empty plan")
    # IDENTITY BEFORE COVERAGE (K1). The row-by-row match below can only see
    # `(verb, conversation_id)`, so it certifies a rehearsal of a DIFFERENT
    # plan whenever the two happen to name the same threads and verbs — Codex
    # probed a P1/add plan against a P3/remove rehearsal `ok: true`. The digest
    # is over the whole payload, including the draft text a human may send
    # verbatim, so it is checked FIRST and the match becomes the second belt.
    stamped = plan.get("plan_digest")
    if not isinstance(stamped, str) or not stamped.strip():
        return 2, ("the plan carries no `plan_digest`, so the rehearsal cannot "
                   "be bound to it and a rehearsal of some other plan would "
                   "certify this one")
    actual = plan_digest(plan["mutations"], plan.get("run_id"))
    if actual != stamped:
        return 2, (f"the plan does not hash to its own stamp (stamped "
                   f"{stamped[:16]}…, computed {actual[:16]}…) — its payload "
                   "changed after it was written, so nothing rehearsed it")
    if dry.get("plan_digest") != stamped:
        return 2, (f"the rehearsal names plan digest "
                   f"{str(dry.get('plan_digest'))[:16]}… and this plan is "
                   f"{stamped[:16]}…. It rehearsed a DIFFERENT plan, which "
                   "proves nothing about this one however well the rows match")
    v = rehearsal_verdict(plan["mutations"], dry["dry"])
    return (0 if v["ok"] else 15 if v["blocked"] else 2), v["reason"]


def _seed_probe(bridge: Any, since: str) -> dict[str, Any]:
    """Was a FRESHER authenticated envelope captured after `since`? MEASURED.

    The 401 stop line asserted "no fresher envelope had been captured by the
    time it failed" while nothing in the build read the capture buffer at 401
    time (review 2026-08-13, round 4). This asks the page, whose
    `cap.freshestSeed` is the same query `init` uses to pick its seed.

    GUARDED, like the reconcile beside it: the transport failure that produced
    the 401 is exactly what would kill this call too, and an unguarded raise
    here would erase the report of a whole night's applied mutations. A probe
    that could not run says so — `measured: False` — and the sentence below
    prints that rather than an absence it never observed.
    """
    try:
        out = bridge.call("seed_probe", {"since": since})["out"] or {}
    except Exception as exc:                                     # noqa: BLE001
        return {"measured": False, "fresher_seed": None,
                "why": f"the seed probe could not run: {str(exc)[:160]}"}
    if not isinstance(out, dict) or "fresher_seed" not in out:
        return {"measured": False, "fresher_seed": None,
                "why": f"the seed probe answered {str(out)[:120]!r}"}
    return {"measured": bool(out.get("measured")),
            "fresher_seed": out.get("fresher_seed"),
            "fresher_seed_at": out.get("fresher_seed_at"),
            "captured_finditem": out.get("captured_finditem"),
            "why": out.get("why")}


def _seed_probe_sentence(seed: dict[str, Any] | None) -> str:
    """What the probe MEASURED, in the operator's own line."""
    if not seed or not seed.get("measured"):
        why = (seed or {}).get("why") or "the probe did not run"
        return (f"Whether a fresher envelope was captured is UNMEASURED here "
                f"({why}).")
    if seed.get("fresher_seed"):
        return ("A fresher authenticated FindItem WAS captured after this pass "
                f"began (at {seed.get('fresher_seed_at')}), so re-seeding from "
                "the buffer would very likely recover — but this lane does not "
                "do that on its own.")
    return ("Measured at failure time: NO fresher authenticated FindItem had "
            f"been captured since this pass began "
            f"({seed.get('captured_finditem')} captured in total).")


def _frozen_todo(vault: Path, run_id: str, plan_path: Path,
                 rehearsal_path: Path | None,
                 done_keys: set[str]) -> dict[str, Any]:
    """The frozen plan, PROVEN to be the one the rehearsal validated (K1).

    Five refusals, and every one of them dispatches nothing:

    * the plan does not hash to its own stamp (`load_frozen_plan`) — which
      since C1 includes a plan whose `run_id` was deleted,
    * the plan names another run,
    * no rehearsal was named — an apply against a frozen plan with no proof it
      was ever rehearsed is the unrehearsed apply wearing the artifact's
      clothes,
    * the rehearsal names a DIFFERENT digest — it rehearsed some other plan,
      which is precisely the P1/add-against-P3/remove case Codex probed
      `ok: true`,
    * the rehearsal REHEARSED NOTHING (review 2026-08-13, round 5, C3).

    THE LAST ONE WAS A DOCSTRING, NOT A CHECK. This function claimed to refuse
    "an apply with no proof it was ever rehearsed" while comparing one digest
    STRING, and both review lanes walked straight through it: a rehearsal of
    `dry: []`, and one whose every row said `would_dispatch: false`, were both
    ACCEPTED for a plan with real rows. The shell gate (`rehearsal-gate`, exit
    15) caught them, so nothing shipped broken — but the shell is a different
    process from the thing that dispatches, and a guarantee stated by the
    dispatcher has to be enforced by the dispatcher. `rehearsal_verdict` is the
    SAME function the shell gate calls, imported here rather than restated:
    one definition of "did this rehearsal cover this plan", two callers.

    `done_keys` still bites afterwards: resume-never-restart is unchanged, and
    a row this checkpoint already carried out is dropped from the todo without
    changing the plan's identity — the digest is over what was PLANNED, never
    over what is left to do.
    """
    plan = load_frozen_plan(plan_path)
    # EXACT, AND NEVER NULL (review 2026-08-13, round 5, C1). This read
    # `not in (None, run_id)`, so a plan whose `run_id` had been DELETED was
    # accepted by whichever run happened to read it — which is the whole replay:
    # an old run's rows against a new run's empty ledger, every one of them
    # re-dispatched, and the lane lock (keyed on the run id) never even
    # contended. The digest now covers the key too, so this is the second belt;
    # it is written as an exact match because a guard that accepts an absence is
    # a guard with a hole in the shape of an absence.
    if plan.get("run_id") != run_id:
        raise MutationStop(
            f"the frozen plan at {plan_path} was built for run "
            f"{plan.get('run_id')!r}, not {run_id!r} — a plan applied to "
            "another run's ledger is a plan about other threads")
    if rehearsal_path is None:
        raise MutationStop(
            "an apply against a frozen plan must name the rehearsal that "
            "validated it (--rehearsal). A plan with no rehearsal behind it is "
            "the unrehearsed apply this artifact exists to prevent")
    try:
        reh = json.loads(Path(rehearsal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} could not be read ({exc}) — "
            "an unreadable rehearsal is not a passed one") from exc
    if not isinstance(reh, dict) or reh.get("plan_digest") != plan["plan_digest"]:
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} names plan digest "
            f"{(reh or {}).get('plan_digest')!r} and this plan is "
            f"{plan['plan_digest']!r}. The rehearsal validated a DIFFERENT "
            "plan, so it proves nothing about this one — a P1/add plan behind "
            "a P3/remove rehearsal is the exact shape this refuses, and it "
            "used to return ok")
    # THE REHEARSAL HAS TO HAVE REHEARSED SOMETHING (C3). Same predicate as the
    # shell gate, same reasons, and the `dry` key is read defensively for the
    # same reason `mutations` is: a missing list used to read as an empty plan.
    dry_rows = reh.get("dry")
    if not isinstance(dry_rows, list):
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} carries no `dry` list — the key "
            "it is read by is part of the contract, and a rehearsal with no "
            "rehearsed rows in it is not a rehearsal this apply may act on")
    verdict = rehearsal_verdict(plan["mutations"], dry_rows)
    if not verdict["ok"]:
        raise MutationStop(
            f"the rehearsal at {rehearsal_path} names this plan's digest but "
            f"did not pass its own gate: {verdict['reason']}. A matching digest "
            "string is INTEGRITY — it says the two files describe one plan — "
            "and this apply also needs the rehearsal to have actually run")
    todo = [m for m in plan["mutations"]
            if f"{m.get('conversation_id')}|{m.get('verb')}" not in done_keys]
    return {"plan": plan, "todo": todo}
