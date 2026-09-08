#!/usr/bin/env python3
"""Produce the reproducible S02 named-entity retrieval evidence bundle.

The ordinary gate has no per-family stratum and cannot prove its own negative
control, so this small companion verifies the frozen fixture hashes, computes
family deltas explicitly, exercises the ADR-0008 invariants, and records the
ordinary gate's PASS/FAIL transcripts without loosening either gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(EVAL))

from harness_direct import METRICS, _metric, _ranked  # noqa: E402
from ne_upgrade_invariants import (  # noqa: E402  (facade re-export)
    FIXTURE as FIXTURE,
    _invariants as _invariants,
)


GOLDEN = REPO / "eval" / "fixtures" / "named-entity-golden.json"
QRELS = REPO / "eval" / "fixtures" / "named-entity-qrels.json"
FREEZE = REPO / "eval" / "runs" / "ne-family-freeze.json"


def _family_metrics(golden: dict, qrels: dict, baseline: dict, candidate: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for family, spec in golden["families"].items():
        ids = spec["query_ids"]
        metrics: dict[str, Any] = {}
        for metric in METRICS:
            current = sum(_metric(metric, qrels[qid], _ranked(baseline[qid])) for qid in ids) / len(ids)
            new = sum(_metric(metric, qrels[qid], _ranked(candidate[qid])) for qid in ids) / len(ids)
            metrics[metric] = {
                "current": round(current, 6), "new": round(new, 6),
                "delta": round(new - current, 6),
            }
        out[family] = {
            "query_ids": ids,
            "metrics": metrics,
            "non_inferior": all(value["delta"] >= 0 for value in metrics.values()),
            "improved": any(value["delta"] > 0 for value in metrics.values()),
        }
    return out


def _established_evaluability(document: dict[str, Any], expected_ids: list[str]) -> dict[str, Any]:
    """Validate that the established arm can actually score its qrels.

    ``harness_direct`` intentionally intersects runs/qrels before computing
    metrics.  That is correct for generic A/B use, but it meant a capture over
    the twelve-note repository vault could look paired while every established
    qrel was absent.  S02's augmented evidence must fail closed instead: every
    established query needs an indexed migrated qrel target in *each* arm.
    """
    scope = document.get("scope", {})
    component = scope.get("components", {}).get("established", {})
    evidence = component.get("qrel_evaluability", {})
    expected = sorted(expected_ids)
    evaluable = sorted(evidence.get("evaluable_query_ids", []))
    unevaluable = sorted(evidence.get("unevaluable_query_ids", []))
    captured = sorted(scope.get("queries_captured", []))
    missing_from_capture = sorted(set(expected) - set(captured))
    failures: list[str] = []
    if sorted(evidence.get("expected_query_ids", [])) != expected:
        failures.append("expected IDs do not match the established golden set")
    if evaluable != expected:
        failures.append("not every established query has an evaluable qrel target")
    if unevaluable:
        failures.append("established component reports unevaluable query IDs")
    if missing_from_capture:
        failures.append("established query IDs are missing from the capture")
    if evidence.get("qrel_document_count", 0) <= 0:
        failures.append("no established qrel documents were recorded")
    if evidence.get("mapped_document_count") != evidence.get("qrel_document_count"):
        failures.append("not every established qrel document was mapped")
    if evidence.get("unmapped_canonical_documents"):
        failures.append("unmapped established canonical documents remain")
    if evidence.get("unindexed_mapping_paths"):
        failures.append("mapped established documents are absent from the index")
    if evidence.get("complete") is not True:
        failures.append("established evaluability is not complete")
    return {
        "expected_query_ids": expected,
        "evaluable_query_ids": evaluable,
        "unevaluable_query_ids": unevaluable,
        "qrel_document_count": evidence.get("qrel_document_count", 0),
        "mapped_document_count": evidence.get("mapped_document_count", 0),
        "path_map_sha256": evidence.get("path_map_sha256", ""),
        "missing_from_capture": missing_from_capture,
        "failures": failures,
        "pass": not failures,
    }


def _wholly_unevaluable_control(document: dict[str, Any]) -> dict[str, Any]:
    """Verify that a negative-control result failed for an empty old stratum.

    A plain ``GATE: FAIL`` transcript is not enough: the negative-control gate
    can fail for an unrelated latency or fixture reason.  This compact check
    proves the recorded control had established queries but zero evaluable qrel
    targets in both source arms.
    """
    established = document.get("established_evaluability", {})
    arms = {name: established.get(name, {}) for name in ("baseline", "candidate")}
    arm_checks: dict[str, bool] = {}
    for name, value in arms.items():
        expected = value.get("expected_query_ids", [])
        evaluable = value.get("evaluable_query_ids", [])
        arm_checks[name] = (
            isinstance(expected, list)
            and bool(expected)
            and evaluable == []
            and value.get("pass") is False
        )
    return {
        "arms": {
            name: {
                "expected_query_count": len(value.get("expected_query_ids", [])),
                "evaluable_query_count": len(value.get("evaluable_query_ids", [])),
                "pass": value.get("pass"),
            }
            for name, value in arms.items()
        },
        "pass": all(arm_checks.values()),
    }


def _build_parser() -> argparse.ArgumentParser:
    """The companion gate's command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--scorecard", required=True)
    parser.add_argument("--shipped-gate-output", required=True)
    parser.add_argument("--negative-gate-output", required=True)
    parser.add_argument("--negative-exit-code", type=int, required=True)
    parser.add_argument("--unevaluable-control-output", required=True,
                        help="Transcript from this companion gate run against a wholly "
                        "unevaluable established component.")
    parser.add_argument("--unevaluable-control-exit-code", type=int, required=True)
    parser.add_argument("--unevaluable-control-result", default=None,
                        help="JSON emitted by the companion gate. When supplied, it must "
                             "prove zero evaluable established qrels in both arms.")
    parser.add_argument("--augmented-golden", default=None,
                        help="Combined local + frozen golden input used by harness_direct.")
    parser.add_argument("--augmented-qrels", default=None,
                        help="Matching combined qrels input used by harness_direct.")
    parser.add_argument("--out", required=True)
    return parser


