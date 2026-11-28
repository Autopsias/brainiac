"""The ``brain --help`` epilog text (single source for every subparser)."""
from __future__ import annotations

EPILOG = """\
note: --vault (and $BRAIN_VAULT) is a TOP-LEVEL option — it must come BEFORE the
      subcommand (`brain --vault ./vault rebuild`, not `brain rebuild --vault …`).
      With $BRAIN_VAULT set, omit --vault.

agentic tool surface (lexical-first, embed lazily):
  grep / bases-query    never embed (cheap first probes)
  search / hybrid-search RRF(k=3, BRAIN_RRF_K=60 rollback) + bounded exact
                        alias/title leg (rrf_k=60; BRAIN_EXACT_LEG_ENABLED=0
                        disables). Cross-encoder rerank ON by default (window
                        20, cap 50; --no-rerank / BRAIN_RERANK_DISABLED=1;
                        timeout $BRAIN_RERANK_TIMEOUT_S=30s). Adaptive gate
                        skips rerank on unique exact pin (--no-rerank-gate /
                        BRAIN_RERANK_GATE_DISABLED=1). --explain emits gated
                        per-stage attribution.
  --variant             repeatable alternative phrasing/translation (max 4,
                        BRAIN_VARIANTS_ENABLED=0 disables; outer BRAIN_MULTI_RRF_K=60,
                        BRAIN_MULTI_GUARD=1). 2+ variants run --rerank-fused
                        by default (--no-rerank-fused / BRAIN_RERANK_FUSED_DISABLED=1).
  BRAIN_ZONE_WEIGHTS    optional JSON dense-only zone multiplier (neutral by
                        default; BRAIN_ZONE_SCOPE=semantic_only|all,
                        BRAIN_ZONE_SOURCE_MODE=column).
  graph-expand          DISCOVERY-ONLY (authoritative: false; confirm via get/read)
  diagnose              runs production ranking and reports target stage/cutoff
                        (withheld target prints only `withheld`)

hit fields & dedup:
  evidence              alias_hit | exact_title_match | title_phrase_match |
                        keyword_exact | high_vector_match | weak_semantic
  create_safety         exists | probable | unknown (`exists` only for visible
                        unique live owner matched on original query; retired
                        owners, collisions, withheld owners, or matches found
                        only via a query variant cannot yield `exists`)
  family collapse       folds byte-identical accepted-supersession families >=
                        $BRAIN_FAMILY_MIN_BODY (1024 B) before scoring
                        (BRAIN_FAMILY_COLLAPSE_DISABLED=1 disables)
  replay                host-only post-egress log under <index_dir>/query-log/;
                        `brain eval replay --against FILE --json`

temporal-intent & retrieval discipline (details in AGENTS.md §5):
  - Decision-state questions: `brain dossier "<question>" --json` (returns
    decisions + sources + `tensions` + `freshness`, retired versions excluded;
    `bases-query --where type=decision --latest-only` is the raw probe).
    `type: decision` is authoritative; a newer `type: source` never overturns
    a decision on its own — report the tension.
  - Temporal queries: `bases-query --latest-only --json`, `bases-query --as-of
    YYYY-MM-DD --json`, `get <id> --json`. `search`/`grep`/`recent` hide
    retired versions by default; pass `--include-retired` for prior versions.
  - React to `freshness` ("N sources newer than your newest hit") and egress
    `-- N withheld` hints (elevate `--max-tier`, do not assume vault empty).

owner-declared supersession (SUP-01):
  drop lane: <file>.supersedes sidecar or `replaces:` in dropped .md under
             inbox/_deliverables/<project>/ (staged as an owner question,
             Internal at most — else use `brain supersede`; failures report
             `supersede_declared_failed`)
  brain-mcp: `supersede` (visibility + relatedness guarded, else proposed) /
             `unsupersede` (host-only CLI: `brain supersede` / `brain unsupersede`)

egress filter (deny-by-default below cap):
  tiers low->high: Public < Internal < Confidential < Restricted < MNPI
  default --max-tier: full vault (MNPI) on host, Internal on --role vm;
  unlabelled notes rank MNPI. Final stage before stdout (egress decision, not
  containment — use `brain project` for filtered workspace containment).
  JSON `egress.total` includes withheld notes; `egress.surfaced` is printed.

examples:
  brain dossier "ERP cutover" --json
  brain search "arctic embed" --variant "embedding" --explain --json
  brain grep "sqlite-vec" --json
  brain bases-query --latest-only --where type=note --json
  brain bases-query --as-of 2026-03-01 --json
  brain diagnose "Cafe Aurora" --target cafe-aurora --json
  brain graph-expand brain-engine --depth 2 --json
  brain get arctic-embed-choice --json
  brain recent -n 5 --max-tier Confidential
  brain --vault ./vault --role vm draft-capture --id note-1 --content "..." --json
  brain --vault ./vault rebuild
  brain --vault ./vault write ...
  brain --vault ./vault verify-audit
  brain --vault ./vault supersede arctic-embed-choice e5-small-choice --reason "switched embedder"
  brain --vault ./vault project --dest /tmp/vm-workspace --max-tier Internal
"""
