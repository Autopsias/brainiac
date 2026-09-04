"""The broker's MEDIATION BOUNDARY — one call, one fresh tally, one recording
DECISION.

The tally is unconditional: every mediated call starts from a zeroed per-task
egress tally. A ROW is not. ``read_log.outcome_from_tally`` returns one of four
outcomes and only ``written`` puts a line on disk — ``nothing_to_record`` when
the body never CALLED the gate (fine for a verb declared in
:data:`BODYLESS_TOOLS`,
:class:`UngatedToolError` for any other), ``disabled`` under a ``vm`` role
(LOW-02: ``BRAIN_READ_LOG=0`` disables the CLI leg but NOT this one — the
broker's own flush passes ``respect_env=False`` so an env var left in a
connector config cannot silently drop an MCP caller's record), ``failed``
when content WAS gated and the line could not be
written (:class:`ReadRecordError`, and the caller never sees the data). A fifth
case sits outside those four: when the BODY raises, the flush still runs but
inside ``suppress(Exception)`` with its outcome discarded, so that call can
leave no row and no signal. :func:`mediate` is where each is decided.

Split out of :mod:`brain.mcp_adapter` (closed-stacks S02, DESK-01) when that
module crossed the 500-line file-size ratchet. The split is along the seam the
adapter's own docstring already draws: :mod:`brain.mcp_adapter` owns the verb
table, the trust gate and the FastMCP subclass; this module owns what happens
AROUND one call — resetting the per-task egress tally, flushing the SEC-06 read
record fail-closed, and telling the four recording outcomes apart.

It is a LEAF: at module scope it imports only the stdlib, and lazily inside the
functions ``egress``/``read_log``/``querylog`` — nothing from
``mcp_adapter``. ``mcp_adapter``
re-exports every public name here, so ``mcp_adapter.mediate``,
``mcp_adapter.BODYLESS_TOOLS`` and the two exception classes remain the
addresses callers and tests already use.

**What this boundary does NOT do: it does not filter.** Filtering lives in the
CLI-backed handler bodies (:mod:`brain.mcp_verbs`, which route through
``egress.apply_gate``). Interception proves the RECORD, not the FILTER. It does
catch the crudest version of the mistake: a body that never reached the gate
leaves an empty tally, and :class:`UngatedToolError` turns that from a silent
pass into a refusal — no document, and no record either. What it cannot catch
is a body that DOES call the gate and gates the wrong content: the tally counts
gate CALLS, not what went through them, so such a call is indistinguishable
from a correct one here and falls to the same four outcomes as any other —
including the ones that write nothing. :func:`mediate` has the list.
"""
from __future__ import annotations

import contextlib as _contextlib
import contextvars as _contextvars
import time as _time

from typing import Any, Iterator

__all__ = [
    "ReadRecordError", "UngatedToolError", "BODYLESS_TOOLS", "mediate",
    "read_log_regime",
]


class ReadRecordError(RuntimeError):
    """A tool gated content and its SEC-06 record could NOT be written.

    Raised INSTEAD of returning the result. The MCP flush used to sit under
    ``except Exception: pass  # never let logging fail a read``, so a read whose
    record could not be written succeeded anyway, silently — the read leaving a
    record is the artefact this plan exists to guarantee, so it now fails closed.

    **SCOPE. Both legs fail closed since 2026-08-31; this class covers the
    BROKER leg.** The CLI leg's identical swallow — ``except Exception: pass``
    around ``read_log.record_from_tally`` — was closed the same way by
    :mod:`brain.cli_read_record`, which holds gated output until the record is
    written and exits ``5`` when it cannot be. This paragraph said the CLI
    swallow was "still live, verbatim" until then, and that mattered: the CLI
    is the leg CLAUDE.md tells every session to use ("call it from your native
    shell, never via MCP") and the one the nightly launchd job runs, so the
    gap this docstring scoped away was on the busier leg.

    The two legs differ only in HOW they refuse, because the shapes differ: the
    broker returns a value, so it raises instead; the CLI streams to stdout, so
    it cannot un-print and holds the output instead.
    """


