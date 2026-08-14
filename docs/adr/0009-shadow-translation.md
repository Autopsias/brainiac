# 9. Shadow translation of note content into the derived index

Date: 2026-08-10

## Status

**Proposed — NOT adopted. SKELETON only.**

This ADR exists so the constraints on shadow translation are written down once,
and so a future session does not relitigate them from scratch. It records no
decision and authorises no code. It is deliberately unfinished: the sections
that would need filling in — the translation source, the measurement design, the
cache invalidation contract — are marked OPEN, and the reason they are open is
that the measurement that would justify filling them came back near zero.

**The measurement:** `_evidence/anylang/s06-residual-report.md` (2026-08-10).
After phase A (caller-opt-in query variants, `_decisions/anylang-s05-ship-ruling.md`),
**2 of 181 gold labels** are unreachable for a language reason shadow
translation could address — both Spanish documents, in a language holding 0.65 %
of the vault and therefore below the census `min_share` that governs the variant
contract. The recommendation on record is CLOSE.

## Context

Retrieval crosses a query–document language boundary in one of two directions.
Phase A crosses it on the **query** side: the calling agent supplies variants,
the engine pools them (`core.search_multi`, RET-05). The engine holds no
generative model and deliberately never will, so the query side costs nothing —
there is already a frontier model in every real query path.

The **document** side is the other direction: store a derived translation of each
note beside it, so a query in language X can reach a note written in language Y
through the derived text. That is what this ADR would govern.

Phase A measured what the query side leaves behind. On the default (non-opt-in)
path 54 of 181 labels are reached by no retrieval leg at depth 50; opted-in that
falls to 37. Of those 37, **34 are labels where the query language and the
document language already match** — translation is a no-op there by definition —
1 is a duplicate-family artefact, and 2 are a genuine language boundary.

## Settled constraints

These follow from the plan's declared `out_of_scope`
(`_plans/anylang-query-variants-2026-08-09/spec.json`) and from the substrate
contract. They hold whenever this ADR is picked up, whatever the residual is
then.

1. **Derived-index-only storage.** Shadow text lives in the derived, rebuildable
   index and nowhere else. Nothing is written into `vault/`: `raw/` is immutable
   and `brain/` is knowledge, not machine-derived text. Deleting the index and
   rebuilding must reproduce it exactly.
2. **Classification inheritance.** A shadow translation carries the source note's
   `classification` verbatim — never a default, never a fresh label, never a
   lowered one. It is surfaced through the same deny-by-default egress gate as
   its source, and a withheld note's translation is withheld with it.
3. **Cache by content hash.** Keyed on the note's `sha256`/`content_hash`, so a
   re-index skips work already done and an edited note invalidates its own
   translation. No time-based expiry, no manual cache clearing step.
4. **`scan_vault` untouched.** The INT-03 indexing-scope rule does not move.
   Shadow text is an attribute of an already-indexed note, never a new document,
   never a second indexing scope.
5. **Language-agnostic or wrong.** No Spanish-specific or Portuguese-specific
   code path. The mechanism keys on the census, not on a hard-coded language.
6. **The AGENTS.md amendment is a precondition, not a follow-up.** §5 rule 3 says
   verbatim "never translate note content or canonical prefixes, and `raw/` stays
   immutable". That sentence was written about the vault, not about a derived
   cache, but it does not distinguish them. If this ADR is ever adopted, the rule
   is amended **in the same commit**, stating explicitly that derived,
   rebuildable, classification-inheriting shadow text is not note content. It is
   never built around silently.

## OPEN — the translation source

Unresolved. Both options carry a cost the other does not, and neither is
recommended here because the population they would serve is 2 labels.

| option | cost | what it re-opens |
|---|---|---|
| **Batch model-in-the-loop session** — the frontier model already present in every real query path translates the corpus through the audited write path, once per refresh | one long session per corpus refresh; every translation egresses note content to the model, which for MNPI-tier notes is a classification decision, not a throughput one | nothing structural: the same trust posture as any drafting session |
| **Vendored local MT** (NLLB, Marian) | a new model dependency, wheels on every platform, the Cowork VM's cp310 ABI constraint, and a permanent maintenance surface | **the dependency stance the owner closed for the query path.** The plan permits proposing it only inside phase B's checkpoint, and only if the residual demands document-scale translation |

## OPEN — how it would be measured honestly

Unresolved, and materially harder than it looks. The 57/57 held-out half of split
`s05-2026-08-09-expanded114` is **spent** — claimed once by phase A's measurement
(`_evidence/eval-power/HELDOUT-CLAIM-LEDGER.md`). A shadow-translation candidate
would need its own pre-registered design over a fresh or expanded split. On the
2026-08-10 residual that measurement is not merely expensive, it is
**unfalsifiable**: two labels cannot power a claim either way.

## Consequences if adopted

- One more derived artefact to rebuild, and rebuild time grows with the corpus.
- A second text representation per note that the egress gate must cover — a new
  place for a classification bug to hide.
- A standing translation-freshness question every time a note is edited, answered
  by constraint 3 and by nothing else.

## Consequences of NOT adopting (the current state)

- Documents in a language below the census `min_share` stay unreachable to queries
  in any other language. On 2026-08-10 that is 2 of 181 gold labels.
- The re-open condition is mechanical, not a judgement call: re-run
  `eval/s06_reachability.py` when the census moves a language above `min_share`,
  or when a golden expansion adds non-English documents at scale. If the
  language-boundary residual is material then, this skeleton is where the work
  starts.
