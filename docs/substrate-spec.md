# Substrate specification — the Brainiac second brain

**Status:** spec of record for SESSION S01 (SUB-01 / SUB-02).
**Derived from:** the project's internal architecture design note (v5).
(canonical; v1–v4 superseded but readable for rationale).
**Decision (2026-06-27, maintainer):** Brainiac **supersedes** Obsidian + Smart
Connections as the retrieval substrate. "Beats today" = beats the current
Obsidian + SC vault baseline. Corpus migration/cutover from the current vault is
in scope (see §7 + `corpus-migration.md`).

---

## 1 · Principles

1. **Markdown + YAML is the single source of truth.** Files on disk are
   authoritative. The sqlite index (resolved host app-data, or
   `$BRAIN_INDEX_DIR`) is a *derived cache* — deletable, rebuildable from
   `vault/` at any time. No database is ever the truth. *(One deliberate
   exception shares that directory and is NOT disposable: the COS approved
   queue, `<index dir>/cos-approved/` — owner-accepted content awaiting the
   signature, and the only copy until the drain runs. See `docs/cos-ops.md`
   §2c and AGENTS.md §1.)*
2. **Two zones.** `raw/` is an **immutable** inbox of captured sources;
   `brain/` is **agent-owned** atomic, densely-linked notes plus `index.md` and
   a generated `backlinks.md`.
3. **Flat, link-first.** Light PARA at the top of `brain/` is the *only* folder
   taxonomy. No Johnny-Decimal, no manual tag taxonomy. Structure = wikilinks.
4. **Classification everywhere, default-deny.** Every note carries
   `classification:`; unlabelled ⇒ treated as MNPI (most-restrictive) at the
   egress boundary. This drives the egress gate (S08).
5. **Egress is the security budget.** At-rest = FDE baseline; the real control
   is what `brain` is willing to surface to the model. (v5 §2–§3.)
6. **OKF is an optional lint profile, never the substrate.**

---

## 2 · File layout (authoritative)

```
vault/
├── raw/                 IMMUTABLE captured sources (append-only)
│   └── YYYY-MM-DD-<slug>.md     each carries sha256 + immutable: true
├── brain/               agent-owned notes
│   ├── index.md             maintained map (type: index)
│   ├── backlinks.md         GENERATED reverse-link map
│   ├── projects/            PARA — active, goal-bound work
│   ├── areas/               PARA — ongoing responsibilities
│   ├── resources/           PARA — reference / topics
│   └── archive/             PARA — inactive
└── .brain/              RUNTIME (gitignored): published snapshot, capture
                         inbox, graph output, memory, staged VM assets
```

- Within any PARA folder, notes are **flat** (`kebab-slug.md`).
- `raw/` ↔ `brain/` are linked by frontmatter (`source:`) and `[[raw/...]]`.
- The live host index and host query ledger are resolved outside `vault/` and
  `vault/.brain/` under host app-data. `.brain/snapshot/` is the VM-readable
  published copy, not the canonical writable index.

## 3 · The engine (`brain`)

- **Index:** sqlite-vec (dense vectors) + FTS5 (lexical), one `.sqlite` file.
- **Embeddings:** **bge-m3-int8** via ONNX (revision-pinned download;
  `Xenova/bge-m3`, exact `onnx/model_int8.onnx`), model mmap'd from the mount
  (never copied to `/tmp`). Bundle the model for Cowork — the VM egress
  allowlist excludes HuggingFace.
- **Four agent verbs:** `search`, `get`, `recent`, `draft_capture` (see §5 +
  AGENTS.md §5). `write_note` is host-broker-only. The CLI also exposes
  agent-facing read helpers (`hybrid-search`, `grep`, `bases-query`,
  `graph-expand`, `read`, `dossier`, `diagnose`) and host-broker maintenance
  verbs; `brain --help` is the current surface.
- **Retrieval ranking:** production hybrid search is RRF at `k=60`: FTS5 BM25,
  dense best-chunk vectors, and the ADR-0008 bounded exact alias/title leg. The
  exact leg is disabled immediately with `BRAIN_EXACT_LEG_ENABLED=0` plus
  process restart; no rebuild is required. `--rerank` is optional and skippable,
  bounded to 10-20 candidates, and keeps cross-encoder scores separate from RRF
  scores.
