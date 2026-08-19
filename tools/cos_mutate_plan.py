"""Screening stages of the COS mutation plan (s18 drain of build_plan).

Every stage is moved verbatim out of ``cos_mutate.build_plan`` and receives
the lane module's own callables (``short``, ``draft_signature``, …) as
arguments, so the definitions stay single and a test that monkeypatches a
name on ``cos_mutate`` keeps governing what these stages do.
"""
from __future__ import annotations

from typing import Any, Callable
import hashlib
import json
from cos_mutate_gates import MutationStop, _utcnow  # noqa: E402
from cos_mutate_policy import CHIP_RANK  # noqa: E402
from cos_reconcile_metrics import MUTATION_VERBS  # noqa: E402


def screen_ledger_rows(rows: list[dict[str, Any]], exclude: Callable,
                       *, short: Callable, chip_for: Callable,
                       managed_chips: tuple[str, ...]) -> list[dict[str, Any]]:
    """Plan the archive and chip mutations the JUDGED ledger rows justify."""
    planned: list[dict[str, Any]] = []
    for row in rows:
        cid = row["conversation_id"]
        if row.get("auto_archive") is True:
            if row.get("read_state") != "read":
                exclude(cid, "archive", "read_state is not `read` — an unread "
                                        "row is untouchable by any lane")
            elif row.get("tier") in ("P0", "P1"):
                exclude(cid, "archive", f"tier {row.get('tier')} is hard-excluded "
                                        "from auto-archive under every lane")
            elif row.get("judgment_pending"):
                exclude(cid, "archive", "the row carries no verdict")
            else:
                planned.append({
                    "verb": "archive", "conversation_id": cid,
                    "reason": f"auto-archive: {row.get('verdict')}/"
                              f"{row.get('tier') or 'untiered'}/"
                              f"{row.get('noise_signal') or 'signal-unnamed'}",
                    "tier": row.get("tier"), "read_state": row.get("read_state"),
                    "received": row.get("received"),
                })
        # THE CHIP COMES FROM THE (bucket, tier) MATRIX, not from `hold_category`
        # (which is the judge's hold-REASON vocabulary — `Held · ask` and its
        # siblings — and could never match a managed chip name; wired to it, the
        # lane was unfireable by construction), and not from the tier ALONE
        # (DOCTRINE v7 §4.1: `read`/P2 → `P3 · Read` while `act`/P2 →
        # `P2 · This week`, same tier, different chip). `verdict` is the bucket
        # `apply_judgment` wrote; `judged_tier` is the tier. The lane is
        # ADD-ONLY: it puts a missing chip on, and never touches a thread that
        # already carries one, because clearing a chip is a shape this build has
        # not accepted from us.
        chip = chip_for(row.get("verdict"), row.get("judged_tier"))
        if chip:
            if chip not in managed_chips:
                exclude(cid, "categorize", f"{chip!r} is not a managed priority "
                                           "chip; only the four may be written")
            elif row.get("judgment_pending"):
                exclude(cid, "categorize", "the row carries no verdict")
            elif row.get("tier"):
                exclude(cid, "categorize",
                        f"the thread already carries a {row.get('tier')} chip and "
                        "this lane is ADD-ONLY — it cannot clear or replace one")
            else:
                planned.append({"verb": "categorize", "conversation_id": cid,
                                "chip": chip, "mode": "add",
                                "received": row.get("received"),
                                "reason": f"chip: judged "
                                          f"{row.get('verdict')}/"
                                          f"{row.get('judged_tier')} on a "
                                          "thread carrying none"})
    return planned


def screen_drafts(drafts: list[dict[str, Any]], ledger_ids: set[str],
                  received_by_cid: dict[str, Any], run_id: str,
                  exclude: Callable, *, short: Callable,
                  draft_signature: Callable,
                  draft_form: Callable) -> list[dict[str, Any]]:
    """Plan the reply drafts the pending-drafts ledger carries."""
    planned: list[dict[str, Any]] = []
    for d in drafts:
        cid = d.get("conversation_id")
        if d.get("saved_to_mailbox"):
            continue
        if cid not in ledger_ids:
            exclude(str(cid), "draft", "the draft names a conversation this run "
                                       "never enumerated")
        elif not str(d.get("text") or "").strip():
            exclude(cid, "draft", "the draft carries no text")
        else:
            # PLACEHOLDERS ARE REQUIRED, NOT A DEFECT. `draft.placeholder_honesty`
            # (cos_judge) refuses an UNGROUNDED draft that carries NO
            # `[owner: confirm …]` marker — "that is an invented fact in the
            # owner's voice". A screen that excluded placeholder-carrying drafts
            # would have blocked the entire draft lane: all 10 of run 115's
            # drafts carry one, measured 2026-08-10. The draft is saved unsent
            # with its markers visible, which is exactly what they are for.
            planned.append({"verb": "draft", "conversation_id": cid,
                            "text": d["text"],
                            "placeholders": d.get("placeholders") or [],
                            "recipient_scope": d.get("recipient_scope"),
                            "signature": draft_signature(run_id, cid),
                            "received": received_by_cid.get(cid),
                            # PROJECTED, NOT INTERPOLATED (design revision 4,
                            # carried finding 2). `form` is MODEL-AUTHORED and
                            # survives D14's projection; `draft.stale_ask_form`
                            # fires only on stale asks, so `--judge` does not
                            # bound it. `reason` reaches the undo ledger, whose
                            # closed field set argues that no value on the row
                            # is free text — an f-string over an unprojected
                            # model string would falsify that in one line.
                            "reason": f"reply draft ({draft_form(d.get('form'))})"})
    return planned


