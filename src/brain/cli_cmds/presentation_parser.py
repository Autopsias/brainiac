"""Register owner-facing summary commands."""

from __future__ import annotations

from .. import classification as cls


def _add_capture(sub) -> None:
    sp = sub.add_parser(
        "capture",
        help="capture a note: HOST signs+writes+syncs; VM drops unsigned draft to capture-inbox/ (UX-01)",
    )
    sp.add_argument(
        "--id", default=None, help="note id (default: derived from content hash)"
    )
    sp.add_argument(
        "--type", default=None, dest="note_type", help="note type (default: note)"
    )
    sp.add_argument(
        "--classification",
        default=None,
        choices=cls.TIERS,
        help="classification tier (default: Internal)",
    )
    sp.add_argument("--content", default=None, help="note text (default: read stdin)")
    sp.add_argument("--reason", default="", help="audit reason (host only)")
    sp.add_argument("--json", action="store_true")


def _add_brief(sub) -> None:
    sp = sub.add_parser(
        "brief",
        help="morning brief: drains pending captures (host) + quiet index summary (UX-02)",
    )
    sp.add_argument(
        "-n", type=int, default=5, help="max recent notes to show (default: 5)"
    )
    sp.add_argument(
        "--no-drain",
        action="store_true",
        help="skip the capture drain (VM / read-only mode)",
    )
    sp.add_argument("--max-tier", default=None, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--html",
        action="store_true",
        help="write a self-contained, overlay-branded HTML brief to .brain/brief/ (host-only — a new file-egress surface, ADR-0003 Ruling c; refused on role=vm)",
    )


def _add_digest(sub) -> None:
    sp = sub.add_parser(
        "digest", help="weekly digest: notes added/updated in the past N days (UX-02)"
    )
    sp.add_argument(
        "--days", type=int, default=7, help="lookback period in days (default: 7)"
    )
    sp.add_argument("--max-tier", default=None, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--html",
        action="store_true",
        help="write a self-contained, overlay-branded HTML digest to .brain/brief/ (host-only — a new file-egress surface, ADR-0003 Ruling c; refused on role=vm)",
    )


def _add_health_report(sub) -> None:
    sp = sub.add_parser(
        "health-report",
        help="render the static HTML health report (verdict + act-now + maintain/index/trend tables) to .brain/brief/health-latest.html (host-only — refused on role=vm)",
    )
    sp.add_argument("--json", action="store_true")


def _add_graph_report(sub) -> None:
    sp = sub.add_parser(
        "graph-report",
        help="render the static HTML graph explorer (WebGL link graph + 3D semantic map) to .brain/graph/graph-explorer.html (host-only — refused on role=vm)",
    )
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_capture(sub)
    _add_brief(sub)
    _add_digest(sub)
    _add_health_report(sub)
    _add_graph_report(sub)
