---
name: save-conversation
description: "Promote a valuable mid-conversation analysis, snippet, or decision into a typed brain note (vault/brain/projects|areas|resources|archive/). Auto-classify the destination PARA zone, run the three-question quality filter (now backed by a real `brain search` duplicate check instead of a grep), propose id/title/frontmatter, present one accept/reject/edit question, and on approval write the file via the brain host-broker write path (or stage a draft on a no-write VM leg). Trigger phrases — '/save', '/save [title]', 'save this', 'save this conversation', 'save this analysis', 'promote this', 'capture this in the brain', 'this is worth keeping'. Use whenever a substantive analysis surfaces in chat that should survive the session — good answers shouldn't disappear into chat history. Do NOT use for promoting an existing draft/raw file that already has a path (use the promote skill), kb maintenance (kb-curator), or execution-work tracking."
---

# save-conversation (brain-substrate kernel)

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

Turns a valuable chat turn — analysis, decision rationale, concept
explanation, snippet — into a properly-typed `brain/` note. One-shot
interaction: auto-classify → quality-filter (duplicate check via `brain
search`) → propose → write → confirm.

**The gap this fills:** `promote` requires the content to already exist as a
file (a `raw/` source or a `capture-inbox/` draft). `/save` covers the case
where **the useful artefact lives only in chat history** — without this
skill it evaporates when the session closes.

---

## Phase 0 — content selection

What gets saved is the substantive turn immediately preceding the `/save`
trigger — typically the most recent assistant turn with analysis-worthy
content (not a clarification question, not a short acknowledgement).

- `/save` with no argument → save the most recent qualifying turn.
- `/save <title>` → same selection, `<title>` seeds `id`/`title`.
- "save the part about X" → scan recent turns for the topic, extract that
  block.
- Ambiguous selection (multiple plausible blocks) → ask **one** clarifying
  question: "Save the block about *<A>* or *<B>*?"

---

## Phase 1 — auto-classify the destination PARA zone

Score the content against these signals. Pick the highest-scoring zone; if
two tie or the top score is weak, jump straight to Phase 3 (low-confidence →
`resources/` with `confidence: 0.2`, the brain-substrate equivalent of an
Inbox triage sink — there is no separate Inbox zone in this substrate, see
AGENTS.md §3, so the **conservative default is `resources/`**, the most
general-purpose PARA folder).

| Signals | Target zone |
|---|---|
| Named concept, "how X works", architecture explanation, durable principle | `areas/` (a standing area of responsibility/knowledge) or `resources/` (a reference note not tied to one project) |
| Audit, comparison, gap study, research synthesis tied to a live body of work | `projects/` |
| "We decided", explicit rationale + accept/reject framing, closes a question | `projects/` (link it from the relevant project note) or `resources/` if no active project owns it |
| Anything else, OR multiple zones plausible, OR confidence weak | `resources/` (the safe default — see Phase 3) |

### Strong vs weak signal

- **Strong** — the content's structure matches the zone (a `projects/`
  candidate is tied to a specific named, currently-active body of work).
- **Weak** — one keyword matches but the content drifts elsewhere (a
  comparison that mentions "we decided X" in passing stays `resources/` or
  `projects/`, not reclassified into a decision-only category — this
  substrate doesn't have one; capture the decision context in the note body
  instead).

---

## Phase 2 — the three-question quality filter

Before writing to any zone other than the low-confidence default, answer all
three honestly:

1. **Is this still true?** Cross-check against the nearest existing note:
   ```bash
   brain --vault "$BRAIN_VAULT" search "<one-line gist>" --rerank --json

> **Multilingual vaults — the variant contract (AGENTS.md §5 rule 3).** Before
> the first vault search, read the derived census: `brain status --json` ->
> `index.languages`. When `multilingual` is true, issue every search as the
> question PLUS one `--variant "<the same question in that language>"` for each
> other entry in `vault_languages` — you write the translation, the engine
> fuses the result lists into one ranking. `multilingual: false`, one language,
> or no census: a single query is correct — do not invent a variant.
   ```
   If a top hit looks like it covers the same ground, `brain get <id> --json`
   to read the full note and confirm before concluding contradiction or
   staleness.
2. **Will this matter later?** A one-shot fact lookup or a recap of
   something already documented — the honest answer is no.
3. **Is this encoded elsewhere?** This is the same `search` call as
   question 1, read for *duplication* rather than *contradiction*. If a
   substantially identical note already exists, surface its id and ask:
   merge, supersede, or skip.

### On filter failure

If any answer is "no" or "duplicate": **default route → `resources/` with
`confidence: 0.2`.** Note the filter outcome in the proposal preamble — e.g.
"Q3 found near-duplicate `[[existing-id]]` (search score 0.91) — routing low
confidence, review for merge." Never silently switch zones without saying so.

---

## Phase 3 — confidence check (low-confidence → `resources/`)

Even when the zone heuristics and the filter both pass, route to
`resources/` with `confidence: 0.2` if any of these fire:

