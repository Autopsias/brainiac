# ADR 0010 — The deliverables shelf lives outside `vault/`

- **Status:** Accepted (2026-08-20, during `/plan-harden` of the Deliverables Shelf plan)
- **Context repo:** Profile A `brain`

## Context

The owner cannot find the final versions of outputs produced from vault content, and does
not want to memorise CLI commands to do it. The accepted design is a generated folder of
latest-version-only copies, grouped by project, maintained by the nightly — a surface he
can scan in Finder.

The obvious placement was `vault/deliverables/`, inside the tree the folder is derived
from. The plan was written that way, and then hardened.

## Decision

The shelf lives **outside** the vault directory, and it is **bound to one vault**:
`<vault>/../brain-deliverables` by default, overridable with `$BRAIN_DELIVERABLES_DIR`.
The binding lives in **one host-private registry keyed by canonical target path**, with
atomic claim semantics: first claim wins, and a second vault resolving to the same target is
refused rather than silently re-namespaced. It cannot be a per-vault file beside each index
directory — vault B cannot read vault A's index dir, so both would write their own binding,
both would believe they own the shelf, and both would write into it. The in-shelf `shelf-manifest.json` is a published advisory view: it
sits in a folder the owner can write to, so it cannot carry an identity claim anything
trusts, and it is never read back for authority.

The folder is **not** called `deliverables`, and it is not namespaced by a hex vault id.
Both were tried and both were wrong on the only vault this ships to:
the reference deployment already keeps its own `deliverables/` folder, hand-maintained by the
owner and holding real work, so the first default would have nested a machine-owned tree
inside it; and a directory named `7f0d4a0d366d8bb3` defeats the premise of a folder someone
can scan in Finder. A readable name plus a fail-closed refusal gets both properties.

The resolver fails closed on three conditions, each naming the override variable and its
recovery in the refusal text:

1. the resolved parent is the user's home directory;
2. the resolved parent contains a `.git` directory and the override was not set explicitly
   — in *this* repository `<vault>/..` is the git root, and it feeds a public export
   pipeline, so an untracked tree of note copies there is one `git add -A` from a
   confidentiality incident;
3. the resolved target **equals** an existing non-empty directory the binding record does
   not name, **lies inside** one, or **contains** one. All three directions: an earlier
   wording said "is a path prefix of", which catches only one of them and misses the case
   that motivated the rule — a shelf at `<workspace>/deliverables/x` lies *inside*
   `<workspace>/deliverables`, which is a prefix of the target rather than the reverse.

Vault binding was added after review: without it two sibling vaults under one parent both
resolve to one directory and each fold displaces the other's entries. The first draft cited
`cos-corpus` as precedent for "outside the vault" while omitting that the precedent is
*also* keyed by vault id — right about the placement, wrong about the shape. The binding is
enforced by refusal rather than by namespacing, because a name a human can read is part of
what this feature is for.

## Consequences

**Why not inside.** `vault/` is walked by machinery that treats every `.md` under it as a
note unless a rule says otherwise. A tree of copies inside it would have needed, and kept
needing:

- an anchored exclusion in `scan_vault` (`notes.py:217`) — and one in every whole-vault
  walker written from then on, each omission a silent leak;
- protection from `backup.py`, whose `EXCLUDE_DIRS` is `{.brain, .git, __pycache__,
  .pytest_cache}` — the shelf duplicates `raw/originals/` payloads, so every encrypted
  backup would roughly double;
- protection from `brain project --dest`, the filtered workspace copy;
- an answer for the path-keyed audit chain: the 2026-08-18 PAR-01 incident showed a fold
  writing note content to new paths under `vault/` reporting one signed note as both
  content-drifted and never-signed, the latter on an absolute ratchet that stayed
  regressed;
- an answer for `auto_dedup_tier1`: a markdown deliverable's payload is its own `.md`, so
  the shelf copy is byte-identical to the note, which is exactly that fold's input — it
  could retire the real note against the convenience copy.

Outside the tree, none of these are guards that can be forgotten; they are absent problems.
The same structural argument already governs `cos-corpus`, which AGENTS.md keeps outside
`vault/` so that "no indexing rule was weakened to get it".

**The fold never deletes.** A displaced copy moves to `<shelf>/_previous/<run-id>/`, the
same way retired vault files move to `inbox/_quarantine/_resolved/<batch>/` rather than
being unlinked. This is what makes the remaining guards affordable: a stale census, a forged
ledger row or an owner edit each cost a recoverable move instead of a destroyed file. It
carries the precedent's lessons with it — `_previous/` is excluded from the unknown-file
report (parked files once made a trend alert fire nightly on already-dispositioned content),
namespaced by run so two versions of one deck cannot overwrite each other, and owner-only.

Its retention rule has one hard limit: **the prune may never delete a sole copy.** An entry
reaches `_previous/` when its note is superseded *or gone*, and a Markdown deliverable's
payload is the note's own file — so for a deleted note, the `_previous/` copy can be the last
surviving bytes, and AGENTS.md reserves deleting a possibly-sole copy for the owner. An entry
is pruned only when byte-identical content still exists in the vault or on the live shelf;
anything else is retained past its window and raised as one owner decision.
`$BRAIN_SHELF_PREVIOUS_DAYS` is an eligibility window, not a delete trigger. A design that
advertises itself as recoverable must not carry a timer that quietly makes it not.

**What it costs.** The shelf no longer travels inside the vault folder if the vault alone
is moved or synced, and it is not captured by `backup.py`. Both are acceptable because the
shelf is a **generated view**: any host can rebuild it by running the fold. `brain status`
prints the resolved path so the folder stays discoverable without documentation.

**What stays unsolved.** Spotlight and Finder search index the vault note and its shelf copy
independently, so a search shows both. That is the price of copies rather than symlinks —
symlinks were rejected because they break on Windows, on cloud sync, and across the Cowork
VirtioFS mount. It is documented in `docs/deliverables-shelf.md` as an accepted trade-off,
not fixed.

## Alternatives considered

- **`vault/deliverables/` with exclusions** — rejected above; a permanent tax whose failure
  mode is silent.
- **Symlinks instead of copies** — rejected on portability (Windows, cloud sync, VirtioFS).
- **Hard links** — rejected: editing the shelf file would edit the archived original, and
  `raw/` is immutable by contract.
