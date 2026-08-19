"""The brief renderer, the PNG writer and the golden-set evaluator of `cos_judge` (batch-2 drain).

Moved verbatim out of `cos_judge` and re-imported by it, so
`cos_judge.compose_brief`, `render_png`, `selfcheck` and `evaluate_golden`
keep their module path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_judge_rules import (  # noqa: E402
    BRIEF_ORDER, FIREWALL_CLOSE, FIREWALL_OPEN, JudgeStop, RULES, _age_days)
from cos_judge_rules_2 import (  # noqa: E402
    check_one, validate_brief, validate_run, validate_verdict)

CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
       'img-src \'self\' data:; style-src \'self\' \'unsafe-inline\'; '
       "font-src 'self' data:; script-src 'none'; base-uri 'none'; "
       'form-action \'none\'">')



# ---------------------------------------------------------------------------
# the morning brief
# ---------------------------------------------------------------------------
def _sect(name: str, body: str) -> str:
    return f"<h2>{escape(name)}</h2>\n{body or '<p>(none)</p>'}\n"


def compose_brief(*, run_id: str, contract: str, counters: dict[str, Any],
                  triage: list[dict[str, Any]], staged: list[dict[str, Any]],
                  drafts: list[dict[str, Any]], holds: dict[str, int],
                  metrics: dict[str, Any], notes: list[str],
                  spans: dict[str, str],
                  footer_notes: tuple[str, ...] = ()) -> str:
    """Phase 5's components, in Phase 5's order, with Phase 5's containment."""
    def rows(items, fmt):
        return ("<ul>" + "".join(f"<li>{fmt(i)}</li>" for i in items) + "</ul>"
                ) if items else ""

    top_reason = max(holds.items(), key=lambda kv: kv[1])[0] if holds else "none"
    staged_line = (f"{counters.get('ingestion_candidates', 0)} staged · "
                   f"{counters.get('ingestion_in_scope', 0)} in scope · "
                   f"{counters.get('ingestion_held', 0)} held ({top_reason})")
    act = [t for t in triage if t.get("bucket") == "act"]
    read = [t for t in triage if t.get("bucket") == "read"]
    noise = [t for t in triage if t.get("bucket") == "noise"]

    banner = (f"<p>OUTCOME CONTRACT: {contract} — profile full · enumerated "
              f"{counters.get('ingestion_in_scope', 0)} · archive:hold:drafted "
              f"0:{counters.get('ingestion_held', 0)}:{len(drafts)}</p>"
              + "".join(f"<p>{escape(n)}</p>" for n in notes))
    untriaged = counters.get("ingestion_in_scope", 0) - len(triage)
    tldr = rows(
        [f"{len(act)} thread(s) need you; {len(read)} worth your eyes; "
         f"{len(noise)} would archive — out of "
         f"{counters.get('ingestion_in_scope', 0)} enumerated"
         + (f", and {untriaged} this run could not triage at all "
            "(no sender or subject was persisted for them)." if untriaged
            else "."),
         f"{counters.get('ingestion_candidates', 0)} candidate(s) staged for the "
         "host's next batched question — nothing is decided here.",
         f"{len(drafts)} reply draft(s) written; none was placed in the mailbox "
         "by this run."],
        lambda s: escape(s))

    def staged_row(c):
        quote = spans.get(c["conversation_id"], "")
        return (f"<b>{escape(c.get('category') or 'uncategorised')}</b> · "
                f"{escape(c.get('substance_kind') or '')} · "
                f"{escape(c.get('dedup_kind') or 'create')}"
                + (f" → {escape(c.get('merge_candidate') or '')}"
                   if c.get("dedup_kind") == "merge_candidate" else "")
                + f"<br><span class=q>{escape(FIREWALL_OPEN)} {escape(quote)} "
                  f"{escape(FIREWALL_CLOSE)}</span>")

    required = (f"<p>{escape(staged_line)}</p>"
                + "<h3>ingestion</h3>" + (rows(staged, staged_row) or "<p>(none)</p>")
                + "<h3>attachment</h3><p>(none)</p>"
                + "<h3>supersede</h3><p>not visible from this leg — see the "
                  "host's inbox question</p>")

    read_block = (rows(read, lambda t: f"{escape(t.get('tier') or '')} · "
                       f"{escape((t.get('summary') or [''])[0])}")
                  + f"<p>Would archive ({len(noise)}): shadow — none archived; "
                    "this run has no mutation lane.</p>")
    ledger = ("<p>0 marked / 0 archived / 0 captured / "
              f"{len(drafts)} draft(s) written (not saved to the mailbox) / "
              f"{counters.get('ingestion_candidates', 0)} candidate(s) judged "
              "(not dropped: judgment-only run)</p>")
    strip = (f"<p>inbox_count {metrics.get('inbox_count')} · "
             f"body_open_actual {metrics.get('body_open_actual')} · "
             f"would_archive_count {len(noise)}</p>")

    body = (_sect("Banner", banner)
            + _sect("TL;DR", tldr)
            + _sect("TODAY", "")
            + _sect("DRAFTS READY", rows(
                drafts,
                lambda d: f"{escape(d.get('recipient') or 'original thread')} · "
                f"RE: {escape(d.get('subject') or '')} · "
                f"{escape((d.get('draft') or {}).get('form') or '')}<br>"
                f"<span class=q>{escape((d.get('summary') or ['', ''])[1])}</span>"))
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
    v = validate_verdict(base, ctx)
    assert not v, f"a conforming row raised {v}"

    # The validator must be PROVABLE ABLE TO FAIL — a check that only ever
    # passes is the vacuous-instrument shape this whole rebuild exists to remove.
    assert any(x["rule_id"] == "staging.held_reason_managed_set"
               for x in validate_verdict(dict(base, disposition="held",
                                              held_reason="no-new-substance"), ctx))
    assert any(x["rule_id"] == "triage.p0_never_noise"
               for x in validate_verdict(
                   dict(base, tier="P0", bucket="noise", summary=None),
                   dict(ctx, priority_map={"a@b.c": "P0"})))
    assert validate_run({"drafts": 11, "act_first": True})
    assert not validate_run({"drafts": 10, "act_first": True})

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

    assert len(RULES) == 48, f"{len(RULES)} rules registered, expected 48"
    print(f"cos_judge selfcheck: OK ({len(RULES)} rules)")
    return 0


# ---------------------------------------------------------------------------
# golden-set evaluation
# ---------------------------------------------------------------------------
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
