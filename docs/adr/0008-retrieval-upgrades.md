# 8. Retrieval upgrades: identity, exact-match fusion, attribution, and private replay

Date: 2026-07-28

## Status

Accepted as the design contract for the retrieval-upgrade work. This ADR is a
design-and-fixture freeze only; it deliberately makes no engine change. The
implementation is split across the later ranking, explain/diagnose, and
query-capture sessions.

## Context

The retrieval engine has a useful hybrid baseline, but it currently treats a
person, company, project, or other named entity much like any other semantic
query. That leaves four gaps:

1. an agent cannot reliably tell whether a note already exists before creating
   another one;
2. a literal alias or title lookup cannot be promoted safely with the existing
   two-leg RRF score alone;
3. users cannot see which ranking stage produced a result; and
4. real-query regressions are hard to investigate without storing private
   queries in the vault or exposing withheld results.

This ADR fixes the contract before ranking code moves. It also freezes a
clean-room evaluation family before tuning constants, so a session cannot
write a tailored test after it has chosen a winning ranking change.

### Pipeline observations and corrections

The contract below is based on the implementation as it exists on this date,
not an assumed pipeline:

| Stage | Observed implementation | Contract consequence |
| --- | --- | --- |
| Lexical candidates | `_lexical_ranked` (`index.py:1266–1282`) builds an FTS expression that ORs individually quoted query tokens. It is not a title or phrase-exact matcher. | “keyword exact” requires a separate literal boundary/phrase check; membership in the lexical leg alone is insufficient. |
| Dense candidates | `_dense_ranked` (`index.py:1324–1369`) asks the vector backend for chunks, then pools each note by its highest-scoring chunk. | Dense attribution records the winning chunk and a high-vector claim must meet both rank and similarity thresholds. |
| Chunk input | `Chunk.embed_input` (`chunk.py:67–80`) prepends contextual note/section text before embedding; `index.py:450–456` adds optional document context. Neither is a post-retrieval score component. | Attribution reports the winning stored chunk and similarity, while keeping contextual-prefix provenance distinct from a ranking boost. |
| Fusion | `hybrid_search` (`index.py:1403–1413`) currently fuses lexical and dense ranks with 1 / (rrf_k + rank), with rrf_k defaulting to 60. | Exact identity evidence becomes a third ranked RRF leg, not a score multiplier applied after fusion. |
| Zone and staleness factors | Zone is applied after two-leg fusion (`index.py:1465–1469`); in semantic_only scope current code skips the zone prior only for `in_lex` hits. `BRAIN_ZONE_WEIGHTS` intentionally supports owner-configured boosts and damps (the documented anti-burial tuning includes about 2.0 and 0.55). Staleness is then applied (`1471–1494`). | Preserve that owner prior. The new literal `in_exact` set joins the lexical exemption when scope is semantic_only, so an exact-only identity/phrase hit is zone-neutral in the default scope; scope=all keeps the existing zone behavior. Staleness applies after all legs. |
| Claimed scale invariant | index.py describes staleness as a penalty at or below 1 and says the fused score cannot exceed 2 / (rrf_k + 1), adding that the zone prior respects that scale. In the current code, a supported zone weight can exceed 1 and BRAIN_RECENCY_WEIGHT is not clamped. | The existing docstring is false for final post-zone scores under a configured boost. Amend it to state a **raw-RRF** ceiling only, preserve finite positive zone weights including boosts, and constrain the recency weight to [0, 1]. Any final-score bound is parameterized by the applied zone maximum. |
| Near-duplicate suppression | `_suppress_near_dups` (`index.py:1196–1263`) runs after score ordering (`1501–1503`) and can defer a dense-only candidate. | Every full alias/exact-title candidate is explicitly exempt; otherwise a literal identity candidate can be swallowed after it was ranked. Partial phrases retain ordinary suppression. |
| Reranking | `_apply_rerank` (`index.py:1664–1710`) may reorder its head solely by cross-encoder score; `rerank.py` supplies the identity fallback. | A fusion score is not a final-order guarantee. The unique full-identity pin must be partitioned outside the reranker. |
| Egress | CLI output is filtered through `egress.apply_gate` (`egress.py:74–86`) at the output boundary. `egress.py:11–16` explicitly calls this a decision mechanism, not filesystem containment. | Explain, diagnose, query capture, and replay must not use the gate as a reason to put sensitive data in a VM-visible file. |
| Evaluation gate | `gate.py:68–72` paired deltas use the intersection of current and candidate IDs. `harness_direct.py:148–155` emits overall, language, and class segments, but no family segment. | The augmented baseline must precede implementation, and each family’s delta is computed explicitly rather than pretending an existing class aggregate is a family gate. |

