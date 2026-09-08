"""Per-row shaping for the four judgment batch prompts `cos_judge.batch_prompts` renders.

`batch_prompts` owns the prompt templates' formatting (the templates themselves
stay in :mod:`cos_judge` — `cos_verify_doctrine.py` reads them out of that
file's text); this module owns what each batch's ROW carries, one shaper per
leg, built from driver facts and the per-row context exactly as before. Import
direction is one-way: this module imports nothing from its parent — the
resolution vocabulary arrives as a parameter because it is the parent's
rule-table constant.
"""
from __future__ import annotations

from typing import Any

#: (JUDGE-03, 2026-08-27) THE HOST FACTS THE TRIAGE RULES GRADE, in the order
#: the prompt names them. `cos_signals` / `cos_signals_stale` compute every one
#: from the run's own ledger and captured text, `cos_judge_night._row_ctx`
#: merges them into the per-row context, and `triage.p3_act_needs_direct_ask`,
#: `triage.noise_signal_required` (via the aged-read and stale-act lane
#: refusals) and `triage.stale_evidence` score the model's answer against them.
#: Until this constant they reached the VALIDATOR and not the PROMPT: measured
#: on run 2026-08-26-run189, zero of these names appear in
#: `batches/batch-triage.md` and the row carried eight fields, so the model was
#: graded on booleans it was never shown. 10 of 120 verdicts refused (8.3%)
#: against a 5% abort threshold, and the first attended night judged no rows.
#:
#: ONE TUPLE, TWO USES — the shaper below and `REDACT_PASSTHROUGH` — because a
#: signal carried into the row and forgotten by the masker is a signal the
#: evidence artifact cannot show.
HOST_SIGNALS = ("unanswered_direct_ask", "live_deadline",
                "stale_deadline_passed", "open_spine_commitment",
                "body_unreadable", "screens_ran_unresolved",
                "thread_carries_draft")

#: The `..._leg` companion of each content fact that has one. A POSITIVE names
#: the side it was found on; a NEGATIVE is `None`, which is why
#: :func:`host_signal_scan` exists beside it.
HOST_SIGNAL_LEGS = ("unanswered_direct_ask_leg", "live_deadline_leg",
                    "stale_deadline_passed_leg")


def host_signal_scan(c: dict[str, Any]) -> str:
    """What the host's content detectors could actually READ on this row.

    A CONSERVATIVE NEGATIVE MUST SAY SO. `cos_signals` is a closed marker list
    over the newest message's own words AND the subject line, and on a row whose
    body never opened — 100% of the rows `triage.p3_act_needs_direct_ask`
    actually refuses, per that module's own docstring — only the subject was
    ever scanned. `unanswered_direct_ask: false` then means "the host did not
    look at the body", not "the host looked and found nothing", and a model
    shown the bare boolean cannot tell the two apart. The `body_unreadable`
    branch is the same silence for a different reason: the body opened and
    `readable_newest_text` handed every detector an EMPTY string.
    """
    if not c.get("body_opened"):
        return "subject-only (the body was never opened tonight)"
    if c.get("body_unreadable"):
        return "subject-only (the body opened UNREADABLE)"
    return "subject+body"


def _host_facts(c: dict[str, Any], *names: str) -> dict[str, Any]:
    """The named host facts, their legs, and the scan that produced them.

    GIVEN, NEVER ASKED FOR BACK — the same rule `staging_rows` states for
    `category`. The model may not certify its own input, so these travel one
    way: into the batch, never back out of the answer.
    """
    out: dict[str, Any] = {n: bool(c.get(n)) for n in names}
    out.update({leg: c.get(leg) for leg in HOST_SIGNAL_LEGS
                if leg[:-len("_leg")] in names})
    out["host_signal_scan"] = host_signal_scan(c)
    return out