- **Zone-authority prior — a measured opt-in, neutral by default.** RRF rewards
  a document's presence in *many* legs over its strength in *one*, which buries
  cross-lingual hits the dense leg alone can reach. `BRAIN_ZONE_WEIGHTS`
  (JSON `zone -> multiplier`, e.g. `{"brain": 2.5, "raw": 1.0}`) applies a
  per-zone boost after fusion, only to dense-leg-only hits. Query-time only —
  no rebuild, reversible by unsetting it. It ships neutral because the 66-query
  golden set can establish the effect (held-out mrr@10 0.198 → 0.386,
  p=0.011) but not calibrate the constant — train argmax 3.0, held-out argmax
  5.0; `eval/FOLLOWUPS.md` #9 carries the evidence and the costs. A malformed
  map or an unrecognised `BRAIN_ZONE_SCOPE` warns on stderr and is dropped
  (scope fails safe to `semantic_only`); confirm what applied per hit with
  `search --explain --json` (`zone.applied` / `zone.factor`).
- **Build matrix:** host macOS + host Windows (Code-tab / terminal) **and**
  Linux aarch64 + x86_64 (Cowork VM). One codebase, four targets.

## 4 · Trust model — host / VM split (consensus hardening)

`brain` runs in two contexts with **different capability sets**:

| | Cowork Linux VM | HOST broker |
|---|---|---|
| Trust | sandboxed, **EDR-blind, not audit-logged**, ephemeral | EDR-visible, holds the Ed25519 audit key |
| Allowed verbs | `search`, `get`, `recent`, `draft_capture` | **all** of the above + `write_note` |
| Owns | nothing canonical | `write_note`, audit signing, WAL writes, snapshot generation, index commit |

**Rule:** the VM is a **read + draft** surface; the **host is the sole writer**.
The signing key and the canonical index mutation never live in the VM.

### 4.1 VM-draft → host-commit capture protocol (S06)

```
[VM]  brain draft-capture  ──writes──▶  .brain/capture-inbox/<id>.md
                                  status: draft
                                  provenance.trust: untrusted
                                  (no index / no WAL / no signature / no key)
                          │
                          ▼  (shared VirtioFS mount — host sees it)
[HOST] brain sync --publish  ──drain-on-invoke──▶
        1. validate frontmatter + classification (default-deny on missing)
        2. compute sha256 of body
        3. promote → raw/ (source) or brain/resources/ (note)
        4. Ed25519-sign the audit-chain entry  (fails closed: no key ⇒ left in place)
        5. write WAL + commit to index.sqlite (incremental upsert, IDX-03)
        6. delete the draft
        7. atomically PUBLISH a new generation-id snapshot → .brain/snapshot/
                          │
                          ▼
[VM]  brain get <id>  →  the same note is now retrievable from the snapshot
```

A draft is **never authoritative** and **never surfaced by `search`** until the
host commits it AND republishes the snapshot. The VM reads ONLY the read-only
snapshot and exposes `brain status` (snapshot generation + age + pending drafts)
so staleness is a surfaced state, not a silent loss.

**No capture daemon, no dedicated drain task.** The host drains *on invoke* (every
host `brain sync`). The **one** sanctioned scheduled task is the ux-02 morning
brief (s09), which doubles as the guaranteed daily drain floor.

**VM read+draft-only is enforced** (`role=vm`): the VM binary cannot write notes,
cannot open the index in WAL/write mode, and cannot resolve a signing key — hard
tests in `tests/test_integration.py`.

## 5 · Egress gate and read-surface observability

`search`/`get`/`recent` filter results by `classification` against the caller's
allowed tier. **Default-deny:** a note whose `classification` is missing or not
in the recognised set is treated as **MNPI** and withheld. Full scheme,
ordering, and tier semantics: `classification-scheme.md`. The gate is the
mechanism S08 builds on.

Every surfaced `search`/`hybrid-search` hit carries both retrieval source
(`lexical`, `semantic`, `both`, or exact-only `exact`) and ADR-0008 identity
evidence:

- `alias_hit`
- `exact_title_match`
- `title_phrase_match`
- `keyword_exact`
- `high_vector_match`
- `weak_semantic`

`create_safety` is derived from that evidence and the complete pre-egress
alias/title owner set. It is one of `exists`, `probable`, or `unknown`.
`exists` is reserved for exactly one visible full alias/title owner. Shared
aliases, title/alias collisions, retired-version collisions, and any full owner
withheld by the egress gate never expose owner counts or hidden ids; the public
answer degrades to `probable` or `unknown`.

`search --explain` and `diagnose` are read-only observability surfaces over the
same production ranking. `--explain` emits gated per-hit attribution and a
bounded candidate digest containing only ids already allowed through egress.
`diagnose` runs the production path unchanged and then reports the requested
target's stage presence/rank/cutoff. If the target is above the egress cap, the
only target identity printed is the sentinel `withheld`.

Host query capture is deliberately outside the vault. On the trusted host, the
post-egress capture lane appends raw query records under the resolved host
app-data index directory at `query-log/`, with owner-only directory and file
permissions. It refuses symlink or environment overrides that resolve below
`vault/` or `vault/.brain/`, records query-free health counters separately, and
retains only whole month JSONL files. VM role cannot read, write, replay, or
resolve the host ledger path.

