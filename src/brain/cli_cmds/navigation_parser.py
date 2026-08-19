"""Register note-navigation commands."""

from __future__ import annotations


from .common_parser import add_common


def _add_graph_expand(sub) -> None:
    sp = sub.add_parser(
        "graph-expand",
        help="wikilink-BFS + PPR multi-hop expansion — DISCOVERY-ONLY (RET-03)",
    )
    sp.add_argument("seeds", nargs="+", help="seed note id(s)")
    sp.add_argument("--depth", type=int, default=2, help="BFS hop depth (default: 2)")
    sp.add_argument("-k", type=int, default=10, help="max candidates (default: 10)")
    sp.add_argument(
        "--no-ppr", action="store_true", help="BFS only, skip Personalized PageRank"
    )
    sp.add_argument(
        "--use-inferred",
        action="store_true",
        help="fold graphify's published INFERRED edges into the traversal too (GRF-01, optional; host-only, silently ignored on role=vm)",
    )
    add_common(sp)


def _add_get(sub) -> None:
    sp = sub.add_parser("get", help="fetch one note by id")
    sp.add_argument("id")
    add_common(sp)


def _add_read(sub) -> None:
    sp = sub.add_parser(
        "read", help="alias of `get`: read one full note by id (RET-04)"
    )
    sp.add_argument("id")
    add_common(sp)


def _add_recent(sub) -> None:
    sp = sub.add_parser("recent", help="list recently updated notes")
    sp.add_argument("-n", type=int, default=10, help="how many (default: 10)")
    add_common(sp)


def add_parser(sub) -> None:
    _add_graph_expand(sub)
    _add_get(sub)
    _add_read(sub)
    _add_recent(sub)
