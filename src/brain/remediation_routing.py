"""REG-03 — the routing step: what the nightly DOES with each finding.

:mod:`brain.remediation` decides what SHOULD happen to an alert key; this is
the half that makes the decision real. Every degradation finding the notify
fold produced is routed to exactly one of four places before it can reach the
session-start banner:

* ``auto`` whose owning branch is LIVE and CONVERGING — suppressed to a
  ``hot.md`` line with no notification marker, because a finding the engine is
  actively fixing is a log entry, not an alarm;
* ``owner`` — ONE decidable question (options + a stated default) through the
  existing capped inbox path, never re-asked once the owner has ANSWERED it.
  Which question, and whether the family is asked per-pair through the
  cross-tier proposal batch or as a single question about the whole
  population, is :mod:`brain.remediation_questions` (EXC-01) — one router,
  reading the finding key and nothing else, so a finding can never occupy
  both surfaces;
* ``log`` — a ``hot.md`` line and nothing else;
* ``banner`` / anything undeclared — straight through, an undeclared key
  wrapped ``untriaged:<key>`` so the FEED itself records that nothing had
  decided what to do with it.

Two rules this module may not break:

1. **The banner class never consults the registry at all.**
   ``remediation.is_unsuppressible`` is checked FIRST, so a hostile or merely
   wrong table cannot route a dead-man's-switch key into something that could
   suppress it. s01 made the TABLE refuse to hold such a row; this is the
   pipeline half — it never asks.
2. **Suppression binds to the FINDING, not just to the branch.** A branch run
   records the detector generation and a hash of the target set it processed;
   a finding is suppressed only when that report covers the CURRENT detector
   generation's target set for that key. Any mismatch — new targets, an
   unknown generation, no signature at all — banners and counts as
   non-convergence.

Where the evidence comes from, and why it is host-private, is
:mod:`brain.remediation_state`.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from . import inbox as _inbox
from . import remediation as _rem
from . import remediation_answers as _ranswers
from . import remediation_exceptions as _rex
from . import remediation_questions as _rq
from . import remediation_state as _rs
from .remediation_state import ESCALATED, NEVER_RUN, THRASH_NIGHTS

# Re-exported: the question TABLES moved to `remediation_questions` (EXC-01)
# and the cap moved with them, because the batch layer asks under the same
# ceiling. Names kept here so every caller keeps one import.
from .remediation_questions import (  # noqa: F401
    DEFAULT_OWNER_CAP, OWNER_CAP_ENV, QUESTION_SHAPE_PREFIXES, QUESTION_SHAPES,
    owner_cap, question_shape,
)

#: A standalone number in a finding's text is its POPULATION SIZE, and the
#: subject must be stable under it: "3 dropped file(s) … (+2 more)" minted a
#: new question key every time the count moved, so answered questions came
#: back as fresh singleton variants that never expire and silted the 5-slot
#: queue (llm-review, s06). Digits INSIDE a token — a dated filename, an id —
#: are identity, so the lookarounds exclude a neighbouring `\w`, `.` or `-`.
_COUNT_RE = re.compile(r"(?<![\w.-])\d+(?![\w.-])")

HOT_KEY_PREFIX = "maintain:remediation:"
QUESTION_KEY_PREFIX = "remediation:"
THRASH_KEY_PREFIX = "remediation-thrash:"


def target_signatures(results: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    """Collect ``{finding key: {generation, hash}}`` from this run's fold results.

    The SEAM a remediation branch's detector fills: any fold result may carry a
    ``target_signatures`` mapping, and the routing layer reads them all. Nothing
    produces one yet — the branches arrive in later sessions — so today this is
    empty, every ``auto`` finding fails the binding check, and the banner is
    exactly what it was before this module existed. That is the intended
    upgrade-day behaviour, not an oversight."""
    out: dict[str, dict[str, str]] = {}
    for value in (results or {}).values():
        sigs = value.get("target_signatures") if isinstance(value, dict) else None
        if not isinstance(sigs, dict):
            continue
        for key, sig in sigs.items():
            if isinstance(sig, dict) and sig.get("generation") and sig.get("hash"):
                out[str(key)] = {"generation": str(sig["generation"]),
                                 "hash": str(sig["hash"])}
    return out


# ---------------------------------------------------------------------------
# Owner questions. WHICH question a key gets — the options, the default, and
# whether it is asked per-pair or as a singleton — is
# :mod:`brain.remediation_questions`; this half only builds the record and
# hands it to the queue.
# ---------------------------------------------------------------------------
def owner_question(
    key: str, text: str, signature: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """ONE decidable question for an owner-class finding.

    Its idempotency key carries the SUBJECT as well as the finding key — the
    current target-set hash when one exists — so a decided question is never
    re-asked while a genuinely different set of targets still gets asked
    about.

    A key nothing repairs publishes no target signature, and used to get a
    BARE key: the first answer entered the ledger and every later occurrence
    of that finding — different files, different documents — was skipped as
    already-decided. So when no signature exists the subject falls back to the
    finding TEXT, which names the population the detector found. Same rule for
    every such key, not a per-key patch."""
    options, default, _source = question_shape(key)
    detail = str((signature or {}).get("detail") or "")
    if detail:
        text = f"{text} — {detail}"
    subject = (str((signature or {}).get("hash") or "")
               or _rq.digest([key, _countless(text)]))[:12]
    qkey = QUESTION_KEY_PREFIX + key + f":{subject}"
    try:
        remedy = _rem.resolve(key)
    except _rem.RegistryError:      # an ambiguous key is drift, not a blocker
        remedy = None
    return {
        # Carried from the registry so `_ask_owner_questions` can tell a
        # decided-and-FINISHED question from a decided-but-STILL-BLOCKING one.
        "keeps_blocking": bool(remedy is not None and remedy.keeps_blocking),
        "key": qkey,
        "question": f"{text} — what should happen?",
        "options": list(options),
        "default": default,
        "context": (f"Remediation finding `{key}`. The default applies if this "
                    "is never answered; an answer is executed through the "
                    "audited write path by the remediation lane that owns it. "
                    "This question does not expire — it stays open until "
                    "answered, and until then the default is what holds."),
        "source": f"remediation:{key}",
        "finding": (key, text),
    }


def _countless(text: str) -> str:
    """``text`` with its standalone counts replaced — see :data:`_COUNT_RE`."""
    return _COUNT_RE.sub("#", text)


def thrashing_question(branch: str, target: str) -> dict[str, Any]:
    return {
        "key": THRASH_KEY_PREFIX + f"{branch}:{target}",
        "question": (f"`{branch}` has healed `{target}` on {THRASH_NIGHTS} "
                     "consecutive nights — a fix that does not stick is a "
                     "symptom, not a repair. What should happen?"),
        "options": ["investigate why the repair does not hold",
                    "keep letting the branch heal it nightly"],
        "default": "keep letting the branch heal it nightly",
        "context": (f"Symptom-remediation thrashing on branch `{branch}`. "
                    "Suppression for this branch's findings is off until this "
                    "is decided."),
        "source": f"remediation-thrash:{branch}",
        "finding": (f"branch-escalate:{branch}",
                    f"{branch} re-healed {target} on {THRASH_NIGHTS} "
                    "consecutive nights (symptom-remediation thrashing)"),
    }


# ---------------------------------------------------------------------------
# The routing decision. Pure — the caller does the I/O.
# ---------------------------------------------------------------------------
def route(
    findings: list[tuple[str, str]], state: Mapping[str, Any],
    today: datetime.date, *, targets: Mapping[str, Any] | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Split ``findings`` into what banners, what is logged, what is suppressed,
    and what becomes an owner question.

    Deduped on the stable KEY exactly as the marker layer is, so a finding can
    never be both suppressed here and re-raised by the trend machinery in the
    same run."""
    out: dict[str, Any] = {"banner": [], "log": [], "suppressed": [], "owner": [],
                           "batch": [], "non_convergence": {}, "bound": {},
                           "reasons": {}}
    seen: set[str] = set()
    for key, text in findings:
        if key in seen:
            continue
        seen.add(key)
        _route_one(key, text, state, today, targets or {}, out, seen,
                   subjects or {})
    return out


