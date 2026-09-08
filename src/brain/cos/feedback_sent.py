"""PEN 3 — what the owner actually SENT, against the draft the porter wrote.

WHY THIS FILE EXISTS. `cos_signals`' module docstring named the ceiling in as
many words: *"the driver enumerates `sentitems` but keeps only
`{item_id, timestamp}` and drops `ConversationId`, so the two cannot be
joined."* That enumeration exists for the ZERO-SEND PROOF, so the one artifact
that can say whether a draft was any good — the reply the owner really sent —
was read every night and thrown away. This is the cheapest feedback in the
system: the owner pays nothing for it, because he was going to send the mail
anyway.

WHAT IT RECORDS, AND WHAT IT DELIBERATELY DOES NOT. A diff class and a change
size, keyed on the conversation, into `sent-diff.jsonl` beside the feedback
record — NOT a thread ruling in `feedback.jsonl`. That is a decision, not an
oversight: the thread record's `wrong` verdict is a PERMANENT do-not-touch and
its `right` verdict RELEASES an earlier one, so mapping "the owner rewrote my
draft" onto either would either retire a live thread forever or lift a hold he
never lifted. A rewritten draft says the DRAFTING was poor; it says nothing
about whether the thread should be touched. Draft quality is a measurement, and
it is filed as one.

NO MAIL TEXT EVER LANDS HERE. Sizes, a ratio and a class — the same discipline
as the feedback record, which stores subject HASHES and no bodies.

THE PEN-2 RULE APPLIES UNCHANGED: a missing or unreadable body records NOTHING,
never `sent-as-is`. An empty capture is not an unedited draft, and the class
that would be minted from silence is the most flattering one there is.
"""
from __future__ import annotations

import difflib
from collections import Counter

from ._shared import *  # noqa: F401,F403
from ._io import _append_jsonl
from ._layout import _ts, _utcnow
from .feedback import feedback_dir, thread_digest

SENT_DIFF_SCHEMA = "cos-sent-diff/1"

#: The four answers, ordered most-used to least. `not-used` is NOT "no draft
#: existed" — that row is never written at all; it is "he had a draft and wrote
#: something else entirely", which is the strongest negative signal the lane can
#: produce and the one worth acting on.
SENT_AS_IS, LIGHT_EDIT, REWRITTEN, NOT_USED = (
    "sent-as-is", "light-edit", "rewritten", "not-used")
SENT_DIFF_CLASSES = (SENT_AS_IS, LIGHT_EDIT, REWRITTEN, NOT_USED)

#: Word-overlap ratio floors, measured on the SENT text with the quoted chain
#: removed. Chosen to be legible rather than tuned — nothing has been fitted to
#: this owner yet, because nothing has been captured yet. The next review moves
#: them against real rows, and the raw `similarity` rides every row so it can.
LIGHT_EDIT_FLOOR = 0.70
REWRITTEN_FLOOR = 0.20

#: The porter stamps every draft it saves (`[cos:<run>:<digest>]`). It is not
#: the owner's words and must not count as agreement or as an edit.
_SIGNATURE = re.compile(r"\[cos:[^\]\n]{0,120}\]")

#: Where the newest message's own words END. Same shapes as
#: `cos_signals._QUOTE_START` — a sent reply reproduces the thread below itself,
#: and diffing a draft against the whole chain scores every reply as a rewrite.
#: Duplicated rather than imported on purpose: `cos_signals` is a driver-side
#: tool module and this is the library, and a one-way import is worth more here
#: than the four lines it saves.
_QUOTE_START = re.compile(
    r"(?mi)^\s*(?:From|De|Von|Van)\s*:\s"
    r"|^\s*Begin forwarded message\s*:"
    r"|^\s*-{2,}\s*(?:Original Message|Mensagem original|Mensaje original)"
    r"|^\s*_{10,}\s*$"
    r"|^\s*>",
)


def own_words(text: Any) -> str:
    """The message's own words: above the quoted chain, minus the cos stamp."""
    body = _SIGNATURE.sub(" ", str(text or ""))
    m = _QUOTE_START.search(body)
    return (body[:m.start()] if m else body).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[0-9a-zA-Zà-ÿÀ-Ÿ]+", text.casefold())


