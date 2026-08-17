# COS host-engine operations (CUT-01E) — broker, signer, corrections, priority map, hold store

Engine version: **0.17.0** (first version carrying these verbs). Everything
here is the HOST side of the chief-of-staff autonomy gates; the only VM-facing
surface is `cos-propose` (plus read access to the `shared/` projection).

## 1 · The canonical ops dir and its permission split

`$BRAIN_COS_OPS_DIR` (default `<vault>/.brain/cos` — gitignored wholesale via
`vault/.brain/`, never indexed by `scan_vault`, never exported by
`tools/export_cleanroom.py` because only git-tracked files ship). Surfaced by
`brain status --json` under the `cos` key.

| Sub-path | Zone | POSIX mode | Who writes | Who reads |
|---|---|---|---|---|
| `host/` | (a) host-private | `0700` | host only | host only |
| `host/evidence/` | signed trust-gate evidence bundles | `0700` | `brain cos-evidence sign` | host only |
| `host/proposals/` | broker queue **of record** (pending/, rejected/, expired/, corrections-pending/, claims.jsonl, batches.jsonl) | `0700` | broker | host only |
| `host/hold/` | auto-capture hold store | `0700` | `brain cos-hold` + broker | host only |
| `host/corrections.sqlite` | `correction_events` of record | `0600` | `brain cos-correct` / inbox answer-consumer | host only |
| `shared/` | (b) VM-readable projection | `0755` (files `0644`) | **host** (`cos-priority-map`) | VM + host |
| `drop/proposal-drop/` | (c) VM-writable input | `0775` | VM (`cos-propose`) | host claims |
| `drop/verdict-drop/` | (c) VM-writable input (shadow ledger + correction drops) | `0775` | VM (s04) | host claims |

The mode bits are best-effort (VirtioFS/Windows may only partially honour
them); the split is **also enforced behaviourally**: no VM-allowed verb ever
resolves a path under `host/`, and s04 reads (b) / writes (c), never (a).

## 2 · Proposal broker (the approval gate)

The trust invariant: **`brain sync` never signs an unconsumed proposal.**
The drain reads `.brain/drafts/`, `.brain/capture-inbox/` and the host-only
**approved queue** (§2c); a proposal drop lives in `drop/proposal-drop/`, which
no sync/ingest path touches.

Flow (each stage isolated; the broker runs standalone as `brain cos-broker`
and is **wired into the hourly `brain maintain` daily branch**, before the
first sync, so a VM drop becomes a queued owner-inbox batch within one
nightly interval and an owner-accepted candidate is signed in the same run):

1. **VM ingress** — `brain cos-propose` (VM_ALLOWED) stamps capture
   frontmatter and writes an UNSIGNED candidate into `drop/proposal-drop/`.
2. **Claim + validate** (host) — schema (`capture.validate`), classification
   tier, secret-scrub (private-key blocks, AWS/Slack/bearer tokens, `api_key:`
   style lines), safe-slug id, duplicate-pending id, and a **content-hash
   claims ledger** (`claims.jsonl`) that rejects a replayed drop. Valid →
   `host/proposals/pending/`; invalid → `rejected/` (logged, never silent).
3. **Batch enqueue** (host) — ALL pending proposals aggregate into ONE
   versioned `cos_ingestion_batch/v1` record: candidate `{id, sha256}` set, a
   canonical **digest**, and an **Ed25519 signature** over the digest by the
   host audit key (fail-closed: no key ⇒ no batch). One owner-inbox question
   per batch (`key = cosbroker:<batch_id>`, options `accept all` /
   `reject all` / `accept: <ids>` , default **reject all**). Backpressure:
   at most ONE open batch at a time (the owner queue is ~5-capped); new
   proposals wait in `pending` and join the next batch.
4. **Answer-consumer** (host) — reads ONLY the `cosbroker:`/`coscorrect:`
   namespaces (unrelated inbox entries are never consumed). Verifies the
   batch signature over the recomputed digest (tampered `batches.jsonl`
   fails), enforces subset validation, re-hashes each accepted pending file
   against the digest-time sha256 (drifted content is not promoted), and
   moves **only accepted candidates into the approved queue** (§2c) — whence
   the ordinary audited host drain signs + indexes them. Rejected →
   `rejected/`.
   A batch is consumed exactly once: a replayed answer is rejected, a late
   answer (post-expiry) is rejected, an unparseable answer requeues the
   candidates.
5. **Lifecycle** — proposal TTL `$BRAIN_COS_PROPOSAL_TTL_DAYS` (14d) →
   `expired/`; batch TTL `$BRAIN_COS_BATCH_TTL_DAYS` (7d) → batch expires,
   its inbox question is closed (`status: expired`, so a late `brain inbox
   --answer` is refused) and its candidates REQUEUE into the next batch —
   missed nights can never permanently jam the owner queue. GC
   (`$BRAIN_COS_GC_DAYS`, 30d) deletes aged rejected/expired files and
   compacts closed batch records.

Proposal states: `dropped → pending → queued → accepted | rejected | expired → GC`.

### 2a · Host-derived stamps and the run gate (STA-01/STA-02, 2026-07-31)

**The producer no longer tells the host what the host already knows.** Run 59
staged 8 candidates; every one arrived with no `category`, no
`extraction_rules_version` and no `bundle_version`, and was defaulted to the
never-graduable `unclassified` — while that run's own
`_cos_ingestion_ledger_<run>.jsonl` carried the right category beside each
proposal id. Facts the host knows are now written by the host.

**The run manifest.** `brain cos-run-begin` (HOST-ONLY, at run LAUNCH) freezes
`<run-id>.json` in the run-record store: the run id, the SKILL.md the executing lane will
load (resolved by `brain.cos_deploy` — the one definition of the lane rules),
that file's content digest, both producer versions read out of it, and the
artifacts the run owes. It is IMMUTABLE. Every later claim stamps candidates
from THAT record — never from whatever skill is deployed at claim time, because
`claim_drops` fires hourly and the deployed bundle changes between runs. The
host also publishes the assigned run id (id + start stamp only) to the
VM-readable `shared/current-run.json`, so the run names its artifacts after it.
The metrics row takes its stamps from the same manifest
(`tools/cos_reconcile_metrics.py --append` refuses a row for a run the host
never began, and refuses one that CONTRADICTS the manifest).

**Where the run records live, and why not under `host/` (gap-05, 2026-08-16).**
The manifest, the recorded verdict (`<run-id>.validity.json`) and the plan
binding (`_cos_plan_binding_<run-id>.json`) sat at `host/runs/` until
2026-08-16, described in three docstrings as "host-private" and "never
VM-writable". That was true of the VM's RULES (AGENTS.md §9: `.brain/` is
host-only and no VM_ALLOWED verb writes there) and false of the FILESYSTEM —
and §2c below had already ruled the same directory out for the approved queue
in as many words: "`.brain/cos/host/` would NOT do: it is visible on that
mount, and `0700` is only a boundary if the VM runs as a different uid AND
VirtioFS honours mode bits (neither is established)". These three files are not
artifacts a run writes; they are the authorities a run is judged BY — which
bundle it was allowed to be, whether its candidates may be claimed
(`CLAIMABLE_VERDICTS`), and which frozen plan its apply dispatched. They now
resolve through `config.cos_run_records_dir` — off the mount beside the writer
lock (INT-05) and the supersede journal (ENF-01), same helper, same app-data
fallback.

Records already on the mount are carried forward ONCE
(`cos.migrate_run_records`, called from `ensure_layout`, `cos.run_manifest` and
`cos_runverify.known_run_ids`) — historical runs stay verifiable, and the
carry-forward is one-time because an always-on import would be a channel for
writing into the run-validity store from the mount. Two copies of one record
FAIL CLOSED: identical bytes are the ordinary resumed migration, differing
bytes are refused, logged as a `run-record-mount-conflict` defect, left in
place as evidence, and take that run — and only that run — to INCONCLUSIVE in
`verify_run`. Neither copy is ever preferred. The store is NOT drain-first
work like the approved queue: it is evidence, and losing it makes historical
runs unverifiable rather than stranding pending work.

**What the run owes per staged candidate.** `brain cos-propose --json` returns
`id` and `sha256`; the run copies BOTH into its ingestion-ledger row beside the
category:

```json
{"disposition": "candidate", "proposal_id": "<id>",
 "content_sha256": "<64 hex>", "category": "<taxonomy name>"}
```

**The join.** At claim time the host looks that row up by id AND full content
digest, across every run ledger — never "the newest ledger" (one day can hold
several runs; 2026-07-31 held 58 degraded and 59 live). It must resolve to
exactly ONE run, ONE digest and ONE category. Be honest about what this buys:
the ledger is still VM-written, so the join makes the category TAMPER-EVIDENT
and SINGLE-SOURCED, **not host-authoritative**. The joined value is still a VM
claim — still validated against `overlay/cos/ingest.md`, still unable to select
the auto lane by itself. What it removes is "prefer the newest row", a rule a
hostile or buggy producer satisfies at will.

**The claim quarantine.** A candidate the host cannot attribute to a VALID run
is neither bound nor discarded: it waits in `host/proposals/claim-quarantine/`
with its reason recorded, counted in `route_stats` (`unjoined_claims`,
`quarantined_claims`) and named in `brain status` + the morning brief, exactly
like `unstamped_batched`. Quarantine reasons:

| code | meaning |
|---|---|
| `no-ledger-row` | no run's ledger accounts for this proposal id |
| `ledger-collision` | two runs, two digests or two categories claim it |
| `ledger-no-digest` / `ledger-digest-mismatch` | the row proves nothing about THESE bytes |
| `no-run-manifest` | the host never began that run |
| `run-invalid` / `run-inconclusive` | the host validator (INS-01) scored the run, or has not — **an absent verdict is INCONCLUSIVE and quarantines like any other; there is no permits-claiming interim** |
| `no-run-attribution` | bound before these stamps existed (the run-59 shape) |

Every pass re-runs the gate over the quarantine, so a queue built while a run
was unscored drains by itself the hour the validator scores it VALID. Nothing
is stranded, and nothing from an unproven run reaches the owner batch, the
evidence keys or the signed drain. Candidates from a run that FAILS validation
are **never re-stamped** — that would launder the output of a provably
uncontrolled run into the trusted pipeline; the content is recovered by
re-extracting the same threads on a run that passes.

### 2b · The host run validator (INS-01, 2026-07-31)

**Doctrine cannot police itself.** Run 59 skipped its ENTIRE self-eval — zero
E-check output across its 16 artifacts — and nothing noticed, because the only
thing checking a run's homework was the run's own report. E16, written to catch
a candidate with no stamps, never executed. So the check lives where it cannot
be skipped: `brain.cos_runverify`, called from `cos_broker_fold` (hourly, no new
scheduling) and standalone via `python3 tools/cos_run_verify.py <vault>`.

**Four states, not pass/fail.** `VALID` · `VALID_DEGRADED` · `INVALID` ·
`INCONCLUSIVE`. `VALID_DEGRADED` never collapses into an ordinary pass — it is
both "the run degraded and said so correctly" and "the host could not
re-execute this control". `INCONCLUSIVE` (the validator could not run) is
surfaced as loudly as `INVALID` and blocks claiming just as hard: a validator
that could not run is not a validator that passed.

**Only completed runs are scored.** Completion is host-owned end to end: every
artifact the manifest declared at LAUNCH is present, and the run's own artifacts
have stopped changing (`$BRAIN_COS_RUN_QUIESCE_SECONDS`, default 900). Anything
short of that is PENDING — no verdict recorded, so the claim gate reads
INCONCLUSIVE and the candidates wait. Every verdict records the digest of the
inputs it was computed over, so a changed manifest or a substituted artifact
re-validates on the next pass instead of resting on a cached verdict.

**Controls are RE-EXECUTED, not read.** Reading a self-eval block's shape proves
only that a string was printed.

| check | what the host does |
|---|---|
| `completion` | observes the manifest's declared artifact set on disk |
| `self_eval` | counts the report's check results against the count re-derived from **the manifest's** skill digest — never from today's `SKILL.md` |
| `metrics_row` | requires the four Phase-1.6 fields + the host stamps, then **RECOUNTS** all three ingestion counters from the run's own ledger |
| `ingestion_ledger` | re-runs `tools/cos_reconcile_metrics.py`'s observation guard (its false-alarm classes are not re-implemented here) |
| `candidate_stamps` | E16's stamp clause where v5.39 put it — one unique proposal id, a real sha256 digest, no invented category |
| `degrade_consistency` | cross-artifact: a degrade marker beside a populated ledger, non-zero counters, an enumerating mail leg, or candidates the HOST received is a FAIL |
| `contract` | **re-executes `tools/cos_contract.py`** over the run's raw PRE/POST snapshots and ledgers and compares the verdict + clauses to the recorded block |

