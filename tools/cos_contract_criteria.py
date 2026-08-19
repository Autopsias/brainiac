"""Evaluate COS contract criteria."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cos_contract import (
    ACCOUNTED,
    BUCKETS,
    CAPABILITIES,
    COUNTERS,
    IN_SCOPE,
    Malformed,
    LEDGER_GLOB,
    PROFILES,
    TOOLSETS,
    _guard_stop_shape,
    _require,
    _run_token,
    guard_stop_corroborated,
    lane_pin,
    run_scoped_rows,
)


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


def _validate_sent_metadata(proof: dict, label: str) -> None:
    """Validate the identity and completeness fields of a sent snapshot."""
    if proof.get("identity_field") != "item_id":
        raise Malformed(f"{label}: sent_zero_send.identity_field must be item_id")
    if proof.get("sort") != "newest-first":
        raise Malformed(f"{label}: sent_zero_send.sort must be newest-first")
    if not isinstance(proof.get("complete"), bool):
        raise Malformed(f"{label}: sent_zero_send.complete must be bool")


def _sent_window(proof: dict, label: str) -> tuple[datetime, datetime]:
    """Parse the sent snapshot's capture window and enforce its ordering."""
    window = _timestamp(proof.get("window_start"), f"{label}: sent_zero_send.window_start")
    captured = _timestamp(proof.get("captured_at"), f"{label}: sent_zero_send.captured_at")
    if captured < window:
        raise Malformed(f"{label}: sent proof was captured before its window")
    return window, captured


def _validate_sent_boundary(
    proof: dict, label: str, window: datetime
) -> None:
    """Validate the boundary that closes a sent-message enumeration."""
    boundary = proof.get("boundary")
    boundary_value = proof.get("boundary_timestamp")
    if boundary == "older-than-window":
        if _timestamp(boundary_value, f"{label}: sent_zero_send.boundary_timestamp") >= window:
            raise Malformed(f"{label}: sent boundary must be older than window_start")
        return
    if boundary == "list-end" and boundary_value is None:
        return
    if boundary == "list-end":
        raise Malformed(f"{label}: list-end boundary_timestamp must be null")
    raise Malformed(
        f"{label}: sent_zero_send.boundary must be older-than-window or list-end"
    )


def _validate_sent_items(
    proof: dict, label: str, window: datetime, captured: datetime
) -> None:
    """Validate sent item identities and timestamps against the capture window."""
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
            item.get("timestamp"), f"{label}: sent item {item_id!r} timestamp"
        )
        if timestamp < window or timestamp > captured:
            raise Malformed(
                f"{label}: sent item {item_id!r} falls outside the captured window"
            )


def _validate_sent_snapshot(obj: dict, label: str) -> None:
    """Validate the full-profile zero-send snapshot."""
    proof = obj.get("sent_zero_send")
    if not isinstance(proof, dict):
        raise Malformed(f"{label}: new-schema full run requires `sent_zero_send`")
    _validate_sent_metadata(proof, label)
    window, captured = _sent_window(proof, label)
    _validate_sent_boundary(proof, label, window)
    _validate_sent_items(proof, label, window, captured)


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
    """Return conversation/item counts before/after and legacy status."""
    new_keys = (
        (pre, "inbox_conversation_count_before", "pre"),
        (pre, "owa_folder_item_count_before", "pre"),
        (post, "inbox_conversation_count_after", "post"),
        (post, "owa_folder_item_count_after", "post"),
    )
    if any(key in obj for obj, key, _label in new_keys):
        values = [_require(obj, key, int, label) for obj, key, label in new_keys]
        if any(value < 0 for value in values):
            raise Malformed("conversation and folder-item counts must be non-negative")
        return (*values, False)

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
    if (not isinstance(attempted, list) or not attempted
            or not all(isinstance(toolset, str) for toolset in attempted)):
        raise Malformed("pre: browser_election.attempted must be a non-empty string list")
    if attempted[0] not in ({"iab", pin} if pin else {"iab"}):
        raise Malformed("pre: browser_election must attempt iab first"
                        if pin is None else
                        f"pre: browser_election must attempt the pinned {pin!r} or iab first")
    if len(set(attempted)) != len(attempted) or any(
            toolset not in TOOLSETS for toolset in attempted):
        raise Malformed("pre: browser_election contains an invalid or repeated toolset")
    if elected not in TOOLSETS or elected != attempted[-1]:
        raise Malformed("pre: browser_election.elected must be the final attempted toolset")
    return elected


