# VULN-3385 — risk-reduction record (CLOSED 2026-09-01)

**For a reader who did not follow the work.** The reported bypass is fixed. A
Cowork agent read a Restricted note straight off disk — unfiltered, unrecorded,
with write access. The vault is off the sandbox mount, every read now goes
through the host broker's classification filter, and the read record fails
closed. §2 and §3 carry the measurements.

If you take one thing from this page: **a Cowork session can still obtain a
high-tier note, and that is intended.** It gets it through the filtered, logged
broker, because an agent that may read what its owner may read is the product
working (`docs/security-acceptances.md` A-01). The question that stays live is
not what an agent may read but what can LEAVE without the owner deciding —
tracked as indirect prompt injection at **A-06**, with its controls and three
stated limits.

This page read "NOT a closure" until 2026-09-01, on a closure test — a
per-caller tier ceiling — the owner ruled was the wrong test. §5 records the
change and preserves the original argument rather than deleting it.

Register entry: `docs/security-acceptances.md` A-05 (closed) and A-06 (the live
residual). Install-page summary for operators: `docs/install/cowork.md`'s
warning box.

**On this file's name.** It was written as `vuln-3385-closure.md` and renamed
on 2026-08-30, when a page titled "NOT a closure" could not sit at a path
ending `-closure.md`. The name stays as it is: this page's value is the
risk-reduction record, and renaming it again would break every reference to it
to gain nothing.

## 1. The finding, as reproduced

Anthropic's Cowork product runs an agent inside a Linux VM sandbox and lets an
owner attach a folder from their own machine so the agent can work with it.
This vault attached that folder read-write, over VirtioFS, directly — the
whole `vault/` tree, every classification tier, as ordinary files.

A penetration test asked the Cowork agent for a simple search over the vault.
Instead of using the `brain` CLI (which applies a classification filter
before returning anything), the agent used plain file tools and read a
Restricted-tier note straight off disk. No control saw it happen; no record
of the read exists anywhere. The same attachment made `vault/` writable with
no draft-approval step — a session could put a file directly into the signed
corpus.

Two properties made this worse than a single missed check:
- The read bypassed the classification gate **entirely** — not a wrong tier,
  no tier at all.
- It left **no record**. A host operator reviewing what a Cowork session did
  would see nothing, because the read never touched any instrumented code
  path.

## 2. What changed, in plain language

**The vault is no longer on anything Cowork attaches.** Nothing was moved —
the vault sits at the same path it always did. What changed is the
*registration*: the folder Cowork previously attached was detached, and a new,
much smaller folder (engine binaries, model cache, skill bundles — no vault
tree, no snapshot, no note bodies) was registered in its place. The Cowork
sandbox now has nothing on its mount to read a note from, because there is no
note there.

**Every read Cowork can still make goes through one broker on the host,
not a folder on the sandbox.** The `brain-mcp` broker runs as a small program
on the owner's own machine, spawned by Claude Desktop and talked to over a
private pipe (not a network port a sandbox process could dial directly). It
answers exactly the same read requests the CLI always has — search, get a
note, recent notes, and so on — and it applies the identical classification
filter before returning anything. Crucially, it now also writes a record of
every read of a VAULT NOTE **before** handing back content, and if that record
can't be written, the read fails instead of quietly succeeding. Before this
work, a failed record write was swallowed and the read succeeded anyway — a
silent gap in exactly the evidence trail meant to catch this kind of thing.

"Every read of a vault note" was the precise claim until 2026-08-30, and the
qualifier was load-bearing: one registered tool, `vault_languages`, returned
vault METADATA — per-language note counts — and moved the ledger by zero. It
now gates the note list under the caller's ceiling and leaves a row like every
other read (§4 records the closure), so the unqualified sentence is true now:
every read through the broker leaves a record.

**The 14 skill bundles Cowork actually ships were rewritten** to call this
broker instead of shelling out to a CLI command against a mounted vault. That
matters because a skill bundle is what an owner's Cowork session actually
runs day to day — fixing the broker without fixing the skills that bypass it
would have closed nothing in practice.