The narrowest material correction to earlier framing is therefore this:
post-RRF boosts are prohibited. They break the score-scale reasoning, do not
help an injected alias candidate with a zero fused score, and make title
promotion unbounded.

The current staleness docstring says, verbatim:

> A *penalty* (≤1), not a boost (>1), so the fused score never exceeds the RRF
> ceiling ``2/(rrf_k+1)`` — the fusion-scale invariant the zone-authority prior
> also respects.

The implementation must preserve the intent while correcting its two-leg
ceiling for the deliberately added exact leg.

## Decision

### 1. Identity evidence and create safety

#### Normalized identity

All matching identity strings use this deterministic normalization:

1. Unicode NFC normalization.
2. Unicode casefold.
3. Replace each run of Unicode whitespace with one ASCII space.
4. Trim leading and trailing space.

The normalizer does not strip punctuation or accents. This deliberately makes
the macOS NFD case explicit: an alias authored as NFD “Café Aurora” and a
query entered as NFC “Café Aurora” compare equal. Display text remains exactly
as authored.

Phrase tokenization is a separate operation for title-phrase eligibility; it
does not change the identity normalizer.

#### Frontmatter and validation contract

A brain note may carry an optional frontmatter field:

~~~yaml
aliases:
  - "Contoso Study"
  - "Café Aurora"
~~~

The field is a list of zero to 128 strings. Each string must:

- be a scalar string, not a map or nested list;
- contain non-whitespace content after trimming;
- contain no more than 256 Unicode scalar values; and
- be unique after normalized-identity comparison within the note.

Both standard block YAML lists and inline YAML lists must work even when the
PyYAML fallback parser is in use. The present fallback recognizes only the
inline form, so the implementation session must extend it and test both forms;
it must not make valid normal YAML silently disappear.

Aliases are allowed only on notes in the brain zone. They are not required and
do not turn source metadata into instructions. The validator adds “aliases” to
tools/validate.py’s OKF_ALLOWED_KEYS so correctly authored aliases do not
generate an unknown-key warning. Invalid local entries are validation errors.
An alias shared by different notes is a collision warning, not an error:
history, migration, and supersession all make collisions legitimate.

Hand-authored aliases are an acknowledged exception to the 2026-07-11
self-organizing-metadata ruling. They record an owner-curated identity claim,
not a folder or tag taxonomy. Automatic alias derivation is explicitly a
future maintain-fold proposal, not behavior in this ADR and not a new scheduled
task.

#### Projection, lifecycle, and collisions

The index adds a normalized title projection and this alias table:

~~~sql
-- `title_norm` is a NOT NULL notes projection using normalized_identity(title).
-- The schema-version bump makes this part of the rebuilt notes table rather
-- than a live ALTER migration.
CREATE TABLE aliases (
    alias_norm TEXT NOT NULL,
    note_rowid INTEGER NOT NULL,
    PRIMARY KEY (alias_norm, note_rowid),
    FOREIGN KEY (note_rowid) REFERENCES notes(rowid)
);
CREATE INDEX idx_aliases_lookup ON aliases(alias_norm);
CREATE INDEX idx_notes_title_norm ON notes(title_norm);
~~~

The notes projection stores title_norm using the same normalized-identity
function. A schema-version bump and a rebuild are required.

Lifecycle rules are deliberately explicit:

- rebuild writes title_norm and every alias row from the scanned note;
- sync deletes old alias rows before writing the refreshed projection, then
  writes the current rows in the same transaction as note/chunk replacement;
- delete removes aliases for the note before removing the note row, rather
  than relying on an implicit cascade; and
- rename, rebase, or path deletion follows the same delete-and-reproject
  path, so no stale owner remains searchable.

