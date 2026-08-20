"""Evidence blocks of the mutation-proof artifact (s18 drain of build_evidence).

The literal sub-dicts moved verbatim out of ``cos_mutate.build_evidence``;
the lane module passes its own constants and callables so the artifact's
wording keeps its one definition.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any


def e17_block(e17: dict[str, Any], live: dict[str, Any] | None,
              *, mutation_lane: str, pending: str) -> dict[str, Any]:
    """The E17 canary block of the mutation-proof artifact."""
    return {
        "lane": mutation_lane,
        "lane_match": e17.get("lane_match"),
        "canary_tested_utc": e17.get("canary_tested_utc"),
        "canary_age_days": e17.get("canary_age_days"),
        "receipts_present": e17.get("receipts_present"),
        "valid": e17.get("valid"),
        "why": e17.get("why"),
        "fresh_drill_this_session": None if not live else True,
        "note": ("the fresh rest-lane drill is a LIVE move-and-move-back and "
                 "is the main session's to run; the drill code path, its "
                 "receipt assembly and its refusal to write a receipt-less "
                 "file are built and tested here"),
    }


def zero_send_block(live: dict[str, Any] | None, *, pending: str,
                    permitted_actions, save_only: str, permitted_folders,
                    draft_folder: str) -> dict[str, Any]:
    """The zero-send guarantees block of the mutation-proof artifact."""
    return {
        "sent_itemid_diff": (live or {}).get("sent_itemid_diff", pending),
        "runtime_allowlist_enforced": True,
        "permitted_actions": list(permitted_actions),
        "disposition_asserted": save_only,
        "permitted_folders": list(permitted_folders) + [draft_folder],
        "rejected_payloads": ((live or {}).get("runtime") or {})
        .get("rejected", []),
        "enforcement_point": ("tools/cos_mutate_page.js `send()` — the line "
                              "before `fetch`, in the page's own world, over "
                              "the payload that is actually about to leave"),
        "source_grep_is_the_second_belt_only": (
            "a source audit greps PYTHON; this engine replays CAPTURED "
            "payloads, so a fixture could carry a sending disposition with "
            "no literal in our source. The allowlist validates the OUTGOING "
            "payload against approved captured-request fingerprints, asserts "
            "the save-only disposition POSITIVELY, and rejects anything else "
            "before dispatch."),
    }


def mutation_shapes_block(live: dict[str, Any] | None, *,
                          pending: str) -> dict[str, Any]:
    """The replay-never-synthesize block of the mutation-proof artifact."""
    return {
        "enforced": True,
        "how": ("every payload is a clone of an APPROVED SKELETON captured "
                "from the app's own request for that verb, with ids "
                "substituted at known paths; a verb with no approved shape "
                "cannot run, and a payload whose key-path fingerprint "
                "differs from the approved shape is rejected before dispatch"),
        "shapes": (live or {}).get("shape_fingerprints", pending),
    }


def seed_fix_block(*, pending: str) -> dict[str, Any]:
    """The FindItem boot-capture fix block of the mutation-proof artifact."""
    return {
        "measured_blocker": ("s03: 137 main-world fetch calls across a full "
                             "310-row scroll, three sort changes, two folder "
                             "switches and a cold reload, with ZERO "
                             "action=FindItem — the app fires its FindItem "
                             "during BOOT and the hook was installed after "
                             "load, so run 116 died at `seed`"),
        "fix": "tools/cos_capture_hook.js, evaluated at document_start",
        "install": ("CDP Page.addScriptToEvaluateOnNewDocument, an extension "
                    "content script at run_at: document_start, or a "
                    "userscript at @run-at document-start — then RELOAD the "
                    "mail tab so the boot call is captured"),
        "self_reporting": ("`__cosCap.stats().document_start` is true only "
                           "when the hook was in place at readyState "
                           "`loading`; `boot_finditem` says whether a "
                           "replayable envelope actually exists"),
        "verified_against_the_live_page": pending,
    }


def runbook_steps() -> list[str]:
    """The ordered live-pass runbook of the mutation-proof artifact."""
    return [
        "export BRAIN_VAULT=$HOME/DeveloperFolder/Brainiac/vault",
        "install tools/cos_capture_hook.js at document_start, reload the "
        "mail tab, and confirm stats().boot_finditem is true",
        "owner performs ONE archive, ONE chip set and ONE draft-save in the "
        "UI, then: python3 tools/cos_mutate.py capture-shapes --tab-id <id>",
        "python3 tools/cos_mutate.py canary --run-id <run> --tab-id <id> "
        "--canary-convid <a disposable thread>   # the fresh E17 drill",
        "python3 tools/cos_mutate.py dry-run --run-id <run> --tab-id <id> "
        "   # read-only; inspect every payload before anything is sent",
        "python3 tools/cos_mutate.py apply --run-id <run> --tab-id <id> "
        "--cap-archive 3 --cap-categorize 5 --cap-draft 2",
        "re-run `evidence` with the live pass output to fill the null fields",
    ]


# ---------------------------------------------------------------------------
# batch-2 drain: `build_evidence` moved verbatim out of `cos_mutate` and is
# re-imported by it.
# ---------------------------------------------------------------------------

def build_evidence(vault: Path | None, run_id: str | None, *,
                   live: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `mutation-proof` artifact. Live fields are NULL until a live pass
    fills them — an invented value is the failure this whole plan exists to
    prevent, and a null awaiting a real run is honest."""
    e17 = canary_status(vault) if vault else {}
    faults = fault_injection_report()
    plan: dict[str, Any] = {}
    if vault and run_id:
        try:
            p = build_plan(vault, run_id,
                           applied=UndoLedger(vault, run_id).applied_counts())
            plan = {"run_id": run_id, "planned_by_verb": p["planned_by_verb"],
                    "excluded": len(p["excluded"]),
                    "exclusion_reasons": sorted({e["reason"] for e in p["excluded"]}),
                    "computed_read_only": True}
        except MutationStop as exc:
            plan = {"run_id": run_id, "error": str(exc)}
    ev: dict[str, Any] = {
        "session": "s04", "item": "MUT-01",
        "produced_at": _ts(),
        "executor": "main-session (a dispatched subagent is classifier-refused "
                    "on live mailbox mutation — owner ruling 2026-08-08)",
        "build_half": "this artifact",
        "live_half": PENDING if not live else "recorded below",
        "vault_root_asserted": (live or {}).get("vault_root_asserted")
        or (assert_vault(vault) if vault else None),
        "e17": e17_block(
            e17, live, mutation_lane=MUTATION_LANE, pending=PENDING),
        "zero_send": zero_send_block(
            live, pending=PENDING, permitted_actions=PERMITTED_ACTIONS,
            save_only=SAVE_ONLY, permitted_folders=PERMITTED_FOLDERS,
            draft_folder=DRAFT_FOLDER),
        # An EMPTY list would read as "the pass ran and mutated nothing", which
        # is a different claim from "the pass has not run". Say the second one.
        "mutations": live.get("results", []) if live else PENDING,
        "final_states": live.get("final_states", {}) if live else PENDING,
        "state_machine": list(STATES),
        "fault_injection": faults,
        "draft_autonomous_resume": DRAFT_RESUME_POLICY,
        "http_449_transitions": live.get("http_449_transitions", []) if live
        else PENDING,
        "dispatch_blocked_until_reprime": faults.get(
            "dispatch_blocked_until_reprime"),
        "in_flight_449_outcome": [t.get("in_flight_449_outcome")
                                  for t in (live or {}).get(
                                      "http_449_transitions", [])] or PENDING,
        "mutation_shapes_replayed_not_synthesized":
            mutation_shapes_block(live, pending=PENDING),
        "caps": DEFAULT_CAPS,
        "scope": {"default_since_days": DEFAULT_SINCE_DAYS,
                  "note": "the recency window bounds a night; caps are unlimited "
                          "by default (owner ruling 2026-08-11)"},
        "plan_observed": plan,
        "kill_switch": (live or {}).get("kill_switch",
                                        kill_switch(vault) if vault else None),
        "stopped": (live or {}).get("stopped"),
        "seed_fix": seed_fix_block(pending=PENDING),
        "runbook_for_the_live_pass":
            runbook_steps(),

    }
    return ev


