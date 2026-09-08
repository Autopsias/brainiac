#!/usr/bin/env python3
"""The judgment layer — the 583 lines that matter (JDG-01, 2026-08-10).

WHAT THIS IS. The other half of `tools/cos_driver.py`. The driver produces a
night's BOOKS with every judgment slot `null`; this file holds the rules that
decide what goes in them, the prompt templates the nightly model answers from
driver-produced batches, the VALIDATOR that refuses anything outside the closed
vocabulary, and the morning brief the whole night is for.

THE CONTRACT, IN ONE PARAGRAPH. The model never touches a mailbox, a counter or
a ledger file. It receives a BATCH of typed fields (and, for opened threads, the
captured text) and returns STRUCTURED verdicts — one JSON object per
conversation, every field drawn from a closed set. `validate_verdict` scores each
returned row against the 47 rules extracted from the doctrine and REFUSES free
text: a word outside the set is a rejection, never a variant. Only validated rows
reach `apply_judgment`, which writes them into the driver's null slots and
recomputes the counters from the judged ledger.

EVIDENCE IS A POINTER, NOT A QUOTE. Every staged candidate carries
`evidence_span: {start, end}` into the run's own capture corpus — offsets, not
text. The corpus is MNPI and host-only; a span can be resolved by whoever is
allowed to read it and is inert to everyone else. A span that does not land
inside the captured text is not evidence, and rule `staging.evidence_required`
fails it.

WHERE THE VOCABULARY LIVES. `brain.cos_runverify` already holds the closed sets
the HOST scores runs against. They are imported from there rather than restated,
because two copies of a closed set is how run 106 and run 108 each invented a
word that left their rows out of every total.

TWO MODEL LEGS, AND THE ORDER IS THE POINT (GAP 9, 2026-08-13). Rule 1¾ drops a
`never`-category thread on the DRAW, before its body is opened — and the
category is a judgment, so it has to be asked BEFORE the bodies, not with them.
`--category-batch` is that first leg: typed fields only, one stamp per
conversation, rendered over the driver's `--enumerate-only` output and answered
back into `cos_driver.py --categories`. The four batches below are the second
leg, and they receive the category as an INPUT rather than re-deciding it.

    python3 tools/cos_judge.py --selfcheck
    python3 tools/cos_judge.py --golden [--answers <judgment-answers.json>]
    python3 tools/cos_judge.py --category-batch --vault <v> \
        --enumeration <enumeration.json> --out <batch-category.md>
    python3 tools/cos_judge.py --batches --vault <v> --run-id <id> --out <dir> \
        [--categories <categories.json>]
    python3 tools/cos_judge.py --judge --vault <v> --run-id <id> \
        --verdicts <verdicts.json> [--categories <categories.json>] \
        --evidence <out.json>
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_judge_cli                                        # noqa: E402
import cos_judge_runfacts                                   # noqa: E402
import cos_judge_verdicts                                   # noqa: E402
import cos_judge_brief                                        # noqa: E402
import cos_judge_grounding                                     # noqa: E402
import cos_voice                                               # noqa: E402
from cos_judge_apply import (  # noqa: E402,F401  batch-2 drain
    JUDGMENT_SLOTS, apply_judgment, archive_eligibility)
from cos_judge_batches import (  # noqa: E402,F401
    REDACT_PASSTHROUGH, _batch_json, batch_membership, batch_prompts,
    category_batch, grounding_required, redact_row)
from cos_judge_brief import (  # noqa: E402,F401
    CSP, GOLDEN, _merge, _score_judgment, _sect, compose_brief,
    evaluate_golden, render_png, selfcheck)
from cos_judge_grounding import (  # noqa: E402,F401
    _answer_mod, _used_block_vocab, grounding_facts, load_categories,
    mark_candidates, mechanical_disposition)
from cos_judge_night import (  # noqa: E402,F401
    _ledger, _now_iso, _short, load_night, load_selection)
from cos_judge_prompts import (  # noqa: E402,F401
    CATEGORY_PROMPT, DRAFT_PROMPT, HOLD_PROMPT, STAGING_PROMPT, TRIAGE_PROMPT,
    _VOCAB_BLOCK)
from cos_judge_rules import (  # noqa: E402,F401
    BRIEF_ORDER, BUCKETS, DRAFT_CAP, FIREWALL_CLOSE, FIREWALL_OPEN,
    HOLD_CATEGORIES, HOLD_SCREENS, HOLD_VERDICTS, JudgeStop, NOISE_SIGNALS,
    NOVELTY_WORDS, READ_NOISE_SIGNAL, RESOLUTIONS, RULES, SECRET_RE,
    STALE_ACT_SIGNAL,
    SUBSTANCE_KINDS, TIERS, TIER_ORDER, Rule, _age_days, _disposition_of, _draft, _footer_notes,
    _g, _r_always, _r_bucket, _r_draft_protected, _r_drafted, _r_evidence,
    _r_first_screen, _r_floor, _r_hold_cat, _r_hold_vocab, _r_never,
    _r_never_open, _r_p0, _r_p0p1, _r_p3, _r_resolved, _r_scope,
    _r_signal, _r_substance, _r_summary, _r_tier, _r_uncertain, _taxo,
    first_failed_screen, rule,)
from cos_judge_rules_2 import (  # noqa: E402,F401
    _headings, _r_cap, _r_category, _r_class, _r_csp, _r_decision,
    _r_dedup_drop, _r_dedup_vocab, _r_disp, _r_draft_idem, _r_draft_scope,
    _r_draft_unread, _r_firewall, _r_held_reason, _r_none, _r_novelty,
    _r_order, _r_outcome, _r_placeholder, _r_recipients, _r_remote,
    _r_secret, _r_send, _r_span, _r_staged, _r_stale, _r_stamps, _r_voice,
    check_one, validate_brief, validate_run, validate_verdict,)


def write_night(vault: Path, run_id: str, judged: dict[str, Any], *,
                out_dir: Path) -> dict[str, Any]:
    """Persist the judged night: ledger slots filled, metrics row superseded,
    brief written and RENDERED. Nothing here touches a mailbox."""
    from brain import cos                                        # noqa: PLC0415
    import cos_reconcile_metrics as recon                        # noqa: PLC0415

    ops = cos.run_ops_dir(vault)
    ledger = _ledger(vault, run_id)
    ledger.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                              for r in judged["applied"]["rows"]), encoding="utf-8")

    day = run_id[:10]
    n = run_id.rsplit("run", 1)[-1]
    html_path = cos_judge_brief.morning_brief_path(ops, run_id)
    png_path = html_path.with_suffix(".png")
    html_path.write_text(judged["brief_html"], encoding="utf-8")
    render = render_png(html_path, png_path,
                        height=cos_judge_brief.brief_png_height(
                            judged["brief_html"]))

    # The draft texts are the product of the drafting leg, and until s04 places
    # them in the mailbox they exist nowhere else. `_pending_` rather than
    # `_ledger_`: nothing was created in the mailbox, and a name that says
    # otherwise is how a run comes to believe it did.
    pending = ops / f"_cos_drafts_pending_{run_id}.jsonl"
    # EVERY DRAFT CARRIES WHAT IT WAS DRAFTED UNDER (VOICE-01). Without this a
    # draft written on a night with no voice profile is indistinguishable, in
    # the ledger, from one written against it — and the score
    # `tools/cos_voice_cli.py --fold` adds later would then be read as grounded
    # whatever it was measuring.
    voice = cos_voice.ledger_fields(
        (judged.get("night") or {}).get("voice_profile")
        or cos_voice.profile_state(None))
    pending.write_text("".join(
        json.dumps({"run": run_id, "conversation_id": d["conversation_id"],
                    "voice_profile": voice, "voice_check": None,
                    "recipient_scope": (d.get("draft") or {}).get(
                        "recipients_scope"),
                    "form": (d.get("draft") or {}).get("form"),
                    "voice": (d.get("draft") or {}).get("voice"),
                    "placeholders": (d.get("draft") or {}).get("placeholders"),
                    "text": (d.get("draft") or {}).get("text"),
                    "saved_to_mailbox": False},
                   ensure_ascii=False, sort_keys=True) + "\n"
        for d in judged["drafts"]), encoding="utf-8")

    prior = recon._rows(ops / "_cos_metrics.jsonl")
    mine = [r for r in prior if r.get("run_id") == run_id]
    row = dict(mine[-1]) if mine else {"run_id": run_id, "date": day, "run": n}
    row.update(judged["applied"]["counters"])
    row["run_ts"] = _now_iso()
    row["drafts_created"] = 0                # written, never saved to the mailbox
    row["judgment_pass"] = "cos_judge"
    if mine:
        row[recon.SUPERSEDES] = str(mine[-1].get("run_ts"))
    append = recon.append_metric(ops, row)
    # A NIGHT DOES NOT CLAIM A PNG IT DID NOT PRODUCE (review 2026-08-15).
    # `render` carried `exists` and `bytes` and NOTHING READ THEM, while
    # `brief_png` reported the path unconditionally — so a render SIGKILLed at
    # the bound on a loaded machine yielded a brief with no image and a night
    # that scored clean, with the only evidence sitting in a field no consumer
    # touched. The claim is now the check: the path is reported when the file is
    # really there and non-empty, and `brief_png_error` names the failure when it
    # is not. Fail-visible, not fail-closed — a brief without its PNG is still a
    # brief, and killing the night over an image would be the wrong trade.
    ok = render.get("exists") and render.get("bytes", 0) > 0
    out = {"ledger": str(ledger), "drafts_pending": str(pending),
           "brief_html": str(html_path),
           "brief_png": str(png_path) if ok else None, "render": render,
           "metrics_append": append}
    if not ok:
        out["brief_png_error"] = (
            f"no PNG at {png_path.name}: renderer returncode "
            f"{render.get('returncode')}, {render.get('bytes', 0)} byte(s)")
        print(f"WARNING: {out['brief_png_error']}", file=sys.stderr)
    return out


def _run_facts(*, taxo: dict[str, Any] | None, categories: dict[str, str] | None,
               rows: list[dict[str, Any]], undefined_stamps: Any,
               applied: dict[str, Any], accepted: dict[str, Any],
               duplicate_reemissions: Any, duplicate_conflicts: Any,
               drafts: list[dict[str, Any]], malformed_drafts: Any,
               model_answered: int, total: int, coverage: float,
               ctx_by_id: dict[str, Any], grounding: Path | None,
               chunks_dir: Path | None, by_id: dict[str, Any],
               voice_profile: dict[str, Any] | None = None,
               read_never_categories: bool = False
               ) -> dict[str, Any]:
    """The run-level facts block `judge_night` hands to `compose_brief` and
    `validate_run` — pulled out verbatim so the caller reads as one pass."""
    import cos_driver                                            # noqa: PLC0415

    return {
        "category_gate": cos_judge_runfacts.category_gate_block(
            cos_driver.category_gate_state, categories, rows, taxo,
            undefined_stamps),
        "drafts": len(drafts),
        # H5: a non-mapping draft is dropped (not crashed, not rejected) but made
        # visible beside the drafts count.
        "malformed_drafts": malformed_drafts,
        # THE THIRD OUTCOME, COUNTED PER WORD (owner ruling 2026-08-28). A run
        # whose act bucket is large and whose drafts and `needs_owner` are both
        # near zero is the silence the ruling forbids, and this is the number
        # that makes it visible without opening the sheet.
        "needs_owner": dict(collections.Counter(
            v["needs_owner"] for v in accepted.values()
            if v.get("needs_owner"))),
        "act_first": all(v.get("bucket") == "act" for v in accepted.values()
                         if _draft(v)),
        "never_category_opens": cos_judge_runfacts.never_category_opens(
            taxo, applied["rows"]),
        # WHY THOSE OPENS ARE OR ARE NOT A FAULT. The count above is the same
        # number it always was; this is the owner's lever beside it, so the run
        # rule judges the opens against what the owner asked for on THIS night
        # rather than against a standing prohibition he has since scoped.
        "read_never_categories": read_never_categories,
        # H3: benign re-emissions vs conflicting duplicates dropped to PENDING.
        "duplicate_reemissions": duplicate_reemissions,
        "duplicate_conflicts": duplicate_conflicts,
        # H4: always-logged model coverage of the enumerated set.
        "model_coverage": {"answered": model_answered, "enumerated": total,
                           "fraction": coverage},
        # GRD-03: what each leg was actually grounded with, and what the closed
        # schema refused on the way in. E10 derives from these counts.
        "grounding": grounding_facts(rows, ctx_by_id, grounding, chunks_dir,
                                     by_id),
        # VOICE-01: whether the draft leg had the owner's profile in front of
        # it, WITHOUT the profile's own text — the digest joins it and the
        # writing stays on the host. `degradation` is non-null exactly when the
        # drafts this run produced were written ungrounded.
        "voice_profile": cos_voice.ledger_fields(
            voice_profile or cos_voice.profile_state(None)),
    }


def judge_night(vault: Path, run_id: str, verdicts: list[dict[str, Any]], *,
                out_dir: Path, contract: str = "PASS",
                categories: dict[str, str] | None = None,
                selection: set[str] | None = None,
                grounding: Path | None = None,
                chunks_dir: Path | None = None) -> dict[str, Any]:
    """Validate, apply, and render; rejected verdicts are never coerced.

    Owner-facing footer notes derive here from accepted verdicts (H2).
    """
    # Preserve the long-standing three-argument seam for tests and callers
    # that do not opt into chaining. The fourth argument exists only when the
    # host supplied an actual bounded selection.
    night = (load_night(vault, run_id, categories)
             if selection is None
             else load_night(vault, run_id, categories, selection))
    rows, ctx_by_id = night["rows"], night["ctx_by_id"]
    # THE VERDICT ADJUDICATION lives in `cos_judge_verdicts` (the s17
    # extraction): the H3 duplicate grouping (canonical-JSON comparison,
    # benign re-emissions vs conflicts) then the per-row accept-or-refuse
    # pass (mechanical bookkeeping, the pre-draw category stamp, the
    # closed-vocabulary validator). The validator, the screen order and the
    # mechanical decider are handed in as callables so a test that patches
    # them on THIS module is still honoured.
    by_id, conflicted_cids, duplicate_conflicts, duplicate_reemissions = \
        cos_judge_verdicts.group_verdicts(verdicts)
    taxonomy_ids = set(night["taxonomy"] or {})
    accepted, rejected, refused_cids, undefined_stamps = \
        cos_judge_verdicts.accept_verdicts(
            rows, by_id, conflicted_cids, ctx_by_id, categories, taxonomy_ids,
            mechanical=mechanical_disposition,
            first_screen=first_failed_screen,
            validate=validate_verdict, short_id=_short)
    total = len(rows)
    rejection_rate = round(len(rejected) / total, 4) if total else 0.0
    mark_candidates(accepted, ctx_by_id)

    # A ROW THE HOST DISCARDED IS `judgment-refused`, NOT `unjudged`. The
    # projection runs in another process and only its COUNTS used to cross
    # back, so every discarded row reached the ledger claiming the model had
    # not answered for it (run170, 2026-08-23).
    refused_cids |= cos_judge_grounding.projection_refused_ids(chunks_dir)
    applied = apply_judgment(rows, accepted, refused=refused_cids)
    # THE BRIEF'S PRODUCTS (holds, staged spans, triage, drafts, the
    # malformed-draft count, the owner footer) live in
    # `cos_judge_runfacts.collect_products`; `_draft` and `_footer_notes` are
    # handed in as callables for the same patch-honouring reason.
    holds, staged, spans, triage, drafts, malformed_drafts, footer_notes = \
        cos_judge_runfacts.collect_products(
            applied, accepted, ctx_by_id, draft_of=_draft,
            footer_notes_of=_footer_notes)
    # H4: always-logged model coverage of the enumerated set (the R2 real-model-
    # content numerator) — `cos_judge_runfacts.model_coverage`.
    model_answered, coverage = cos_judge_runfacts.model_coverage(
        rows, by_id, total)

    run_facts = _run_facts(
        taxo=night["taxonomy"], categories=categories, rows=rows,
        undefined_stamps=undefined_stamps, applied=applied, accepted=accepted,
        duplicate_reemissions=duplicate_reemissions,
        duplicate_conflicts=duplicate_conflicts, drafts=drafts,
        malformed_drafts=malformed_drafts, model_answered=model_answered,
        total=total, coverage=coverage, ctx_by_id=ctx_by_id,
        grounding=grounding, chunks_dir=chunks_dir, by_id=by_id,
        voice_profile=night.get("voice_profile"),
        read_never_categories=bool(night.get("read_never_categories")))
    html = compose_brief(
        run_id=run_id, contract=contract, counters=applied["counters"],
        triage=triage, staged=staged, drafts=drafts, holds=holds,
        # THE SHEET'S ATTACHMENT LINE IS NOT THIS LEG'S TO WRITE (review
        # 2026-08-25). Its two producers both run LATER in
        # `tools/cos_nightly.sh` — the ingest bridge writes the manifest and
        # the attachment fetch writes the report, in that order, well after
        # this call — so reading them here reads an empty directory and prints
        # a zero that looks like a measurement. `compose_brief` therefore emits
        # the anchored PENDING line, and `cos_driver_night_records
        # .stamp_attachment_brief` (called by the fetch leg that moves the
        # bytes) fills in the counts, exactly as `stamp_attachment_lane`
        # supersedes the metrics row.
        metrics={"inbox_count": total,
                 "body_open_actual": sum(1 for r in rows if r.get("body_opened"))},
        notes=[f"{applied['judgment_pending']} row(s) carry no verdict: this run "
               "persisted no sender or subject for them, and Phase 1.5 judges "
               "from typed fields only."] if applied["judgment_pending"] else [],
        spans=spans, footer_notes=footer_notes,
        # (STALE-01) THE HOST'S OWN DECISION, read back off the ledger rows
        # `apply_judgment` just stamped — not the model's `stale` claim, which
        # `archive_eligibility` may decline (unread, P0/P1, unjudged). This is
        # what makes the ruling's second half true: the sheet NAMES the threads
        # the porter took, instead of listing them as needing the owner.
        stale_archived={r["conversation_id"] for r in applied["rows"]
                        if r.get("noise_signal") == STALE_ACT_SIGNAL})
    out_dir.mkdir(parents=True, exist_ok=True)
    return {"night": night, "accepted": accepted, "rejected": rejected,
            "rejection_rate": rejection_rate, "applied": applied, "holds": holds,
            "staged": staged, "drafts": drafts, "brief_html": html,
            "coverage": coverage,
            "brief_violations": validate_brief(html, {"staged": len(staged)}),
            "run_violations": validate_run(run_facts),
            "run_facts": run_facts}



def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--selfcheck", action="store_true")
    p.add_argument("--golden", action="store_true")
    p.add_argument("--answers", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--batches", action="store_true",
                   help="render the judgment batch prompts for a run")
    p.add_argument("--judge", action="store_true",
                   help="validate a verdicts file, apply it, render the brief")
    p.add_argument("--redact", action="store_true",
                   help="with --batches: replace every sender, subject and body "
                        "with its length. Required for any copy written where "
                        "git can reach it — this repository is a public-export "
                        "source and a batch is real mail")
    p.add_argument("--verdicts", type=Path, default=None)
    p.add_argument("--category-batch", action="store_true",
                   help="render the PRE-DRAW category batch over a driver "
                        "`--enumerate-only` file. Its answer feeds "
                        "`cos_driver.py --categories`, which is what arms rule "
                        "1¾'s exclusion BEFORE any body is opened")
    p.add_argument("--enumeration", type=Path, default=None,
                   help="with --category-batch: the driver's enumerate-only json")
    p.add_argument("--categories", type=Path, default=None,
                   help="with --batches/--judge: the category batch's answer. "
                        "The SAME file the driver drew against — the category is "
                        "decided once, before the draw, and every later leg "
                        "reads it rather than re-deciding it")
    p.add_argument("--selection", type=Path, default=None,
                   help="with --batches/--judge: host-selected enumeration "
                        "that bounds both model legs to the per-batch cap")
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--grounding", type=Path, default=None,
                   help="with --judge: the run's grounding.json, for the "
                        "per-leg grounded/ungrounded run facts E10 derives from")
    p.add_argument("--chunks-dir", type=Path, default=None,
                   help="with --judge: the chunk dir, for the closed-schema "
                        "projection's counts")
    p.add_argument("--reject-abort", type=float, default=0.05,
                   help="STOP if this fraction of verdicts is refused by the "
                        "closed vocabulary: past it the prompt templates and the "
                        "validator disagree, and continuing produces a night of "
                        "coerced values")
    args = p.parse_args(argv[1:])
    if args.selfcheck:
        return selfcheck()
    if args.judge:
        return cos_judge_cli.run_judge(
            args, load_categories=load_categories,
            load_selection=load_selection, judge_night=judge_night,
            write_night=write_night, short_id=_short)
    if args.category_batch:
        return cos_judge_cli.run_category_batch(
            args, category_batch=category_batch)
    if args.batches:
        return cos_judge_cli.run_batches(
            args, load_night=load_night, load_categories=load_categories,
            load_selection=load_selection,
            batch_prompts=batch_prompts)
    if args.golden:
        return cos_judge_cli.run_golden(args, evaluate_golden=evaluate_golden)
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