For a normalized query q, the identity owner set is the distinct union of
notes where aliases.alias_norm = q and notes.title_norm = q. One note that owns
both forms still counts once. A title on note A and an alias on note B is a
collision, even if either match by itself appears exact.

This is a pre-egress calculation over the complete indexed owner set. It is
not recalculated over the returned rows: doing that would turn a visible member
of a hidden collision into a false unique identity. The public response never
exposes the owner count, an owner-visibility flag, or a hidden owner's
identity.

When more than one owner exists, exact-leg ordering uses this stable tiebreak:

1. a note whose is_latest_version is not false;
2. zone authority (higher configured finite zone multiplier first);
3. valid-time date, choosing effective_date, then document_date, then
   created, with newer dates first; and
4. stable note id ascending.

Every `brain supersede` of an aliased note can manufacture an alias collision,
because the retired note keeps its aliases. That is intentional: the retired
alias remains retrievable as history but cannot cause a false “already exists”
conclusion.

#### Evidence enum

Every surfaced hit receives exactly one evidence label, selecting the strongest
applicable item in this order:

| Label | Meaning |
| --- | --- |
| alias_hit | The normalized query matches an alias on this note. |
| exact_title_match | The normalized query matches this note’s normalized title. |
| title_phrase_match | A separately verified contiguous query phrase is eligible for the title exact leg. |
| keyword_exact | A separately verified literal word-boundary query phrase occurs in the title or body, but no stronger identity rule applies. |
| high_vector_match | The note is dense rank 1–3 and its best-chunk similarity is at least 0.80, with no stronger rule. |
| weak_semantic | Any remaining surfaced hit. |

An eligible title phrase has at least two normalized phrase tokens, is strictly
shorter than the full title, occurs contiguously in the title, and covers at
least 60% of title tokens. It is never inferred from FTS token-OR membership.
“keyword_exact” also uses a literal boundary/phrase verifier. For a one-token
query, it is eligible only when the token is identifier-shaped: at least eight
alphanumeric characters, or it contains a digit, hyphen, underscore, or slash.
This prevents a common word from acquiring an identity-level conclusion merely
because FTS returned it.

The labels are observational, not authority upgrades. In particular, a raw
source cannot become a decision because it has an alias-like phrase.

`evidence` is the complete new match explanation. The existing `source` field
keeps its organic-leg meaning (`lexical`, `semantic`, or `both`) whenever either
organic leg fired; a candidate injected solely by the exact leg is reported as
`source: "exact"`. An exact contribution never overwrites an existing organic
source value; `--explain` records all contributing legs.

#### create_safety derivation

create_safety is an agent-facing answer to “may I skip creating a new note?”.
It is intentionally stricter than relevance.

| Strongest condition for a surfaced candidate | Full pre-egress identity owner set | create_safety | Agent meaning |
| --- | --- | --- | --- |
| A full alias/title owner is withheld | any | unknown | The gate cannot safely establish visible uniqueness; do not infer a hidden collision or hidden note from this value. |
| alias_hit or exact_title_match | exactly 1, and that owner is surfaced | exists | A unique existing note owns the queried identity; creation may be skipped after reading it. |
| alias_hit or exact_title_match | 2 or more, all owners surfaced | probable | An identity collision exists; do not skip creation based on either note. |
| title_phrase_match | any | probable | A title appears related, but the query is not the whole identity. |
| keyword_exact | any | probable | Literal relevance is not unique identity. |
| high_vector_match | any | probable | Strong semantic resemblance is not proof of identity. |
| weak_semantic or no surfaced identity | any | unknown | There is no safe basis for a create/no-create conclusion. |

The collision rule is non-negotiable: an alias uniquely owned by one note may
yield exists; an alias owned by two or more distinct notes may not. The field
agents trust to skip creating a note therefore never returns exists from a
collision, even when the collision winner is ranked first.

In fact, exists is reserved for a unique full normalized alias or title
identity. Even an unusually specific literal body keyword remains probable:
the text may describe an entity without the note being its canonical record.

