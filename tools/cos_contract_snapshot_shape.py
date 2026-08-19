"""Snapshot-input shape sub-checks of the outcome-contract checker.

One function per shape clause of ``cos_contract.validate`` (the candidate
records) and ``cos_contract._validate_sent_snapshot`` (the sent-proof boundary
and items). Each returns the FIRST problem found as a string, or ``None``;
the caller raises ``Malformed`` on it, so the messages stay byte-identical
with the single-function shape they were extracted from. Everything the
clauses need from the parent (``_timestamp``, the capability vocabulary,
``Malformed``) arrives as a parameter — this module never imports
``cos_contract``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable


def sent_boundary_problem(proof: dict, window: datetime, label: str,
                          parse: Callable[[object, str], datetime]) -> str | None:
    """The sent-proof boundary clause: older-than-window ordered before the
    window, list-end with a null timestamp, or a named refusal."""
    boundary = proof.get("boundary")
    boundary_value = proof.get("boundary_timestamp")
    if boundary == "older-than-window":
        if parse(boundary_value, f"{label}: sent_zero_send.boundary_timestamp") >= window:
            return f"{label}: sent boundary must be older than window_start"
        return None
    if boundary == "list-end":
        if boundary_value is not None:
            return f"{label}: list-end boundary_timestamp must be null"
        return None
    return (f"{label}: sent_zero_send.boundary must be older-than-window "
            "or list-end")


def sent_item_problem(item: object, label: str, seen: set[str],
                      window: datetime, captured: datetime,
                      parse: Callable[[object, str], datetime]) -> str | None:
    """One sent item's shape clause: object, non-empty unique item_id (recorded
    in ``seen`` exactly as the original loop did), timestamp inside the
    captured window."""
    if not isinstance(item, dict):
        return f"{label}: each sent item must be an object"
    item_id = item.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return f"{label}: each sent item needs a non-empty item_id"
    if item_id in seen:
        return f"{label}: duplicate sent item_id {item_id!r}"
    seen.add(item_id)
    timestamp = parse(item.get("timestamp"), f"{label}: sent item {item_id!r} timestamp")
    if timestamp < window or timestamp > captured:
        return (f"{label}: sent item {item_id!r} falls outside the "
                "captured window")
    return None


def candidate_shape_problem(rec: object, enumerated: set[str],
                            candidate_keys: set[tuple[str, str]],
                            capabilities: tuple[str, ...]) -> str | None:
    """One candidate record's shape clause: object, enumerated convid, known
    capability, no duplicate (convid, capability) pair, boolean ``eligible``."""
    if not isinstance(rec, dict):
        return "post: each candidate record must be an object"
    convid = rec.get("convid")
    if not isinstance(convid, str) or not convid:
        return "post: each candidate record needs a non-empty `convid`"
    if convid not in enumerated:
        return f"post: candidate convid {convid!r} was not enumerated"
    capability = rec.get("capability")
    if capability not in capabilities:
        return f"post: candidate for unknown capability {capability!r}"
    key = (convid, capability)
    if key in candidate_keys:
        return f"post: duplicate candidate {key!r}"
    candidate_keys.add(key)
    if not isinstance(rec.get("eligible"), bool):
        return "post: each candidate record needs a boolean `eligible`"
    return None
