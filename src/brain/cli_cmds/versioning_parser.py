"""Register version-chain commands."""

from __future__ import annotations


def _add_supersede(sub) -> None:
    sp = sub.add_parser(
        "supersede",
        help="host-broker: retire <old-id> in favour of <new-id> — both sides of the version chain, signed (TMP-02, ADR-0003 Ruling 2/8)",
    )
    sp.add_argument("old_id", metavar="old-id")
    sp.add_argument("new_id", metavar="new-id")
    sp.add_argument("--reason", default="")
    sp.add_argument("--json", action="store_true")


def _add_unsupersede(sub) -> None:
    sp = sub.add_parser(
        "unsupersede",
        help="host-broker: BREAK the <old-id> -> <new-id> supersession link — both sides, signed. The audited undo for a wrong auto-link (ENF-01)",
    )
    sp.add_argument("old_id", metavar="old-id")
    sp.add_argument("new_id", metavar="new-id")
    sp.add_argument("--reason", default="")
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_supersede(sub)
    _add_unsupersede(sub)
