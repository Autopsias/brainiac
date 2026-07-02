#!/usr/bin/env python3
"""EF-02 (s01) — emit the COMMITTABLE, EGRESS-SAFE manifest for the PT golden set.

The full golden set + qrels + labeling console are MNPI-bearing (real
counterparty names, real paths, real query text) and live under gitignored
``_evidence/``. This manifest is the ONLY committed artifact: it carries
counts by language + class, the seeded split acceptance, methodology, per-class
Cohen's kappa (once Ricardo locks), and **id-only qrels** — every note is an
opaque hash of its source path (no path, no title, no snippet, no query text),
so nothing MNPI is disclosed. (Egress-safe by construction: figures/verdicts,
no raw MNPI — mirrors docs/operations/final-eval-comparison_2026-07-01.md.)

Usage:
  python3 eval/emit_pt_manifest.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    [--adjudicated _evidence/s01/qrels_adjudicated.json] \
    --out docs/eval-bench/pt-golden-set.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def oid(path: str) -> str:
    return "n" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]


def kappa(labels_a: list[int], labels_b: list[int]) -> float | None:
    """Cohen's kappa for two binary label vectors (1=relevant, 0=not)."""
    n = len(labels_a)
    if n == 0:
        return None
    po = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
    pa1 = sum(labels_a) / n
    pb1 = sum(labels_b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe == 1.0:
        return 1.0
    return round((po - pe) / (1 - pe), 3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--candidates", default=None,
                    help="qrels_candidates.json — needed to compute machine:true predictions for kappa")
    ap.add_argument("--adjudicated", default=None,
                    help="qrels_adjudicated.json (present => compute kappa)")
    ap.add_argument("--dual-stats", default=None,
                    help="dual-model agreement stats JSON from merge_dual_model_qrels.py "
                         "(present => dual-model adjudication mode: trusted tier = "
                         "dual-model agreed, kappa = Claude-vs-Codex)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    g = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    split = json.loads(Path(args.split).read_text(encoding="utf-8"))
    qs = g["queries"]

    by_stratum = Counter(q["stratum"] for q in qs)
    by_lang = Counter(q["lang"] for q in qs)
    by_target = Counter(q["target_lang"] for q in qs)
    pt_touch = sum(1 for q in qs if "PT" in (q["lang"], q["target_lang"]))
    anchors = [q["id"] for q in qs if q.get("anchor")]
    anchor_set = set(anchors)

    # id-only qrels (opaque)
    idqrels = {q["id"]: {oid(r["path"]): r["grade"] for r in q["qrels"]} for q in qs}

    rel: dict = {}
    adj_meta: dict = {}
    if args.adjudicated and Path(args.adjudicated).is_file():
        adj_probe = json.loads(Path(args.adjudicated).read_text(encoding="utf-8"))
        rel = adj_probe.get("qrels", {})
        adj_meta = {k: v for k, v in adj_probe.items() if k != "qrels"}

    dual_stats: dict = {}
    if args.dual_stats and Path(args.dual_stats).is_file():
        dual_stats = json.loads(Path(args.dual_stats).read_text(encoding="utf-8"))

    if dual_stats:
        # Dual-model trust model (Ricardo directive, s01 adjudication session):
        # EVERY query (all 102) was adjudicated pair-by-pair by TWO model
        # families — Claude (labeler) and Codex/GPT (independent blind
        # verifier, per H15: verifier from a different family). Trust is
        # per-qid on the OUTCOME:
        #   dual-locked   = query has >=1 note both models agreed is relevant
        #   dual-zero-rel = both models adjudicated it but agreed on NO
        #                   relevant note -> unusable for Recall@k until the
        #                   excluded pairs are resolved (human or re-judge)
        trust = {
            q["id"]: ("dual-locked" if rel.get(q["id"]) else "dual-zero-rel")
            for q in qs
        }
        n_trusted = sum(1 for v in trust.values() if v == "dual-locked")
    else:
        # Two-tier trust model (Ricardo decision, s01 console rebuild): ONLY the 15
        # anchor queries are ever human-adjudicated (the labeling console shows
        # anchors only — the other 87 keep their machine-drafted qrels and are
        # never rendered for adjudication). So trust is per-qid, not per-corpus:
        #   trusted-anchor  = an anchor query with a human decision in `rel`
        #   pending-anchor  = an anchor query not yet adjudicated
        #   directional     = a non-anchor query (machine-drafted only, by design)
        trust = {
            q["id"]: (
                "trusted-anchor" if q["id"] in anchor_set and q["id"] in rel
                else "pending-anchor" if q["id"] in anchor_set
                else "directional"
            )
            for q in qs
        }
        n_trusted = sum(1 for v in trust.values() if v == "trusted-anchor")

    # kappa (per class) — computed ONLY over anchor-query decisions, because
    # those are the only ones with a real human label. Folding non-anchor
    # (never-adjudicated) qids into this table would silently score every one
    # of their notes h=0 and manufacture a fake kappa — the exact bug this
    # rebuild fixes for snippets, so the same discipline applies to metrics.
    kappa_block = ("**Status: PENDING** — awaiting Ricardo's adjudication of the "
                   f"{len(anchors)} anchor queries via "
                   "`_evidence/s01/qrels-labeling-console-v2.html` (anchors-only console).\n")
    if dual_stats:
        ds = dual_stats
        n_pairs = ds["n_pairs"]
        pct = lambda x: f"{100 * x / max(1, n_pairs):.1f}%"  # noqa: E731
        lines = ["| class | n(pairs, both definite) | Cohen's κ (Claude vs Codex) |",
                 "|---|--:|--:|"]
        for cls, v in sorted(ds["kappa_per_class"].items()):
            lines.append(f"| {cls} | {v['n']} | {v['kappa'] if v['kappa'] is not None else 'n/a'} |")
        lines.append(f"| **overall** | {ds['kappa_base_pairs']} | {ds['kappa_overall']} |")
        kappa_block = (
            f"**Status: LOCKED (dual-model)** — all {ds['n_queries']} queries × candidates "
            f"({n_pairs} pairs) adjudicated by TWO model families at {ds['generated_at']}: "
            "Claude (Opus-class, labeler — judged on query + relevance-bearing snippet, "
            "escalating to the full note when the snippet was insufficient) and Codex/GPT "
            "(independent verifier, blind to Claude's labels; two-round: snippet, then enriched "
            "note passages for pairs not locked in round 1). Per H15 the "
            "verifier is a DIFFERENT model family than the labeler. κ is computed over "
            f"pairs where BOTH gave a definite label (rel/notrel): {ds['kappa_base_pairs']}"
            f"/{n_pairs} = {pct(ds['kappa_base_pairs'])} of pairs.\n\n"
            + "\n".join(lines) + "\n\n"
            f"**Outcome split:** locked-rel {ds['n_locked_rel']} ({pct(ds['n_locked_rel'])}) · "
            f"locked-notrel {ds['n_locked_notrel']} ({pct(ds['n_locked_notrel'])}) · "
            f"**excluded (disagreement or either-unsure) {ds['n_excluded']} ({pct(ds['n_excluded'])})** — "
            "excluded pairs are OUT of the trusted core (state `unsure`), preserved in "
            "`_evidence/s01/disagreements.json` for optional later human review. "
            f"Codex-unavailable pairs: {ds['n_codex_unavailable']}.\n\n"
            f"**Zero-locked-rel queries: {ds['n_queries'] - ds['n_queries_with_locked_rel']}"
            f"/{ds['n_queries']}** — these queries have NO note both models agreed is relevant "
            "and are unusable for Recall@k until resolved.\n"
        )
    elif args.adjudicated and args.candidates and Path(args.adjudicated).is_file():
        cand = {c["qid"]: c for c in json.loads(Path(args.candidates).read_text(encoding="utf-8"))}
        adj = json.loads(Path(args.adjudicated).read_text(encoding="utf-8"))
        rel = adj.get("qrels", {})  # {qid:{note_id:1}} — only qids Ricardo actually touched
        per_class: dict[str, list[tuple[int, int]]] = defaultdict(list)
        stratum_of = {q["id"]: q["stratum"] for q in qs}
        adjudicated_anchor_qids = [qid for qid in anchors if qid in rel]
        for qid in adjudicated_anchor_qids:
            c = cand.get(qid)
            if not c:
                continue
            human_rel = set(rel.get(qid, {}).keys())
            for note in c["candidates"]:
                nid = note["note_id"]
                m = 1 if note.get("machine") else 0
                h = 1 if nid in human_rel else 0
                per_class[stratum_of.get(qid, "?")].append((m, h))
        if not adjudicated_anchor_qids:
            kappa_block = ("**Status: PENDING** — `--adjudicated` file present but contains no "
                           f"anchor-qid decisions (expected qids: {', '.join(anchors)}).\n")
        else:
            lines = ["| class | n(note-decisions) | Cohen's κ (machine:true vs Ricardo 'rel') |",
                     "|---|--:|--:|"]
            allm, allh = [], []
            for cls in sorted(per_class):
                pairs = per_class[cls]
                ma = [p[0] for p in pairs]
                ha = [p[1] for p in pairs]
                allm += ma; allh += ha
                k = kappa(ma, ha)
                lines.append(f"| {cls} | {len(pairs)} | {k if k is not None else 'n/a'} |")
            lines.append(f"| **overall** | {len(allm)} | {kappa(allm, allh)} |")
            status_word = "LOCKED" if len(adjudicated_anchor_qids) == len(anchors) else "PARTIAL"
            kappa_block = (
                f"**Status: {status_word}** — {len(adjudicated_anchor_qids)}/{len(anchors)} anchor "
                f"queries adjudicated by {adj.get('adjudicator','Ricardo')} at "
                f"{adj.get('generated_at','?')}. Computed over **anchor-query decisions ONLY** — "
                "the other 87 queries are never adjudicated by design (anchors-only console) and "
                "their machine-drafted qrels remain DIRECTIONAL, not evaluated for agreement here.\n\n"
                + "\n".join(lines) + "\n"
            )

    def counter_md(c: Counter) -> str:
        return " · ".join(f"{k}={v}" for k, v in sorted(c.items()))

    fs = split["fold_stats"]
    acc = split["acceptance"]

    if dual_stats:
        status_line = (
            f"dual-model-locked ({n_trusted}/{len(qs)} queries with >=1 locked-rel; "
            f"kappa={dual_stats['kappa_overall']}; no human pass)"
        )
    elif n_trusted == len(anchors) and anchors:
        status_line = f"anchors-locked ({n_trusted}/{len(anchors)} trusted; full set directional)"
    elif n_trusted > 0:
        status_line = f"anchors-partial ({n_trusted}/{len(anchors)} trusted; full set directional)"
    else:
        status_line = "machine-draft (awaiting anchor adjudication; full set directional)"

    # --- conditional prose blocks (dual-model vs anchors-only human model) ---
    if dual_stats:
        anchor_line = (
            f"- **Adjudication coverage:** ALL {len(qs)} queries (× ~9 candidates = "
            f"{dual_stats['n_pairs']} pairs) dual-model adjudicated — **{n_trusted}/{len(qs)} "
            f"queries locked with ≥1 agreed-relevant note**; "
            f"{len(qs) - n_trusted} have zero locked-rel (unusable for Recall@k until resolved). "
            f"The {len(anchors)} anchor queries carry no special trust status under this model."
        )
        adjudication_md = (
            "- **Adjudication (H15 / H31-amended — dual-model, NO human pass):** every "
            "(query, candidate) pair was independently judged by TWO model families — "
            "**Claude (Opus-class)** as labeler (query + highest-similarity real snippet, "
            "escalating to the full note text on disk when the snippet was insufficient) and "
            "**Codex/GPT (codex-cli)** as verifier, **blind** to Claude's labels (strict-JSON "
            "3-way verdicts: rel / notrel / unsure). Codex judged in two rounds: round 1 on the "
            "same snippet; round 2 re-judged only the pairs NOT locked by round-1 agreement, with "
            "richer evidence (the snippet plus the note's best query-term passages) — still blind "
            "to Claude's labels, mirroring the full-note escalation the Claude side used. "
            "Agreement rule: both-rel → locked rel; both-notrel → locked notrel; ANY other "
            "combination (incl. unsure) → excluded from the trusted core.\n"
            "  - **H15 satisfied:** the verifier is a different model family than the labeler "
            "(Claude vs GPT).\n"
            "  - **No human labeling:** Ricardo explicitly redirected adjudication to the two "
            "model families; human test–retest κ is **n/a** (no human pass exists). The "
            "reported κ is **inter-model** (Claude-vs-Codex) agreement.\n"
            "  - **H31 (amended):** both Claude and Codex are PRE-AUTHORIZED clients of this "
            "vault (dual-client model on record) — the Codex pass added **no new egress "
            "surface**; raw content still never left the two already-authorized processors, "
            "and only this opaque manifest is committed."
        )
        tiers_md = f"""## Trust tiers (dual-model qrels — Ricardo directive, s01 adjudication)

Adjudication covered **all {len(qs)} queries** (no anchors-only restriction): the
trusted tier is **"dual-model agreed"**, not "human-locked". A note is in the
trusted core iff BOTH model families independently called it relevant.

| tier | meaning | n queries |
|---|---|--:|
| **dual-locked** | ≥1 candidate note agreed relevant by both Claude and Codex — trusted core for Recall@k / precision | {n_trusted} |
| **dual-zero-rel** | adjudicated, but NO note agreed relevant — unusable for Recall@k until the excluded pairs are resolved (optional human review of `disagreements.json`) | {len(qs) - n_trusted} |

**The author-drafted grades (id-only qrels below) remain DIRECTIONAL** — they
are the assisted-labeler's own draft, kept for provenance. The trusted surface
is the dual-model locked set (second JSON block below)."""
        kappa_caveat_md = (
            "> The reported κ is **inter-model agreement (Claude-vs-Codex)** — two independent\n"
            "> judges from different model families (H15), each blind to the other. There is\n"
            "> **no human adjudication pass** on this set (Ricardo's explicit redirect), so\n"
            "> human–machine κ and human test–retest κ are **n/a**. Excluded pairs are\n"
            "> reported above, never silently folded into either class."
        )
        trust_vocab = "`dual-locked` / `dual-zero-rel`"
        locked_idqrels = {
            qid: {oid(nid): 1 for nid in sorted(notes)}
            for qid, notes in sorted(rel.items())
        }
        locked_block = f"""
### Dual-model LOCKED qrels (trusted core, opaque)
Same opaque id scheme. Only notes BOTH models agreed are relevant (grade 1 =
locked-rel). Queries absent here are `dual-zero-rel`.

```json
{json.dumps(locked_idqrels, ensure_ascii=False, indent=1)}
```
"""
        repro_extra = """# 8. DUAL-MODEL adjudication (Ricardo directive — replaces the human console pass)
# 8a. real snippets for ALL 102 queries (same chunker/embedder as the index)
BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_MODEL_CACHE=.fastembed_cache \\
  .venv-embed/bin/python eval/build_anchor_snippets.py --all \\
  --golden _evidence/s01/pt-golden-set.json \\
  --candidates _evidence/s01/qrels_candidates.json \\
  --source-vault /path/to/your-vault \\
  --out _evidence/s01/all_candidates_snippets.json
# 8b. Claude labels every pair (in-session; judgments_claude.json)
# 8c. Codex verifies every pair blind (codex exec, read-only sandbox); round 2
#     re-judges not-yet-locked pairs with enriched note passages (still blind)
python3 eval/codex_judge.py --snippets _evidence/s01/all_candidates_snippets.json \\
  --out _evidence/s01/judgments_codex.json
python3 eval/codex_judge.py --snippets _evidence/s01/_snippets_round2.json \\
  --out _evidence/s01/_judgments_codex_r2.json --batch 6 --max-snippet-chars 2400
# 8d. merge under the agreement rule + Cohen's kappa
python3 eval/merge_dual_model_qrels.py \\
  --claude _evidence/s01/judgments_claude.json \\
  --codex _evidence/s01/judgments_codex.json \\
  --snippets _evidence/s01/all_candidates_snippets.json \\
  --golden _evidence/s01/pt-golden-set.json \\
  --out-qrels _evidence/s01/qrels_adjudicated.json \\
  --out-disagreements _evidence/s01/disagreements.json \\
  --out-stats _evidence/s01/dual_model_stats.json
# 8e. re-emit this manifest in dual-model mode
python3 eval/emit_pt_manifest.py \\
  --golden _evidence/s01/pt-golden-set.json --split _evidence/s01/pt-split.json \\
  --candidates _evidence/s01/qrels_candidates.json \\
  --adjudicated _evidence/s01/qrels_adjudicated.json \\
  --dual-stats _evidence/s01/dual_model_stats.json \\
  --out docs/eval-bench/pt-golden-set.md
"""
    else:
        anchor_line = (
            f"- **Anchor queries** (re-labeled blind for test–retest κ): {len(anchors)} — "
            f"**{n_trusted}/{len(anchors)} human-locked (trusted)**, "
            f"{len(anchors) - n_trusted} pending adjudication."
        )
        adjudication_md = (
            f"- **Adjudication (H15/H31):** {g['adjudication']['adjudicator']}. "
            f"{g['adjudication']['protocol']}\n"
            f"  - Assisted labeler: {g['adjudication']['assisted_labeler']}.\n"
            "  - **H31:** no raw MNPI leaves the Mac host — Ricardo adjudicates on-host in the "
            "browser console; only this opaque manifest is committed."
        )
        tiers_md = f"""## Trust tiers (two-tier qrels model — Ricardo decision, s01 console rebuild)

The labeling console (`_evidence/s01/qrels-labeling-console-v2.html`) shows
**only the {len(anchors)} anchor queries** — the other {len(qs) - len(anchors)} keep their
original machine-drafted (assisted-labeler) qrels and are **never rendered for
human adjudication**. This is by design (volume: {len(qs)} queries × ~9
candidates ≈ 900 decisions was unworkable; the anchors are the trusted core).

| tier | meaning | n queries |
|---|---|--:|
| **trusted-anchor** | anchor query, human-adjudicated by Ricardo — treat as ground truth | {n_trusted} |
| **pending-anchor** | anchor query, not yet adjudicated — will become trusted-anchor once labeled | {len(anchors) - n_trusted} |
| **directional** | non-anchor query — machine-drafted qrels only, never human-verified; use for directional signal, NOT for precision/recall claims | {len(qs) - len(anchors)} |

**Any metric computed over the full {len(qs)}-query set (not anchor-filtered)
is DIRECTIONAL until further adjudication** — it validates against the
assisted labeler's own drafted grades, not an independent human. Anchor-only
metrics (the κ table below, and any anchor-filtered precision/recall computed
downstream) are the trustworthy numbers."""
        kappa_caveat_md = (
            f"> With a single human adjudicator (Ricardo), the reported agreement is\n"
            "> **human(Ricardo)-vs-assisted-labeler κ per class** (validates the LLM-drafted\n"
            f"> labels against ground truth), plus a **test–retest** on the {len(anchors)}\n"
            "> anchor queries for intra-annotator consistency. The two-human double-label is\n"
            "> not applicable with one authorized on-host adjudicator."
        )
        trust_vocab = "`trusted-anchor` / `pending-anchor` / `directional`"
        locked_block = ""
        repro_extra = ""

    md = f"""---
type: eval-bench
item: ef-02
created: 2026-07-01
status: {status_line}
egress: safe (counts + opaque id-only qrels; no paths, titles, snippets, or query text)
---

# PT-majority retrieval golden set — manifest

Expanded, Portuguese-heavy golden set + qrels built against the **live migrated
corpus** ({split['n_total']} queries) so PT retrieval is measured honestly
instead of on the 12 queries that all scored 0.000 in the cutover eval. The
full set (query text, real paths, snippets) is MNPI-bearing and stays under
gitignored `_evidence/s01/`; this manifest is the committed, egress-safe view.

## Relevance unit (H16/H17)
`{g['canonical_key']}`
**{g['relevance_unit']}** — no chunk id or vector is baked into any qrel, so
the set survives the s07 re-index and any embedder swap unchanged.

## Coverage
- **Total queries:** {len(qs)}
- **By query class (stratum):** {counter_md(by_stratum)}
- **By query language:** {counter_md(by_lang)}
- **By answer (target) language:** {counter_md(by_target)}
- **PT-touching queries** (query OR answer in PT): **{pt_touch} / {len(qs)} = {round(100*pt_touch/max(1,len(qs)))}%** → PT is the majority.
{anchor_line}

## Seeded stratified 4-way split (H34/H36/H37)
Seed `{split['seed']}`, stratified by {split['stratified_by']}. Folds:

| fold | purpose | n | PT-touching | by stratum |
|---|---|--:|--:|---|
| train | {split['fold_purpose']['train']} | {fs['train']['n']} | {fs['train']['pt_touching']} | {counter_md(Counter(fs['train']['by_stratum']))} |
| dev | {split['fold_purpose']['dev']} | {fs['dev']['n']} | {fs['dev']['pt_touching']} | {counter_md(Counter(fs['dev']['by_stratum']))} |
| adoption-validation | {split['fold_purpose']['adoption-validation']} | {fs['adoption-validation']['n']} | {fs['adoption-validation']['pt_touching']} | {counter_md(Counter(fs['adoption-validation']['by_stratum']))} |
| held-out | {split['fold_purpose']['held-out']} | {fs['held-out']['n']} | {fs['held-out']['pt_touching']} | {counter_md(Counter(fs['held-out']['by_stratum']))} |

**Barrier:** {split['barrier']}

**Acceptance:**
""" + "\n".join(
        f"- {'✅ PASS' if v['pass'] else '❌ FAIL'} `{k}` = {v['value']}"
        for k, v in acc.items()
    ) + f"""

> If any floor FAILs, per H18 the aggregate PT slice is still reported but fine
> language×class slices are marked **exploratory** (not gate-power).

## Methodology
- **Grounded authoring:** every query was written after reading a real note whose content answers it; the definitive note is graded 3. Query classes: {', '.join(sorted(g['strata']))}.
- **Grading scale:** 3 definitive · 2 strong · 1 partial · 0 (implicit) not relevant.
- **Candidate proposal:** the live brain retriever (hybrid + rerank, real `multilingual-e5-small` embedder) proposed top-k notes per query; author-asserted relevant notes are `machine:true` (the assisted-labeler positives), retriever extras are shown `machine:false` for confirmation so Ricardo can add missed relevance.
- **Honesty gate:** any query whose definitive note is not in the retrievable corpus was dropped (never fabricate a match).
{adjudication_md}

{tiers_md}

## Inter-annotator agreement (per class Cohen's κ)
{kappa_block}
{kappa_caveat_md}

## id-only qrels (opaque) + trust tier per qid
Every note id below is `n<sha1(source_path)[:10]>` — reversible only with the
gitignored path map. Format: `qid → {{opaque_note_id: grade}}`. The trust map
({trust_vocab}) is the tier model from the section above, keyed by the same `qid`.

```json
{json.dumps(idqrels, ensure_ascii=False, indent=1)}
```

```json
{json.dumps({"trust": trust}, ensure_ascii=False, indent=1)}
```
{locked_block}
## Reproduce (on-host, real embedder)
```bash
# 1. assemble + validate (drops unresolvable definitive qrels)
python3 eval/build_pt_golden_set.py --drafts <draft*.json> \\
  --path-map _evidence/cutover-s10/path-map.json \\
  --source-vault /path/to/your-vault \\
  --out _evidence/s01/pt-golden-set.json --qrels-out _evidence/s01/pt-qrels.json \\
  --report _evidence/s01/pt-golden-set-report.json
# 2. seeded stratified 4-way split
python3 eval/make_pt_split.py --golden _evidence/s01/pt-golden-set.json \\
  --out _evidence/s01/pt-split.json --seed {split['seed']}
# 3. retriever candidates for the FULL 102-query set (machine-drafted; directional)
BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_INDEX_DIR=_workspace/live-vault/.brain \\
  .venv-embed/bin/python eval/build_pt_candidates.py \\
  --golden _evidence/s01/pt-golden-set.json --vault _workspace/live-vault \\
  --path-map _evidence/cutover-s10/path-map.json \\
  --source-vault /path/to/your-vault \\
  --console-template _evidence/s01/qrels-labeling-console.html \\
  --candidates-out _evidence/s01/qrels_candidates.json \\
  --console-out _evidence/s01/qrels-labeling-console.html -k 8
# 4. REAL relevant-passage snippets for the 15 ANCHOR queries only (REAL embedder;
#    chunk-level cosine match against the live brain chunker/contextual-prefix scheme)
BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_MODEL_CACHE=.fastembed_cache \\
  .venv-embed/bin/python eval/build_anchor_snippets.py \\
  --golden _evidence/s01/pt-golden-set.json \\
  --candidates _evidence/s01/qrels_candidates.json \\
  --source-vault /path/to/your-vault \\
  --out _evidence/s01/anchor_candidates_v2.json
# 5. rebuild the anchors-only console (v2 — fixes the snippet + volume defects)
python3 eval/build_qrels_console_v2.py \\
  --anchor-candidates _evidence/s01/anchor_candidates_v2.json \\
  --all-candidates _evidence/s01/qrels_candidates.json \\
  --template eval/qrels_console_v2_template.html \\
  --out _evidence/s01/qrels-labeling-console-v2.html
# 6. (Ricardo) open qrels-labeling-console-v2.html, adjudicate the 15 anchors,
#    export qrels_adjudicated.json (Export button)
# 7. re-emit this manifest with --adjudicated to lock the anchor tier + fill in per-class κ
python3 eval/emit_pt_manifest.py \\
  --golden _evidence/s01/pt-golden-set.json --split _evidence/s01/pt-split.json \\
  --candidates _evidence/s01/qrels_candidates.json \\
  --adjudicated _evidence/s01/qrels_adjudicated.json \\
  --out docs/eval-bench/pt-golden-set.md
{repro_extra}```

**Trust note:** see § Trust tiers for which qrels surface is trusted vs
directional under the current adjudication model.

Reused downstream by ef-03, ef-04, pt-01/02, em-01, gq-01.
"""
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"wrote {args.out} ({len(qs)} queries, PT-touching {pt_touch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