def _route_one(
    key: str, text: str, state: Mapping[str, Any], today: datetime.date,
    targets: Mapping[str, Any], out: dict[str, Any], seen: set[str],
    subjects: Mapping[str, Any],
) -> None:
    # Rule 1: the banner class is answered BEFORE the table is consulted. A
    # liveness/dead-man key must survive a hostile registry, so the pipeline
    # never asks one about it.
    if _rem.is_unsuppressible(key):
        out["banner"].append((key, text))
        out["reasons"][key] = "unsuppressible"
        return
    try:
        remedy = _rem.resolve(key)
    except _rem.RegistryError:
        remedy = None  # an ambiguous key is drift, same as an undeclared one
    if remedy is None:
        out["banner"].append((_rem.UNTRIAGED_PREFIX + key, text))
        out["reasons"][key] = "untriaged"
        return
    if remedy.disposition == _rem.LOG:
        out["log"].append((key, text))
        out["reasons"][key] = "log"
        return
    if remedy.disposition == _rem.OWNER:
        # EXC-01's one router, and it reads the KEY alone. A per-pair family
        # never also becomes an aggregate question, which is how the same
        # finding is kept out of two surfaces at once.
        if _rq.is_batch_key(key):
            out["batch"].append((key, text))
            out["reasons"][key] = "owner-batch"
            return
        out["owner"].append(owner_question(
            key, text, targets.get(key) or subjects.get(key)))
        out["reasons"][key] = "owner"
        return
    if remedy.disposition == _rem.AUTO:
        _route_auto(key, text, remedy, state, today, targets, out, seen)
        return
    out["banner"].append((key, text))
    out["reasons"][key] = "banner"


