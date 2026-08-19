"""Register scheduled maintenance commands."""

from __future__ import annotations

from .common_parser import add_common


def _add_check(sub) -> None:
    sp = sub.add_parser(
        "check", help="daily-check fold: index reconcile + drain drafts + status (host)"
    )
    sp.add_argument("--dry-run", action="store_true", help="report only; no sync/drain")
    sp.add_argument("--json", action="store_true")


def _add_health(sub) -> None:
    sp = sub.add_parser(
        "health",
        help="health fold: status + audit-chain verify + substrate self-test (host)",
    )
    sp.add_argument("--json", action="store_true")


def _add_curate(sub) -> None:
    sp = sub.add_parser(
        "curate",
        help="curation fold: refresh-index + unclassified-notes lint + stale-wikilink-target detection + age x centrality revisit sample (host); orphan/contradiction/callout lint stay vault-overlay (no brain equivalent)",
    )
    sp.add_argument(
        "--dry-run", action="store_true", help="report only; no refresh-index"
    )
    sp.add_argument("-k", type=int, default=50, help="max findings (default: 50)")
    add_common(sp)


def _add_integrity(sub) -> None:
    sp = sub.add_parser(
        "integrity",
        help="integrity-scan fold: audit-chain verify + corpus-wide near-dup scan directly over the brain vector backend (host; G1)",
    )
    sp.add_argument(
        "--min-score",
        type=float,
        default=0.95,
        help="near-dup cosine threshold (default: 0.95)",
    )
    sp.add_argument(
        "-k", type=int, default=5, help="ANN probe depth per note (default: 5)"
    )
    add_common(sp)


def _add_promote_scan(sub) -> None:
    sp = sub.add_parser(
        "promote-scan",
        help="promotion-scan fold: triage raw/ sources not yet promoted to a typed brain/ note (host; promotion itself stays a human gate)",
    )
    sp.add_argument("-k", type=int, default=50, help="max candidates (default: 50)")
    add_common(sp)


def _add_sweep_workspace(sub) -> None:
    sp = sub.add_parser(
        "sweep-workspace",
        help="WSP-01: move SETTLED top-level files (mtime older than --age-days) from configured working folder(s) into <vault>/inbox/ for the standard ingest drain — the lifecycle for session-artifact dumping grounds. Sources: --dir (repeatable) or $BRAIN_WORKSPACE_SWEEP_DIRS. Subdirectories and dotfiles are never touched; already-ingested content dedups by hash downstream. Runs inside the nightly maintain automatically when configured (host-only)",
    )
    sp.add_argument(
        "--dir",
        action="append",
        default=None,
        dest="dirs",
        help="workspace folder to sweep (repeatable; default: $BRAIN_WORKSPACE_SWEEP_DIRS)",
    )
    sp.add_argument(
        "--age-days",
        type=int,
        default=None,
        help="settled threshold in days (default: $BRAIN_WORKSPACE_SWEEP_AGE_DAYS or 14)",
    )
    sp.add_argument(
        "--dry-run", action="store_true", help="report what would move; touch nothing"
    )
    sp.add_argument("--json", action="store_true")


def _add_maintain(sub) -> None:
    sp = sub.add_parser(
        "maintain",
        help="the umbrella: THE single sanctioned host task (brain-nightly) — workspace sweep (when configured) + sync --publish + brief + recommendations-aging fold, plus date-gated health/integrity/digest(+curate+promote-scan)/graphify branches; due-since-last-run catch-up + single-runner lock (ADR-0003 Ruling 5/d)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="skip sync/drain/publish/signing; still runs the real read-only health/integrity probes for any due branch",
    )
    sp.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD override for date-gate testing (default: today)",
    )
    sp.add_argument(
        "--allow-future-date",
        action="store_true",
        help="permit a --date AFTER the wall clock (needed only for deliberate future date-gate exercises; default: refuse)",
    )
    sp.add_argument(
        "--min-score",
        type=float,
        default=0.95,
        help="near-dup cosine threshold on a due Tuesday branch (default: 0.95)",
    )
    sp.add_argument("--json", action="store_true")


def _add_graphify(sub) -> None:
    sp = sub.add_parser(
        "graphify",
        help="graphify discovery build: derived, non-authoritative graph (wikilinks + capped embedding-neighbour INFERRED edges) + human-review link candidates (host; ADR-0003 Ruling 6/(a))",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="bypass the corpus-drift gate and rebuild anyway",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="build + report only; never publish graph.json",
    )
    sp.add_argument(
        "-n", type=int, default=20, help="max candidates to surface (default: 20)"
    )
    sp.add_argument(
        "--progress",
        action="store_true",
        help="force stderr progress lines even when stderr isn't a TTY (same as BRAIN_PROGRESS=1)",
    )
    add_common(sp)


def add_parser(sub) -> None:
    _add_check(sub)
    _add_health(sub)
    _add_curate(sub)
    _add_integrity(sub)
    _add_promote_scan(sub)
    _add_sweep_workspace(sub)
    _add_maintain(sub)
    _add_graphify(sub)
