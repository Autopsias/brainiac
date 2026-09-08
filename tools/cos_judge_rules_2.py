"""The second half of `cos_judge`'s rule checks, plus the three validators (batch-2 drain).

Moved verbatim out of `cos_judge`; the rules register into
`cos_judge_rules.RULES` through the imported decorator, so the registry stays
ONE dict and the parent's re-export is unchanged.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from brain.cos_runverify import (              # noqa: E402  the ONE definition
    _DEDUP_CHECKS as DEDUP_CHECKS,
    _HELD_REASONS as HELD_REASONS,
    _LEDGER_DISPOSITIONS as LEDGER_DISPOSITIONS,
    _PLACEHOLDER_CATEGORIES as PLACEHOLDER_CATEGORIES,
)
import cos_judge_ingest  # noqa: E402
from cos_judge_rules_stale import (  # noqa: E402
    STALE_REASONS, stale_field_refusal)
from cos_judge_rules import (  # noqa: E402
    BRIEF_ORDER, DRAFT_CAP, FIREWALL_CLOSE, FIREWALL_OPEN,
    NOVELTY_WORDS, RULES, SECRET_RE,
    TIER_ORDER, _draft,
    _g, _taxo, rule)


@rule("staging.evidence_required", "row",
      "Phase 1.6 rule 2 — 'No evidence ⇒ no candidate'")
def _r_span(v, ctx):
    if v.get("disposition") != "candidate":
        return None
    span = v.get("evidence_span")
    if not isinstance(span, dict):
        return ("a candidate with no source span — a plausible-sounding inference "
                "with no quote to back it is dropped, never proposed")
    try:
        start, end = int(span["start"]), int(span["end"])
    except (KeyError, TypeError, ValueError):
        return f"evidence_span {span!r} is not a pair of integer offsets"
    if start < 0 or end <= start:
        return f"evidence_span {start}..{end} is empty or inverted"
    n = int(_g(ctx, "text_len", 0))
    if end > n:
        return (f"evidence_span {start}..{end} runs past the {n} characters this "
                "run actually captured — an unresolvable pointer is not evidence")
    return None


@rule("staging.dedup_never_drops", "row",
      "Phase 1.6 rule 5 / DED-01 — 'DEDUP NEVER DROPS A CANDIDATE'")
def _r_dedup_drop(v, ctx):
    near = _g(ctx, "brain_near_dup")
    qualified = bool(v.get("substance_kind")) and bool(v.get("evidence_span"))
    if not near or not qualified:
        return None
    if v.get("disposition") != "candidate":
        return ("a rule-2-qualified thread yielded no candidate because its "
                "substance is already in the brain — that is a MERGE, not a "
                "silence")
    if v.get("dedup_kind") != "merge_candidate" or not v.get("merge_candidate"):
        return (f"a near-duplicate of {near!r} staged as a fresh `create` — the "
                "owner's batch answer must read 'merge', not 'add'")
    return None


@rule("staging.dedup_vocabulary", "row",
      "Phase 1.6 rule 5 — `clean` | `inconclusive` | `not-run`, AND NO OTHER")
def _r_dedup_vocab(v, ctx):
    d = v.get("dedup_check")
    if d is not None and d not in DEDUP_CHECKS:
        return (f"dedup_check {d!r} is outside {sorted(DEDUP_CHECKS)} — run 106 "
                "wrote a novelty verdict into this slot")
    return None


@rule("staging.no_novelty_verdict", "row",
      "Phase 1.6 rule 5 — rule 2 is a SUBSTANCE test, never a novelty test")
def _r_novelty(v, ctx):
    for field in ("held_reason", "dedup_check", "disposition"):
        val = str(v.get(field) or "").lower()
        if not val:
            continue
        if any(w in val for w in NOVELTY_WORDS):
            return (f"{field}={v.get(field)!r} is a NOVELTY verdict — a word that "
                    "appears zero times in the doctrine, standing in for rule 2's "
                    "substance test")
        if " " in val.strip():
            return f"{field}={v.get(field)!r} is a sentence, not a managed word"
    return None


@rule("staging.disposition_vocabulary", "row",
      "Phase 1.6 rule 8 — `candidate` | `held` | `no-substance` (+ the marker)")
def _r_disp(v, ctx):
    d = v.get("disposition")
    if d not in LEDGER_DISPOSITIONS:
        return (f"disposition {d!r} is outside {sorted(LEDGER_DISPOSITIONS)}; "
                "these words DEFINE the counters, so an invented one reads as "
                "absence")
    return None


@rule("staging.held_reason_managed_set", "row",
      "Phase 1.6 rule 8 — held_reason REQUIRED on every non-candidate row, from the set")
def _r_held_reason(v, ctx):
    hr = v.get("held_reason")
    if v.get("disposition") == "candidate":
        return f"a `candidate` row carries held_reason {hr!r}" if hr else None
    if not hr:
        return "a non-`candidate` row carries no `held_reason` at all"
    if hr not in HELD_REASONS:
        return (f"held_reason {hr!r} is outside the managed set — the checks key "
                "on the WORD, so an invented one is invisible to them")
    return None


@rule("staging.classification_default_mnpi", "row",
      "Phase 1.6 rule 4 — most-restrictive default; only an overlay mapping lowers it")
def _r_class(v, ctx):
    if v.get("disposition") != "candidate":
        return None
    c = v.get("classification")
    if c not in TIER_ORDER:
        return f"classification {c!r} is not one of {TIER_ORDER}"
    if c == "MNPI":
        return None
    mapped = _g(ctx, "overlay_keyword_tier")
    if mapped != c:
        return (f"classification {c!r} below MNPI with no explicit overlay "
                "keyword mapping — a vault with no such rule ships MNPI")
    return None


@rule("staging.secret_scrub", "row",
      "Phase 1.6 rule 3 — credential-shaped spans redacted before the drop")
def _r_secret(v, ctx):
    text = v.get("candidate_text")
    if text and SECRET_RE.search(text):
        return "the candidate text carries a credential-shaped span unredacted"
    return None


@rule("staging.category_defined_id", "row",
      "Phase 1.6 rule 1¾ / E16 — the real id or absent, never a stand-in")
def _r_category(v, ctx):
    c = v.get("category")
    if c is None:
        return None
    if str(c).strip().lower() in PLACEHOLDER_CATEGORIES:
        return f"category {c!r} is a placeholder — the value is the real id or absent"
    if c not in _taxo(ctx):
        return f"category {c!r} is not an id the owner's parsed taxonomy defines"
    return None


@rule("triage.stale_vocabulary", "row",
      "STALE-01 — `stale` is {is_stale, reason}, `act` only, reason from the closed set")
def _r_stale(v, ctx):
    """The FIELD's SHAPE, checked for every row. Its EVIDENCE is
    `triage.stale_evidence` below, and the SIGNAL's is
    `triage.noise_signal_required`. Three rules because they fail differently:
    a malformed `stale` is a broken answer, a stale claim the facts refuse is a
    wrong one, a signal with no field behind it is a word with no producer —
    and a run that cannot tell them apart cannot be calibrated.
    """
    st = v.get("stale")
    if st is None:
        return None
    if not isinstance(st, dict):
        return f"stale {st!r} is not a mapping of `is_stale` + `reason`"
    if not isinstance(st.get("is_stale"), bool):
        return (f"stale.is_stale {st.get('is_stale')!r} is not a boolean — "
                "the counters key on the value, so an absent one reads as false")
    if not st.get("is_stale"):
        return None
    if st.get("reason") not in STALE_REASONS:
        return (f"stale.reason {st.get('reason')!r} is outside "
                f"{list(STALE_REASONS)} — the checks key on the WORD")
    if v.get("bucket") != "act":
        return (f"stale claimed on a `{v.get('bucket')}` verdict — the lane "
                "archives ACTIONABLE threads the world moved past; a `read` or "
                "`noise` thread has its own lanes and its own evidence")
    return None


@rule("triage.stale_evidence", "row",
      "STALE-01 — a `stale` FIELD that claims staleness faces the host's own "
      "facts, whatever the verdict says about archiving")
def _r_stale_evidence(v, ctx):
    """THE FIELD IS WHAT ARCHIVES THE THREAD, so the field is what is checked.
    `archive_eligibility` promotes an `act` row off `stale` alone, while the
    only caller of the evidence refusal was `triage.noise_signal_required`,
    which returns early on `not v.get("auto_archive")` — so the shape the
    prompt's own template prints (`"auto_archive": false` beside a populated
    `stale`) validated clean against a context carrying a standing ask, a live
    deadline, an open commitment AND an unsent draft, and was archived anyway
    (review 2026-08-25, finding 1). The signal rule keeps its dispatch for the
    one thing this cannot say: a signal with no field behind it.
    """
    return stale_field_refusal(v, ctx)


@rule("staging.ingest_independent", "row",
      "INGEST-01 belt 1 — every in-scope row carries an explicit `ingest`, "
      "decided from substance and never from the bucket, the archive or the draft")
def _r_ingest(v, ctx):
    """RECOMPUTED, not read. `_prepared_verdict` derives the field; this
    derives it AGAIN from the same verdict and refuses a disagreement, so a
    producer that starts reading the bucket, `auto_archive` or the draft is
    caught by a check that never learned to.

    THE ABSENT FIELD IS THE FAILURE THIS RULE IS NAMED FOR. Commit f270700
    records the near-identical predecessor: a single-vocabulary design let 110
    read-but-unowed threads fall through with NO LANE AT ALL — no word said
    what happened to them, so nothing could count them. An `ingest` that is
    missing, or is not a mapping, is refused here rather than defaulted.
    """
    got = v.get("ingest")
    if not isinstance(got, dict):
        return (f"the row carries ingest {got!r} — every in-scope row states "
                "its ingest relevance explicitly, because a row with no lane "
                "at all is counted by nothing (f270700: 110 such rows)")
    want = cos_judge_ingest.ingest_field(v, ctx)
    if got.get("relevant") is not want["relevant"]:
        return (f"ingest.relevant is {got.get('relevant')!r} on a "
                f"`{v.get('disposition')}` row — relevance is the staging "
                "pass's substance answer and nothing else; the bucket, the "
                "archive claim and the draft never move it")
    if got.get("relevant") and got.get("content") not in cos_judge_ingest.CONTENT_LANES:
        return (f"ingest.content {got.get('content')!r} is outside "
                f"{list(cos_judge_ingest.CONTENT_LANES)} — a relevant thread "
                "whose lane is unnamed is a thread the bridge cannot build "
                "content from")
    if got.get("relevant") and got.get("content") != want["content"]:
        return (f"ingest.content {got.get('content')!r} disagrees with the "
                f"taxonomy lane {want['content']!r} for category "
                f"{v.get('category')!r} — one rule, not one rumour")
    if not got.get("relevant") and got.get("content") is not None:
        return (f"a non-relevant row names content lane {got.get('content')!r}")
    return None


@rule("staging.candidate_stamps", "row",
      "Phase 1.6 rule 8 / STA-03 — a candidate carries its proposal id and digest")
def _r_stamps(v, ctx):
    if v.get("disposition") != "candidate" or not _g(ctx, "proposals_dropped", True):
        return None
    if not v.get("proposal_id"):
        return "a `candidate` row with no `proposal_id`"
    sha = str(v.get("content_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return ("a `candidate` row without the host-returned `content_sha256` "
                "proves nothing about those bytes and quarantines")
    return None


# -- drafting (SKILL.md Phase 1 step 5) -------------------------------------
@rule("draft.never_sends", "row", "Phase 1 rule 10 — drafts only; sending is the owner's")
def _r_send(v, ctx):
    d = _draft(v)
    if v.get("send_attempted"):
        return "a send was attempted — the run has no send primitive at all"
    if d and d.get("sent"):
        return "a draft marked sent — sending is the owner's alone, structurally"
    return None


@rule("draft.original_thread_recipients_only", "row",
      "Phase 1 EXFIL-06 rule 12 — an off-thread recipient is HELD, never composed")
def _r_recipients(v, ctx):
    d = _draft(v)
    if d and d.get("recipients_scope") != "original-thread-only":
        return (f"draft recipients scope {d.get('recipients_scope')!r} — a reply "
                "to any recipient off the original thread is HELD")
    return None


@rule("draft.response_warranted_scope", "row",
      "Phase 1 step 5 v5.27 — ACT rows, plus READ rows held on `ask`/`deadline`")
def _r_draft_scope(v, ctx):
    if not _draft(v):
        return None
    if v.get("bucket") == "act":
        return None
    if v.get("hold_category") in ("Held · ask", "Held · deadline"):
        return None
    return (f"a draft on a `{v.get('bucket')}` row held as "
            f"{v.get('hold_category')!r} — not response-warranted")


@rule("draft.idempotent_vs_drafts", "row",
      "Phase 1 step 5 — a convid already carrying an unsent draft is skipped")
def _r_draft_idem(v, ctx):
    if not _draft(v):
        return None
    cid = v.get("conversation_id")
    if cid in (_g(ctx, "drafts_inventory") or []) \
            and cid not in (_g(ctx, "expired_cos_draft_convids") or []):
        return "a second draft on a conversation that already carries an unsent one"
    return None


@rule("draft.owner_confirm_placeholders", "row",
      "Phase 1 step 5 — brain-grounded, with `[owner: confirm …]` where it is silent")
def _r_placeholder(v, ctx):
    d = _draft(v)
    if not d:
        return None
    if d.get("brain_grounded") is True:
        return None
    if not (d.get("placeholders") or []):
        return ("an ungrounded draft with no `[owner: confirm …]` placeholder — "
                "that is an invented fact in the owner's voice")
    return None


@rule("draft.stale_ask_form", "row",
      "Phase 1 step 5 v2.1 — 'Age alone is never a logged skip reason'")
def _r_stale(v, ctx):
    if int(_g(ctx, "ask_age_days", 0)) <= 7:
        return None
    if v.get("bucket") != "act" and v.get("hold_category") not in (
            "Held · ask", "Held · deadline"):
        return None
    d = _draft(v)
    if not d:
        if "stale" in str(v.get("skip_reason") or "").lower() \
                or "age" in str(v.get("skip_reason") or "").lower():
            return "an ACT row skipped for age — age alone is never a skip reason"
        return None
    if d.get("form") != "acknowledge-late":
        return ("a stale ask drafted in the standard form — it takes the shorter "
                "acknowledge-late + current-position form")
    return None


@rule("draft.voice_or_declared_neutral", "row",
      "Phase 1 step 5 — the voice skill (the owner's own bundle if installed, else the `overlay-style` kernel skill) in DRAFT then CHECK, or a DECLARED fallback")
def _r_voice(v, ctx):
    d = _draft(v)
    if not d:
        return None
    voice = str(d.get("voice") or "")
    if voice == "skill:draft+check" or voice.startswith("neutral:"):
        return None
    return ("a draft with neither a voice-skill DRAFT+CHECK pass nor a declared "
            "neutral-register fallback (the brief footer has to say so)")


@rule("draft.never_unread_row", "row",
      "Phase 1 step 5 / E22(a4) — never open, select or hover an UNREAD row")
def _r_draft_unread(v, ctx):
    if _draft(v) and _g(ctx, "read_state") != "read":
        return "a draft composed off an UNREAD row — an automatic FAIL"
    return None


# -- run-level ---------------------------------------------------------------
@rule("draft.cap", "run", "Phase 1 step 5 — one cap for the leg as a whole, ACT first")
def _r_cap(run, _ctx=None):
    n = int(run.get("drafts", 0))
    if n > DRAFT_CAP:
        return f"{n} drafts against a cap of {DRAFT_CAP} for the leg as a whole"
    if run.get("act_first") is False:
        return ("READ rows took slots ahead of ACT rows — they compete for the "
                f"SAME {DRAFT_CAP}, ACT first")
    return None


# -- the brief (SKILL.md Phase 5) -------------------------------------------
def _headings(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", h).strip()
            for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S)]


@rule("brief.csp_first_head_element", "brief",
      "Phase 5 — the image-containment CSP is the REQUIRED first element of <head>")
def _r_csp(html, _ctx=None):
    m = re.search(r"<head[^>]*>(.*?)(<\w)", html, re.S)
    if not m:
        return "the brief has no <head>"
    first = html[m.start(2):m.start(2) + 200]
    if not first.lstrip().lower().startswith("<meta"):
        return "the first element inside <head> is not the CSP meta"
    if "Content-Security-Policy" not in first or "script-src 'none'" not in first:
        return "the first <head> element is not the image-containment CSP"
    return None


@rule("brief.no_remote_assets", "brief",
      "Phase 5 / EXFIL-03 — no remote <img>, no script; a remote image is zero-click exfil")
def _r_remote(html, _ctx=None):
    for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
        if not src.startswith("data:"):
            return f"a non-data image source ({src[:60]}) — a zero-click exfil channel"
    if re.search(r"<script\b", html, re.I):
        return "the brief carries a <script> element; it is CSS-only by contract"
    return None


@rule("brief.component_order", "brief", "Phase 5 — components in the documented order")
def _r_order(html, _ctx=None):
    got = [h for h in _headings(html) if h in BRIEF_ORDER]
    want = [h for h in BRIEF_ORDER if h in got]
    if got != want:
        return f"component order {got} is not the documented {want}"
    missing = [h for h in BRIEF_ORDER if h not in got]
    if missing:
        return f"components missing from the brief entirely: {missing}"
    return None


@rule("brief.staged_line_denominator", "brief",
      "Phase 5 component 5 / EXT-06b — staged count FIRST, and the denominator named")
def _r_staged(html, _ctx=None):
    if not re.search(r"\d+\s+staged\s+·\s+\d+\s+in scope\s+·\s+\d+\s+held", html):
        return ("the staged line is missing or does not name its denominator "
                "('<staged> staged · <in-scope> in scope · <held> held (<reason>)')")
    return None


@rule("brief.outcome_contract_line", "brief",
      "Phase 5 component 1 / OC-01 — a STANDING line on EVERY run")
def _r_outcome(html, _ctx=None):
    if not re.search(r"OUTCOME CONTRACT:\s*(PASS|FAILED)", html):
        return "no OUTCOME CONTRACT line — it is standing, degraded or clean"
    return None


@rule("brief.empty_sections_render_none", "brief",
      "Phase 5 — empty sections render `(none)`, never vanish")
def _r_none(html, _ctx=None):
    got = _headings(html)
    for want in BRIEF_ORDER:
        if want not in got:
            return f"the `{want}` component vanished instead of rendering `(none)`"
    return None


@rule("brief.evidence_line_firewalled", "brief",
      "Phase 5 component 5 — one evidence line per item, never unwrapped")
def _r_firewall(html, ctx=None):
    n = int((ctx or {}).get("staged", 0)) if isinstance(ctx, dict) else 0
    opens = html.count(FIREWALL_OPEN)
    closes = html.count(FIREWALL_CLOSE)
    if opens != closes:
        return f"{opens} firewall openers against {closes} closers"
    if opens == 0 and "staged" in html and not re.search(r"\b0 staged", html):
        return ("staged items rendered with no firewalled evidence line — an item "
                "with no evidence line is a bug, not a short render")
    if n and opens < n:
        return f"{n} staged item(s) but only {opens} firewalled evidence line(s)"
    return None


@rule("brief.never_a_decision_surface", "brief",
      "Phase 5 component 5 — never adds an option, never recommends an answer")
def _r_decision(html, _ctx=None):
    for pat in (r"recommended answer", r"we recommend (?:you )?accept",
                r"\baccepted by this run\b", r"\bdecided tonight\b"):
        if re.search(pat, html, re.I):
            return f"the brief recommends or reports a decision ({pat!r})"
    return None


# ---------------------------------------------------------------------------
# validation entry points
# ---------------------------------------------------------------------------
def validate_verdict(v: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for r in RULES.values():
        if r.scope != "row":
            continue
        detail = r.check(v, ctx)
        if detail:
            out.append({"rule_id": r.id, "detail": detail})
    return out


def validate_run(run: dict[str, Any]) -> list[dict[str, str]]:
    return [{"rule_id": r.id, "detail": d}
            for r in RULES.values() if r.scope == "run"
            for d in [r.check(run)] if d]


def validate_brief(html: str, ctx: dict[str, Any] | None = None) -> list[dict[str, str]]:
    return [{"rule_id": r.id, "detail": d}
            for r in RULES.values() if r.scope == "brief"
            for d in [r.check(html, ctx or {})] if d]


def check_one(rule_id: str, payload: Any, ctx: Any) -> str | None:
    r = RULES[rule_id]
    return r.check(payload, ctx)
