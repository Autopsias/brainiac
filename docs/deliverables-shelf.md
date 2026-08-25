# The deliverables shelf

> The folder where the latest version of every output you produced from vault
> content sits, grouped by project, so you can find it in Finder without
> knowing a single command. It is **generated** — the nightly rebuilds it, and
> nothing you do to the folder itself is ever read back as authority.

Design of record: [ADR 0010](adr/0010-deliverables-shelf-outside-the-vault.md).
Capture rule (what makes a note a deliverable in the first place): AGENTS.md §4,
mirrored for Claude Code at `.claude/rules/capture-and-invariants.md`.

---

## 1 · What it is

One real copy per **marked, non-retired deliverable**:

```
<shelf>/
├── README.md                 ← generated index: what is here, newest first
├── shelf-manifest.json       ← published advisory view (never read for authority)
├── <project-slug>/           ← one folder per project
│   └── <payload filename>
└── _previous/<run-id>/       ← displaced copies; nothing is ever deleted
```

A note is a deliverable when its frontmatter carries `deliverable: true` (plus
an optional `project:`). The copy on the shelf is the **payload**: the archived
original the note's `source:` points at when there is one, otherwise the note's
own `.md`. `type:` is never changed to mark something — `type: decision` *is*
the decision layer, and retagging would mark the file by removing it from
retrieval.

Its **effective tier is the higher of the note's and its payload's**, and the
README states the highest tier present at the top. The folder is owner-only
(`0700`/`0600`, re-proved every run).

## 2 · Where it lives, and why it is outside `vault/`

Default `<vault>/../brain-deliverables`; override with
`$BRAIN_DELIVERABLES_DIR`. `brain status` prints the resolved path.

`vault/` is walked by machinery that treats every `.md` under it as a note
unless a rule says otherwise. A tree of copies inside it would have needed a
permanent exclusion in `scan_vault`, in `backup.py`, in `brain project --dest`,
and in every whole-vault walker written from then on — each omission a silent
leak. It would also have fed `auto_dedup_tier1` a byte-identical twin of every
Markdown deliverable and let that fold retire the real note against the
convenience copy. Outside the tree, none of those are guards that can be
forgotten; they are absent problems.

