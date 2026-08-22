---
paths:
  - "vault/**"
  - "**/.brain/**"
---

# Indexing scope exceptions

> Moved verbatim from `AGENTS.md` during the S05 context diet (2026-08-22).
> `AGENTS.md` remains canonical for Codex/Gemini; this rule is the Claude Code
> path-scoped copy, loaded only when a file under its `paths:` glob is touched.

**Indexing scope — validated or excluded, never a third state (INT-03).**
Every `.md` under `vault/` is either a note the conventions validator checks
(`brain/`, `raw/`) or it is **excluded from the retrieval index by an anchored
top-level rule**: `.brain/` (runtime), `inbox/` (drop zone), `overlay/`
(personalization config), `raw/originals/` (archived evidence), the generated
`backlinks.md`, and — since INT-03 — `cos-ops/` and any other dir listed in
`brain.notes.MACHINE_OUTPUT_DIRS`. Machine output is run reports, review-gate
drafting workspaces, and decision cards a fold wrote for itself: unvalidated,
unclassified, and (measured on the reference deployment) 78 successive
revisions of one in-flight draft crowding retrieval. When such an artifact
turns out to be knowledge, it gets **promoted into `brain/`/`raw/` through the
audited write path** — that is the route to retrievability, never a second
indexing scope. `sync`/`rebuild` report the excluded count
(`excluded_machine_output`) so an excluded tree is never silent.

> **Two exceptions, and both are deliberate: `<index dir>/cos-approved/` and
> `<index dir>/cos-attachment-anchors/`.** The COS approved queue (INT-01,
> `docs/cos-ops.md` §2c) holds owner-accepted content that is not yet signed
> into `vault/`, so between the accept and the next drain it is the ONLY copy.
> The attachment ACCEPTANCE ANCHORS (INT-04) are the host-signed record of
> which bytes the owner accepted for each file already released into
> `vault/inbox/`: the payload survives losing one, but the anchor is what holds
> that file at its email-derived MNPI floor and proves the bytes are the
> accepted ones — so losing it makes the next drain REFUSE the file (fail
> closed, never an unlabelled `Internal` ingest) until it is re-accepted. Both
> live here precisely because the VM cannot reach here. Everything else under
> the index dir is disposable; these are not. Drain first (`brain sync`) before
> deleting the index dir, repointing `$BRAIN_INDEX_DIR`, or uninstalling.
> `brain status` reports `cos.approved_awaiting_signature` and
> `cos.attachment_anchors_awaiting_drain`, and `brain rebuild` returns a
> `warning` + both counts in its RESULT (so a headless/launchd run sees it too,
> not just a TTY) whenever items are still waiting.
>
> **A third exception, different in kind: `<index dir>/cos-corpus/`
> (CAP-01/CAP-02, `docs/cos-ops.md` §2d).** One append-only JSONL per COS run
> holding the MESSAGE TEXT that run's verdicts were made from — the input the
> ingestion ledgers discard, which is why re-judging anything used to cost a
> 90-minute live run. It is not a queue and nothing drains it. **Retention is
> AUTOMATIC:** `cos_corpus.prune` deletes expired corpora as
> WHOLE run files (never rows within one — a partially pruned corpus would
> silently change a replay's denominator), it honours
> `$BRAIN_COS_CORPUS_DAYS` (default 30), and the nightly `brain maintain`
> daily retention block calls it beside the duplicate and query-log prunes —
> so mail bodies age out on a schedule, not on an operator remembering. An
> expired corpus that never CLOSED is held (a live writer's inode) and
> reported as action-required rather than deleted on a guess.
> `brain status` reports `cos.capture_corpus` (runs, bytes, oldest run and its
> age, unclosed runs, and `pruned_by_a_scheduled_fold` +
> `last_scheduled_prune` — read from this host's `maintain-state.json`, so a
> host where the nightly never ran still says `false`). It is the most
> sensitive thing on this disk:
> real mail bodies, classified **MNPI** (the file's tier is the floor of its
> most sensitive row and no overlay mapping lowers it), owner-only, proven off
> the VM mount, and never indexed — it is not under `vault/`, so `scan_vault`
> never reaches it. That exclusion is STRUCTURAL, not a filter: no indexing
> rule was weakened to get it. Losing it loses evidence rather than stranding
> pending work, so it is not a drain-first item; repointing `$BRAIN_INDEX_DIR`
> simply starts a new corpus. **Uninstalling or deleting the index dir is a
> DECISION, not a cleanup:** up to a window's worth of real mail bodies is in
> there, and an uninstall stops the nightly that would have aged them out —
> leaving it behind leaves them at rest forever.
> Read `cos.capture_corpus` in `brain status`, then keep the directory
> deliberately or delete it deliberately.
>
> **A fourth, and it is evidence like the third: `<index dir>/cos-runs/`
> (gap-05, `docs/cos-ops.md` §2a).** One directory per vault holding the COS
> run manifests, the recorded run verdicts and the plan bindings — the
> authorities a night is judged BY, not artifacts a night writes. They lived at
> `<vault>/.brain/cos/host/runs` until 2026-08-16 and were called "host-private"
> and "never VM-writable" in three places: true of the VM's RULES, false of the
> filesystem, since that path is inside the Cowork workspace. The forged copy a
> VM could write there decided whether a run's candidates were claimable and
> which plan its apply was allowed to have dispatched. Existing records are
> carried forward ONCE; a record present in both places with differing bytes is
> refused and takes that run to INCONCLUSIVE rather than either copy winning.
> Not drain-first — losing it makes past nights unverifiable rather than
> stranding pending work.