class UngatedToolError(RuntimeError):
    """A tool that is supposed to gate note bodies produced NO egress tally.

    The tally is written by ``egress.apply_gate``. No tally means the handler
    body never reached the gate — so the result was never filtered, and the
    "nothing to record" branch would have waved it through unrecorded as
    though it were a status verb.

    Found by adversarial review 2026-08-28: inferring "this verb has no bodies"
    from the ABSENCE of security instrumentation makes a forgotten gate
    indistinguishable from a bodyless verb, which is exactly the future mistake
    DESK-01 exists to catch. So the bodyless verbs are DECLARED
    (:data:`BODYLESS_TOOLS`) and everything else must prove it gated.
    """


#: Tools that legitimately surface no note bodies, so they have nothing to
#: record and no gate to run. DECLARED, never inferred — see
#: :class:`UngatedToolError`.
#:
#: Measured 2026-08-28 across the 11 tools s02 registered, on a real vault:
#: every one leaves exactly one read-log row except ``vault_languages``,
#: INCLUDING the empty-result cases (``get`` on a missing id, ``grep`` on a
#: pattern that matches nothing) — ``apply_gate`` tallies even when it gates
#: zero notes. So that set was exactly right, not merely conservative.
#:
#: ``vault_languages`` LEFT this set on 2026-08-30. It returns note counts, and
#: a count over the whole index is vault metadata a clamped caller should not
#: see unrecorded; it now gates the note list under the caller's ceiling and so
#: leaves a row like every other read (closed-stacks s09, V-2).
#:
#: ``alerts``, ``exceptions`` and ``inbox`` (S03, DESK-03) join it for a
#: different reason: they never touch a note body at all — each is a pure call
#: into ``alerts.collect``/``exceptions_cli.collect``, the SAME functions the
#: CLI runs, over ``.brain/`` status files (a signed summary, a findings feed),
#: never over ``vault/brain/``. There is nothing there for ``egress.apply_gate``
#: to gate, so DECLARING them here (rather than leaving them to raise
#: ``UngatedToolError``) is the honest statement, not an exemption from one.
#:
#: ``capture`` (S04, DESK-04) joins for a THIRD reason, and it is the one this
#: set's own name warns against reading loosely: it is bodyless in the SEC-06
#: sense — nothing about it ever calls ``egress.apply_gate``, because it moves
#: a draft INTO the vault rather than reading one OUT — but it is not a status
#: verb like the three above, and declaring it here does not mean it leaves no
#: trace. A capture call writes its OWN write-shaped record
#: (``read_log.record(..., cmd="mcp:capture", ...)`` inside
#: :func:`brain.mcp_capture_verbs.dispatch_capture`), distinct from this
#: module's read-shaped flush and from the signed entry ``brain write`` makes
#: later when the host drains the staged draft. Wrapping capture in the
#: ordinary (non-bodyless) path would have made every call raise
#: :class:`UngatedToolError` (its tally is always empty), and forcing a fake
#: gate call just to dodge that would leave a READ record — "surfaced: 0" —
#: describing an event that is actually a WRITE, which is worse than no
#: record at all.
BODYLESS_TOOLS = frozenset({"alerts", "exceptions", "inbox", "capture"})


#: True while a call is already inside :func:`mediate`. The nested
#: ``dispatch()`` must not reset the tally the outer wrapper is accumulating,
#: nor write a second record for the same call. A ContextVar, not a flag, for
#: the same reason the tally is one: the broker's concurrency is asyncio TASKS.
_MEDIATED: _contextvars.ContextVar[bool] = _contextvars.ContextVar(
    "brain_mcp_mediated", default=False,
)


def in_mediated_call() -> bool:
    """True inside an active :func:`mediate` block on THIS task."""
    return _MEDIATED.get()


def _flush_record(
    *, vault: Any, role: str, cmd: str, max_tier: str, started: float,
) -> str:
    """``respect_env=False`` (LOW-02): a ``BRAIN_READ_LOG=0`` left in a
    Desktop connector env — the exact scenario :func:`read_log_regime` exists
    to make loud — must not silently drop the record for every MCP caller.
    Only a vm role still disables this flush; see ``read_log.enabled``."""
    from . import egress as _egress, read_log as _read_log

    return _read_log.outcome_from_tally(
        vault=vault,
        role=role or "host",
        cmd=cmd,
        max_tier=max_tier,
        tally=_egress.take_tally(),
        latency_ms=(_time.perf_counter() - started) * 1000.0,
        respect_env=False,
    )


