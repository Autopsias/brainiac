---
name: promote
description: Execute a brain-substrate promotion — move a draft/staging note into a typed PARA zone (vault/brain/projects/, areas/, resources/, or archive/) via a single accept/reject/modify question. Handles frontmatter generation (id/title/type/classification/created/updated), a duplicate/encoded-elsewhere check against the brain index, and the write itself. Use whenever the user says "promote this", "move this to brain/resources", "file this under projects", or a draft/capture-inbox note needs to become a real, indexed brain note. Do not use for edits within an already-promoted brain/ note — this skill is specifically for the draft/raw → typed-PARA-zone promotion ritual.
---

# Promote Skill (brain-substrate kernel)

<!-- BRAINIAC-DESK-BLOCK v1 -- BYTE-IDENTICAL in all 14 Cowork bundles.
     Canonical copy: docs/operations/cowork-desk-block.md. Pinned by
     tests/test_cowork_skill_bundles.py against the LIVE broker
     (brain.mcp_adapter.TOOLS). Edit the canonical copy and every bundle in one
     commit, never one bundle alone, and never to name a tool the broker does
     not serve. -->

## Cowork (VM leg) — the vault is served by the desk, not by this sandbox

Reach the vault ONLY through the **desk**: the host-run `brainiac` MCP server,
whose tools Claude Desktop announces into this session. Call the tool. Do not
shell out for note content, and do not open a note file.

| the CLI verb you know | the desk tool to call |
|---|---|
| `search`, `hybrid-search` | `search`, `hybrid_search` |
| `get`, `read` | `get`, `read` |
| `recent` | `recent` |
| `dossier` | `dossier` |
| `bases-query` | `bases_query` |
| `grep` | `grep` |
| `graph-expand` | `graph_expand` |
| `vault-languages` | `vault_languages` |
| `diagnose` | `diagnose` |
| `alerts`, `exceptions`, `inbox` | `alerts`, `exceptions`, `inbox` |
| `draft-capture` (stage an unsigned note) | `capture` |

Those fifteen names are the whole desk, and they are the only route to note
CONTENT this session may use. **"Not on the desk" is not the same as "refused
here"**, and conflating the two is how a session talks itself into a workaround:
measured 2026-08-28, nine of the CLI's 22 VM-allowed verbs have no desk tool
(`brief`, `cos-propose`, `digest`, `doctor`, `draft-capture`, `init`,
`mcp-config`, `provision-request`, `status`), and two desk tools (`inbox`,
`vault_languages`) are not VM-allowed on the CLI at all. Of those nine, only
`cos-propose` — the COS ingress nothing on the desk serves — and the staging
probes below are still invoked locally by a shipped bundle, both deliberately.
Every verb that COMMITS or rebuilds — `write`, `sync`, `maintain`, `rebuild`,
`ingest`, `curate`, `health`, `integrity`, `verify-audit`, `graph-report` — is a
HOST-broker privilege and is not available here at all. **Unless a line is
explicitly marked as a Cowork instruction, a shell command anywhere else in this
skill is a HOST-lane instruction**, correct on the owner's Mac and not this
session's route.

**Do NOT bootstrap a vault path, do NOT export one onto `PATH`, do NOT hunt for
a staged binary to retry with, and do NOT open a note under `brain/` or `raw/`
with `cat`, `ls`, `Read` or a glob.** That is a RULE, and until the vault
actually moves off the mount it is the only thing standing between this session
and the bypass this whole change exists to close. Stated exactly, because the
two halves are true at different times:

- **While a published snapshot is still on the mount** (every workspace
  installed before the cutover), a staged engine CAN answer a local retrieval
  verb from `<vault>/.brain/snapshot/` — measured 2026-08-28: a local `search`
  at role `vm` returned the note body and exit 0 with the host index deleted,
  because the
  snapshot path is independent of it. Content that arrives that way skipped the
  desk, and so skipped the classification egress record the desk writes. A read
  that works is not a read you were allowed to make.
- **Once the vault is off the mount**, the local retrieval verbs fail rather
  than returning less — measured 2026-08-28: `search`, `get`, `recent` and
  `grep` exit 3 with `OperationalError: unable to open database file`, and
  `status --json` carries that same error in its `index` block. That is the desk
  telling you to use it, not a broken install and not a host-only refusal.
  **Not every surface is that loud**, so never read a quiet answer as an
  answer: an EMPTY result and a `not-computed` status are also what you get from
  a vault you cannot see. Only a hit you obtained through the desk is evidence.