import sys                                                    # noqa: E402
from pathlib import Path                                      # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_mutate_gates import MUTATION_LANE  # noqa: E402
from cos_mutate_policy import (  # noqa: E402
    DRAFT_FOLDER, DRAFT_RESUME_POLICY, PENDING, PERMITTED_ACTIONS,
    PERMITTED_FOLDERS, SAVE_ONLY)


# ---------------------------------------------------------------------------
# batch-2 drain: `fault_injection_report` moved verbatim out of `cos_mutate`.
# ---------------------------------------------------------------------------
import sys                                                    # noqa: E402
from pathlib import Path                                      # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_mutate_gates import (  # noqa: E402
    MutationStop, _ts, assert_vault, canary_status, kill_switch)
from cos_mutate_ledger import UndoLedger, _write_text_atomic  # noqa: E402
from cos_mutate_plan import build_plan  # noqa: E402
from cos_mutate_policy import (  # noqa: E402
    DEFAULT_CAPS, DEFAULT_SINCE_DAYS, STATES)


def fault_injection_report() -> dict[str, Any]:
    """RUN the fault-injection suite and report what it returned.

    Not a claim typed into a file: this shells out to the node suite, which
    simulates a response lost AFTER the server accepted the write for every
    verb and asserts the reconciliation query finds the server-side effect.
    """
    script = Path(__file__).resolve().parents[1] / "tests" / "js" \
        / "cos_mutate_page.test.mjs"
    if not script.exists():
        return {"ran": False, "why": f"{script} is missing"}
    proc = subprocess.run(["node", str(script), "--json"], capture_output=True,
                          text=True, timeout=300)
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ran": False, "returncode": proc.returncode,
                "stderr": proc.stderr[-800:], "stdout": proc.stdout[-800:]}
    report["ran"] = True
    report["returncode"] = proc.returncode
    return report