def _route_auto(
    key: str, text: str, remedy: Any, state: Mapping[str, Any],
    today: datetime.date, targets: Mapping[str, Any], out: dict[str, Any],
    seen: set[str],
) -> None:
    branch = str(remedy.owning_branch)
    status, reasons = _rs.branch_liveness(
        state, branch, int(remedy.escalate_after or 1), today)
    if status == NEVER_RUN:
        # Upgrade day: no state row means this branch has never run, so it
        # neither suppresses nor escalates. Identical to the behaviour before
        # remediation existed.
        out["banner"].append((key, text))
        out["reasons"][key] = "branch-never-run"
        return
    if status == ESCALATED:
        out["banner"].append((key, text))
        out["reasons"][key] = "branch-escalated"
        _add_branch_escalate(branch, reasons, out, seen)
        return
    thrash = _rs.thrashing_targets(state, branch)
    if thrash:
        out["owner"].append(thrashing_question(branch, thrash[0]))
        out["banner"].append((key, text))
        out["reasons"][key] = "thrashing"
        return
    coverage = _rs.coverage(state, branch, key)
    if not _rs.binding_matches(coverage, targets.get(key)):
        # Rule 3: stale or absent coverage may not suppress a finding about
        # targets the branch has not been shown.
        out["banner"].append((key, text))
        out["reasons"][key] = "binding-mismatch"
        out["non_convergence"].setdefault(branch, []).append(key)
        return
    if not _rs.converged(coverage or {}):
        out["banner"].append((key, text))
        out["reasons"][key] = "not-converging"
        out["non_convergence"].setdefault(branch, []).append(key)
        return
    out["suppressed"].append((key, text, branch))
    out["reasons"][key] = "suppressed"
    out["bound"].setdefault(branch, []).append(key)


def _add_branch_escalate(
    branch: str, reasons: list[str], out: dict[str, Any], seen: set[str],
) -> None:
    key = f"branch-escalate:{branch}"
    if key in seen:
        return  # already reported by maintain_escalation this run
    seen.add(key)
    detail = "; ".join(reasons) or "not converging"
    out["banner"].append(
        (key, f"remediation branch '{branch}' escalated: {detail}"))


# ---------------------------------------------------------------------------
# Applying the routing — the only I/O.
# ---------------------------------------------------------------------------
def render_hot_entry(
    suppressed: list[tuple[str, str, str]], log_only: list[tuple[str, str]],
    today: datetime.date,
) -> str:
    """ONE dated ``hot.md`` block per run — a log, never a queue item."""
    lines = [f"## {today.isoformat()} — Remediation routing: "
             f"{len(suppressed)} suppressed, {len(log_only)} logged"]
    for key, text, branch in suppressed:
        lines.append(f"- **suppressed** `{key}` — `{branch}` is live and "
                     f"converging on this run's targets: {text}")
    for key, text in log_only:
        lines.append(f"- **log** `{key}`: {text}")
    lines.append("- **Next:** nothing. A suppressed finding banners again the "
                 "moment its branch stops converging, dies, or is shown targets "
                 "it has not processed.")
    return "\n".join(lines) + "\n"


