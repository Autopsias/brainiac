#!/usr/bin/env python3
"""EF-02 (s01) — assemble the expanded PT-majority golden set + qrels from the
four grounding-agent draft files, validating every qrel path against the
retrievable universe (the s10 path-map values) AND source-vault existence.

Canonical key = ``source_path`` (real your-vault relative path). The relevance
unit is the NOTE/DOCUMENT (a path), which is **embedder- and chunking-invariant**
(H16/H17) — it survives the s07 re-index and any embedder swap because no
chunk id or vector is baked into the qrel.

Honesty gates:
  * a query whose grade-3 (definitive) qrel path is not retrievable is DROPPED
    (never fabricate a match) — same discipline as build_live_golden_set.py.
  * grade 1/2 qrel paths that don't resolve are dropped from that query but the
    query is kept (it still has its definitive anchor).
  * exact-duplicate query strings are de-duplicated.

MNPI: the output carries real project/counterparty names + real paths, so it
MUST stay under a gitignored path (``_evidence/``). The committable manifest
(counts + methodology + id-only/opaque qrels + kappa) is written separately by
``eval/emit_pt_manifest.py``.

Usage:
  python3 eval/build_pt_golden_set.py \
    --drafts DRAFT1.json DRAFT2.json ... \
    --path-map _evidence/cutover-s10/path-map.json \
    --source-vault /path/to/your-vault \
    --out _evidence/s01/pt-golden-set.json \
    --qrels-out _evidence/s01/pt-qrels.json \
    --report _evidence/s01/pt-golden-set-report.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

GRADING_SCALE = {
    "3": "definitive — the note that directly and primarily answers the query",
    "2": "strong — contains the answer among other material",
    "1": "related/partial — on-topic, supports the answer but is not sufficient",
    "0": "not relevant (implicit: any note not listed scores 0)",
}
STRATA = {
    "monolingual_pt": "PT query, PT answer",
    "monolingual_en": "EN query, EN answer",
    "monolingual_es": "ES query, ES answer",
    "cross_lingual_en_pt": "EN query, PT answer (content tokens disjoint bar entities)",
    "cross_lingual_pt_en": "PT query, EN answer (content tokens disjoint bar entities)",
    "lexical_identifier": "exact identifier lookup (module/system/acronym/person/id)",
    "temporal": "point-in-time / version-disambiguating query",
    "multi_hop": "requires connecting 2+ notes/entities",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drafts", nargs="+", required=True)
    ap.add_argument("--path-map", required=True)
    ap.add_argument("--source-vault", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qrels-out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    pm = json.loads(Path(args.path_map).read_text(encoding="utf-8"))
    universe = set(pm.values())  # retrievable canonical source paths
    src = Path(args.source_vault)

    def resolvable(p: str) -> bool:
        return p in universe and (src / p).is_file()

    raw: list[dict] = []
    for d in args.drafts:
        data = json.loads(Path(d).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "queries" in data:
            data = data["queries"]
        raw.extend(data)

    kept: list[dict] = []
    dropped: list[dict] = []
    seen_text: set[str] = set()
    seq = 0
    for q in raw:
        text = (q.get("text") or "").strip()
        stratum = q.get("stratum")
        if not text or stratum not in STRATA:
            dropped.append({"id": q.get("id"), "reason": "missing text/invalid stratum"})
            continue
        norm = text.casefold()
        if norm in seen_text:
            dropped.append({"id": q.get("id"), "reason": "duplicate text", "text": text})
            continue
        qrels_in = q.get("qrels") or []
        g3 = [r for r in qrels_in if int(r.get("grade", 0)) == 3 and resolvable(r["path"])]
        if not g3:
            dropped.append({"id": q.get("id"), "reason": "no resolvable grade-3 qrel",
                            "text": text, "qrels": qrels_in})
            continue
        # keep first resolvable grade-3 + any resolvable grade 1/2 (dedup path)
        clean, paths = [], set()
        for r in qrels_in:
            p = r["path"]
            grade = int(r.get("grade", 0))
            if grade not in (1, 2, 3) or p in paths or not resolvable(p):
                continue
            clean.append({"path": p, "grade": grade})
            paths.add(p)
        if not any(r["grade"] == 3 for r in clean):  # keep exactly the resolvable g3
            clean.append({"path": g3[0]["path"], "grade": 3})
        seen_text.add(norm)
        seq += 1
        lang = q.get("lang", "EN")
        kept.append({
            "id": q.get("id") or f"q{seq:03d}",
            "lang": lang,
            "target_lang": q.get("target_lang", lang),
            "stratum": stratum,
            "anchor": bool(q.get("anchor", False)),
            "provenance": q.get("provenance", "grounded-domain-read"),
            "text": text,
            "qrels": sorted(clean, key=lambda r: -r["grade"]),
            "rationale": q.get("rationale", ""),
        })

    # ranx-native qrels {qid: {path: grade}}
    qrels_ranx = {q["id"]: {r["path"]: r["grade"] for r in q["qrels"]} for q in kept}

    by_stratum = Counter(q["stratum"] for q in kept)
    by_lang = Counter(q["lang"] for q in kept)
    pt_touch = sum(1 for q in kept if "PT" in (q["lang"], q["target_lang"]))
    anchors = [q["id"] for q in kept if q["anchor"]]

    doc = {
        "schema_version": "s01.pt-golden.v1",
        "created": "2026-07-01",
        "session": "s01",
        "item": "ef-02",
        "canonical_key": "source_path (real your-vault relative path); both retrievers normalise to this via eval/path_normalize.py + the s10 path-map",
        "relevance_unit": "NOTE/DOCUMENT (path) — embedder- AND chunking-INVARIANT (H16/H17); survives the s07 re-index and any embedder swap",
        "grading_scale": GRADING_SCALE,
        "strata": STRATA,
        "adjudication": {
            "status": "MACHINE-DRAFT — AWAITS Ricardo lock via _evidence/s01/qrels-labeling-console.html",
            "adjudicator": "Ricardo (PT-fluent, on-host, authorized-internal — satisfies H31: no raw MNPI leaves the Mac host)",
            "assisted_labeler": "Opus (this session) — drafts machine:true relevance; different agent from any downstream tuning/selection session",
            "protocol": "LABEL-FIRST (H34 barrier): Ricardo locks qrels BEFORE s04/s05 tune; anchors re-labeled blind for test-retest kappa; agreement = machine:true vs Ricardo 'rel' => Cohen's kappa per class (H15).",
        },
        "coverage": {
            "n": len(kept),
            "by_stratum": dict(sorted(by_stratum.items())),
            "by_query_lang": dict(sorted(by_lang.items())),
            "pt_touching": pt_touch,
            "pt_touching_share": round(pt_touch / max(1, len(kept)), 3),
            "anchors": anchors,
            "n_anchors": len(anchors),
        },
        "queries": kept,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.qrels_out).write_text(json.dumps(qrels_ranx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "drafts": args.drafts,
        "raw_total": len(raw),
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_detail": dropped,
        "coverage": doc["coverage"],
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"kept {len(kept)}/{len(raw)} queries (dropped {len(dropped)})")
    print(f"by stratum: {dict(sorted(by_stratum.items()))}")
    print(f"by query-lang: {dict(sorted(by_lang.items()))}  | PT-touching: {pt_touch} ({doc['coverage']['pt_touching_share']*100:.0f}%)")
    print(f"anchors: {len(anchors)}")
    print(f"wrote {args.out}\nwrote {args.qrels_out}\nwrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
