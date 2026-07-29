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
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "eval"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(EVAL))

from brain import egress  # noqa: E402
from brain.embed import get_embedder  # noqa: E402
from brain.index import BrainIndex, _ExactLeg  # noqa: E402
from brain.vectors import get_backend  # noqa: E402
from harness_direct import METRICS, _metric, _ranked  # noqa: E402


FIXTURE = REPO / "tests" / "fixtures" / "named_entity_vault"
GOLDEN = REPO / "eval" / "fixtures" / "named-entity-golden.json"
QRELS = REPO / "eval" / "fixtures" / "named-entity-qrels.json"
FREEZE = REPO / "eval" / "runs" / "ne-family-freeze.json"


@contextmanager
def _env(**updates: str | None) -> Iterator[None]:
    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _index(vault: Path, directory: Path, name: str) -> BrainIndex:
    idx = BrainIndex(
        db_path=directory / f"{name}.sqlite",
        backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    idx.rebuild(vault)
    return idx


def _note(note_id: str, title: str, body: str, *, classification: str = "Internal",
          aliases: str = "") -> str:
    return (
        f"---\nid: {note_id}\ntitle: \"{title}\"\ntype: note\n"
        f"classification: {classification}\ncreated: 2026-07-28\nupdated: 2026-07-28\n"
        f"{aliases}---\n\n{body}\n"
    )


def _empty_exact() -> _ExactLeg:
    return _ExactLeg([], {}, set(), set(), set(), set(), set(), None, [])


def _invariants() -> dict[str, Any]:
    """Exercise the exact behavior independently of aggregate ranking metrics."""
    with tempfile.TemporaryDirectory(prefix="brain-s02-ne-") as temp:
        root = Path(temp)
        with _env(
            BRAIN_EXACT_LEG_ENABLED="1", BRAIN_RECENCY_WEIGHT="0",
            BRAIN_ZONE_WEIGHTS=None, BRAIN_ZONE_SCOPE="semantic_only",
            BRAIN_DEDUP_THRESHOLD=None, BRAIN_DEDUP_SCOPE=None,
        ):
            idx = _index(FIXTURE, root, "fixture")
            alias = idx.hybrid_search("Cafe\u0301 Aurora", k=8)
            alias_ok = (alias[0].id == "fabrikam-aurora-cafe"
                        and alias[0].evidence == "alias_hit"
                        and alias[0].create_safety == "exists")

            collision = idx.hybrid_search("Orbit", k=24)
            collision_ids = [hit.id for hit in collision]
            collision_ok = (
                collision_ids.index("contoso-orbit-current")
                < collision_ids.index("contoso-orbit-retired")
                and all(hit.create_safety == "probable" for hit in collision
                        if hit.id.startswith("contoso-orbit-"))
            )

            class AdversarialReranker:
                model_id = "s02-adversarial"

                def rerank(self, query: str, passages: list[str]) -> list[float]:
                    return [100.0 if "retired" in passage.lower() else float(pos)
                            for pos, passage in enumerate(passages)]

            unique_reranked = idx.hybrid_search(
                "Hall of Lanterns", k=12, rerank=True,
                reranker=AdversarialReranker(), rerank_top=20,
            )
            collision_reranked = idx.hybrid_search(
                "Orbit", k=24, rerank=True,
                reranker=AdversarialReranker(), rerank_top=20,
            )
            reranked_ids = [hit.id for hit in collision_reranked]
            rerank_ok = (
                unique_reranked[0].id == "contoso-lantern-program"
                and reranked_ids.index("contoso-orbit-current")
                < reranked_ids.index("contoso-orbit-retired")
            )

            # The switch must give the same legacy IDs, order, scores, source,
            # and snippets as a manually empty third leg.
            with _env(BRAIN_EXACT_LEG_ENABLED="0"):
                switched_off = idx.hybrid_search("Northwind Relay", k=16)
            original = idx._exact_leg
            try:
                idx._exact_leg = lambda query, rrf_k: _empty_exact()  # type: ignore[method-assign]
                manual_legacy = idx.hybrid_search("Northwind Relay", k=16)
            finally:
                idx._exact_leg = original  # type: ignore[method-assign]
            def legacy_fields(hits):
                return [(h.id, h.score, h.source, h.snippet) for h in hits]

            kill_switch_ok = legacy_fields(switched_off) == legacy_fields(manual_legacy)

            # Construct a rank-1 organic both anchor plus a partial-title-only
            # candidate. The 0.25 exact tier must not leap the 2-leg anchor.
            phrase_vault = root / "phrase-vault"
            (phrase_vault / "brain" / "resources").mkdir(parents=True)
            (phrase_vault / "raw").mkdir()
            (phrase_vault / "brain" / "index.md").write_text(
                _note("index", "Index", "fixture"), encoding="utf-8"
            )
            (phrase_vault / "brain" / "resources" / "anchor.md").write_text(
                _note("anchor", "Organic Anchor", "Northwind Relay organic anchor."), encoding="utf-8"
            )
            (phrase_vault / "brain" / "resources" / "partial.md").write_text(
                _note("partial", "Northwind Relay Gateway", "different title target"), encoding="utf-8"
            )
            phrase_idx = _index(phrase_vault, root, "phrase")
            anchor_rowid = phrase_idx._rowid_of("anchor")
            anchor_chunk = phrase_idx.conn.execute(
                "SELECT rowid FROM chunks WHERE note_rowid=?", (anchor_rowid,)
            ).fetchone()[0]
            phrase_idx._lexical_ranked = lambda query, n: [anchor_rowid]  # type: ignore[method-assign]
            phrase_idx._dense_ranked = lambda query, n: (  # type: ignore[method-assign]
                [anchor_rowid], {anchor_rowid: "anchor"}, {anchor_rowid: anchor_chunk},
                {anchor_rowid: 0.9},
            )
            phrase_hits = phrase_idx.hybrid_search("Northwind Relay", k=5)
            phrase_ok = (
                phrase_hits[0].id == "anchor"
                and next(hit for hit in phrase_hits if hit.id == "partial").evidence
                == "title_phrase_match"
            )

            # An exact alias is injected before the egress gate but a Restricted
            # owner must still be withheld; visible companions become unknown.
            egress_vault = root / "egress-vault"
            (egress_vault / "brain" / "resources").mkdir(parents=True)
            (egress_vault / "raw").mkdir()
            (egress_vault / "brain" / "index.md").write_text(
                _note("index", "Index", "Internal helper note."), encoding="utf-8"
            )
            (egress_vault / "brain" / "resources" / "restricted.md").write_text(
                _note("restricted", "Restricted Canonical", "Private record.",
                      classification="Restricted", aliases="aliases: ['Hidden Alias']\n"),
                encoding="utf-8",
            )
            egress_idx = _index(egress_vault, root, "egress")
            raw_hits = [hit.to_dict() for hit in egress_idx.hybrid_search("Hidden Alias", k=8)]
            surfaced, report = egress.apply_gate(raw_hits, "Internal")
            egress_idx.annotate_create_safety("Hidden Alias", surfaced, "Internal")
            egress_ok = (
                "restricted" not in {hit["id"] for hit in surfaced}
                and report["withheld"] >= 1
                and all(hit["create_safety"] == "unknown" for hit in surfaced)
            )

            # Full identities are exempt even after score ordering reaches the
            # near-duplicate pass.
            dedup_idx = BrainIndex(
                db_path=root / "dedup.sqlite", backend=get_backend("brute-force"),
                embedder=get_embedder("hash"),
            )
            with _env(BRAIN_DEDUP_THRESHOLD="0.5", BRAIN_DEDUP_SCOPE="all"):
                dedup_idx.backend.get_vectors = lambda conn, ids: {  # type: ignore[method-assign]
                    1: [1.0, 0.0], 2: [1.0, 0.0]
                }
                dedup_ok = dedup_idx._suppress_near_dups(
                    [10, 20], {10: 1, 20: 2}, {10: "brain", 20: "brain"},
                    {10: "brain", 20: "brain"}, set(), {20},
                ) == [10, 20]

    rerank_off_checks = {
        "alias_nfc_nfd": alias_ok,
        "collision_live_before_retired_and_probable": collision_ok,
        "partial_phrase_bound": phrase_ok,
        "egress_after_alias_hop": egress_ok,
        "full_exact_near_duplicate_exemption": dedup_ok,
    }
    adversarial_checks = {
        "unique_pin_and_collision_slots": rerank_ok,
    }
    kill_switch = {
        "legacy_result_equivalence": kill_switch_ok,
    }
    return {
        "rerank_off": {
            "checks": rerank_off_checks,
            "pass": all(rerank_off_checks.values()),
        },
        "adversarial_reranker_on": {
            "checks": adversarial_checks,
            "pass": all(adversarial_checks.values()),
        },
        "kill_switch_baseline_equivalence": {
            "checks": kill_switch,
            "pass": all(kill_switch.values()),
        },
        "pass": all(rerank_off_checks.values()) and all(adversarial_checks.values())
        and all(kill_switch.values()),
    }


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


def main() -> int:
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
    args = parser.parse_args()

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

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    qrels = json.loads(QRELS.read_text(encoding="utf-8"))
    baseline_document = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate_document = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    baseline = baseline_document["runs"]
    candidate = candidate_document["runs"]
    scorecard = json.loads(Path(args.scorecard).read_text(encoding="utf-8"))
    shipped_output = Path(args.shipped_gate_output).read_text(encoding="utf-8")
    negative_output = Path(args.negative_gate_output).read_text(encoding="utf-8")
    unevaluable_output = Path(args.unevaluable_control_output).read_text(encoding="utf-8")
    unevaluable_result: dict[str, Any] | None = None
    unevaluable_result_check = {"arms": {}, "pass": False}
    if args.unevaluable_control_result:
        try:
            unevaluable_result = json.loads(
                Path(args.unevaluable_control_result).read_text(encoding="utf-8")
            )
            unevaluable_result_check = _wholly_unevaluable_control(unevaluable_result)
        except (OSError, ValueError, TypeError):
            # Evidence is fail-closed: a malformed/missing control artifact can
            # never turn a generic FAIL line into proof of this specific guard.
            unevaluable_result_check = {"arms": {}, "pass": False}
    families = _family_metrics(golden, qrels, baseline, candidate)
    invariants = _invariants()

    expected_ids = sorted(query["id"] for query in golden["queries"])
    augmented: dict[str, Any] | None = None
    augmented_qrels: dict[str, Any] | None = None
    expected_augmented_ids = expected_ids
    if args.augmented_golden:
        augmented = json.loads(Path(args.augmented_golden).read_text(encoding="utf-8"))
        expected_augmented_ids = sorted(query["id"] for query in augmented["queries"])
    if args.augmented_qrels:
        augmented_qrels = json.loads(Path(args.augmented_qrels).read_text(encoding="utf-8"))

    paired_ids = scorecard["paired_scope"]["scored_ids"]
    frozen_set = set(expected_ids)
    augmented_set = set(expected_augmented_ids)
    baseline_ids = set(baseline)
    candidate_ids = set(candidate)
    paired_set = set(paired_ids)
    all_augmented_ids_paired = (
        baseline_ids == augmented_set == candidate_ids == paired_set
    )
    all_frozen_ids_paired = frozen_set <= baseline_ids and frozen_set <= candidate_ids and frozen_set <= paired_set
    baseline_params = baseline_document.get("index_state", {}).get("params", {})
    candidate_params = candidate_document.get("index_state", {}).get("params", {})
    rerank_off = baseline_params.get("rerank") is False and candidate_params.get("rerank") is False
    baseline_timing = baseline_document.get("timing")
    candidate_timing = candidate_document.get("timing")
    timing_comparable = (
        isinstance(baseline_timing, dict)
        and baseline_timing == candidate_timing
        and baseline_timing.get("warmup_per_query", 0) >= 1
        and baseline_timing.get("samples_per_query", 0) >= 1
        and baseline_timing.get("within_query_statistic") == "median"
        and baseline_timing.get("paired_capture_cycles", 0) >= 2
        and baseline_timing.get("cross_cycle_statistic") == "median"
    )
    shipped_pass = "GATE: PASS" in shipped_output and rerank_off
    negative_failed = args.negative_exit_code == 1 and "GATE: FAIL" in negative_output
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
    established_evaluability_pass = (
        baseline_evaluability["pass"] and candidate_evaluability["pass"]
        and same_established_path_map
    )
    unevaluable_control_failed = (
        args.unevaluable_control_exit_code == 1
        and "— FAIL" in unevaluable_output
        and unevaluable_result_check["pass"]
    )
    all_family_noninferior = all(item["non_inferior"] for item in families.values())
    materially_improved = [name for name, item in families.items() if item["improved"]]
    result = {
        "schema_version": "s02-ne-upgrade-gate.v3",
        "fixture_freeze": {"hashes": frozen, "hashes_unchanged": hashes_ok},
        "baseline_coverage": {
            "expected_query_ids": expected_ids,
            "augmented_expected_query_ids": expected_augmented_ids,
            "baseline_query_ids": sorted(baseline_ids),
            "candidate_query_ids": sorted(candidate_ids),
            "paired_query_ids": paired_ids,
            "all_frozen_ids_paired": all_frozen_ids_paired,
            "all_augmented_ids_paired": all_augmented_ids_paired,
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
            "pass": shipped_pass,
            "rerank_off": rerank_off,
            "baseline_params": baseline_params,
            "candidate_params": candidate_params,
            "scorecard": args.scorecard,
            "transcript": args.shipped_gate_output,
        },
        "paired_timing": {
            "comparable": timing_comparable,
            "baseline": baseline_timing,
            "candidate": candidate_timing,
        },
        "established_evaluability": {
            "baseline": baseline_evaluability,
            "candidate": candidate_evaluability,
            "same_path_map": same_established_path_map,
            "pass": established_evaluability_pass,
        },
        "per_family_deltas": families,
        "family_summary": {
            "all_noninferior": all_family_noninferior,
            "materially_improved_families": materially_improved,
            "preserved_family": [name for name, item in families.items() if not item["improved"]],
        },
        "invariants": invariants,
        "negative_control": {
            "configuration": "k=1 with BRAIN_EXACT_LEG_ENABLED=0",
            "exit_code": args.negative_exit_code,
            "expected_fail_observed": negative_failed,
            "transcript": args.negative_gate_output,
        },
        "unevaluable_established_control": {
            "configuration": "previous 12-note repository-vault 82-query capture",
            "exit_code": args.unevaluable_control_exit_code,
            "expected_fail_observed": unevaluable_control_failed,
            "transcript": args.unevaluable_control_output,
            "result": args.unevaluable_control_result,
            "wholly_unevaluable": unevaluable_result_check,
        },
    }
    required = (
        hashes_ok and result["baseline_coverage"]["all_frozen_ids_paired"]
        and result["baseline_coverage"]["all_augmented_ids_paired"]
        and result["augmented_input"]["frozen_ids_in_input"]
        and result["augmented_input"]["qrels_cover_input"] and shipped_pass
        and timing_comparable and all_family_noninferior and bool(materially_improved)
        and invariants["pass"] and negative_failed and established_evaluability_pass
        and unevaluable_control_failed
    )
    result["verdict"] = "PASS" if required else "FAIL"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} — {result['verdict']}")
    return 0 if required else 1


if __name__ == "__main__":
    raise SystemExit(main())