The API may expose evidence and create_safety only on egress-surfaced results.
A withheld full-identity owner affects the internal safety calculation only by
forcing `unknown`; it never yields an exposed owner count, collision label,
title, id, rank, or explanation. Unrelated withheld candidates do not alter the
identity calculation. A query with no surfaced unique owner is unknown, not
“does not exist.”

### 2. Named-entity ranking: a weighted third RRF leg

#### Rejected post-fusion multiplier

The design does not multiply a fused score after RRF. A multiplier greater than
1 breaks the current stated “penalty only” scale: it can exceed the two-leg
ceiling 2 / (rrf_k + 1). It is also undefined for an alias-injected note that
is absent from both lexical and dense legs: its fused score is zero, and any
multiple of zero remains zero. Finally, at rrf_k = 60 a dense-only rank-30 note
has 1 / 90 = 0.011111 while a rank-1 both-leg hit has 2 / 61 = 0.032787;
lifting the former past the latter requires roughly a 3x multiplier. That is
the unbounded, parameter-sensitive behavior this design forbids.

#### Exact leg and pinned calibration

Production RRF is pinned at rrf_k = 60 while this contract is in force. RRF
weights are parameter-sensitive; an experiment that changes rrf_k must disable
the exact leg or provide a new ADR-calibrated set of weights and bounds.

hybrid_search constructs three ordered candidate lists:

1. lexical: the existing FTS list;
2. dense: the existing best-chunk-pooled list; and
3. exact: candidates found by exact alias, exact title, or eligible contiguous
   title phrase matching.

For a note qualifying through more than one exact condition, it appears once
in the exact list at its strongest tier. Exact-list order is tier first and the
collision tiebreak above within a tier. The list retains at most 16 full exact
candidates and at most 16 partial-phrase candidates; the cap bounds work and
diagnostics, not a hidden post-fusion boost. Its weighted contribution is:

~~~text
score_exact(note) = w_exact(tier) / (60 + exact_rank)
~~~

| Exact tier | w_exact | Largest contribution at rank 1 | Rank-space consequence |
| --- | ---: | ---: | --- |
| Uniquely-owned full alias or full exact title | 2.25 | 2.25 / 61 = 0.036885 | A unique full identity has one owner and is rank 1 in this tier. Its contribution is greater than a rank-1 organic both-leg score, 2 / 61 = 0.032787: a uniquely-owned full alias/exact title therefore dominates any organic hit in raw RRF rank space. |
| Colliding full alias or colliding full exact title | 1.00 | 1 / 61 = 0.016393 | Cannot by itself displace a rank-1 organic both-leg hit. A collision is a supported historical state, not permission to override recency or acquire a global top-1 right. |
| Floor-gated contiguous partial title phrase | 0.25 | 0.25 / 61 = 0.004098 | Cannot by itself displace even a rank-1 one-leg hit, much less a rank-1 both-leg hit. |

This is a bounded rank-space contract, not a promise that every later
cross-encoder score is numerically comparable with RRF.

The previous docstring in index.py around the staleness factor is amended in
the implementation session to state the deliberate three-leg **raw-RRF**
invariant:

~~~text
With rrf_k = 60 and w_exact_max = 2.25, raw RRF score never exceeds
(2 + w_exact_max) / (rrf_k + 1) = 4.25 / 61. The staleness factor is a
penalty in (0, 1]. The separately configured zone-authority multiplier is
applied after raw RRF and may intentionally be greater than 1; it is not part
of this raw-fusion ceiling.
~~~

The implementation must make that statement true, rather than merely change
the comment. `BRAIN_RECENCY_WEIGHT` is constrained to [0, 1].
`BRAIN_ZONE_WEIGHTS` preserves existing finite positive owner factors,
including boosts above 1; malformed, non-finite, or non-positive entries fall
back to 1.0. If `z_max(query)` is the largest zone factor actually applied to a
candidate, then the separately useful final bound is
`pre_rerank_score <= z_max(query) * 4.25 / 61`. There is deliberately no
unqualified final-score ceiling under an owner-configured zone boost. This
correction preserves the anti-burial contract and makes
`BRAIN_EXACT_LEG_ENABLED=0` a real rollback.

#### Composition, deduplication, and final order

The pre-rerank order is:

