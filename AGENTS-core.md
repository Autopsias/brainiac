# AGENTS.md — Profile A `brain` conventions (read this at startup)

> This is the **single conventions file** the assistant reads before doing any
> work in this repo. It defines the note shape, link style, capture rules, the
> four interactions, and the security posture. It is the substrate contract —
> if a tool, agent, or human and this file disagree about *shape*, this file
> wins. Behaviour/specs that need more room live under `docs/`.

> **Retrieval non-negotiables (details in §5):** every hit carries `type` —
> a `decision` hit IS the decision layer, a `source` hit is material under
> consideration. Decision-state questions ("what have we decided", "current
> state of X") route **`brain dossier "<question>" --json`** — the one-call
> sweep returning the decision layer and sources SEPARATED, each decision
> carrying `tensions` (newer sources post-dating it: report the tension,
> never promote the proposal), retired versions pre-excluded
> (`bases-query --where type=decision --latest-only` remains the raw
> probe). A newer raw source NEVER overturns the decision layer on its
> own; react to the `freshness` block and the egress `hint` instead of
> concluding the vault is thin.

This repo is **Profile A** — a local, any-LLM second brain whose **substrate is
plain Markdown + YAML frontmatter**. It is being built to **supersede Obsidian +
Smart Connections** as the retrieval substrate. Retrieval,
embeddings, and indexing are owned by a `brain` engine (sqlite-vec + FTS5 +
bge-m3-int8 embeddings), not by an Obsidian plugin. Design of record:
`docs/substrate-spec.md`. (Unfamiliar term below? Check `docs/glossary.md`.)

---

## 1 · The substrate in one screen

```
profile-a-brain/
├── AGENTS.md            ← you are here (conventions + schema)
├── docs/                ← specs (substrate, classification, migration, deps, OKF)
├── overlay/             ← the GENERIC per-user personalization template +
│                           worked example; the active overlay lives
│                           at <vault>/overlay/, see below
├── templates/           ← kernel note templates, one per typed entity
│                           (placeholder-only); overlay override:
│                           <vault>/overlay/templates/<type>.md
├── tools/validate.py    ← conventions validator (run before commit)
└── vault/               ← the data (this is the second brain)
    ├── raw/             ← IMMUTABLE captured sources (append-only, never edited)
    │   └── originals/       ← immutable archived binaries ingested from inbox/
    │                           (write-once, non-`.md`; `<date>-<slug>/<file>`)
    ├── brain/           ← agent-owned atomic notes, densely wikilinked
    │   ├── index.md         ← hand/agent-maintained map of the brain
    │   ├── backlinks.md     ← GENERATED reverse-link map (do not hand-edit)
    │   ├── projects/        ← PARA (the ONLY folder taxonomy; flat within)
    │   ├── areas/
    │   ├── resources/
    │   └── archive/
    ├── inbox/           ← ingestion DROP ZONE (gitignored, never indexed);
    │                       unknown extensions quarantine to inbox/_quarantine/
    │                       (ADR-0003 Ruling 1)
    ├── overlay/         ← THIS owner's personalization layer (voice/brand/
    │                       keywords/people) — see `overlay/README.md` for the
    │                       schema; `brain init --validate-overlay` checks it
    ├── cos-ops/         ← MACHINE OUTPUT (run reports, review-gate drafting
    │                       workspaces, decision cards) — NEVER indexed and
    │                       never validated; operational artifacts, not
    │                       knowledge (INT-03, see below)
    └── .brain/          ← per-vault runtime: published snapshot, capture
                            inbox, routines copy (gitignored). The live
                            index.sqlite + audit chain live in the per-user
                            app-data dir (`config.index_dir()`); override
                            with $BRAIN_INDEX_DIR per vault.
                            ├── memory/      ← session memory: handoff.md,
                            │                  hot.md, lessons.md, archive/
                            │                  (host-only, never indexed — §9)
                            └── graph/       ← graphify build output
                                               (graph.json + manifest.json,
                                               `authoritative: false` — §5)
```

**The overlay** (per-owner voice/brand/keywords/people layer, `docs/glossary.md`)
**is the only place owner identity lives.** `vault/brain/` and
`vault/raw/` carry no hard-coded voice/brand/people content — a new owner
fills in `vault/overlay/{voice,brand,keywords,people}/` (starter scaffold:
`overlay/template/`) and every drafting-facing kernel skill reads from
there instead. `brain init --validate-overlay` (minimal slice — full
`brain init` orchestration is a later session) detects and shape-checks the
active overlay; it never depends on the index, so it works on a brand-new
install before one exists. Full schema: `overlay/README.md`.

**Two zones, two rules:**

| Zone | Owner | Mutability | Rule |
|---|---|---|---|
| `vault/raw/` | capture only | **immutable** | Sources land here once and are never edited or deleted. A note that needs to change is a `brain/` note, not a raw edit. |
| `vault/brain/` | the agent | mutable | Atomic notes, one idea each, densely wikilinked. `index.md` + `backlinks.md` keep it navigable without folders. |

Markdown + YAML is the **single source of truth**. The sqlite index (per-user
app-data dir, or `$BRAIN_INDEX_DIR`) is a *derived cache* — deletable and
rebuildable from `vault/` at any time. **OKF is an optional lint profile (`docs/okf-lint-profile.md`), not the
substrate** — never required to read or write a note.

---

## 5 · The four interactions

The `brain` engine exposes exactly four verbs. Everything an agent does maps to
one of these:

| Interaction | What it does | Privilege |
|---|---|---|
| **search** | semantic + lexical retrieval over `brain/` (sqlite-vec + FTS5) | read |
| **get** | fetch one note by `id` (+ its backlinks) | read |
| **recent** | list recently created/updated notes | read |
| **draft_capture** | stage a *candidate* note/source (NOT committed to the index) | quasi-write |

`write_note` (the real commit: sign + index + WAL) is **NOT** one of the four
agent-facing verbs — it is a **host-broker privilege** (the trusted
host-side-only role, §6). The retrieval verbs
honour the **classification egress gate** (the deny-by-default filter applied
just before stdout, below).

> **Four verbs ≠ the whole CLI.** The table above is the **agent-facing trust
> surface** (what an untrusted/VM leg may invoke). The shipped `brain` CLI also
> carries **host-broker / maintenance** commands — `write` (the host-side commit
> = `write_note`, audited), `rebuild` (regenerate the disposable index),
> `project` (filtered-workspace containment), and `verify-audit` — none of which
> an untrusted leg should hold. The **authoritative, always-current command list
> is `brain --help`**; this table governs *privilege*, not the full surface.
> `draft_capture` is the VM-side capture verb (§6 VM-draft → host-commit), shipped
> as **`brain draft-capture`**: it stages a plain DRAFT into `capture-inbox/`
> and NEVER signs, indexes, or opens WAL. The host commit path is `brain write`
> (used by drain-on-invoke).


---

## 8 · Before you commit

## 8 · Before you commit

Run the validator from the repo root:

```bash
python3 tools/validate.py vault            # check conventions, default-deny report
python3 tools/validate.py vault --backlinks  # regenerate brain/backlinks.md
python3 tools/validate.py vault --okf      # also run the optional OKF lint profile
```

A clean validate (exit 0) is the conventions gate.

---

## Where the rest lives

This file is the ≤200-line core, imported by `CLAUDE.md` (`@AGENTS-core.md`).
`AGENTS.md` stays the full, unmodified canonical file for Codex/Gemini/the
Desktop Code tab. Claude Code loads the rest of AGENTS.md's content
CONDITIONALLY, only when a matching file is touched, from:

| File | Loads when touching | Covers |
|---|---|---|
| `.claude/rules/indexing-exceptions.md` | `vault/**`, `**/.brain/**` | indexing-scope exceptions (cos-ops, approved queue, corpus, runs) |
| `.claude/rules/note-shape.md` | `vault/brain/**`, `vault/raw/**`, `templates/**` | frontmatter schema, link style |
| `.claude/rules/capture-and-invariants.md` | `vault/raw/**`, `vault/brain/**`, `src/brain/invariants.py`, `tools/validate.py` | capture rules, corpus invariants |
| `.claude/rules/retrieval-and-security.md` | `vault/**`, `src/brain/**`, `eval/**` | the four interactions in detail, agentic tool surface, retrieval discipline, security posture |
| `.claude/rules/host-vm-trust.md` | `src/brain/**`, `vault/.brain/**`, cowork paths | host/VM capability split, capture protocol, single-writer discipline |
| `.claude/rules/pre-commit.md` | always | test-suite invocation, quality-ratchet detail |
| `.claude/rules/session-memory.md` | always | handoff/hot-log/owner-inbox contract |
| `docs/substrate-readiness.md` | read on demand | substrate readiness vs. operational cutover |

Full detail on any topic above: read `AGENTS.md` directly, or the linked
rules/docs file.
