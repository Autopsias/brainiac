"""Snapshot-shape validation of `cos_contract` — PRE/POST provenance, zero-send and count-schema checks (batch-2 drain).

Moved verbatim out of `cos_contract`, including `Malformed`; the parent
re-imports every name, so `cc.Malformed` keeps its identity and the parent's
`validate`/`preflight`/`evaluate` call these through the re-imported bindings.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_contract_ledger_scan import TOOLSETS, _run_token  # noqa: E402
from cos_contract_snapshot_shape import (  # noqa: E402
    sent_boundary_problem, sent_item_problem)


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

    problem = sent_boundary_problem(proof, window, label, _timestamp)
    if problem is not None:
        raise Malformed(problem)

    items = proof.get("items")
    if not isinstance(items, list):
        raise Malformed(f"{label}: sent_zero_send.items must be a list")
    seen: set[str] = set()
    for item in items:
        problem = sent_item_problem(item, label, seen, window, captured,
                                    _timestamp)
        if problem is not None:
            raise Malformed(problem)


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