Where a control genuinely cannot be re-executed — the run's skill bytes are
gone, the checkers are not installed beside the engine — the row says so and
scores `degraded` or `inconclusive`, never `pass`.

**The degrade exemption is cross-artifact, never a marker.** Run 58 was a
LEGITIMATE degrade: Outlook signed out, the mail leg correctly stopped, an
honest `zero-eligible` marker, a contract that honestly FAILED. It scores
`VALID_DEGRADED`. A guard that fires on honest degradation gets muted — which is
exactly how E16 stayed trusted while vacuous. So a FAILED contract is exempt
ONLY when the degrade is TOTAL and consistent everywhere (0 enumerated, 0
ledgered, 0 reported, 0 candidates the host received). Run 59's shape wearing
run 58's marker fails, as it must.

**And it blocks.** The verdict is not a counter: `claim_drops` quarantines the
candidates of any run not `VALID`/`VALID_DEGRADED` (§2a). It is also loud —
`route_stats.invalid_runs` / `.inconclusive_runs`, a `brain status` WARNING, a
morning-brief line and a `hot.md` log entry, exactly like `unstamped_batched`.

Known positives: `tests/test_cos_runverify.py` (runs 57/58/59's shapes, each
sub-check fired on its own, the adversarial marker fixture) and, on a host that
has them, the REAL run 57/58/59 artifacts.

### 2c · The approved queue (INT-01/INT-04) — accept to signature, with no writable gap

An owner-accepted candidate used to wait in `capture-inbox/`, which is
**VM-writable by design**: `brain --role vm draft-capture` can overwrite the
same id, and the drain then re-read those mutable bytes and signed them. The
accept covered one set of bytes; the signature covered another.

Two properties close that, and both are needed:

- **Location.** The approved payload waits in the per-vault **host app-data
  dir** (`config.index_dir(...)/cos-approved/`, the ADR-0008 query-ledger
  precedent) — physically outside `<vault>/`, so it is not on the Cowork
  VirtioFS mount at all. `.brain/cos/host/` would NOT do: it is visible on that
  mount, and `0700` is only a boundary if the VM runs as a different uid AND
  VirtioFS honours mode bits (neither is established). A queue that resolves
  inside the vault (a misconfigured `$BRAIN_INDEX_DIR`, or a symlink that lands
  back inside) is **refused**, never used.
- **An unforgeable anchor.** The approved sha rides in an Ed25519-signed
  `cos_approved_anchor/v1` record beside the payload, signed with the same host
  audit key the batch digest uses — held in the OS secret store, not on the
  filesystem. Location alone would leave the "one write substitutes both" hole:
  whoever can write the payload can write a plain sidecar sha next to it, after
  which a signing-time check verifies the attacker's hash against the
  attacker's bytes and passes.

Two upstream paths feed the queue, and BOTH are authenticated, because a
guarded waiting room is worth nothing if the thing that decides what enters it
is unauthenticated and on the mount:

- **The owner answer.** `consume_answers` verifies the batch's Ed25519
  signature, the owner's inbox answer and the accepted subset — and the
  CRASH-RECOVERY path re-derives its decision through that same routine
  (`_verified_decision`) instead of trusting the journal it found. The journal
  and `batches.jsonl` are both on the mount; a forged pair used to be enough.
- **The hold record.** A `cos_hold_record/v1` authorization is signed at hold
  CREATION over (id, content sha, `not_before`, authorization, vault), and
  release reads `not_before` from the SIGNED body and re-hashes the payload
  against the signed sha. Two forged files in `host/hold/` used to buy a
  host-signed approval anchor. Unverifiable records are refused — marker AND
  payload quarantined together as `<id>.refused.{json,md}` plus a defect — never
  released. A hold parked by a **pre-INT-01 engine** is unsigned by definition:
  it takes the same quarantine, with a defect that says *upgrade*, not
  tampering. Recovery is an operator action (re-propose the payload through
  `cos-propose`), deliberately not an automatic re-entry that would skip claim
  validation.

### 2d · The capture corpus (CAP-01/CAP-02) — keep what the run READ

Nine runs of `_cos_ingestion_ledger_<run>.jsonl` are on disk and **the longest
text field in any of them is 219 characters.** The ledger records the verdict
(`verdict`, `category`, `body_opened`, `tier`, `held_reason`) and discards the
message text that verdict was made from. Two consequences, both measured:
re-judging anything costs a 90-minute live run against a real mailbox, and run
64 could rebuild run 63's ledger with no host-side artifact able to tell.

`src/brain/cos_corpus.py` keeps the input. The corpus is **evidence, never a
judgment** — nothing in this module decides anything; the replay harness is
what re-runs a judgment over it.

#### The format

One **append-only JSONL file per run**, named for the run id
(`<YYYY-MM-DD>-run<N>.jsonl`), one row per thread. Schema
`cos_capture_corpus/v1`:

```json
{"schema": "cos_capture_corpus/v1", "run": "2026-08-02-run99",
 "classification": "MNPI", "conversation_id": "<thread-root@example.com>",
 "captured": "2026-08-02T03:14:07Z",
 "text": "Contoso confirms the revised annex.\n\n> Northwind asked for Friday.\n",
 "text_sha256": "0dcf770eb195825f4f499c9d046fc08fdb18cc253d918d819875f433bd81095e",
 "chars": 67,
 "provenance": {"sender": "Alice <a@example.com>", "sent": "2026-08-02T09:14:00Z",
                "subject": "RE: annex"},
 "secret_findings": [], "read_lane": "chrome-plugin", "body_opened": true}
```

`conversation_id` is the **join key back to the ingestion ledger**: the ledger
keeps the verdict, the corpus keeps the input, and the pair is what makes a
verdict re-checkable. An absent provenance field is recorded as absent
(`null`) — never as `unknown`, the placeholder shape the category lane already
had to outlaw.

**The file is closed, once, by a final `cos_capture_corpus_close/v1` record**
carrying the row count, after which the file is dropped to `0400`. That record
is what distinguishes a crashed capture stage from a genuinely quiet night: an
unclosed corpus says the run died mid-capture, a closed one carrying `rows: 0`
says there was nothing to read. It lives INSIDE the jsonl rather than beside it
so a corpus copied to the replay harness carries its own denominator with it.

**The count is a convenience, not a security claim, and there is deliberately
no digest over it.** The independent check on how many threads a run read is
the run's OWN ingestion ledger, which counted the same threads from the other
side — `wir-03` joins them. A hash over the same bytes that produced the count
proves only that the file did not change after it was written, which is not the
failure this artifact exists to catch. An earlier draft carried a chained
whole-row digest, a per-run lock over the existing append lock, per-row `fsync`
and a six-case refusal state machine; all of it defended a plaintext file on a
single-user laptop against an attacker who could simply read it, so it was cut
(owner ruling, 2026-08-02). The chmod stays, because without it
"write-once" is a check the next caller can forget to make. Pruning is
unaffected — unlinking needs write permission on the DIRECTORY, not the file.
A night that read nothing still closes, with `rows: 0` — the `zero-eligible`
shape, so an empty night and a crashed one are different artifacts.

**Untrusted input is bounded, and over the bound a row is REFUSED, never
trimmed.** Every field arrives from the browser leg: `text` caps at 1 MiB
(three orders above the run's own 4000-character extraction budget — it only
ever trips on a runaway page dump) and every other field at 4096 characters —
`read_lane` and each provenance value by character count, `extraction` by the
bytes its canonical JSON encoding adds to the row, and `body_opened` must be an
actual boolean (it is read as a fact about what the run did, and a truthy
string is not that fact). A silently shortened body is a corpus that lies about
what the judge read, and a silently shortened `conversation_id` joins to
nothing while looking like a perfectly good row.

**The run id must carry a real, non-future calendar date.** It reaches the host
from VM-writable directory names and only its SHAPE was ever checked, so
`2026-99-99-run1` was a legal filename — and since retention reads the date out
of that filename, such a corpus would be filed "unrecognized" and kept forever.
Refused before a file exists, along with any date more than 2 days ahead of the
host clock.

**Damage is REPORTED on read, not policed on write.** `corpus_status` returns
`rows`, `declared_rows`, `bad_lines` (lines that would not parse — a torn
append leaves one), `duplicate_conversation_ids` (a retried thread appended
twice), and a plain-language `reason` when `complete` is false. A replay reads
that and decides; nothing here refuses a close or rolls back a write to defend
a number the ledger already carries independently. This is the deliberate
trade: one function that counts and explains, instead of a state machine that
adjudicates.

**Deliberately NOT SQLite.** Three reasons, in order of weight:

1. An authoritative SQLite corpus would be the ONE un-rebuildable database in
   a system whose stated architecture is "flat files are truth, every index is
   a disposable cache" (AGENTS.md §1). Losing the index costs a rebuild;
   losing this costs a night of real mail that cannot be re-read.
2. SQLite is single-writer even in WAL mode, and this repo has already spent an
   arc (CC-01/CC-02) on exactly that contention between the hourly job and a
   hand-run command. A corpus written mid-run must not be able to block, or be
   blocked by, an index write.
3. 120 rows a night is not a database problem.

The **dual-sink** pattern is the intent: the JSONL is the authoritative
immutable record, and any queryable index built over it later is derived and
rebuildable from these files — never the other way round.

The writer takes no browser and no mailbox anywhere in its call, so the replay
harness and the test fixtures build corpora through the same
`cos_corpus.append_thread` the run uses. A synthetic corpus and a real one are
the same artifact, or the harness is measuring a different format from the one
the run writes.

#### How the nightly writes it (WIR-01) — `brain cos-corpus-append` / `-close`

**Writing the corpus is part of READING, not a step afterwards.** Phase 1.6
rule 1½ (SKILL.md v5.44) saves each body at the moment it is extracted and
BEFORE rule 2 judges it — an appended "and then save everything" step is
exactly the kind a 100k-line run skips, which is how the phase's body pass came
to not execute at all on run 65.

```
# one thread whose body was opened — the text on STDIN, never in argv
printf '%s' "$BODY" | brain cos-corpus-append --run-id "$RUN" \
    --conversation-id "<AAQkAD...@example.com>" \
    --sender "…" --sent 2026-08-02 --subject "…" --read-lane chrome-plugin

# the threads enumerated and never opened — no text to lose, so one call
brain cos-corpus-append --run-id "$RUN" --read-lane chrome-plugin \
    --bodyless "<c1@example.com>" "<c2@example.com>" …

brain cos-corpus-close --run-id "$RUN"
```

- **One row per IN-SCOPE thread**, the same denominator as that run's
  `_cos_ingestion_ledger_<date>-run<N>.jsonl`, joined on `conversation_id`.
  The ledger holds the verdict; the corpus holds the input it was made from.
  The rows carrying text are exactly the ledger's `body_opened: true` rows,
  so `corpus rows == ledger rows` and `bodied rows == body_opened count ≤ the
  run's declared open cap`. That is the identity `wir-03` checks host-side.
- **A row cannot claim an open that did not happen.** `--conversation-id`
  with empty text is REFUSED (exit 3) and names `--bodyless` instead: a row
  carrying text asserts the judge saw that thread.
- **A mid-batch refusal names how many rows already landed** — the corpus is
  append-only, so a caller told only "refused" re-sends the whole batch and
  duplicates everything that went in.
- **Both verbs are HOST-BROKER only** and are the only two the nightly invokes
  without `--role vm`. The corpus is unfiltered MNPI mail bodies: on the Cowork
  VM they refuse, which is the correct answer there. `cos-ops/` is on the
  VM-visible mount and is never a home for a mail body.
- **Closing is not optional.** `prune` deletes only CLOSED corpora, so an
  unclosed one is unfiltered mail at rest indefinitely; and a corpus closed
  carrying `rows: 0` is what makes a quiet night distinguishable from a
  capture stage that died.
- **A close that certified ZERO rows can be retracted — and nothing else can.**
  Run 68 (2026-08-03) hit a transient tab-binding failure, closed with
  `rows: 0`, recovered six minutes later, and lost three real message bodies to
  `CorpusClosed`. `brain cos-corpus-reopen --run-id "$RUN"` puts the file back
  to 0600 and appending resumes; closing again records the TRUE count. A close
  carrying one or more rows is FINAL — a replay may already have used that
  denominator — so the engine refuses it, with no force flag and no repair
  path; capture the rest of that night under a new run id. The retraction is
  APPENDED, so the false close stays on the file and a later reader can see the
  night had one rather than having to infer it. `is_closed` reads the LAST
  lifecycle record, not "a close record exists".
- **Doctrine ships ahead of the engine.** The run probes `brain --help` for
  `cos-corpus-append` (the same idiom Phase 1.6 rule 6 uses for
  `cos-run-begin`); on an engine without it the run captures nothing, runs no
  precondition check, behaves exactly as v5.43 and says so in its report.

