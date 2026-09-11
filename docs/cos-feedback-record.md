# The COS owner-feedback record (FB-01, frozen 2026-08-25)

> **This file is the interface.** Four later sessions of The Night Porter plan
> build against it and cannot renegotiate it: **s05** (the two pens and the
> judge/draft prompt side), **s07** (the morning sheet), **s09** (the week
> report), **s10** (acceptance). If a consumer genuinely needs a field this
> record cannot carry, EXTEND this file and record the extension as a named
> deviation in that session's closeout — do not invent a second shape.
>
> Code, FOUR modules, imports flowing one way:
> `src/brain/cos/feedback.py` (the record — paths, row shapes, append, read),
> `src/brain/cos/feedback_render.py` (how much of it one prompt may carry),
> `src/brain/cos/feedback_sheet.py` (the sheet's embedded state) and
> `src/brain/cos/feedback_cli.py` (s05 — the two PENS that write it).
> Tests: `tests/test_cos_feedback.py` (the record), `tests/test_cos_feedback_pens.py`
> (the pens and the two consumers).
>
> **This file has been extended five times: three by s05, two by s08 after the
> owner read the first sheet published against a live mailbox. Each is marked
> EXTENSION at the point of change and collected in §9.**

There are **two objects** here, with different lifetimes, and confusing them is
the failure this document exists to prevent:

| | The record | The sheet state |
|---|---|---|
| Where | HOST-PRIVATE app data, off the mount (§1) | inside `<vault>/.brain/cos/sheets/<date>.html`, on the mount |
| Lifetime | append-only, forever | one page per night |
| Written by | `feedback.record_rule` / `record_thread_ruling` | s07's sheet builder; the OWNER fills the marks on Save |
| Read by | the judge/draft prompts, the sheet, the week report | `brain cos feedback --from-sheet <html>` (s05) |
| Carries text? | **hashes only** — no subjects, no bodies | display subject text, so the owner can recognise the thread |

---

## 1 · The two locations, and why they differ

```
<host app data>/cos-feedback/<vault_slug8>/feedback.jsonl   # the record
                                                            # (feedback.feedback_path)
<vault>/.brain/cos/sheets/<date>.html                       # the sheets
                                                            # (feedback.sheets_dir)
```

**Always ask `feedback.feedback_path(vault)` / `feedback.sheets_dir(vault)`.
Never spell either path by hand.**

### The record is HOST-PRIVATE — a deliberate change from the plan text

**DEVIATION, recorded in s04's closeout.** The plan specified
`<vault>/.brain/cos/feedback.jsonl`. That path is on the mount, and this is
measured rather than assumed: `config.vm_visible_roots()` returns the vault
root **and `<vault>/.brain`**, and `config.proven_off_mount()` refuses
`<vault>/.brain/cos` outright.

What this record does is turn its own rows into **standing owner rulings in
the judge's and the drafter's prompts** — text that decides what the nightly
archives in a real mailbox. An untrusted leg able to author owner instructions
is the lethal trifecta, and this repo has closed that exact class four times:
the approved queue (INT-01), the drift dispositions (INT-02), the attachment
anchors (INT-04) and the writer lock (INT-05) all moved off the mount, plus the
standing ingestion approval. Each time the finding was the same one, in
INT-05's words: *"No amount of checking at open time fixes that; not being
reachable does."*

So the record is keyed exactly like the approved queue: a host-controlled base
(`cos._host_private_base()`, i.e. `$BRAIN_INDEX_DIR` or the per-user app-data
directory) plus `config.vault_slug8` — the hash of the RESOLVED VAULT PATH,
which is the one identity a VM cannot rewrite. It is **never**
`.brain/vault-id`, a plain file on the mount. `cos._proven_off_mount` then
refuses the directory outright if configuration puts it back inside a
VM-visible root, and a test asserts both the refusal and the placement.

**Consequence to know:** moving the vault changes `vault_slug8` and therefore
the record's directory. Rulings recorded before the move are not lost, but they
are not found either until the directory is moved with the vault. Same bounded
cost the approved queue already carries.

**To reverse this decision**, change the one function `feedback.feedback_dir`
back to `config.brain_runtime_dir(vault) / "cos"`; every consumer goes through
it. Nothing else in this document changes.

### The sheets stay on the mount

`<vault>/.brain/cos/sheets/` is where s07 writes `<date>.html`. The sheet has
to be reachable — the owner opens it, a session publishes it — and it
authorises nothing by existing: every mark read back off one goes through
`feedback_sheet.validate_sheet_state` and then `record_rule` /
`record_thread_ruling`, which is where the closed shape and the host-private
ledger take over.

`sheets_dir()` is named in `feedback.py` rather than in s07's `sheet.py`
because **s06's out-of-band heartbeat reads this directory's newest mtime**
to decide the nightly has died — and s06 runs before `sheet.py` exists. Two
spellings of one path is how a heartbeat ends up watching a directory nothing
writes.

### Neither is ever indexed

Nothing here reaches `search` / `get` / `recent`: the record is outside
`vault/` entirely, and `.brain/` is gitignored wholesale and never indexed
(ADR-0003 Ruling 4).

Writes go through `cos._append_jsonl` (O_NOFOLLOW append under a host-private
per-ledger lock); reads go through `cos._read_nofollow`. No raw `open()` — the
`tests/test_cos_pathguard_io.py` AST scan covers this module, and
`tests/test_cos_pathguard_census.py` classifies `read_record` as `OFF_MOUNT`.

---

## 2 · Row kind 1 — the RULE row

A normalized standing ruling the judge quotes. **Deduplicated by rule, capped
when rendered, and expired.**

```json
{
  "kind": "rule",
  "schema": "cos-feedback/1",
  "ts": "2026-08-26T06:31:00Z",
  "source": "sheet",
  "thread": {
    "conversation_id": "AAQkAD…",
    "conversation_id_digest": "ba7816bf8f01cfea",
    "subject_sha256": "9f86d081…"
  },
  "action_taken": "archived",
  "verdict": "right",
  "note": "keep doing this",
  "rule": "Iberdrola digests are noise",
  "rule_key": "iberdrola digests are noise",
  "confirmations": 2,
  "expires_at": "2026-11-24T06:31:00Z",
  "revoked": false,
  "run": "2026-08-26-run190"
}
```

| Field | Meaning | Producer |
|---|---|---|
| `kind` | `"rule"` | `record_rule` |
| `schema` | `cos-feedback/1` | `record_rule` |
| `ts` | when the ruling was recorded, `%Y-%m-%dT%H:%M:%SZ` | `record_rule` |
| `source` | `sheet` \| `outlook` | caller (s05's two pens) |
| `thread.conversation_id` | the mailbox conversation, raw — this is what fb-02 joins the undo ledger on | caller |
| `thread.conversation_id_digest` | `feedback.thread_digest` = `sha256(id)[:16]` | `record_rule` |
| `thread.subject_sha256` | `feedback.subject_digest` = full `sha256` of the whitespace-normalized subject | `record_rule` |
| `action_taken` | what the porter did (closed set, §4) | caller |
| `verdict` | `right` \| `wrong` \| `missed` | the owner |
| `note` | the owner's free text for this thread (may be `""`) | the owner |
| `rule` | the standing ruling text, as the judge will quote it VERBATIM (may be `""`). Whitespace-collapsed on append — the owner's casing and punctuation are kept, a textarea's stray padding is not | the owner / s05, normalized by `record_rule` |
| `rule_key` | `feedback.rule_key(rule)` — casefolded, whitespace-collapsed, trailing punctuation stripped | `record_rule` |
| `confirmations` | `1` on first sight, `1 +` the highest this `rule_key` has reached, thereafter | `record_rule` |
| `expires_at` | `ts + 90 days` (§5) | `record_rule` |
| `revoked` | `true` retires the rule from every rendered block | the owner (sheet revoke control) |
| `run` | the run id whose action is being judged (`""` if unknown) | caller |

`RULE_ROW_KEYS` is a **closed set**: any other key is refused on append and on
read-back.

---

## 3 · Row kind 2 — the THREAD RULING row

**Keyed on `conversation_id` alone. A do-not-touch. Never deduplicated, never
capped in the ledger, never expired.**

```json
{
  "kind": "thread",
  "schema": "cos-feedback/1",
  "ts": "2026-08-26T02:14:00Z",
  "source": "outlook",
  "conversation_id": "AAQkAD…",
  "conversation_id_digest": "3f2a91c0d4e8b117",
  "subject_sha256": "9f86d081…",
  "action_taken": "archived",
  "verdict": "wrong",
  "note": "",
  "run": "2026-08-25-run188"
}
```

**Why the second kind exists.** A sheet `wrong` mark and *every* outlook-diff
row carry **no rule text**, and fb-03 deduplicates BY RULE. Without a kind that
is keyed on the thread instead, the owner drags a thread back out of Archive,
the next run judges it identically on identical evidence, and archives it
again — nightly, forever.

`THREAD_ROW_KEYS` deliberately **omits `expires_at`, `confirmations`, `rule`
and `revoked`.** "Never expired, never deduplicated" is therefore structural,
not prose: a thread row that carries an expiry is refused rather than quietly
honoured (`tests/test_cos_feedback.py::test_a_thread_row_that_claims_an_expiry_is_refused`).

`conversation_id_digest` uses the **same 16-hex convention as the undo
ledger's own `conversation_id_digest`** (`tools/cos_driver_transport.short`), so
a feedback row and a mutation row for one thread quote the same short id in
evidence. The test asserts that equality against the tool rather than trusting
this sentence.

---

## 4 · Closed vocabularies

| Constant | Values |
|---|---|
| `KINDS` | `rule`, `thread` |
| `SOURCES` | `sheet`, `outlook` |
| `VERDICTS` | `right`, `wrong`, `missed` |
| `ACTIONS` | `archived`, `ingested`, `drafted`, `held`, `chipped`, `stale-archived`, `none` |

`missed` is the third verdict on purpose: "you did nothing and should have" is
a correction `right`/`wrong` cannot express.

`ACTIONS` is the **sheet's** outcome vocabulary, not the undo ledger's
`MUTATION_VERBS` (`archive`, `categorize`, `draft`). The one-way mapping is
`feedback.ACTION_FOR_VERB` — a checked dict, not a table that can rot: a
test asserts its keys are exactly `MUTATION_VERBS` and its values are all
in `ACTIONS`, so a verb added to the mutation layer fails here rather than
reaching the sheet as an unknown word.

| undo-ledger verb | `action_taken` |
|---|---|
| `archive` | `archived` (or `stale-archived` when judge-02's stale lane planned it) |
| `categorize` | `chipped` |
| `draft` | `drafted` |
| *(no mutation — vault write)* | `ingested` |
| *(no mutation — hold placed)* | `held` |
| *(nothing planned)* | `none` |

---

## 5 · The expiry window, and the rule that reads `confirmations`

**Default expiry window: 90 days** (`DEFAULT_RULE_EXPIRY_DAYS`, override
`$BRAIN_COS_FEEDBACK_RULE_DAYS`). It matches `DEFAULT_AUTOCAP_WINDOW_DAYS`, the
window this vault already uses for "how far back does behavioural evidence
count"; a ruling about the owner's mail is the same sort of claim.

**A confirmation is an append, not a mutation.** Recording a rule whose
`rule_key` already exists writes a new row with `confirmations = previous + 1`
and `expires_at = now + 90 days`. **A REVOKE is not a confirmation** (EXTENSION,
s05): a `revoked=True` row carries the count it retires (never below 1, which
the field set requires) rather than one more, so re-minting the same rule later
starts from what the evidence actually reached. The fold (`live_rules`) takes the latest row
per `rule_key`, so history is never rewritten and a revoke is just a later row.

**`confirmations` has exactly one consumer, and it is
`feedback_render.ranked_rules`:** the
rules block is ordered by **confirmations DESC, then `ts` DESC**, then cut at
the cap. Recency alone would let one noisy sheet evict a twice-confirmed rule,
which is precisely backwards. The test pins both directions — the confirmed
rule survives the cap, and a recency-only ordering is shown to drop it.

`feedback_render.live_rules` drops a row that is `revoked`, past its `expires_at`, **or whose
`rule` text is empty**. That last clause is the belt under §3: a `wrong` mark
carrying no rule must never reach the judge as a blank instruction. It reaches
the judge as a thread ruling instead.

---

## 6 · Stored vs rendered — the caps and the ceiling

Storing every ruling and rendering every ruling are different things.

| | Stored in the ledger | Rendered into one prompt |
|---|---|---|
| Rule rows | every row, forever | `DEFAULT_RULE_CAP = 40` (`$BRAIN_COS_FEEDBACK_RULE_CAP`), after dedupe by `rule_key`, revoked and expired dropped |
| Thread rulings | every row, forever, uncapped and unexpired | at most the **latest active row per `conversation_id`**, SPLIT BY VERDICT into a do-not-touch list (`wrong`) and a `missed` list, then cut at `DEFAULT_THREAD_RENDER_CEILING = 80` (`$BRAIN_COS_FEEDBACK_THREAD_CEILING`) rendered rows **in total**, do-not-touch served first |

**Rendered-row ceiling: 40 + 80 = 120 ruling rows, maximum.** The measured
cliff is ~250 rows in a single model message — 258 verdicts with 24 dropping
compliance — and s06's per-batch thread cap sits "well under 250". 120 ruling
rows leaves headroom for a 120-thread batch beneath that cliff. Rendering 361
thread rulings, which the plan's own 361-thread volume would produce, recreates
exactly the failure the batch cap exists to prevent.

Both numbers are **read in the pass that renders**, never at import — the batch
size they share a message with is decided per run.

**Nothing excluded is silently dropped.** (All three in
`feedback_render`.) `ranked_rules` returns `excluded` +
`excluded_keys`; `projected_thread_rulings` returns `excluded` +
`excluded_digests` (the do-not-touch side), `wanted_more_excluded` +
`wanted_more_digests` (the `missed` side), plus `stored`, `active` and
`wanted_more_active`. `render_budget(vault)` returns
both blocks in one call; s05's run report must print those counts — this
session ships the numbers, not the printer (§8).

`render_budget` also returns **`unreadable`**: the number of ledger lines that
would not parse, or that parsed and then failed the schema. They are COUNTED,
not skipped. Silently dropping them would turn a truncated or tampered ledger
into "the owner never ruled on that".

---

## 7 · The sheet's embedded state

The sheet embeds its state as JSON in

```html
<script type="application/json" id="cos-sheet-state"> … </script>
```

**Element id: `cos-sheet-state`** (`feedback.SHEET_STATE_ELEMENT_ID`).
**Schema: `cos-sheet-state/4`** (`feedback.SHEET_STATE_SCHEMA`) — `/4` since
2026-09-09, when the state gained `questions` (the owner-interview lane, INT-01,
below). It was `/3` from 2026-09-07, when thread rows gained `capture` (§4b), and `cos-sheet-state/2`,
bumped from `/1` by extension 6 (§10), which replaced the single
`verdict` control with three columns and added five state blocks.
s07 writes it; the page republishes itself with the owner's marks on Save;
s05's `--from-sheet` parses this element and appends record rows.
`feedback_sheet.validate_sheet_state(state)` MUST be called by both ends — a
shape checked only at write time still lets a truncated page look like an owner
who marked nothing. s04 ships the validator; s07 and s05 are the callers, and
neither exists yet (§8).

| Field | What s07 renders | Producer |
|---|---|---|
| `schema` | — | s07 (constant) |
| `date` | the sheet's date | s07 |
| `generated_at` | build stamp | s07 |
| `run_ids` | which runs the night comprised | s07, from the run ledger |
| `counts` | the summary rows: archived / ingested / drafted / held / stale | s07: archived/drafted from landed undo rows, ingested from the signed-ingestion join, held from judged dispositions, stale from judge-02 |
| `threads` | **one line per thread, each with THREE marks — `judgment`, `label`, `draft_mark` — plus a `note` box, the row's `tier`, its `draft_text`, its proposed `rules` and whether it is `shown` (EXTENSION 6, s02; it carried one `verdict` and one `rule` box from s05 until then)** | s07 renders; the **OWNER** fills the three columns, `note` and `rules`; s02's pen reads them back and DERIVES `verdict`, which stays `null` in the built page |
| `held_out` | `{k: 25, conversation_ids: […], population: N}` — the **k=25 held-out sample ids**, archived-not-drafted, sampled uniformly at random and shown regardless of confidence. It was `k=10` until s02 (2026-09-05): a missed-act rate needs a denominator, and 10 puts the 95% interval on a single miss at roughly 0.3-44.5% — a range that cannot tell a good night from a bad one | s07 |
| `stale_archived` | the **'stale, archived' list** from judge-02 | s07 |
| `standing_rulings` | every live rule with `rule`, `rule_key`, `confirmations`, `expires_at`, a **`revoke` flag** (the revoke control), the retired rule's own `conversation_id` + `subject_sha256` (EXTENSION, s05), and its `fired_count`, `contradicted_count` and `in_prompt` (EXTENSION 6, s02) | `feedback_render.ranked_rules`; `revoke` filled by the OWNER, read back by s05 |
| `overturned_last_time` | what the owner overturned on the previous sheet | s07 renders, s05 supplies |
| `excluded` | `{rules: N, thread_rulings: N}` — rulings omitted from the capped **prompt** projection, counted by kind; the sheet itself still lists every live rule | `feedback_render.render_budget` |
| `door_check` | `{verdict: …}` — the **door-check verdict**, one of `open` / `closed` / `skipped-not-signed-in` | **THE RUN LEDGER (s06)**, not the feedback record |
| `batch_stop` | `{reason: …}` — the **batch stop reason**, one of `backlog-empty` / `batches-reached` / `door-closed` / `session-died` / `not-started` | **THE RUN LEDGER (s06)**, not the feedback record |
| `feedback_text` | the **one free-text box** at the bottom of the sheet | the OWNER, read back by s05 |
| `questions` | `{rows, applied, quiet}` — **the questions the vault asks the owner** (INT-01, 2026-09-09): each row is `{key, shape, asked_on, expires_on, question, evidence, options, default, target, change, answer, note, status}` with `options` a list of `{action, label}` and `default` always `skip`; the page writes the picked option's `action` into `answer` and the free text into `note`. `applied` lists the lines the last answers produced; `quiet` says the lane is down to one question a day. Present and empty when there is nothing to ask | `interview.sheet_block` (the nightly's `brain interview --nightly`, run inside `chain_finish` before the sheet build); answers ride the marks file as `answers` and are applied by `interview_apply` on the next night |
| `category_legend` | one entry per category ON THIS PAGE — `{category, disposition, means}` — saying what that category CAUSES; **empty** when the taxonomy is off or unparseable (EXTENSION 5, s08) | s07, from `_taxonomy.ingest_taxonomy` — the OWNER's own `overlay/cos/ingest.md`. Never read back |

Three rules the validator enforces, each of which is a real failure mode:

1. **An unmarked thread row's `verdict` is `null`, never a default.** A sheet
   shipping every row pre-marked `right` manufactures agreement the owner never
   gave, and s09 would score it as a real denominator.
2. **`door_check` and `batch_stop` come from the run ledger.** s06 writes those
   as named ledger fields precisely because `sheet.py` does not exist when s06
   runs. `SHEET_STATE_PRODUCERS` says so per field, and a test asserts it.
3. **`held_out.conversation_ids` holds `k` ids, or the whole population when it
   is smaller.** A short sample is a smaller denominator, not a full one — s09's
   only unbiased overturn rate depends on knowing which.

**Text boundary.** `threads[].subject` carries the display subject: the owner
has to recognise the thread on a page they read in a minute. The durable
record never does — only `subject_sha256`. When a subject reaches the record,
it is hashed on the way in by `record_rule` / `record_thread_ruling`.

---

## 8 · What this session did NOT build

Deliberate, so the next session does not go looking:

- **No pen is wired.** `brain cos feedback --from-sheet` and the enumeration's
  Outlook diff are s05's (`feedback_cli.py`, `tools/cos_driver.py`).
- **No prompt block is assembled.** The RULINGS block beside the vault context
  map is s05's (`tools/cos_judge_grounding.py`).
- **No sheet is written.** `src/brain/cos/sheet.py` and its template are s07's.
- **Headroom note for s05:** `feedback.py` is 460 LOC against the 500-LOC file-size ratchet. Put the CLI in `feedback_cli.py` and the prompt block in the grounding tools, as fb-02/fb-03 already say — do not grow this module past the bound.
- **No confirmation is detected.** `record_rule` computes the counter when a
  repeat arrives; deciding that a later night's evidence *agrees* with a rule
  is s05's job.

---

## 9 · What s05 built on it, and the extensions the sheet has recorded

**The two pens** are `src/brain/cos/feedback_cli.py`:

| | Pen 1 — the sheet | Pen 2 — Outlook |
|---|---|---|
| Entry point | `brain cos-feedback --from-sheet <html>` | `cos_driver --enumerate-only` (`cos_driver_enumeration.owner_reversals`) |
| Input | the saved page's embedded state | the undo ledgers x tonight's Inbox enumeration |
| Mints | a THREAD ruling per `wrong`/`missed` mark, a **RELEASE** (a `right` thread row) per `right` mark on a thread currently held do-not-touch, a RULE row per typed rule, a CONFIRMATION per `right` mark on a subject a live rule was minted from, a `revoked` row per revoke tick | a THREAD ruling per landed mutation the owner undid |
| Refuses | a page with no state element, no JSON, or the wrong shape — an unreadable page is not an owner who marked nothing | anything whose message set CHANGED: a newer message means new work, and an unreadable `received`/`action_ts` is treated as new work, never as agreement |
| Repeats | NOT idempotent — reading one saved sheet twice appends twice (ceiling stated in `record_from_sheet`; a human runs it, once per sheet) | idempotent — an outlook ruling is minted once per landed mutation, so the thread staying in the Inbox does not re-mint it nightly |

**The two consumers, and they are not the same thing.**

* **The prompt** (`cos_judge_grounding.rulings_block`, rendered into
  `TRIAGE_PROMPT` and `DRAFT_PROMPT` above `RULES THAT BIND`, exactly where the
  voice profile sits and for the same doctrine-quotation reason) carries the
  CAPPED view: 40 ranked rules and 80 projected thread rulings. Excluded
  rulings are COUNTED in the prompt and NAMED in the run report
  (`cos_judge --batches` prints `rules_excluded_keys` and
  `thread_rulings_excluded_digests`) — naming 281 digests inside a model
  message is the ~250-row overflow the ceiling exists to prevent.
* **The host screen** (`cos_mutate_plan.screen_owner_rulings`) reads the FULL
  ledger, uncapped, and drops every planned mutation on a do-not-touch thread.
  It runs over the ASSEMBLED plan after every lane has spoken — archive, chip,
  ingest mark and draft — because an exclusion honoured in one lane while
  another spends a slot on the same thread is not a guard.
  **A thread ruling changes what the porter DOES; a rule row changes what the
  judge is TOLD.** Rule text is the owner's free prose and no host code can
  evaluate it, so it is quoted to the model and nothing more. That ceiling is
  stated here so nobody reads the rules block as a mechanism.
  Only `verdict: wrong` is a do-not-touch: `right` is agreement and `missed`
  asks for MORE rather than less. `feedback.DO_NOT_TOUCH_VERDICT` is the one
  definition of that test and `feedback_render.held_threads` the one fold that
  applies it — the host screen and the prompt block both call it, because they
  did drift: the prompt rendered `missed` rulings under a heading promising a
  host refusal that only ever covered `wrong`, telling the judge to leave alone
  the thread the owner had asked it to act on. `missed` now renders as its own
  list, under its own heading, which states plainly that nothing enforces it.
  A later `right` ruling releases a thread an earlier `wrong` one held, and that
  release HAS A PRODUCER reachable from a pen: a `right` mark on the morning
  sheet for a thread currently held (`feedback_cli._mark`). Without one, a
  thread mis-marked `wrong` on one night was excluded from every mutation lane
  forever with no in-product way back — a documented escape hatch no shipped
  code could open. A `right` mark on a thread nobody is holding mints nothing.
  **Pen 2 cannot release**: it reads the Inbox, and a thread the owner files
  back into Archive is not visible there. The sheet is the only release lane,
  and that ceiling is stated rather than implied.
  A ledger line the reader could not parse is REPORTED on both sides rather
  than halting the night — `plan["owner_rulings"]["unreadable_record_lines"]`
  and a `WARNING:` line in the rulings block. The failure self-heals in the one
  direction that matters (the thread is archived once more, the owner pulls it
  back once more, and pen 2 mints a fresh readable row); stopping the mailbox
  automation on one truncated byte does not.

**The three extensions**, each because a consumer genuinely could not do its job
without it:

1. **`threads[].rule`** — a per-thread rule box on the sheet. Without it the
   sheet gives the owner a verdict, a note and one page-wide free-text box, and
   NONE of those can produce a rule row: `rule` is documented above as coming
   from "the owner / s05", and there was nowhere for the owner to write one.
   `feedback_text` still mints nothing — a rule row is keyed on a thread and a
   page-wide box names none. It is read back into the pen's report instead.
2. **`standing_rulings[].conversation_id` + `.subject_sha256`** — the retired
   rule's own provenance, carried back with the revoke tick. A revoke is
   appended as a rule row and a rule row is keyed on a thread; without these the
   retirement would have to invent a conversation id and stamp `sha256("")` as
   the subject of a thread that had one.
3. **`record_rule(subject_sha256=…)` / `record_thread_ruling(subject_sha256=…)`**
   — the already-hashed subject, for the callers in (2) and for the sheet rows,
   which carry the record's own digest rather than the text it was made from.
   `_subject_hash` is the ONE decision point: give a subject or a digest, never
   both.

**Two more extensions, s08 (2026-08-27), and this time the consumer is the
OWNER.** The first sheet published against a live mailbox came back with one
report: it is unreadable. It asked for a verdict on an action while showing
neither the mail's date, nor the files that came with it, nor why the porter
acted, and it named four controls — Right, Wrong, Missed, Standing rule — that
the page never defined. A verdict on grounds the owner cannot see is not a
verdict, and s09's overturn rate is computed from exactly those verdicts.

4. **`threads[].received` / `.category` / `.reason` / `.files`** — display-only
   context, every field already present on the run's ingestion-ledger row
   (`received`, `disposition`, `held_reason`, `category`,
   `attachment_manifest`). No new capture. `reason` is a plain sentence built
   from the disposition and the hold reason; an unmapped `held_reason` is
   QUOTED, never dropped. `files` reads `attachment_manifest` and NOT
   `attachments` — the latter counts inline images, so it would list
   `image.png` three times and call them the files on the thread. s05 never
   reads any of the four, but the thread key set is CLOSED, so a sheet saved
   without them must refuse as a version skew rather than read as a thread that
   had no date and no files.
4b. **`threads[].capture`** (the key is `capture`) — WHAT REACHED THE VAULT, as two separate
   answers, added 2026-09-07 on the owner's request: *"important to
   differentiate email body ingestion and file attachments ingestion too"*.
   The dict carries `body` (`in the vault` / `not needed` / `NOT captured`),
   `body_why` (the row's own `held_reason`, so a refusal he can act on reads
   differently from one he cannot), `files` (`in the vault` / `NOT captured` /
   `none attached`) and `files_count`.

   THE TWO LANES HAVE DIFFERENT AUTHORITIES. `body` comes from
   `signed_ingested_catching_up` — the sheet's own, which counts a note signed
   by a later catch-up run — and `files` from
   `signed_attachment_conversations`, the file lane's own end-to-end walk.
   Measured on `2026-09-07-run269`: 68 bodies in the vault, 40 not needed, 1
   judged worth keeping that did not land, and 22 threads whose files went in
   with them. Merged into a single word, none of that is visible on the only
   page the owner reads. NOTE for a later reader: the SINGLE-RUN function
   `signed_ingested_conversations` answers 22 on the same run, and using it
   here would report 36 threads as missing that a catch-up run had already
   signed. Display-only like the
   four fields above, and inside the same CLOSED key set, which is why
   `SHEET_STATE_SCHEMA` went to `/3`.

5. **`category_legend`** — what each category word on the page CAUSES. The
   words are the OWNER's, from `overlay/cos/ingest.md`, and the engine holds no
   definition of one beyond the disposition that file gives it: the legend
   therefore states the disposition and never invents a meaning. It covers only
   the categories present on this page. An absent or unparseable taxonomy makes
   it EMPTY, and empty must never be confusable with absent — hence a required
   key that may be an empty list. The page also states in words that the
   category is context and is not the thing being judged.

---

## 10 · Extension 6 (s02, 2026-09-05) — three marks, and a file the browser saves

The sheet built by s07 could not be used. Two causes, both measured on the one
sheet the owner opened:

* **every control was `<input disabled>`** until `claude.use('artifact')`
  resolved, and that capability does not exist on `file://` — which is where
  the owner opens the page, on his own Mac. Nothing could be marked at all.
* **it asked ONE question** — right / wrong / missed — about an action that is
  really three decisions. *Right archive, wrong label* and *right to draft, but
  that draft is too long* were both unsayable, so an owner who meant either
  picked whichever word was least wrong.

### 10.1 · The three columns

| Column | Values | Where the vocabulary comes from |
|---|---|---|
| `judgment` | `right`, or `<bucket>:<tier>` — the twelve should-be values `act:P0`, `act:P1`, `act:P2`, `act:P3`, `read:P0`, `read:P1`, `read:P2`, `read:P3`, `noise:P0`, `noise:P1`, `noise:P2`, `noise:P3` (`sheet_marks.JUDGMENT_VALUES`) | `BUCKETS` × `TIERS`, asserted equal to `tools/cos_judge_rules.py`'s own |
| `label` | `right`, or any entry in this sheet's `label_vocabulary` | the OWNER's `overlay/cos/ingest.md` taxonomy, so it is checked against the sheet and never against a constant here |
| `draft_mark` | `send-as-is`, `not-needed`, `tone`, `facts`, `too-long`, `missing-point` (`sheet_marks.DRAFT_VALUES`) | fixed; they are the four things the drafting prompt can act on |

**`""` — UNSET — is a first-class fourth value on every one of them, and it is
never filed.** An unmarked row is UNKNOWN: not agreement, not disagreement. So
no control is pre-selected (`sheet_render.UNSET_LABEL` is the selected option
on a fresh page), the saved file carries only rows the owner actually answered,
and `rows_shown` travels beside them as the denominator any later rate needs.
A row whose three columns are all unset is refused if one ever reaches the pen
— finding one means a page filed silence as an answer.

A thread row therefore carries, alongside the three columns: its
`conversation_id` and `conversation_id_digest`, its `subject` and
`subject_sha256`, the `action_taken`, the judge's own `tier` (from
`judged_tier` — the question is whether the porter judged this right), the
`reason`, `received`, `category` and `files` context of extension 4, the
`draft_text` the drafting leg actually wrote, whether it is `held_out`,
`stale_archived` or `shown`, the `note`, the proposed `rules`, and `verdict`
— which stays `null` in a built sheet and is derived at read-back.

**The porter's own verdict is shown as TEXT, never as a pre-selected control.**
That is the Argilla / Label Studio shape — a RECORD, a closed QUESTION per
column, the model's output as a SUGGESTION, the human's answer as a RESPONSE.
Neither tool is adoptable (both need a running server; this page must open from
`file://` with nothing running) but the shape is exactly right.

**The old three words are DERIVED, not replaced** (`sheet_marks.derive_verdict`),
so the record stays ONE record and every row in §2 and §3 keeps its meaning:

* `missed` — the owner wants MORE than happened (`act:*` on a thread nothing
  was drafted for; `noise:*` on a thread still in the Inbox);
* `wrong` — the owner wants LESS (`read:*` on a thread that was archived,
  drafted for or chipped). Only this is a do-not-touch;
* `right` — everything else, **including a row whose judgment is right and
  whose label or draft is not**. A mis-filed label is not a reason to stop the
  porter ever touching a thread again; those corrections travel as RULE rows,
  which change what the judge is TOLD without freezing a thread.

### 10.2 · The rule a should-be mark proposes

A should-be answer offers a rule sentence from a template per (column, value) —
`sheet_marks.RULE_TEMPLATES`, substituted by `suggested_rule`. **No model call**:
the page has to work with nothing running, so the same table is embedded in the
state as `rule_templates` and the browser substitutes it identically. The owner
edits the sentence or clears the box to propose none.

The templates name a CLASS (the thread's `category`, or `uncategorised`), never
this one conversation — a rule that can only ever match one thread is a thread
ruling wearing a rule's clothes, and the record already has the right kind for
that. Marking the same rule twice yields ONE rule with two confirmations,
through the `rule_key` dedupe that already existed (§5).

Two new fields on the rule row make a revoke decision possible:

| Field | Meaning | Producer |
|---|---|---|
| `rule_origin` | `<column>:<value>:<category>` — what the rule was made FROM. `""` for a rule the owner typed with no should-be behind it | `sheet_marks.rule_origin` |
| `fired_count` | threads this rule applied to that the owner has since answered | `record_rule(fired=…)`, carried forward |
| `contradicted_count` | of those, how many the owner answered a DIFFERENT way | `record_rule(contradicted=…)`, carried forward |

`rule_origin` is what makes the second counter computable at all: nothing
records which rules the judge actually weighed on which thread, so two rules
CONTRADICT when they came off the same column and the same category with a
different value (`sheet_marks.contradicts`). **Stated ceiling:** `fired_count`
therefore means "applied to, by column and category", not "the judge read this
rule and acted on it". A contradiction is NOT a confirmation — `record_rule`
takes `confirm=False` on that path, because counting a disagreement as
agreement would push the rule up the ranking and out another 90 days.

### 10.3 · The marks file, and the two transports

Save writes a JSON file through a Blob download — `cos-marks-<date>.json`, on
the owner's own Mac, read back with `brain cos-feedback --from-marks <path>`.
The Artifact path is KEPT as an optional SECOND transport
(`--from-sheet <html>`), and it is a transport and not a second
implementation: both routes normalise to the payload
`sheet_marks.marks_from_state` produces and land in `feedback_marks.record_marks`,
the ONE writer.

```json
{
  "schema": "cos-marks/1",
  "sheet_id": "1f0a9c4e7b2d5a83",
  "date": "2026-09-04",
  "run": "2026-09-04-run190",
  "transport": "file",
  "rows_shown": 36,
  "marks": [{"conversation_id": "AAQkAD…", "subject_sha256": "9f86d081…",
             "action_taken": "archived", "category": "market-digest",
             "judgment": "read:P2", "label": "", "draft": "",
             "draft_text": "", "note": "I wanted to keep this one",
             "rules": [{"column": "judgment", "value": "read:P2",
                        "text": "Threads categorised market-digest are for reading only — leave them in the Inbox rather than archiving them."}]}],
  "revoked": [],
  "feedback_text": "",
  "content_sha256": "…"
}
```

The file's own keys are `schema`, `sheet_id`, `date`, `run`, `transport`,
`rows_shown`, `marks`, `revoked`, `feedback_text`, `answers` and `content_sha256`; a
`revoked` entry carries `rule_key`, `rule`, `conversation_id` and
`subject_sha256`, and a proposed rule carries `column`, `value` and `text`.
An `answers` entry (INT-01, 2026-09-09) carries `key`, `action` and `note` —
the interview question's key, the option ACTION the owner picked (never the
label, which the phrasing leg may reword) and the free text beside it; the
list is present and empty when nothing was answered, and it is INSIDE the
`content_sha256` digest on both ends. `sheet_select.record_consumed` copies
it onto the consumed-sheets row, where `interview_apply` reads it.

`MARKS_FILE_KEYS` and `MARK_KEYS` are **closed sets**, checked by
`sheet_marks.validate_marks` at BOTH ends — the sheet builder on the payload it
derives, the CLI on the bytes the browser wrote. A file carrying an unknown
mark value, an unknown key, or a `content_sha256` that does not cover its own
marks is **REFUSED, not skipped**: a page this reader cannot fully understand
must never become a partial owner ruling. A file whose rows are all unset is
not a refusal and not an agreement — it files NOTHING and reports zero marks.

**Identity is NEVER the filename.** A browser asked to save the same download
twice writes `cos-marks-<date> (1).json`, so a reader keyed on the name would
file the same marks twice and inflate every counter they touch. The key is
`sheet_id` — `sha256(date \x00 generated_at \x00 run_ids)[:16]`,
`sheet_select.sheet_id` — and a second filing of one is refused against a
host-private consumed ledger (`sheets-consumed.jsonl`, schema
`cos-marks-consumed/1`, beside the record for the same reason the record is
there: a file an untrusted leg could delete is a second filing of marks already
filed). Rebuilding the night produces a NEW id, which is correct — those are
different sheets and their marks are different marks.

**Which route a mark came in on is READABLE FROM THE ROW.** Both row kinds
carry `transport` (`file` \| `artifact`, `feedback.TRANSPORTS`) and `sheet_id`,
so "did this ruling come from the file or the Artifact?" is answered from the
record and not from a log. `run` travels inside the marks file — and inside
`content_sha256` — because the file transport has nothing else to recover it
from; it is `""` when the night ran more than once, exactly as §2 documents.

### 10.4 · Which rows the owner is asked to look at, and what his last marks did

Two more state blocks, both from `sheet_select`:

* **`selection`** — `{shown, total, reasons: {draft, held_out, loud_archive,
  outlook}}`. A row is SHOWN when a mark on it changes something: every draft
  (every draft WRITTEN, not every draft that landed — measured 2026-09-04,
  25 threads carried draft text and 20 of them landed), the held-out sample,
  P0/P1 archives, and threads the owner moved in Outlook since the last sheet.
  Everything else is still RENDERED, behind one toggle: **short is measured in
  rows he must LOOK AT, not rows the page contains.** Marking a hidden row
  counts exactly the same, so `rows_shown` never bounds the number of marks.
* **`effect`** — `{previous_sheet_id, previous_date, rows_shown, marks,
  ruled, seen_again, changed, sentence}`, rendered as the first thing on the
  page. The sentence carries its own denominator in words — "of the N rows you
  were shown" — and when no marks preceded it, it SAYS SO rather than printing
  a zero: a zero from "you have never marked anything" and a zero from "you
  marked nine things and none of them changed anything" are opposite findings.
  **`marks` and `ruled` are two different numbers and the sentence prints
  both.** `marks` is what the previous marks FILE carried, read off the
  consumed ledger; `ruled` is how many of those left a row in the record, and
  it is the denominator of `seen_again`. Counting record rows for both was
  wrong in each direction at once — a `right` on a thread nobody is holding is
  a real answer that mints no row, and a revoke tick mints a row against a
  thread the owner never marked, so a revoked row is excluded from `ruled`
  entirely. **Stated ceiling:** `changed` measures that the porter BEHAVED
  DIFFERENTLY, not that it did so BECAUSE of the mark.

And two the page needs to ask its questions at all: **`label_vocabulary`** (every
category the owner may re-label a thread TO — wider than `category_legend`,
which explains only the ones on this page; empty when the taxonomy is off, and
the LABEL column then offers `right` alone) and **`rule_templates`** (§10.2).

### 10.5 · What extension 6 did NOT change

The record's two kinds, their key sets, the dedupe, the cap, the ceiling, the
90-day window and the host-private placement are all untouched. A mark made in
the new vocabulary and a mark made in the old one reach the ledger as the same
row, which is why `derive_verdict` exists rather than a fourth verdict word.

---

## 11 · Extension 7 (s10, 2026-09-06) — Pen 3, the draft-to-sent diff, in its OWN file

`sent-diff.jsonl` sits beside `feedback.jsonl` in the same host-private
directory and is **not** part of the record frozen above. It carries what the
owner actually sent, scored against the draft the porter wrote:

```json
{"schema": "cos-sent-diff/1", "ts": "…", "run": "2026-09-05-run263",
 "conversation_id_digest": "…", "item_id": "…", "diff_class": "light-edit",
 "similarity": 0.9474, "changed_words": 1, "draft_words": 9, "sent_words": 10}
```

`diff_class` is one of `sent-as-is`, `light-edit`, `rewritten`, `not-used`.

**Why a separate file, and this is the load-bearing part.** A thread ruling's
`wrong` verdict is a PERMANENT do-not-touch and its `right` verdict RELEASES an
earlier one (§4). Mapping "the owner rewrote my draft" onto either would retire
a live thread forever or lift a hold he never lifted. A rewritten draft says the
DRAFTING was poor; it says nothing about whether the thread should be touched.
Draft quality is a measurement and is filed as one — so no verdict, no
`action_taken`, and nothing in this file can block a mutation.

Same two disciplines as the record itself: **no mail text and no raw
conversation id** are ever written here, and a **missing or unreadable sent body
records NOTHING** — never `sent-as-is`, which is the most flattering class the
lane can mint and therefore the one silence must never produce.

Producers: `tools/cos_signals_sent.py` (the host-side join — which conversations
this lane drafted on, and the text it drafted) and `brain.cos.feedback_sent`
(the classifier and the append). The mailbox half is `cos_driver_page.js`, whose
`sentitems` projection now carries `conv_id`, plus an opt-in `sent-bodies` phase
capped at `SENT_BODY_CAP` fetches per night.