def apply_recency_window(planned: list[dict[str, Any]], since_days: int | None,
                         exclude: Callable, *, within_window: Callable,
                         now) -> list[dict[str, Any]]:
    """Keep only mutations inside the recency window; record what fell out."""
    # THE RECENCY WINDOW is the real bound now, not a count. A thread older than
    # `since_days` is out of scope — recorded, not silently dropped — unless the
    # window was lifted (`--all`, historic). This is what replaces the numeric
    # cap: the mailbox and its dates decide the size of a night, not a number.
    windowed: list[dict[str, Any]] = []
    for m in planned:
        if within_window(m.get("received"), since_days, now):
            windowed.append(m)
        else:
            exclude(m["conversation_id"], m["verb"],
                    f"outside the {since_days}-day window (received "
                    f"{str(m.get('received'))[:10]}) — run historic/--all to "
                    "include it")
    return windowed


def drop_already_applied(planned: list[dict[str, Any]], skip_keys: set[str],
                         exclude: Callable) -> list[dict[str, Any]]:
    """Drop mutations this checkpoint already carried out, before the cap.

    A mutation this checkpoint ALREADY carried out is dropped BEFORE the cap,
    never counted through it twice. It still counts in `applied`, so the blast
    radius is unchanged — what changes is that the remaining slots go to work
    that has not happened yet. Measured on run 118: a resumed pass planned its
    five chips, three of them already applied, and delivered nothing.
    """
    fresh: list[dict[str, Any]] = []
    for m in planned:
        if f"{m['conversation_id']}|{m['verb']}" in skip_keys:
            exclude(m["conversation_id"], m["verb"],
                    "already carried out this checkpoint — it counts against "
                    "the cap in `applied`, not as a slot to spend again")
        else:
            fresh.append(m)
    return fresh


def apply_caps(planned: list[dict[str, Any]], caps: dict[str, int | None],
               applied: dict[str, int], exclude: Callable, *,
               verbs: tuple[str, ...]) -> list[dict[str, Any]]:
    """Let the per-run caps bite LAST, over the re-screened plan.

    Caps count what is already applied this checkpoint; ``caps[verb] is None``
    is UNLIMITED — the default. A number still bounds a deliberately small
    hand-run.
    """
    kept: list[dict[str, Any]] = []
    counts = {v: applied.get(v, 0) for v in verbs}
    for m in planned:
        verb = m["verb"]
        if caps[verb] is not None and counts[verb] >= caps[verb]:
            exclude(m["conversation_id"], verb,
                    f"per-run cap reached ({caps[verb]}, {applied.get(verb, 0)} "
                    "already applied this checkpoint)")
            continue
        counts[verb] += 1
        kept.append(m)
    return kept


# ---------------------------------------------------------------------------
# batch-2 drain: the plan builders moved verbatim out of `cos_mutate` and
# are re-imported by it; the vault gates they read live in
# `cos_mutate_gates` beside this module.
# ---------------------------------------------------------------------------
import sys                                                    # noqa: E402
from pathlib import Path                                      # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from brain import cos_chips  # noqa: E402
chip_for = cos_chips.chip_for  # noqa: E401
import os                                                      # noqa: E402
from cos_mutate_gates import (  # noqa: E402
    absent_skip_cap, draft_form, draft_signature, short, _ledger_path,
    _read_jsonl, _within_window, kill_switch, stop_file, stopped)
from cos_mutate_policy import (  # noqa: E402
    DEFAULT_CAPS, MANAGED_CHIPS, STATES)
#: The recency window default, read once at import exactly as the parent does
#: (a default argument cannot wait for a namespace).
DEFAULT_SINCE_DAYS = int(os.environ.get("BRAIN_COS_SINCE_DAYS", "14") or "14")



