"""Register owner-decision commands."""

from __future__ import annotations

from .common_parser import add_common


def _add_inbox(sub) -> None:
    sp = sub.add_parser(
        "inbox",
        help="the Tier-2 owner-decision queue: list open questions, or record an answer (--answer KEY --value TEXT). HOST-ONLY.",
    )
    sp.add_argument(
        "--answer",
        default=None,
        metavar="KEY",
        help="record an answer to the open question with this key",
    )
    sp.add_argument(
        "--value",
        default=None,
        metavar="TEXT",
        help="the answer text (required with --answer)",
    )
    add_common(sp)


def _add_retro(sub) -> None:
    sp = sub.add_parser(
        "retro",
        help="retro fold: scan this vault's maintenance output for engine failure signatures and write engine-feedback prompts. HOST-ONLY.",
    )
    add_common(sp)


def add_parser(sub) -> None:
    _add_inbox(sub)
    _add_retro(sub)