## 3. What proves it — every number re-run today (2026-08-30)

All counts below were produced by running the named test files at the moment
this page was written, from `.plan-worktrees/closed-stacks-2026-08-27`
(worktree HEAD `572c1cf`), using
`PYTHONPATH=src .venv/bin/python -B -m pytest -q <files>`. Full verbose
transcript saved alongside this plan's evidence:
`_plans/closed-stacks-2026-08-27/_evidence/s08/pytest-vuln3385-2026-08-30.txt`.

| Claim | Test file(s) | Result |
|---|---|---|
| No note body is recoverable from the new (relocated) workspace by path, glob, or content search | `test_direct_file_read_relocated.py`, `test_cowork_staging_off_the_mount.py` | **55 passed** |
| Every registered MCP tool applies the classification filter; the broker's own record-write is fail-closed | `test_mcp_broker_seam.py`, `test_mcp_tool_surface.py` | **84 passed** (83 until 2026-08-30; the new test pins that `get` no longer leaks existence through the egress counts) |
| The 14 shipped skill bundles call the broker, not a direct `brain --role vm` shell-out against a mounted vault | `test_cowork_skill_bundles.py`, `test_cowork_skill_verbs.py` | **50 passed** |
| A Cowork verb against a workspace with no local vault data fails loudly (exit 3), not quietly | `test_desk_fails_closed.py` | **13 passed** |
| The two S01 pentest-replay probes | `test_closed_stacks_s01_egress_probes.py` | **5 passed.** Until 2026-08-30 this file asserted the DEFECT on purpose (4 tests, "When s03-s06 fix either one, these tests fail"), and its passes proved both leaks were still live. It was rewritten the same day, when both leaks were closed (§4, owner option A): it now asserts the FIX, each with a known negative, and its passes mean the leaks are closed. |
| The remaining originals/broker-door/leak-scan/capture/housekeeping/retrieval/restage/snapshot-registry/install-guard surface | `test_originals_resolve.py`, `test_mcp_broker_doors.py`, `test_direct_file_read.py`, `test_cowork_leak_scan_note_dumps.py`, `test_cowork_scan_budget.py`, `test_mcp_tools_capture.py`, `test_mcp_tools_housekeeping.py`, `test_mcp_tools_retrieval.py`, `test_cowork_restage.py`, `test_cowork_snapshot_dir_registry.py`, `test_cowork_documented_setup_route.py`, `test_cowork_install_snapshot_guard.py` | **140 passed, 1 skipped** (the skip is `test_cowork_restage.py`'s known-environment guard) |
| **Combined, in one run: the 19 files above EXCEPT `test_desk_fails_closed.py`** | (all of the above bar that one file, which was measured on its own) | **334 passed, 1 skipped** = 55 + 84 + 50 + 5 + 140 (332 = 55 + 83 + 50 + 4 + 140 before the 2026-08-30 fix). This row read "all 19 files above" and split the last row as 127 until 2026-08-30, which reconciled the arithmetic by writing a number no command produced: the table lists 20 files, the combined run covers 19, and 127 is 140 minus the 13 measured separately. |

The old fail-open line is gone from BOTH record-write paths, confirmed by
reading the current source (`src/brain/mcp_mediation.py` for the broker,
`src/brain/cli_read_record.py` for the CLI) — it survives only as docstrings
naming what it used to do. It said "the broker's own record-write path" and
named the CLI as the one place it was deliberately still true until
2026-08-31, when the CLI leg was closed too (§4).

The registered-workspace fact was verified against the live host, not
inferred: `brain doctor`'s VULN-3385 row reports the new workspace current and
the old (detached) folder still carrying the historical leak finding — which
is correct, since that folder still holds the vault and is simply no longer
attached to anything.

## 4. The residual, stated plainly

**Two leaks the S01 probes measured as live — CLOSED 2026-08-30.** Both were
inside the broker, not the mount, and the plan's sessions did not touch either.
They were listed in §3 as evidence of the fix until 2026-08-30, which had the
sign backwards; the same day the owner chose to keep the full-vault ceiling
(the 2026-08-17 ruling stands) and close the two leaks in code, and
`tests/test_closed_stacks_s01_egress_probes.py` was flipped to assert the fix
with a known negative beside each.

- **The existence oracle under a clamped ceiling — closed.** A caller held
  below a note's tier used to get a visible `null` either way while the
  `egress` block differed — `1/1` for a withheld note against `0/0` for an id
  that does not exist — so it could learn WHETHER an id existed without reading
  a word. `get`/`read` now return the absent-shaped report whenever nothing
  surfaced (`mcp_verbs.dispatch_note`). The host read record still counts the
  withheld note; only the caller-visible report is normalised. This is what
  makes a lower per-caller ceiling safe to wire later: §5's proposal no longer
  arms a metadata leak. Still open and recorded, not changed: the same counter
  on `bases_query`'s predicate and on the ranked verbs, where the survey's
  `recorded_not_changed[0]` explains why it stays the engine-wide contract.

- **`vault_languages` with no record and no ceiling — closed.** It now gates
  the note LIST (ids and tiers, never bodies) under the caller's `max_tier`, so
  it tallies, leaves a SEC-06 row, and counts languages only over the notes
  the ceiling admits. At the full-vault ceiling the cached census is returned
  unchanged; under a lower one the admitted bodies are classified on the spot
  and the block carries `scoped_to_max_tier`. It left `BODYLESS_TOOLS`.

Neither was a bypass of the mount and neither returned a note body. Both are
kept here, closed, because a record that lists only the residuals its own plan
created is not a residual list — and because the probe file that asserted them
open now asserts them closed, so a regression fails a test rather than a reader.

**No per-caller tier ceiling exists.** The broker's classification ceiling
comes from one process-wide environment variable
(`$BRAIN_MAX_EGRESS_TIER`) that cannot distinguish a Cowork caller from the
owner's own Desktop app calling the same broker. Checked live against this
machine's actual Claude Desktop configuration on 2026-08-30, and re-counted
after review the same day: all FOUR `brain-mcp` entries in this machine's Claude Desktop config carried `BRAIN_MAX_EGRESS_TIER=MNPI` (full vault) and none set `BRAIN_ROLE`. **Re-measured 2026-09-01, and it had drifted:** ONE stanza read `Internal` while a SECOND stanza over the very same vault read `MNPI` — one vault answering two ways depending on which entry asked, which is the "answers from scraps" failure `connect.py`'s own comment names. The owner re-ran `brain connect --max-tier MNPI` the same day and all four read `MNPI` again. **No entry has ever set `BRAIN_ROLE`, so the finding itself never moved** — only the count did, twice, which is why it is re-read rather than quoted. (Stanza names are deliberately not written here: they carry a client identifier, and this file ships) — the top tier, i.e. the full
vault — and **none sets a role at all**. The first count said two, and had
inspected two; the two it missed carry identical values, so the finding stands
either way. It is recorded here because a security record that quietly corrects
its own numbers is worth less than one that shows them being corrected. The code that builds
these entries (`connect.py`) never writes a role variable in the first place,
so even if a lower ceiling were configured for Cowork specifically, nothing
distinguishes which caller is asking. This gap was flagged in writing during
this plan's own work (s06b, 2026-08-28) as a finding for a later session; no
session in this plan closed it.

**The practical consequence:** a Cowork session, using a shipped skill
bundle exactly as intended, can still ask the broker for the same
Restricted-tier note the original penetration test read off disk — and the
broker will hand it back, because its ceiling is the full vault by design
for every caller today. The difference from before is real and worth having:
the read is filtered through the same classification code path as every
other read, and it leaves an audit record that fails closed rather than
silently. But nothing here *prevents* the disclosure. This is why the
finding stays open.

**A narrower, related asymmetry — CLOSED 2026-08-31, after this plan.** The
record-write fail-closed fix applied to the broker's own path only; the plain
CLI's equivalent still swallowed its own failures. That was close to moot for
a brand-new Cowork workspace, because there is no local vault data left for a
CLI verb to read against (it exits 3 instead, confirmed above) — but it
remained true for the owner's own shell on the host, which is the leg
`CLAUDE.md` points every session at and the one the nightly launchd job runs.

The CLI now fails closed the same way, differing only in HOW it refuses.
The broker returns a value, so it raises; the CLI streams to stdout and cannot
un-print, so `brain.cli_read_record` HOLDS the output of a gated verb until
the record is written, then releases it — or discards it and exits `5` with a
message naming the cause. Output from a verb that never gates (`maintain`,
`rebuild`, `doctor`) streams unchanged, so the long-running commands are
untouched. Probed in both directions
(`tests/test_cli_read_record_fails_closed.py`, 7 tests): with the fix reverted
the withholding test fails; with it in place a forced record failure withholds
the notes and leaves no row, while `BRAIN_READ_LOG=0`, a non-gating verb and an
empty result set all still answer.

**No per-session original-document hand-off exists, at all.** An earlier
design would have let a Cowork session request one archived original file,
staged into a directory scoped to that one session behind a time-bounded
lease. It was retired by owner ruling before it shipped: Claude Desktop does
not forward any per-session identity the broker can verify, so there is no
way to bind a staging directory to one caller — an unguessable folder name is
not access control. What exists instead is `brain authorize-original`, which
runs host-only, is refused before the vault even opens if invoked with a
Cowork role, and records a decision (which tier, when) without ever learning
or delivering the document itself. The only way a document actually reaches
anyone is a host operator manually running `brain project --dest <dir>
--max-tier <tier>` — a human, at a keyboard, outside any Cowork session.

**Marketplace propagation is not verified from this repo.** The 14 rewritten
skill bundles were released to the skill marketplace as version 0.20.32. A
Cowork workspace only receives that text when it next refreshes from the
marketplace on its own schedule — this repository cannot see, and this page
does not claim to know, whether any specific already-running Cowork
workspace has done so yet.

**Shipped since, and what it is worth here (2026-09-01, 0.20.35).** Three
things landed after this page was last re-read. None of them is item 1 of §5,
so none of them moves the verdict — they are recorded because a residual list
that omits the mitigations built against it understates the posture, and
because two of them carry limits a reader must not guess at.

- **SEC-07, the outbound term guard.** `brain check-egress` judges one
  outbound string against the vault's decoder ring and exits `6` at or above
  `Confidential`; `scripts/brainiac-egress-guard.sh` is the Claude Code
  `PreToolUse` wiring. This closes a channel this page never listed: the
  classification gate decides what a caller may READ and said nothing about
  what the model then puts into a tool-call argument, and nobody reviews a
  web-search query string. **Its reach is asymmetric and that matters here.**
  It is ENFORCED where Claude Code hooks run — the host's `~/.claude`. On the
  Cowork leg the verb is `VM_ALLOWED` and the rule is carried as PROSE in the
  staged `AGENTS.md`; nothing fires it automatically, exactly the soft
  instruction-level guarantee `brain --role vm alerts` relies on. So the leg
  this finding is ABOUT gets an instruction, not a mechanism.
- **The decoder ring fills itself.** The guard's ring was hand-written, which
  meant empty on any vault nobody curates — an inert guard. A nightly fold now
  derives it from the vault's own `project` notes classified `Confidential` or
  above, plus their aliases. Narrowed to `project` by measurement: harvesting
  `concept`/`company`/`person` produced vocabulary, public firm names and bare
  first names, and an over-refusing guard is switched off within a day. The
  generated ring is EGRESS-ONLY and deliberately not in `overlay/keywords/`,
  which feeds ingest classification.
- **The two indirect-injection detectors now run unattended.** The SEC-05
  concealed-instruction corpus re-scan and the SEC-06 bulk-read alarm lived in
  the `brain integrity` CLI body, so they fired only when a human typed the
  command. Both moved into `core.integrity()` and the Tuesday fold raises each
  with its own `notify_key`, reporting a COUNT and never note names.

**What none of it does.** A Cowork session can still ask the broker for the
same Restricted-tier note and receive it, because the ceiling is the full vault
for every caller by design. The guard constrains one exfiltration channel on
one leg; it does not establish a per-caller ceiling, and it cannot see a
paraphrase or a fact restated in the model's own words. §5's verdict is
unchanged.

## 5. CLOSED 2026-09-01 — and why this section is kept, not deleted

**The finding is closed.** Everything below §5's heading was written while the
closure test was "a per-caller tier ceiling for the Cowork leg", and it argued
correctly that the test was not met. On 2026-09-01 the owner ruled that the
test itself was wrong, and the reasoning is short enough to state in full:

- Retrieving a high-tier note is **functionality, not a vulnerability**. It is
  the ruling already recorded at A-01, applied here: "the purpose of brainiac
  is to work and access all kind of information the user has access to".
- The **reported incident** was an agent reading a note off disk — unfiltered,
  unrecorded, with write access, invisible. Every part of that is fixed and
  re-measured in §2–§3.
- What was left was an agent obtaining a note **through the broker**: filtered,
  ceiling-checked, and recorded fail-closed. That is the product working.
- The live risk is therefore not what an agent may READ but **what can leave
  without the owner deciding** — a prompt-injection question. It is tracked on
  its own terms at **A-06** in `docs/security-acceptances.md`, together with
  the controls built for it and three limits stated plainly, including that
  SEC-07 does not run inside Cowork.

The original argument is preserved verbatim below because a record that
silently rewrites its own verdict is worth less than one that shows the verdict
changing and says who changed it and why. Read it as: correct against the test
it was given, and the test was replaced.

### 5a. The original argument — why this was a mitigation under the old test

The plan's own acceptance criteria, written before any of this work started,
drew the line explicitly: the outcome is a closure only where a per-caller
ceiling was established for the Cowork leg, and a mitigation — finding left
open, at reduced severity — everywhere else. Session s01 measured, live and
reproducibly, that no such ceiling exists; its checkpoint recommendation
(approved 2026-08-27T22:55:03) was to re-scope the plan around a configuration-level
tier clamp and a host-side read record, and to drop a fully isolated,
per-session hand-off to a documented non-goal with the manual `brain project`
copy as the fallback. That is exactly what shipped. Nothing after that
checkpoint changed the answer to the ceiling question — re-verified fresh for
this page, both in the current source and against the live host
configuration.

**What would actually close this finding — and where each item now
stands, re-read 2026-09-01.** This list named three things when it was
written. Two of them have since been answered, and the answers point in
opposite directions, so the list is kept and annotated rather than rewritten:

1. **Wire `connect.py` to write a distinct, lower `BRAIN_MAX_EGRESS_TIER`
   (and a `BRAIN_ROLE`) into whichever Desktop config stanza Cowork uses**,
   so the broker's existing ceiling has something per-caller to clamp
   against. **DECLINED by the owner on 2026-08-31** (recorded at A-05 in
   `docs/security-acceptances.md`): the full-vault default stands, as ruled
   on 2026-08-10 and again on 2026-08-17, because the broker cannot
   distinguish a Cowork caller from a host one, so clamping the sandbox
   clamps the owner's own Desktop sessions with it. Do not re-raise without
   new information. This is the item that decides the finding's fate: with
   it declined, **no path to a closure remains, and A-05 is the terminal
   state of VULN-3385, not a way-station.**
2. **Make the CLI's own record-write fail-closed to match the broker.**
   **DONE 2026-08-31** (§4, `brain.cli_read_record`), and shipped in
   0.20.33. It did not close the finding, which is the honest measure of
   what it was worth. It also cost a regression: fail-closed inherited
   `querylog`'s POSIX-only gates and withheld every gated read on Windows
   until 0.20.34 repaired it.
3. **A Claude Desktop mechanism that forwards a per-session identity the
   broker can trust**, if a real per-session original-document hand-off is
   still wanted. Still absent, still outside this repository's control.