def build_plan(vault: Path, run_id: str, *, caps: dict[str, int] | None = None,
               applied: dict[str, int] | None = None,
               skip_keys: set[str] | None = None,
               since_days: int | None = DEFAULT_SINCE_DAYS) -> dict[str, Any]:
    """Turn the JUDGED ledger into a mutation plan — and re-screen it in CODE.

    The judge decides WHAT (`auto_archive`, `hold_category`, the draft text);
    this re-applies the mechanical screens that are facts rather than opinions,
    so a judgment defect cannot reach the mailbox through a slot the driver
    never re-checked. Every exclusion is recorded with its reason: a plan that
    silently drops rows is how a cap becomes invisible.
    """
    caps = dict(DEFAULT_CAPS, **(caps or {}))
    applied = applied or {}
    rows = _read_jsonl(_ledger_path(vault, run_id))
    if not rows:
        raise MutationStop(
            f"no ingestion ledger for {run_id} — the judgment pass has to have "
            "run before there is anything to execute")
    drafts = _read_jsonl(
        _ledger_path(vault, run_id).with_name(f"_cos_drafts_pending_{run_id}.jsonl"))

    excluded: list[dict[str, Any]] = []

    def exclude(cid: str, verb: str, why: str) -> None:
        excluded.append({"conversation_id": short(cid), "verb": verb, "reason": why})

    # The screening stages themselves live in cos_mutate_plan (s18): each is
    # handed this module's own callables, so a monkeypatch on cos_mutate keeps
    # governing them and the definitions stay single.
    planned = screen_ledger_rows(
        rows, exclude, short=short, chip_for=chip_for, managed_chips=MANAGED_CHIPS)
    ledger_ids = {r["conversation_id"] for r in rows}
    received_by_cid = {r["conversation_id"]: r.get("received") for r in rows}
    planned += screen_drafts(
        drafts, ledger_ids, received_by_cid, run_id, exclude,
        short=short, draft_signature=draft_signature, draft_form=draft_form)

    # WORST-FIRST INSIDE EACH VERB, then the cap. A cap that spends its five
    # chip slots on P2 threads while P1 threads sit unchipped is the same defect
    # as a body-open cap that draws P3 rows first: the selection decides what the
    # run is worth, and it is invisible once the cap has bitten. Enumeration
    # order (newest first) breaks the tie, so the sort stays stable.
    planned.sort(key=lambda m: CHIP_RANK.get(m.get("chip") or "", 9))

    planned = apply_recency_window(
        planned, since_days, exclude, within_window=_within_window, now=_utcnow())
    if skip_keys:
        planned = drop_already_applied(planned, skip_keys, exclude)
    kept = apply_caps(planned, caps, applied, exclude,
                                  verbs=MUTATION_VERBS)

    return {"run_id": run_id, "mutations": kept, "excluded": excluded,
            "caps": caps, "applied_before": applied, "since_days": since_days,
            "plan_digest": plan_digest(kept, run_id),
            "planned_by_verb": {v: sum(1 for m in kept if m["verb"] == v)
                                for v in MUTATION_VERBS}}


