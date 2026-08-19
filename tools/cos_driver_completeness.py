"""The per-run enumeration invariant of `cos_driver` — completeness compared as ID sets

Moved verbatim out of `cos_driver` (batch-2 drain) and re-imported by it, so
every name keeps its `cos_driver` module path; the parent's night orchestration
calls these through its own globals exactly as before, so a test that
monkeypatches one on `cos_driver` still steers the callers.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_transport import DriverStop, short  # noqa: E402


# ---------------------------------------------------------------------------
# completeness: SETS, not counts (adv-10)
# ---------------------------------------------------------------------------
#: Until a trustworthy baseline has been MEASURED, the tolerance is zero. A
#: non-zero one would have to cite the measurement that justifies it.
SET_DIFFERENCE_TOLERANCE = 0


def completeness(capture: dict[str, Any]) -> dict[str, Any]:
    """The per-run enumeration invariant, compared as ID SETS.

    Counts are not sets. A truncated enumeration that drops five threads while
    five arrivals appear has an equal COUNT and is exactly the failure this
    exists to catch, so the symmetric difference is itemised and every element
    must be attributable. Any unexplained element is a HARD STOP before a single
    body is fetched: if enumeration silently under-returns, the ledgers, the
    metrics and the contract all agree with each other and the night scores PASS
    over a mailbox it barely read.
    """
    items = capture["enumeration"].get("items", [])
    enumerated = {it["convId"] for it in items if it.get("convId")}
    scanner = set(capture["scan"].get("ids") or [])
    # THE DOM SCANNER SEES ONE VIEW. Focused/Other is a UI filter and switching
    # it means clicking a tab, which this driver may not do — so the cross-check
    # is run over the REST census PARTITIONED by the same `InferenceClassification`
    # the displayed view represents. Comparing the whole REST set against one
    # view's DOM ids would raise a hard stop on every run for a reason that is
    # not a truncation. The uncovered partition is REPORTED, never assumed away:
    # `TotalItemsInView` reconciliation and pagination termination are the two
    # completeness signals that do cover the whole folder, and both are hard.
    view = capture["scan"].get("view")
    if view in ("Focused", "Other"):
        covered = {it["convId"] for it in items
                   if it.get("convId") and str(it.get("cls") or "") == view}
    else:
        covered = enumerated
    only_rest = sorted(covered - scanner)
    only_scan = sorted(scanner - covered)
    folder_total = capture["enumeration"].get("folder_total")
    messages = len(capture["enumeration"].get("items", []))
    return {
        "enumerated_count": len(enumerated),
        "scanner_count": len(scanner),
        "dom_cross_check_view": view,
        "dom_cross_check_covered": len(covered),
        "dom_cross_check_uncovered": len(enumerated - covered),
        "enumerated_ids": sorted(short(c) for c in enumerated),
        "scanner_ids": sorted(short(c) for c in scanner),
        "cross_checked_ids": sorted(short(c) for c in covered),
        "symmetric_difference": {
            "only_in_rest_enumeration": [short(c) for c in only_rest],
            "only_in_dom_scanner": [short(c) for c in only_scan],
        },
        # Nothing arrives or moves inside a read-only night by our hand, so an
        # element is attributable only to mail that arrived while we read. Any
        # element we cannot attribute is unexplained, and unexplained is fatal.
        "attributed": [],
        "unexplained_set_difference": len(only_rest) + len(only_scan),
        "tolerance": SET_DIFFERENCE_TOLERANCE,
        "tolerance_basis": "zero until a trustworthy baseline has been measured",
        "pagination_terminated": bool(capture["enumeration"].get("terminated")),
        "page_count": capture["enumeration"].get("page_count"),
        "terminating_condition": ("FindItem RootFolder.IncludesLastItemInRange"
                                  if capture["enumeration"].get("terminated")
                                  else "page cap reached without a last-item flag"),
        "folder_total_reported": folder_total,
        "messages_enumerated": messages,
        "folder_total_reconciled": folder_total == messages,
        "dom_declared_size": capture["scan"].get("declared"),
        "dom_scan_complete": bool(capture["scan"].get("complete")),
    }


def assert_complete(report: dict[str, Any]) -> None:
    if report["unexplained_set_difference"] > report["tolerance"]:
        raise DriverStop(
            f"enumeration completeness FAILED: "
            f"{report['unexplained_set_difference']} conversation id(s) appear "
            f"in one enumeration and not the other and none is attributable to "
            f"a recorded arrival — REST {report['enumerated_count']}, DOM "
            f"scanner {report['scanner_count']}. Refusing to fetch any body: a "
            "truncated night whose ledgers, metrics and contract all agree with "
            "each other scores PASS over a mailbox it barely read.")
    if not report["pagination_terminated"]:
        raise DriverStop(
            "enumeration paging never reached the end of the folder "
            f"({report['page_count']} page(s), no IncludesLastItemInRange) — a "
            "short read that never paged is the same failure as a dropped id.")
    if not report["folder_total_reconciled"]:
        raise DriverStop(
            f"the server reports {report['folder_total_reported']} item(s) in "
            f"the folder and the enumeration returned "
            f"{report['messages_enumerated']} — the census does not reconcile.")