~~~text
raw_rrf = lexical_rrf + dense_rrf + exact_rrf
pre_rerank_score = raw_rrf * zone_penalty * staleness_penalty
~~~

The existing zone rule remains visible in this composition. In `semantic_only`
scope an organic lexical candidate skips the zone prior. The new exact leg is a
literal-match signal, so `in_exact` joins that exemption: an exact-only injected
note receives a neutral zone factor in the default scope rather than being
treated as dense-only. `scope=all` continues to apply the owner's zone factor
to every candidate. Staleness runs after zone for every candidate and, with its
validated [0, 1] weight, never increases the score.

The phrase-bound assertion is explicitly a raw-RRF/pre-rerank test case: it
runs with `semantic_only`, neutral/equal staleness factors, and a rank-1
organic `both` anchor. A configured `scope=all` authority factor or an
intentional recency preference may change a cross-zone final order; explain
must attribute that to the existing post-fusion prior, never claim that the
partial exact tier overrode the anchor.

_suppress_near_dups runs after this ordering. Every full alias/exact-title
candidate, unique or colliding, is exempt from suppression; a partial phrase
retains ordinary suppression behavior. The exemption is narrow because it is
for literal identity candidates, not a general near-duplicate bypass. It keeps
both members of a version collision available for the required live-before-
retired ordering while granting neither member a global pin.

Reranking may reorder a head, so final-order behavior is explicit:

1. form and score all three RRF legs, then apply zone and staleness penalties;
2. apply duplicate suppression with the unique full-identity exemption;
3. take at most one unique full-identity winner, pin it at final rank 1, and
   exclude that one result from reranker input;
4. rerank only the remaining eligible head; the no-op reranker preserves the
   incoming order; and
5. across the complete post-rerank candidate sequence (reranked head plus its
   untouched tail), retain the slots held by each normalized collision-owner
   set but refill those same slots in the stable collision order (live, zone
   authority, valid-time date, id); and
6. return the pinned result followed by the collision-normalized reranked
   remainder.

The slot refill in step 5 is deliberately a relative-order rule, not a global
pin. It leaves every unrelated result in its reranker-selected slot and leaves
the collision set collectively free to move against organic hits. It only
prevents a deterministic or learned reranker from putting a retired collision
owner above its own live successor. There is no global pin for a collision or
partial phrase.

The named-family invariant suite runs reranking off and with a deterministic
adversarial reranker. The adversarial case must both try to demote a uniquely
pinned full identity and assign a retired collision owner a higher score than
its live successor; the pin and the within-collision slot rule must both hold.

The exact leg has a runtime kill switch:

~~~text
BRAIN_EXACT_LEG_ENABLED=0
~~~

It disables alias/title/phrase exact candidate injection, exact RRF
contributions, the exact-zone exemption, collision-slot normalization, and the
unique-result pin without a schema rollback. In the disabled state, legacy
ranking IDs, order, scores, source selection, and snippets must be
byte-equivalent for a fixed index and query under every currently supported
zone boost/damp, zone scope, staleness setting, and rerank mode; newly additive
explain fields may be present but cannot change the legacy result values.

Egress remains after candidate selection and before serialization. An exact
candidate that is withheld cannot appear by id, title, rank, contribution, or
identity-owner count in a response.

### 3. Explain and diagnose are per-stage, egress-safe interfaces

Explain is an additive JSON mode. It does not make the normal search response
more verbose by default. It records enough to explain a surfaced result without
inventing a common scale between RRF and a cross-encoder:

~~~json
{
  "query": "Café Aurora",
  "ranking": {
    "rrf_k": 60,
    "exact_leg_enabled": true,
    "rerank_requested": true,
    "rerank_applied": true
  },
  "results": [
    {
      "id": "contoso-cafe-aurora",
      "evidence": "alias_hit",
      "create_safety": "exists",
      "explain": {
        "lexical": {"rank": 2, "contribution": 0.016129},
        "dense": {
          "rank": 1,
          "best_chunk_rowid": 42,
          "similarity": 0.91,
          "contribution": 0.016393
        },
        "exact": {
          "tier": "full_alias",
          "rank": 1,
          "weight": 2.25,
          "contribution": 0.036885
        },
        "raw_rrf_score": 0.069407,
        "zone": {"scope": "semantic_only", "factor": 1.0, "applied": false},
        "staleness": {"factor": 0.95},
        "pre_rerank_score": 0.065937,
        "near_duplicate": {"exempt": true, "suppressed": false},
        "pin": {"eligible": true, "applied": true},
        "rerank_score": null,
        "rerank_rank": null,
        "final_rank": 1
      }
    }
  ],
  "egress": {"surfaced": 1, "withheld": 2}
}
~~~