#### The judging precondition (WIR-02) — `brain cos-corpus-check`

**A corpus whose rows carry no body text is REFUSED, and judging does not
start.** Run 65 did not execute Phase 1.6's body pass and then judged 58
threads `no-substance` whose bodies were never opened; run 64 read the same
instruction six times and skipped it anyway. An obligation a 100k-line run has
to REMEMBER is not in force, so this is a precondition rather than a reminder:
the message body IS the judge's input, and a missing body pass is therefore a
missing input.

```
$ brain cos-corpus-check --run-id 2026-08-02-run65
cos-corpus-check REFUSED (NoBodiesToJudge): refused to judge run
2026-08-02-run65: 0 of 58 corpus row(s) carry body text — all 58 row(s) are
bodyless. The judge's input IS the message body, so this is a MISSING INPUT,
not a quiet night: the body pass did not run. Nothing was judged. Fix the body
pass, never this check.                                             [exit 3]
```

It names WHAT is missing and HOW MANY rows, because a bare failure sends the
operator to the wrong subsystem — this repo has already paid for a limit
shipped without an outcome word. `cos_corpus.judgeable()` is the one
implementation; the offline replay harness (`eval/cos_replay.py`) calls it
before its first model call and refuses the same way.

**The partial case, decided: SOME bodyless rows are normal and never refuse the
run.** Phase 1.6 rule 1½ forbids opening an UNREAD thread and caps opens at 20
a night, so an honest corpus always carries bodyless rows — refusing on any of
them would refuse every real night. The bodied rows are judged, and the skipped
count comes back BESIDE them in the same return value, so the run states its
denominator instead of implying one. That is what stops a short candidate rate
from reading as thin mail. Only ZERO bodied rows refuses, which is exactly run
65's shape.

Host-only, like everything else in this module. It is a gate the run passes
through, in the same idiom as `tools/cos_contract.py`'s outcome contract — the
run supplies the artifacts, the engine supplies the verdict.

#### Where it lives, and who can read it

`config.host_private_base() / "cos-corpus" / <vault_slug8> /`, resolved through
`config.proven_off_mount` — **the same two functions** the approved queue
(INT-01), the attachment anchors (INT-04) and the writer lock (INT-05) go
through. No second resolver: a second copy of a verification rule is how the
first one ends up subtly weaker.

- **Off the Cowork mount, or not written at all.** A corpus directory that
  resolves inside the vault, inside `.brain/`, inside the declared workspace
  root, or through a symlink that lands back on any of them, raises
  `CorpusUnsafe` and **does not fall back**. The writer lock may degrade to the
  app-data base because a refusing lock takes the whole write path down with
  it; unfiltered mail bodies on a VM-readable path is the one outcome this
  module may not produce.
- **Host-broker only, refused on `role=vm` at the module boundary.** The mount
  proof CANNOT catch a VM here: `config.host_private_base()` on a VM resolves
  to the VM's own app-data directory, which is genuinely off the VM's vault
  mount, so `proven_off_mount` passes and the module would write unfiltered
  mail bodies inside the ephemeral, EDR-blind sandbox. `corpus_root` (and
  therefore every path, read and write through it) raises `CorpusHostOnly`
  under `BRAIN_ROLE=vm` — the same posture as `write`, `ingest`, `supersede`
  and `graphify`, made in the module rather than only in the CLI so a future
  call site cannot forget it.
- **Owner-only.** `0700` directory, `0600` rows, `0400` once closed. The
  directory is created with `mkdir(mode=0o700)` rather than
  created-then-chmod'ed, so there is no window at the umask default. The chmods
  themselves go through `config.secure_file_permissions`, which is best-effort
  by design — the real protection is the location (off the mount, host-private,
  outside any checkout), and full-disk encryption underneath it. POSIX mode
  bits alone were never the control here.
- **Never indexed, and no indexing rule was weakened to get there.** The
  corpus is not under `<vault>/` at all, so `notes.scan_vault` — which feeds
  every index and therefore `search`/`get`/`recent` — never reaches it. The
  exclusion is STRUCTURAL, not a filter: `MACHINE_OUTPUT_DIRS` is unchanged.
- **Gitignored** (`cos-corpus/`), belt-and-braces for an operator who points
  `$BRAIN_INDEX_DIR` somewhere inside a checkout.
- **Classified MNPI, and not negotiable by overlay.** An email-derived SOURCE
  defaults to MNPI and an explicit `overlay/keywords/` tier mapping may lower
  THAT note (AGENTS.md §2). A corpus FILE is a different object: it holds every
  thread the run read, unfiltered and pre-judgment, so its tier is the floor of
  its most sensitive row and can only be the maximum. Deriving a file tier from
  the least-sensitive row in it is exactly backwards.

**The text is not scrubbed, and that is deliberate.** `provenance.scrub` runs
on every surface that serializes a claim OUTWARD — the proposal, the ingest
manifest, the batch question, the report, the ledger. The corpus is the
opposite direction: it must be byte-faithful to what the judge saw, or a replay
over it is not a replay. Each row instead records `secret_findings` (the NAMES
of the patterns present, never the values), so a corpus known to hold
credentials is visible at rest, and the outbound candidate path scrubs exactly
as it does today.

#### When `CorpusUnsafe` fires — trigger, remedy, and what the run does

This is the guard that is DESIGNED to fire, so it gets a playbook rather than a
mention.

- **Trigger.** `$BRAIN_INDEX_DIR` points inside the vault, inside `.brain/`,
  inside the declared workspace root, or at a symlink landing on any of them —
  so the corpus directory would be readable from the Cowork VM. Also fires when
  the owner-only permissions cannot be established (above). The most common
  cause by far is the first: an operator pointing `$BRAIN_INDEX_DIR` at a path
  inside the mounted workspace, which is exactly what the `cos-corpus/` line in
  `.gitignore` anticipates.
- **Remedy.** Point `$BRAIN_INDEX_DIR` at a host-only path outside every
  workspace (or unset it and take the per-user app-data default), and set it
  IDENTICALLY in every context that writes this vault — the launchd job, the
  interactive shell, the COS nightly (see INT-05). `brain status` prints the
  resolved directory under `cos.capture_corpus.dir`, and the error message
  names the offending path and the root it landed inside. Nothing needs to be
  migrated: a corpus is per-run, so a corrected path simply starts writing the
  next run's file where it belongs.
- **What the run does.** It CONTINUES, and the capture stage records an
  explicit `capture: refused` marker carrying the reason. It does not abort the
  night's mail work — the corpus is evidence, not the job — and it must not
  quietly produce nothing, because a missing corpus and a night that read
  nothing look identical after the fact. A run whose capture was refused is a
  run whose ledger cannot be replayed, and that has to be stated, not inferred.
  (The marker is written by the session that wires capture into the nightly,
  `wir-01`; this module raises the
  exception and refuses to fall back to another location.)

#### Retention

`$BRAIN_COS_CORPUS_DAYS`, **default 30 days** (matching `cos.DEFAULT_GC_DAYS`).

**The nightly calls `cos_corpus.prune`.** It runs in `BrainCore.maintain`'s
daily retention block, beside the duplicate-retention and query-log prunes and
under the same once-per-day marker and writer lock — so the 30 days is enforced
by the schedule, not by an operator remembering. The fold stamps
`_cos_corpus_prune.last_run` into `maintain-state.json`, and `brain status`
reports what is on disk plus whether retention has actually run HERE
(`cos.capture_corpus`: run count, total bytes, oldest run and its age in days,
how many runs never closed, `pruned_by_a_scheduled_fold` and
`last_scheduled_prune`). That flag is read from this host's state file, never
inferred from the code being present: an engine that ships the fold still
deletes nothing on a host whose nightly has never fired.

**The cutoff is the real UTC clock — `brain maintain --date` never moves it.**
`--date` exists to exercise WHETHER a date-gated fold runs; feeding it into a
destructive window meant `--date <future> --allow-future-date` deleted real,
unexpired mail bodies on the spot. The fold therefore passes no `now=` to
`prune`, and `--date` still decides only whether the fold runs at all.

**A failed delete is reported, never marked done.** `prune` returns an
`errors` list — an unreadable corpus directory, a refused unlink — kept
separate from `unrecognized` (a filename this fold does not understand, which
is not damage). The nightly stamps `_cos_corpus_prune.last_run` ONLY when that
list is empty and raises a blocked item otherwise, so a permission error can
never leave expired MNPI bodies on disk while `brain status` reports
`pruned_by_a_scheduled_fold: true`. An absent corpus directory is not an error
— there is nothing at rest to expire.

**A pruned corpus is a THIRD reason `brain cos-verify-run` finds none.**
`check_corpus_join` says so in as many words: predates capture, wrote no
corpus, or aged out. Re-verifying a run older than the window still scores
`degraded` (safe), and the reason it prints is now true.

**Letting a REAL corpus expire is the decision, not an oversight** (owner,
2026-08-02). Capture costs about 21 minutes measured, so a real night is cheap
to re-make and hoarding one is not justified; the permanent fixed baseline is
the committed synthetic fixture (`eval/fixtures/cos-corpus-synthetic.jsonl`),
which holds no real mail. Real mail's job is realism, and a fresh capture beats
a stale one at that. If a frozen real baseline is ever wanted for a specific
comparison, it gets copied out deliberately at that moment.

**A window below 1 day is refused, not clamped.** `days=-1` puts the cutoff in
the FUTURE and deletes every current run: a retention knob that, held the wrong
way, is a delete-everything button. A `$BRAIN_COS_CORPUS_DAYS` outside 1–36500
raises, exactly as an explicit `days=` argument does — never clamped, because
`=0` reads like "off" and would silently become "keep one day".

**Only a CLOSED corpus is deleted.** An unlink while a writer holds the
descriptor keeps that writer appending to a detached inode, and the bytes
disappear when it closes — a corpus that silently lost rows. An expired corpus
that never closed is therefore reported under `held` and kept, never deleted on
the assumption that nobody is writing it; the nightly fold surfaces a held
corpus as action-required, since an unclosed file that never ages out is
exactly the "forever" retention exists to end. The unlink itself is a plain
unlink by pathname — racing it needs local code execution, which could read the
plaintext file anyway.

**Whole run files, never rows inside one.** A partially pruned corpus would
silently change a replay's denominator — the same shape as a run reporting
reads it never performed — so the unit of deletion is the file and there is no
per-row path at all. A corpus is wholly present or wholly gone. Age comes from
the run id in the FILENAME (host-assigned at launch, cannot drift), not from
mtime, which any tool that touches the file rewrites. A filename that is not a
run id is left alone and REPORTED, never deleted on a guess.

**Disk does not constrain the window.** Measured with the shipped writer over
synthetic mail-shaped text, 120 threads a night:

| scenario | file size | 30 nights |
|---|---|---|
| tonight's doctrine (20 opened at the 4000-char budget, 100 previews) | **158 KiB** | 4.6 MiB |
| every thread opened at the 4000-char body budget | **533 KiB** | 15.6 MiB |
| every thread at the 6000-char raw-page fallback | **769 KiB** | 22.5 MiB |
| a 200-thread night, all opened at budget | 888 KiB | 26.0 MiB |

So the window is purely a decision about **how long unfiltered mail bodies sit
at rest**, and the pressure on it is downward. 30 days buys ~30 nights of
regression baseline for ~15 MiB.

#### What this is NOT

Not a signed record. The corpus is host-written on the host, so the close
record's declared row count catches a truncated file — not a dishonest host. If a corpus ever needs to survive host compromise, that is an
Ed25519 anchor like the approved queue's, and it is not this.

### Mount-resident data that becomes a filesystem path