def _frozen_fixture_hashes() -> tuple[list[dict[str, Any]], bool]:
    """Re-hash every frozen fixture, and say whether all of them still match."""
    freeze = json.loads(FREEZE.read_text(encoding="utf-8"))
    frozen: list[dict[str, Any]] = []
    hashes_ok = True
    for item in freeze["fixtures"]:
        path = REPO / item["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        matched = actual == item["sha256"]
        hashes_ok &= matched
        frozen.append({"path": item["path"], "expected": item["sha256"], "actual": actual,
                       "matched": matched, "query_ids": item["query_ids"]})
    return frozen, hashes_ok


def _unevaluable_result_check(path: str | None) -> dict[str, Any]:
    """The wholly-unevaluable control's own verdict, fail-closed.

    Evidence is fail-closed: a malformed/missing control artifact can never
    turn a generic FAIL line into proof of this specific guard."""
    if not path:
        return {"arms": {}, "pass": False}
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return _wholly_unevaluable_control(document)
    except (OSError, ValueError, TypeError):
        return {"arms": {}, "pass": False}


def _timing_comparable(baseline_timing: Any, candidate_timing: Any) -> bool:
    """True when both arms were captured under the same paired timing regime."""
    return (
        isinstance(baseline_timing, dict)
        and baseline_timing == candidate_timing
        and baseline_timing.get("warmup_per_query", 0) >= 1
        and baseline_timing.get("samples_per_query", 0) >= 1
        and baseline_timing.get("within_query_statistic") == "median"
        and baseline_timing.get("paired_capture_cycles", 0) >= 2
        and baseline_timing.get("cross_cycle_statistic") == "median"
    )


def _build_result(args, frozen, hashes_ok, families, invariants, ids,
                  documents, transcripts) -> dict[str, Any]:
    """The evidence bundle. Every judgement it carries is computed here."""
    expected_ids, expected_augmented_ids, paired_ids = ids["expected"], ids["augmented"], ids["paired"]
    baseline_document, candidate_document = documents["baseline"], documents["candidate"]
    augmented, augmented_qrels = documents["augmented"], documents["augmented_qrels"]

    frozen_set, augmented_set = set(expected_ids), set(expected_augmented_ids)
    baseline_ids, candidate_ids = set(baseline_document["runs"]), set(candidate_document["runs"])
    paired_set = set(paired_ids)

    baseline_params = baseline_document.get("index_state", {}).get("params", {})
    candidate_params = candidate_document.get("index_state", {}).get("params", {})
    rerank_off = baseline_params.get("rerank") is False and candidate_params.get("rerank") is False
    baseline_timing = baseline_document.get("timing")
    candidate_timing = candidate_document.get("timing")

    # The frozen synthetic family is the known set difference.  Do not infer
    # component membership from an ID prefix: the established corpus owns its
    # identifiers and may add an `ne_*`-looking ID in a future revision.
    established_ids = sorted(augmented_set - frozen_set)
    baseline_evaluability = _established_evaluability(baseline_document, established_ids)
    candidate_evaluability = _established_evaluability(candidate_document, established_ids)
    same_established_path_map = (
        bool(baseline_evaluability["path_map_sha256"])
        and baseline_evaluability["path_map_sha256"]
        == candidate_evaluability["path_map_sha256"]
    )
    return {
        "schema_version": "s02-ne-upgrade-gate.v3",
        "fixture_freeze": {"hashes": frozen, "hashes_unchanged": hashes_ok},
        "baseline_coverage": {
            "expected_query_ids": expected_ids,
            "augmented_expected_query_ids": expected_augmented_ids,
            "baseline_query_ids": sorted(baseline_ids),
            "candidate_query_ids": sorted(candidate_ids),
            "paired_query_ids": paired_ids,
            "all_frozen_ids_paired": (frozen_set <= baseline_ids
                                      and frozen_set <= candidate_ids
                                      and frozen_set <= paired_set),
            "all_augmented_ids_paired": (
                baseline_ids == augmented_set == candidate_ids == paired_set),
        },
        "augmented_input": {
            "provided": augmented is not None,
            "query_count": len(expected_augmented_ids),
            "frozen_ids_in_input": frozen_set <= augmented_set,
            "qrels_cover_input": (
                augmented_qrels is not None and augmented_set <= set(augmented_qrels)
            ) if augmented is not None else True,
        },
        "shipped_rerank_off_gate": {
            "pass": "GATE: PASS" in transcripts["shipped"] and rerank_off,
            "rerank_off": rerank_off,
            "baseline_params": baseline_params,
            "candidate_params": candidate_params,
            "scorecard": args.scorecard,
            "transcript": args.shipped_gate_output,
        },
        "paired_timing": {
            "comparable": _timing_comparable(baseline_timing, candidate_timing),
            "baseline": baseline_timing,
            "candidate": candidate_timing,
        },
        "established_evaluability": {
            "baseline": baseline_evaluability,
            "candidate": candidate_evaluability,
            "same_path_map": same_established_path_map,
            "pass": (baseline_evaluability["pass"] and candidate_evaluability["pass"]
                     and same_established_path_map),
        },
        "per_family_deltas": families,
        "family_summary": {
            "all_noninferior": all(item["non_inferior"] for item in families.values()),
            "materially_improved_families": [
                name for name, item in families.items() if item["improved"]],
            "preserved_family": [
                name for name, item in families.items() if not item["improved"]],
        },
        "invariants": invariants,
        "negative_control": {
            "configuration": "k=1 with BRAIN_EXACT_LEG_ENABLED=0",
            "exit_code": args.negative_exit_code,
            "expected_fail_observed": (args.negative_exit_code == 1
                                       and "GATE: FAIL" in transcripts["negative"]),
            "transcript": args.negative_gate_output,
        },
        "unevaluable_established_control": {
            "configuration": "previous 12-note repository-vault 82-query capture",
            "exit_code": args.unevaluable_control_exit_code,
            "expected_fail_observed": (
                args.unevaluable_control_exit_code == 1
                and "— FAIL" in transcripts["unevaluable"]
                and transcripts["unevaluable_check"]["pass"]),
            "transcript": args.unevaluable_control_output,
            "result": args.unevaluable_control_result,
            "wholly_unevaluable": transcripts["unevaluable_check"],
        },
    }


def _required_pass(result: dict[str, Any]) -> bool:
    """Every clause the verdict depends on, read back off the bundle itself."""
    return bool(
        result["fixture_freeze"]["hashes_unchanged"]
        and result["baseline_coverage"]["all_frozen_ids_paired"]
        and result["baseline_coverage"]["all_augmented_ids_paired"]
        and result["augmented_input"]["frozen_ids_in_input"]
        and result["augmented_input"]["qrels_cover_input"]
        and result["shipped_rerank_off_gate"]["pass"]
        and result["paired_timing"]["comparable"]
        and result["family_summary"]["all_noninferior"]
        and result["family_summary"]["materially_improved_families"]
        and result["invariants"]["pass"]
        and result["negative_control"]["expected_fail_observed"]
        and result["established_evaluability"]["pass"]
        and result["unevaluable_established_control"]["expected_fail_observed"]
    )


def main() -> int:
    args = _build_parser().parse_args()

    frozen, hashes_ok = _frozen_fixture_hashes()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    qrels = json.loads(QRELS.read_text(encoding="utf-8"))
    baseline_document = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate_document = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    scorecard = json.loads(Path(args.scorecard).read_text(encoding="utf-8"))
    transcripts = {
        "shipped": Path(args.shipped_gate_output).read_text(encoding="utf-8"),
        "negative": Path(args.negative_gate_output).read_text(encoding="utf-8"),
        "unevaluable": Path(args.unevaluable_control_output).read_text(encoding="utf-8"),
        "unevaluable_check": _unevaluable_result_check(args.unevaluable_control_result),
    }
    families = _family_metrics(golden, qrels, baseline_document["runs"],
                               candidate_document["runs"])
    invariants = _invariants()

    expected_ids = sorted(query["id"] for query in golden["queries"])
    augmented = augmented_qrels = None
    expected_augmented_ids = expected_ids
    if args.augmented_golden:
        augmented = json.loads(Path(args.augmented_golden).read_text(encoding="utf-8"))
        expected_augmented_ids = sorted(query["id"] for query in augmented["queries"])
    if args.augmented_qrels:
        augmented_qrels = json.loads(Path(args.augmented_qrels).read_text(encoding="utf-8"))

    result = _build_result(
        args, frozen, hashes_ok, families, invariants,
        {"expected": expected_ids, "augmented": expected_augmented_ids,
         "paired": scorecard["paired_scope"]["scored_ids"]},
        {"baseline": baseline_document, "candidate": candidate_document,
         "augmented": augmented, "augmented_qrels": augmented_qrels},
        transcripts)

    required = _required_pass(result)
    result["verdict"] = "PASS" if required else "FAIL"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} — {result['verdict']}")
    return 0 if required else 1


if __name__ == "__main__":
    raise SystemExit(main())