def _validate_scan_provenance(
    obj: dict, label: str, run_id: str, elected: str
) -> None:
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


def _validate_browser_provenance(
    pre: dict, post: dict, run_id: str, pin: str | None = None
) -> str:
    """Require fresh same-lane scans and IAB-first election."""
    elected = _validate_browser_election(pre, pin)
    for label, obj in (("pre", pre), ("post", post)):
        _validate_scan_provenance(obj, label, run_id, elected)
    return elected


def _validate_snapshot_fields(
    pre: dict,
    post: dict,
    profile: str,
    run_id: str,
    pin: str | None,
) -> list[str]:
    """Validate required snapshot fields and return enumerated conversation ids."""
    if profile not in PROFILES:
        raise Malformed(f"unknown --profile {profile!r} (expected one of {PROFILES})")
    declared = pre.get("run_profile")
    if declared is not None and declared != profile:
        raise Malformed(f"pre declares run_profile={declared!r} but --profile={profile!r}")

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
    return enumerated_list


def _validate_post_run(post: dict) -> None:
    """Validate the post-run bucket and capability vocabularies."""
    for convid, bucket in post["post_run"].items():
        if bucket not in BUCKETS:
            raise Malformed(f"post: convid {convid!r} carries unknown bucket {bucket!r} "
                            f"(expected one of {BUCKETS})")
    for cap in post["capabilities"]:
        if cap not in CAPABILITIES:
            raise Malformed(f"post: unknown capability {cap!r}")


def _validate_candidates(post: dict, enumerated_list: list[str]) -> None:
    """Validate candidate records against the enumerated conversation set."""
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
            raise Malformed(f"post: candidate for unknown capability {capability!r}")
        key = (convid, capability)
        if key in candidate_keys:
            raise Malformed(f"post: duplicate candidate {key!r}")
        candidate_keys.add(key)
        if not isinstance(rec.get("eligible"), bool):
            raise Malformed("post: each candidate record needs a boolean `eligible`")


def validate(
    pre: dict,
    post: dict,
    profile: str,
    run_id: str,
    pin: str | None = None,
) -> None:
    """Validate the serialized pre/post contract shape."""
    enumerated = _validate_snapshot_fields(pre, post, profile, run_id, pin)
    _validate_post_run(post)
    _validate_candidates(post, enumerated)


def _bucket_counts(enumerated: list[str], post_run: dict) -> dict:
    """Count post-run buckets for the enumerated conversation set."""
    counts = dict.fromkeys(BUCKETS, 0)
    for convid in enumerated:
        bucket = post_run.get(convid)
        if bucket in BUCKETS:
            counts[bucket] += 1
    counts["enumerated"] = len(enumerated)
    return counts


def _provenance_checks(
    pre: dict,
    post: dict,
    enumerated: list[str],
    counts: dict,
    ledgers: Path,
    run_id: str,
    profile: str,
    pin: str | None,
) -> dict:
    """Evaluate accounting, guard-stop, and lane provenance criteria."""
    post_run = post["post_run"]
    arrived = post["arrived_during_run"]
    conversation_before, folder_items_before, conversation_after, folder_items_after, legacy = _counts(pre, post)
    reasons: list[str] = []
    enumeration_complete = (_complete_enumeration(pre, conversation_before)
                            and _complete_enumeration(post, conversation_after))
    if not legacy and not enumeration_complete:
        reasons.append("OC-provenance-incomplete-enumeration")
    if not legacy and conversation_before != len(enumerated):
        reasons.append("OC-provenance-pre-enumeration-count")
    if sum(counts[bucket] for bucket in BUCKETS) != len(enumerated):
        reasons.append("OC-provenance-bucket-sum")
    resident = (counts["held_non_drafted"] + counts["held_drafted"]
                + counts["chipped"] + counts["unaccounted"]
                + counts["stopped_by_guard"] + len(arrived))
    if resident != conversation_after:
        reasons.append("OC-provenance-residency")
    if legacy and folder_items_after != conversation_after:
        reasons.append("OC-provenance-folder-count")
    enumerated_set = set(enumerated)
    stray = sorted(set(post_run) - enumerated_set - set(arrived))
    if stray:
        reasons.append("OC-provenance-unknown-convid")
    unaccounted = sorted(
        convid for convid in enumerated if post_run.get(convid) not in ACCOUNTED[profile]
    )
    if unaccounted:
        reasons.append("OC-a-unaccounted")
    stopped = sorted(convid for convid in enumerated if post_run.get(convid) == "stopped_by_guard")
    guard_stop = _guard_stop_shape(post, enumerated_set)
    if stopped and guard_stop is None:
        reasons.append("OC-guard-stop-unrecorded")
    elif stopped and not guard_stop_corroborated(ledgers, run_id, guard_stop["guard"]):
        reasons.append("OC-guard-stop-uncorroborated")
    if pin is not None and not legacy and pre["browser_election"]["elected"] != pin:
        reasons.append("OC-lane-pin-not-honoured")
    if profile == "label-only" and counts["archived"]:
        reasons.append("OC-scope-violation-archived-under-label-only")
    return {
        "reasons": reasons,
        "enumeration_complete": enumeration_complete,
        "conversation_before": conversation_before,
        "folder_items_before": folder_items_before,
        "conversation_after": conversation_after,
        "folder_items_after": folder_items_after,
        "legacy_counts": legacy,
        "stray": stray,
        "unaccounted_convids": unaccounted,
        "stopped_convids": stopped,
        "guard_stop": guard_stop,
    }


