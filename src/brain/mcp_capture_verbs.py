"""Implement the MCP ``capture`` verb body (closed-stacks S04, DESK-04).

Split out as its own leaf module for the same reason
:mod:`brain.mcp_housekeeping_verbs` is one: a single function belongs beside
its own docstring, not folded into the read-verb file at the file-size
ratchet ceiling. Same shape as that module — at module scope this imports
only the stdlib typing helper, and lazily inside the function the modules it
delegates to.

**Host-side staging, unconditionally.** The tool body below calls
``core.capture()`` directly — the SAME unified verb the CLI's ``brain
capture`` runs (:mod:`brain.core._briefing`). That function already branches
on ``core.role``: on the HOST it writes + signs + indexes immediately; on the
VM it calls ``draft_capture()``, which stages a plain file under
``config.capture_inbox_dir(core.vault)``. The broker process that RUNS this
body is always the HOST process — a Cowork session reaches it over the
channel Claude Desktop sets up, never by importing this module into the
sandbox — so ``core.vault`` here is always the host's own vault path, not
anything mounted into the VM. Wiring capture through this seam is what makes
"the sandbox writes no file itself" true: the file that lands in
``capture-inbox/`` is written by the host, on the host's own filesystem, in
response to a tool CALL whose only payload is the draft's CONTENT.

**Why this must NOT go through the ordinary gated path.** ``core.capture()``
never touches ``egress.apply_gate`` — a capture moves a draft INTO the vault,
it does not read one out — so wrapping it as an ordinary tool would make
every call raise ``UngatedToolError`` (mediate() sees an empty tally). It is
declared in ``mcp_mediation.BODYLESS_TOOLS`` instead, for the reason recorded
there. That declaration buys silence from the read-shaped machinery, not a
free pass from being recorded at all: see ``_record_capture_trace`` below.
"""
from __future__ import annotations

from typing import Any


class CaptureRecordError(RuntimeError):
    """A capture staged its draft but its WRITE-side trace could not be recorded.

    The write-side mirror of ``mcp_mediation.ReadRecordError``. Until s06 this
    call ignored ``read_log.record``'s return value entirely: ``capture`` is
    declared bodyless (``BODYLESS_TOOLS``), so ``mediate()`` never sees a tally
    for it and never flushes anything on its behalf. A log directory that
    briefly could not be written left the capture succeeding with no trace and
    no signal one was missing — fail-OPEN, where the read leg (S02) is
    fail-closed.

    **READ THE RAISE AS "NOT TRACED", NEVER AS "NOT WRITTEN".** This is raised
    INSTEAD of returning the result, so only the CALLER's success response is
    withheld; nothing is rolled back, and what already happened differs by leg:

    * **host** — the leg the broker actually runs (see the module docstring):
      ``core.capture()`` has ALREADY signed the note, appended its audit-chain
      entry and indexed it, so the note is committed and searchable before
      this function is reached. A signed entry therefore exists; the untraced
      thing is the fact that the BROKER accepted the call. The id is a content
      hash, so a client that retries rewrites the same path and duplicates
      nothing.
    * **vm** — the draft is already durable in ``capture-inbox/`` and no
      signed entry exists yet; here this trace is the only record naming the
      event until the host drains it. ``stage_draft_unique`` gives each retry
      a fresh uuid-suffixed name, so a retrying client stages the same content
      more than once and the host's duplicate-id guard resolves it at drain.
    """


def _record_capture_trace(*, vault: Any) -> None:
    """Append a WRITE-shaped trace of this capture, distinct from two other
    records that do NOT exist for a captured-but-undrained draft:

    * the READ-shaped SEC-06 flush ``mediate()`` would otherwise attempt —
      skipped by declaring ``capture`` in ``BODYLESS_TOOLS`` — which counts
      note bodies gated, a field this call has nothing to put a number in;
    * the SIGNED audit-chain entry ``brain write`` appends later, when the
      host drains this same draft (``draft_drain._sign_wal_index_candidate``)
      — that entry does not exist until the drain succeeds, so a draft
      captured and never drained would otherwise leave NOTHING durable
      naming the event (the staged file itself is durable evidence of the
      CONTENT, but nothing records that the broker accepted a capture call).

    Reuses ``read_log.record`` unmodified (same on-disk shape, same fsync +
    lock-file discipline, same ``BRAIN_READ_LOG=0`` opt-out) rather than a
    second hand-rolled writer. ``role="host"`` always, regardless of the
    calling session's configured role: unlike a read — self-reported by
    whichever leg ran the query, which is why ``read_log.enabled()`` refuses
    to record a vm-role read as evidence — this event is written by the HOST
    process that received and staged the bytes. It is evidence about what the
    HOST did, not a VM's account of itself, so the vm-disables-logging
    rationale does not apply here.

    **FAIL-CLOSED (s06), not merely present.** ``read_log.record`` returns
    ``False`` for TWO unrelated reasons and this function tells them apart the
    same way ``read_log.outcome_from_tally`` does for reads: ``BRAIN_READ_LOG=0``
    is an operator decision (checked first, via the same ``enabled()``
    predicate — NOT a failure, so this returns quietly), while any OTHER
    ``False`` means the write could not be made durable (no securable log
    dir, an OSError, a lock timeout) and raises :class:`CaptureRecordError`
    instead of letting the call succeed untraced.
    """
    from . import read_log as _read_log

    if not _read_log.enabled("host"):
        return  # BRAIN_READ_LOG=0 — an operator decision, not a failure
    written = _read_log.record(
        vault=vault, role="host", cmd="mcp:capture", max_tier=None,
        surfaced=0, withheld=0, gates=0,
    )
    if not written:
        raise CaptureRecordError(
            "mcp:capture: the capture COMPLETED — on the host leg the note is "
            "already signed and indexed, on the vm leg the draft is already "
            "staged — but its write-side trace could not be recorded, so the "
            "result is withheld. Nothing was rolled back."
        )


def dispatch_capture(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Stage a draft through ``core.capture()`` — signed immediately on the
    host, staged unsigned in ``capture-inbox/`` on the VM leg. See the module
    docstring for why this is host-side staging by construction, not by a
    fallback."""  # noqa: ARG001
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("capture requires a non-empty 'content' argument")

    res = core.capture(
        content,
        note_id=args.get("id") or None,
        note_type=args.get("type") or None,
        classification=args.get("classification") or None,
        reason="mcp capture",
    )
    _record_capture_trace(vault=core.vault)
    return res