Three review rounds produced one finding each from a single defect CLASS — a
value the VM can write used as, or to build, a path the host then opens, moves,
renames or unlinks — at a different site every time. Guarding reported sites
failed twice, so the guards are now derived from an ENUMERATION: the census
block in `src/brain/cos.py` ("MOUNT-RESIDENT DATA THAT BECOMES A FILESYSTEM
PATH") lists every reader of mount-resident JSON with its path-bearing fields
and its guard. An `id` is not the only field that reaches the filesystem: an
attachment sidecar's `path` is a MOVE SOURCE, its `filename` is a move
DESTINATION, and a lifecycle record's `dest`/`src` are unlink targets. One
primitive per field class: `_safe_meta_id` (a bare slug, length-capped in
encoded BYTES) and `_unique_dest` (a bare filename). The third —
`_safe_meta_path`, which resolved a mount-written path and then checked the
result — is **gone** (INT-05): it was resolve-then-*use*, and a rename plus a
substituted symlink between the two won that race. Rather than narrow the
window, the surface was removed. A mount-written path field is now reduced to
its last component (`_safe_basename`) and joined onto a root the HOST derives
(`_leaf_in`), and the attachment payload is not read from the sidecar at all —
it is derived from the guarded id plus the real directory entry
(`_quarantine_payload`). A record naming `/etc/hosts` therefore points at
`<vault>/inbox/hosts`, which does not exist, at every instant rather than
after a check. Every write in `cos.py` goes through
`_write_atomic` (unpredictable temp name, `O_CREAT|O_EXCL|O_NOFOLLOW`,
write-until-complete, cleanup on any failure) — a predictable `.tmp` on the
mount was found twice, so the rule is now "no raw writes in this module at
all", and a test enforces it. The census also marks which sites belong to the
attachment lane, whose own accept-to-signature window is closed by the signed
anchor described in §2c.

**What the structural test actually binds, and what it does not (INT-05).**
`tests/test_cos_pathguard.py` fails if a `cos.py` function parses JSON off disk
— `json.loads`, `json.load(fh)`, a `JSONDecoder`, `_read_jsonl` or any other
`_read_*` helper, at module level too — without being classified in its table.
A `GUARDED` classification is now checked by TAINT rather than by grepping the
function for a guard's name: the parsed mapping is tainted at its binding,
cleared by a guard call, and a path expression over a still-tainted field is a
failure. Both detectors are themselves probed with known-positive fixtures, so
a detector that stops detecting fails instead of reporting clean.

**Round 2 found the taint detector reporting clean on flows its own docstring
named**, which is the same defect class the instrument exists to remove, so all
of them are covered and each is now a case in the known-positive probe: a
`_read_*` HELPER's return value is a taint source (not only an inline
`json.loads`), taint PROPAGATES through intermediate bindings, the sinks
include `os.rename`/`os.unlink`/`open()`/`shutil.move` and not just `Path()`
and `/`, and the `GUARDED:<consumer>` functions named in the census are
analysed too — a reader that delegates its guarding downstream is making a
claim about that consumer. Guard recognition is expression-shaped rather than
name-shaped: a guard call's subtree is skipped wholesale, so
`d / _safe_basename(m["x"])` and `[_safe_basename(n) for n in names]` are clean
without special-casing either idiom, and `Path(x).name` is string surgery, not
a filesystem sink. The improved detector immediately found a real one:
`ingest_sweep` filtered manifest filenames with an inline
`n == os.path.basename(n)` test instead of `_safe_basename`, and on POSIX
`os.path.basename("..\\..\\win")` is the whole string — a Windows-style
traversal passed, on an engine that ships to Windows. It routes through the one
definition now.

**Each of the five recorded gaps is closed, and each fix is proven able to
fail.** The negative-control run (`_evidence/cos2/s11/negative-controls.txt`)
breaks every guard's property on purpose — one mutation at a time, in the real
source — and records that the named test goes red. A test nobody has watched
fail is a test nobody has watched work:

- *Reader detection was idiom-bound* — closed. Detection covers every parse
  idiom in use plus module-level readers, and reader-hood propagates one hop
  through a `_read_*` helper (a naming convention the suite enforces, so a
  helper cannot opt out of detection by being called something else). The bound
  is deliberate and measured: the full call-graph closure would classify 81
  functions instead of 40, and the extra 41 are orchestration whose
  classification would read "guarded by its callee".
- *The guard check was a source grep* — closed by the taint check above.
- *`_safe_meta_path` was resolve-then-use* — the primitive is deleted, not
  reinforced; see the paragraph above this one.
- *The single-writer lock was ON THE MOUNT* — moved. `config.writer_lock_path`
  resolves under `config.host_lock_dir()` (the same off-mount base the approved
  queue and the append locks use), keyed by vault identity. The
  unlink-and-replace attack — unlink the inode a holder has, drop a replacement
  at the same name, and a second holder locks the NEW inode and runs
  concurrently against the same sqlite index — needs a path the VM cannot reach
  at all. **Round 2 tightened three things about that move:**
  - the lock root is now RESOLVED and proven outside every VM-visible root, so
    a `$BRAIN_INDEX_DIR` pointed into the workspace (or a symlink that lands
    there) cannot put the lockfile back under VM control. It does not follow
    the bad value and it does not take every write verb down with it: it falls
    back to the per-user app-data base, which is host-controlled by
    construction. Fail-closed for the LOCK; the approved queue already refuses
    that same misconfiguration outright, which is the surface that must be loud.
    **Round 3: that fallback is no longer silent** — acquiring the writer lock
    (`lock.vault_writer_lock` → `config.warn_if_lock_dir_fallback`) writes one
    `host-lock-dir-fallback` defect per process, because the fallback means the
    INDEX the lock protects is still on the mount and that was diagnosable only
    through some other code path;
  - **the boundary the proof is made against comes from HOST configuration**
    (round 3, HIGH — both reviewers, independently). `vm_visible_roots` used to
    infer the workspace root from `.brain/{bin,model,vendor}` — files inside
    the tree `cos.py`'s own census header calls attacker-writable. Deleting
    them shrank the visible set to `[vault, runtime]`, after which a
    `$BRAIN_INDEX_DIR` naming a SIBLING of `vault/` inside the same mount
    passed `proven_off_mount` — the exact case that arm exists to catch — and
    the writer lock, the anchor store, the approved queue and the ingest
    scratch dir all went back on the mount. It was also time-varying: a restage
    that ADDS the markers could relocate the lock while a long rebuild held the
    old path. The roots now come from `$BRAIN_WORKSPACE_ROOT` or the workspace
    registry in the operator's home (`~/.brainiac/workspaces.json`, written by
    the same install/cowork-setup flow that stages the markers, and outside
    every mount). The markers remain, as a fallback that may only ever WIDEN
    the set. **Recorded, not closed:** a staged workspace that was never
    registered AND has had its markers deleted has no host-side boundary left
    to read, and falls back to vault-only. Closing that means making
    registration a hard precondition of the staged install, which is an
    installer change with its own test pass;
  - `host_lock_dir()` no longer mkdirs and chmods as a side effect of NAME
    RESOLUTION (every index-mutating verb calls it, including the read-side
    `update-probe` liveness check). Creation happens at acquisition, in
    `brain.lock.writer_lock`;
  - **the upgrade window is closed by RELEASE ORDERING, not by a second lock.**
    This host runs two engines — launchd pins `BRAIN_BIN` to a staged build
    while a hand-run `brain` resolves from PATH — so between an upgrade and the
    restage the old engine locks `<vault>/.brain/writer.lock` and the new one
    locks the host path, and `flock` on two different files excludes nothing.
    A compat lock (the new engine taking the old mount path too) was written
    and then REMOVED: a lock that lives on the mount cannot provide exclusion
    in the first place — the VM unlinks the locked inode, drops a replacement
    at the same name, and the next holder flocks a different file — and it put
    the acquisition path's `os.ftruncate` behind an attacker-plantable name.
    So the two lanes only exclude each other once BOTH resolve the off-mount
    path, which makes **restaging the launchd-pinned engine BEFORE the new
    engine is used from PATH a required, ordered release step** (release
    runbook, INT-05 appendix). `lock._open_lock_fd` opens with `O_NOFOLLOW`
    where the platform has it — depth, now that the location is the defence.

  `$BRAIN_INDEX_DIR` remains host-wide configuration that must be set
  identically in every context writing a given vault. That is not a second
  identity for the lock: the lock protects the sqlite index and
  `$BRAIN_INDEX_DIR` IS where that index lives, so lock and index can never
  disagree about which index is being protected — two contexts with different
  values are writing different indexes.
- *The attachment lane had no signed anchor* — closed by INT-04; see the
  attachment-lane paragraph in §2c.
- *The no-raw-write gate exempted `os.open`* — it reads the FLAGS now
  (resolving a local `flags = …` variable), treats an unresolvable flag
  expression as a write until proven otherwise, and catches a non-literal
  `open()` mode and `r+`. `_write_atomic` and `_open_append_nofollow` are the
  two sanctioned raw writers.
- *The shared-zone permission test bypassed its production wiring* — it drives
  `write_run_manifest` now, and separately asserts the fchmod-before-replace
  ORDERING, so neither deleting the production `mode=` argument nor moving the
  chmod after the rename leaves it green.

**Still not bound, stated so nobody over-reads the above:** reader-hood does
not propagate past the one `_read_*` helper hop (measured, above), and
`maintain_lock_path` (`<vault>/.brain/maintain.lock`) is still on the mount —
a single-*runner* advisory lock whose loss degrades to a duplicate maintenance
run rather than to two writers on one index, so it is recorded here rather than
moved in the same pass.

Quarantine destinations are derived from the real directory entry. The vault is
bound into every signed body by ONE host-protected identity — the resolved-path
hash, which also names the queue's own directory — never OR'd with the
mount-resident `.brain/vault-id`, which a VM can overwrite with another vault's
value. The cost is deliberate: **moving a vault invalidates anything still
queued**, so drain (`brain sync`) before a move. `batches.jsonl` and the consume
journal are written and removed atomically and durably, since recovery re-derives
its decision from that signed row.

The drain reads the payload **once** (`O_NOFOLLOW`, regular files only), hashes
the bytes it actually read, re-verifies the anchor **immediately before
`write_note`** — a read-time check alone is TOCTOU — and signs **that buffer**;
it never re-opens the path. A payload whose frontmatter id differs from the
anchored id is refused too. Anything that fails verification is quarantined out
of the queue as `<ts>-<id>.md.refused` with an `approved-queue-refusal` defect
row and an `approved_refused` count in `brain status` — loudly, once, never a
silent retry and never a signature. Released auto-capture holds (§6) take the
same route.

**The ATTACHMENT lane (INT-04) holds the same property by a different
arrangement.** An accepted attachment is a binary and the thing that signs it
is the ingest drain, which reads `vault/inbox/` — on the mount — so the payload
cannot be held off-mount until it is signed. Only the ANCHOR moves. At accept
(`cos._accept_attachment`) the quarantined bytes are read **once**
(`O_NOFOLLOW`, regular files only), hashed, and checked against the sha the
owner's **Ed25519-signed batch digest** covered — never against the sidecar's
own `sha256`, which lives on the mount beside the payload and is rewritten by
the same hand. An anchor naming `inbox/<file>` and that hash is signed into the
host-private store (`cos.stage_attachment_anchor`, off-mount and off every
VM-visible root), and the **verified buffer is then written** to the
destination; the old code moved a file it had not read, so the CAS and the move
could see different objects. The anchor is filed under **two keys** and both
matter: by DESTINATION, which catches substitution in place, and by CONTENT
sha, which keeps the acceptance attached to the bytes when the NAME changes —
without it a rename silently strips the owner's claim, and email-derived
material that resolves MNPI *because* it carries provenance would ingest as a
plain unlabelled drop at `Internal`, a classification downgrade performed with
nothing but `mv`. (It also survives the ingest pipeline's own collision and
retry renames.) At drain time
(`brain.ingest.pipeline.run_ingest`) the bytes it is about to sign are checked
against that anchor: a mismatch is quarantined as `approved_anchor_mismatch`
with an `attachment-anchor-mismatch` defect row and never signed, a key outage
leaves the file in place for the next drain (never a guess, never a
quarantine), and an unanchored file is an ordinary manual drop and is treated
exactly as before. The anchor also carries the owner's claimed provenance,
which used to travel in a plain JSON store under `<vault>/.brain/` — one write
substituted both, so that store is gone rather than merely double-checked.

**The tier no longer depends on a mount-resident field either.** A swept
attachment is email-derived by construction (the sweep only produces one from a
mail manifest), so the anchor carries a HOST-derived `lane: attachment` stamp
and the drain applies the email-derived floor (MNPI) from that. It used to
depend on the four provenance FIELDS being present — which arrive in a sidecar
on the mount, so deleting them before the accept ingested the same
owner-accepted material at `Internal`, i.e. inside the VM egress cap, with no
signature involved. A swept attachment with no provenance claim at all now
lands at MNPI and carries no `provenance.*` keys: the tier comes from the lane,
never from an invented claim.

**Round 2 closed five more holes on this lane.** The first was the one that
mattered most, and it was on the path that matters most:

- **The verified bytes were discarded before the note was built.** The anchor
  checked `original_bytes`, and the extractor was then handed the PATH — which
  every handler re-opens (`TextHandler`/HTML/eml with `read_bytes()`, the
  docx/xlsx/pptx/pdf/zip handlers by giving the path to a library). That path is
  `vault/inbox/_processing/<name>`, on the mount, so a swap in the window put
  the attacker's content in the SIGNED note while the archive kept the approved
  bytes. Handlers are now given the verified buffer, materialised in a
  host-private temp file outside the vault and outside the workspace
  (`pipeline._extract_verified`) — one helper at the single dispatch point, so
  the library-needs-a-real-path handlers are covered by the same change. No
  handler used the filename (only `.suffix`, which is preserved), so nothing
  about the produced note changes.