def classify_sent_draft(draft_text: Any, sent_text: Any) -> dict[str, Any] | None:
    """`{class, similarity, changed_words, draft_words, sent_words}`, or `None`.

    `None` — RECORD NOTHING — whenever either side has no words to compare:
    an unreadable body, a body that was never fetched, a draft the run did not
    store. See the module docstring: silence is not `sent-as-is`.
    """
    a, b = _words(own_words(draft_text)), _words(own_words(sent_text))
    if not a or not b:
        return None
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    ratio = sm.ratio()
    if a == b:
        cls = SENT_AS_IS
    elif ratio >= LIGHT_EDIT_FLOOR:
        cls = LIGHT_EDIT
    elif ratio >= REWRITTEN_FLOOR:
        cls = REWRITTEN
    else:
        cls = NOT_USED
    return {"diff_class": cls, "similarity": round(ratio, 4),
            "changed_words": len(a) + len(b) - 2 * matched,
            "draft_words": len(a), "sent_words": len(b)}


def sent_draft_diffs(sent_bodies: list[dict[str, Any]],
                     drafts: dict[str, str]) -> dict[str, Any]:
    """PURE. Classify each captured sent body against the porter draft for its
    conversation, and say why every skipped one was skipped.

    `sent_bodies` are the driver's `sent_bodies` rows (`conv_id`, `text`, and
    whatever the fetch reported); `drafts` is `{conversation_id: draft text}`
    for the conversations the undo ledger says this lane drafted — the gate the
    item asks for, applied by the CALLER so this function stays pure.
    """
    out: dict[str, Any] = {"rows": [], "skipped": []}
    for item in sent_bodies or []:
        cid = str(item.get("conv_id") or item.get("conversation_id") or "")
        def skip(why: str) -> None:
            out["skipped"].append({"conversation_id": cid, "reason": why})
        if not cid:
            skip("the sent item carries no conversation id")
            continue
        if cid not in drafts:
            skip("no porter draft on this conversation")
            continue
        got = classify_sent_draft(drafts[cid], item.get("text"))
        if got is None:
            skip("the sent body was missing or unreadable — "
                 "an unreadable body is not an unedited draft")
            continue
        out["rows"].append(dict(got, conversation_id=cid,
                                item_id=str(item.get("item_id") or "")))
    return out


def sent_diff_path(vault: Any = None) -> Path:
    """`sent-diff.jsonl`, beside `feedback.jsonl` in the host-private dir."""
    return feedback_dir(vault) / "sent-diff.jsonl"


def record_sent_diffs(vault: Any, sent_bodies: list[dict[str, Any]],
                      drafts: dict[str, str], *, run: str = "",
                      now: _dt.datetime | None = None) -> dict[str, Any]:
    """PEN 3: run the diff and append one row per classified sent reply."""
    diff = sent_draft_diffs(sent_bodies, drafts)
    stamp = _ts(now or _utcnow())
    path = sent_diff_path(vault)
    for row in diff["rows"]:
        _append_jsonl(path, {
            "schema": SENT_DIFF_SCHEMA, "ts": stamp, "run": str(run),
            "conversation_id_digest": thread_digest(row["conversation_id"]),
            "item_id": row["item_id"], "diff_class": row["diff_class"],
            "similarity": row["similarity"],
            "changed_words": row["changed_words"],
            "draft_words": row["draft_words"], "sent_words": row["sent_words"],
        }, vault=vault)
    return {"path": str(path), "recorded": len(diff["rows"]),
            "rows": diff["rows"], "skipped": diff["skipped"],
            "classes": dict(Counter(r["diff_class"] for r in diff["rows"]))}


__all__ = ["SENT_DIFF_SCHEMA", "SENT_DIFF_CLASSES", "LIGHT_EDIT_FLOOR",
           "REWRITTEN_FLOOR", "own_words", "classify_sent_draft",
           "sent_draft_diffs", "sent_diff_path", "record_sent_diffs"]
