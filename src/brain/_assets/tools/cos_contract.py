#!/usr/bin/env python3
"""Render the run-level OUTCOME CONTRACT verdict — the run supplies data, this
script supplies the verdict.

WHY THIS EXISTS (measured). Six consecutive COS runs scored 27/27 on the
E-checks while archiving nothing for seven days. The E-checks verify the
PARTS; nothing verified the OUTCOME. Agents assert completion while the
environment disagrees in 45-78% of failing trajectories, and an LLM judge
cannot detect it (no configuration above AUROC 0.65) — while a plain
count-comparison outperforms reflective self-checks 4-8x. So the verdict is
computed by deterministic code from three inputs, never composed by the run.

The three inputs are all authored by the run being judged, so this checker
DISTRUSTS them: a bucket sum that does not equal the enumerated set, a PRE
conversation count that does not equal that set, a residency mismatch against
the post-run Inbox conversation count, or a convid in the re-enumeration that
was never enumerated and never arrived, each render FAILED rather than a clean
PASS with a `verdict_source` sha on it. OWA's transcribed folder badge is
recorded separately because it counts message items, not conversations.

    python3 tools/cos_contract.py --pre pre.json --post post.json \
        --ledgers <vault>/cos-ops --run-id 41 --profile full --out block.json

`--run-id` is REQUIRED: it scopes the ledger scan to THIS run's rows, or
yesterday's ledgers in the same directory silently satisfy today's liveness
guard. Exit 0 = PASS, 1 = FAILED, 2 = malformed input.

A run whose safety guard correctly STOPPED still owes a bucket for every row it
enumerated: `stopped_by_guard` is a terminal, ACCOUNTED disposition (v5.52), and
it is refused unless the stop is both declared and corroborated by the run's own
ledgers. The owner's elected-lane pin (`overlay/cos/browser-lane.md`) is read
from the vault, never from the run, so a silent fallback to the other lane is a
named clause instead of an unremarked lane change.

ponytail: the output counts come from the ledgers and the eligible-input
counts from the run's own candidate records — the run may not hand this script
either total. Unknown ledger row shapes are ignored, never guessed, so the
output side under-counts before it over-counts.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_reconcile_metrics import _rows, counts_archive, counts_draft  # noqa: E402

BUCKETS = ("archived", "held_non_drafted", "held_drafted", "chipped",
           "unaccounted", "stopped_by_guard")
PROFILES = ("full", "label-only")

#: WHICH buckets count as ACCOUNTED is profile-dependent, and that is
#: load-bearing: `full` refuses a bare P-chip (v5.26 wants a Held label on
#: anything not archived), `label-only` accepts it and forbids archiving.
#: `stopped_by_guard` is accounted under BOTH — a safety stop halts ACTION,
#: never ACCOUNTING (v5.48/v5.52), and the disposition a stopped run owes is a
#: terminal bucket that says so, not an absence.
ACCOUNTED = {
    "full": frozenset({"archived", "held_non_drafted", "held_drafted",
                       "stopped_by_guard"}),
    "label-only": frozenset({"chipped", "held_non_drafted", "held_drafted",
                             "stopped_by_guard"}),
}

#: The CLOSED vocabulary of stops that may excuse a row from its disposition.
#: A run may not invent one: doctrine names the guard, and the guard's own
#: ledger reason word is what corroborates it (E30(c)).
GUARD_STOPS = ("target-identity-mismatch",)

#: Where a recorded guard stop leaves its own trace. The checker DERIVES the
#: corroboration from these — the `guard_stop` record alone is the run's word.
#:
#: THE ACTION LEDGER HAS NO v7 PRODUCER AND IS KEPT ANYWAY (s10, 2026-08-16).
#: `_cos_action_ledger_*` was written by the MODEL leg of the pre-v7 browser
#: lane; the v7 model legs run `--tools "Read,Glob"` with editing denied and
#: cannot write a file at all. It is NOT removed, for two measured reasons:
#:   1. `cos_runverify.check_contract` RE-EXECUTES this checker over every run
#:      it scores, including historical ones. Six runs in the reference vault
#:      declare a `target-identity-mismatch` stop, and for TWO of them
#:      (2026-08-09-run104, 2026-08-10-run112) the ingestion ledger carries
#:      ZERO corroborating rows and the action ledger carries all of them.
#:      Dropping this glob flips those two from PASS to
#:      `OC-guard-stop-uncorroborated` on the next re-verification — rewriting
#:      history from a change of reader, not a change of fact.
#:   2. Its input is not "permanently absent": nine action ledgers are on disk.
#: Direction check, because a dead reader is only safe when it fails CLOSED:
#: this glob can only ever ADD corroboration, so its silence on a v7 night
#: cannot buy a PASS — an uncorroborated stop is refused. The v7 lane declares
#: no guard stop at all (`cos_driver.build_contract_inputs` writes no
#: `guard_stop` key), so `guard_stop_corroborated` is not even reached; the
#: click-era identity risk it scores is the same one `cos_runverify` RETIRED
#: with `target_identity`, because the REST lane addresses a conversation by id.
GUARD_STOP_GLOBS = ("_cos_ingestion_ledger_*.jsonl", "_cos_action_ledger_*.jsonl")

CAPABILITIES = ("archives", "drafts", "chip_clears")

TOOLSETS = ("iab", "chrome-plugin")

#: A guard may only be evaluated for a capability IN SCOPE for the profile —
#: under `label-only` archiving and drafting are forbidden BY DEFINITION, so
#: evaluating them would fail every correct midday run by construction.
IN_SCOPE = {
    "full": {"archives": True, "drafts": True, "chip_clears": True},
    "label-only": {"archives": False, "drafts": False, "chip_clears": True},
}

#: The OUTPUT side of capability liveness, per capability.
#:
#: ALL THREE ARE PRE-v7 MODEL-WRITTEN LEDGERS WITH NO v7 PRODUCER, AND THEY ARE
#: DELIBERATELY *NOT* REPOINTED AT `_cos_undo_ledger_*` (s10, 2026-08-16).
#: The reason is the ORDER of the night, not sentiment. The outcome contract is
#: a READ-lane instrument: `cos_driver` renders PRE/POST and runs this checker
#: hours before `cos_mutate apply` exists, so the undo ledger is not on disk
#: when the block is written. `cos_runverify.check_contract` then RE-EXECUTES
#: this checker after the apply and FAILS the run when the recomputation
#: disagrees with the recorded block. Reading an artifact written between the
#: two executions would make a deterministic re-derivation depend on the clock —
#: which is precisely the property `check_contract` exists to test.
#:
#: So under v7 `capability_liveness` reports `output: 0` for all three. That is
#: NOT a silent all-clear, and the distinction is the whole point of this note:
#: the FAIL clause is `in_scope and output == 0 and eligible_inputs > 0`, and
#: the v7 read lane declares EVERY archive candidate ineligible with a stated
#: `exclusion_reason` ("read-only night: the driver has no mutation lane"), so
#: `eligible_inputs` is 0 and the clause is unreachable by the run's own honest
#: declaration rather than by a missing file. Measured on the reference vault:
#: run 102 (pre-v7) `archives output 1 / eligible 1` — the clause was live and
#: this glob fed it; runs 145 and 148 (v7) `output 0 / eligible 0`.
#:
#: What holds the property under v7 instead: `cos_runverify.check_plan_binding`
#: joins every dispatched `conversation_id|verb` key to the host-private frozen
#: plan, and `check_mutation_counters` recounts the metrics row against the undo
#: ledger. Both run AFTER the apply, where a mutation-liveness question belongs.
LEDGER_GLOB = {
    "archives": "_cos_archive_ledger_*.jsonl",
    "drafts": "_cos_drafts_ledger_*.jsonl",
    "chip_clears": "_cos_chip_ledger_*.jsonl",
}


class Malformed(Exception):
    """Input the checker cannot read as a contract report at all (exit 2)."""


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise Malformed(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Malformed(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise Malformed(f"{label} must include a timezone")
    return parsed


def _validate_sent_snapshot(obj: dict, label: str) -> None:
    proof = obj.get("sent_zero_send")
    if not isinstance(proof, dict):
        raise Malformed(f"{label}: new-schema full run requires `sent_zero_send`")
    if proof.get("identity_field") != "item_id":
        raise Malformed(f"{label}: sent_zero_send.identity_field must be item_id")
    if proof.get("sort") != "newest-first":
        raise Malformed(f"{label}: sent_zero_send.sort must be newest-first")
    if not isinstance(proof.get("complete"), bool):
        raise Malformed(f"{label}: sent_zero_send.complete must be bool")

    window = _timestamp(proof.get("window_start"), f"{label}: sent_zero_send.window_start")
    captured = _timestamp(proof.get("captured_at"), f"{label}: sent_zero_send.captured_at")
    if captured < window:
        raise Malformed(f"{label}: sent proof was captured before its window")

    boundary = proof.get("boundary")
    boundary_value = proof.get("boundary_timestamp")
    if boundary == "older-than-window":
        if _timestamp(boundary_value, f"{label}: sent_zero_send.boundary_timestamp") >= window:
            raise Malformed(f"{label}: sent boundary must be older than window_start")
    elif boundary == "list-end":
        if boundary_value is not None:
            raise Malformed(f"{label}: list-end boundary_timestamp must be null")
    else:
        raise Malformed(
            f"{label}: sent_zero_send.boundary must be older-than-window or list-end")

    items = proof.get("items")
    if not isinstance(items, list):
        raise Malformed(f"{label}: sent_zero_send.items must be a list")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise Malformed(f"{label}: each sent item must be an object")
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise Malformed(f"{label}: each sent item needs a non-empty item_id")
        if item_id in seen:
            raise Malformed(f"{label}: duplicate sent item_id {item_id!r}")
        seen.add(item_id)
        timestamp = _timestamp(
            item.get("timestamp"), f"{label}: sent item {item_id!r} timestamp")
        if timestamp < window or timestamp > captured:
            raise Malformed(
                f"{label}: sent item {item_id!r} falls outside the captured window")


def _sent_zero_send(pre: dict, post: dict) -> tuple[dict, list[str]]:
    before, after = pre["sent_zero_send"], post["sent_zero_send"]
    pre_ids = {item["item_id"] for item in before["items"]}
    post_ids = {item["item_id"] for item in after["items"]}
    new_ids = sorted(post_ids - pre_ids)
    reasons: list[str] = []
    if before["window_start"] != after["window_start"]:
        reasons.append("ZS-window-mismatch")
    if not before["complete"] or not after["complete"]:
        reasons.append("ZS-incomplete")
    if new_ids:
        reasons.append("ZS-new-sent-item")
    return {
        "identity_field": "item_id",
        "window_start": before["window_start"],
        "pre_item_count": len(pre_ids),
        "post_item_count": len(post_ids),
        "new_item_ids": new_ids,
        "complete": not reasons,
    }, reasons


def _counts(pre: dict, post: dict) -> tuple[int, int, int, int, bool]:
    """Return conversation/item counts before/after and whether input is legacy."""
    new_keys = (
        (pre, "inbox_conversation_count_before", "pre"),
        (pre, "owa_folder_item_count_before", "pre"),
        (post, "inbox_conversation_count_after", "post"),
        (post, "owa_folder_item_count_after", "post"),
    )
    if any(key in obj for obj, key, _ in new_keys):
        values = [_require(obj, key, int, label) for obj, key, label in new_keys]
        if any(value < 0 for value in values):
            raise Malformed("conversation and folder-item counts must be non-negative")
        return (*values, False)

    # Legacy v5.28 inputs used one ambiguous Inbox count for both units.
    before = _require(pre, "inbox_count_before", int, "pre")
    after = _require(post, "inbox_count_after", int, "post")
    folder_after = _require(post, "owa_folder_count", int, "post")
    if min(before, after, folder_after) < 0:
        raise Malformed("Inbox counts must be non-negative")
    return before, before, after, folder_after, True


def _complete_enumeration(obj: dict, expected: int) -> bool:
    evidence = obj.get("enumeration_evidence")
    return (
        obj.get("enumeration_complete") is True
        and isinstance(evidence, dict)
        and evidence.get("unique_ids") == expected
        and evidence.get("list_declared_size") == expected
        and isinstance(evidence.get("stagnant_scans"), int)
        and evidence["stagnant_scans"] >= 3
        and evidence.get("scroll_at_end") is True
    )


def _uses_new_count_schema(pre: dict, post: dict) -> bool:
    return any(
        key in obj
        for obj, key in (
            (pre, "inbox_conversation_count_before"),
            (pre, "owa_folder_item_count_before"),
            (post, "inbox_conversation_count_after"),
            (post, "owa_folder_item_count_after"),
        )
    )


def _validate_browser_election(pre: dict, pin: str | None = None) -> str:
    election = pre.get("browser_election")
    if not isinstance(election, dict):
        raise Malformed("pre: new-schema snapshot requires browser_election")
    attempted = election.get("attempted")
    elected = election.get("elected")
    if (not isinstance(attempted, list) or not attempted or
            not all(isinstance(toolset, str) for toolset in attempted)):
        raise Malformed("pre: browser_election.attempted must be a non-empty string list")
    # Under an owner pin the pinned toolset is attempted first instead of iab.
    # Whether the pin was HONOURED is a verdict clause, never a malformed input:
    # a run that fell back must still render a block that says so.
    if attempted[0] not in ({"iab", pin} if pin else {"iab"}):
        raise Malformed("pre: browser_election must attempt iab first"
                        if pin is None else
                        f"pre: browser_election must attempt the pinned {pin!r} "
                        "or iab first")
    if len(set(attempted)) != len(attempted) or any(toolset not in TOOLSETS for toolset in attempted):
        raise Malformed("pre: browser_election contains an invalid or repeated toolset")
    if elected not in TOOLSETS or elected != attempted[-1]:
        raise Malformed("pre: browser_election.elected must be the final attempted toolset")
    return elected


def _validate_scan_provenance(
        obj: dict, label: str, run_id: str, elected: str) -> None:
    provenance = obj.get("scan_provenance")
    if not isinstance(provenance, dict):
        raise Malformed(f"{label}: new-schema snapshot requires scan_provenance")
    if _run_token(provenance.get("run_id")) != _run_token(run_id):
        raise Malformed(f"{label}: scan_provenance.run_id must match --run-id")
    if provenance.get("toolset") != elected:
        raise Malformed(f"{label}: scan_provenance.toolset must match the elected toolset")
    if provenance.get("folder") != "Inbox":
        raise Malformed(f"{label}: scan_provenance.folder must be Inbox")
    if provenance.get("identity_field") != "conversation_id":
        raise Malformed(f"{label}: scan_provenance.identity_field must be conversation_id")


def _validate_browser_provenance(pre: dict, post: dict, run_id: str,
                                 pin: str | None = None) -> str:
    """Require fresh, same-lane scans and IAB-first election for v5.30 inputs."""
    elected = _validate_browser_election(pre, pin)
    for label, obj in (("pre", pre), ("post", post)):
        _validate_scan_provenance(obj, label, run_id, elected)
    return elected


# --- ledger side: what the run actually PRODUCED, scoped to this run --------

def counts_chip_clear(rows: list[dict]) -> int:
    """Verified chip CLEARS (LIF-01 `action: cleared`), never adds/re-levels."""
    n = 0
    for r in rows:
        if r.get("action") != "cleared":
            continue
        ver = r.get("verification")
        if isinstance(ver, dict):
            n += 1
        elif isinstance(ver, str) and ver.startswith(
            ("verified", "response-confirmed", "server-reread-confirmed", "PASS")
        ):
            n += 1
        elif str(r.get("status") or "").startswith(("verified", "response-confirmed")):
            n += 1
    return n


COUNTERS = {"archives": counts_archive, "drafts": counts_draft,
            "chip_clears": counts_chip_clear}


#: Every spelling of a run id that the COS surfaces actually produce:
#: `108`, `run108`, and — since MAN-01 (v5.58) told the run to take its
#: identity from the host's manifest sheet verbatim — `2026-08-09-run108`.
_RUN_TOKEN_RE = re.compile(r"(?:^|-)run(\d+)$|^(\d+)$")


def _run_token(value: object) -> str:
    """The RUN NUMBER, from whichever spelling of the id was handed over.

    WHY THIS IS NOT `lstrip("run")` ANY MORE (measured, run 108, 2026-08-09).
    MAN-01 made the run read its identity off the host's manifest sheet, whose
    `run_id` is the FULL `<date>-run<N>`. Run 108 obeyed: it stamped
    `2026-08-09-run108` into `scan_provenance.run_id` and into every ledger
    row, and invoked this checker with `--run-id 2026-08-09-run108`, which
    passed. The HOST validator re-executes the same checker with
    `cos_runverify._run_number(run_id)` — the bare `108` — and the old token
    (a leading-`run` strip, nothing more) made those two spellings unequal.
    Two consequences on one night: `scan_provenance.run_id must match
    --run-id` raised Malformed, so a genuine PASS block scored `contract:
    FAIL`; and `run_scoped_rows` matched NONE of the run's 423 ledger rows,
    which is the `OC-a-unaccounted` shape arriving from a spelling difference
    rather than from missing work.

    The run NUMBER is what every other joiner in this system already scopes on
    (`_file_run`, `canonical_block_path`, `cos_runverify._run_number`), and a
    run that writes under a foreign DATE has its own check
    (`cos_runverify.check_artifact_naming`, built for exactly that run-64
    defect) — so collapsing to the number here adds no blind spot it covers.
    A value that is no recognised spelling is returned unchanged, so it can
    still only match itself.
    """
    text = str(value).strip()
    m = _RUN_TOKEN_RE.search(text)
    return (m.group(1) or m.group(2)) if m else text


def _file_run(path: Path) -> str | None:
    m = re.search(r"-run([^.]+)\.jsonl$", path.name)
    return m.group(1) if m else None


def run_scoped_rows(ledgers: Path, glob: str, run_id: str) -> tuple[list[dict], int]:
    """Rows attributable to `run_id`, plus the count of unattributable rows.

    A row is this run's when it carries `run`/`run_id` matching, or when it
    lives in a `…-run<N>.jsonl` file for this run. A row with NO attribution in
    a file with NO run token cannot be proven to be this run's, so it is
    SKIPPED (and surfaced) — v5.27 already requires per-run attribution.
    """
    want = _run_token(run_id)
    keep: list[dict] = []
    unattributed = 0
    for path in sorted(ledgers.glob(glob)):
        file_run = _file_run(path)
        for row in _rows(path):
            rid = row.get("run", row.get("run_id"))
            if rid is not None:
                if _run_token(rid) == want:
                    keep.append(row)
            elif file_run is not None:
                if _run_token(file_run) == want:
                    keep.append(row)
            else:
                unattributed += 1
    return keep, unattributed


# --- guard stops: a stop halts action, never accounting (v5.52) --------------

def _guard_stop_shape(post: dict, enumerated: set[str]) -> dict | None:
    """The declared `guard_stop`, or None when it is absent or unusable.

    Shape only — whether the stop actually HAPPENED is decided from the run's
    own ledgers by `guard_stop_corroborated`, never from this record.
    """
    record = post.get("guard_stop")
    if not isinstance(record, dict):
        return None
    if record.get("guard") not in GUARD_STOPS:
        return None
    convid = record.get("convid")
    if not isinstance(convid, str) or convid not in enumerated:
        return None
    return record


def guard_stop_corroborated(ledgers: Path, run_id: str, guard: str) -> bool:
    """Did THIS run's own ledgers record the named guard firing?

    The stop's evidence is the reason word doctrine already requires on the row
    the guard fired on — `held_reason` on the ingestion ledger (E30(c)) or
    `action` on the action ledger. A run that declares a stop it never ledgered
    is asserting the one thing that would excuse its unaccounted rows, which is
    exactly the shape this checker refuses everywhere else.
    """
    for glob in GUARD_STOP_GLOBS:
        rows, _ = run_scoped_rows(ledgers, glob, run_id)
        for row in rows:
            if guard in (row.get("held_reason"), row.get("action")):
                return True
    return False


# --- the elected-lane pin (owner overlay, v5.52) -----------------------------

def lane_pin(ledgers: Path) -> str | None:
    """The owner's pinned browser toolset, from `overlay/cos/browser-lane.md`.

    ABSENT file, absent key, or any unrecognised value ⇒ **no pin** and the
    ordinary IAB-first election stands. Owner configuration, so it is read from
    the vault beside the ops dir and never supplied by the run — a pin the run
    could declare for itself is a pin a silent fallback can drop.
    """
    path = ledgers.parent / "overlay" / "cos" / "browser-lane.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    m = re.search(r"^pin:[ \t]*(\S+)[ \t]*$", body, re.M)
    if m is None or m.group(1) not in TOOLSETS:
        return None
    return m.group(1)


# --- input validation -------------------------------------------------------

def _load(path: Path, label: str) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Malformed(f"{label}: no such file {path}") from exc
    except json.JSONDecodeError as exc:
        raise Malformed(f"{label}: not JSON ({exc})") from exc
    if not isinstance(obj, dict):
        raise Malformed(f"{label}: expected a JSON object")
    return obj


def _require(obj: dict, key: str, kind: type, label: str):
    if key not in obj:
        raise Malformed(f"{label}: missing required key `{key}`")
    if not isinstance(obj[key], kind):
        raise Malformed(f"{label}: `{key}` must be {kind.__name__}")
    return obj[key]


def validate(pre: dict, post: dict, profile: str, run_id: str,
             pin: str | None = None) -> None:
    if profile not in PROFILES:
        raise Malformed(f"unknown --profile {profile!r} (expected one of {PROFILES})")
    declared = pre.get("run_profile")
    if declared is not None and declared != profile:
        raise Malformed(
            f"pre declares run_profile={declared!r} but --profile={profile!r}")

    _require(pre, "enumerated_at", str, "pre")
    enumerated_list = _require(pre, "enumerated", list, "pre")
    if (not all(isinstance(convid, str) and convid for convid in enumerated_list)
            or len(set(enumerated_list)) != len(enumerated_list)):
        raise Malformed("pre: enumerated must contain unique non-empty conversation ids")
    _require(pre, "pre_run_holds", dict, "pre")
    _require(post, "post_run", dict, "post")
    _require(post, "arrived_during_run", list, "post")
    _require(post, "candidates", list, "post")
    _require(post, "capabilities", dict, "post")
    _counts(pre, post)
    if _uses_new_count_schema(pre, post):
        _validate_browser_provenance(pre, post, run_id, pin)
        if profile == "full":
            _validate_sent_snapshot(pre, "pre")
            _validate_sent_snapshot(post, "post")

    for convid, bucket in post["post_run"].items():
        if bucket not in BUCKETS:
            raise Malformed(f"post: convid {convid!r} carries unknown bucket "
                            f"{bucket!r} (expected one of {BUCKETS})")
    for cap in post["capabilities"]:
        if cap not in CAPABILITIES:
            raise Malformed(f"post: unknown capability {cap!r}")
    enumerated = set(enumerated_list)
    candidate_keys: set[tuple[str, str]] = set()
    for rec in post["candidates"]:
        if not isinstance(rec, dict):
            raise Malformed("post: each candidate record must be an object")
        convid = rec.get("convid")
        if not isinstance(convid, str) or not convid:
            raise Malformed("post: each candidate record needs a non-empty `convid`")
        if convid not in enumerated:
            raise Malformed(f"post: candidate convid {convid!r} was not enumerated")
        capability = rec.get("capability")
        if capability not in CAPABILITIES:
            raise Malformed(f"post: candidate for unknown capability "
                            f"{capability!r}")
        key = (convid, capability)
        if key in candidate_keys:
            raise Malformed(f"post: duplicate candidate {key!r}")
        candidate_keys.add(key)
        if not isinstance(rec.get("eligible"), bool):
            raise Malformed("post: each candidate record needs a boolean `eligible`")


def preflight(pre: dict, profile: str, run_id: str,
              ledgers: Path | None = None) -> list[str]:
    """Validate the serialized PRE snapshot before any mailbox mutation.

    Pass `ledgers` (the ops dir) to check the owner's lane pin HERE, at 19:05,
    instead of discovering at 21:30 that the whole night ran on the wrong lane.
    """
    if profile not in PROFILES:
        raise Malformed(f"unknown --profile {profile!r} (expected one of {PROFILES})")
    if pre.get("run_profile") != profile:
        raise Malformed(
            f"pre declares run_profile={pre.get('run_profile')!r} "
            f"but --profile={profile!r}")
    _require(pre, "enumerated_at", str, "pre")
    enumerated = _require(pre, "enumerated", list, "pre")
    _require(pre, "pre_run_holds", dict, "pre")
    count = _require(pre, "inbox_conversation_count_before", int, "pre")
    folder_items = _require(pre, "owa_folder_item_count_before", int, "pre")
    if min(count, folder_items) < 0:
        raise Malformed("pre: conversation and folder-item counts must be non-negative")
    pin = lane_pin(ledgers) if ledgers is not None else None
    elected = _validate_browser_election(pre, pin)
    _validate_scan_provenance(pre, "pre", run_id, elected)
    if profile == "full":
        _validate_sent_snapshot(pre, "pre")

    reasons: list[str] = []
    if pin is not None and elected != pin:
        reasons.append("OC-lane-pin-not-honoured")
    if (not all(isinstance(convid, str) and convid for convid in enumerated)
            or len(set(enumerated)) != len(enumerated)
            or count != len(enumerated)):
        reasons.append("OC-provenance-pre-enumeration-count")
    if not _complete_enumeration(pre, count):
        reasons.append("OC-provenance-incomplete-enumeration")
    if profile == "full" and not pre["sent_zero_send"]["complete"]:
        reasons.append("ZS-incomplete")
    return reasons


# --- the contract ------------------------------------------------------------

def _sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def evaluate(pre: dict, post: dict, ledgers: Path, run_id: str, profile: str) -> dict:
    pin = lane_pin(ledgers)
    validate(pre, post, profile, run_id, pin)

    enumerated: list[str] = list(pre["enumerated"])
    enumerated_set = set(enumerated)
    pre_holds: dict = pre["pre_run_holds"]
    post_run: dict = post["post_run"]
    arrived: list[str] = list(post["arrived_during_run"])
    (conversation_before, folder_items_before, conversation_after,
     folder_items_after, legacy_counts) = _counts(pre, post)
    reasons: list[str] = []
    zero_send_proof = None
    if not legacy_counts and profile == "full":
        zero_send_proof, zero_send_reasons = _sent_zero_send(pre, post)
        reasons.extend(zero_send_reasons)

    counts = dict.fromkeys(BUCKETS, 0)
    for convid in enumerated:
        bucket = post_run.get(convid)
        if bucket in BUCKETS:
            counts[bucket] += 1
    counts["enumerated"] = len(enumerated)

    # --- provenance: the checker distrusts its own inputs -------------------
    enumeration_complete = (
        _complete_enumeration(pre, conversation_before)
        and _complete_enumeration(post, conversation_after)
    )
    if not legacy_counts and not enumeration_complete:
        reasons.append("OC-provenance-incomplete-enumeration")
    if not legacy_counts and conversation_before != len(enumerated):
        reasons.append("OC-provenance-pre-enumeration-count")
    bucket_sum = sum(counts[b] for b in BUCKETS)
    if bucket_sum != len(enumerated):
        reasons.append("OC-provenance-bucket-sum")
    # A guard-stopped row was never archived, so it is still in the Inbox and
    # counts toward residency exactly as an unaccounted one did.
    resident = (counts["held_non_drafted"] + counts["held_drafted"]
                + counts["chipped"] + counts["unaccounted"]
                + counts["stopped_by_guard"] + len(arrived))
    if resident != conversation_after:
        reasons.append("OC-provenance-residency")
    if legacy_counts and folder_items_after != conversation_after:
        reasons.append("OC-provenance-folder-count")
    stray = sorted(set(post_run) - enumerated_set - set(arrived))
    if stray:
        reasons.append("OC-provenance-unknown-convid")

    # --- clause (a): accounted, per the run's declared profile --------------
    #
    # A STOP HALTS ACTION, NEVER ACCOUNTING (v5.48 for the ingestion ledger,
    # v5.52 here). A run whose safety guard correctly ended every mutation still
    # owes a terminal bucket for every row it enumerated: `stopped_by_guard`
    # says "no disposition was written because writing one was forbidden", and
    # it is ACCOUNTED. It is not a free pass — the stop must be RECORDED, and
    # the record must be corroborated by the run's own ledgers. A row
    # unaccounted for any OTHER reason still FAILS exactly as before.
    accounted = ACCOUNTED[profile]
    unaccounted_convids = sorted(
        c for c in enumerated if post_run.get(c) not in accounted)
    if unaccounted_convids:
        reasons.append("OC-a-unaccounted")

    stopped_convids = sorted(
        c for c in enumerated if post_run.get(c) == "stopped_by_guard")
    guard_stop = _guard_stop_shape(post, enumerated_set)
    if stopped_convids and guard_stop is None:
        reasons.append("OC-guard-stop-unrecorded")
    elif stopped_convids and not guard_stop_corroborated(
            ledgers, run_id, guard_stop["guard"]):
        reasons.append("OC-guard-stop-uncorroborated")

    # The lane the owner pinned is the lane the run owes. A fallback is a named
    # failure with the elected lane on the record, never a silent lane change.
    if pin is not None and not legacy_counts:
        if pre["browser_election"]["elected"] != pin:
            reasons.append("OC-lane-pin-not-honoured")

    # `label-only` forbids archiving: an archived row is a scope violation.
    if profile == "label-only" and counts["archived"]:
        reasons.append("OC-scope-violation-archived-under-label-only")

    # --- capability liveness -------------------------------------------------
    liveness: dict[str, dict] = {}
    unattributed_total = 0
    eligible_seen = {cap: 0 for cap in CAPABILITIES}
    raw_seen = {cap: 0 for cap in CAPABILITIES}
    archive_candidates: set[str] = set()
    for rec in post["candidates"]:
        cap = rec["capability"]
        raw_seen[cap] += 1
        if cap == "archives":
            archive_candidates.add(rec["convid"])
        if rec["eligible"]:
            eligible_seen[cap] += 1
        elif not rec.get("exclusion_reason"):
            if "OC-candidate-no-exclusion-reason" not in reasons:
                reasons.append("OC-candidate-no-exclusion-reason")

    # A newly classified hold is legitimate when its archive decision was
    # reported and rejected by a safety guard. Missing that per-conversation
    # evidence is the degenerate "label everything Held" shape.
    held_total = counts["held_non_drafted"] + counts["held_drafted"]
    newly_held = {
        convid for convid in enumerated
        if convid not in pre_holds
        and post_run.get(convid) in {"held_non_drafted", "held_drafted"}
    }
    if (profile == "full" and held_total > len(pre_holds)
            and counts["archived"] == 0 and not arrived
            and not newly_held.issubset(archive_candidates)):
        reasons.append("OC-degenerate")

    for cap in CAPABILITIES:
        declared = post["capabilities"].get(cap)
        in_scope = IN_SCOPE[profile][cap]          # computed, never read
        if not isinstance(declared, dict):
            reasons.append(f"OC-liveness-missing:{cap}")
        elif declared.get("in_scope") != in_scope:
            reasons.append(f"OC-liveness-in-scope:{cap}")
        rows, unattributed = run_scoped_rows(ledgers, LEDGER_GLOB[cap], run_id)
        unattributed_total += unattributed
        output = COUNTERS[cap](rows)
        liveness[cap] = {
            "in_scope": in_scope,
            "output": output,
            "eligible_inputs": eligible_seen[cap],
            "raw_inputs": raw_seen[cap],
        }
        if in_scope and output == 0 and eligible_seen[cap] > 0:
            reasons.append(f"OC-liveness:{cap}")

    block = {
        "run_profile": profile,
        "run_id": str(run_id),
        "enumerated_at": pre["enumerated_at"],
        "enumeration_complete": enumeration_complete,
        "enumerated": enumerated,
        "pre_run_holds": pre_holds,
        "post_run": post_run,
        "counts": counts,
        "arrived_during_run": arrived,
        "inbox_conversation_count_before": conversation_before,
        "inbox_conversation_count_after": conversation_after,
        "inbox_conversation_delta": conversation_after - conversation_before,
        "owa_folder_item_count_before": folder_items_before,
        "owa_folder_item_count_after": folder_items_after,
        "owa_folder_item_delta": folder_items_after - folder_items_before,
        # Compatibility aliases for metrics readers written against v5.28.
        "inbox_count_before": conversation_before,
        "inbox_count_after": conversation_after,
        "inbox_delta": conversation_after - conversation_before,
        "split": {"archive": counts["archived"], "hold": counts["held_non_drafted"],
                  "drafted": counts["held_drafted"]},
        "unaccounted_convids": unaccounted_convids,
        "stopped_by_guard_convids": stopped_convids,
        "guard_stop": guard_stop,
        "lane": {
            "elected": (pre["browser_election"]["elected"]
                        if not legacy_counts else None),
            "pin": pin,
            "pin_honoured": (None if pin is None or legacy_counts
                             else pre["browser_election"]["elected"] == pin),
        },
        "unknown_convids": stray,
        "unattributed_ledger_rows": unattributed_total,
        "capability_liveness": liveness,
        "zero_send_proof": zero_send_proof,
        "verdict": "FAILED" if reasons else "PASS",
        "verdict_reasons": reasons,
        "verdict_source": f"tools/cos_contract.py@{_sha()}",
    }
    return block


def render(block: dict) -> str:
    c = block["counts"]
    lines = [
        f"OUTCOME CONTRACT — {block['verdict']}  "
        f"(profile {block['run_profile']}, run {block['run_id']})",
        f"  enumerated {c['enumerated']} at {block['enumerated_at']}  "
        f"archive : hold : drafted = {block['split']['archive']} : "
        f"{block['split']['hold']} : {block['split']['drafted']}",
        f"  inbox conversations {block['inbox_conversation_count_before']} -> "
        f"{block['inbox_conversation_count_after']} "
        f"(delta {block['inbox_conversation_delta']}); OWA items "
        f"{block['owa_folder_item_count_before']} -> "
        f"{block['owa_folder_item_count_after']}  "
        f"arrived_during_run {len(block['arrived_during_run'])}",
    ]
    lane = block.get("lane") or {}
    lines.append(f"  lane: elected {lane.get('elected')}  pin {lane.get('pin')}"
                 f"  honoured {lane.get('pin_honoured')}")
    for cap, v in block["capability_liveness"].items():
        lines.append(f"  {cap}: output {v['output']} / eligible {v['eligible_inputs']}"
                     f" (raw {v['raw_inputs']}, in_scope {v['in_scope']})")
    if c.get("stopped_by_guard"):
        guard = (block.get("guard_stop") or {}).get("guard")
        lines.append(f"  stopped_by_guard {c['stopped_by_guard']} "
                     f"(guard {guard}) — accounted, no disposition written")
    if block["verdict_reasons"]:
        lines.append("  FAILED clauses: " + ", ".join(block["verdict_reasons"]))
    if block["unaccounted_convids"]:
        lines.append("  unaccounted: " + ", ".join(block["unaccounted_convids"]))
    lines.append(f"  {block['verdict_source']}")
    return "\n".join(lines)


def canonical_block_path(ledgers: Path, run_id: str) -> Path | None:
    """``<ops>/cos_contract_block_<host run id>.json`` — the name the HOST reads.

    WHY THIS EXISTS (measured, runs 63-64). SKILL.md writes the CLI as
    ``--out <block.json>``: a placeholder, never a name. Runs 40-62 happened to
    choose ``cos_contract_block_<run>.json``, which is what the host validator
    reads; runs 63 and 64 chose ``outcome_contract_<run>.json`` and the
    validator correctly reported "no readable OUTCOME CONTRACT block". A file
    name that only convention pins is a file name that drifts, so the checker
    now emits the canonical copy itself and ``--out`` stays the run's own.

    The full run id comes from the HOST pointer ``cos-run-begin`` writes, so
    this artifact is immune to the local-vs-UTC date drift as well: the run may
    pass ``--run-id 64`` or ``--run-id 2026-08-01-run64``, and either way the
    block lands under the id the host froze. No pointer, or a pointer naming a
    different run, means the host cannot tell WHICH run this block belongs to —
    then only ``--out`` is written and the validator says so.
    """
    want = re.search(r"(\d+)$", str(run_id))
    if want is None:
        return None
    try:
        from brain import cos                                # noqa: PLC0415
        pointer = json.loads(
            cos.current_run_path(ledgers.parent).read_text(encoding="utf-8"))
        host_run_id = str(pointer["run_id"])
    except Exception:                                        # noqa: BLE001
        return None
    m = re.search(r"run(\d+)$", host_run_id)
    if m is None or m.group(1) != want.group(1):
        return None
    return ledgers / f"cos_contract_block_{host_run_id}.json"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pre", required=True, type=Path)
    p.add_argument("--post", type=Path)
    p.add_argument("--ledgers", type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--profile", required=True, choices=list(PROFILES))
    p.add_argument("--out", type=Path)
    p.add_argument("--preflight", action="store_true")
    args = p.parse_args(argv[1:])

    try:
        if args.preflight:
            reasons = preflight(_load(args.pre, "pre"), args.profile, args.run_id,
                                args.ledgers)
            if reasons:
                print("PRE-FLIGHT FAILED: " + ", ".join(reasons))
                return 1
            print(f"PRE-FLIGHT PASS (profile {args.profile}, run {args.run_id})")
            return 0
        if args.post is None or args.ledgers is None:
            raise Malformed("--post and --ledgers are required without --preflight")
        if not args.ledgers.is_dir():
            raise Malformed(f"--ledgers: no such directory {args.ledgers}")
        block = evaluate(_load(args.pre, "pre"), _load(args.post, "post"),
                         args.ledgers, args.run_id, args.profile)
    except Malformed as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(block, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
    canonical = canonical_block_path(args.ledgers, args.run_id)
    if canonical is not None and canonical.resolve() != (
            args.out.resolve() if args.out else None):
        canonical.write_text(payload, encoding="utf-8")
    print(render(block))
    return 1 if block["verdict"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