def triage_rows(rows: list[dict[str, Any]],
                ctx_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One shaped row per thread the TRIAGE leg judges."""
    out: list[dict[str, Any]] = []
    for r in rows:
        c = ctx_by_id.get(r["conversation_id"], {})
        out.append(
            {"conversation_id": r["conversation_id"],
             "sender": c.get("sender"),
             "subject": c.get("subject"),
             "received": r.get("received"), "read_state": r.get("read_state"),
             "chip": r.get("tier"),
             "priority_map_tier": (c.get("priority_map") or {}).get(
                 c.get("sender")),
             "rows_from_sender_tonight": c.get("sender_rows_this_run"),
             **_host_facts(c, *HOST_SIGNALS)})
    return out


def staging_rows(rows: list[dict[str, Any]],
                 ctx_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One shaped row per thread whose body this run captured.

    The STAGING batch carries the TEXT. Rule 2 judges substance out of a
    message body and quotes a span of it; a batch that names the text and
    ships none asks for a verdict the model cannot reach.

    NO HOST SIGNAL IS CARRIED HERE, AND THAT IS THE CHECKED ANSWER, NOT AN
    OVERSIGHT (JUDGE-03). Every `staging.*` rule was read: they score
    `text_len`, `overlay_keyword_tier`, `taxonomy`/`category` and `brain_near_dup`
    — all of which this row already carries or the prompt already renders — and
    not one reads any of `HOST_SIGNALS`. `staging.scope` grades the MERGED
    verdict's `bucket`/`tier`, which are the triage leg's answer, not a host
    fact this leg could be shown. Widening a batch with a fact no rule reads
    buys prompt bytes and no verdict.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        c = ctx_by_id.get(r["conversation_id"], {})
        out.append(
            {"conversation_id": r["conversation_id"], "tier": r.get("tier"),
             # GIVEN, not asked. The pre-draw category batch decided this
             # and the draw was made on it; the model re-deciding it here
             # is how an OPENED row comes to carry a `never` stamp, which
             # is the exact `body_pass` failure of runs 126/129/130.
             "category": c.get("category"),
             "sender": c.get("sender"),
             "subject": c.get("subject"),
             "body_chars": r.get("body_chars"),
             "overlay_keyword_tier": c.get("overlay_keyword_tier"),
             "text": c.get("text")})
    return out


def hold_rows(rows: list[dict[str, Any]],
              ctx_by_id: dict[str, dict[str, Any]],
              resolutions: dict[str, str]) -> list[dict[str, Any]]:
    """One shaped row per chipped thread the HOLD leg re-evaluates.

    ONE host signal, and only one, because only one hold rule reads one
    (JUDGE-03): `hold.p0p1_archive_explicit_resolution` refuses RESOLVED on
    any row where `unanswered_direct_ask` is True, and the prompt already
    promises the model that "a genuinely unanswered direct ask is NEVER
    resolved" without ever showing it the boolean that decides.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        c = ctx_by_id.get(r["conversation_id"], {})
        out.append(
            {"conversation_id": r["conversation_id"], "tier": r.get("tier"),
             "received": r.get("received"), "read_state": r.get("read_state"),
             "subject": c.get("subject"),
             "chip": r.get("tier"),
             **_host_facts(c, "unanswered_direct_ask"),
             # THE FLAGS, SHOWN. `RESOLVED` is admissible only on a flag this
             # run actually observed, and a driver that observes none makes
             # it unreachable — so a batch that hides them asks for a verdict
             # that can only be refused. Measured on run 120: 16 of 45 hold
             # rows refused for claiming a resolution nothing recorded.
             "resolution_flags_observed": {
                 name: bool(c.get(flag))
                 for name, flag in sorted(resolutions.items())}})
    return out


def draft_rows(rows: list[dict[str, Any]],
               ctx_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One shaped row per thread a reply COULD be written from.

    The DRAFT batch carries the TEXT for the same reason staging does: a
    reply is written FROM the message, and a batch that names the thread
    and ships none asks for prose the model would have to invent.

    AND THE TWO HOST FACTS ITS SCOPE RULE IS SCORED ON (JUDGE-03).
    `draft.response_warranted_scope` accepts a non-`act` row only when its
    `hold_category` is `Held · ask` or `Held · deadline` — and that category is
    not the model's to send: `cos_judge_rules.first_failed_screen` COMPUTES it,
    from `unanswered_direct_ask` and `live_deadline`. So "response-warranted",
    the rule the prompt says refuses most drafts, is decided by two host
    booleans the draft row has never carried; that dead second clause is one of
    the three defects `cos_signals`' own header records. `ask_age_days` joins
    them because `draft.stale_ask_form` keys the acknowledge-late form on the
    host's number, not on the model's own arithmetic over `received`.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        c = ctx_by_id.get(r["conversation_id"], {})
        out.append(
            {"conversation_id": r["conversation_id"], "tier": r.get("tier"),
             "sender": c.get("sender"),
             "subject": c.get("subject"),
             "received": r.get("received"), "read_state": r.get("read_state"),
             "ask_age_days": c.get("ask_age_days"),
             **_host_facts(c, "unanswered_direct_ask", "live_deadline"),
             "text": c.get("text")})
    return out


# ---------------------------------------------------------------------------
# batch-2 drain: the batch renderers and `batch_prompts` moved verbatim out
# of `cos_judge` and are re-imported by it; the prompt texts live in
# `cos_judge_prompts` beside this module.
# ---------------------------------------------------------------------------
import json                                                   # noqa: E402
import sys                                                    # noqa: E402
from pathlib import Path                                      # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_judge_night import _short            # noqa: E402
from cos_judge_rules import (  # noqa: E402
    DRAFT_CAP, HOLD_SCREENS, RESOLUTIONS)

# batch-2 drain: `batch_prompts` moved in beside the four row shapers above,
# and its own local lists share their names — bind the shapers under distinct
# aliases so the locals cannot shadow them (the call sites used to reach the
# shapers through the module qualifier from `cos_judge.py`).
_triage_shaper = triage_rows
_staging_shaper = staging_rows
_hold_shaper = hold_rows
_draft_shaper = draft_rows
from cos_judge_prompts import (  # noqa: E402
    CATEGORY_PROMPT, DRAFT_PROMPT, HOLD_PROMPT, STAGING_PROMPT,
    TRIAGE_PROMPT, _VOCAB_BLOCK)
import cos_voice                                              # noqa: E402


REDACT_PASSTHROUGH = frozenset({
    "received", "read_state", "chip", "tier", "priority_map_tier",
    # THE HOST SIGNALS PASS, AND MASKING THEM WOULD BE THEATRE (JUDGE-03).
    # Every one is a boolean, a closed-set leg word (`body`/`subject`/null), a
    # closed-set scan phrase, or an integer age — no sender, subject or body
    # text can travel in any of them. And `redact_row` renders a value as its
    # LENGTH, so a masked boolean comes out `<redacted:4 chars>` for True and
    # `<redacted:5 chars>` for False: the mask publishes the value it hides.
    # Passing them through instead makes a redacted batch REVIEWABLE — which
    # is the only way an evidence artifact can prove the facts reached the
    # prompt at all, the exact measurement run 189 had to make by hand.
    *HOST_SIGNALS, *HOST_SIGNAL_LEGS, "host_signal_scan", "ask_age_days"})


def redact_row(row: dict[str, Any]) -> dict[str, Any]:
    """One batch row, masked by default. An unknown key comes out
    `<redacted:N chars>`; that is the property the inversion is for, and the
    test that proves it adds a key nobody enumerated."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "conversation_id":
            out[key] = _short(value)
        elif key in REDACT_PASSTHROUGH or value in (None, ""):
            out[key] = value
        else:
            out[key] = f"<redacted:{len(str(value))} chars>"
    return out


def _batch_json(rows: list[dict[str, Any]], *, redact: bool) -> str:
    return json.dumps([redact_row(r) for r in rows] if redact else rows,
                      indent=1, ensure_ascii=False)


def category_batch(rows: list[dict[str, Any]], taxonomy: dict[str, Any], *,
                   redact: bool = False) -> str:
    """The PRE-DRAW category batch, rendered over the driver's enumeration.

    `rows` are `cos_driver.enumerate_only`'s typed fields — no body exists yet,
    which is the whole point: rule 1¾ excludes a `never` thread on the DRAW, and
    the category is a judgment, so the judgment has to happen between the
    enumeration and the draw. Same redaction contract as the other four.
    """
    taxo = "\n".join(f"  {k}: {(v or {}).get('disposition')}"
                     for k, v in sorted(taxonomy.items()))
    return CATEGORY_PROMPT.format(
        n=len(rows), taxonomy=taxo,
        batch=_batch_json([
            {"conversation_id": r["conversation_id"],
             "sender": r.get("sender"),
             "subject": r.get("subject"),
             "received": r.get("received"),
             "read_state": r.get("read_state"),
             "chip": r.get("chip")}
            for r in rows], redact=redact))


def batch_membership(rows: list[dict[str, Any]],
                     ctx_by_id: dict[str, dict[str, Any]]
                     ) -> dict[str, list[str]]:
    """conversation ids per judgment batch: triage, staging, hold, draft.

    Computed from PRE-JUDGMENT facts only — `typed_fields_available`,
    `body_opened`, `tier`, `read_state`, `DRAFT_CAP` — which is why
    `cos_ground.py` can call it BEFORE the batches are rendered.
    `batch_prompts` calls THIS; it does not keep a second copy. Three copies is
    how a denominator drifts (DOCTRINE §8.2 E9).

    Never `cos_echecks.in_scope()`, which reads `verdict` and `judged_tier` —
    both POST-judgment. A pre-judgment set can never promise anything about
    post-judgment tiers (D13).
    """
    def ctx(cid: str) -> dict[str, Any]:
        return ctx_by_id.get(cid, {})

    triage = [r["conversation_id"] for r in rows
              if ctx(r["conversation_id"]).get("typed_fields_available", False)]
    staging = [r["conversation_id"] for r in rows if r.get("body_opened")]
    hold = [r["conversation_id"] for r in rows if r.get("tier")]
    staged = set(staging)
    chips = ("P0", "P1", "P2", "P3")
    draft = [r["conversation_id"] for r in sorted(
        (r for r in rows
         if r["conversation_id"] in staged
         and ctx(r["conversation_id"]).get("read_state") == "read"),
        key=lambda r: chips.index(r["tier"]) if r.get("tier") in chips
        else len(chips))][:DRAFT_CAP]
    return {"triage": triage, "staging": staging, "hold": hold, "draft": draft}


def grounding_required(rows: list[dict[str, Any]],
                       ctx_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """The UNSUBTRACTED union of the four judgment batches (D13).

    The guarantee, exactly: every row that appears in any of the four judgment
    batches is grounded, because the required set IS that union. There is no row
    the model sees that grounding did not cover. It makes no claim about
    post-judgment tier and needs none.

    The `never`-category subtraction of the design's revision 2 is deliberately
    absent: `batch_prompts` never consults `category_gate_excluded`, so such a
    row IS rendered into triage and hold, and subtracting it would leave a row
    the model sees ungrounded.
    """
    m = batch_membership(rows, ctx_by_id)
    seen: dict[str, None] = {}
    for name in ("triage", "staging", "hold", "draft"):
        for cid in m[name]:
            seen.setdefault(cid, None)
    return list(seen)


def batch_prompts(rows: list[dict[str, Any]], ctx_by_id: dict[str, dict[str, Any]],
                  taxonomy: dict[str, Any], *, redact: bool = False,
                  voice_profile: dict[str, Any] | None = None,
                  rulings: dict[str, Any] | None = None) -> dict[str, str]:
    """The four prompts, rendered over the driver's own JSON.

    `redact` exists because these files are USEFUL AS EVIDENCE and DANGEROUS AS
    ARTIFACTS: a real batch carries senders, subjects and message bodies out of
    a live mailbox, and this repository is a public-export source. Redacted, the
    prompt and its shape are intact and every payload value is a length. The
    nightly never redacts; anything written where git can reach it always does.

    `voice_profile` is `cos_voice.profile_state(vault)` — the OWNER's profile,
    read VAULT-relative, and the draft batch is the only one that gets it. It
    is optional here and its DEFAULT IS NOT SILENT: `None` renders the named
    degradation `voice profile absent, drafts ungrounded` into the prompt,
    exactly as an unreadable profile does, so no caller can quietly drop it and
    have the drafts still read as grounded.

    `rulings` is `cos_judge_grounding.owner_rulings(vault)` — the owner's own
    standing corrections, under the per-kind render caps. Its default is not
    silent either, and for the same reason: `None` renders the named
    degradation `owner-feedback record not read this run` into both prompts
    that carry it, so a caller cannot quietly drop the owner's rulings and have
    the night read as one on which he had never corrected anything.
    """
    # THE MASKER IS AN ALLOWLIST (D10). Rows are built with their REAL values and
    # every one of them is masked on the way out unless `REDACT_PASSTHROUGH` says
    # otherwise — so a key added to a row a year from now is redacted by default
    # rather than by whoever remembers. `conversation_id` passes as its digest.
    # ONE SELECTION FUNCTION, and this is the only caller that renders from it
    # (D13). `cos_ground.py` calls the same `batch_membership` to compute which
    # threads must be grounded, so the fetcher and the batches cannot disagree
    # about the population — the drift test asserts exactly that.
    # AT CALL TIME: `cos_judge_grounding` imports this module for
    # `batch_membership`, so a module-level import here is a cycle.
    from cos_judge_grounding import rulings_block                # noqa: PLC0415

    _membership = batch_membership(rows, ctx_by_id)
    _by_id = {r["conversation_id"]: r for r in rows}
    triage_rows = [_by_id[c] for c in _membership["triage"]]
    staging_rows = [_by_id[c] for c in _membership["staging"]]
    hold_rows = [_by_id[c] for c in _membership["hold"]]
    # The DRAFT batch was hard-coded to `[]` until 2026-08-12. Runs 118, 121,
    # 122 and 123 passed every draft guard and produced no candidate — run 123
    # with 22 rows judged `act` — because nobody had ever asked the model to
    # draft. It is drawn from DRIVER FACTS ONLY, like the other three: all four
    # prompts render BEFORE judgment, so `disposition == "act"` does not exist
    # yet. The batch is the rows a reply COULD be written from, and the model
    # omits the ones that warrant none — that is what "response-warranted rows,
    # ACT first" asks of it, not a gap. Two facts narrow it to rows the draft
    # rules can be satisfied against: `body_opened` (no text, no reply — same
    # population as staging) and a READ row (`draft.never_unread_row` makes a
    # draft off an UNREAD row an automatic FAIL, so batching one asks for a
    # rejection). P0 first, because run 101 spent a capped selection drawn in
    # ledger order on 20 P3 bodies; the chip tier is the only ACT proxy a
    # pre-judgment batch has.
    # NOT filtered on the drafts inventory: that is a live Drafts enumeration
    # this offline pass never performs (`load_night` hard-codes it empty), so
    # a re-draft is refused downstream by `draft.idempotent_vs_drafts` instead.
    draft_rows = [_by_id[c] for c in _membership["draft"]]
    taxo = "\n".join(f"  {k}: {(v or {}).get('disposition')}"
                     for k, v in sorted(taxonomy.items()))
    # WHAT EACH ROW CARRIES lives in `cos_judge_batches`, one shaper per leg
    # (the staging/draft text-carrying and the hold flags-shown reasoning moved
    # with their shapers); the templates above stay HERE because
    # `cos_verify_doctrine.py` reads them out of this file's own text.
    return {
        "triage": TRIAGE_PROMPT.format(
            n=len(triage_rows), vocab=_VOCAB_BLOCK,
            rulings=rulings_block(rulings, redact=redact),
            batch=_batch_json(_triage_shaper(
                triage_rows, ctx_by_id), redact=redact)),
        "staging": STAGING_PROMPT.format(
            n=len(staging_rows), vocab=_VOCAB_BLOCK, taxonomy=taxo,
            batch=_batch_json(_staging_shaper(
                staging_rows, ctx_by_id), redact=redact)),
        "hold": HOLD_PROMPT.format(
            n=len(hold_rows), vocab=_VOCAB_BLOCK, screens=" → ".join(HOLD_SCREENS),
            batch=_batch_json(_hold_shaper(
                hold_rows, ctx_by_id, RESOLUTIONS), redact=redact)),
        "draft": DRAFT_PROMPT.format(
            n=len(draft_rows), cap=DRAFT_CAP,
            rulings=rulings_block(rulings, redact=redact),
            voice=cos_voice.prompt_block(
                voice_profile or cos_voice.profile_state(None), redact=redact),
            batch=_batch_json(_draft_shaper(
                draft_rows, ctx_by_id), redact=redact)),
    }
