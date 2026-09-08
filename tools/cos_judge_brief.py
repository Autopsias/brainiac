"""The brief renderer, the PNG writer and the golden-set evaluator of `cos_judge` (batch-2 drain).

Moved verbatim out of `cos_judge` and re-imported by it, so
`cos_judge.compose_brief`, `render_png`, `selfcheck` and `evaluate_golden`
keep their module path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_judge_ingest  # noqa: E402
from cos_judge_rules import (  # noqa: E402
    DRAFT_CAP, FIREWALL_CLOSE, FIREWALL_OPEN, RULES,
    _age_days)
from cos_judge_rules_2 import (  # noqa: E402
    validate_brief, validate_run, validate_verdict)

CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
       'img-src \'self\' data:; style-src \'self\' \'unsafe-inline\'; '
       "font-src 'self' data:; script-src 'none'; base-uri 'none'; "
       'form-action \'none\'">')



# ---------------------------------------------------------------------------
# the morning brief
# ---------------------------------------------------------------------------
def _sect(name: str, body: str) -> str:
    return f"<h2>{escape(name)}</h2>\n{body or '<p>(none)</p>'}\n"


def _rows_html(items, fmt):
    return ("<ul>" + "".join(f"<li>{fmt(i)}</li>" for i in items) + "</ul>"
           ) if items else ""


def _banner_and_tldr(contract: str, counters: dict[str, Any], notes: list[str],
                     drafts: list[dict[str, Any]], act: list[dict[str, Any]],
                     read: list[dict[str, Any]], leaving: int,
                     triage_count: int,
                     stuck: list[dict[str, Any]]) -> tuple[str, str]:
    """The Banner and TL;DR sections — Phase 5's first two, both driven off the
    same triage split and counters.

    `stuck` is the third outcome the owner ruled on 2026-08-28: a thread the
    porter neither drafted for nor archived, carrying the ONE WORD saying why.
    It is named in the TL;DR because the sheet is read from the top, and a
    silence three sections down is the failure this ruling exists to end."""
    banner = (f"<p>OUTCOME CONTRACT: {contract} — profile full · enumerated "
              f"{counters.get('ingestion_in_scope', 0)} · archive:hold:drafted "
              f"0:{counters.get('ingestion_held', 0)}:{len(drafts)}</p>"
              + "".join(f"<p>{escape(n)}</p>" for n in notes))
    untriaged = counters.get("ingestion_in_scope", 0) - triage_count
    tldr = _rows_html(
        # `leaving` — NOT `len(noise)`: the same number the READ section and
        # the metrics strip print (noise + the stale-act threads the porter
        # took). One sheet printing two would-archive counts is the same drift
        # in the opposite direction.
        [f"{len(act)} thread(s) need you; {len(read)} worth your eyes; "
         f"{leaving} would archive — out of "
         f"{counters.get('ingestion_in_scope', 0)} enumerated"
         + (f", and {untriaged} this run could not triage at all "
            "(no sender or subject was persisted for them)." if untriaged
            else "."),
         f"{counters.get('ingestion_candidates', 0)} candidate(s) staged for the "
         "host's next batched question — nothing is decided here.",
         f"{len(drafts)} reply draft(s) written; none was placed in the mailbox "
         "by this run.",
         f"{len(stuck)} thread(s) the porter could not decide — see NEEDS YOU."],
        lambda s: escape(s))
    return banner, tldr


def _staged_row(c: dict[str, Any], spans: dict[str, str]) -> str:
    quote = spans.get(c["conversation_id"], "")
    return (f"<b>{escape(c.get('category') or 'uncategorised')}</b> · "
            f"{escape(c.get('substance_kind') or '')} · "
            f"{escape(c.get('dedup_kind') or 'create')}"
            + (f" → {escape(c.get('merge_candidate') or '')}"
               if c.get("dedup_kind") == "merge_candidate" else "")
            + f"<br><span class=q>{escape(FIREWALL_OPEN)} {escape(quote)} "
              f"{escape(FIREWALL_CLOSE)}</span>")


#: The sheet's attachment line is written by a leg that runs BEFORE the two
#: producers of its numbers, so the line is ANCHORED and stamped later. See
#: `attachment_paragraph` and `stamp_attachment_brief` below.
ATTACHMENT_ANCHOR_ID = "attachment-lane"
ATTACHMENT_ELEMENT_TAGS = (f'<p id="{ATTACHMENT_ANCHOR_ID}">', "</p>")


def brief_png_height(html: str) -> int:
    """The render height for a sheet — ~26px per rendered line, floored at the
    viewport default and capped so a pathological night cannot ask Chrome for
    a 100k-pixel window. One definition: `write_night` renders the sheet and
    the attachment stamp re-renders it, and two formulas would give the
    morning two differently-cropped images of one night."""
    lines = html.count("<li>") + html.count("<p>")
    return max(1600, min(12000, 900 + 46 * lines))


def morning_brief_path(ops: Path, run_id: str) -> Path:
    """Where the night's morning sheet lands. ONE definition, two readers:
    `cos_judge.write_night` writes it, and the attachment stamp reopens it
    hours later to fill in the line it could not know at compose time."""
    return ops / f"_briefing_morning_{run_id[:10]}-run{run_id.rsplit('run', 1)[-1]}.html"


def attachment_paragraph(metrics: dict[str, Any]) -> str:
    """The sheet's attachment line — ANCHORED, and produced by whichever leg
    can honestly answer it.

    THE JUDGMENT LEG CANNOT (review 2026-08-25). Both producers of these
    numbers run AFTER it: `tools/cos_nightly.sh` runs the judge at the
    `cos_judge.py --judge` line, the ingest bridge (which writes the manifest)
    ~140 lines later and the attachment fetch (which writes the report)
    ~270 lines later. FIX-02's first cut had `judge_night` read both artifacts
    anyway, so on a run188-shaped night it read an EMPTY manifest directory and
    printed `(none this run — lane not-exercised)` — the exact constant the fix
    set out to remove, now as a fabricated read.

    So the judge emits the PENDING form, which says what it is, and the fetch
    leg stamps the real counts into this element afterwards — the same
    supersede-after-the-fact shape `stamp_attachment_lane` already uses for the
    metrics row. A sheet still carrying the pending sentence in the morning is
    telling the truth: the attachment lane never ran that night.
    """
    open_tag, close_tag = ATTACHMENT_ELEMENT_TAGS
    if "attachments_dropped" not in metrics and "attachments_fetched" not in metrics:
        body = ("not reported by this leg — the attachment lane runs after it. "
                "The fetch leg stamps the run's real counts here; a line still "
                "reading this in the morning means the lane never ran")
    else:
        att_dropped = int(metrics.get("attachments_dropped") or 0)
        att_fetched = int(metrics.get("attachments_fetched") or 0)
        lane = escape(str(metrics.get("attachment_lane") or "?"))
        body = (f"{att_dropped} attachment(s) dropped to the manifest, "
                f"{att_fetched} fetched — lane {lane}"
                if att_dropped or att_fetched
                else f"(none this run — lane {lane})")
    return f"{open_tag}{body}{close_tag}"


def stamp_attachment_brief(vault: Path, run_id: str,
                           report: dict[str, Any] | None = None
                           ) -> dict[str, Any]:
    """FILL IN the sheet's attachment line, now that it can be read.

    Called by `tools/cos_attachment_fetch.py` — the leg that moves the bytes,
    and therefore the first moment in the night when both producers of these
    counts have run. It replaces the anchored PENDING element
    `attachment_paragraph` left behind with the real line, and re-renders the
    PNG, because the sheet's image is what the morning looks at and a stale
    one would put the pending sentence back.

    Same shape as `cos_driver_night_records.stamp_attachment_lane` does for
    the metrics row: the artifact is corrected after the fact rather than
    guessed at ahead of it. Reported, never fatal — an unstamped sheet is a
    reporting gap, not a reason to unwind delivered files.

    IT LIVES HERE, WITH THE SHEET IT EDITS, and that is a shipping constraint
    as much as a tidiness one: `cos_driver_night_records` rides the wheel and
    is re-executed by the run validator from an installed engine that has no
    `tools/` of its own, so an import of this module from there would put the
    whole brief renderer in the validator's closure
    (`tests/test_release.py::test_every_checker_the_run_validator_loads_rides_the_wheel`).
    """
    from cos_driver_night_records import attachment_lane_facts   # noqa: PLC0415

    from brain import cos                                        # noqa: PLC0415

    facts = attachment_lane_facts(vault, run_id, report=report)
    path = morning_brief_path(cos.run_ops_dir(vault), run_id)
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return {"stamped": "no-brief", "facts": facts}
    open_tag, close_tag = ATTACHMENT_ELEMENT_TAGS
    start = html.find(open_tag)
    end = html.find(close_tag, start) if start >= 0 else -1
    if start < 0 or end < 0:
        # A SHEET WITH NO ANCHOR IS NOT SILENTLY LEFT ALONE. The compose side
        # and this one share one element definition; if they ever drift, the
        # morning must see the gap rather than a sentence nobody updated.
        return {"stamped": "no-anchor", "facts": facts}
    end += len(close_tag)
    line = attachment_paragraph(facts)
    if html[start:end] == line:
        return {"stamped": "unchanged", "facts": facts}
    stamped = html[:start] + line + html[end:]
    path.write_text(stamped, encoding="utf-8")
    png = path.with_suffix(".png")
    render = render_png(path, png, height=brief_png_height(stamped))
    ok = render.get("exists") and render.get("bytes", 0) > 0
    return {"stamped": "rewritten", "facts": facts, "brief_html": str(path),
            "brief_png": str(png) if ok else None, "render": render}


def _required_html(holds: dict[str, int], counters: dict[str, Any],
                   staged: list[dict[str, Any]], spans: dict[str, str],
                   metrics: dict[str, Any]) -> str:
    """The REQUIRED ACTIONS section: staged candidates plus the attachment line.

    The attachment line itself is `attachment_paragraph` above — read its
    docstring before changing who passes counts in here.
    """
    top_reason = max(holds.items(), key=lambda kv: kv[1])[0] if holds else "none"
    staged_line = (f"{counters.get('ingestion_candidates', 0)} staged · "
                   f"{counters.get('ingestion_in_scope', 0)} in scope · "
                   f"{counters.get('ingestion_held', 0)} held ({top_reason})")
    return (f"<p>{escape(staged_line)}</p>"
            + "<h3>ingestion</h3>"
            + (_rows_html(staged, lambda c: _staged_row(c, spans)) or "<p>(none)</p>")
            + f"<h3>attachment</h3>{attachment_paragraph(metrics)}"
            + "<h3>supersede</h3><p>not visible from this leg — see the "
              "host's inbox question</p>")


def compose_brief(*, run_id: str, contract: str, counters: dict[str, Any],
                  triage: list[dict[str, Any]], staged: list[dict[str, Any]],
                  drafts: list[dict[str, Any]], holds: dict[str, int],
                  metrics: dict[str, Any], notes: list[str],
                  spans: dict[str, str],
                  footer_notes: tuple[str, ...] = (),
                  stale_archived: set[str] | frozenset = frozenset()) -> str:
    """Phase 5's components, in Phase 5's order, with Phase 5's containment.

    `stale_archived` is the conversation-id set the HOST made archive-eligible
    on the stale-act lane (`cos_judge_apply.archive_eligibility`, read off the
    ledger the caller just wrote — never off the model's own claim). The
    ruling STALE-01 encodes has two halves — "the porter archives it AND names
    it on the sheet" — and only the first had a producer: such a thread went on
    being counted under "thread(s) need you" while it was leaving the inbox,
    and the would-archive census counted `noise` alone (review 2026-08-25,
    finding 4). It is now named in its own line and counted where it belongs.
    """
    stale = [t for t in triage if t.get("bucket") == "act"
             and t.get("conversation_id") in stale_archived]
    act = [t for t in triage if t.get("bucket") == "act"
           and t.get("conversation_id") not in stale_archived]
    read = [t for t in triage if t.get("bucket") == "read"]
    noise = [t for t in triage if t.get("bucket") == "noise"]
    leaving = len(noise) + len(stale)

    # THE THIRD OUTCOME (owner ruling 2026-08-28). Every actionable thread must
    # produce SOMETHING: a draft, or this word. The list is derived from the
    # accepted triage rows, so a word the projection refused never reaches here.
    stuck = [t for t in triage if t.get("needs_owner")]
    banner, tldr = _banner_and_tldr(contract, counters, notes, drafts,
                                    act, read, leaving, len(triage), stuck)
    required = _required_html(holds, counters, staged, spans, metrics)

    read_block = (_rows_html(read, lambda t: f"{escape(t.get('tier') or '')} · "
                             f"{escape((t.get('summary') or [''])[0])}")
                  + f"<p>Would archive ({leaving}): shadow — none archived; "
                    "this run has no mutation lane.</p>"
                  + f"<h3>stale, archived ({len(stale)})</h3>"
                  + (_rows_html(
                      stale,
                      lambda t: f"{escape(t.get('tier') or '')} · "
                                f"{escape((t.get('summary') or [''])[0])} — "
                                f"{escape((t.get('stale') or {}).get('reason') or '')}")
                     or "<p>(none)</p>"))
    ledger = ("<p>0 marked / 0 archived / 0 captured / "
              f"{len(drafts)} draft(s) written (not saved to the mailbox) / "
              f"{counters.get('ingestion_candidates', 0)} candidate(s) judged "
              "(not dropped: judgment-only run)</p>")
    strip = (f"<p>inbox_count {metrics.get('inbox_count')} · "
             f"body_open_actual {metrics.get('body_open_actual')} · "
             f"would_archive_count {leaving}</p>")

    body = (_sect("Banner", banner)
            + _sect("TL;DR", tldr)
            + _sect("TODAY", "")
            + _sect("DRAFTS READY", _rows_html(
                drafts,
                lambda d: f"{escape(d.get('recipient') or 'original thread')} · "
                f"RE: {escape(d.get('subject') or '')} · "
                f"{escape((d.get('draft') or {}).get('form') or '')}<br>"
                f"<span class=q>{escape((d.get('summary') or ['', ''])[1])}</span>"))
            + _sect("NEEDS YOU", _rows_html(
                stuck,
                lambda t: f"<b>{escape(t.get('needs_owner') or '')}</b> · "
                f"{escape(t.get('tier') or '')} · "
                f"{escape((t.get('summary') or [''])[0])}"))
            + _sect("REQUIRED ACTIONS", required)
            + _sect("READ", read_block)
            + _sect("BATTLECARDS", "")
            + _sect("LATE + RADAR", "")
            + _sect("OVERNIGHT LEDGER", ledger)
            + _sect("TOMORROW", "")
            + _sect("INBOX-ZERO METRICS", strip)
            + _sect("CALIBRATION", "<p>Drafts sendable as-is? · brief too "
                    "long/short/right? · anything misjudged or missed? Reply here "
                    "or add one dated line to cos-ops/_cos_feedback.md.</p>"
                    + "".join(f"<p>{escape(n)}</p>" for n in footer_notes)))
    style = ("body{font:15px/1.5 -apple-system,Segoe UI,sans-serif;margin:2rem auto;"
             "max-width:60rem;color:#1b1b1b}h1{font-size:1.4rem}h2{font-size:1rem;"
             "text-transform:uppercase;letter-spacing:.06em;color:#0b6;"
             "border-bottom:1px solid #ddd;padding-bottom:.2rem;margin-top:1.6rem}"
             "h3{font-size:.85rem;color:#555;margin:.8rem 0 .2rem}"
             "li{margin:.25rem 0}.q{color:#666;font-size:.85rem}")
    return (f"<!doctype html><html><head>{CSP}"
            f"<meta charset=\"utf-8\"><title>COS brief {escape(run_id)}</title>"
            f"<style>{style}</style></head><body>"
            f"<h1>Morning brief — {escape(run_id)}</h1>{body}</body></html>")


def render_png(html_path: Path, png_path: Path, *, height: int = 1600
               ) -> dict[str, Any]:
    """Render, then LOOK: a rendered file is not a rendered brief.

    The height is passed explicitly and sized to the CONTENT. The renderer's
    1600px viewport default silently clips a long brief at the fold, and a
    clipped PNG is indistinguishable from a complete one unless somebody looks
    — which is the whole failure mode this function's docstring names.
    """
    # cos_render_png bounds a render at 60s and SIGKILLs the whole Chrome
    # process GROUP past it — so on a loaded machine a render that takes 14s
    # idle exceeds the bound and returns `no-png-produced` with returncode -9.
    # Measured 2026-08-15: this render passes alone and fails DETERMINISTICALLY
    # (twice, back to back) under the 8-worker test gate. The bound is therefore
    # overridable by the caller that KNOWS it is contended. The default is the
    # renderer's own 60s, so THE INNER BOUND is unchanged for the nightly.
    #
    # THE OUTER WAIT IS NOT UNCHANGED, and the commit that made this change said
    # "byte-for-byte unchanged" when it was not (review 2026-08-15): the
    # subprocess wait moved from a fixed `timeout=300` to `bound + 60`, which is
    # 120 s at the default. It is still strictly greater than the inner bound —
    # which is the property that matters, since a shorter outer wait would kill
    # the renderer before it could report its own timeout honestly — but a
    # nightly render that used to have 300 s of caller patience now has 120 s.
    # Stated rather than left as a claim somebody would read as settled.
    # Coerced to a float and CLAMPED, so the argv element below can only ever
    # be a number in [10, 900] — a non-numeric value raises here and never
    # reaches the argument list. There is no shell (argv list, shell=False),
    # so the semgrep taint below is on a value that cannot carry a command.
    try:
        bound = float(os.environ.get("COS_RENDER_TIMEOUT_S") or 60.0)
    except ValueError:
        bound = 60.0
    bound = min(900.0, max(10.0, bound))
    proc = subprocess.run(
        # The rule anchors on the argv list (the taint source), so the
        # suppression has to sit here, not above the `subprocess.run(` line
        # (the same placement `tools/brain_daily.py` documents).
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        [sys.executable, str(Path(__file__).resolve().parent / "cos_render_png.py"),
         "render", str(html_path), "--out", str(png_path),
         "--height", str(height), "--timeout", str(bound)],
        # The outer wait must outlast the inner bound, or this kills the
        # renderer before the renderer can report its own timeout honestly.
        capture_output=True, text=True, timeout=bound + 60.0)
    return {"returncode": proc.returncode, "stdout": proc.stdout[-500:],
            "stderr": proc.stderr[-500:], "exists": png_path.exists(),
            "bytes": png_path.stat().st_size if png_path.exists() else 0}


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------
def selfcheck() -> int:
    base = {"conversation_id": "c1", "bucket": "act", "tier": "P1",
            "triage_evidence": "direct ask from a P1 sender", "summary": ["a", "b"],
            "category": "decision-record", "disposition": "candidate",
            "held_reason": None, "dedup_check": "clean", "dedup_kind": "create",
            "classification": "MNPI", "substance_kind": "decision",
            "evidence_span": {"start": 1, "end": 20}, "proposal_id": "p1",
            "content_sha256": "a" * 64}
    ctx = {"sender": "a@b.c", "priority_map": {"a@b.c": "P1"}, "read_state": "read",
           "body_opened": True, "text_len": 100,
           "taxonomy": {"decision-record": {"disposition": "always"}}}
    # (INGEST-01) DERIVED exactly as `_prepared_verdict` derives it in
    # production, so this self-check tests the validator, not a stale literal.
    base["ingest"] = cos_judge_ingest.ingest_field(base, ctx)
    v = validate_verdict(base, ctx)
    assert not v, f"a conforming row raised {v}"

    # The validator must be PROVABLE ABLE TO FAIL — a check that only ever
    # passes is the vacuous-instrument shape this whole rebuild exists to remove.
    assert any(x["rule_id"] == "staging.held_reason_managed_set"
               for x in validate_verdict(
                   dict(base, disposition="held", held_reason="no-new-substance",
                        ingest={"relevant": False, "content": None}), ctx))
    # (INGEST-01) And the belt itself can fail: a row with no lane at all is
    # the f270700 shape and is refused, never defaulted.
    assert any(x["rule_id"] == "staging.ingest_independent"
               for x in validate_verdict(dict(base, ingest=None), ctx))
    assert any(x["rule_id"] == "triage.p0_never_noise"
               for x in validate_verdict(
                   dict(base, tier="P0", bucket="noise", summary=None),
                   dict(ctx, priority_map={"a@b.c": "P0"})))
    assert validate_run({"drafts": DRAFT_CAP + 1, "act_first": True})
    assert not validate_run({"drafts": DRAFT_CAP, "act_first": True})

    assert validate_run({"never_category_opens": 2, "drafts": 0, "act_first": True})
    assert _age_days("2020-01-01T00:00:00+00:00") > 1000

    html = compose_brief(run_id="x-run1", contract="PASS",
                         counters={"ingestion_in_scope": 3, "ingestion_candidates": 1,
                                   "ingestion_held": 2},
                         triage=[{"bucket": "act", "tier": "P1", "summary": ["a", "b"]}],
                         staged=[{"conversation_id": "c1", "category": "decision-record",
                                  "substance_kind": "decision", "dedup_kind": "create"}],
                         drafts=[], holds={"over-cap": 2},
                         metrics={"inbox_count": 3, "body_open_actual": 1},
                         notes=[], spans={"c1": "the board approved the budget"})
    bad = validate_brief(html, {"staged": 1})
    assert not bad, f"the composed brief fails its own rules: {bad}"
    assert validate_brief(html.replace(FIREWALL_OPEN, ""), {"staged": 1})

    assert len(RULES) == 51, f"{len(RULES)} rules registered, expected 51"
    print(f"cos_judge selfcheck: OK ({len(RULES)} rules)")
    return 0


# ---------------------------------------------------------------------------
# golden-set evaluation — moved to `cos_judge_golden_eval` (file-size ratchet,
# 2026-08-28) and re-exported here so `cos_judge.evaluate_golden` and every
# test that reads these off THIS module keep working unchanged.
# ---------------------------------------------------------------------------
from cos_judge_golden_eval import (  # noqa: E402,F401
    GOLDEN, _merge, _score_judgment, evaluate_golden)
