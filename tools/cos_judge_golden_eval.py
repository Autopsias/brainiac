"""The golden-set evaluator of `cos_judge` — split out of `cos_judge_brief`.

The brief renderer and the golden scorer never shared a line of state; they
shared a file only because both were drained out of `cos_judge` in one batch.
The file-size ratchet is what separated them (2026-08-28), and the seam is the
one that was already drawn as a comment banner. Every name is re-exported by
`cos_judge_brief`, so `cos_judge.evaluate_golden` keeps its module path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_judge_rules import JudgeStop, RULES           # noqa: E402
from cos_judge_rules_2 import (  # noqa: E402
    check_one)


GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / \
    "cos_judgment_golden.json"


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    out.update(patch)
    return out


def evaluate_golden(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score the validator (and, where given, a model's answers) per RULE."""
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    import importlib.util                                        # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        "_golden_bases", GOLDEN.parent / "build_cos_judgment_golden.py")
    mod = importlib.util.module_from_spec(spec)
    mod.OUT = Path("/dev/null")
    spec.loader.exec_module(mod)

    per_rule: dict[str, dict[str, Any]] = {}
    failures = []
    for case in doc["cases"]:
        rid = case["rule_id"]
        slot = per_rule.setdefault(rid, {"rule_id": rid, "n": 0, "errors": 0})
        slot["n"] += 1
        kind = case["kind"]
        if kind == "row":
            v = _merge(mod.BASE_ROW, case["patch"])
            ctx = _merge(mod.BASE_CTX, case["ctx_patch"])
            got = check_one(rid, v, ctx)
        elif kind == "run":
            got = check_one(rid, case["run"], None)
        elif kind == "brief":
            got = check_one(rid, case["html"], {})
        elif kind == "judgment":
            if not answers:
                slot["n"] -= 1
                continue
            got = _score_judgment(case, answers.get(case["case_id"]))
        else:                                                    # pragma: no cover
            raise JudgeStop(f"unknown case kind {kind!r}")
        # A `judgment` case has no `accept` key: its label IS the answer, so the
        # only passing outcome is agreement with it.
        want = bool(case["label"].get("accept", True))
        if (got is None) != want:
            slot["errors"] += 1
            failures.append({"case_id": case["case_id"], "rule_id": rid,
                             "kind": kind, "expected_accept": want,
                             "got": got or "accepted"})
    rows = []
    for rid, slot in sorted(per_rule.items()):
        n = slot["n"]
        slot["error_rate"] = round(slot["errors"] / n, 4) if n else 0.0
        rows.append(slot)
    th = doc["thresholds"]
    scored = [r for r in rows if r["n"]]
    total_n = sum(r["n"] for r in scored)
    total_e = sum(r["errors"] for r in scored)
    return {
        "golden_set_ref": str(GOLDEN.relative_to(GOLDEN.parents[2])),
        "golden_set_size": len(doc["cases"]),
        "label_provenance": doc["label_provenance"],
        "thresholds": th,
        "rules_total": len(RULES),
        "rules_covered": sorted({c["rule_id"] for c in doc["cases"]}),
        "coverage_gaps": sorted(set(RULES) - {c["rule_id"] for c in doc["cases"]}),
        "per_rule_results": rows,
        "overall_error_rate": round(total_e / total_n, 4) if total_n else 0.0,
        "worst_rule_error_rate": max((r["error_rate"] for r in scored), default=0.0),
        "failures": failures,
        "passes_thresholds": (
            not (set(RULES) - {c["rule_id"] for c in doc["cases"]})
            and (total_e / total_n if total_n else 0) <= th["per_rule_error_rate_max"]
            and max((r["error_rate"] for r in scored), default=0.0)
            <= th["single_rule_error_rate_max"]),
    }


def _score_judgment(case: dict[str, Any], answer: dict[str, Any] | None) -> str | None:
    """A judgment case is scored on its LABELLED FIELDS only."""
    if not answer:
        return "no answer returned for this case"
    for k, want in case["label"].items():
        got = answer.get(k)
        if isinstance(want, bool) or want is None:
            if bool(got) != bool(want):
                return f"{k}: model said {got!r}, label says {want!r}"
        elif str(got) != str(want):
            return f"{k}: model said {got!r}, label says {want!r}"
    return None
