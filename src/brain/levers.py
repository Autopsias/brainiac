"""Parked COS/engine switches we keep on purpose, each with an owner and a review date.

Lives in the package, never on the vault mount: the Cowork VM can write the mount.
``alerts.lever_alerts`` reminds the owner once a review date passes.
"""
from __future__ import annotations

from typing import NamedTuple


class Lever(NamedTuple):
    name: str
    kind: str  # "setting" | "function"
    owner: str
    reason: str
    review_date: str  # ISO date


_DEFAULT_REVIEW = "2027-02-14"
_AUTOCAP = "COS auto-cap graduation knob, kept parked until real verdict volume exists"

LEVERS: tuple[Lever, ...] = (
    Lever("BRAIN_COS_AUTOCAP_MIN_VOLUME", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_COS_AUTOCAP_MIN_LOWER_BOUND", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_COS_AUTOCAP_UNDO_HOURS", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_COS_AUTOCAP_EXPLORATION_K", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_COS_AUTOCAP_WINDOW_DAYS", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_COS_AUTOCAP_WINDOW_VERDICTS", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_COS_AUTOCAP_BULK_MAX_BATCH", "setting", "owner", _AUTOCAP, _DEFAULT_REVIEW),
    Lever("BRAIN_DAILY_NOTE", "setting", "owner",
          "opt-in signed daily note in the publish fold, off by default", _DEFAULT_REVIEW),
    Lever("BRAIN_MULTI_GUARD", "setting", "owner",
          "opt-in correlated-vote guard in retrieval, off by default", _DEFAULT_REVIEW),
    Lever("COS_DRAFT_LABELS", "setting", "owner",
          "nightly draft-label step: set to off to stop it; the review date is when to check "
          "the labels are worth the nightly model call", "2026-12-20"),
    Lever("COS_ARCHIVE_REPLY_SIGNAL", "setting", "owner",
          "nightly archive-reply record (record only, no review effect): set to off to stop it; "
          "the review date is when to check the rows are worth keeping", "2026-12-20"),
    Lever("rebuild_all", "function", "owner",
          "spine.rebuild_all: manual full spine rebuild, no scheduled caller", _DEFAULT_REVIEW),
)
