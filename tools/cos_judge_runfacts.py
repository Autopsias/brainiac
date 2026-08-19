"""Run-fact assembly sub-steps of `cos_judge.judge_night`'s post-judgment pass.

Everything the night reports ABOUT its own judged rows, gathered in one place:
the brief's product lists (holds, staged candidates with their span quotes,
triage verdicts, reply drafts, the malformed-draft count), the H4 model-coverage
numerator, the never-category open count, and the category-gate block. Import
direction is one-way: this module imports nothing from its parent — the draft
guard and the footer-note derivation arrive as callables so a test that patches
them on the parent is still honoured.
"""
from __future__ import annotations

from typing import Any, Callable


def collect_products(applied: dict[str, Any],
                     accepted: dict[str, dict[str, Any]],
                     ctx_by_id: dict[str, dict[str, Any]], *,
                     draft_of: Callable[[dict[str, Any]], dict[str, Any] | None],
                     footer_notes_of: Callable[[list[dict[str, Any]]],
                                               tuple[str, ...]]
                     ) -> tuple[dict[str, int], list[dict[str, Any]],
                                dict[str, str], list[dict[str, Any]],
                                list[dict[str, Any]], int, tuple[str, ...]]:
    """``(holds, staged, spans, triage, drafts, malformed_drafts, footer_notes)``
    — everything `compose_brief` and the report render out of the accepted set.

    `_draft`, not raw `v.get("draft")` truthiness: a STRING draft (run 131) is
    truthy but non-mapping, and admitting it here carried it to the
    `(d.get("draft") or {}).get(...)` consumers in `write_night` and the report
    footer, which crashed on the string. The validator already treats a
    non-mapping draft as no draft, so this is the same decision one step later.
    """
    holds: dict[str, int] = {}
    for r in applied["rows"]:
        if r.get("held_reason"):
            holds[r["held_reason"]] = holds.get(r["held_reason"], 0) + 1

    staged = [accepted[c] for c in accepted
              if accepted[c].get("disposition") == "candidate"]
    spans: dict[str, str] = {}
    for c in staged:
        span = c.get("evidence_span") or {}
        text = ctx_by_id[c["conversation_id"]]["text"]
        spans[c["conversation_id"]] = " ".join(
            text[int(span.get("start", 0)):int(span.get("end", 0))].split())[:220]
    triage = [v for v in accepted.values() if v.get("bucket")]
    drafts = [dict(v, subject=ctx_by_id[v["conversation_id"]]["subject"],
                   recipient=ctx_by_id[v["conversation_id"]]["sender"])
              for v in accepted.values() if draft_of(v)]
    # H5 (both reviewers, MEDIUM). A non-mapping `draft` (a string/list — run 131
    # emitted a string) routes through `_draft` → None → silently dropped above.
    # `_draft` STAYS the defensive guard (a bad draft never crashes and never
    # discards a good triage verdict), but the dropped draft is now COUNTED so it
    # is visible in the run facts rather than vanishing.
    malformed_drafts = sum(1 for v in accepted.values()
                           if v.get("draft") is not None and draft_of(v) is None)

    # H2 (Codex MEDIUM, injection into the owner briefing). The footer is derived
    # from the ACCEPTED, validated verdicts ONLY — inside the judgment pass —
    # never from raw parser output in main(). An unenumerated or rejected object
    # carrying `draft.voice: "neutral: …"` reached `accepted.values()` for
    # neither reason, so it can no longer reach the owner-facing footer.
    footer_notes = footer_notes_of(list(accepted.values()))
    return holds, staged, spans, triage, drafts, malformed_drafts, footer_notes


def model_coverage(rows: list[dict[str, Any]],
                   by_id: dict[str, dict[str, Any]],
                   total: int) -> tuple[int, float]:
    """``(model_answered, coverage)`` — H4's always-logged numerator.

    H4 (Codex HIGH, silent partial parse). `extract_objects` succeeds if ANY
    object parsed, so a mostly-malformed answer yields a small verdicts.json
    that passes the `-s` gate; rows with NO verdict are silently PENDING and
    never counted. Coverage = the fraction of ENUMERATED conversations the
    MODEL actually answered (present in by_id — a mechanical-disposition-only
    row is NOT a model answer). Always logged; the CLI aborts read-only below a
    conservative floor. `total` is len(rows), the distinct enumerated cids.
    R2 (Codex re-review HIGH): count only objects that carry REAL model
    content, not bare `{"conversation_id": …}`. A truncated stream can leave
    id-only fragments, and a mechanical disposition can then complete them, so
    counting by_id PRESENCE let a mostly-mechanical night certify full model
    coverage. An answer the model actually made has at least one key beyond the
    id it was asked to key on.
    """
    def _model_answered(o: Any) -> bool:
        # A real model answer carries the triage `bucket` it was REQUIRED to
        # produce for every substantive thread — not merely a second key (R2
        # round 3, Codex: an object with `conversation_id` + any irrelevant key
        # gamed coverage, and mechanical disposition then supplied the verdict).
        return isinstance(o, dict) and bool(o.get("bucket"))
    model_answered = sum(1 for r in rows
                         if _model_answered(by_id.get(r["conversation_id"])))
    coverage = round(model_answered / total, 4) if total else 1.0
    return model_answered, coverage


def never_category_opens(taxo: dict[str, Any],
                         applied_rows: list[dict[str, Any]]) -> int:
    """How many `never`-category rows this run opened the bodies of."""
    never_ids = {k for k, r in taxo.items()
                 if str((r or {}).get("disposition") or "").lower() == "never"}
    return sum(1 for r in applied_rows
               if r.get("category") in never_ids and r.get("body_opened"))


def category_gate_block(category_gate_state: Callable[..., dict[str, Any]],
                        categories: dict[str, str] | None,
                        rows: list[dict[str, Any]],
                        taxo: dict[str, Any],
                        undefined_stamps: dict[str, int]) -> dict[str, Any]:
    """The run-facts `category_gate` block, from the driver's own predicate.

    THE SAME PREDICATE THE DRIVER CALLS, not a second spelling of it.
    `armed if categories is not None` (driver) and `armed if categories`
    (here) disagreed on an empty answer, so one run reported both.
    """
    return dict(
        category_gate_state(
            categories, (r["conversation_id"] for r in rows), taxo),
        stamps_undefined_and_dropped=undefined_stamps,
        rows_excluded_before_draw=sum(
            1 for r in rows if r.get("category_gate_excluded")))
