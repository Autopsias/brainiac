# S03 evidence — Index & embeddings (IDX-01 / IDX-02 / IDX-03)

**Session:** S03 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core (`brain` engine, sqlite-vec/FTS5 adapter, Ed25519 audit).
**Model note:** executed on Opus. Real ONNX embedding path proven live (no PyTorch).

This is the evidence index the plan's `require_evidence` gate consumes. Every
artifact listed exists on disk and is non-empty.

## What shipped (design v5 §3–4)

### IDX-01 — Arctic-embed via fastembed/ONNX (no PyTorch), MRL-256
`src/brain/embed.py`. `ArcticEmbedder` runs **snowflake-arctic-embed-m-v2.0**
(model of record; `model_id`+`dim` exposed) locally via `fastembed.TextEmbedding`
over **ONNX Runtime — no PyTorch**. Vectors are MRL-truncated to **256** and
re-normalised (`mrl_truncate`). The canonical task prefix `query: ` is applied to
queries only (asymmetric retrieval) and is **never translated**. The index stores
`embed_model` + `embed_dim` in `meta`; a mismatch on `sync` **forces a clean
rebuild** (Arctic and HashEmbedder vectors must never mix). The deterministic
offline `HashEmbedder` remains the test/CI fallback; `get_embedder("auto")`
prefers Arctic when its runtime is importable.

- **No-PyTorch proof:** the embedding venv has only `fastembed`, `onnxruntime`,
  `numpy` — `import torch` fails. See `_evidence/s03/xlingual-probe.json`
  (`"torch_present": false`).
- **fastembed-catalog caveat (honest):** fastembed 0.8.0's built-in catalog does
  not yet list `arctic-embed-m-v2.0` (only v1.x English models). Running the exact
  v2.0 model means either a newer fastembed that lists it, or registering it as a
  custom ONNX model (`TextEmbedding.add_custom_model`) from the bundled export.
  The **code path is model-agnostic**; the live probe exercises it with the
  catalogued multilingual ONNX model `paraphrase-multilingual-MiniLM-L12-v2`.

### IDX-02 — Chunking + in-language contextual prefix
`src/brain/chunk.py`. Notes are split at **section/block level** (heading-scoped
blocks, soft 900-char target / 1400 ceiling) — whole notes are **not** embedded.
Each chunk is embedded with an **Anthropic-style contextual prefix written in the
chunk's OWN language** (`detect_language` → EN/PT/ES; PT chunk ⇒ PT blurb). The
canonical `query:`/`passage:` token is separate (added by the embedder) and never
translated. The index keys vectors by **chunk rowid** and folds the best chunk
per note up to a note-level hit (`src/brain/index.py:search`).

### IDX-03 — Incremental upsert by path+hash + delete-propagation
`src/brain/index.py:sync` + `src/brain/notes.py` (`content_hash` = sha256 of the
full file). `sync` re-indexes only notes whose **path is new or content-hash
changed**, leaves unchanged notes untouched, and **propagates deletes** (a note
whose file vanished is removed, chunks+vectors+FTS included). No full rebuild.
`src/brain/vectors.py` gained per-rowid `delete()` on both backends.

## Hardening obligations → where satisfied

| Hardening tag | Obligation | Evidence |
|---|---|---|
| consensus | authoritative writable index on HOST; ship READ-ONLY snapshot to VM; WAL/VirtioFS caveat; rebuildable | `src/brain/snapshot.py` (atomic publish + WAL-checkpoint + 0444), `docs/operations/s03-evidence.md` §snapshot |
| r2-codex | snapshot publisher **atomic + generation-id manifest**; `brain status` reports generation+age | `src/brain/snapshot.py`, `brain status`/`snapshot` CLI, `tests/test_s03_indexing.py::test_publish_snapshot_atomic_and_generation_increments`, `_evidence/s03/cli-smoke-s03.txt` |
| claude / codex-verify-r1 | corpus migration + bulk classification BEFORE eval; **evidence-gated** coverage % + mislabel rate + human spot-review | `tools/migrate_corpus.py`, `_evidence/s03/migration-coverage.json`, `_evidence/s03/migration-manifest.jsonl`, `_evidence/s03/classification-spot-review.md` |
| r2-codex | bulk-classification **pass thresholds** (100% labelled-or-excluded; ZERO known Restricted/Secret false-negatives; quarantine uncertain; named sign-off) | `_evidence/s03/classification-spot-review.md`, `tools/fn_sweep.py` + `_evidence/s03/fn-sweep.json` (whole-corpus false-negative bound) |
| drain-on-invoke (maintainer) | the incremental indexer IS the capture drain (host sign+classify+upsert); no daemon, no dedicated drain task | `src/brain/core.py:drain_drafts`/`sync`, `tests/test_s03_indexing.py::test_drain_on_invoke_*` |
| r2-codex | scheduled-task honesty | stated as **no capture DAEMON and no DEDICATED drain task** (the ux-02 morning brief is the one sanctioned task and the drain floor) — `src/brain/core.py:drain_drafts` docstring |