| Signal | Rationale |
|---|---|
| Two zones tied for top score in Phase 1 | Wrong-zone placement is harder to fix than a follow-up move |
| The user's message contains "maybe", "not sure", "could be", "I think" | Explicit verbal uncertainty |
| Content is < 200 words and not a clear standing decision | Probably not durable yet |
| Phase 1 picked `projects/` but no specific, currently-open project is named | Ambiguous binding — `resources/` is the safe parking spot |
| The thread is exploratory ("let's brainstorm", "what if") rather than concluded | Premature codification causes churn |

---

## Phase 4 — propose id, title, frontmatter

- `id`: lowercase-hyphen slug from `<title>` if the user gave one, else from
  the first ~6 words of a one-line summary.
- `title`: a clear human-readable title (≤60 chars).

```yaml
---
id: <slug>
title: "<title>"
type: note
classification: Internal          # propose a value; never write a note with no classification
created: YYYY-MM-DD
updated: YYYY-MM-DD
source: "[[raw/<id>]]"            # only if this turn was substantially derived from one cited source
---
```

There is no `tags:` taxonomy beyond the optional, emergent field AGENTS.md
§2 allows — skip it unless something obvious surfaces, and never invent a
controlled vocabulary.

**Is this a deliverable? (DLV-01, AGENTS.md §4)** Ask it here, once: does this
note anchor a *finished output produced for an audience* — a deck, a memo, an
analysis someone receives — rather than a working note or captured material?
If yes, add two keys:

```yaml
deliverable: true                          # orthogonal to `type:`
project: <project-id>                      # bare id or [[wikilink]]
provenance.produced_by: save-conversation  # always stamped, deliverable or not
```

Never change `type:` to mark a deliverable — `type` is single-valued and
load-bearing (`type: decision` IS the decision layer), so a produced decision
keeps its type and gains the marker. Stamp `provenance.produced_by` on every
note this skill writes: the stamp is automatic, the marker is the judgment, and
"produced but unmarked" is exactly the gap worth seeing. A new version of a
deliverable is a **supersede**, never an edit.

---

## Phase 5 — ask (single structured question)

One question, exactly three options, decidable in under 15 seconds:

- **Body**: "Save as `vault/brain/{zone}/{id}.md`? — y / n / edit" followed
  by the one-line proposal preamble (route + reasoning), the frontmatter
  block, and the first 120 chars of body content so the user can verify the
  right block was selected.
- **Accept (recommended)**: write now, confirm.
- **Reject**: skip — content stays in chat history only.
- **Edit**: pause and change title/zone/frontmatter, then re-present once.

---

## Phase 6 — on accept: write

```bash
brain --vault "$BRAIN_VAULT" write "brain/{zone}/{id}.md" \
  --content "$(cat <<'EOF'
---
id: {id}
title: "{title}"
type: note
classification: {classification}
created: {today}
updated: {today}
---

{cleaned body}
EOF
)" --reason "/save: auto-classified {zone}"
```

On a no-write leg, go through the desk's **`capture`** tool instead:
`content` = the same full Markdown, `id` = `{id}`. The broker runs capture in
the HOST process, so it returns `signed: true` / `indexed: true` — quote that,
rather than telling the user a draft is pending.

**Then reindex — `write` does not auto-sync.** `brain write` signs and
commits the file but leaves the search index untouched; run
`brain --vault "$BRAIN_VAULT" sync` right after so the note is actually
findable, not just on disk. A draft staged by the local CLI's
`draft-capture` needs the same host `sync` call to be drained, signed and
indexed; a desk `capture` does not — it was already both.

Body cleanup before writing: strip trailing meta-commentary ("let me know if
you want…"), strip self-references. **Add at least one wikilink** to a
related existing note — a freshly-written note with zero outbound links is
the next `kb-curator audit-orphans` finding (AGENTS.md §3: "every note should
connect to ≥1 other").

Acknowledge: "Saved → `brain/{zone}/{id}.md`." If routed to `resources/`
under low confidence, add: "Low-confidence routing — worth a follow-up
review."

## Phase 7 — on reject / edit

**Reject:** no write. "Skipped — content stays in chat history only."

**Edit:** ask what to change, re-draft, re-present once more. Never loop
more than twice — surface the friction and offer to skip instead.

---

## What `/save` does NOT do

- Does not promote a file that already exists on disk — use `promote`.
- Does not rewrite an existing `brain/` note — use a direct edit.
- Does not run a maintenance/cleanup pass — use `kb-curator`.
- Does not loop autonomously — exactly one structured question per
  invocation, re-presented at most once on Edit.

## Cross-references

- `AGENTS.md` §2–§4 (note shape, link style, capture rules), §6 (host/VM write split)
- `.claude/skills/promote/SKILL.md` — sibling skill for an existing-file promotion
- `.claude/skills/kb-curator/SKILL.md` — `audit-orphans` will flag a save with no outbound link
