"""Register client-wiring commands."""

from __future__ import annotations

from .. import connect as _connect


def _add_connect(sub) -> None:
    sp = sub.add_parser(
        "connect",
        help="SUI-02: wire ONE client (claude-code|claude-desktop|codex|gemini) to this vault — shows a diff, asks before touching any user config file, idempotent (re-run says 'already connected'). Host-only, self-executing (not print-only — that's `mcp-config`). `--remove` unwires the same client.",
    )
    sp.add_argument("--client", required=True, choices=list(_connect.CLIENTS))
    sp.add_argument(
        "--target",
        default=".",
        help="project directory being wired (default: cwd) — where CLAUDE.md/AGENTS.md/.gemini/settings.json live",
    )
    sp.add_argument(
        "--name",
        default="brainiac",
        help="MCP server name for --client claude-desktop (default: %(default)s)",
    )
    sp.add_argument(
        "--max-tier",
        default=None,
        help="egress ceiling baked into the claude-desktop MCP stanza (default: the host full-vault tier, matches `mcp-config`)",
    )
    sp.add_argument(
        "--marketplace-source",
        default=_connect.DEFAULT_MARKETPLACE_SOURCE,
        help="source passed to `claude plugin marketplace add` for --client claude-code (default: %(default)s)",
    )
    sp.add_argument(
        "--remove", action="store_true", help="unwire this client instead of wiring it"
    )
    sp.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive y/N confirmation (required when not a TTY)",
    )
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_connect(sub)