Null rerank values mean “not scored by the reranker,” not zero. When present,
rerank_score is a model-specific relevance score and is incomparable with
pre_rerank_score; no score delta is emitted. final_rank is one-based among
results after egress, never an internal pre-gate rank. The implementation may
keep internal diagnostics while computing a response, but it must discard them
before stdout for withheld rows.

The compact candidate digest used by both explain and capture is versioned:

~~~json
{
  "version": 1,
  "per_leg_limit": 20,
  "truncated": false,
  "legs": {
    "lexical": [{"id": "example", "rank": 1}],
    "dense": [{"id": "example", "rank": 1}],
    "exact": [{"id": "example", "rank": 1}]
  },
  "pre_rerank": [{"id": "example", "rank": 1}],
  "final": [{"id": "example", "rank": 1}]
}
~~~

Only egress-surfaced IDs appear in a digest. Counts of withheld candidates are
allowed; their IDs, titles, labels, ownership state, tiers, and ranks are not.
If truncated is true, absence is unknown rather than proof a target never
entered a candidate set.

diagnose --target runs ordinary production search unchanged, then probes the
specified surfaced target out of band. The probe does not inject the target,
widen candidate cutoffs, modify RRF ranks, or affect the normal result list.
For an allowed target it reports candidate-leg presence, scores/factors when
available, and a machine-readable verdict plus one final line:

~~~text
VERDICT: exact-identity-pinned
~~~

Allowed verdicts for an egress-surfaced target are exact-identity-pinned,
exact-identity-collision, partial-title-bounded, organic-candidate, and
candidate-miss. `withheld` is reserved for a withheld target, which returns
only:

~~~json
{"target": "withheld", "verdict": "withheld"}
~~~

plus the ordinary aggregate egress counts. It must not disclose the target’s
id, display title, classification, candidate presence, rank, evidence,
ownership state, or reason for withholding. This applies equally to --explain
and diagnose output.

### 4. Real-query capture and replay stay host-contained

Raw queries can carry Restricted or MNPI terms. The capture log therefore lives
under:

~~~text
config.index_dir() / "query-log" / "YYYY-MM.jsonl"
~~~

It never lives under vault, vault/.brain, the published snapshot, or any path
that resolves inside the vault tree. This is a containment requirement, not a
role-policy preference: config.py documents that Cowork mounts the workspace
and sees vault/.brain, while egress.py documents that role checks are not
filesystem containment.

On initialization, the implementation resolves the vault and log directory
through symlinks. If the resolved log directory is inside the resolved vault,
capture is disabled and a local status counter records the configuration error.
The directory is owner-only (0700) and each file owner-read/write only (0600).
`config.secure_file_permissions()` expresses the existing 0600 posture but is
explicitly best-effort, so it is not proof: capture must chmod and stat-verify
the directory/file modes on POSIX. On a platform where equivalent owner-only
ACLs cannot be established and verified, capture is disabled rather than
writing a weaker file.

Capture is enabled by default on the trusted host and disabled unconditionally
in the VM. The host kill switch is:

~~~text
BRAIN_QUERY_CAPTURE_ENABLED=0
~~~

Capture runs at the shared post-egress serialization seam. It receives the raw
query only on the host plus the already-gated response and gated digest; it
never receives an unfiltered result list for logging. The raw query is the
reason the ledger is host-contained rather than vault-contained.

An append record has this minimum schema:

