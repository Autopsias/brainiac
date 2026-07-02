#!/usr/bin/env python3
"""s01 console rebuild (Ricardo directive) — inline the REAL anchor-only
candidate data (with actual relevant-passage snippets + full note text, from
``build_anchor_snippets.py``) into the v2 labeling console template
(``qrels_console_v2_template.html``, MNPI-free, committed under ``eval/``).

Fixes the two verified usability defects of the v1 console
(``_evidence/s01/qrels-labeling-console.html``):
  1. Snippets were the note's first line (a header) — nothing to judge
     relevance on. v2 shows the actual highest-similarity chunk.
  2. Volume — 102 queries x ~9 candidates ~= 887 decisions dumped at once, when
     the intent (H15) was to carefully adjudicate only the 15 anchor queries.
     v2 shows ONLY the anchors; the other 87 stay machine-drafted/directional.

Also builds a lightweight "add a missed note" typeahead picker from every
unique note_id/title seen across the FULL 102-query candidate pool (not just
the anchors) — gives Ricardo a broader net to catch a retriever miss without
requiring a fresh vault scan.

The output HTML is MNPI-bearing (real query text, paths, note contents) and
MUST be written under gitignored ``_evidence/`` — never committed.

Usage:
  python3 eval/build_qrels_console_v2.py \\
    --anchor-candidates _evidence/s01/anchor_candidates_v2.json \\
    --all-candidates _evidence/s01/qrels_candidates.json \\
    --template eval/qrels_console_v2_template.html \\
    --out _evidence/s01/qrels-labeling-console-v2.html
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--anchor-candidates", required=True,
                    help="output of build_anchor_snippets.py (15 anchors, real snippets + full_text)")
    ap.add_argument("--all-candidates", required=True,
                    help="qrels_candidates.json (all 102 queries) — source for the add-missed-note picker only")
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    anchor_data = json.loads(Path(args.anchor_candidates).read_text(encoding="utf-8"))
    if len(anchor_data) != 15:
        print(f"WARNING: expected 15 anchor queries, got {len(anchor_data)}")

    all_cands = json.loads(Path(args.all_candidates).read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    for q in all_cands:
        for c in q["candidates"]:
            seen.setdefault(c["note_id"], c.get("title") or Path(c["note_id"]).stem)
    picker = [{"note_id": nid, "title": title} for nid, title in sorted(seen.items())]

    tpl = Path(args.template).read_text(encoding="utf-8")
    payload = (
        "<script>window.QRELS_CANDIDATES = "
        + json.dumps(anchor_data, ensure_ascii=False)
        + ";\nwindow.QRELS_NOTE_PICKER = "
        + json.dumps(picker, ensure_ascii=False)
        + ";</script>\n"
    )
    # CRITICAL (same gotcha as build_pt_candidates.py): the template binds DATA
    # and calls render() at parse-time of its FIRST <script> tag, so the
    # payload must land BEFORE that script — otherwise DATA/PICKER silently
    # fall back to the harmless SAMPLE and the console shows fake rows.
    if "<script>" in tpl:
        html = tpl.replace("<script>", payload + "<script>", 1)
    elif "</head>" in tpl:
        html = tpl.replace("</head>", payload + "</head>", 1)
    else:
        html = payload + tpl

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(html, encoding="utf-8")

    n_cands = sum(len(a["candidates"]) for a in anchor_data)
    print(f"anchor queries: {len(anchor_data)}  candidates: {n_cands}  picker entries: {len(picker)}")
    print(f"wrote {args.out} (window.QRELS_CANDIDATES + window.QRELS_NOTE_PICKER inlined)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
