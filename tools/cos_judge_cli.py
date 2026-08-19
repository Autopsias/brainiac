"""The command bodies `cos_judge.main` dispatches to, one function per CLI verb.

`main` owns argument parsing; this module owns what each verb DOES — the
`--judge` report with its abort gates, the `--category-batch` feature-off
state, the `--batches` render, and the `--golden` scoring run. Import
direction is one-way: this module imports nothing from its parent — the
night loader, the category loader, the judge, the night writer, the batch
renderer and the id shortener all arrive as callables, so a test that
patches `cos_judge.load_night` or `cos_judge.write_night` is still honoured.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable


def run_judge(args: argparse.Namespace, *, load_categories, judge_night,
              write_night, short_id: Callable[[str], str]) -> int:
    """`--judge`: validate a verdicts file, apply it, render the brief."""
    if not (args.vault and args.run_id and args.verdicts and args.out):
        print("--judge needs --vault, --run-id, --verdicts and --out",
              file=sys.stderr)
        return 2
    verdicts = json.loads(args.verdicts.read_text(encoding="utf-8"))
    # H2: the footer is derived INSIDE judge_night, over the accepted
    # verdicts — never here over the raw parser output.
    judged = judge_night(args.vault, args.run_id, verdicts,
                         grounding=args.grounding,
                         chunks_dir=args.chunks_dir,
                         out_dir=args.out.parent,
                         categories=load_categories(args.categories))
    report: dict[str, Any] = {
        "run_id": args.run_id,
        "verdicts_returned": len(verdicts),
        "verdicts_total": len(judged["applied"]["rows"]),
        "verdicts_accepted": len(judged["accepted"]),
        "vocabulary_rejection_rate": judged["rejection_rate"],
        "vocabulary_rejection_abort_threshold": args.reject_abort,
        "rejected": judged["rejected"],
        "run_facts": judged["run_facts"],
        "run_violations": judged["run_violations"],
        "brief_violations": judged["brief_violations"],
        "counters": judged["applied"]["counters"],
        "judgment_pending_rows": judged["applied"]["judgment_pending"],
        "held_reasons": judged["holds"],
    }
    c = judged["applied"]["counters"]
    # Still checked, and still able to fail: it fires whenever these
    # counters stop satisfying the SHARED definition's identity — which is
    # precisely the drift that made run 121 unappendable.
    unaccounted = (c["ingestion_in_scope"] - c["ingestion_candidates"]
                   - c["ingestion_held"])
    report["unaccounted_rows"] = unaccounted
    # H4 (Codex HIGH). A coverage floor against a silent partial parse:
    # `extract_objects` succeeds on ANY object, so a 2-of-251 disaster passes
    # the `-s` gate and its 249 unanswered rows go silently PENDING. Coverage
    # is ALWAYS logged; below a CONSERVATIVE floor (default 0.5 — run 131
    # answered ~all 251, so a normal night never trips it) the night is
    # READ-ONLY, the same survivable path the rejection-rate abort uses.
    cov = judged["run_facts"]["model_coverage"]
    report["model_coverage"] = cov
    import os                                                 # noqa: PLC0415
    min_coverage = float(os.environ.get("BRAIN_COS_MIN_COVERAGE", "0.5"))
    # RECORDED, not merely applied (DOCTRINE v7 §8.2 E9). The floor decided
    # whether this night proceeded, so "coverage is at or above the floor it
    # recorded" needs the floor to BE recorded — an env var read at judgment
    # time and thrown away cannot be re-read by a validator hours later.
    report["model_coverage_floor"] = min_coverage
    coverage_short = cov["fraction"] < min_coverage
    if judged["rejection_rate"] > args.reject_abort or unaccounted \
            or coverage_short:
        report["stopped"] = (
            f"{judged['rejection_rate']:.1%} of verdicts were refused by the "
            f"closed vocabulary (abort threshold {args.reject_abort:.0%}) and "
            f"{unaccounted} row(s) are accounted NOWHERE. Nothing was "
            "written: a refused verdict leaves its row unjudged, and a night "
            "whose counters do not close is the run-106 shape (15 rows in no "
            "total, and nothing said so). Correct the refused verdicts and "
            "re-run.")
        if coverage_short:
            report["stopped"] += (
                f" READ-ONLY: the model answered only {cov['answered']} of "
                f"{cov['enumerated']} enumerated conversations "
                f"({cov['fraction']:.1%}, floor {min_coverage:.0%}) — a "
                "mostly-unanswered batch is a silent partial parse, not a "
                "night to plan or apply.")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False)
                            + "\n", encoding="utf-8")
        print(report["stopped"], file=sys.stderr)
        return 3
    report["written"] = write_night(args.vault, args.run_id, judged,
                                    out_dir=args.out.parent)
    report["staging_candidates"] = staging_candidate_rows(judged["staged"],
                                                          short_id)
    report["drafts_written"] = drafts_written_rows(judged["drafts"], short_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("rejected", "staging_candidates",
                                   "drafts_written")}, indent=2))
    return 0


def staging_candidate_rows(staged: list[dict[str, Any]],
                           short_id: Callable[[str], str]) -> list[dict[str, Any]]:
    """The report's candidate digest rows — digested ids, never mailbox ids."""
    return [
        {"conversation_id": short_id(c["conversation_id"]),
         "category": c.get("category"), "substance_kind": c.get("substance_kind"),
         "rule2_class": c.get("dedup_kind"),
         "merge_candidate": c.get("merge_candidate"),
         "dedup_check": c.get("dedup_check"),
         "classification": c.get("classification"),
         "evidence_span": c.get("evidence_span")}
        for c in staged]