## Snapshot / host-VM split (consensus + r2-codex)

The **authoritative writable index lives on the HOST** (`%LOCALAPPDATA%` /
`~/Library/Application Support`, WAL, single-writer). `brain snapshot` publishes a
**read-only, generation-stamped** copy (`index.snapshot.sqlite` + atomically
written `snapshot.manifest.json`) that the Cowork VM mounts read-only in
`./.brain/`; **writers never run in the VM.** Publish is atomic (`os.replace`),
WAL-checkpointed first, chmod 0444, and the manifest (written last) always
describes a complete DB at its `generation`. **WAL+VirtioFS caveat documented:**
two concurrent Cowork sessions on one mounted index risk lock contention /
corruption — which is why the VM gets a *copy*, never the authoritative WAL DB;
the index stays rebuildable-from-Markdown so any corruption is recoverable.
`brain status` reports snapshot generation + age for the VM-side view.

## Corpus migration + bulk classification (evidence-gated)

Ran `tools/migrate_corpus.py` against the **real** Example Corp vault (read-only dry-run):

- **3318** content notes inventoried; **100.0%** labelled-or-excluded; **0**
  unlabelled; **1** quarantined → Secret. Tiers: Restricted 2094 · Confidential 549
  · Internal 632 · Secret 43. (`migration-coverage.json`, `migration-manifest.jsonl`.)
- **Human spot-review** (`classification-spot-review.md`): stratified 41-note
  sample, 2 rounds; found **4 false-negatives** (9.8% initial under-classification
  rate), all root-caused (body-only keyword scan; missing frontmatter + synonyms)
  and **remediated**.
- **Whole-corpus false-negative sweep** (`tools/fn_sweep.py` → `fn-sweep.json`):
  all 632 notes at/below Internal re-scanned for strong sensitivity tokens →
  **0 candidates**. ⇒ **ZERO known Restricted/Secret false-negatives.**
- ⚠ **Named human sign-off PENDING (maintainer)** — the one open item against the
  r2-codex sign-off clause; required for operational cutover, not for the
  substrate-readiness build. Recommend elevating migration to its own first-class
  session on the next plan rebuild (codex-verify-r1 NOTE).

## Live cross-lingual probe (no PyTorch) — `_evidence/s03/xlingual-probe.json`

EN query *"growth of sea-based renewable electricity generation"* over a
multilingual sample vault (EN/PT/ES notes with disjoint vocabulary), real ONNX
embedder: top hits **pt-renew (0.65) > en-renew (0.56) > es-renew (0.56)** — the
PT note outranks the English one with **zero shared tokens**. `PASS: true`
(cross-lingual PT+ES reached, chunks>notes, torch absent, incremental
add/update/delete, model-change-forces-rebuild).

## Tests — 56 passing
`python3 -m pytest -q` → **56 passed** (41 prior + 15 new in
`tests/test_s03_indexing.py`: embedder model_id/MRL/query-prefix; chunk
split/lang/contextual-prefix; sync add/update/unchanged/delete; model-change
rebuild; snapshot atomic+generation; drain-on-invoke promote + fail-closed).
`testpaths=tests` added so collection never descends into the model venv.

## CLI smoke — `_evidence/s03/cli-smoke-s03.txt`
`brain rebuild` (8 notes / **13 chunks** → chunking active), `brain sync`
(incremental, drain), `brain snapshot` (generation 1 + sha256 manifest),
`brain status`, `brain search` (chunk-backed hybrid, egress-filtered).

## Evidence file index
- `_evidence/s03/migration-coverage.json` — coverage %, tier/zone distribution, thresholds met
- `_evidence/s03/migration-manifest.jsonl` — per-note source→target + tier + rationale (3318 rows)
- `_evidence/s03/classification-spot-review.md` — human spot-review, mislabel rate, acceptance gate
- `_evidence/s03/fn-sweep.json` — whole-corpus false-negative bound (0 candidates)
- `_evidence/s03/xlingual-probe.json` — live ONNX cross-lingual + sync + no-torch probe (PASS)
- `_evidence/s03/cli-smoke-s03.txt` — CLI transcript of the new subcommands
- `_evidence/s03/grep-count.txt` — count-based proofs of engagement

## Known limitations / routed forward
- The exact `arctic-embed-m-v2.0` model is not in fastembed 0.8.0's catalog;
  bundle the ONNX export or register via `add_custom_model` at packaging time
  (S06/S07 host build). The probe proves the path on a catalogued multilingual model.
- LLM-assisted second classification pass + human sign-off on Restricted/Secret are
  downstream (corpus-migration Phase 3.2–3.4) — recommend a dedicated migration
  session next rebuild.
- sqlite-vec chunk-vector backend exercised via brute-force fallback in this env;
  sqlite-vec path is unchanged from S02 and works when the extension loads.
