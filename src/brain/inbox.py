"""Owner question queue (Tier-2) — the PUSH replacement for pull-based hot.md
"owner input needed" entries.

Field redesign (2026-07-13): the owner will not read hot.md / brief-latest.html
by hand. Findings a competent curator model can decide are auto-resolved in the
weekly synthesis session (Tier 1, act+log). Only GENUINELY owner-only decisions
— credentials/spend, deletion of a possibly-sole-copy, real business calls, and
anything a Tier-1 pass self-assesses as low-confidence — land here as a
STRUCTURED question: exactly one decidable question with enumerated options and
a stated default. Never "review this bucket by hand".

Stored as JSONL at ``<vault>/.brain/memory/inbox.jsonl`` (host-only, never
indexed). The headless synthesis session ENQUEUES (it cannot ask); an
interactive session ANSWERS by ASKING the owner (AskUserQuestion) and writing
the reply back here; the next fold CONSUMES the answers and executes them
through the audited write path. The owner is never sent to a command to go and
read a queue — the queue is a write-back store, not a destination.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

REQUIRED_FIELDS = ("question", "options", "default")


class QuestionShapeError(ValueError):
    """A queued question violated the options+default invariant."""


def question_key(source: str, question: str) -> str:
    """Stable idempotency key for a (source, question) pair — so re-running the
    enqueuing fold doesn't stack duplicate questions."""
    return hashlib.sha256(f"{source}\n{question}".encode("utf-8")).hexdigest()[:12]


def validate_question(q: dict[str, Any]) -> None:
    """Enforce the acceptance invariant: every queued entry has a non-empty
    question, >=2 enumerated options, and a default that is one of them."""
    for k in REQUIRED_FIELDS:
        if not q.get(k):
            raise QuestionShapeError(f"question missing required field: {k!r}")
    opts = q["options"]
    if not isinstance(opts, list) or len(opts) < 2:
        raise QuestionShapeError("a queued question needs >= 2 enumerated options")
    if q["default"] not in opts:
        raise QuestionShapeError("the default must be one of the options")


def parse_inbox(text: str) -> list[dict[str, Any]]:
    """Parse the JSONL queue; a blank/corrupt line is dropped, never raised."""
    out: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def render_inbox(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    return "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n"


def enqueue(
    entries: list[dict[str, Any]], question: dict[str, Any], *,
    created: str, source: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Validate and append one question, unless an OPEN entry with the same key
    already exists (idempotent). Returns ``(entries, appended)``."""
    validate_question(question)
    key = question.get("key") or question_key(source, question["question"])
    for e in entries:
        if e.get("key") == key and e.get("status", "open") == "open":
            return entries, False
    entry = {
        "key": key,
        "created": created,
        "source": source,
        "question": question["question"],
        "options": list(question["options"]),
        "default": question["default"],
        "context": question.get("context", ""),
        "status": "open",
        "answer": None,
        "answered": None,
    }
    # At most ONE expired record per key survives the re-ask. A lane that
    # re-asks after expiry (the cross-tier exception batch, whose TTL frees the
    # slot without deciding anything) added one dead row per TTL cycle, forever,
    # to a file whose whole point is a ~5-question queue. The NEWEST expiry is
    # kept — it is the visible evidence that the slot was freed — and the ones
    # before it are dropped. Only `expired`: an `answered` entry may still be
    # waiting to be CONSUMED (the COS broker reads answered entries out of this
    # file), so those are never touched.
    seen = [i for i, e in enumerate(entries)
            if e.get("key") == key and e.get("status") == "expired"]
    stale = set(seen[:-1])
    return [e for i, e in enumerate(entries) if i not in stale] + [entry], True


def open_questions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e.get("status", "open") == "open"]


def record_answer(
    entries: list[dict[str, Any]], key: str, answer: str, *, answered: str,
    answered_at: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Mark the open question ``key`` answered. Returns ``(entries, matched)``.

    ``answered`` stays the human-facing DATE. ``answered_at`` is the durable
    full timestamp of the human act — consumers judge answer TIMELINESS on it
    (HARDENED:codex-7), never on when they happen to run, so an answer given
    inside a deadline is not lost because the consumer fired after it."""
    import datetime as _dt

    stamp = answered_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    matched = False
    for e in entries:
        if e.get("key") == key and e.get("status", "open") == "open":
            e["status"] = "answered"
            e["answer"] = answer
            e["answered"] = answered
            e["answered_at"] = stamp
            matched = True
    return entries, matched


def expire_questions(
    entries: list[dict[str, Any]], keys: set[str], *, expired: str,
) -> tuple[list[dict[str, Any]], int]:
    """Close the OPEN questions in ``keys``. Returns ``(entries, closed)``.

    Expiry is what keeps the ~5-cap queue from silting up with slots nobody
    answered, and it also refuses a LATE answer at the queue level, since
    :func:`record_answer` only touches ``open`` entries. Whether the underlying
    CONDITION is thereby settled is the caller's business and not the same
    question: the COS broker treats an expired batch as decided, the
    cross-tier exception batch does not."""
    closed = 0
    for e in entries:
        if e.get("key") in keys and e.get("status", "open") == "open":
            e["status"] = "expired"
            e["expired"] = expired
            closed += 1
    return entries, closed


def summary_line(entries: list[dict[str, Any]]) -> str:
    """One-line push summary for the SessionStart hook (empty when the queue is
    empty). Deliberately terse — it's injected into every session."""
    n = len(open_questions(entries))
    if not n:
        return ""
    return (f"{n} owner decision(s) pending — the assistant will walk you "
            f"through them (~{n} min).")