The resolver **fails closed** rather than guessing, and every refusal names
`$BRAIN_DELIVERABLES_DIR` and its recovery in the message. It refuses when the
target's parent is your home directory; when the parent is a git working tree
root and the override was not set explicitly; when the target collides with an
existing non-empty directory the binding registry does not name; when the vault
has no stable id; and when the target is already bound to a **different** vault
(one host-private registry, first claim wins — two sibling vaults under one
parent would otherwise each displace the other's entries).

A refused shelf is **not placed at all**. There is deliberately no fallback
inside `vault/`.

## 3 · How it is maintained

`deliverables_shelf_fold` runs on every `brain-nightly` firing — a fold, not a
second scheduled task, so it rides the umbrella's run lock, state file and
health history. Each run:

1. takes the census (every marked, non-retired note with a resolvable payload);
2. plans one shelf path per entry, suffixing `(<note-id>)` on **all** members of
   a basename collision so the result never depends on scan order;
3. writes the ledger **before** touching a file, claiming every path the run
   could write and recognising both the old and the new bytes;
4. copies what changed, and **moves** — never deletes — anything displaced into
   `_previous/<run-id>/`.

Three guards you will meet in the report:

- **`diverged`** — the bytes on disk are not the fold's. An owner edit or a
  hand-dropped file sitting exactly where a deliverable wants to go. The fold
  touches neither side and carries the ledger row forward so it stays visible.
- **`unknown`** — a file on the shelf no ledger row claims. Reported, never
  touched. (`_previous/` is excluded from this report.)
- **`moves_held`** — a blast-radius cap. A run may replace or retire at most a
  fifth of the ledger (floor 5); a census that reads **zero** over a non-empty
  ledger is the signature of a failed read, not an emptied vault, and holds the
  moves outright. `$BRAIN_SHELF_MAX_MOVES` is the explicit escape.

Three corpus invariants trend the mechanism in `health-history.jsonl`:
`unshelved_deliverables`, `stale_shelf_entries`, and
`unanchored_deliverable_payloads` (drop-lane payloads that never got an anchor —
the one metric that can still see a failure after marking stops entirely).

**Never hand-edit the shelf.** Edits to `README.md` are overwritten on the next
run, and an edited payload turns into a `diverged` row that stops the fold from
maintaining that entry at all. Change the note, not the copy.

## 4 · The accepted trade-off: Spotlight and Finder show both

The shelf holds **copies**, so macOS indexes the vault note *and* its shelf copy
independently and a Spotlight search returns both. That is accepted, not fixed.

Symlinks would collapse the two, and were rejected: they break on Windows, on
cloud sync, and across the Cowork VirtioFS mount. Hard links were rejected too —
editing the shelf file would edit the archived original, and `raw/` is immutable
by contract.

If the duplication bothers you on a particular machine, exclude the shelf
directory from Spotlight (System Settings → Siri & Spotlight → Search Privacy).
The vault copy stays indexed.

---

## 5 · Backfilling an existing vault — the repeatable procedure

A vault that has been in use before the marker existed holds deliverables
nobody ever marked. This is the procedure that was run against the reference
vault on 2026-08-24; follow it exactly for the next one. Evidence from that run:
`_evidence/s06/backfill-proposal.json` and `_evidence/s07/shelf-after.md`.

### 5.1 Scan — propose, write nothing

Walk **both** populations and emit ONE proposal file:

- **the vault**, for notes that are produced outputs but carry no marker;
- **the owner's own finals folder**, if one exists, for documents the vault
  never ingested.

Rubric — a row is a candidate when it is a **document produced from vault
content about the business**. Exclude, each with its reason recorded in the
proposal so the owner can audit the *exclusions* as well as the candidates:
notes whose authorship cannot be established; entity and generated maps;
knowledge notes with no underlying document; operational vault or tooling
records; payloads that do not resolve; ingested third-party material; production
run records; and any dot-directory of tool state (`.trust/`, `.memo-loop/`) —
that is machine state, not a document.

Every candidate row carries, and the proposal is worthless without them:

| Field | Why |
|---|---|
| `note_sha256` | the note file as approved |
| `frontmatter_sha256` | the frontmatter block alone |
| `payload_sha256` + `payload_kind` | **the archived bytes** — a raw note's own `sha256:` is over extracted Markdown, so hashing the note leaves the approved bytes unbound |
| `audit_head` (batch-level) | the chain tip the decision was made against |
| `title`, `type`, `note_path` | so a substituted note contradicts the row |
| proposed `project` and `classification` | a folder-row with no reasoned tier **fails closed to MNPI** — the drop lane declares `Internal` and the tier guard only raises against an existing higher-tier twin, so an undeclared unique synthesis would enter Internal and reach an Internal-capped VM |

Also record a **complete before-image of the owner's folder** — every file,
including the excluded dot-directories — with a per-file sha256 and one tree
digest. Without every row, "copy, never move" is unverifiable.

Version families (`…-v14`/`…-v15`) are **proposed**, never applied. CUR-01: only
an owner accept may link two documents as a supersession.

### 5.2 Owner review — one decision card

Give the owner the counts, the exclusions with their reasons, and the specific
costs of saying yes (superseded drafts landing beside the current version; one
document reachable under several note names). The answer is a ruling recorded in
the plan's review log, because a checkpoint passes no text to the session that
executes it.

### 5.3 Preflight — one complete pass, over both populations, writing nothing

Everything is verified before anything is written. An abort here is cheap; a
half-applied mass write to a live vault is not.

- every vault row: the id resolves, **all three hashes** still match, the title
  and type do not contradict the row, the note is not already retired;
- the **batch audit head** still equals the live chain tip. If the chain has
  moved, that is an **abort, not a re-bind** — never re-stamp rows against a
  newer head to make a batch apply;
- every folder row: still present, bytes and size unchanged;
- **admissibility through the lane's own dry run** — `handler_for` →
  `handler.available()` → `read_nofollow` → `_extract_verified`. "A handler
  claims this suffix" is not the admission test: it misses an unavailable
  dependency and an extraction that quarantines, and a permanently inadmissible
  file discovered mid-ingest is discovered after approved writes have landed;
- the **shelf side**: resolve it (the resolver can refuse), read the ledger, plan
  the moves, and check the run against the move cap;
- the folder tree digest, compared **here**, before anything is written.

Probe each ad-hoc check with a **known positive** before trusting its all-clear —
a check that returns clean because its input was empty is worse than no check.

**Abort condition.** If any row fails, write the complete mismatch list and stop
without writing anything. Do not repair the proposal. Do not apply the part that
verified.

### 5.4 Apply — one audited batch

Stamps and any approved supersessions go through **one batch verb**
(`deliverables_apply.apply_batch`), not a sequence of individual writes:
`brain write` does not take the writer lock and each `supersede` takes its own,
so the obvious way is N unsynchronised transactions the hourly nightly can
interleave with. The batch takes the outer writer lock **once**, re-checks every
recorded hash and the audit head **under it**, and keeps a per-row journal whose
`pre`/`post` images let an interrupted run resume forward without ever
re-stamping a note something outside the batch has changed.

The stamp is **additive** — `deliverable: true` plus `project:`, and `type:` is
never rewritten. That is what keeps a mass stamp reversible by deleting two
lines.

### 5.5 Absorb the owner's folder — through the ordinary drop lane

Copy each approved file into `vault/inbox/_deliverables/<project>/` and run the
normal drain (`deliverables_absorb.absorb`). No bespoke import path: a second
ingest route is a second thing to keep correct forever.

- **Copy, never move.** The owner's folder is left exactly as found; the undo is
  that the original was never touched.
- The row's tier is declared to the lane the only way the lane accepts one — a
  `.classification` file beside the payload. That marker is **folder-wide**, so
  the preflight refuses if the lane holds any payload this batch did not stage,
  and the marker is restored in a `finally` so a failed copy cannot leave one
  governing the folder.
- A **byte-identical duplicate is not re-ingested, and not simply skipped
  either**: it still needs the brain-zone anchor that puts it on the shelf.
  "Already present" describes the payload, never the outcome.
- `raw/` is immutable, so a failure at row 90 of 120 cannot be rolled back. The
  driver's guarantee is **resumability**, and the row-done test is the vault's
  own `ingest-manifest.json`, never the driver's journal — a journal that was
  the sole authority would be a second opinion about the vault's contents, and
  the disagreement would surface as a double ingest.

### 5.6 Fold and verify — render it, never a count

Run the shelf fold and read **its own report**. Then render the result as the
owner will see it: `ls -R` of the resolved shelf plus the generated README,
quoted into evidence. A green exit code is not evidence the operation succeeded.

Check, in this order:

1. the fold's `copied` / `diverged` / `unknown` / `ledger_entries` agree with the
   rendered listing. On a first run there should be no `diverged` and no
   `unknown` — either one means investigate before going further;
2. `verify-audit` is clean (chain ok, no unexplained content drift);
3. **recompute the owner's folder tree digest** and quote it beside the recorded
   one — a check over only the approved rows would miss exactly the files nobody
   is watching;
4. record the **first** `unshelved_deliverables` value **after** the apply.
   Floors ratchet to best-ever, so a pre-backfill reading pins a floor the
   backfill immediately ratchets away, and the metric would detect nothing
   during the exact window it exists for.