- **The auto lane signed a hash the writable sidecar supplied.** An
  auto-captured attachment releases an undo-window later with no owner batch to
  CAS against, so the release fell back to `meta["sha256"]` — the sidecar, on
  the mount beside the payload — and a missing value skipped the comparison
  entirely. A `cos_attachment_hold/v1` authorization is now signed when the
  attachment ENTERS the hold, off-mount beside the acceptance anchors, over
  (id, content sha, `not_before`, vault); release verifies it, takes
  `not_before` from the SIGNED body, and passes the SIGNED sha as the CAS
  target. No authorization, no release (`attachment-release-unauthorized`
  defect). `_accept_attachment` REQUIRES a protected hash on both lanes — a
  `None` expected hash never means "no check".
- **The authorization bound the bytes but not the PARSER that reads them**
  (round 3, CRITICAL). The destination name came from `meta["filename"]` on the
  mount, so keeping the authorized bytes and rewriting that field to another
  extension chose a different ingest handler for owner-accepted content (a
  polyglot is the sharp case); dropping `category` additionally made
  `_still_eligible_at_release` read an auto hold as operator-placed and skip
  the demotion re-check. The canonical destination NAME is now signed into both
  authorizations — into `cos_attachment_hold/v1` at hold time, and into the
  owner's signed batch row (`candidates[].name`, so it is the name he was shown
  in the question) — plus the CATEGORY on the auto lane, which is what the
  re-check runs against. `_accept_attachment` requires the protected name
  exactly as it requires the protected hash. An authorization from a
  pre-round-3 engine carries neither and is refused rather than trusted: the
  attachment stays in quarantine and rejoins the next batch. What still rides
  from the sidecar, deliberately: the claimed `tier`/`provenance`/rules
  evidence, which the drain can only ever use to RAISE the tier
  (`provenance.email_classification` — the MNPI floor is host-derived from the
  lane stamp), so the exposure there is mislabelling, not a downgrade.
- **The claimed inbox entry was re-resolved by NAME after the symlink screen**
  (round 3, HIGH). Symlinks are rejected over the inbox listing BEFORE
  `_claim`, and the drain then called `claimed.stat()` and
  `claimed.read_bytes()` — two more resolutions of that name, both following a
  symlink swapped into `_processing/` in between, so an ordinary inbox drop
  became a signed, archived copy of any host-readable file. There is now ONE
  no-follow open of the claimed entry (`cos.read_nofollow`, the same primitive
  the accept path uses — not a second implementation), the regular-file check
  and the size cap are enforced on THAT descriptor, and that single buffer is
  what gets hashed, extracted and archived. A swapped entry quarantines as
  `irregular_entry`; the dry-run preview takes the same read.
- **Anchor absence failed OPEN.** "No anchor" legitimately means "ordinary
  inbox drop" — but for a file this host RELEASED it means the anchor was LOST
  (the 30-day GC, a repointed `$BRAIN_INDEX_DIR`, a deleted index dir), and
  ingesting it as an ordinary drop is exactly the `Internal` downgrade the
  content key was added to prevent. The drain now refuses those
  (`approved_anchor_missing` + defect); the GC never prunes an anchor whose
  destination is still in `vault/inbox/`; and the anchor store joins the
  approved queue in the index-dir non-disposable contract, counted by
  `brain status` (`cos.attachment_anchors_awaiting_drain`) and by the
  `brain rebuild` warning.
- **In-flight state from the pre-INT-04 engine.** An attachment accepted by the
  old code and still in the inbox at upgrade time has an entry in the old
  `<vault>/.brain/ingest-provenance.json` claim store and no anchor. That store
  is read ONCE by the drain — as evidence the file was accepted, never as
  provenance — the file is refused until re-accepted, and the store is deleted.
  Reading it as provenance would have restored precisely the forgeable
  unsigned channel INT-04 removed.
- **A systemic failure was reported as a per-item content verdict.** A locked
  keychain or a misconfigured `$BRAIN_INDEX_DIR` on the owner-batch path landed
  in the owner-facing `invalid` bucket with no defect row, sending an operator
  to the attachments instead of the keychain — while the hold-release path for
  the same failure already wrote one. Those two exception classes now get their
  own `systemic_error` bucket plus an `attachment-release-refused` defect, the
  same vocabulary on both paths; only `ApprovedRefused` stays in `invalid`.

**The content key's residual risk, decided rather than left silent.** The
content-keyed anchor is one file per digest, so identical bytes accepted from
two different messages would overwrite each other's signed claim, and
content-only recovery would then attach whichever record survived — another
email's provenance and category. Two bounded guards close the practical
exposure without a schema change: an ambiguous digest keeps the FIRST record
and raises a marker, after which recovery BY CONTENT refuses (the
destination-keyed anchor still covers both files, so only the rename
convenience is lost); and content-only recovery requires the SUFFIX to match,
because renaming `x.txt` to `x.html` is not a rename — it is a different parser
over owner-accepted content. What stays open, deliberately: storing every owner
for a digest (rather than refusing an ambiguous one) and binding a media type
into the signed body are schema changes to the anchor, and the refusals above
already fail closed.

**Still open on this lane, recorded not absorbed.** Round 3 bound the
destination NAME into both signed authorizations and the CATEGORY into the auto
lane's (above), which closes the parser-selection and demotion-bypass halves of
what this paragraph used to describe. What remains open, and why it is not
closed here:

1. **The rest of the sidecar still rides unsigned.** The owner's batch
   signature covers `(id, sha256, name)`; the claimed `tier`, `provenance` and
   rules evidence are still read from the sidecar at accept time and stamped
   into the anchor. They can only ever RAISE the tier (the MNPI floor comes
   from the host-derived lane stamp; `proposed`/`category` are max-ed, never
   min-ed), so the exposure is mislabelling, not a downgrade. Closing it means
   binding a metadata digest into the signed batch row — a schema change to a
   structure the NOTE lane shares, with its own test pass.
2. **A DELETED release record is invisible.** Corrupting one now halts the
   drain (`attachment-release-records-unreadable`, nothing ingested, defect
   row); deleting one leaves nothing to notice, and the released file then
   ingests as an ordinary `Internal` drop. The records live on the mount, so
   deletion is available to the VM. The real answer is a host-private,
   append-only release ledger beside the anchor store, retiring each entry only
   once that exact sha is committed or quarantined — a new persistent store on
   the non-disposable side of the index dir, with its own GC, status count and
   recovery story. Not started here.
   `tests/test_cos_approved_queue.py::test_unreadable_release_records_halt_the_drain_instead_of_failing_open`
   pins both halves, the fix and the residual, so the gap is asserted rather
   than assumed. The same store's LEGACY half still unlinks itself once per
   drain rather than per-sha, so a crash mid-drain loses the refusal for the
   files not yet reached.
3. **An unregistered, marker-deleted staged workspace has no boundary left**
   (the `vm_visible_roots` note in the INT-05 bullet above).
3a. **A mixed-version window is not excluded by the code, only by the release
   order.** During the window between installing this engine and restaging the
   launchd-pinned one, the un-restaged old engine takes ONLY the pre-INT-05
   mount lock (`<vault>/.brain/writer.lock`) and the new one takes only the
   off-mount lock — `flock` on two different files excludes nothing, so both
   can write the same sqlite index. That is exactly why restaging first is now
   a required, ordered release step (release runbook, INT-05 appendix) instead
   of something the code papers over: the compat lock that tried to close it
   lived on the mount, where a lock cannot exclude anything at all (unlink the
   inode, drop a replacement, and the next holder locks a different file), and
   it exposed the acquisition path's `ftruncate` to a planted symlink. The
   residual is therefore an OPERATOR-ORDER obligation, verified by running both
   lanes, not by reading the plist.
3b. **Attachments already HELD at upgrade time are dropped, not carried.** They
   carry no `cos_attachment_hold/v1` authorization (or, after round 3, one with
   no signed destination name), so every hourly run logs
   `attachment-release-unauthorized` and none of them ever releases; nothing
   returns them to `pending`, and `expire_proposals` moves them aside at TTL —
   an owner-accepted capture lost after N defect rows. Re-signing an
   authorization for pre-existing holds on first run would mean the host
   asserting a decision it has no record of making, and returning them to
   `pending` is a state transition the release path does not currently own; the
   honest interim is the operator step now written into the release runbook's
   INT-05 appendix (grep the defect ledger, re-propose). The OPEN-BATCH half of
   the same upgrade IS self-healing: a pre-round-3 batch row has no bound name,
   the accept refuses it, and the attachment rejoins the next batch.
4. **`vault_slug8` is 32 bits.** The vault identity bound into every signature
   and into the queue's own directory name is 8 hex characters of SHA-256 —
   ample as a collision boundary between an operator's own vaults, and NOT a
   tenant-isolation boundary: a chosen-prefix search over 32 bits is cheap, and
   two colliding resolved vault paths share the authorization namespace under
   one account key. Widening it to 128 bits (or a host-private vault UUID)
   renames every queue/anchor/lock path, so it is a migration, not an edit.
5. **A failed signature verification cannot be distinguished from a broken
   backend.** `_verified_attachment_anchor` collapses every exception from
   `pubkey.verify` to `None`, so a transient crypto failure after key loading
   reads as "no host approval" — which quarantines an accepted attachment and
   emits a tampering-shaped defect instead of taking the systemic retry path
   the key-outage arm already has. The fix is to catch `InvalidSignature` and
   JSON/shape errors as "invalid" and convert everything else to
   `ApprovedKeyUnavailable`; it touches the one routine every anchor and
   authorization lookup shares, so it wants its own pass.
6. **`host_lock_dir`'s fallback moves the LOCK, not the INDEX.** With
   `$BRAIN_INDEX_DIR` on the mount the writer lock relocates to app-data while
   `config.index_path()` still names the mounted sqlite, so the write path
   continues against a VM-writable database under a host lock. That is
   deliberate: refusing outright is fail-closed for the lock and fail-BROKEN
   for every other verb, and the approved queue already refuses the same
   misconfiguration on its own. Round 3 added the missing half — the fallback
   now writes a `host-lock-dir-fallback` defect — but the residual exposure
   (mounted index, host lock) is real and is not fixed by the diagnosis.
7. **A test asserted a lock property vacuously.**
   `test_the_append_lock_is_off_the_mount` claimed "no lock of any kind under
   the mount" while nothing on its path took a write lock at all. It now takes
   both an append lock and the single-writer lock first, and then asserts the
   stray set is EMPTY — true again since the compat lock was dropped, and no
   longer true by accident.
8. **Instrument residue and drift, found in the audit's own tooling.**
   `os.path.basename` was still listed as a taint-clearing guard in
   `tests/test_cos_pathguard.py` — the very idiom whose unsafety this round
   found — and is now removed (production `cos.py` no longer calls it, so the
   entry was pure residue). The negative-control runner's docstring described a
   per-case `expect` override it never implemented, and
   `docs/cos-instrument-inventory.md` disagreed with the runner about the case
   count; both corrected. Still open there: the AST taint detector never clears
   a variable's `clean` state on REASSIGNMENT (`x = _safe_basename(...)`
   followed by `x = m["path"]` reads as guarded), and the `_read_*`-leak
   negative control is scored by the CENSUS test rather than the taint test it
   is meant to prove. Both are instrument-fidelity fixes to the detector, not
   to the production guards, and both belong in one pass over that file.
**Verification key handling:**
the key is resolved ONCE per drain; if it cannot be (locked keychain, wrong
scheduler user, missing `cryptography`, rotated key) the queue is skipped
whole with `no-signing-key (fail-closed)` and nothing is quarantined — a key
outage must never present as tampering. **Vault identity is ONE value** — the
resolved-path hash — and it binds both the signed body and the queue's own
directory; `.brain/vault-id` is deliberately NOT consulted for either, because
it is a file on the mount (an earlier revision accepted either, which let a
rewritten `vault-id` replay another vault's anchors, and left the queue's parent
steerable by the same file). The bounded cost is that **moving a vault
invalidates anything still queued: drain (`brain sync`) before a move.**
`brain --role vm draft-capture` keeps today's path unchanged: an
ordinary draft still lands in `capture-inbox/` and is still drained from there,
after the approved queue (order matters — otherwise a VM draft under an
approved id would be signed first and the owner's copy would lose to the
duplicate-id guard).

## 3 · Evidence signer

`brain cos-evidence sign --bundle-version V --model-version M
--dataset-window W [--file F]... [--name N]` writes
`host/evidence/<name>-<utc>/` with the payload files plus a
`manifest.json` (`cos-evidence-manifest/v1`) binding: bundle version, model
version, **snapshot generation** (read from the published snapshot manifest),
dataset window, **source-ledger hash** (sha256 over the `verdict-drop/`
shadow-ledger bytes, `"none"` if absent), and per-file sha256s. The manifest
is canonicalised (sorted-key JSON, sig fields excluded) and **Ed25519-signed
with the host audit key**.

