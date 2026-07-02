#!/usr/bin/env python3
"""EF-02 (s01) — run the live brain retriever over the PT golden set to propose
candidate relevant notes for human adjudication, and emit the labeling-console
payload (``qrels_candidates.json`` + an inlined copy of the console HTML).

Console schema (per the s01 template):
  [{qid, query, lang, qclass, anchor:bool,
    candidates:[{note_id, title, snippet, machine:bool}]}]

  * machine=true  — the assisted labeler ASSERTED this note relevant
                    (author grade>=2 seed). These are the positive predictions
                    Cohen's kappa is computed against (machine:true vs Ricardo 'rel').
  * machine=false — shown for CONFIRMATION only: author grade-1 (borderline)
                    + the retriever's top-k that the author did not cite (lets
                    Ricardo catch relevance the labeler missed — recall side).

note_id == canonical source_path (embedder/chunking-invariant, H16/H17).

Run with the REAL embedder:
  BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_INDEX_DIR=<vault>/.brain \
  .venv-embed/bin/python eval/build_pt_candidates.py \
    --golden _evidence/s01/pt-golden-set.json \
    --vault _workspace/live-vault \
    --path-map _evidence/cutover-s10/path-map.json \
    --source-vault /path/to/your-vault \
    --console-template _evidence/s01/qrels-labeling-console.html \
    --candidates-out _evidence/s01/qrels_candidates.json \
    --console-out _evidence/s01/qrels-labeling-console.html \
    -k 8
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))
import path_normalize as pn  # noqa: E402

QCLASS = {
    "monolingual_pt": "mono-PT", "monolingual_en": "mono-EN", "monolingual_es": "mono-ES",
    "cross_lingual_en_pt": "xl-EN→PT", "cross_lingual_pt_en": "xl-PT→EN",
    "lexical_identifier": "lexical-id", "temporal": "temporal", "multi_hop": "multi-hop",
}


def snippet_for(src_root: Path, path: str, n: int = 160) -> str:
    fp = src_root / path
    if not fp.is_file():
        return ""
    txt = fp.read_text(encoding="utf-8", errors="replace")
    if txt.startswith("---"):
        parts = txt.split("---", 2)
        if len(parts) >= 3:
            txt = parts[2]
    for line in txt.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and not s.startswith("!["):
            return s[:n]
    return txt.strip()[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--path-map", required=True)
    ap.add_argument("--source-vault", required=True)
    ap.add_argument("--console-template", required=True)
    ap.add_argument("--candidates-out", required=True)
    ap.add_argument("--console-out", required=True)
    ap.add_argument("-k", type=int, default=8)
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    mapping = json.loads(Path(args.path_map).read_text(encoding="utf-8"))
    src_root = Path(args.source_vault)
    vault_root = str(Path(args.vault).resolve())

    from brain.core import BrainCore
    core = BrainCore(vault=vault_root)
    st = core.status().get("index", {})
    print(f"index: {st.get('notes')} notes, model={st.get('embed_model')}, backend={st.get('vector_backend')}")

    title_cache: dict[str, str] = {}

    def title_for(path: str) -> str:
        if path in title_cache:
            return title_cache[path]
        t = Path(path).stem
        title_cache[path] = t
        return t

    console_data = []
    retr_hit_at_any = 0  # queries where retriever surfaced >=1 author-relevant note in top-k
    for q in golden["queries"]:
        text = q["text"]
        hh = core.hybrid_search(text, k=args.k, rerank=True)
        retr_paths: list[str] = []
        for h in hh:
            raw = h.path
            rel = os.path.relpath(raw, vault_root) if os.path.isabs(raw) else raw
            sp = pn.normalize(rel, mapping)
            if sp not in retr_paths:
                retr_paths.append(sp)

        author_rel = {r["path"]: r["grade"] for r in q["qrels"]}
        # machine:true = author asserted relevant (grade>=2)
        cands = []
        added = set()
        for path, grade in sorted(author_rel.items(), key=lambda kv: -kv[1]):
            cands.append({"note_id": path, "title": title_for(path),
                          "snippet": snippet_for(src_root, path),
                          "machine": grade >= 2})
            added.add(path)
        # retriever proposals for confirmation (machine:false unless already asserted)
        for path in retr_paths:
            if path in added:
                continue
            cands.append({"note_id": path, "title": title_for(path),
                          "snippet": snippet_for(src_root, path), "machine": False})
            added.add(path)
        if any(p in retr_paths for p in author_rel):
            retr_hit_at_any += 1

        console_data.append({
            "qid": q["id"], "query": text, "lang": q["lang"],
            "qclass": QCLASS.get(q["stratum"], q["stratum"]),
            "anchor": bool(q.get("anchor", False)),
            "candidates": cands,
        })

    Path(args.candidates_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.candidates_out).write_text(
        json.dumps(console_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # inline into a copy of the console template. CRITICAL: the template binds
    # `DATA` and calls render() at parse-time of its first <script>, so the
    # payload MUST be set BEFORE that script runs — otherwise DATA falls back to
    # the SAMPLE and the console shows 3 fake rows instead of the real set.
    tpl = Path(args.console_template).read_text(encoding="utf-8")
    payload = ("<script>window.QRELS_CANDIDATES = "
               + json.dumps(console_data, ensure_ascii=False) + ";</script>\n")
    if "<script>" in tpl:
        html = tpl.replace("<script>", payload + "<script>", 1)  # before first app script
    elif "</head>" in tpl:
        html = tpl.replace("</head>", payload + "</head>", 1)
    else:
        html = payload + tpl
    Path(args.console_out).write_text(html, encoding="utf-8")

    print(f"queries: {len(console_data)}")
    print(f"retriever surfaced >=1 author-relevant note in top-{args.k}: "
          f"{retr_hit_at_any}/{len(console_data)}")
    print(f"wrote {args.candidates_out}")
    print(f"wrote {args.console_out} (window.QRELS_CANDIDATES inlined)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
