---
paths:
  - "src/brain/**"
  - "vault/.brain/**"
  - "docs/cowork-windows-install.md"
  - "cowork*/**"
---

# Host / VM trust split (load-bearing)

> Moved verbatim from `AGENTS.md` during the S05 context diet (2026-08-22).
> `AGENTS.md` remains canonical for Codex/Gemini; this rule is the Claude Code
> path-scoped copy, loaded only when a file under its `paths:` glob is touched.

## 6 · Host / VM trust split (load-bearing)

`brain` runs in two trust contexts. **Capability is split by context:**

| Context | May do | May NOT do |
|---|---|---|
| **Cowork Linux VM** (sandbox, EDR-blind) | `search`, `get`, `recent`, `draft_capture` (full VM_ALLOWED list: `init, doctor, alerts, search, hybrid-search, diagnose, grep, bases-query, graph-expand, get, read, recent, status, draft-capture, capture, brief, digest, cos-propose, provision-request` — `diagnose` is read-only and applies the same egress gate; `alerts` is the degradation digest every harness runs at session start (§9), file-reads only, and names the host-home sources the VM cannot reach instead of skipping them; `cos-propose` is an UNSIGNED drop into a proposal-drop dir `sync` never reads; only the host broker's owner-inbox gate can move it toward signing; `provision-request` (PRV-10) stages a NEW-VAULT request marker — a plain-file drop, no key, no launchd, no registry — that the host's `provision-drain` completes, see the protocol below) | sign, index-commit, WAL write, snapshot, `write_note`, `ingest`, `ingest-transcript`, `supersede`, `unsupersede`, `graphify`, every other `cos-*` verb (broker/correct/evidence/priority-map/hold) |
| **HOST broker** (macOS/Windows, EDR-visible, holds the audit key) | everything: `write_note`, audit signing, WAL writes, snapshot generation, index commit, plus the ADR-0003 host-only verbs `ingest`/`ingest-transcript` (drop-zone → signed `raw/`, originals archived immutably), `supersede`/`unsupersede` (both sides of a version chain, and its audited undo), `graphify` (bounded monthly link-discovery build) | — |

**Why:** the Cowork VM is ephemeral, EDR-blind, and not audit-logged — it must
never be the thing that signs the audit chain or mutates the canonical index.
The VM is a **read + draft** surface only; the host is the **only writer**.

### VM-draft → host-commit capture protocol

1. **VM `brain draft-capture`** writes a candidate file to `.brain/capture-inbox/`
   (on the VirtioFS mount, so the host sees it) with `status: draft` and a
   `provenance.trust: untrusted` stamp. It does **not** touch the index, WAL, or
   audit chain, and it does **not** resolve a signing key.
2. The draft sits on the **shared mount** (host-visible immediately). It is NOT
   under `vault/` proper, so `scan_vault` never indexes it as a real note.
3. **Host drain-on-invoke** (`brain sync`, first step): for each draft in
   `capture-inbox/` (and legacy `.brain/drafts/`), the host-broker `write_note`
   validates frontmatter + classification, computes `sha256`, promotes it into
   `raw/` (if source) or `brain/resources/` (if note), **Ed25519-signs** the
   audit-chain entry, writes the **WAL**, and **commits to the sqlite index**.
   The draft is removed after a successful, signed commit (fails closed: no key ⇒
   draft left in place, never promoted unsigned).
4. **Snapshot publish** (`brain sync --publish` / `brain snapshot`): the host
   atomically republishes the read-only, generation-stamped snapshot into
   `.brain/snapshot/`. Only now is the note retrievable from the VM.

### VM-request → host-drain vault provisioning (PRV-10, 2026-08-17)

A Cowork session can also create a whole NEW vault, with the same trust
split. It scaffolds `<workspace>/vault` as plain files and runs
`brain --role vm provision-request` — which writes ONE marker,
`vault/.brain/provision-request.json`, and nothing else (no key, no launchd,
no registry). The host's `provision-drain` (a fold on every registered
vault's hourly `brain-nightly` daily branch, and a host verb to run it on
demand) scans the PARENT directories of already-registered workspaces for
pending markers and completes each: `brain init --full --apply` (key check +
per-vault nightly registration + seeding), model staging from a local copy,
`brain sync --publish`, and the registry upsert. The outcome lands beside
the marker as `provision-result.json`, readable from the VM. Rules: the
vault path derives from WHERE the marker sits, never from its contents; an
already-registered vault is never re-provisioned; every provision is
reported through the maintain results, never silent. Stated limit: the
FIRST vault on a machine still needs one host `/brainiac-install` — an
empty registry has no roots to scan and no nightly to ride.