def _candidate_inputs(post: dict) -> tuple[dict[str, int], dict[str, int], set[str], list[str]]:
    """Count candidate inputs and retain archive candidates for degeneracy checks."""
    eligible_seen = {capability: 0 for capability in CAPABILITIES}
    raw_seen = {capability: 0 for capability in CAPABILITIES}
    archive_candidates: set[str] = set()
    reasons: list[str] = []
    for rec in post["candidates"]:
        capability = rec["capability"]
        raw_seen[capability] += 1
        if capability == "archives":
            archive_candidates.add(rec["convid"])
        if rec["eligible"]:
            eligible_seen[capability] += 1
        elif not rec.get("exclusion_reason") and "OC-candidate-no-exclusion-reason" not in reasons:
            reasons.append("OC-candidate-no-exclusion-reason")
    return eligible_seen, raw_seen, archive_candidates, reasons


def _is_degenerate(
    profile: str,
    counts: dict,
    pre_holds: dict,
    enumerated: list[str],
    post_run: dict,
    arrived: list[str],
    archive_candidates: set[str],
) -> bool:
    """Detect a full run that held new items without an archive candidate."""
    held_total = counts["held_non_drafted"] + counts["held_drafted"]
    newly_held = {convid for convid in enumerated
                  if convid not in pre_holds
                  and post_run.get(convid) in {"held_non_drafted", "held_drafted"}}
    return (profile == "full" and held_total > len(pre_holds)
            and counts["archived"] == 0 and not arrived
            and not newly_held.issubset(archive_candidates))


def _liveness_checks(
    post: dict,
    ledgers: Path,
    run_id: str,
    profile: str,
    eligible_seen: dict[str, int],
    raw_seen: dict[str, int],
) -> tuple[dict[str, dict], int, list[str]]:
    """Evaluate per-capability declarations and ledger output."""
    liveness: dict[str, dict] = {}
    unattributed_total = 0
    reasons: list[str] = []
    for capability in CAPABILITIES:
        declared = post["capabilities"].get(capability)
        in_scope = IN_SCOPE[profile][capability]
        if not isinstance(declared, dict):
            reasons.append(f"OC-liveness-missing:{capability}")
        elif declared.get("in_scope") != in_scope:
            reasons.append(f"OC-liveness-in-scope:{capability}")
        rows, unattributed = run_scoped_rows(ledgers, LEDGER_GLOB[capability], run_id)
        unattributed_total += unattributed
        output = COUNTERS[capability](rows)
        liveness[capability] = {"in_scope": in_scope, "output": output,
                                "eligible_inputs": eligible_seen[capability],
                                "raw_inputs": raw_seen[capability]}
        if in_scope and output == 0 and eligible_seen[capability] > 0:
            reasons.append(f"OC-liveness:{capability}")
    return liveness, unattributed_total, reasons


from cos_contract_criteria_2 import evaluate  # noqa: E402


__all__ = [
    "_complete_enumeration",
    "_counts",
    "_sent_zero_send",
    "_timestamp",
    "_uses_new_count_schema",
    "_validate_browser_election",
    "_validate_browser_provenance",
    "_validate_scan_provenance",
    "_validate_sent_snapshot",
    "evaluate",
    "validate",
]