Either way the answer is the same: call the desk. And the desk itself has no
offline substitute — it speaks stdio to the host process Claude Desktop spawned,
so there is no endpoint in this sandbox to dial and nothing to fall back to.

**The one local exception, and it returns no note.** If this workspace still
carries a staged engine, its three staging probes — `brain --version`, `brain
--role vm doctor`, `brain --role vm status --json` — read `.brain/` staging
metadata: engine stamp, model cache, vendored-deps ABI, snapshot presence. Keep
`--role vm` on them; without it the shell runs at role `host`, whose egress cap
is the whole vault. Use them for staging questions only. Their absence is not a
fault to fix, and none of the three is a way to reach a note.

If you need something the desk does not serve, say so and stop. That is a
finding for the engine, not a gap to work around.

Executes the brain-substrate promotion ritual in one structured interaction:
quality filter → single accept/reject/modify question → write (host-broker,
signed) → confirm. This is the generic kernel — the Smart-Connections-era
version cross-checked against an Obsidian vault's `Atlas.md` / typed
zones; this version cross-checks against the `brain` index directly.

## Gather parameters (if not provided)

| Parameter | Description |
|---|---|
| `source` | The draft to promote — a `vault/raw/` source id, a `capture-inbox/` draft, or a chat-only block with no file yet |
| `target_zone` | One of `vault/brain/projects/`, `areas/`, `resources/`, `archive/` |
| `proposed_frontmatter` | The note's YAML block (see contract below) |
| `rationale` | Why this passes the three-question filter |