`brain cos-evidence verify --dir D` verifies the signature **against the
host-resolved key** (never the manifest's embedded copy) and re-hashes every
payload file — a stale or edited JSON fails verification. Both verbs are
host-broker only.

## 4 · Correction transport

Store of record: `correction_events(round, msg_key, corrected_bucket,
corrected_tier, ts, actor)` in `host/corrections.sqlite`,
`PRIMARY KEY (round, msg_key)`. **Append-only** — the engine has no
update/delete path; a duplicate key raises, an unknown key (one not present
in the `verdict-drop/shadow-ledger*.jsonl` drop) raises.

**How the owner's one-line Cowork correction reaches it** (defined contract):

1. In Cowork the VM runs `brain cos-propose --kind correction --content
   '{"round": 3, "msg_key": "m-abc", "corrected_bucket": "urgent",
   "corrected_tier": "Confidential"}'` → an UNSIGNED drop in
   `drop/verdict-drop/`. **A VM write never mutates the store of record.**
2. The host broker claims the drop and enqueues one owner-inbox question
   (`coscorrect:<round>:<msg_key>`, options `apply`/`discard`, default
   `discard`).
3. The **human answer on the host** (`/brain-inbox` → `brain inbox --answer`)
   is the act the row is attributed to: on `apply` the answer-consumer
   inserts the row with `actor = owner-inbox:coscorrect:<round>:<msg_key>`.

Host-side one-liners skip the drop: `brain cos-correct --round 3 --msg-key
m-abc --bucket urgent --tier Confidential` (`actor = host-cli`) — already a
human act at a host terminal.

**Calibration report (s04):** `brain cos-report [--json]` (host-broker only)
reduces the `verdict-drop/shadow-ledger*.jsonl` verdicts against
`correction_events` — rounds completed, per-round corrected counts, per-bucket
precision (a tier-only correction leaves the bucket correct). This is the
evidence input the 10-round trust gate reads; it never mutates either store.

## 5 · Priority-map generator

`brain cos-priority-map [--max-tier T]` (host-broker only) queries
`type: person` / `type: company` notes and writes the VM-readable
`shared/priority-map.md`. The tier policy is the HOST egress default — the
**full vault**, deliberately NOT capped to Internal (owner ruling
2026-07-10); pass `--max-tier` to narrow. The map lists ids/titles/metadata
only, never note bodies. Owner overrides live in the **overlay `cos/`
category** (optional; validated by `brain init --validate-overlay` when
present): body list lines `- <note-id>: high|normal|low|exclude`.

## 6 · Auto-capture hold store

`brain cos-hold add --not-before <ISO> [--id I]` parks a qualifying
auto-capture item UNSIGNED under `host/hold/`. It enters the approved queue
(§2c, and thence the signed drain) **only after** `not_before` — resolving the
undo-window vs draining conflict. `brain cos-hold cancel --id I` is atomic
against a concurrent release: both claim the hold marker by `os.rename`
(atomic), exactly one wins. `release-due` runs standalone and inside the
broker fold.

## 6a · Auto-capture criteria (ING-04, s08)

`cos.auto_capture_eligible(vault, pattern, bundle_version)` decides whether a
PENDING proposal's `pattern` (opaque string, skill-supplied) is eligible to
skip the owner batch entirely and go straight into the hold store above.
Held to a strictly higher bar than auto-archive — this is the one
IRREVERSIBLE step in the broker (a signed note joins the hash-chained audit
brain):

- a documented **minimum volume** (`min_volume`, default 8 — 1/1 = 100% is
  disqualified by construction);
- **zero** claim-time classification/security defects for the pattern in the
  window (`claim_drops` records a `claim-rejected-security` outcome whenever
  its secret-scrub fires on a patterned candidate);
- a **Wilson-score lower bound** on the accept rate (`min_lower_bound`,
  default 0.85) — never the raw percentage.

Evidence (`cos.record_outcome` → `host/proposals/outcomes.jsonl`, host-only,
append-only) is scoped to the candidate's OWN `bundle_version` — a fresh
skill build starts every pattern back at zero (s07 version-binding rule).
Thresholds are owner-editable per-pattern in
`host/autocap-config.json` (`{"min_volume":.., "min_lower_bound":..,
"undo_hours":.., "patterns": {"<name>": {...overrides}}}`) — never hardcoded
in skill text. `cos.auto_capture_fold` (wired into `cos_broker_fold` right
before `enqueue_batch`) routes every eligible pending candidate into
`hold_add` with `not_before = now + undo_hours` (default 24h,
`BRAIN_COS_AUTOCAP_UNDO_HOURS`) instead of the next owner batch; the
existing hold-store undo window + `brain cos-hold cancel <id>` is the
revert. `brain status --json`'s `cos.holds_pending` (id + `not_before`,
never content) is the daily digest.

## 6b · Commitment spine (SP-01/SP-02, s08)

`src/brain/spine.py` — a durable, event-sourced ledger of everything owed,
independent module, host-only (`host/commitments.sqlite`). Two tables:
`events` (append-only: created/rescheduled/completed/cancelled/corrected/
reopened) and `commitments` (a pure materialized projection, fully rebuilt
from `events` by `spine._reduce` on every write — never a targeted `UPDATE`,
so "never mutate status/due in place" holds structurally). Identity
(`commitment_id_for(direction, counterparty, topic)`) is a hash that
deliberately EXCLUDES `due` — a reschedule never mints a duplicate, and
re-recording the same `(direction, counterparty, topic)` dedups for free.
Replay is sorted by `(ts, event_id)`, so an out-of-order or conflicting
event slots into its correct place in history rather than clobbering newer
state.

`cos._spine_ingest_commitment` (called from `cos.consume_answers` when an
accepted candidate carries `kind: commitment`) records a `created` event and
decides "keeper": counterparty priority `high` in
`load_priority_overrides` (reusing the existing priority-map vocabulary as
the P0/P1 equivalent) AND due ≥ `BRAIN_COS_KEEPER_HORIZON_DAYS` (default 7)
out. A keeper is ALSO signed as a brain note (staged into the approved queue,
§2c); a non-keeper is spine-only — its candidate content is archived to
`host/spine-evidence/` instead, and never signed.

`spine.radar(vault)` returns `late` (due has passed) and `at_risk` (due ≤ 48h,
`DEFAULT_AT_RISK_HOURS`) open commitments, each with `age_days`.
`spine.render_spine_summary` (run every broker fold via
`core.cos_spine_render`) writes the VM-readable projection
`shared/spine-summary.md` — same host-generates/VM-reads pattern as
`priority-map.md`. `brain cos-spine record|radar|render` is the host CLI
surface (`record` is for the two named spine sources that aren't wired to
the ingestion pipeline yet — calendar follow-ups, the drafts ledger — the
DB/reducer/radar are source-agnostic, so wiring those two in is additive,
not a redesign).

## 6c · Category learning + per-category graduation (LRN-01/LRN-02, DOC-01)

The pattern gate above is one key. The **category** is a SECOND, additive key
— the owner's own ingest taxonomy (`docs/cos-ingest-taxonomy.md`, overlay
home `overlay/cos/ingest.md`), scoped far more narrowly:

```
(category, lane [text|attachment], classification tier, extraction_rules_version)
```

- **Lane and tier are exact.** Attachment evidence never authorizes the
  email-text lane (or the reverse), and Internal evidence never authorizes
  auto-ingest of MNPI material.
- **The ruleset version, not the bundle string.** `extraction_rules_version`
  is bumped only when Phase 1.5/1.6 extraction rules actually change; the
  frequent stamp-only `bundle_version` bumps CARRY evidence forward (at ~8
  candidates a night, bundle-keyed evidence would never reach min-volume).
- **The tier is HOST-DERIVED, not a VM label.** Both lanes resolve their
  ceiling through `provenance.email_classification`: email-derived material
  defaults to **MNPI**, and an explicitly proposed tier or the category's
  `min_tier` floor can only RAISE it. The owner's overlay `keywords/` mapping
  is the one thing that LOWERS it — and it is matched only against
  **host-verified** text (`verified_texts`: subject/sender parsed by the host's
  own `.eml` handler out of the archived original, which alone earns
  `provenance.verified`). A VM-authored subject, sender, filename or body is a
  CLAIM and is never a lowering input: otherwise a producer could plant a
  mapped term in its own `provenance.subject` and bind Restricted material to
  a graduated `Internal` evidence key. So a proposal drop and a swept
  attachment both resolve **MNPI** today. Binding a VM-authored label
  to the content sha would make it tamper-*evident*, not authoritative — a
  producer could file Restricted material as `Internal` and have the graduated
  `Internal` key auto-capture it.
- **The other routing labels carry no lane authority.** `category` is
  host-VALIDATED (the claimed name must exist in the owner's taxonomy and its
  rule must cover the lane); the host cannot prove content MEMBERSHIP, so the
  name only chooses which evidence bucket the candidate is matched against —
  and that bucket is filled exclusively by recorded owner verdicts.
  `pattern`, `bundle_version` and `extraction_rules_version` are opaque
  producer identity used to SCOPE evidence and can only NARROW: an unseen
  value has zero accrued verdicts and forces the owner batch, an absent one is
  `unclassified` and vetoes the auto lane outright.
- **Everything is bound at claim time.** `claim_drops` writes the resolved
  values into `pending/<id>.json` alongside the content sha, and eligibility
  never re-reads the VM's own frontmatter.

**The both-keys policy, in one sentence:** a candidate is auto-committed only
when the pattern gate is eligible AND its category has graduated on the same
(lane, tier, ruleset) key; `never` or `unclassified` on either key vetoes the
auto lane, anything merely propose-only or un-graduated routes to the owner
batch, and even an auto candidate commits through the undo-windowed hold —
never an instant signature. `cos.route_decision` is that policy in code.

**Evidence hygiene.** One verdict per **evidence unit** (source conversation +
normalized content fingerprint + category + lane + ruleset) — re-forwards and
re-extractions cannot inflate the Wilson sample. Outcomes are idempotent per
`(proposal id, outcome)`, so a TTL requeue into a second batch cannot
double-count. An **`accept all` over more than `bulk_accept_max_batch`
(default 3) candidates is excluded from numerator AND denominator** — measured
approval fatigue is not per-candidate agreement. Rejects always count.
**Expiry is not a verdict**: an unanswered candidate writes no outcome at all.

**Staying honest after graduation.** Evidence is recency-windowed
(`window_days` 90 / `window_verdicts` 50), so a rolling accept-rate drop
un-graduates the category on its own; a graduated category still routes
1-in-`exploration_k` (default 5) of its candidates back through the batch as
an exploration sample whose verdict accrues normally. Demotion (an append-only
marker in `host/proposals/demotions.jsonl` that resets the evidence window)
fires on: a claim-time security defect, an owner undo in ANY state, a category
removed from `ingest.md`, and a category flipped to `never`.

**Demotion applies to holds that already exist.** A held item is precisely the
population that has NOT committed yet, so `hold_release_due` re-runs the
both-keys policy against each due hold's stored host-bound evidence and the
CURRENT taxonomy/statistics before releasing it. An item whose category has
since been demoted — by a security defect, an undo, a `never` flip, removal
from `ingest.md`, or a rolling accept-rate drop — is **returned to the pending
queue** and joins the next owner batch (with a `hold-returned-to-owner` defect
logged), never released on stale eligibility. A hold placed by the operator's
own `brain cos-hold add` carries no graduation and is unaffected.

**The undo state machine** (`cos.hold_undo`, `brain cos-hold cancel --id I`):
`held → releasing → capture-pending → signed`, plus `inbox-pending` for a
released attachment. The undo timestamp is made durable BEFORE any state race,
so a cancel recorded before the hold deadline always wins the atomic-rename
race. Undo of an unsigned capture draft deletes the draft; undo after signing
is an **audited retirement** through the signed write path.

That retirement stamps `retired: true` / `retired_date` / `retired_reason` —
deliberately **not** `is_latest_version: false` + `superseded_date`. An undo
retracts a claim, it does not replace it, so there is no successor; and per
AGENTS.md §2 those keys without a `superseded_by` are a shape
`tools/validate.py` rejects, which would have left the vault permanently
failing the documented pre-commit conventions gate (§8) far from the cause.
`brain supersede` remains the path when a real successor exists.

**Consume atomicity.** `consume_answers`, `expire_proposals`,
`expire_batches`, `enqueue_batch`, `auto_capture_fold`, `hold_release_due`,
`hold_undo` and `gc_compact` all run under the same single-writer lock as
`sync`/`rebuild`. The per-batch decision is journalled **and fsynced** to
`host/proposals/consume-pending.json`, then compare-and-set into an
`applying` generation, and only THEN are any files moved — a lost CAS means
nothing happened, and a crash after it leaves an `applying` batch the next
call RESUMES (every step is idempotent) rather than discards. The
per-candidate sha is the proposal-level CAS, and it is carried forward into
the signed approval anchor (§2c) so the signing gate re-checks it against bytes
the VM cannot reach. Answer timeliness is judged on
the **durable answer timestamp** (`answered_at`), never on when the consumer
happens to run.