@_contextlib.contextmanager
def mediate(
    *, vault: Any, role: str, cmd: str, max_tier: str, expects_gate: bool = True,
) -> Iterator[None]:
    """Reset the egress tally around one call, then flush its record FAIL-CLOSED.

    Three of the four outcomes let the result through, and telling them apart is
    the whole point (``read_log.outcome_from_tally``):

    * ``nothing_to_record`` — the verb never CALLED ``egress.apply_gate``
      (``gates == 0``); gating zero notes still CREATES a tally, so it can
      never land here — the other three bullets govern what happens to it
      instead. The housekeeping verbs are like this, and logging a zero
      for them would bury the real reads. It SUCCEEDS **only for a tool declared in**
      :data:`BODYLESS_TOOLS`; for any other tool it means the handler never
      reached ``egress.apply_gate``, and that raises :class:`UngatedToolError`
      instead of returning an unfiltered, unrecorded result (``expects_gate``).
    * ``disabled`` — a vm role, for which a self-written access log is not
      evidence. An operator decision. SUCCEEDS. **Not ``BRAIN_READ_LOG=0``
      (LOW-02):** that variable disables the CLI leg, where the operator
      typed the command and sees ``cli_read_record``'s stderr notice that
      it did — but ``_flush_record`` above passes ``respect_env=False``, so
      it cannot silently drop an MCP caller's record the same way.
    * ``written`` — the record is on disk and fsynced. SUCCEEDS.
    * ``failed`` — content WAS gated and the line could not be written (no
      securable log dir, an OSError, a lock timeout). :class:`ReadRecordError`,
      and the caller never sees the data.

    On an exception from the body the record is still flushed, best-effort, and
    the original exception propagates unmasked — a failed read must not be
    reported as a logging failure. **Residual, deliberate:** that flush is
    suppressed and its outcome DISCARDED, so a call that gated content and then
    raised can leave no SEC-06 row and no signal that one is missing. No content
    reaches the client on that path.
    """
    from . import egress as _egress

    _egress.take_tally()  # never attribute a prior call's counts to this one
    started = _time.perf_counter()
    token = _MEDIATED.set(True)
    try:
        try:
            yield
        except BaseException:
            with _contextlib.suppress(Exception):
                _flush_record(
                    vault=vault, role=role, cmd=cmd, max_tier=max_tier,
                    started=started,
                )
            raise
        outcome = _flush_record(
            vault=vault, role=role, cmd=cmd, max_tier=max_tier, started=started,
        )
    finally:
        _MEDIATED.reset(token)
    from . import read_log as _read_log

    if outcome == _read_log.RECORD_FAILED:
        raise ReadRecordError(
            f"{cmd}: content was gated but its SEC-06 read record could not be "
            "written; refusing to return the result"
        )
    if expects_gate and outcome == _read_log.RECORD_NOTHING_TO_RECORD:
        raise UngatedToolError(
            f"{cmd}: returned without ever calling egress.apply_gate, so its "
            "result was never filtered and would not have been recorded. Route "
            "the handler through mcp_adapter.dispatch, or declare the tool in "
            "mcp_adapter.BODYLESS_TOOLS if it genuinely surfaces no note bodies"
        )


def read_log_regime(vault: Any, role: str) -> str:
    """One line naming which SEC-06 recording state the broker's OWN flush is
    actually in — ``respect_env=False``, the same call ``_flush_record``
    makes (LOW-02), so this banner can never claim "DISABLED" while the
    broker goes on writing rows.

    Two production states used to be indistinguishable to an operator:
    ``BRAIN_READ_LOG=0`` left in a Desktop connector env and ``BRAIN_ROLE=vm``
    mis-set on a host broker both made every read succeed and none get
    recorded, with nothing printed. Since LOW-02 only the vm-role case still
    disables the broker's flush — ``BRAIN_READ_LOG=0`` no longer silently
    drops an MCP caller's record — so that is the only DISABLED state left
    here; an unwritable log dir makes every gated read RAISE instead, and
    ``brain maintain``'s nightly read_log block still needs to tell that apart
    from "nothing was read" (adversarial review 2026-08-28). Claude Desktop
    surfaces a connector's stderr, so one line at startup is the cheapest
    observability this transport has.
    """
    from . import read_log as _read_log

    if not _read_log.enabled(role, respect_env=False):
        return "read-log DISABLED by role=vm — reads will not be recorded"
    location = _read_log._log_dir(vault)
    if location is None:
        return ("read-log UNWRITABLE — no securable log dir; every gated read "
                "will REFUSE (fail-closed)")
    return f"read-log recording -> {location[1]}"
