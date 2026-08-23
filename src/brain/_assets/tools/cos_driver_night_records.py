"""The written record of a COS night: evidence skeleton, ledger/corpus/
contract artifacts, replay digest.

Sub-steps of ``cos_driver._run_night`` that produce what a night leaves on
disk. ``_run_night`` keeps its name and module (the tests, the replay check
and the nightly's expectations name it there); every parent callable or
constant this needs (``write_jsonl``, ``write_corpus``,
``build_contract_inputs``, ``write_report``, ``READ_LANE``,
``DIFF_EXCLUDED``) arrives as a parameter — this module never imports
``cos_driver``, so a monkeypatched parent attribute keeps working. The
metrics row deliberately stays in the parent: a test pins the literal
``"read_lane": READ_LANE`` to the driver's own source.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

REPLAY_TIMEOUT_S = 900


def night_evidence_skeleton(run_id: str, manifest_lane: str, vault: Path,
                            ops: Path, raw_sources: int, started_at: str,
                            read_lane: str,
                            diff_excluded: dict[str, str]) -> dict[str, Any]:
    """The evidence record declared UP FRONT, zeros visible before the night
    earns its numbers."""
    return {
        "session": "s02", "item": "REST-02",
        "run_id": run_id,
        "run_id_source": "host-stamped MAN-01 sheet (brain cos-run-begin)",
        "manifest_lane": manifest_lane,
        "manifest_lane_accepted": True,
        "driver_read_lane": read_lane,
        "vault_root_asserted": {
            "BRAIN_VAULT": str(vault),
            "raw_source_count_at_preflight": raw_sources,
            "cos_ops_exists": ops.is_dir(),
            "asserted_before_any_browser_action": True,
        },
        "started_at": started_at,
        # Declared UP FRONT and overwritten as the night earns them. A stop then
        # leaves a complete-shaped record whose zeros are visible, instead of an
        # absence a reader has to interpret (WAT-01: ship the failure mode with
        # the number that reveals it).
        "bodies_attempted": 0,
        "bodies_succeeded": 0,
        "bodies_error": 0,
        "seed_kind": None,
        "contract": {"exit_code": None, "render": "not reached"},
        "host_checks_executed": [],
        "second_process_diff": None,
        "excluded_fields": diff_excluded,
        "fixture_ref": None,
    }


def write_night_artifacts(
        vault: Path, ops: Path, run_id: str, capture: dict[str, Any],
        accounting: dict[str, Any], report: dict[str, Any],
        enumerated_at: str, reported_at: str, *,
        write_jsonl: Callable[[Path, list[dict[str, Any]]], None],
        write_corpus: Callable[..., dict[str, Any]],
        build_contract_inputs: Callable[..., tuple[dict, dict]],
        write_report: Callable[..., None]) -> dict[str, Any]:
    """Ledger, corpus, contract PRE/POST snapshots, sent baseline, nightly
    report — everything the metrics row (kept in the parent) is written
    beside."""
    ledger = ops / f"_cos_ingestion_ledger_{run_id}.jsonl"
    write_jsonl(ledger, accounting["rows"])
    corpus = write_corpus(vault, run_id, accounting, capture)
    pre, post = build_contract_inputs(capture, accounting, run_id=run_id,
                                      enumerated_at=enumerated_at,
                                      reported_at=reported_at)
    pre_path = ops / f"cos_contract_pre_{run_id}.json"
    post_path = ops / f"cos_contract_post_{run_id}.json"
    pre_path.write_text(json.dumps(pre, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    post_path.write_text(json.dumps(post, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    (ops / f"_cos_sent_baseline_{run_id}.json").write_text(
        json.dumps(pre["sent_zero_send"], indent=2) + "\n", encoding="utf-8")
    write_report(ops / f"_cos_nightly_{run_id}.md", run_id, accounting, report)
    return {"ledger": ledger, "corpus": corpus, "pre_path": pre_path,
            "post_path": post_path}


def replay_determinism(vault: Path, run_id: str,
                       replay_script: Path) -> tuple[Any, dict[str, Any]]:
    """The second-process determinism replay: (second_process_diff,
    determinism) exactly as the evidence record carries them."""
    replay = subprocess.run(
        [sys.executable, str(replay_script),
         "--vault", str(vault), "--run-id", run_id],
        capture_output=True, text=True, timeout=REPLAY_TIMEOUT_S)
    try:
        rep = json.loads(replay.stdout)
        return rep["second_process_diff"], {k: rep[k] for k in
                                            ("method", "rows_live", "rows_replayed",
                                             "excluded_fields",
                                             "enumerated_at_compared_as_one_value")}
    except (ValueError, KeyError):
        return None, {"error": replay.stderr[-800:]}


def fixture_ref(corpus_appended: Any, run_id: str,
                bodies: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "host-only COS capture corpus (never in this repository)",
        "run_id": run_id,
        "rows": corpus_appended,
        "schema": "brain.cos_corpus CORPUS_SCHEMA + `extraction` census facts",
        "response_digests": sorted(
            b["body_sha256"] for b in bodies if b.get("body_sha256")),
        "why_not_here": ("the raw responses are real message bodies, classified "
                         "MNPI; `_evidence/` is inside the repository and no "
                         "retention or deletion guarantee reaches git history"),
    }