**Pattern auto-capture is SUSPENDED until the producer stamps the category.**
The both-keys policy needs `category` + `extraction_rules_version` on every
candidate. Chief-of-staff **v5.37** ships both (measured on the reference deployment: run 57,
2026-07-30 — `unstamped_batched` fell to 0 on the first live night, with no
code change, exactly as designed). A candidate from an OLDER producer still
lands in the owner batch and is counted by `unstamped_batched`.

Satisfying the both-keys policy is necessary, not sufficient. A stamped
candidate then faces the graduation test, and **at go-live no category has
graduated** — graduation comes only from accumulated owner verdicts under that
candidate's own `extraction_rules_version`, and a ruleset bump RESETS that
evidence. So every category starts in the owner batch and stays there until it
earns the lane. That is deliberate: holding an un-graduated category is what
the category key exists to do. What is NOT acceptable is it happening
silently, so: `brain status --json` carries `cos.pattern_autocapture` (which
of the two conditions is holding) and `cos.route_stats.unstamped_batched`, the
same counter rides `batch_liveness` into the morning brief ("N COS
candidate(s) sent to the owner batch for a missing category/ruleset stamp"),
and `cos_broker_fold`'s `auto_captured` block reports it per run.

**Attachments take the same loop (DOC-01).** `cos-ingest-sweep` no longer
moves a matched download into `vault/inbox/`. The flow is
`manifest → host-private quarantine (host/attachments/quarantine/) → owner
batch verdict (or the auto lane once the attachment-lane category has
graduated) → only an ACCEPTED file moves to vault/inbox/` for the signed
ingest drain. A `never` category attachment is refused before it leaves the
staging dir, with a logged defect.

**Where the quarantine actually is, and for how long.** It resolves *inside*
`<vault>/.brain/cos/host/attachments/` (`$BRAIN_COS_OPS_DIR` overrides the
root) — host-only, 0700, gitignored wholesale and never indexed, but **not**
outside the vault tree, so it is visible on the Cowork VirtioFS mount. That is
a real change of posture worth stating plainly: unverdicted third-party
binaries now dwell there for up to the proposal TTL (14 days), where they
previously transited `vault/inbox/` into signed `raw/originals/` within the
hour.

**Rejecting an attachment is recoverable, and the question says so.** A
rejected file leaves **zero residue in the vault** — no `raw/` note, no
archived original, no index row, no audit entry, because it never entered the
vault — but it is *not destroyed*. The sweep already MOVED it out of the
owner's download location, so an immediate unlink on a reject (whose stated
default is `reject all`) could take out his only copy; AGENTS.md §9 names
deleting a possibly-sole-copy as a genuinely owner-only decision. So the
payload and its sidecar go to the GC-windowed
`host/attachments/expired/`, recoverable for `$BRAIN_COS_GC_DAYS` (30). The
owner-inbox question names each attachment as a **FILE** with its filename,
size and sender — never as a "candidate note" behind an opaque `att-…` id —
and its context line states the recovery window.

**Undo still reaches an attachment after release (B3).** Once released, the
quarantine sidecar is consumed and the ingest drain renames the file to a
date+filename `raw/` id, so `att-…` matches nothing. A lifecycle record under
`host/attachments/lifecycle/` keeps id → inbox destination → content sha; the
drain's own `.brain/ingest-manifest.json` maps that sha to the final note id.
`brain cos-hold cancel --id att-…` therefore withdraws the unsigned inbox copy
before ingestion, and retires the mapped `raw/` note (audited) after it.

**What has actually traversed this lane (2026-07-31).** Until this date, zero
files had: the lane was wired but `attachment_lane: blocked-no-downloads-mount`
on every run. The **host half is now proven end to end on the deployed engine
against the live vault**, in both directions — sweep → host-private quarantine
→ owner batch beside a text candidate → accept → `vault/inbox/` → signed ingest
→ `raw/` source carrying the flat dotted `provenance.sender`/`.sent`/
`.conversation_id` keys; and reject → zero vault residue (audit chain unmoved)
with the payload byte-identical in `expired/`. Evidence:
`_evidence/gaps/2-attachment-lane-host-half.md`.

That was done with a **canary document, not a real attachment**. One link
upstream remains unproven and cannot be manufactured: *an attachment-bearing
message on a live night, with the browser leg downloading into
`$BRAIN_COS_DOWNLOADS_DIR` (what v5.38's `Browser.setDownloadBehavior` change
provides) and writing the manifest line for it.* Everything downstream of that
manifest line is now a path with an execution trace.

**Taxonomy failure semantics** (mirrored from
`docs/cos-ingest-taxonomy.md` §5): ABSENT ⇒ the feature is off (nothing
graduates); UNPARSEABLE ⇒ fail closed to all-`propose` plus a defect in
`host/proposals/defects.jsonl`; one bad rule ⇒ that rule is `propose` with a
warning.

**Liveness, not failure.** An unanswered batch breaks nothing while quietly
re-killing the funnel behind the one-open-batch backpressure, so
`cos.batch_liveness` (oldest open-batch age, candidates held behind it,
`BRAIN_COS_BATCH_STALE_HOURS` default 48) surfaces in `brain status` and the
morning brief.

## 6d · Deduced version links (VER-01/VER-02)

When a newly committed document looks like a newer version of one already in
the vault, the broker says so — as a **proposal in the same nightly batch**,
never an automatic supersede. Signals live in `src/brain/versionlink.py`; the
broker half (candidate, batch, apply, memory) lives in `src/brain/cos.py`.

**Three tiers, and only the third is new.** sha256-identical duplicates stay
automatic (`maintenance.auto_dedup_tier1`, DDP-01); explicit `…-vN` id
families stay automatic (`maintenance.auto_version_chains`, VER-01). The
DEDUCED tier is propose-only, and it skips any pair the other two own.

**A pair is proposed only when ALL of these hold:**

1. **Direction** — the successor's valid date (`effective_date` →
   `document_date` → host-verified `provenance.sent` → `created`) is strictly
   newer.
2. **A HOST-VERIFIED email link** — a shared `provenance.conversation_id` or
   `provenance.sender`, with `provenance.verified: true` on BOTH sides. A
   VM-claimed sender/subject/filename is a CLAIM (PRV-01/PRV-02, S04): it may
   corroborate but can never produce a proposal, so a spoofed header never
   reaches the owner batch.
3. **A name identity signal** — matching normalized name stems (NFC first —
   macOS `readdir` returns NFD), or an ordinal version marker that advances
   (`v2`, `rev 3`, `draft`→`final`). The email SUBJECT is deliberately
   excluded from identity and used only for markers: a subject belongs to a
   THREAD, so treating it as identity makes "same thread" and "same document"
   the same test.
4. **Evidence** — near-duplicate content, or that same advancing marker.