~~~json
{
  "version": 1,
  "at": "2026-07-28T10:00:00Z",
  "query": "raw user query",
  "mode": "hybrid-search",
  "k": 10,
  "rrf_k": 60,
  "exact_leg_enabled": true,
  "rerank_mode": "disabled",
  "rerank": {
    "requested": false,
    "applied": false,
    "model": null,
    "top_n": 0
  },
  "latency_ms": 18,
  "vault_fingerprint": "sha256:...",
  "top": [{"id": "allowed-id", "pre_rerank_score": 0.04, "final_rank": 1}],
  "candidate_digest": {
    "version": 1,
    "per_leg_limit": 20,
    "truncated": false,
    "legs": {"lexical": [{"id": "allowed-id", "rank": 1}],
             "dense": [{"id": "allowed-id", "rank": 1}],
             "exact": []},
    "pre_rerank": [{"id": "allowed-id", "rank": 1}],
    "final": [{"id": "allowed-id", "rank": 1}]
  }
}
~~~

`rerank_mode` is the compact traffic-segmentation label (`disabled`,
`requested_not_applied`, or `applied`); the nested object retains the exact
request/application details. The record contains raw query text because replay
needs it; it contains no note
body, snippet, classification, or withheld ID. The digest uses the egress-safe
shape above. The index’s vault fingerprint is a hash of the final sorted
path/content-hash projection, not a snapshot generation or a timestamp. Rebuild
already writes that kind of fingerprint. Incremental sync must recompute and
commit it in the same transaction for additions, edits, deletion propagation,
moves, and rebases; otherwise replay cannot distinguish vault drift from
ranking drift honestly.

Retention is BRAIN_QUERY_LOG_RETENTION_MONTHS with a default of 3. The
existing maintenance sweep fold deletes expired, whole YYYY-MM.jsonl files; it
never rewrites, compacts, or truncates the live month file while unlocked
appenders may be writing it. This is a fold in the existing maintenance
umbrella, not a new scheduled task.

Replay is a host-broker-only command, refused before core construction on a VM
in the same way as supersede:

~~~text
brain eval replay --against <month-file>
~~~

It never appends a new capture record. Its default result is report-only:
top-1 stability, Jaccard at k, rank movement, candidate-digest presence, and
latency delta. Records with the same vault fingerprint are “vault_same” and
are comparable ranking signals. Records with a different fingerprint are
“drift_or_mixed”; they may be reported but cannot support a conclusion about
ranking regression, because the engine cannot infer why the vault changed.

Optional --fail-under-top1 and --fail-under-jaccard thresholds accept values
in [0, 1]. Replay exits nonzero only when at least one vault_same record exists
and its selected threshold is breached. It exits successfully with no
comparable records, while malformed logs remain a distinct data-error exit.
Replay is a risk signal for human review, not a merge gate or an automatic
release veto.

### 5. Frozen clean-room golden families and gate procedure

The following tracked, synthetic-only artifacts are frozen in this design
session:

| Artifact | Purpose |
| --- | --- |
| eval/fixtures/named-entity-golden.json | 16 named-entity query cases, carrying family and partial-phrase-bound assertions. |
| eval/fixtures/named-entity-qrels.json | Matching qrels for every frozen query. |
| tests/fixtures/named_entity_vault/ | A small Contoso/Northwind/Fabrikam-only corpus that exercises aliases, titles, generic notes, collisions, and chunk dilution. |
| eval/runs/ne-family-freeze.json | Query IDs and SHA-256 hashes of the golden and qrel fixtures, frozen before ranking implementation. |

The families are:

| Family | Query IDs | Cases | Required behavior |
| --- | --- | ---: | --- |
| alias-synonym | ne_alias_01 through ne_alias_03 | 3 | Unique aliases resolve through normalization, including NFC/NFD equivalence. |
| alias-collision | ne_collision_01 through ne_collision_03 | 3 | A collision is ordered deterministically, preserves live-before-retired within its reranked slots, and never creates create_safety = exists. |
| title-exact | ne_title_01 through ne_title_03 and ne_title_partial_bound_01 | 4 | Full titles qualify for the full tier; a 60%-floor partial phrase cannot displace a rank-1 both-leg anchor. |
| generic-vs-named | ne_named_01 through ne_named_03 | 3 | Specific named identity wins appropriately over generic semantic material without turning generic results into identity proof. |
| chunk-dilution | ne_chunk_01 through ne_chunk_03 | 3 | A relevant entity embedded in a longer note remains attributable through best-chunk pooling. |

