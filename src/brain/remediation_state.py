"""REG-03 — the HOST-PRIVATE remediation state, and what it is allowed to say.

The routing decision itself lives in :mod:`brain.remediation_routing`; this is
the evidence that decision reads. It is split out because the two halves have
different trust properties, and the split is the point: everything here is
about WHERE the evidence comes from, and nothing here decides anything.

Three rules, each a ruling rather than a preference:

* **Authoritative state is host-private, AND bound to this vault.**
  ``config.index_dir()/remediation/``, proven off every VM-visible root before
  it is read. The mount-visible ``maintain-state.json`` is a display
  projection: a VM that could write ``last_run: today`` there would silence
  every finding that branch owns. Location is not enough on its own, though —
  ``index_dir`` is KEYED on ``<vault>/.brain/vault-id``, a file on the mount,
  so rewriting it selects a DIFFERENT off-mount directory. Proving a path is
  off the mount proves location, not binding (adversarial review 2026-08-21).
  So every record carries the resolved vault path this host wrote it for, and
  a record naming another vault is refused — a VM can point the key elsewhere,
  but it cannot make a foreign store claim this vault.
* **Unreadable is not healthy.** An unparseable file, an unrecognised
  ``schema_version``, a path that resolves onto the mount — all of them read as
  ``{}``, which is the NEVER-RUN state for every branch and suppresses nothing.
  A remediation system that cannot read its own state must be loud.
* **Owner answers are host-authenticated.** They are appended here by the
  host-broker inbox verb; the copy in the mount-visible ``inbox.jsonl`` is what
  the owner reads, and a forged ``status: answered`` line there must not be
  able to retire a question nobody answered.

Schema (design-freeze (e), extended by REG-03 with ``covered``,
``heal_streaks`` and ``routing_non_convergence`` — see that file's amendment
note)::

    {"schema_version": "remediation_state/v1",
     "branches": {"<branch>": {
        "last_run": "YYYY-MM-DD", "consecutive_failures": 0,
        "consecutive_skips": 0, "unchanged_runs": 0,
        "covered": {"<finding key>": {"detector_generation": "...",
                                      "target_set_hash": "<sha256>",
                                      "remaining": 0,
                                      "previous_remaining": 0}},
        "heal_streaks": {"<target>": {"last": "YYYY-MM-DD", "nights": 1}},
        "routing_non_convergence": 0}}}
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from . import maintenance_escalation as _escalation

#: The host-private root, under ``config.index_dir(vault)``.
DIRNAME = "remediation"
STATE_FILENAME = "state.json"
ANSWERS_FILENAME = "answers.jsonl"
STATE_SCHEMA = "remediation_state/v1"
ANSWERS_SCHEMA = "remediation_answers/v1"

#: The three liveness states (grill ruling). ``never-run`` is deliberately
#: distinct from both others: it suppresses nothing and escalates nothing, so
#: upgrade day behaves exactly like the day before it.
NEVER_RUN, LIVE, ESCALATED = "never-run", "live", "escalated"

#: The same target healed this many nights running is symptom-remediation
#: thrashing, and becomes an owner-class exception.
THRASH_NIGHTS = 3


def remediation_dir(vault: str | os.PathLike[str]) -> Path:
    """``<index dir>/remediation``, PROVEN outside every VM-visible root.

    Raises ``config.HostPathUnsafe`` when it is not — ``index_dir`` is keyed on
    the mount-resident ``.brain/vault-id``, so a repoint must be a loud refusal
    rather than a silently empty store."""
    from . import config

    return config.proven_off_mount(
        config.index_dir(vault) / DIRNAME, vault, what="remediation state")


def vault_binding(vault: str | os.PathLike[str]) -> str:
    """The canonical, HOST-controlled identity of this vault.

    Deliberately the resolved filesystem path and not ``.brain/vault-id``: the
    id file is on the mount and is exactly what an attacker rewrites to aim
    ``index_dir`` at another store. Where the host mounted this vault is not
    something the guest can restate."""
    return str(Path(vault).expanduser().resolve())


def read_state(vault: str | os.PathLike[str]) -> dict[str, Any]:
    """The branch state, or ``{}`` when it cannot be read or trusted.

    ``{}`` is not "healthy" — it is the never-run state for every branch, which
    suppresses nothing. An unparseable file, an unrecognised ``schema_version``
    and an unsafe path all land here on purpose: a remediation system that
    cannot read its own state must be loud, never quiet."""
    try:
        raw = json.loads(
            (remediation_dir(vault) / STATE_FILENAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — every failure is the same fail-closed answer
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA:
        return {}
    if raw.get("vault") != vault_binding(vault):
        # A store that does not name THIS vault is foreign — reached by a
        # rewritten `.brain/vault-id`, or simply left behind by a repoint. It
        # reads as never-run, which suppresses nothing.
        return {}
    branches = raw.get("branches")
    if not isinstance(branches, dict):
        return {}
    return {"schema_version": STATE_SCHEMA,
            "branches": {str(k): v for k, v in branches.items()
                         if isinstance(v, dict)}}


def write_state(vault: str | os.PathLike[str], state: Mapping[str, Any]) -> bool:
    """Persist ``state`` atomically. False (never an exception) on any failure."""
    payload = {"schema_version": STATE_SCHEMA,
               "vault": vault_binding(vault),
               "branches": dict(state.get("branches") or {})}
    try:
        d = remediation_dir(vault)
        d.mkdir(parents=True, exist_ok=True)
        path = d / STATE_FILENAME
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — losing a counter never fails a maintain run
        return False


def record_owner_answer(
    vault: str | os.PathLike[str], entry: Mapping[str, Any],
) -> bool:
    """Append one HOST-AUTHENTICATED owner answer to the host-private ledger.

    This is the only record audited remediation may act on. The mount-visible
    ``inbox.jsonl`` carries the same answer for the owner to read, but a VM can
    write that file, so an ``answered`` line there is a CLAIM. Called from the
    host-broker inbox verb; best-effort by design — the CONSUMER fails closed on
    a missing ledger entry rather than this failing the owner's answer."""
    row = {"schema_version": ANSWERS_SCHEMA, "vault": vault_binding(vault),
           **dict(entry)}
    try:
        d = remediation_dir(vault)
        d.mkdir(parents=True, exist_ok=True)
        with (d / ANSWERS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def owner_decisions(vault: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """``{question key: the owner's LATEST answer row}`` — the whole decision.

    THE reader for this ledger, and the only one a consumer that ACTS may use.
    ``record_owner_answer`` has always written the answer VALUE here; both
    consumers read the KEYS and threw the rest away, so accepting "raise the
    lower copy" and accepting the default "defer" retired a cross-tier pair
    identically and the accepted raise never ran (llm-review, s06). A later
    row for the same key wins: the owner may answer the same question twice."""
    out: dict[str, dict[str, Any]] = {}
    try:
        text = (remediation_dir(vault) / ANSWERS_FILENAME).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — no ledger means nothing is decided yet
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("schema_version") == ANSWERS_SCHEMA
                and row.get("vault") == vault_binding(vault) and row.get("key")):
            # Same binding as the state file, and for the same reason: an
            # answers ledger reached through a rewritten vault-id could retire
            # this vault's owner questions with another vault's decisions.
            out[str(row["key"])] = row
    return out


def decided_question_keys(vault: str | os.PathLike[str]) -> set[str]:
    """Membership only: which questions have an answer, never WHICH answer.

    Derived from :func:`owner_decisions` so there is ONE parse of the ledger.
    Nothing in ``src/brain`` may call this — a consumer that reads keys and
    ignores values is the defect above, and
    ``tests/test_inbox_conversion.py::test_no_engine_module_reads_the_answers_ledger_key_only``
    fails on the next one."""
    return set(owner_decisions(vault))


# ---------------------------------------------------------------------------
# Daily counting. THE primitive — see the class note below.
# ---------------------------------------------------------------------------
def once_per_day(
    row: MutableMapping[str, Any], counter: str, today: datetime.date,
) -> bool:
    """Claim ``today`` for ``counter``. True at most ONCE per calendar day.

    **This exists because the same bug shipped three times.** Every threshold
    the remediation lane is judged against is denominated in DAYS —
    ``escalate_after`` counts runs at the branch's declared cadence (1 day),
    ``shadow_nights`` counts nights, ``THRASH_NIGHTS`` counts nights — while
    the fold that writes those counters rides the HOURLY maintain flow. A bare
    ``+= 1`` therefore means "per hour", so a threshold of 3 fires in three
    hours instead of three days. It was found and fixed separately in
    ``record_shadow``, then in ``_count_unchanged``, then in
    ``record_failure``; three sites, one class (llm-review + adversarial
    review, 2026-08-21).

    So the arithmetic stays at the call site — a decrement, an increment and a
    reset are genuinely different — and only the DAY CLAIM is shared. Call it
    LAST, after every other guard has passed: it stamps the marker as a side
    effect, and a run that claims the day and then declines to count has
    silently eaten that day's count.

    ``tests/test_remediation_hardening.py`` asserts by AST that every
    day-denominated counter in the lane routes through here, so a fourth site
    cannot repeat the class."""
    marker = f"{counter}_last_day"
    iso = today.isoformat()
    if row.get(marker) == iso:
        return False
    row[marker] = iso
    return True


def reset_daily(row: MutableMapping[str, Any], counter: str) -> None:
    """Zero ``counter`` and release its day claim.

    A RESET is never date-gated: the moment the condition the counter was
    tracking stops being true, it must drop to zero on the spot, whatever hour
    it is. Releasing the marker is what lets the same day count again if the
    condition returns."""
    row[counter] = 0
    row.pop(f"{counter}_last_day", None)


# ---------------------------------------------------------------------------
# Liveness, convergence, and the finding binding. All pure.
# ---------------------------------------------------------------------------
def branch_liveness(
    state: Mapping[str, Any], branch: str, escalate_after: int,
    today: datetime.date,
) -> tuple[str, list[str]]:
    """One of the three states, plus the reasons an escalated branch escalated.

    The generic ladder (repeated failure, a wedged writer lock, a ``last_run``
    older than two cadences) is ``maintenance_escalation.branch_escalation`` —
    imported, never re-derived, so remediation branches are judged by the same
    rules as every other branch. On top of it sit the two the REGISTRY declares
    per branch: ``escalate_after`` consecutive failures, and ``escalate_after``
    consecutive runs whose remaining-target set did not shrink. The second is
    what makes non-convergence escalate even when every run exited 0."""
    entry = (state.get("branches") or {}).get(branch)
    if not isinstance(entry, dict) or not entry.get("last_run"):
        return NEVER_RUN, []
    reasons = list(_escalation.branch_escalation(branch, entry, today)["reasons"])
    after = max(1, int(escalate_after or 1))
    failures = int(entry.get("consecutive_failures", 0) or 0)
    if failures >= after and not any("consecutive failures" in r for r in reasons):
        reasons.append(f"{failures} consecutive failures")
    unchanged = int(entry.get("unchanged_runs", 0) or 0)
    if unchanged >= after:
        reasons.append(
            f"remaining target set unchanged for {unchanged} run(s) — the branch "
            "runs but does not converge")
    return (ESCALATED, reasons) if reasons else (LIVE, [])


def thrashing_targets(state: Mapping[str, Any], branch: str) -> list[str]:
    """Targets this branch has healed on ``THRASH_NIGHTS`` consecutive nights.

    A fix that has to be re-applied every night is not a fix, it is a symptom
    being suppressed nightly — so it stops being automation and becomes an
    owner-class exception."""
    entry = (state.get("branches") or {}).get(branch)
    streaks = entry.get("heal_streaks") if isinstance(entry, dict) else None
    if not isinstance(streaks, dict):
        return []
    return sorted(
        str(target) for target, row in streaks.items()
        if isinstance(row, dict) and int(row.get("nights", 0) or 0) >= THRASH_NIGHTS)


def coverage(state: Mapping[str, Any], branch: str, key: str) -> dict[str, Any] | None:
    entry = (state.get("branches") or {}).get(branch)
    covered = entry.get("covered") if isinstance(entry, dict) else None
    row = covered.get(key) if isinstance(covered, dict) else None
    return row if isinstance(row, dict) else None


def binding_matches(
    covered: Mapping[str, Any] | None, signature: Mapping[str, Any] | None,
) -> bool:
    """Whether the branch's last report covers the CURRENT target set for a key.

    Both halves must be present and equal. A missing signature is a MISMATCH,
    not a pass: with no current target set to compare against there is no
    evidence the branch processed the targets this finding is about, and the
    whole point of the binding is that stale coverage may not suppress a
    finding about targets the branch has never seen.

    And the coverage must come from a run that was allowed to REPAIR. A branch
    inside its shadow window has by definition fixed nothing, so its report is
    a rehearsal, not evidence — yet ``converged()`` would happily pass it the
    moment the population shrank for an unrelated reason, suppressing a
    finding nothing had ever acted on. Shadow mode is the safety property that
    protects the very first unattended run on a real vault; a rehearsal that
    can suppress is not a rehearsal. The mode is stamped on the row by the
    producer and CHECKED here — in the module that decides suppression — so
    the rule binds every producer, including ones not written yet
    (llm-review, 2026-08-21)."""
    if not covered or not signature:
        return False
    if str(covered.get("mode") or "") != "live":
        return False
    generation = str(signature.get("generation") or "")
    digest = str(signature.get("hash") or "")
    if not generation or not digest:
        return False
    return (str(covered.get("detector_generation") or "") == generation
            and str(covered.get("target_set_hash") or "") == digest)


def converged(covered: Mapping[str, Any]) -> bool:
    """Remaining is zero, or strictly smaller than the previous run's."""
    remaining = covered.get("remaining")
    if not isinstance(remaining, int):
        return False
    if remaining == 0:
        return True
    previous = covered.get("previous_remaining")
    return isinstance(previous, int) and remaining < previous