`brain eval replay --against <month.jsonl>` is host-only and never appends new
capture rows. Replay reports top-1 stability, Jaccard@k, rank movement,
candidate-digest presence, and latency delta. Because the log contains no
target qrels, thresholds are honest only over `vault_same` rows whose live
index fingerprint still matches the capture; changed fingerprints are reported
as `drift_or_mixed` and remain non-gating.

## 6 · At-rest posture (v5-corrected)

- **Baseline:** FDE (FileVault/BitLocker) + OS file permissions — sufficient for
  single-user local.
- **Conditional app-encryption**: the shipped AES-256-GCM module protects
  **backups only** (`brain backup`) today — the live index/vault/audit chain
  rest on the FDE baseline (see `docs/security-overview.html` §6.8). The
  flip-list for wanting more remains: off-device backup/sync, regulated data
  (PCI/MNPI/PII regime), multi-user machine, or a cyber-team mandate.
  **Encrypt any off-device backup.**
- Budget goes to **egress**, not broad at-rest encryption (v5 §2–§3).

## 7 · Substrate readiness ≠ operational cutover (scope guard)

This spec makes Brainiac **ready** to replace Obsidian + Smart Connections. It
does **not** perform the live swap. Within these 10 sessions, "migration/cutover
in scope" means precisely:

1. **Corpus migration (S03)** — import the existing Obsidian/Johnny-Decimal
   vault into the flat/PARA + `classification:` substrate at scale
   (`corpus-migration.md`).
2. **Emit operational-cutover HOOKS** for a *separate follow-on plan* — the
   dependency-inventory **shape** (`dependency-inventory.md`) that S10 populates,
   listing every control-plane surface that today names Obsidian/Smart
   Connections.

It does **NOT** mean swapping the operating model (CLAUDE.md, the 14 P-rules,
the retrieval-cascade rule, the 8 Bases, the ~10 scheduled tasks, the SC health
tripwire) inside these sessions. **Substrate readiness is not operational
cutover** — say so plainly.

## 8 · Validation

`tools/validate.py` enforces the conventions: required frontmatter, allowed
`classification` values, default-deny reporting, immutability markers on `raw/`,
no Johnny-Decimal filenames, presence of `index.md`, and (optionally)
`--backlinks` regeneration and `--okf` lint. A clean run (exit 0) is the gate.

### 8.1 · Bitemporal frontmatter (ADR-0003 ruling 2)

### 8.0 · Aliases (ADR-0008)

`aliases:` is an optional brain-zone-only frontmatter list for owner-curated
identity strings, distinct from `tags:`. Each alias is stored as authored and
indexed through a normalized identity projection (NFC, casefold, whitespace
collapse) beside normalized titles. A note may carry 0-128 aliases, each
non-empty and at most 256 Unicode scalar values. Duplicate aliases within one
note are validation errors after normalization; aliases shared across notes are
warnings, because historical and supersession collisions are legitimate.

Retrieval computes alias/title ownership over the complete pre-egress index.
That means a hidden owner can never make a visible collision look uniquely safe:
`create_safety: exists` is emitted only for one visible unique full owner, while
shared or withheld owners degrade to `probable` or `unknown` without leaking
hidden ids, titles, counts, ranks, or a collision label.

### 8.1 · Bitemporal frontmatter (ADR-0003 ruling 2)

Seven **optional** keys (`document_date`, `effective_date`, `superseded_date`,
`is_latest_version`, `superseded_by`, `previous_version`, `replaces` — full
schema and edit-vs-supersede rule in AGENTS.md §2) let a note distinguish when
it was produced from when its claim takes effect, and chain to a successor
when the world changed under it. Existing notes carrying none of these keys
validate exactly as before.

When present, `tools/validate.py` checks:

- **type/format (errors):** dates ISO-8601; `is_latest_version` a real
  boolean; `superseded_by`/`previous_version`/`replaces` resolve to an
  existing note id.
- **per-note consistency (errors):** `is_latest_version: false` requires
  `superseded_by`; `superseded_date` requires `superseded_by`; a note may not
  supersede itself.
- **chain invariants (errors, whole-vault):** no cycles, no forks (two
  successors claiming the same predecessor / re-superseding an
  already-superseded note), at most one `is_latest_version: true` per chain,
  and **both sides of every supersession link must carry an explicit
  `classification`** — a missing label on either end fails loudly rather than
  silently defaulting.
- **warn-only:** a missing reciprocal `previous_version`/`replaces` on the
  successor when the predecessor declares `superseded_by`.