Derive missing values: read the source content, extract a sensible `title`,
a one-sentence `description`-equivalent (used in the rationale, not stored —
`brain/` notes don't carry a `description:` key, see AGENTS.md §2), and a
slug for `id`. Draft the target path as
`vault/brain/<target_zone>/<id>.md`.

## Run the three-question filter first

Before asking, answer all three:

1. **Is this still true?** Cross-check against the most relevant existing
   note: `brain get <likely-related-id> --json` if one is obvious, or
   `brain search "<gist>" --rerank --json` to find the nearest neighbours. If
   the content is stale or contradicted by what's already indexed, flag it in
   the rationale — do not silently skip.
2. **Will this affect future work?** If no, the honest recommendation is
   `archive/`, not `projects/`, `areas/`, or `resources/`.
3. **Is this encoded elsewhere?** This is the duplicate check, and it is now
   a real retrieval call instead of a grep:
   ```bash
   brain --vault "$BRAIN_VAULT" search "<content gist>" --rerank --json
   brain --vault "$BRAIN_VAULT" get <top-candidate-id> --json   # confirm before concluding duplicate
   ```

> **Multilingual vaults — the variant contract (AGENTS.md §5 rule 3).** Before
> the first vault search, read the derived census: `brain status --json` ->
> `index.languages`. When `multilingual` is true, issue every search as the
> question PLUS one `--variant "<the same question in that language>"` for each
> other entry in `vault_languages` — you write the translation, the engine
> fuses the result lists into one ranking. `multilingual: false`, one language,
> or no census: a single query is correct — do not invent a variant.
   `search` does the recall pass; `get` confirms the specific candidate
   actually says the same thing (search results are sourced JSON with
   snippets, not full bodies — don't conclude "duplicate" from a snippet
   alone). If a substantially identical note already exists, surface its id
   and ask whether to merge, supersede, or skip. Do not create duplicates.

If the filter says "archive instead": set `target_zone` to `archive/` and
note the reason.

## Ask — single structured question

Present exactly one question with three options:

- **Header**: "Promote `{source}` → `vault/brain/{target_zone}/{id}.md`?"
- **Body**: rationale (2–3 sentences), then the full proposed frontmatter
- **Option 1 — Accept**: write now, confirm
- **Option 2 — Reject**: skip, no changes
- **Option 3 — Modify**: pause for a correction, then re-present

## On Accept

1. **Write the target note** via the host-broker commit path (signs +
   indexes + WALs in one step). `--content` is the **full Markdown file**
   (frontmatter block + body) — `write` does not take a separate
   frontmatter argument:
   ```bash
   brain --vault "$BRAIN_VAULT" write "brain/{target_zone}/{id}.md" \
     --content "$(cat <<'EOF'
   ---
   id: {id}
   title: "{title}"
   type: note
   classification: {classification}
   created: {today}
   updated: {today}
   ---

   {body}
   EOF
   )" --reason "promote: {rationale}"
   ```
   In a Cowork session do not reach for `write` or a local CLI — call the
   desk's **`capture`** tool: `content` = the same full Markdown as above,
   `id` = `{id}`. The broker runs capture in the HOST process, so the reply
   comes back `signed: true` / `indexed: true` and the note is retrievable at
   once; there is no draft to drain and no host `brain sync` owed. Report the
   `id` the tool returned, never your intent to capture.
2. **Reindex — always required.** `brain write` signs and commits the
   Markdown file but does **not** touch the search index itself; the note is
   not retrievable until the next `brain sync` (or `brain rebuild`) runs:
   ```bash
   brain --vault "$BRAIN_VAULT" sync --publish
   ```
   Run this immediately after a host `write` so the promotion is actually
   findable, not just on disk. A VM draft needs the same host `brain sync`
   to become durable AND searchable (the drain, the sign, and the index
   update all happen in that one call).
3. **Cross-link.** Add at least one wikilink from the new note to a related
   existing note (`[[related-id]]`), and consider whether `vault/brain/
   index.md` needs a one-line pointer. Density of links is how this
   substrate stays navigable without folders (AGENTS.md §3) — a promoted
   note with zero outbound links is itself the next `audit-orphans` finding.
4. **If the source was a `raw/` source**, leave it in place (immutable per
   AGENTS.md §4) and set the new note's `source:` field to point back at it.
5. **If the source was a `capture-inbox/` draft**, it is consumed by the
   `write`/`sync` drain automatically — do not delete it manually.

## On Reject

No write. Tell the user: "Skipped — `{source}` stays as-is."

## On Modify

Ask what to change. Accept free text. Re-draft the frontmatter or target
zone, then re-present once. Never loop more than twice without surfacing a
dead end.

## Frontmatter contract

Per AGENTS.md §2 — the promoted file must carry:

```yaml
---
id: <stable-slug>                  # lowercase-hyphen, unique
title: "<Human-readable title>"
type: note                         # note | index | moc | source-derived
classification: Internal           # Public|Internal|Confidential|Restricted|MNPI — required, no default
created: YYYY-MM-DD
updated: YYYY-MM-DD
source: "[[raw/<id>]]"             # only if derived from a raw/ source; omit if original
---
```

**`classification` has no safe default.** A missing or unrecognised value is
treated as MNPI and withheld at every read surface (AGENTS.md §5) — so the
question of what classification this note carries is part of the rationale
you present, not an afterthought filled in after writing.

**Is this a deliverable? (DLV-01, AGENTS.md §4)** One extra step, asked once
before you draft the frontmatter: does this note anchor a *finished output
produced for an audience* — a deck, a memo, an analysis someone receives —
rather than material that came in or a working note? If yes, add two keys:

```yaml
deliverable: true                  # orthogonal to `type:` — NEVER change `type`
project: <project-id>              # bare id or [[wikilink]]; groups the shelf
provenance.produced_by: promote    # the producing surface; always stamped
```

`deliverable` is **not** a member of the `type:` vocabulary. `type` is
single-valued and load-bearing — `type: decision` IS the decision layer — so a
produced decision document keeps `type: decision` and gains the marker. Stamp
`provenance.produced_by: promote` on every note this skill writes, deliverable
or not: it is automatic where the marker is a judgment, and the difference
between the two is what makes adoption measurable. A new version of a
deliverable is a **supersede**, never an edit.

## Archival path (when the filter says "archive instead")

Target: `vault/brain/archive/<id>.md`. The PARA taxonomy is flat — `archive/`
is a sibling of `projects/areas/resources/`, never nested under them
(AGENTS.md §3). No special frontmatter beyond the standard contract; the
zone itself carries the "this is retired" signal.

## Batching multiple candidates

When several promotion candidates are queued, work through them one at a
time in priority order. One question per candidate — never bundle. After
each accept/reject, move to the next.

## Cross-references

- `AGENTS.md` §2 (note shape), §3 (PARA + link style), §5 (classification gate), §6 (host/VM write split)
- `brain --help` — `write`, `draft-capture`, `search`, `get`
