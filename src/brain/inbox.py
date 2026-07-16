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
interactive ``/brain-inbox`` session ANSWERS; the next fold CONSUMES the answers
and executes them through the audited write path.
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
    return entries + [entry], True


def open_questions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e.get("status", "open") == "open"]


def record_answer(
    entries: list[dict[str, Any]], key: str, answer: str, *, answered: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Mark the open question ``key`` answered. Returns ``(entries, matched)``."""
    matched = False
    for e in entries:
        if e.get("key") == key and e.get("status", "open") == "open":
            e["status"] = "answered"
            e["answer"] = answer
            e["answered"] = answered
            matched = True
    return entries, matched


def summary_line(entries: list[dict[str, Any]]) -> str:
    """One-line push summary for the SessionStart hook (empty when the queue is
    empty). Deliberately terse — it's injected into every session."""
    n = len(open_questions(entries))
    if not n:
        return ""
    return (f"{n} owner decision(s) pending in the brain inbox — say "
            f"'brain inbox' (or run /brain-inbox) to answer them (~{n} min).")
