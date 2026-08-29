"""The housekeeping verbs — alerts, exceptions, inbox — through the broker
(closed-stacks S03, DESK-03).

What this buys, stated plainly: once a Cowork session's vault leaves the
VirtioFS mount (the Closed Stacks plan's own premise), it can no longer read
``.brain/notify-sent/current.json`` or ``.brain/exceptions.json`` itself, so
"what needs me?" and "is the vault healthy?" go dark unless something serves
those same answers over the broker. These three tools are that something.

**Bodies are unchanged from the CLI's, not reimplemented.** Each is a thin
call into ``mcp_adapter.dispatch``, which routes to ``dispatch_alerts`` /
``dispatch_exceptions`` in :mod:`brain.mcp_housekeeping_verbs` — the SAME
``alerts.collect`` / ``exceptions_cli.collect`` functions
``brain alerts --json`` / `brain exceptions --json`` call. That is what makes
the VM-side verification contract (signature, pinned workspace identity,
schema, freshness — ``exceptions_verify.verify``, HARDENED:codex-verify-r1)
apply here too: it is not re-run at this seam, it is the SAME call. An
unverifiable summary reports ``unreachable``, never a fabricated zero — see
``tests/test_mcp_tools_housekeeping.py`` for the four checks probed one at a
time, plus the happy path.

**No fourth tool for the raw owner-decision queue.** ``inbox.jsonl`` is
host-only doctrine (owner ruling 2026-08-22: "attacker-writable file
existence is not evidence") and the ``brain-inbox`` skill that answers it is
host-only for the same reason. ``inbox`` here is registered anyway, because
the item asks for it — but its body is byte-identical to ``exceptions``:
the same signed summary, same verification, never the raw file. See
``dispatch_exceptions``'s docstring in :mod:`brain.mcp_housekeeping_verbs`.

All three are declared in ``mcp_mediation.BODYLESS_TOOLS``: they read
``.brain/`` status files, never a note body, so there is nothing for
``egress.apply_gate`` to gate.
"""
from __future__ import annotations

from typing import Any

from ..mcp_adapter import dispatch


def register(server: Any, *, core: Any) -> None:
    """Register the housekeeping verbs on ``server``."""

    @server.tool()
    def alerts() -> dict:
        """The degradation digest: what needs the owner, right now.

        Same payload as ``brain alerts --json``. On the VM leg, the two
        host-home sources (auto-update state, weekly synthesis health) are
        reported under ``unreachable`` rather than skipped."""
        return dispatch("alerts", {}, core=core)

    @server.tool()
    def exceptions() -> dict:
        """The exceptions banner: a count of things that need the owner, and
        where to read them.

        Same payload as ``brain exceptions --json``. On the VM leg this reads
        a SIGNED summary and verifies it (signature, pinned workspace
        identity, schema, freshness) before trusting anything in it — an
        unverifiable summary reports ``unreachable``, never a fabricated
        zero."""
        return dispatch("exceptions", {}, core=core)

    @server.tool()
    def inbox() -> dict:
        """Alias of `exceptions`. There is no separate owner-decision-queue
        tool: ``inbox.jsonl`` stays host-only doctrine, so this serves the
        same signed summary `exceptions` does — never the raw file, and
        never real question text."""
        return dispatch("inbox", {}, core=core)
