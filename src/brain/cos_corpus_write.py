"""Corpus writers: append_thread, close_run, reopen_run (CAP-01/CAP-02)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any

from . import config, cos, provenance

# -- writing ------------------------------------------------------------------
def append_thread(vault, run_id: Any, *, conversation_id: Any, text: str,
                  sender: Any = None, sent: Any = None, subject: Any = None,
                  read_lane: Any = None, body_opened: Any = None,
                  extraction: Any = None,
                  now: _dt.datetime | None = None) -> dict[str, Any]:
    """Append ONE thread's captured text to ``run_id``'s corpus.

    Usable with no browser and no mailbox anywhere in the call: the replay
    harness and the fixtures build corpora through this same function, which is
    the only way a synthetic corpus and a real one can be the same artifact.

    ``conversation_id`` is the join key back to
    ``_cos_ingestion_ledger_<run>.jsonl`` — the ledger keeps the verdict, this
    keeps the input, and the pair is what makes a verdict re-checkable.
    """
    rid = _corpus_run_id(run_id, now=now)
    cid = str(conversation_id or "").strip()
    if not cid:
        raise CorpusRefused(
            "a corpus row needs a conversation id — it is the only key that "
            "joins this text back to the verdict the ledger recorded for it")
    if len(cid) > MAX_FIELD_CHARS:
        raise CorpusRefused(
            f"conversation id is {len(cid)} characters, over the "
            f"{MAX_FIELD_CHARS}-character field cap")
    body = str(text or "")
    n = len(body.encode("utf-8"))
    if n > MAX_TEXT_BYTES:
        raise CorpusRefused(
            f"thread {cid[:40]!r} carries {n} bytes of text, over the "
            f"{MAX_TEXT_BYTES}-byte per-row cap. Refused rather than "
            f"truncated: a shortened row is a corpus that lies about what the "
            f"judge read.")
    if body_opened is not None and not isinstance(body_opened, bool):
        raise CorpusRefused(
            f"body_opened must be a boolean, not "
            f"{type(body_opened).__name__} — it is read as a fact about what "
            f"the run did, and a truthy string is not that fact.")
    if extraction is not None:
        _bounded_json(extraction, field="extraction")
    p = corpus_path(vault, rid)
    _ensure(vault)
    row: dict[str, Any] = {
        "schema": CORPUS_SCHEMA,
        "run": rid,
        "classification": CLASSIFICATION,
        "conversation_id": cid,
        "captured": cos.timestamp(now),
        "text": body,
        "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "chars": len(body),
        "provenance": {"sender": _opt(sender, field="sender"),
                       "sent": _opt(sent, field="sent"),
                       "subject": _opt(subject, field="subject")},
        "secret_findings": provenance.secret_findings(body),
    }
    for key, val in (("read_lane", _opt(read_lane, field="read_lane")),
                     ("body_opened", body_opened),
                     ("extraction", extraction)):
        if val is not None:
            row[key] = val
    try:
        cos.append_jsonl(p, row)
    except PermissionError:
        # THE 0400 MODE IS THE ENFORCEMENT of write-once, not a check a caller
        # has to remember. Past the close the kernel refuses the open; this
        # translates the refusal into the reason rather than a bare EACCES the
        # caller would read as a disk problem.
        raise CorpusClosed(
            f"the corpus for {rid} is read-only — it was closed") from None
    config.secure_file_permissions(p, 0o600)
    return row


def _opt(value: Any, *, field: str) -> str | None:
    """A field as a trimmed string, or ``None`` when the run had none. An
    absent value is recorded as absent — never as ``"unknown"``, the
    placeholder shape the category lane already had to outlaw."""
    s = str(value).strip() if value is not None else ""
    if len(s) > MAX_FIELD_CHARS:
        raise CorpusRefused(
            f"{field} is {len(s)} characters, over the "
            f"{MAX_FIELD_CHARS}-character field cap")
    return s or None


def _bounded_json(value: Any, *, field: str) -> None:
    """``value`` must be strict JSON and fit the per-field cap ENCODED.

    ``allow_nan=False`` because Python's default writes the bare tokens ``NaN``
    and ``Infinity``, which are not JSON — one optional browser field could
    make an otherwise valid corpus unreadable to any strict replay.
    """
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False)
    except (TypeError, ValueError):
        raise CorpusRefused(
            f"{field} is not strict-JSON-serialisable; the corpus stores rows "
            f"as JSONL and a row it cannot write is a row it must refuse."
        ) from None
    n = len(encoded.encode("utf-8"))
    if n > MAX_FIELD_CHARS:
        raise CorpusRefused(
            f"{field} encodes to {n} bytes, over the {MAX_FIELD_CHARS}-byte "
            f"field cap")


def close_run(vault, run_id: Any, *, now: _dt.datetime | None = None
              ) -> dict[str, Any]:
    """Close ``run_id``'s corpus: append the close record, drop it to 0400.

    The close record is what makes a crashed capture stage distinguishable
    from a genuinely quiet night — an unclosed corpus says the run died, a
    closed one carrying ``rows: 0`` says there was nothing to read. The count
    is not a security claim; the independent check on it is the run's own
    ledger (wir-03), which counted the same threads from the other side.

    A close carrying rows is FINAL. A close carrying zero certified nothing and
    can be retracted — see :func:`reopen_run`, which exists because run 68 made
    exactly that close six minutes before its lane recovered.
    """
    rid = _corpus_run_id(run_id, now=now)
    p = corpus_path(vault, rid)
    rows, close, _ = _read(p, rid)
    if close is not None:
        raise CorpusClosed(f"the corpus for {rid} is already closed")
    _ensure(vault)
    record = {"schema": CLOSE_SCHEMA, "run": rid,
              "classification": CLASSIFICATION,
              "closed": cos.timestamp(now), "rows": len(rows)}
    cos.append_jsonl(p, record)
    config.secure_file_permissions(p, 0o400)
    return record


def reopen_run(vault, run_id: Any, *, now: _dt.datetime | None = None
               ) -> dict[str, Any]:
    """Retract a close that certified ZERO rows. Never one that certified rows.

    MEASURED, run 68 (2026-08-03, real mailbox). At 21:24:58 the browser lane
    hit a transient tab-binding failure, the run concluded the body pass could
    not run and closed the corpus with ``rows: 0`` — the doctrine-sanctioned way
    to record a quiet night. Six minutes later the lane recovered and the run
    opened THREE real message bodies. Every one was refused ``CorpusClosed``.
    One transient hiccup permanently destroyed the night's capture.

    THE ASYMMETRY IS THE WHOLE DESIGN. **A close certifying zero rows certifies
    nothing**: there is no denominator to invalidate, no replay whose scope
    could silently change, no ledger row it contradicts. That is the only case
    where retracting it is safe, and it is exactly the case that bit run 68.
    A close carrying ONE OR MORE rows is final — a replay may already have been
    run against that count, and there is no ``--force``.

    Append-only, so nothing is deleted: the false close stays on disk, this
    record lands after it, and ``_read`` reads the LAST lifecycle record. A
    later reader SEES the night had a premature close rather than inferring it.
    Closing again records the TRUE row count.
    """
    rid = _corpus_run_id(run_id, now=now)
    p = corpus_path(vault, rid)
    _, close, bad = _read(p, rid)
    if close is None:
        raise CorpusRefused(
            f"the corpus for {rid} is not closed, so there is nothing to "
            f"reopen — just keep appending")
    # The DECLARED count is the authoritative one: it is what a replay read.
    # Gating on how many rows parse RIGHT NOW would let a torn line retract a
    # close that certified rows — the one thing this asymmetry rests on. A file
    # we cannot fully read is not one to make writable again either.
    declared = close.get("rows")
    if declared != 0 or bad:
        detail = (f"its close certified {declared!r} row(s)" if declared != 0
                  else f"it has {bad} unreadable line(s)")
        raise CorpusRefused(
            f"refused to reopen {rid}: {detail}. Only a fully readable close "
            f"carrying ZERO rows certified nothing and can be retracted; a "
            f"count a replay may already have used is final. Capture the rest "
            f"of this night under a new run id.")
    record = {"schema": REOPEN_SCHEMA, "run": rid,
              "classification": CLASSIFICATION,
              "reopened": cos.timestamp(now),
              "retracted": close.get("closed"),
              "reason": f"the close certified {declared} rows, so it "
                        f"certified nothing"}
    config.secure_file_permissions(p, 0o600)
    try:
        cos.append_jsonl(p, record)
    except Exception:
        # A reopen that failed HALFWAY would leave a writable file whose last
        # lifecycle record is still a close — rows appending past a count that
        # says zero. Put the mode back; the mode is the enforcement.
        config.secure_file_permissions(p, 0o400)
        raise
    return record


# -- retention ----------------------------------------------------------------

# Parent-namespace binds, deferred past this module's own defs (circular-import
# safety, whichever of brain.cos_corpus / brain.cos_corpus_write loads first).
from .cos_corpus import (  # noqa: E402
    CLASSIFICATION as CLASSIFICATION,
    CorpusClosed as CorpusClosed,
    CorpusRefused as CorpusRefused,
    MAX_FIELD_CHARS as MAX_FIELD_CHARS,
    MAX_TEXT_BYTES as MAX_TEXT_BYTES,
    _corpus_run_id as _corpus_run_id,
    _ensure as _ensure,
    _read as _read,
    CLOSE_SCHEMA as CLOSE_SCHEMA,
    CORPUS_SCHEMA as CORPUS_SCHEMA,
    REOPEN_SCHEMA as REOPEN_SCHEMA,
    corpus_path as corpus_path,
)
