"""The structural self-check of `cos_mutate`

Moved verbatim out of `cos_mutate` (batch-2 drain); every name is re-imported
by the parent so its `cos_mutate` module path is unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import os  # noqa: E402
from cos_mutate_gates import MutationStop, _ts, canary_status  # noqa: E402
from cos_mutate_ledger import UndoLedger, _write_text_atomic  # noqa: E402

# ---------------------------------------------------------------------------
# selfcheck — the structural properties, provable with no mailbox
# ---------------------------------------------------------------------------
def selfcheck() -> int:
    import tempfile                                              # noqa: PLC0415

    # The state machine's vocabulary is closed.
    with tempfile.TemporaryDirectory() as d:
        vault = Path(d) / "vault"
        (vault / "cos-ops").mkdir(parents=True)
        os.environ.setdefault("BRAIN_VAULT", str(vault))
        led = UndoLedger.__new__(UndoLedger)
        led.path = vault / "cos-ops" / "_cos_undo_ledger_x.jsonl"
        led.run_id = "x"
        try:
            led.append({"idempotency_key": "a|archive", "verb": "archive",
                        "state": "probably-fine"})
            raise AssertionError("an invented state was accepted")
        except MutationStop:
            pass
        led.append({"idempotency_key": "a|archive", "verb": "archive",
                    "state": "intent"})
        led.append({"idempotency_key": "a|archive", "verb": "archive",
                    "state": "reconciled"})
        assert led.latest()["a|archive"]["state"] == "reconciled"
        assert led.applied_counts()["archive"] == 1
        assert led.unfinished() == []

    # A canary with no receipts is a FAIL, whatever else it carries.
    with tempfile.TemporaryDirectory() as d:
        vault = Path(d)
        (vault / "cos-ops").mkdir(parents=True)
        _write_text_atomic(vault / "cos-ops" / "_cos_undo_canary.json", json.dumps(
            {"lanes": {"rest": {"tested": _ts(), "idempotent_replay": "confirmed"}}}))
        st = canary_status(vault)
        assert st["lane_match"] and not st["valid"], st
        assert "receipts" in st["why"]

    print("cos_mutate selfcheck: OK")
    return 0
