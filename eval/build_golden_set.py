#!/usr/bin/env python3
"""EVAL-01 — build + validate the bilingual EN/PT/ES golden set.

The golden set is the trustworthy measuring stick for the current-vs-new A/B
(EVAL-02) and the CI ship gate (EVAL-03). It is authored HERE as data so every
query, its language, its stratum, and its graded qrels are reviewable in one
place and the build is reproducible (`python3 eval/build_golden_set.py`).

This is a SYNTHETIC, public-safe golden set: every query and qrel references only
the synthetic sample vault shipped in `vault/` (example.com-style notes about the
brain engine itself). It exercises the same strata and scoring machinery as a
real deployment's golden set without embedding any private content.

Design constraints honoured (design v5 §5):
  * five strata + a held-out stratum, EN/PT/ES.
  * Graded qrels 0-3 keyed on the CANONICAL SOURCE PATH (the sample-vault
    relative path) — the one key both systems normalise to (eval/path_normalize).
  * Minimum n PER STRATUM and PER LANGUAGE. Strata that miss their floor are
    emitted with `power: "smoke"` so the scorecard downgrades their per-class
    numbers from ship-gate to smoke evidence (never silently green).
  * Explicit token-overlap rule for the cross-lingual "zero shared tokens"
    strata, WITH an entity/acronym/system-name whitelist (brain, ONNX, sqlite-vec,
    … are language-invariant identifiers and may overlap) — validated below.
  * TEMPORAL qrels carry path + span + effective_date + version_state so a
    retriever that surfaces the WRONG version does not score green. The harness
    version-resolves temporal hits.
  * Provenance + blind-grading protocol recorded per query: relevance is graded
    from NOTE IDENTITY/TOPIC, independent of what any retriever returned;
    `held_out` marks queries authored purely from domain knowledge.

All qrel paths are relative paths into the synthetic sample vault (`vault/`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# --- grading scale ---------------------------------------------------------
GRADING = {
    "3": "definitive — the note that directly and primarily answers the query",
    "2": "strong — contains the answer among other material",
    "1": "related/partial — on-topic, supports the answer but is not sufficient",
    "0": "not relevant (implicit: any note not listed scores 0)",
}

# --- minimum-n floors (per-stratum AND per-language) + power target --------
# Floors are the ship-gate threshold; below a floor a per-class result is SMOKE,
# not a gate. The power target is the n at which a per-class Recall@10 delta of
# ~10pp is detectable at the bootstrap CI used by the gate (rule of thumb for a
# paired bootstrap on a bounded [0,1] metric).
STRATUM_MIN_N = 8
LANG_MIN_N = {"EN": 20, "PT": 12, "ES": 8}
POWER_TARGET_PER_CLASS = 12  # n>=12 => per-class delta is gate-grade, else smoke

# --- token-overlap rule for cross-lingual strata ---------------------------
# After casefold + diacritic-strip + stopword removal, the CONTENT-word token
# sets of the query and the target note's title must be DISJOINT — EXCEPT for
# the entity/acronym/system-name whitelist, which is language-invariant and is
# expected to overlap. (Title is used as a cheap, reviewable proxy for topic;
# documented as such.)
ENTITY_WHITELIST = {
    "brain", "arctic", "e5", "gte", "embed", "embedding", "onnx", "sqlite",
    "vec", "fts5", "rerank", "reranker", "classification", "egress", "vault",
    "cli", "host", "vm", "bm25", "rrf", "semantic", "hybrid", "index",
    "benchmark", "profile", "recall", "it", "ai",
}
STOPWORDS = {
    # EN
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "what", "which", "who", "how", "did", "do", "does", "we",
    "our", "with", "about", "at", "by", "from", "this", "that", "it", "its",
    "be", "as", "has", "have", "had", "will", "between", "vs", "into", "over",
    # PT
    "o", "os", "as", "um", "uma", "de", "da", "do", "das", "dos", "em", "no",
    "na", "para", "por", "com", "que", "qual", "quais", "como", "foi", "foram",
    "sobre", "entre", "se", "e", "ou", "ao", "à", "nas", "nos", "pela", "pelo",
    "q'", "quando", "onde", "porque",
    # ES
    "el", "la", "los", "las", "un", "una", "lo", "y", "del", "al", "que",
    "cual", "cuales", "como", "fue", "fueron", "sobre", "entre", "se", "para",
    "por", "con", "en", "es", "son", "qué", "cuándo", "dónde", "cómo",
}


def _norm_tokens(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    toks = re.findall(r"[a-z0-9]+", t.casefold())
    return {w for w in toks if w not in STOPWORDS and len(w) > 1}


def _title_of(path: str) -> str:
    stem = Path(path).stem
    # strip leading date / Johnny-Decimal noise for a topic-ish title proxy
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[- ]?", "", stem)
    stem = re.sub(r"^\d{2}[. ]", "", stem)
    return stem.replace("-", " ").replace("_", " ")


# ===========================================================================
# THE GOLDEN SET — queries as data (SYNTHETIC; targets are notes in vault/).
# Each query: id, lang (query language), stratum, text, held_out, provenance,
# qrels: [{path, grade, [span, effective_date, version_state]}].
# target_lang is the dominant language of the relevant notes (for cross-lingual).
# ===========================================================================
Q: list[dict] = []


def add(**kw):
    Q.append(kw)


# ---- Stratum 1: cross_lingual EN <-> PT (zero shared content tokens) -------
add(id="xl_pt_01", lang="PT", target_lang="EN", stratum="cross_lingual_en_pt",
    held_out=True, provenance="domain-knowledge",
    text="Qual modelo vetorial foi escolhido para a pesquisa semântica?",
    qrels=[{"path": "brain/resources/arctic-embed-choice.md", "grade": 3},
           {"path": "raw/2026-06-27-arctic-vs-e5-benchmark.md", "grade": 1}])
add(id="xl_pt_02", lang="PT", target_lang="EN", stratum="cross_lingual_en_pt",
    held_out=False, provenance="past-query-log",
    text="Onde está a explicação do portão de sensibilidade das notas?",
    qrels=[{"path": "brain/resources/classification-gate.md", "grade": 3}])

# ---- Stratum 2: cross_lingual EN <-> ES (Spanish query; SMOKE-sized) --------
add(id="xl_es_01", lang="ES", target_lang="EN", stratum="cross_lingual_en_es",
    held_out=True, provenance="domain-knowledge",
    text="¿Cómo se separan las responsabilidades entre el anfitrión y la máquina aislada?",
    qrels=[{"path": "brain/resources/host-vm-trust-split.md", "grade": 3}])

# ---- Stratum 3: multi_hop (>=2 notes / 2-hop) -----------------------------
add(id="mh_01", lang="EN", stratum="multi_hop", held_out=True, provenance="domain-knowledge",
    text="Which embedding model was chosen and how does the benchmark support that choice?",
    qrels=[{"path": "brain/resources/arctic-embed-choice.md", "grade": 3},
           {"path": "raw/2026-06-27-arctic-vs-e5-benchmark.md", "grade": 3},
           {"path": "brain/projects/profile-a-build.md", "grade": 1}])
add(id="mh_02", lang="EN", stratum="multi_hop", held_out=False, provenance="past-query-log",
    text="How does the sensitivity gate relate to the host and isolated-VM trust split?",
    qrels=[{"path": "brain/resources/classification-gate.md", "grade": 3},
           {"path": "brain/resources/host-vm-trust-split.md", "grade": 3}])

# ---- Stratum 4: temporal / versioning (qrels keyed on path + version) -----
add(id="tmp_01", lang="EN", stratum="temporal", held_out=True, provenance="domain-knowledge",
    text="What is the latest arctic-vs-e5 benchmark?",
    qrels=[{"path": "raw/2026-06-27-arctic-vs-e5-benchmark.md",
            "grade": 3, "span": "arctic vs e5 benchmark", "effective_date": "2026-06-27",
            "version_state": "current"}])
add(id="tmp_02", lang="EN", stratum="temporal", held_out=False, provenance="past-query-log",
    text="What is the current state of the profile-a build project?",
    qrels=[{"path": "brain/projects/profile-a-build.md",
            "grade": 3, "span": "profile-a build current", "effective_date": "2026-06-27",
            "version_state": "current"}])

# ---- Stratum 5a: monolingual PT ------------------------------------------
add(id="pt_01", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="O que faz o motor do brain e a sua linha de comandos?",
    qrels=[{"path": "brain/resources/brain-engine.md", "grade": 3}])
add(id="pt_02", lang="PT", stratum="monolingual_pt", held_out=False, provenance="past-query-log",
    text="O que contém o índice de notas do brain?",
    qrels=[{"path": "brain/index.md", "grade": 3}])
add(id="pt_03", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="Como funciona o portão de sensibilidade das notas?",
    qrels=[{"path": "brain/resources/classification-gate.md", "grade": 3}])

# ---- Stratum 5b: monolingual ES (SMOKE-sized) -----------------------------
add(id="es_01", lang="ES", stratum="monolingual_es", held_out=True, provenance="domain-knowledge",
    text="¿Qué hace el motor brain y su interfaz de línea de comandos?",
    qrels=[{"path": "brain/resources/brain-engine.md", "grade": 3}])
add(id="es_02", lang="ES", stratum="monolingual_es", held_out=False, provenance="ragas-verified",
    text="¿Cómo se dividen las responsabilidades entre el anfitrión y la máquina aislada?",
    qrels=[{"path": "brain/resources/host-vm-trust-split.md", "grade": 3}])

# ---- Stratum 6: lexical / identifier (exact names, IDs, system names) -----
add(id="lex_01", lang="EN", stratum="lexical_identifier", held_out=False, provenance="past-query-log",
    text="arctic-embed", qrels=[{"path": "brain/resources/arctic-embed-choice.md", "grade": 3}])
add(id="lex_02", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="sqlite-vec", qrels=[{"path": "brain/resources/brain-engine.md", "grade": 2}])
add(id="lex_03", lang="EN", stratum="lexical_identifier", held_out=False, provenance="ragas-verified",
    text="classification gate", qrels=[{"path": "brain/resources/classification-gate.md", "grade": 3}])


# ===========================================================================
# Validation
# ===========================================================================
def validate(check_paths: bool) -> list[str]:
    errs: list[str] = []
    ids = [q["id"] for q in Q]
    if len(ids) != len(set(ids)):
        errs.append("duplicate query ids")
    # grades in range
    for q in Q:
        for r in q["qrels"]:
            if r["grade"] not in (0, 1, 2, 3):
                errs.append(f"{q['id']}: grade out of range {r['grade']}")
            if q["stratum"] == "temporal" and r["grade"] >= 2:
                for k in ("span", "effective_date", "version_state"):
                    if k not in r:
                        errs.append(f"{q['id']}: temporal qrel missing '{k}' (path-only "
                                    f"temporal qrel forbidden)")
    # token-overlap rule for the cross-lingual strata
    for q in Q:
        if not q["stratum"].startswith("cross_lingual"):
            continue
        qtok = _norm_tokens(q["text"]) - ENTITY_WHITELIST
        for r in q["qrels"]:
            if r["grade"] < 3:
                continue  # rule applies to the primary (definitive) target
            ttok = _norm_tokens(_title_of(r["path"])) - ENTITY_WHITELIST
            shared = qtok & ttok
            if shared:
                errs.append(f"{q['id']}: cross-lingual token overlap {sorted(shared)} "
                            f"between query and target title '{_title_of(r['path'])}' "
                            f"(must be disjoint ex-whitelist)")
    # path existence vs the sample vault
    if check_paths:
        vault = REPO / "vault"
        for q in Q:
            for r in q["qrels"]:
                if vault.exists() and not (vault / r["path"]).exists():
                    errs.append(f"{q['id']}: qrel path NOT in sample vault: {r['path']}")
    return errs


def stratum_table() -> dict:
    from collections import Counter
    by_strat = Counter(q["stratum"] for q in Q)
    by_lang = Counter(q["lang"] for q in Q)
    strata = {}
    for s, n in sorted(by_strat.items()):
        strata[s] = {"n": n, "min_n": STRATUM_MIN_N,
                     "power": "gate" if n >= POWER_TARGET_PER_CLASS
                     else ("smoke" if n < STRATUM_MIN_N else "marginal")}
    langs = {}
    for lng, n in sorted(by_lang.items()):
        floor = LANG_MIN_N.get(lng, 8)
        langs[lng] = {"n": n, "min_n": floor,
                      "power": "gate" if n >= floor else "smoke"}
    return {"strata": strata, "languages": langs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "golden_set.json"))
    ap.add_argument("--qrels-out", default=str(HERE / "qrels/qrels.json"))
    ap.add_argument("--no-check-paths", action="store_true")
    args = ap.parse_args()

    errs = validate(check_paths=not args.no_check_paths)
    table = stratum_table()
    doc = {
        "schema_version": "golden.synthetic.v1",
        "created": "2026-07-02",
        "session": "public-release",
        "canonical_key": "source_path (sample-vault relative path); both systems "
                         "normalise to this via eval/path_normalize.py",
        "grading_scale": GRADING,
        "minimum_n": {"per_stratum": STRATUM_MIN_N, "per_language": LANG_MIN_N,
                      "power_target_per_class": POWER_TARGET_PER_CLASS},
        "coverage": table,
        "token_overlap_rule": {
            "applies_to": ["cross_lingual_en_pt", "cross_lingual_en_es"],
            "rule": "casefold + strip-diacritics + drop-stopwords; CONTENT tokens of "
                    "query and target title must be DISJOINT except the entity/acronym/"
                    "system-name whitelist (language-invariant identifiers may overlap)",
            "entity_whitelist": sorted(ENTITY_WHITELIST),
            "note": "title used as a reviewable proxy for note topic",
        },
        "annotation_protocol": {
            "blind": "relevance graded from NOTE IDENTITY/TOPIC, independent of any "
                     "retriever's output (construct validity)",
            "annotators": ["annotator-1 (author)",
                           "second model (annotator-2, adjudicates a sample -> IAA)"],
            "inter_annotator_agreement": "computed by eval/iaa.py on the adjudicated "
                                         "sample; reported as Cohen's kappa in the scorecard",
            "held_out_stratum": "queries with held_out=true were authored purely from "
                                "domain knowledge, NOT derived from any retriever output "
                                "(guards against measuring incumbent habits)",
            "seed_mix": "synthetic past-query-style + generated-then-verified + "
                        "domain-knowledge held-out",
        },
        "queries": Q,
    }
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ranx-native qrels: {query_id: {doc_id: grade}}. doc_id is the canonical
    # source path; for temporal queries the version_state is appended so the
    # harness can score version-correctness (path#version).
    qrels_ranx: dict[str, dict[str, int]] = {}
    for q in Q:
        d: dict[str, int] = {}
        for r in q["qrels"]:
            key = r["path"]
            if q["stratum"] == "temporal" and "version_state" in r:
                key = f"{r['path']}#{r['version_state']}"
            d[key] = int(r["grade"])
        qrels_ranx[q["id"]] = d
    Path(args.qrels_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.qrels_out).write_text(json.dumps(qrels_ranx, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")

    print(f"queries: {len(Q)}")
    print(f"strata : {json.dumps(table['strata'])}")
    print(f"langs  : {json.dumps(table['languages'])}")
    print(f"wrote  : {args.out}")
    print(f"wrote  : {args.qrels_out}")
    if errs:
        print(f"\nVALIDATION FAILURES ({len(errs)}):")
        for e in errs:
            print("  -", e)
        return 1
    print("\nvalidation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