**No capture daemon, no dedicated drain task.** The host drains *on invoke*.
Scheduled tasks are a **small, curated set, each justified on its own merits —
no fixed cap, no over-engineering** (owner ruling 2026-07-20, superseding the
earlier fixed-two-tasks framing): currently **`brain-nightly`** — the
maintenance umbrella (fires **hourly**; every firing runs sweep + ingest +
drain + incremental sync + snapshot publish + the self-organization folds of
§4 rule 4 — a captured document is searchable within the hour — while the
weekly/monthly branches stay date-gated) and **`brain-synthesis`** — a weekly
(Sun 08:00), registry-driven, model-backed kb-curator session that keeps the
SYNTHESIS layer (state/MOC notes, promotions, index.md) current, since prose
synthesis needs a model the engine deliberately does not hold. Recurring
vault-METADATA work (the graph_hygiene fold is the reference case, §4 rule 4)
should normally join the `brain-nightly` umbrella as a date-gated branch
rather than become a new scheduled task — a fold reuses the umbrella's
run-lock, state file, escalation, and health-history plumbing for free, with
no separate plist/cron entry to manage. `brain status` surfaces snapshot
generation/age + pending-draft count so staleness is visible, never silent.

So: **a VM session can read and propose; only the host can canonise.** A draft
is never authoritative and never surfaced by `search` until the host commits it
and republishes the snapshot.

### Single-writer discipline (CC-01/CC-02, 2026-07-20)

The hourly `brain-nightly` job and a hand-run `brain sync`/`rebuild` write the
SAME sqlite index, so two protections apply to every index-mutating verb
(`sync`, `rebuild`, `maintain`, `snapshot`, `restore-index`, and — since the
2026-07-20 dedup-batch fix (finding 1) — `supersede`, whose full critical
section (both signed note writes + its trailing reindex) now runs under ONE
acquisition of the same lock, never an unbounded wait):

- **A bounded, jittered write-retry** absorbs transient lock contention
  ("database is locked") past the existing 5s `busy_timeout` — write
  transactions use `BEGIN IMMEDIATE` (not SQLite's default DEFERRED) so the
  busy handler can legally wait instead of hitting an un-retriable
  lock-upgrade `SQLITE_BUSY`. Bounded to ~30s total (`$BRAIN_WRITE_RETRY_SECONDS`).
- **An advisory single-writer lock** (`fcntl.flock` on a HOST-PRIVATE
  `writer-<vault>.lock`, off the Cowork mount beside the COS append locks —
  process-lifetime, NOT a pidfile: the kernel releases it on crash/kill, so
  there is no stale-lock heuristic to get wrong) serializes the hourly job
  against a hand-run command. It lived at `<vault>/.brain/writer.lock` until
  INT-05; on the mount, a lock can be unlinked while a holder has it and
  replaced at the same name, after which a second holder locks the NEW inode
  and both run against one index. Re-entrant within one process (`sync` self-delegating to
  `rebuild` shares one lock, never deadlocks on it). Bounded wait
  (`$BRAIN_WRITER_LOCK_SECONDS`, default 30s); the loser's error names the
  current holder's pid + verb. **Read paths (`search`/`get`/`recent`) and the
  VM leg NEVER take this lock** — it is created only by the HOST-broker write
  verbs above.
- **A blocked hourly run skips cleanly, not with an error** — a rebuild can
  legitimately hold the lock for 90 minutes. `brain maintain`'s `daily`
  branch reports `status: "skipped-writer-busy"`, refreshes `last_attempt`,
  and increments a separate `consecutive_skips` counter (+ `writer_busy_since`
  on the first skip of a streak) — it never increments
  `consecutive_failures` and never touches `last_run` (those mean *work
  completed*, and are what liveness detection keys on). `>= 6` consecutive
  skips, or a `writer_busy_since` older than a bounded grace period, is
  pathological (a leaked lock, a wedged rebuild) and must be surfaced as
  loudly as a failure — never silently reported HEALTHY.
- **The supersede crash journal is host-private too** (ENF-01, 2026-08-10). It
  is the rollback record for an unfinished `supersede`/`unsupersede`, and it
  holds BOTH NOTES' COMPLETE PRE-IMAGES — full note text at whatever tier
  those notes carry. On `<vault>/.brain/` that was unrestricted
  Confidential/Restricted/MNPI content sitting outside the egress gate, so it
  moved off the mount beside the writer lock (same helper, same app-data
  fallback). A journal exists only while a transaction is unfinished; an
  unparseable or STRUCTURALLY INCOMPLETE one fails closed — preserved, and
  every supersession verb refuses until a human repairs the pair. `brain
  maintain` validates/recovers it ONCE at the top of the run, before any
  branch, so a blocked journal can never read as an ordinary skip.

