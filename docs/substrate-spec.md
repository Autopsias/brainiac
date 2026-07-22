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
   authoritative. The sqlite index (`.brain/index.sqlite`) is a *derived cache*
   — deletable, rebuildable from `vault/` at any time. No database is ever the
   truth.
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
└── .brain/              RUNTIME (gitignored): brain binary, index.sqlite,
                         model.onnx, WAL, snapshots/, drafts/
```

- Within any PARA folder, notes are **flat** (`kebab-slug.md`).
- `raw/` ↔ `brain/` are linked by frontmatter (`source:`) and `[[raw/...]]`.

## 3 · The engine (`brain`)

- **Index:** sqlite-vec (dense vectors) + FTS5 (lexical), one `.sqlite` file.
- **Embeddings:** **multilingual-e5-small** via ONNX (revision-pinned
  download; `Xenova/multilingual-e5-small`), model mmap'd from the mount
  (never copied to `/tmp`). Bundle the model for Cowork — the VM egress
  allowlist excludes HuggingFace.
- **Four agent verbs:** `search`, `get`, `recent`, `draft_capture` (see §5 +
  AGENTS.md §5). `write_note` is host-broker-only.
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

## 5 · Egress gate (classification-driven)

`search`/`get`/`recent` filter results by `classification` against the caller's
allowed tier. **Default-deny:** a note whose `classification` is missing or not
in the recognised set is treated as **MNPI** and withheld. Full scheme,
ordering, and tier semantics: `classification-scheme.md`. The gate is the
mechanism S08 builds on.

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