**Content similarity alone never proposes, and never even nominates.** The
measured precision trap is two distinct agreements cut from one boilerplate
template, from the same counterparty: they sit in the same (often >0.95)
cosine band as two genuine versions. Similarity is computed on bodies with
quoted reply chains, forward headers and signature blocks REMOVED
(`versionlink.similarity_text` — two unrelated one-line replies under one
quoted chain otherwise score 0.996), from the stripped body alone with no
title/zone prefix. Threshold: `$BRAIN_VERSIONLINK_MIN_SIMILARITY`, default
`0.95` (matching `brain integrity`'s near-dup floor), clamped to 0.90-0.99.
Successor window: `$BRAIN_VERSIONLINK_WINDOW_DAYS`, default 14.

**Ambiguity declines, it does not guess.** Version markers that disagree with
the dates, or that do not advance, or that are on different scales (a numeric
marker vs `final`) end as `declined` in
`host/proposals/version-links/ledger.jsonl` — logged once, never proposed.
This mirrors `auto_version_chains`' `skipped_ambiguous`.

**Capacity.** One open batch carries at most `BATCH_CAP_TOTAL` (12)
candidates: `BATCH_SUBCAP_INGESTION` (8) reserved for ingestion,
`BATCH_SUBCAP_SUPERSEDE` (4) for version links, with any unused supersede
slots going to ingestion. Overflow is reported as `deferred` and joins the
next batch — supersede proposals can never starve ingestion.

**Applying an accept.** `core.supersede(old, new, expect=…)` — the content
hashes the proposal was decided against are re-verified **inside supersede's
own writer lock, before the first signed write** (a caller's own pre-check is
TOCTOU: the nightly folds mutate notes while a proposal waits). Both sides are
also re-verified at consume time so a pair that moved declines legibly
(`supersedes_declined`) instead of raising. A rejected, applied, declined,
stale or expired pair is recorded in the ledger and **never asked again** — in
either direction.

**Learning.** Verdicts accrue against the ordinary S05 evidence key with
`category = version-link`, `rules_version = versionlink.RULES_VERSION`, and
the pair's most restrictive classification tier, so the class can graduate
later under the same Wilson gate with no special-case code — and only after
the owner adds `version-link` to the ingest taxonomy. One host-verified
conversation is ONE counted evidence unit (`evidence_lineage`), so a thread
carrying five successive versions cannot walk the class toward graduation on
its own.

### 6d.1 · What has actually traversed this lane, and the two things that gate it

Until 2026-07-31 **nothing had**: 23 tests passed, no `version-links/` ledger
had ever existed outside a fixture, and the first real traversal found the lane
**broken in the shipping context**.

*The bug.* Every test fixture pins `BRAIN_EMBEDDER=hash`, whose vectors are
plain Python floats. The shipping embedder returns `list[list[numpy.float32]]`;
`vectors.cosine` propagates that type straight through its `-> float`
annotation, and the resulting `numpy.float32` in `signals` made `json.dumps`
raise `TypeError: Object of type float32 is not JSON serializable` at the first
proposal write. `cos_broker_fold` isolates stage failures, so the whole
version-link stage aborted **every run**, and — because the ambiguous DECLINES
are written to the ledger *before* the propose loop, and a decided pair is never
re-asked — each attempt permanently retired the ambiguous pairs it saw while
never once writing a proposal. Fixed in `versionlink._similarity` (the one place
a similarity enters the module); regression test:
`tests/test_cos_versionlink.py::test_real_embedder_float32_scores_still_serialize`,
which reproduces the shipping embedder's exact numeric contract and fails
against the unfixed code.

*The drill.* `_evidence/gaps/5-versionlink.sh <workdir>` runs the whole lane on
the deployed engine against a scratch vault: real `.eml` messages ingested
through the HOST pipeline (so `provenance.verified` is EARNED — hand-writing it
is the forgery the engine refuses), the fold, the `kind=supersede` proposal in
the owner batch, an accept applying through `core.supersede`, the `expect` gate
shown able to REFUSE, and all three companions (sha-identical duplicates
auto-retire, an ambiguous pair declines, a spoofed VM-claimed pair never
proposes). Pass `LIVE_VAULT=<path>` for a read-only scale probe.

*Two conditions gate go-live, and neither is code.*

1. **The deployed engine must be restaged.** `dist/engines/brainiac-<v>` is a
   pinned snapshot; a fix in `src/brain/` is not live until it is copied there.
   The engine deployed on 2026-07-31 still carries the `float32` bug **and** a
   `PATTERN_AUTOCAPTURE_STATUS` string that was corrected in-tree at 8d63354 and
   still reads `suspended: … lands in S07` — an operator-facing message that
   outlived its own truth, printed by every `cos-broker` run.
2. **The live vault has zero host-verified material.** Measured 2026-07-31:
   2 197 notes in versionlink scope, **0** carrying `provenance.verified`. The
   three archived `.eml` originals predate PRV-02 and carry no provenance keys
   at all. Until real email originals are ingested through the host pipeline —
   the same dependency the attachment lane has — this lane has nothing to act
   on, however correct the code is.

### 6c.1 · The graduation canary (drill for the learned lane)

The learned funnel's first REAL graduation is ~22 clean verdicts away, so its
first exercise would otherwise happen unattended. `_evidence/gaps/4b-canary.sh
<workdir>` runs the whole lane on the deployed engine against a scratch vault
carrying a synthetic `graduation-canary` category: seed host-side outcome
records (21 → still held at Wilson 0.8454; 22 → 0.8513, graduated), a candidate
takes the AUTO lane, commits through the 24-hour undo-windowed hold and never an
instant signature, the owner undo retires the signed note through the AUDITED
path (`retired`/`retired_date`/`retired_reason` — never a raw delete, and the
vault still passes `tools/validate.py`), the category is demoted with its
evidence reset to zero, and the next candidate goes back to the owner batch.

**Never seed the live outcomes ledger.** Those records ARE the learning
evidence; a synthetic verdict in them is indistinguishable from a real one and
poisons the gate it is meant to test. The drill uses a scratch vault mirroring
the live config and a throwaway signing key, so the real audit chain is never
touched.

## 6e · Rollout gates (the three checks a bundle upload must survive)

A COS bundle upload is an owner-only click in Claude Desktop, and for a long
time the only confirmation was the owner saying they clicked. That is not a
readback, and it cost a live run: on 2026-07-30, run 57 ran **v5.37** against a
**v5.36** calibration pin, so guard condition 4 — a plain string equality —
froze auto-archive and chip re-evaluation for the whole run while every check
still reported PASS. Same shape as run 37 (2026-07-25). Three tools close it.

| Tool | Question it answers |
|---|---|
| `tools/cos_deployed_version.py <vault>` | **What is actually deployed, ON THE LANE THAT EXECUTES?** Reads the version back out of the run's own report line (AUTHORITATIVE, lane-independent — the bundle that wrote it ran) plus the **resolved deployment lane**. `--expect VERSION` exits non-zero unless the resolved lane or a run report reports it, so it can gate a pin move. See the lane note below. |
| `tools/cos_publish_pin.py --restamp --reason=... <vault>` | **Move the pin and republish in one act.** Idempotent; records the move under a dated `repinned_*` key beside the existing history; `--check` still verifies. |
| `tools/cos_reconcile_metrics.py --observation-guard <date>-run<N> <vault>/cos-ops` | **Did the funnel actually run?** FAIL when the ingest lane is open AND the mail leg enumerated threads AND the run ledger carries zero category-stamped candidates. The ledger↔metrics join cannot catch that — 0 ledgered against 0 reported reconciles perfectly (E22 precedent). Reports `PENDING` for an in-flight run and `NOT-APPLICABLE` when the lane opened after the run started, so it never cries wolf; a real PASS is never masked. |

The upload order is **click → readback → pin re-stamp → engine repoint**, all in
one sitting. A per-bundle `post-upload.sh` in the upload kit chains them and
refuses to change anything if the readback fails.

### "The deployment" is not one thing — the readback is LANE-AWARE

Two surfaces can execute the COS nightly, and they hold **different** versions:

| Lane | What it is | How the readback finds it |
|---|---|---|
| `codex-automation` | a Codex automation whose prompt names a `SKILL.md` path verbatim ("Read and execute `<path>` end to end"). That file **is** what runs. Live since 2026-07-26. | reads `~/.codex/automations/*/automation.toml`, takes the named path, reads its `kernel_version:` |
| `cowork-desktop` | a bundle uploaded into Claude Desktop's session skill store via the owner-only "Save skill" click | the bounded `STORE_GLOBS` scan of `~/Library/Application Support/Claude/local-agent-mode-sessions` |

On **2026-07-31** the tool read only the Desktop store while the Codex lane was
the execution path, and answered `MISMATCH … Do NOT move the calibration pin`
against a perfectly healthy **v5.38** deployment. Acting on that — reverting the
pin to v5.37 — would have caused the exact freeze the message warns about. A
readback pointed at the wrong surface is worse than none: it manufactures the
wrong remediation with confidence.

So the tool now **refuses to answer rather than guess**:

1. `--lane {codex-automation,cowork-desktop}` — the operator asserts it.
2. Otherwise, an **ACTIVE** Codex automation naming an existing `SKILL.md`
   resolves the lane. It is a directive, not an artifact: it states what will be
   executed.
3. Otherwise **exit 2, no verdict**. A Desktop skill-store entry proves an
   upload happened once; it never proves that store runs tonight.

A version sitting on the **non-executing** surface is printed under
`OTHER SURFACES` and never counted — it satisfies no `--expect` and refutes
none. Cover: `tests/test_cos_deployed_version.py`, plus
`python3 tools/cos_deployed_version.py --selfcheck`.

#### The Desktop store is RETIRED as a version source (DEP-03, 2026-08-01)

The Claude Desktop skill store is **not the executing surface** for the
Codex-automation lane, and reading it as one has now produced **two** false
freeze alarms — 2026-07-26 and the 2026-07-31 case above. Both times a tool
read an old version out of the store while the executing mirror was several
versions ahead, and both times the remediation it manufactured ("do NOT move
the calibration pin") was the exact opposite of correct.

Writing that down does not retire it. A stale bundle in that store stays
runnable the moment the owner opens Desktop, and any tool that reads a version
out of it still looks authoritative. So the refusal is the retirement:

- **`--lane cowork-desktop` returns `UNSUPPORTED` (exit 2), not a version**,
  whenever an ACTIVE Codex automation executes something else. The refusal
  comes BEFORE `--expect` is evaluated, so the retired surface can neither
  satisfy nor refute an expectation — satisfying one is how it produced its
  false alarms. What the store holds is still printed, under
  `WHAT THIS RETIRED SURFACE HOLDS (reported, NEVER an answer)`.
- **`brain.cos_deploy.deployed_skill(lane="cowork-desktop")` raises
  `SurfaceUnsupported`**, so a run manifest can never be stamped from it either.
- **`brain doctor` names it.** The `COS deployed skill (executing lane)` row
  reports the executing lane's version, digest and path, and — when the store
  disagrees — says in the same line that the store holds an older bundle and is
  retired. Which version runs tonight is now one command.

**The way back is the owner's click, not a flag.** Upload the current bundle in
Claude Desktop and the store matches the executing lane again, at which point
the surface answers normally with no code change. Until then the refusal stands.
One rule, both outcomes: `brain.cos_deploy.cowork_support`. Cover:
`tests/test_cos_deployed_version.py` (refusal, re-upload, and the manifest
path), `tests/test_doctor.py::test_cos_deployed_skill_row_names_the_executing_lane`.

### The negative half: `scripts/vm-boundary-probe.sh`

Run it from inside the **actual** Cowork VM — one line from the workspace root:

```bash
bash vault/.brain/vm-boundary-probe.sh
```

`scripts/vm-selftest.sh` proves the VM leg works; this proves it still refuses
all 21 host-broker verbs (`sync`, `write`, `supersede`, `ingest`,
`ingest-transcript`, `graphify`, every host `cos-*`, `verify-audit`, `rebuild`,
`snapshot`, `maintain`, `inbox`, `retro`, `project`) and resolves no signing
key — while the snapshot read and the unsigned proposal drop still succeed. A
host shell with `BRAIN_ROLE=vm` is **not** a substitute: it shares the host
filesystem, keychain and privileges, so it can pass every probe while proving
nothing about the physical boundary (run on the host it prints that warning
itself, and correctly reports BREACHED on the signing key).

Three scoring rules, each written after a measured false result (2026-07-31):

- **A non-zero exit is not a refusal.** A probe passes only when the output
  carries the engine's own role-refusal signature; a usage error or a crash is
  an `INVALID PROBE` and yields `INCONCLUSIVE`, never a pass. (Two of the
  original fourteen probes used flags the CLI does not have and scored PASS
  from argparse's usage message alone.)
- **Host-private readability is an OBSERVATION, not a breach.** The Cowork
  workspace is mounted wholesale over VirtioFS, which "may only partially
  honour POSIX bits" — AGENTS.md §9 says the VM never reads `.brain/` *by
  contract*, mount visibility notwithstanding. The check that binds is §B2: a
  token existing only under `.brain/` must return **no hit whose path is under
  `.brain/`** (path-based, because `brain grep` is a tokenised index scan, not
  `grep(1)`).
- **An unimportable engine is INCONCLUSIVE.** The frozen-ELF lane ships no
  importable `brain`, so the signing-key probe's `ModuleNotFoundError` must not
  read as "no key".

§B3 adds the **approved-queue** class (INT-01). The queue's safety is a
LOCATION property — no payload and no anchor may be reachable from the VM — and
a location claim is only worth what a real VM session measures, so the probe
walks the mount for `cos-approved/` and `*.anchor.json` and fails on any hit
(a unit test cannot make that claim: it shares the host filesystem). It also
checks the other side of the same property from what the VM CAN see: no
owner-accepted id may be sitting in the VM-writable `capture-inbox/`.

`tests/test_vm_boundary_probe.py` parses the script and feeds every negative
probe's argv to the real CLI parser, and derives the required host-only `cos-*`
set from `cli.VM_ALLOWED` — so neither a mistyped flag nor a new host-only verb
can slip through unnoticed.

**Staging is durable as of 0.19.22 (DEP-02).** The probe rides the wheel
(`ENGINE_ASSET_FILES`) and is staged `0755` into `<workspace>/vault/.brain/` by
BOTH staging paths — `tools/cowork_workspace_install.sh` and `brain update` —
exactly like `vm-selftest.sh`. Before that it was a hand copy that the next
re-stage silently deleted, which made the boundary claim unrepeatable: the
measurement existed once, on one machine, and nothing would have told you it
was gone. Cover: `tests/test_update.py::test_stage_stages_vm_boundary_probe_executable`
(+ the companion asserting the shell installer names it too — one staging path
fixed is not a fix).

## 6.5 · Trust anchors (load-bearing platform assumptions)

The broker's approval integrity rests on TWO standing platform guarantees the
whole engine already depends on — the COS verbs inherit them, they are not
re-derived here (adversarial-review, 2026-07-13):

1. **The owner answer lives in `inbox.jsonl`, which is host-only.** The batch
   RECORD (candidate set + digest) is Ed25519-signed, so a VM that edits
   `batches.jsonl` is caught. But the ANSWER (`accept all` / `apply`) is a
   plain `inbox.jsonl` entry. A VM that could WRITE `<vault>/.brain/memory/
   inbox.jsonl` could forge owner approval for its own drop — exactly as it
   could forge approval for any other owner decision. This is why `.brain/` is
   host-only by contract (AGENTS.md §6/§9): the Cowork mount must not grant the
   VM write access to `.brain/memory/`. The COS drop dirs (`drop/…`) are the
   ONLY VM-writable surface; `host/` and `.brain/memory/` are not. **Enforce
   this at the mount** (the VM leg reads the snapshot and writes only
   `capture-inbox/` + `cos/drop/`); the answer-consumer's namespace filter,
   one-shot consumption, and signed batch record are defense-in-depth ON TOP
   of that containment, not a substitute for it.
2. **The VM leg must run with `role=vm`.** The CLI role resolver defaults to
   `host` (`$BRAIN_ROLE` unset ⇒ host); the VM_ALLOWED gate refuses every
   `cos-*` verb except `cos-propose` ONLY when the resolved role is `vm`. The
   Cowork session export (`export BRAIN_ROLE=vm`, docs/cowork-windows-install
   .md) is what supplies this — the same requirement every existing
   host-broker verb (`write`, `supersede`, `ingest`) already relies on. A VM
   that fails to set the role is a mis-provisioned VM, not a new hole this
   feature opens; the mount-level containment in (1) is the backstop.

## 7 · Migration + rollback contract (0.16.1 → 0.17.0)

Forward migration is **idempotent and additive**:

- No index/snapshot schema change; no note or frontmatter change.
- The ops dir is created lazily by the broker fold (`ensure_layout`, safe to
  re-run); the overlay gains an OPTIONAL `cos/` category — a pre-0.17
  overlay validates exactly as before.
- Queue behaviour: the broker adds ONE owner-inbox namespace
  (`cosbroker:`/`coscorrect:`); existing entries are never touched
  (namespace-filtered consumer).

**Pre-migration backup** (performed by `tools/cos_canary_install.sh`, or by
hand): record `brain --version`, copy the current wheel/venv reference, and
snapshot `<vault>/.brain/memory/inbox.jsonl` + `maintain-state.json`.

**Rollback / downgrade path**:

1. Reinstall the previous engine (`pip install brainiac_cli==0.16.1` in the
   engine venv, or repoint the canary venv symlink back).
2. Restore the backed-up `inbox.jsonl` if broker questions should disappear
   (optional — a 0.16.1 engine simply ignores answered `cosbroker:` entries).
3. Optionally delete `<vault>/.brain/cos/` (derived queue state; proposals
   still pending are plain files an owner can inspect first). Nothing in the
   vault proper or the audit chain needs reverting — no 0.17 write happens
   outside the ordinary audited paths.
4. Remove `<vault>/overlay/cos/` if added (the validator treats it as
   optional either way).

**Backward-compat check**: a 0.16.1 CLI against a vault a 0.17.0 engine ran
on sees only an extra gitignored `.brain/cos/` dir and (possibly) answered
inbox entries in an unknown namespace — both inert.

**Canary scope (no unvalidated global swap)**: 0.17.0 is installed into an
**isolated, versioned venv** (`dist/engines/brainiac-0.17.0/`) scoped to the
canary vault; the globally-installed `brain` is untouched until the canary +
per-workspace health checks (`tools/workspace_registry.py` +
`brain doctor`) are green.
