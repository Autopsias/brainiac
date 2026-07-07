# Eval follow-ups

Logged, not yet actioned. Surfaced during the v0.9.0 full-corpus baseline
(`s13-final`, recorded 2026-07-05; see `_evidence/eval/heartbeat.txt` for the
metrics record). None block a release — the retrieval baseline is informational
(ADR-0004 / HARDENED:grill-1).

## 1. Spanish-query retrieval is genuinely weak (not just the fixed qrels bug)
After correcting the stale ground truth (overall recall@10 0.350 → 0.471), the
Spanish segment is still low (recall@10 ≈ 0.17). It is a real
`multilingual-e5-small` weakness on Spanish-worded queries, independent of the
qrels fix: for the *same* target document, an English-worded query ranked it in
the top handful while its Spanish-worded twin ranked it far deeper. **Candidate
fix:** evaluate a stronger multilingual embedder (e.g. `multilingual-e5-base`,
or a reranker on the ES slice) and re-baseline; weigh the latency/size cost
against the gain. Highest-value of these three.

## 2. The `monolingual_es` stratum is mislabeled
Its target documents are English-language, so it actually tests cross-lingual
ES→EN retrieval, not monolingual Spanish. **Fix:** rename the stratum to reflect
that, or add genuinely Spanish-language target notes if a true monolingual-ES
test is wanted (none surfaced in the current corpus).

## 3. Make the eval self-healing against fixture/corpus drift
The committed eval fixtures (`golden_set.json`, `qrels/qrels.json` — both
gitignored/private) reference entity names by an anonymization scheme that
drifts from the private corpus over time; when it drifts, ground-truth paths
stop resolving and scores silently understate quality (this is what produced the
0.000 Spanish artifact). **Fix:** a gitignored codename map applied at score
time (same pattern as the release contamination denylist) so the public fixtures
and the private corpus stay reconcilable without either side leaking into the
other.
