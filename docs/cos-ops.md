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
The drain only reads `.brain/drafts/` and `.brain/capture-inbox/`; a proposal
drop lives in `drop/proposal-drop/`, which no sync/ingest path touches.

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
   moves **only accepted candidates into `capture-inbox/`** — whence the
   ordinary audited host drain signs + indexes them. Rejected → `rejected/`.
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

**How Ricardo's one-line Cowork correction reaches it** (defined contract):

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
auto-capture item UNSIGNED under `host/hold/`. It enters `capture-inbox/`
(and thence the signed drain) **only after** `not_before` — resolving the
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
out. A keeper is ALSO signed as a brain note (moved to `capture-inbox/` as
before); a non-keeper is spine-only — its candidate content is archived to
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
