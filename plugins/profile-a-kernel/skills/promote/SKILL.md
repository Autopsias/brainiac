---
name: promote
description: Execute a brain-substrate promotion — move a draft/staging note into a typed PARA zone (vault/brain/projects/, areas/, resources/, or archive/) via a single accept/reject/modify question. Handles frontmatter generation (id/title/type/classification/created/updated), a duplicate/encoded-elsewhere check against the brain index, and the write itself. Use whenever the user says "promote this", "move this to brain/resources", "file this under projects", or a draft/capture-inbox note needs to become a real, indexed brain note. Do not use for edits within an already-promoted brain/ note — this skill is specifically for the draft/raw → typed-PARA-zone promotion ritual.
---

# Promote Skill (brain-substrate kernel)

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
   On the Cowork VM (`--role vm`), `write` is refused by design — stage a
   draft instead and let the host drain it (same full-Markdown content
   convention; `--id` and `--source` are the only other flags):
   ```bash
   brain --vault "$BRAIN_VAULT" draft-capture --id {id} --content "<same full markdown as above>"
   ```
   and tell the user the promotion is staged, not yet committed, pending the
   next host `brain sync` (which drains `capture-inbox/`, signs, indexes, and
   republishes the snapshot).
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

- `docs/cutover/repoint-map.md` §2 — the dependency table this skill implements
- `AGENTS.md` §2 (note shape), §3 (PARA + link style), §5 (classification gate), §6 (host/VM write split)
- `docs/cutover/brain-cli-verbs.md` — `write`, `draft-capture`, `search`, `get`
