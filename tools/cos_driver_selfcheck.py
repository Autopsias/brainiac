"""The structural self-check of `cos_driver` — the properties provable with no mailbox

Moved verbatim out of `cos_driver` (batch-2 drain) and re-imported by it, so
every name keeps its `cos_driver` module path; the parent's night orchestration
calls these through its own globals exactly as before, so a test that
monkeypatches one on `cos_driver` still steers the callers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_accounting import build_accounting  # noqa: E402
from cos_driver_completeness import assert_complete, completeness  # noqa: E402
from cos_driver_draw import body_draw  # noqa: E402
from cos_driver_transport import DriverStop, load_sheet  # noqa: E402


def selfcheck() -> int:
    """The structural properties, provable with no mailbox anywhere."""
    # The SOURCE-level properties (no click dispatch, no mutation verb) live in
    # `tests/test_cos_driver.py`, not here: a scanner that reads the file it is
    # written in matches its own pattern list, and the fix for that is to put the
    # patterns in a different file rather than to weaken the scan.

    # Unread is excluded BEFORE the fetch.
    convs = [{"convId": "a", "itemId": "1", "isRead": True, "received": "2026-08-10",
              "categories": ["P1 · Today"]},
             {"convId": "b", "itemId": "2", "isRead": False, "received": "2026-08-11",
              "categories": ["P0 · Now"]},
             {"convId": "c", "itemId": "3", "isRead": True, "received": "2026-08-09",
              "categories": ["P0 · Now"]}]
    draw = body_draw(convs, 10)
    assert [d["convId"] for d in draw] == ["c", "a"], draw
    assert all(d["convId"] != "b" for d in draw), "an unread row reached the draw"

    # Completeness compares SETS, and equal counts over different ids FAIL.
    capture = {"enumeration": {"items": [{"convId": x, "received": "", "isRead": True}
                                         for x in ("a", "b", "c")],
                               "folder_total": 3, "terminated": True, "page_count": 1},
               "scan": {"ids": ["a", "b", "d"], "declared": 3, "complete": True,
                        "stagnant_scans": 3, "at_end": True}}
    rep = completeness(capture)
    assert rep["enumerated_count"] == rep["scanner_count"] == 3
    assert rep["unexplained_set_difference"] == 2, rep
    try:
        assert_complete(rep)
        raise AssertionError("equal counts over different ids must be a HARD STOP")
    except DriverStop:
        pass

    # The accounting is a pure function: same capture in, byte-identical rows out.
    cap2 = {"enumeration": {"items": convs}, "bodies": [], "draw": []}
    a1 = build_accounting(cap2, run_id="2026-01-01-run1", bundle_version="v5.61",
                          rules_version="ext-4", enumerated_at="2026-01-01T00:00:00Z")
    a2 = build_accounting(cap2, run_id="2026-01-01-run1", bundle_version="v5.61",
                          rules_version="ext-4", enumerated_at="2026-01-01T00:00:00Z")
    assert json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True)
    for row in a1["rows"]:
        for slot in ("verdict", "category", "disposition", "held_reason", "dedup_check"):
            assert row[slot] is None, f"{slot} was filled by the driver"
    assert a1["counters"] == {"ingestion_in_scope": 3, "ingestion_candidates": 0,
                              "ingestion_held": 3}

    # The sheet gate refuses a vault with no stamped sheet.
    import tempfile                                              # noqa: PLC0415
    with tempfile.TemporaryDirectory() as d:
        try:
            load_sheet(Path(d))
            raise AssertionError("the driver started without a stamped sheet")
        except DriverStop as exc:
            assert "cos-run-begin" in str(exc)

    print("cos_driver selfcheck: OK")
    return 0