def apply_routing(
    core: Any, routing: Mapping[str, Any], today: datetime.date, *,
    pairs: Mapping[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """Write the hot.md line, ask the owner questions, persist the counters.

    Returns the ``(key, text)`` findings that could NOT become owner questions
    (the queue was already at its cap, the enqueue failed, or a per-pair family
    had no pairs to convert): they go back to the banner rather than
    disappearing. Loud beats tidy.

    Singletons are asked FIRST and the batch fills what is left of the cap:
    a singleton is one finding's whole surface, while an unasked pair is still
    staged, still on the exceptions page and asked as soon as a slot frees."""
    vault = Path(core.vault)
    if routing["suppressed"] or routing["log"]:
        core._append_hot_once(
            f"{HOT_KEY_PREFIX}{today.isoformat()}",
            render_hot_entry(routing["suppressed"], routing["log"], today))
    deferred = _ask_owner_questions(core, vault, routing["owner"], today)
    deferred += _rex.apply_batches(
        core, list(routing.get("batch") or []), pairs or {}, today)
    _persist_non_convergence(vault, routing, today)
    return deferred


def _ask_owner_questions(
    core: Any, vault: Path, questions: list[dict[str, Any]], today: datetime.date,
) -> list[tuple[str, str]]:
    if not questions:
        return []
    # The DECISIONS, never just the keys: a `defer` and an unreadable answer
    # used to retire a question exactly as a real ruling did (llm-review, s06).
    decisions = _rs.owner_decisions(vault)
    holds = _ranswers.deferred_holds(decisions, today)
    cap = owner_cap()
    try:
        open_now = len(_inbox.open_questions(core._read_inbox()))
    except Exception:  # noqa: BLE001 — an unreadable queue is a full queue
        open_now = cap
    deferred: list[tuple[str, str]] = []
    for question in questions:
        finding = question.get("finding") or (question["key"], question["question"])
        decision = decisions.get(question["key"])
        verdict = (_rq.answer_verdict(finding[0], decision.get("answer"),
                                      question.get("options"))
                   if decision is not None else None)
        if verdict in (_rq.ACCEPTED, _rq.SETTLED):
            # The owner has ruled on this subject, so it is never re-asked.
            # But an answer settles the QUESTION, not always the CONDITION: a
            # redirected write RECURS, stalling the batch every hour until the
            # cause is fixed, and its signature is stable run to run — so one
            # answer used to put the key in the ledger forever and it reached
            # neither the banner nor the alerts feed while the lane quietly
            # stopped repairing. A key the registry marks `keeps_blocking`
            # therefore keeps its surface, unasked (llm-review, final).
            if question.get("keeps_blocking"):
                deferred.append(finding)
            continue
        if verdict == _rq.DEFERRED and question["key"] in holds:
            # "Not now" — so it is not re-asked until the cooldown ends, and it
            # is NOT silent while it waits: the condition is still true and the
            # owner has not acted on it, so it goes back to the banner.
            deferred.append(finding)
            continue
        # Everything else is asked (again): a defer whose cooldown elapsed, and
        # an UNRECOGNISED answer — which fails closed, because an answer this
        # engine cannot read is not consent to stop asking.
        if open_now >= cap:
            deferred.append(finding)
            continue
        try:
            if core.enqueue_question(question, source=question.get("source", ""),
                                     today=today):
                open_now += 1
        except Exception:  # noqa: BLE001 — an unaskable question must still be seen
            deferred.append(finding)
    return deferred


def _persist_non_convergence(
    vault: Path, routing: Mapping[str, Any], today: datetime.date,
) -> None:
    """Count the runs a branch spent unable to prove it covered a finding.

    Only ever touched for branches that already have a state row, so this never
    creates state a branch has not written."""
    if not routing["non_convergence"] and not routing["bound"]:
        return
    state = _rs.read_state(vault)
    branches = state.get("branches") or {}
    changed = False
    for branch, keys in routing["non_convergence"].items():
        entry = branches.get(branch)
        # Day-gated like every other counter in this lane. The routing step
        # runs once per maintain run, i.e. HOURLY, so a bare increment would
        # read "24" after one bad day and mislead the first consumer to
        # threshold it (audit A, 2026-08-21 — the class `once_per_day` owns).
        if isinstance(entry, dict) and _rs.once_per_day(
                entry, "routing_non_convergence", today):
            entry["routing_non_convergence"] = int(
                entry.get("routing_non_convergence", 0) or 0) + len(keys)
            changed = True
    for branch in routing["bound"]:
        entry = branches.get(branch)
        if isinstance(entry, dict) and entry.get("routing_non_convergence"):
            _rs.reset_daily(entry, "routing_non_convergence")
            changed = True
    if changed:
        _rs.write_state(vault, state)


def router_for(
    core: Any, today: datetime.date, *, targets: Mapping[str, Any] | None = None,
    subjects: Mapping[str, Any] | None = None,
    pairs: Mapping[str, Any] | None = None,
) -> Callable[[list[tuple[str, str]]], list[tuple[str, str]]]:
    """The callable ``pending_notifications`` applies to its raw findings.

    Fails CLOSED: if anything at all goes wrong the untouched findings are
    returned, so a routing bug can only ever make the banner louder."""
    def _route(findings: list[tuple[str, str]]) -> list[tuple[str, str]]:
        try:
            routing = route(findings, _rs.read_state(Path(core.vault)), today,
                            targets=targets, subjects=subjects)
            return list(routing["banner"]) + apply_routing(
                core, routing, today, pairs=pairs)
        except Exception:  # noqa: BLE001 — never lose a finding to a routing bug
            return findings

    return _route
