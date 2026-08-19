"""The ``brain --help`` epilog text (single source for every subparser)."""
from __future__ import annotations

EPILOG = """\
note: --vault (and $BRAIN_VAULT) is a TOP-LEVEL option — it must come BEFORE the
      subcommand. `brain --vault ./vault rebuild`, not `brain rebuild --vault …`.
      With $BRAIN_VAULT set, you can omit it entirely.

agentic tool surface (RET-04 — compose these; lexical-first, embed lazily):
  grep / bases-query never embed (cheap first probe); hybrid-search embeds the
  query only on semantic escalation; graph-expand is DISCOVERY-ONLY (its derived
  wikilink graph is never authoritative — confirm candidates with get/read).

ADR-0008 exact identity:
  search/hybrid-search use fused RRF(k=60): BM25 + dense + a bounded exact
  alias/title leg. The exact leg never trusts FTS token-OR membership for phrase
  claims: aliases/titles are normalized identity projections, title phrases are
  verified as contiguous title-token spans, and keyword_exact uses a literal
  boundary verifier. Emergency retrieval rollback is immediate:
    BRAIN_EXACT_LEG_ENABLED=0 <restart the invoking process>
  No rebuild is needed. With the switch off, exact ranking injection/pinning/
  collision slot normalization are disabled, while already-surfaced organic
  hits can still carry truthful evidence/create_safety labels.

BR-03 rerank default (owner ruling 2026-08-04, window revised same day):
  search/hybrid-search rerank ON by default, cross-encoder candidate window
  20 — measured on the 66-query golden set (eval/FOLLOWUPS.md #4): mrr@10
  0.267 -> 0.411, hit@1 0.212 -> 0.349, for ~5.5s p50 / 8.2s p95 added
  latency the owner has explicitly accepted for the quality gain. The window
  briefly shipped at 50 on a latency figure that turned out to be the
  window-20 row mislabelled; at 50 it really costs p50 68.0s and 85% of
  queries blow the 30s timeout and get the BARE ordering anyway
  (eval/FOLLOWUPS.md #6). Window 20 is the latency actually accepted and it
  reranks every query. The CEILING stays 50, so BRAIN_RERANK_TOP /
  BRAIN_RERANK_MAX still opt into the wide pass deliberately. Skippable per
  call (--no-rerank) or globally:
    BRAIN_RERANK_DISABLED=1 <restart the invoking process>
  An explicit --rerank/--no-rerank always wins over the env var. A slow
  rerank call is also caller-bounded: it degrades to the pre-rerank order
  after $BRAIN_RERANK_TIMEOUT_S (default 30s) rather than hanging the
  caller — the model can't be interrupted mid-call, so the slow call keeps
  running in the background and its result is discarded (BrainIndex._rerank_impl).
  At window 20 the slowest golden query measures 10.5s, so 30s is ~2.9x the
  measured worst case; raise it alongside any wide-candidate pass.
  BRAIN_RERANK_MAX raises the window ceiling further; BRAIN_RERANK_TOP
  overrides the requested window size itself.

RK-02 adaptive rerank gate (2026-08-04):
  reranking is default-on but not always worth what it costs. When ADR-0008
  pins a UNIQUE full alias/title owner, rank 1 is already decided and the
  cross-encoder may not touch it — so search SKIPS reranking on those
  queries. Measured on the same 66-query golden set
  (eval/runs/rerank-gate-calibration-2026-08-04.json): the 7 queries it
  fires on score IDENTICALLY to always-on — recall@10/recall@20/mrr@10/hit@1
  all +0.0000 (checked against both the window-20 and window-50 arms) —
  while their latency drops from a 6.2s median to a 200ms median at the
  shipped window 20. Force unconditional reranking back on per
  call with --no-rerank-gate, or globally with
    BRAIN_RERANK_GATE_DISABLED=1 <restart the invoking process>
  An explicit --rerank-gate/--no-rerank-gate always wins over the env var.
  `search --explain --json` reports the decision under
  ranking.rerank_gate (enabled/skipped/reason).

RET-01 zone-authority prior — a measured OPT-IN, off by default (s06, 2026-08-04):
  RRF sums 1/(60+rank) across legs, so a document present weakly in TWO legs
  outranks one present strongly in ONE. That is why a Portuguese question
  against English notes loses: its BM25 leg finds nothing (no shared tokens),
  the dense leg has the answer at median rank 2, and the fused ranking exits
  it at median rank 52 (eval/FOLLOWUPS.md #7). The engine carries a
  counterweight — a multiplicative per-zone boost applied AFTER fusion, only
  to dense-leg-only hits (scope=semantic_only, so an exact/lexical hit's OWN
  factor stays 1.0). That protects the exact hit's SCORE, not its RANK:
  boosting its neighbours can still out-rank it, and measurably does —
  lexical_identifier mrr@10 falls 0.750 -> 0.725 at W=3.0 (and 0.631 at 5.0)
  while its recall@10 holds flat. It is NEUTRAL out of the box.
  Arm it per vault, keyed on that vault's ZONE NAMES:
    BRAIN_ZONE_WEIGHTS='{"brain": 2.5, "raw": 1.0}' <restart the process>
  Measured on the 66-query golden set, weight selected on its pre-registered
  TRAIN half and read ONCE on the held-out half (eval/FOLLOWUPS.md #9,
  eval/zone_prior_calibration.py): held-out mrr@10 0.198 -> 0.386, paired
  permutation p=0.011; cross_lingual_pt_en recall@10 0.000 -> 0.458 at W=3.0.
  NO REBUILD — it is a query-time multiplier, it never touches embed_model /
  embed_dim; unset it and the next process is back to shipped behaviour.
  It ships OFF because 66 queries can resolve the DIRECTION but not the
  CONSTANT: the train half's argmax is 3.0, the held-out half's is 5.0, and
  the observed effect sits below the minimum this n could reliably detect
  (MDE 0.199 at 80% power). 2.0-3.0 is the evidenced range, not a calibrated
  value. Costs at 3.0: temporal mrr@10 0.431 -> 0.344 (and one gold document
  out of the top 10), cross-lingual-ES recall@10 0.250 -> 0.167, identifier
  precision as above. At 4.0-5.0 those become a collapse — identifier mrr@10
  0.665/0.631, temporal mrr@10 0.317/0.179, cross-lingual-ES recall@10 0.000
  at 4.0 — while the aggregate barely moves. That is the ceiling.
  CONFIRM IT ENGAGED: `search --explain --json` reports each hit's
  zone.applied / zone.factor (an unknown zone key resolves to 1.0). A
  malformed BRAIN_ZONE_WEIGHTS / BRAIN_ZONE_SCOPE prints one stderr warning
  and is dropped; an unrecognised scope fails safe to semantic_only.
  NOTE for tuning: _resolve_zone prefers a note's `source_zone:` frontmatter
  and falls back to the flattened `brain`/`raw` zone column. On a vault whose
  notes carry no `source_zone` (the reference vault: 0 of its 2,570 indexed
  notes), keys other than `brain`/`raw` are silently a no-op. Zone factors
  must be finite and within [1e-6, 1e6]. BRAIN_ZONE_SCOPE = all |
  semantic_only (default semantic_only); BRAIN_ZONE_SOURCE_MODE=column
  disables the frontmatter lookup.

--variant (RET-05 multi-query fan-out, wired 2026-08-09):
  ask the SAME question in several phrasings and fuse the answers:
    brain search "what did we decide about pricing" \
        --variant "o que decidimos sobre precos" --explain --json
  Each variant runs its own full hybrid search (shallow, per_query_k 20 — a
  wider per-variant fetch measurably HURTS: 20->80 dropped fan-out recall
  0.736->0.625) and the result lists are Reciprocal-Rank-Fused into one
  ranking. The ORIGINAL query is always variant 0 and keeps every ADR-0008
  guarantee: only IT can pin rank 1 or report create_safety `exists`, so a
  mistranslated variant that happens to match an unrelated note's title can
  never tell a capture agent that note already exists (it is capped at
  `probable`). The egress gate applies ONCE, to the pooled result.
  TWO CONSTANTS: the INNER per-variant fusion is unchanged (RRF_K_FUSE, with
  rrf_k=60 as ADR-0008's exact-leg key); the OUTER pooling constant is
  separate and moves only via BRAIN_MULTI_RRF_K (default 60) — deliberately
  not a flag, since a non-60 rrf_k would silently disable the exact leg.
  Guards: identical variants deduplicate, an empty variant contributes
  nothing, variants past BRAIN_MULTI_MAX_VARIANTS (default 4) are dropped
  from the TAIL and reported (supply variants in descending vault-language
  prevalence), BRAIN_MULTI_GUARD=1 arms the correlated-vote guard (a document
  some variant ranked in its top 3 outranks one no variant did), and
  BRAIN_VARIANTS_ENABLED=0 is the kill switch.
  --rerank-fused runs the cross-encoder once over the POOLED candidates
  against the original query. Owner ruling 2026-08-12: it is ON BY DEFAULT
  whenever 2+ variants survive the guards, and NEVER on a single query — the
  single-query ranking is untouched. Evidence: +0.0643 recall@10 over the
  shipped configuration (p 0.0284, 6 wins / 1 loss), TRAIN-HALF ONLY and not
  confirmed on a held-out half. Costs one extra cross-encoder pass (~5-25s) on
  fan-out calls. Opt out per call with --no-rerank-fused;
  BRAIN_RERANK_FUSED_DISABLED=1 is the global kill switch (restart the invoking
  process; no rebuild).
  CONFIRM IT ENGAGED: `--explain --json` reports `variants`
  (count, dropped sets, both constants, guard, pin, rerank gate, and each
  variant's rank/contribution for every surfaced hit).

evidence/create_safety:
  every surfaced search hit carries one evidence label: alias_hit,
  exact_title_match, title_phrase_match, keyword_exact, high_vector_match, or
  weak_semantic. create_safety is exists/probable/unknown. `exists` is reserved
  for one visible unique full alias/title owner; alias/title collisions, retired
  owners, or any full owner withheld by egress degrade the public answer without
  exposing hidden ids, owner counts, ranks, titles, or a collision label.

duplicate-family collapse (HYG-01):
  the same bytes indexed twice (a copy and its date-stamped re-ingest) split one
  document's ranking signal across two near-identical vectors. Ranked retrieval
  folds such a family into its canonical member BEFORE the legs are scored, so
  the canonical inherits the family's best rank instead of two halves; the
  absorbed ids ride on the surviving hit as `duplicates`, never as extra slots.
  DELIBERATELY NARROW: a family needs byte-identical bodies AND an explicit
  owner-accepted supersession link AND >= $BRAIN_FAMILY_MIN_BODY (1024) bytes of
  body. Name/near-body similarity is NOT collapsed — AGENTS.md §4 CUR-01 makes
  that a PROPOSAL for the owner, not a ranking-time identity claim — and short
  bodies are refused because `[no text detected]` extraction stubs share one
  hash across unrelated documents. Two genuine revisions are never one family.
  `search --explain --json` reports ranking.family_collapse
  (enabled/collapsed/declined); `BRAIN_FAMILY_COLLAPSE_DISABLED=1` is the
  rollback and needs no rebuild.

explain / diagnose:
  `search --explain` emits gated per-hit attribution (lexical/dense/exact
  contributions, raw RRF, zone/staleness, rerank, pin, near-dup) plus a bounded
  candidate digest whose ids are also egress-surfaced. `diagnose` runs the same
  production ranking and then reports a target's stage presence/rank/cutoff;
  if the target is above the egress cap, the target prints only as `withheld`.
  rerank is skippable and bounded to the top 10-50 (ON by default at window
  20 — see BR-03
  above); unique full identities are pinned outside the reranker, collision
  groups keep live-before-retired order only inside the slots the reranker
  selected, and reranker scores remain separate from RRF scores.

host query log / replay:
  host query capture is post-egress, best-effort, and HOST ONLY. It writes raw
  queries under the resolved host app-data index directory at `query-log/`,
  deliberately outside vault/ and vault/.brain/, with owner-only directories
  and files; unsafe symlink/env overrides into the vault are refused. Retention
  removes whole month JSONL files. VM role cannot read, write, resolve, or
  replay the host ledger. `brain eval replay --against FILE --json` never
  recaptures; it reports top1/Jaccard/rank/latency telemetry split into
  vault_same and drift_or_mixed. Thresholds apply only to vault_same because the
  log has no target qrels.

temporal-intent routing (TMP-03): when a question is really about TIME —
"latest", "current version", "as of <date>", "previous version" — probe the
temporal query surface FIRST, before plain semantic search:
  brain bases-query --latest-only --json          # "what's current" / "latest"
  brain bases-query --as-of 2026-03-01 --json      # "as of <date>" / point-in-time
  brain get <id> --json                            # inspect previous_version /
                                                     # superseded_by / is_latest_version
                                                     # on any single hit ("previous version")
search/get results also carry `is_latest_version` on every hit, so even a plain
semantic-search agent can prefer the current claim without a second round-trip.

retrieval discipline (non-negotiable; details in AGENTS.md §5):
  - every search hit carries `type` — the AUTHORITY signal. A `type: decision`
    hit IS the recorded decision layer; a `type: source` hit (memos, decks,
    drafts) is material under consideration and NEVER overturns a decision on
    its own. Conflict between a newer source and a decision note? Report the
    tension — never promote the proposal.
  - decision-state questions ("what have we decided", "latest decisions",
    "current state of X"): `brain dossier "<question>" --json` is the
    ONE-CALL sweep — decision layer + sources + tensions (newer sources
    post-dating a decision) + freshness, retired versions pre-excluded.
    (`bases-query --where type=decision --latest-only` remains the raw
    probe.) The newest DOCUMENT VERSION is not the newest DECISION STATE.
  - react to the search response's `freshness` block ("N sources newer than
    your newest hit"): probe past your hits (recent / --latest-only / a
    narrower search) before answering a "latest/current" question.
  - a `-- N withheld` egress line means elevate --max-tier, not "vault empty".

examples:
  brain grep "sqlite-vec" --json
  brain bases-query --where type=note --where classification=Internal --json
  brain bases-query --latest-only --where type=note --json
  brain bases-query --as-of 2026-03-01 --json
  brain search "arctic embed" --rerank --explain --json
  brain diagnose "Café Aurora" --target cafe-aurora --json
  brain eval replay --against ~/.local/share/brainiac/<vault-id>/query-log/2026-07.jsonl --json
  brain graph-expand brain-engine --depth 2 --json
  brain get arctic-embed-choice --json
  brain recent -n 5 --max-tier Confidential
  brain --vault ./vault rebuild
  brain --vault ./vault supersede arctic-embed-choice e5-small-choice --reason "switched embedder"
  brain --vault ./vault project --dest /tmp/vm-workspace --max-tier Internal

egress filter (deny-by-default below the cap):
  tiers low->high: Public < Internal < Confidential < Restricted < MNPI
  default --max-tier: full vault (MNPI) on host, Internal on --role vm;
  unlabelled notes rank as MNPI (withheld at any lower cap).
  the filter is the final stage before stdout. it is an egress DECISION, not
  containment — a file-capable harness reads Markdown directly; use
  `brain project` (a filtered workspace copy) for real containment.
  JSON `egress.total` INCLUDES withheld notes by design (it is an audit count,
  not a leak of content); `egress.surfaced` is what was printed.
"""
