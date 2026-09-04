"""The CLI leg's SEC-06 read record, made fail-CLOSED (VULN-3385, A-05).

The broker leg stopped swallowing a failed access-record write on 2026-08-28
(``mcp_mediation.ReadRecordError``). The CLI leg kept the original line ---
``except Exception: pass  # never let logging fail a read`` --- so a host read
whose record could not be written still returned the notes, unrecorded. That
is the leg ``CLAUDE.md`` tells every session to use, and the one the nightly
launchd job runs, so it is the leg that mattered most.

**Why this is not simply "call outcome_from_tally instead".** The record is
flushed AFTER the handler ran, because only then are the counts complete --- and
by then the handler has already written its result to stdout. You cannot
un-print. So the result is HELD until the record decision is made.

**What is held, and what is not.** Only output emitted AFTER the egress gate
has fired in this invocation (``egress.gated()``). A verb that never gates ---
``maintain``, ``rebuild``, ``doctor`` --- streams exactly as it did before, which
matters because those are the long ones. A verb that gates has, by construction,
already finished filtering before it renders, so holding its render costs one
payload that was fully built in memory anyway.

The four outcomes come from :mod:`brain.read_log`. Only ``failed`` withholds
the output: ``nothing_to_record`` (the verb gated nothing), ``disabled``
(``BRAIN_READ_LOG=0``, or a vm role, for which a self-written log is not
evidence) and ``written`` all release it. Failing on ``disabled`` would break
every read for an operator who turned the log off on purpose.
"""
from __future__ import annotations

import contextvars as _contextvars
from typing import Any

#: Exit code for a read that gated content and could not record the fact.
#: Its own number, not the generic 3: an operator reading a non-zero exit needs
#: to tell "the vault refused you" from "the audit log is broken", because the
#: fix is completely different.
EXIT_RECORD_FAILED = 5

#: Held stdout chunks for THIS invocation. A ContextVar, not a module global,
#: for the same reason ``egress._TALLY`` is one: the test suite calls ``_main``
#: many times in one process, and a leaked buffer would print one command's
#: result under another's.
_HELD: _contextvars.ContextVar[list[str] | None] = _contextvars.ContextVar(
    "brain_cli_held_output", default=None,
)


def begin() -> None:
    """Start holding gated output for one CLI invocation."""
    _HELD.set([])


def hold(text: str) -> bool:
    """Take one chunk of output if this invocation has gated content.

    Returns True when the chunk was held (the caller must NOT write it).
    """
    from . import egress

    buffer = _HELD.get()
    if buffer is None or not egress.gated():
        return False
    buffer.append(text)
    return True


def settle(
    *, vault: Any, role: str, cmd: str, max_tier: str | None,
    tally: dict[str, int] | None, latency_ms: float | int | None,
) -> tuple[str, list[str]]:
    """Flush the record and say what happened, with whatever output was held.

    Never raises: a failure to DECIDE must not become a traceback, and the
    conservative reading of an unexpected error here is the same as a failed
    write --- so it returns ``failed`` and the caller withholds the output.

    **The read-log can be switched off silently — LOW-02.** ``BRAIN_READ_LOG=0``
    made this outcome ``disabled`` with nothing printed anywhere, so an
    operator who set it once (or inherited it from an old shell profile) had
    no way to notice a gated read was going unrecorded. When THIS specific
    cause applies — not a vm role, which is the CLI leg's ordinary state and
    already unrecorded by design — one line goes to stderr, once per
    invocation (``settle`` runs once per ``cli.main`` call).
    """
    from . import read_log

    held = _HELD.get() or []
    _HELD.set(None)
    try:
        outcome = read_log.outcome_from_tally(
            vault=vault, role=role, cmd=cmd, max_tier=max_tier,
            tally=tally, latency_ms=latency_ms,
        )
    except Exception:
        outcome = read_log.RECORD_FAILED
    if outcome == read_log.RECORD_DISABLED and read_log.enabled(role, respect_env=False):
        # Disabled with the env check removed still disables ONLY for a vm
        # role — so reaching `disabled` at all here means BRAIN_READ_LOG=0
        # is the cause, not role=vm.
        import sys

        print("read record disabled by BRAIN_READ_LOG=0", file=sys.stderr)
    return outcome, held


def settle_for(core: Any, role: str, args: Any, started: float) -> tuple[bool, list[str]]:
    """:func:`settle`, with the four fields read off one CLI invocation.

    Returns ``(withhold, held_output)``. It reports the DECISION rather than
    the outcome word so no copy of ``read_log.RECORD_FAILED`` lives here to
    drift out of step with the original.

    Lives here rather than as a closure in ``cli.py`` only because that module
    is at its 500-line file-size ratchet.
    """
    import time as _time

    from . import egress, read_log

    outcome, held = settle(
        vault=getattr(core, "vault", None),
        role=role,
        cmd=args.cmd,
        max_tier=getattr(args, "max_tier", None),
        tally=egress.take_tally(),
        latency_ms=(_time.perf_counter() - started) * 1000.0,
    )
    return outcome == read_log.RECORD_FAILED, held


def refusal(cmd: str) -> str:
    """The message a withheld read prints, naming the cause and the fix."""
    return (
        f"refused: '{cmd}' read vault content but its access record could not "
        f"be written, so the result is withheld (SEC-06 fail-closed). Check the "
        f"read-log directory is writable and owner-only; "
        f"`BRAIN_READ_LOG=0` disables the log deliberately."
    )

