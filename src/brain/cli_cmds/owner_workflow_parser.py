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


def _add_interview(sub) -> None:
    sp = sub.add_parser(
        "interview",
        help="the owner-interview lane (INT-01): list the open questions the vault asks on the morning sheet; --nightly applies the answers, then draws the day's questions. HOST-ONLY.",
    )
    sp.add_argument("--nightly", action="store_true",
                    help="apply pending answers, expire, then generate")
    sp.add_argument("--apply", action="store_true",
                    help="apply the answers on consumed sheets only")
    sp.add_argument("--generate", action="store_true",
                    help="draw today's questions only")
    sp.add_argument("--prompt-out", default=None, metavar="DIR",
                    help="with --nightly/--generate: write the phrasing leg's prompt.txt here")
    sp.add_argument("--answer", default=None, metavar="KEY",
                    help="answer one open question now (with --action, optional --note)")
    sp.add_argument("--action", default=None, metavar="ACTION",
                    help="the option action to apply (see the question's options)")
    sp.add_argument("--note", default="", metavar="TEXT",
                    help="the free-text note beside the answer")
    sp.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="run as of this date (tests and replays)")
    add_common(sp)


def add_parser(sub) -> None:
    _add_inbox(sub)
    _add_retro(sub)
    _add_interview(sub)
