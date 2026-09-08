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
import collections
import json
import sys
from typing import Any, Callable


def rejection_causes(rejected: list[dict[str, Any]]) -> str:
    """`rule_id ×N`, worst first — WHAT actually refused these verdicts.

    (JUDGE-03, 2026-08-27) The abort line called the cause "the closed
    vocabulary" for every stop. On run 2026-08-26-run189 that was wrong for all
    ten refusals: none was an out-of-vocabulary word, every one was a doctrine
    RULE violation (`triage.p3_act_needs_direct_ask` ×5,
    `triage.noise_signal_required` ×4, `triage.stale_evidence` ×1), and the
    reader of the stop line went hunting for a missing word. `rejected[]`
    already carries `rule_id` and `detail` per row; this reads them.

    A row may carry SEVERAL violations, so these counts sum to at least the
    number of refused rows and the caller states both numbers.
    """
    counts = collections.Counter(
        str(v.get("rule_id") or "<unnamed rule>")
        for row in rejected for v in (row.get("violations") or []))
    if not counts:
        return "no rule was named"
    return ", ".join(f"{rid} ×{n}" for rid, n in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def run_judge(args: argparse.Namespace, *, load_categories, load_selection,
              judge_night, write_night, short_id: Callable[[str], str]) -> int:
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
                         categories=load_categories(args.categories),
                         selection=load_selection(getattr(args, "selection", None)))
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
        # NAME THE RULES THAT FIRED, NOT A CAUSE NOBODY CHECKED (JUDGE-03).
        # A refusal comes from a doctrine RULE — an out-of-vocabulary word is
        # only one of the ~40 that can fire — so the line prints the tally the
        # report already holds instead of guessing at the vocabulary.
        report["rejection_causes"] = rejection_causes(judged["rejected"])
        report["stopped"] = (
            f"{judged['rejection_rate']:.1%} of verdicts "
            f"({len(judged['rejected'])} of {len(judged['applied']['rows'])}) "
            "were REFUSED by the doctrine rules — "
            f"{report['rejection_causes']} — against an abort threshold of "
            f"{args.reject_abort:.0%}, and "
            f"{unaccounted} row(s) are accounted NOWHERE. Nothing was "
            "written: a refused verdict leaves its row unjudged, and a night "
            "whose counters do not close is the run-106 shape (15 rows in no "
            "total, and nothing said so). Each refusal's `detail` is in this "
            "report's `rejected[]`. Correct the refused verdicts and re-run.")
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
                load_selection=lambda _path: None, batch_prompts) -> int:
    """`--batches`: render the four judgment batch prompts for a run."""
    if not (args.vault and args.run_id and args.out):
        print("--batches needs --vault, --run-id and --out", file=sys.stderr)
        return 2
    selection = load_selection(getattr(args, "selection", None))
    night = (load_night(args.vault, args.run_id,
                        load_categories(args.categories))
             if selection is None
             else load_night(args.vault, args.run_id,
                             load_categories(args.categories), selection))
    prompts = batch_prompts(night["rows"], night["ctx_by_id"],
                            night["taxonomy"], redact=args.redact,
                            voice_profile=night.get("voice_profile"),
                            rulings=night.get("rulings"))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in prompts.items():
        (args.out / f"batch-{name}.md").write_text(text, encoding="utf-8")
    judgeable = sum(1 for c in night["ctx_by_id"].values()
                    if c["typed_fields_available"])
    voice = night.get("voice_profile") or {}
    print(json.dumps({"run_id": args.run_id, "rows": len(night["rows"]),
                      # THE RULINGS THE CEILING LEFT OUT, NAMED (FB-03). The
                      # prompt carries the COUNT — printing 281 conversation
                      # digests into a model message is the ~250-row overflow
                      # the ceiling exists to prevent. This is where they cost
                      # nothing, so this is where they are named. A ruling above
                      # the ceiling is stored, counted and named, never dropped;
                      # and the mutation lane refuses every do-not-touch thread
                      # off the FULL record whether it was rendered or not.
                      **_rulings_report(night.get("rulings")),
                      "typed_fields_available": judgeable,
                      # LOUD ON THE WAY OUT. The nightly logs this line, so a
                      # vault with no overlay voice profile says so in the run
                      # log at the moment the draft prompt is written.
                      "voice_profile_present": bool(voice.get("present")),
                      "voice_profile_degradation": voice.get("degradation"),
                      "bodies_captured": sum(1 for r in night["rows"]
                                             if r.get("body_opened")),
                      "batches": sorted(prompts)}, indent=2))
    return 0


def _rulings_report(budget: dict | None) -> dict:
    """What `--batches` prints about the owner-feedback record, per kind."""
    if budget is None:
        return {"rulings": {"state": "not-read"}}
    rules, threads = budget["rules"], budget["thread_rulings"]
    return {"rulings": {
        "state": "read", "record": budget["path"],
        "unreadable_lines": budget["unreadable"],
        "rules_live": rules["live"], "rules_rendered": len(rules["rendered"]),
        "rules_excluded": rules["excluded"],
        "rules_excluded_keys": rules["excluded_keys"],
        "thread_rulings_stored": threads["stored"],
        "thread_rulings_active": threads["active"],
        "thread_rulings_rendered": len(threads["rendered"]),
        "thread_rulings_excluded": threads["excluded"],
        "thread_rulings_excluded_digests": threads["excluded_digests"],
        # The `missed` side of the same projection. Counted and NAMED here for
        # the same reason as the do-not-touch side: the prompt shows what fits
        # under the ceiling, the report says what did not.
        "wanted_more_active": threads["wanted_more_active"],
        "wanted_more_rendered": len(threads["wanted_more"]),
        "wanted_more_excluded": threads["wanted_more_excluded"],
        "wanted_more_excluded_digests": threads["wanted_more_digests"]}}


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