def drafts_written_rows(drafts: list[dict[str, Any]],
                        short_id: Callable[[str], str]) -> list[dict[str, Any]]:
    """The report's draft rows — counts and shapes, never draft text."""
    return [
        {"conversation_id": short_id(d["conversation_id"]),
         "chars": len((d.get("draft") or {}).get("text") or ""),
         "form": (d.get("draft") or {}).get("form"),
         "placeholders": len((d.get("draft") or {}).get("placeholders") or []),
         "saved_to_mailbox": False}
        for d in drafts]


def run_category_batch(args: argparse.Namespace, *, category_batch) -> int:
    """`--category-batch`: render the PRE-DRAW category batch."""
    if not (args.vault and args.enumeration and args.out):
        print("--category-batch needs --vault, --enumeration and --out",
              file=sys.stderr)
        return 2
    from brain import cos                                         # noqa: PLC0415
    enumeration = json.loads(args.enumeration.read_text(encoding="utf-8"))
    taxonomy = (cos.ingest_taxonomy(args.vault) or {})
    rules = taxonomy.get("rules") or {}
    if taxonomy.get("mode") != "active" or not rules:
        # THE FEATURE-OFF STATE, SAID OUT LOUD. `category_stamp` scores an
        # inactive taxonomy as PASS with every row null, so rendering a
        # batch over an empty vocabulary would ask the model for a stamp it
        # is not allowed to give. Exit 4 so a caller can tell "no taxonomy"
        # apart from "the batch failed".
        print(json.dumps({"run_id": enumeration.get("run_id"),
                          "taxonomy_mode": taxonomy.get("mode"),
                          "stopped": "the owner's ingest taxonomy is not "
                                     "active, so rule 1¾ is not in force and "
                                     "there is no category to stamp"},
                         indent=2))
        return 4
    text = category_batch(enumeration.get("rows") or [], rules,
                          redact=args.redact)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(json.dumps({"run_id": enumeration.get("run_id"),
                      "rows": len(enumeration.get("rows") or []),
                      "categories_offered": sorted(rules),
                      "never_categories": sorted(
                          k for k, v in rules.items()
                          if str((v or {}).get("disposition") or "").lower()
                          == "never"),
                      "batch": str(args.out)}, indent=2))
    return 0


def run_batches(args: argparse.Namespace, *, load_night, load_categories,
                batch_prompts) -> int:
    """`--batches`: render the four judgment batch prompts for a run."""
    if not (args.vault and args.run_id and args.out):
        print("--batches needs --vault, --run-id and --out", file=sys.stderr)
        return 2
    night = load_night(args.vault, args.run_id,
                       load_categories(args.categories))
    prompts = batch_prompts(night["rows"], night["ctx_by_id"],
                            night["taxonomy"], redact=args.redact)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in prompts.items():
        (args.out / f"batch-{name}.md").write_text(text, encoding="utf-8")
    judgeable = sum(1 for c in night["ctx_by_id"].values()
                    if c["typed_fields_available"])
    print(json.dumps({"run_id": args.run_id, "rows": len(night["rows"]),
                      "typed_fields_available": judgeable,
                      "bodies_captured": sum(1 for r in night["rows"]
                                             if r.get("body_opened")),
                      "batches": sorted(prompts)}, indent=2))
    return 0


def run_golden(args: argparse.Namespace, *, evaluate_golden) -> int:
    """`--golden`: score the validator (and, where given, a model's answers)."""
    answers = json.loads(args.answers.read_text()) if args.answers else None
    report = evaluate_golden(answers)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passes_thresholds"] else 1