# ---------------------------------------------------------------------------
# ONE FROZEN PLAN (review 2026-08-13, round 2, K1 — Codex CRITICAL + Claude)
# ---------------------------------------------------------------------------
# THREE INDEPENDENT PLANS USED TO EXIST. `cos_mutate.py plan` built one and
# wrote `plan.json`; `dry_run()` called `build_plan()` AGAIN and rehearsed
# whatever it got; `apply_pass()` called it a THIRD time and dispatched whatever
# THAT got. The validated artifact was never consumed by anything. Codex probed
# it end to end: a P1/add plan matched against a P3/remove rehearsal returned
# `ok: true`, because the rehearsal gate compares only `(verb,
# conversation_id)` — two rows can name the same thread and the same verb while
# carrying a different chip, a different mode, or entirely different DRAFT TEXT
# destined for the owner's real Drafts folder.
#
# The three builds PREDATE s08. What s08 added is a gate that now CLAIMS the
# rehearsal covers the plan — so the claim outran the mechanism, which is worse
# than no claim.
#
# THE FIX IS IDENTITY, NOT MORE MATCHING. One plan is built ONCE, digested over
# its whole payload, and written to disk; the rehearsal names that digest; the
# apply refuses to run unless the plan it reads still hashes to the digest the
# rehearsal named. A payload that changed after the rehearsal — one character of
# draft text, a chip swapped, a row added — no longer hashes the same and is
# REFUSED, not applied.
def plan_digest(mutations: list[dict[str, Any]], run_id: Any) -> str:
    """The identity of a plan: sha256 over its run id AND its WHOLE mutation list.

    Over the whole row, deliberately. `(verb, conversation_id)` is what the
    rehearsal gate matches on and is exactly what K1 showed to be insufficient:
    the draft lane's payload is free text a human may send verbatim, and two
    rows agreeing on verb and thread can disagree about every word of it.

    THE RUN ID IS INSIDE THE HASH, and `run_id` is a REQUIRED argument
    (review 2026-08-13, round 5, C1 — consensus CRITICAL). It used to be a
    sibling key of `plan_digest` in the written document, covered by nothing:
    both review lanes deleted it from an OLD run's plan and applied that plan
    under a NEW run whose UndoLedger is empty, so `done_keys` skipped nothing
    and every archive, chip and draft in it was dispatched a second time —
    around the lane lock, which is keyed on the run id and therefore hands a
    different lock to a different run. A digest that does not cover the run id
    is the identity of a PAYLOAD, and this artifact's job is the identity of a
    payload FOR A RUN. Deleting or editing the key now changes the hash, so
    `load_frozen_plan` refuses the document before anything reads its rows.

    No default. A default of `None` would let a caller silently reproduce the
    exact hole this closes, and there is no site in this file that legitimately
    digests a plan without knowing which run it belongs to.

    Canonical serialization (`sort_keys`, no whitespace) so that key order,
    indentation and a re-serialization round trip cannot change the identity of
    a plan that did not change.
    """
    return hashlib.sha256(json.dumps(
        {"run_id": run_id, "mutations": mutations}, sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def load_frozen_plan(path: Path) -> dict[str, Any]:
    """Read a written plan and REFUSE it unless it still hashes to its stamp.

    This is the whole point of the artifact: a plan that was edited, truncated,
    or replaced between the rehearsal and the apply is not the plan that was
    rehearsed, and the only safe reading of one is to dispatch nothing.

    The stamp covers `run_id` as well as `mutations` (C1), so a plan whose run
    id was DELETED to make it apply under another run no longer hashes to its
    own stamp and stops here, before `_frozen_todo`'s run check even runs.
    """
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MutationStop(
            f"the frozen plan at {path} could not be read ({exc}) — an "
            "unreadable plan is not an empty one, and nothing is dispatched "
            "against a plan this lane never saw") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("mutations"), list):
        raise MutationStop(
            f"the frozen plan at {path} carries no `mutations` list — the key "
            "it is read by is part of the contract, and a missing one used to "
            "read as an empty plan")
    stamped = doc.get("plan_digest")
    if not isinstance(stamped, str) or not stamped.strip():
        raise MutationStop(
            f"the frozen plan at {path} carries no `plan_digest`. An unstamped "
            "plan cannot be bound to the rehearsal that validated it, which is "
            "the whole reason this artifact is written to disk")
    actual = plan_digest(doc["mutations"], doc.get("run_id"))
    if actual != stamped:
        raise MutationStop(
            f"the frozen plan at {path} does not hash to its own stamp "
            f"(stamped {stamped[:16]}…, computed {actual[:16]}…) — its payload "
            "changed after it was written, so it is not the plan anything "
            "rehearsed and nothing is dispatched")
    return doc


def plan_binding_path(vault: Path, run_id: str) -> Path:
    """Where the apply records WHICH plan it dispatched, for the validator.

    HOST-PRIVATE, beside the run manifest and validity verdicts (review
    2026-08-13, round 6, H-forge). It used to live in `run_ops_dir` = the
    `cos-ops` directory, which `cos.run_ops_dir` itself documents as
    "VM-writable by nature" — and `check_plan_binding` read `source` straight
    out of it, so a VM (or a self-planned `apply_pass` rebuild) could write
    `{"source":"frozen"}` there and launder a clean K1 verdict, no digest, no
    plan, no rehearsal. A run-validity control cannot take an unauthenticated
    assertion from the VM-writable ops zone. `runs_dir` is the same location
    the manifest the validator already trusts lives in, and it IS reachable by
    `cos_runverify` (it reads the manifest and the validity verdict from there
    on every pass).

    Round 6 said that location was "never VM-writable" and it was not: until
    gap-05 (2026-08-16) `runs_dir` resolved to `<vault>/.brain/cos/host/runs`,
    host-private by CONTRACT (AGENTS.md §9) and inside the VirtioFS workspace
    by FILESYSTEM. It now resolves through `config.cos_run_records_dir`, off
    the mount beside the writer lock and the supersede journal, so the sentence
    the whole control rests on is finally true.

    The NAME lives in `cos.run_plan_binding_path` (round 7) — writer and reader
    held the same literal twice, so the round-6 move had to be made in two
    files. This wrapper adds only the directory the writer needs.
    """
    from brain import cos                                        # noqa: PLC0415

    p = cos.run_plan_binding_path(vault, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
