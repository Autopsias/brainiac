"""The ``capture`` verb through the broker (closed-stacks S04, DESK-04).

What this buys, stated plainly: "save this as a note" used to mean the
sandbox wrote a draft file straight into ``.brain/capture-inbox/`` on the
attached mount. Once the vault leaves that mount (the plan's own premise),
the sandbox has no filesystem path to write to at all. This tool is the
replacement — the broker (a HOST process) accepts the draft's CONTENT as a
tool argument and stages it itself, so the sandbox writes no file of its
own, ever.

**Body is unchanged from the CLI's, not reimplemented.** The tool is a thin
call into ``mcp_adapter.dispatch``, which routes to ``dispatch_capture`` in
:mod:`brain.mcp_capture_verbs` — a call into ``core.capture()``, the SAME
unified verb ``brain capture`` runs. On the host that signs + indexes
immediately; on the VM leg it stages an unsigned, untrusted draft the same
way ``brain draft-capture`` always has, just without the VM ever touching a
filesystem. The audited commit path, the untrusted-author sanitisation, and
the unsigned-until-drained contract are unchanged — see
``mcp_capture_verbs`` and ``draft_drain.sanitize_untrusted_note`` for where
each actually lives.

Declared in ``mcp_mediation.BODYLESS_TOOLS`` (see that module for why) and
given its own write-shaped trace instead — see
``mcp_capture_verbs._record_capture_trace``.
"""
from __future__ import annotations

from typing import Any

from ..mcp_adapter import dispatch


def register(server: Any, *, core: Any) -> None:
    """Register the ``capture`` tool on ``server``."""

    @server.tool()
    def capture(
        content: str,
        id: str | None = None,  # noqa: A002 -- the MCP argument name is `id`
        type: str | None = None,  # noqa: A002 -- ditto, matches core.capture()'s note_type
        classification: str | None = None,
    ) -> dict:
        """Save ``content`` as a draft note.

        Host: signed, indexed, and immediately retrievable. VM (Cowork): staged
        unsigned and untrusted for the host's next drain-on-invoke — never
        authoritative, never surfaced by search, until the host promotes it.
        ``id``/``type``/``classification`` are optional overrides; a missing
        classification defaults to Internal so the draft is usable without an
        egress elevation."""
        return dispatch(
            "capture",
            {"content": content, "id": id, "type": type, "classification": classification},
            core=core,
        )