Every entity is synthetic. The normal owner golden set and qrels are intentionally
ignored because they may contain private vault material; adding real codenames
to a committed fixture is a known contamination route that has already shipped
in a wheel. The clean-room adjunct avoids changing or exposing that owner data.

The exact gate procedure is:

1. Verify eval/runs/ne-family-freeze.json against both fixture files before
   taking a baseline. Any hash mismatch stops tuning.
2. Build the augmented evaluation input by combining the local/private baseline
   only where available with this clean-room corpus, then capture the baseline
   before exact-leg ranking code changes. Run with rerank off and exact leg
   disabled. The captured baseline must contain every new query ID.
3. Verify the freeze hashes again, implement against the same augmented input,
   and capture the candidate run with rerank off for the primary ranking gate.
4. Run the ordinary gate. Because gate.py computes paired results with the
   intersection of baseline and candidate IDs, authoring the family after the
   baseline would silently exclude it from all statistics.
5. Compute and print per-family paired deltas explicitly by the fixture’s
   family field. The current harness has overall, language, and class
   aggregates (not merely language), but it has no family stratum; a
   named_entity class aggregate is not a substitute for each family.
6. Run separate assertions for the partial-phrase bound, collision
   create_safety, exact-leg-disabled baseline equivalence, and an adversarial
   reranker that attempts both to demote a uniquely pinned identity and to put
   a retired collision owner ahead of its live successor.
7. Record a deliberately regressed candidate as a negative control and confirm
   the expected gate or invariant failure; then restore the intended candidate.
8. Verify the freeze hashes once more before commit.

The fixture and qrel family is authored before constants are implemented so an
improvement claim cannot become near-tautological. The manifest is committed
despite eval/runs normally being ignored, making the freeze reviewable.

## Consequences

**Positive.** Exact lookup becomes a bounded, inspectable ranking signal rather
than a hidden score hack. Agents receive a conservative create/no-create
signal. Replay can distinguish a stable vault from an honest unknown-drift
case. The fixture freeze is portable and contains no owner names.

**Negative.** The schema and sync path gain an alias projection and stricter
configuration validation. Exact behavior has a deliberately narrow
final-order pin, which needs tests against duplicate suppression and reranking.
Raw-query logs are sensitive host data and need permissions, retention, and
host-only replay discipline.

**Risk accepted.** A hand-authored alias can be stale or incomplete. The
collision-safe create_safety table makes that failure conservative: it can
produce probable or unknown, but it cannot turn ambiguity into exists.

## Session implementation notes

- The current zone-weight configuration intentionally accepts anti-burial boosts
  such as 2.0 as well as damps such as 0.55. The three-leg ceiling is therefore
  a raw-RRF invariant; final post-zone scores are parameterized by the applied
  zone maximum so exact-leg rollback preserves existing owner behavior.
- The reranker really sorts its entire head by cross-encoder score. Collision
  ordering is consequently restored by refilling only collision-owner slots
  after reranking, which keeps a live successor ahead of its retired version
  without pinning the collision group above unrelated results.
- Full identity ownership is calculated before egress. If any owner is
  withheld, surfaced results receive create_safety=unknown rather than a
  collision count or a false exists conclusion.
- config.secure_file_permissions is deliberately best-effort; a raw-query log
  needs explicit mode/ACL verification and disables itself on failure.
- The existing golden/qrel paths are ignored owner-vault artifacts. The
  committed clean-room adjunct is intentionally separate so freeze evidence
  does not publish owner terms; later evaluation combines it locally only.
- The evaluation harness has class aggregates as well as language aggregates,
  contrary to the narrower initial description, but neither is a per-family
  statistic. The explicit family calculation remains necessary.

## Non-goals

This ADR does not introduce:

- typed graph edges;
- schema packs;
- volunteer or push context;
- any new scheduled task;
- alias auto-derivation;
- a vault-resident query log;
- an automatic decision promotion from raw material; or
- engine implementation in this design session.

Automatic alias derivation may be proposed later as a bounded maintenance fold
with its own validation and provenance contract. It is not silently implied by
the hand-authored alias exception here.
