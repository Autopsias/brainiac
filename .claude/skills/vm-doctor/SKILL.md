---
name: vm-doctor
description: "In-Cowork (VM-leg) health and version diagnosis for a Brainiac workspace: runs the three read-only STAGING probes (`brain --version`, `brain doctor`, `brain status --json`) and reports one VERSION-READY verdict with a per-surface breakdown. Triggers: \"is the brain healthy\", \"which version is running\", \"run a diagnosis\", \"vm doctor\", \"check the engine\", \"is semantic search working\", \"is cowork version-ready\", \"brain doctor\", \"is the vault stale in cowork\", \"is the engine staged in this workspace\". NOT for host-side trend/health-history readouts across every registered vault — that's the host's `/brainiac-health`; this skill is the in-VM, single-workspace lane, read+draft only, and never installs, upgrades, or troubleshoots vendor/model files itself."
---

# vm-doctor (Cowork VM-leg health check)

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

**Motivating failure (2026-07-20):** a Cowork session was asked to run
`brain doctor`. No `brain` was on PATH, and rather than say so, the agent
invented a wrong claim ("doctor is a host-only verb") and gave up. The remedy
it reached for at the time — loading the session bootstrap — is now FORBIDDEN
(see the desk block above and §1); that half of this story is superseded
history, not instruction. The error being recorded here is the inference, not
the missing bootstrap: `doctor` **is** in the CLI's
`VM_ALLOWED` list (`src/brain/cli.py`, `run_doctor_vm`) — it is a full
VM-leg health table, read-only, safe to run any number of times. This
skill's job: make the diagnosis deterministic so no future session
rationalizes "command not found" into a wrong architecture claim.

## Checklist

1. Decide which lane you are in (§1) — in Cowork there is nothing to bootstrap
2. Run the three staging probes in order
3. Read the interpretation table below against each probe's output
4. Apply the hard rules (never troubleshoot the fix yourself)
5. Emit the one-screen report

## 1 — Which lane you are in (decide this first)

**Cowork (VM leg): bootstrap nothing.** The desk block above is the whole story
for vault content, and that is a RULE, not yet a fact about the filesystem: a
workspace provisioned before the s07 cutover still has the vault and its
snapshot on the mount. Bootstrap nothing REGARDLESS — pointing at a mount vault
is what the desk exists to replace, and content read that way skips the desk's
read record. After the cutover there is nothing there to point at either. Run the §2 probes with whatever `brain` is on `PATH`. **If there is none, that
is NOT evidence the workspace is unstaged.** The workspace installer stages the
engine at `<vault>/.brain/brain` and `<vault>/.brain/bin/` and never writes to a
`PATH` location; the only thing that ever put `brain` on `PATH` was the
per-session bootstrap this section deletes. So on a fully staged workspace,
following this rule, `command -v brain` finds nothing. Report exactly that —
"no `brain` on this session's `PATH`; staging cannot be confirmed from here" —
and go to §2b. Do not report "no staged engine".

The bootstrap block this section carried until 2026-08-28 (`export
BRAIN_VAULT="$PWD/vault"`, `BRAIN_RUNTIME_DIR`, a `PATH` prepend) was deleted,
not moved. It points at a mount-resident vault — one the s07 cutover removes,
and one that must not be read even while it is still there — and re-exporting it
teaches a session to hunt for note content inside the sandbox.
Never reintroduce it, and never copy it out of an older bundle.

**Host (the owner's Mac): nothing to do.** `brain` is already on `PATH`; run
the §2 probes against the vault you mean (`--vault <dir>`).

## 2 — The three staging probes, in order

```bash
brain --version
brain --role vm doctor   # read-only; exits non-zero if any gating surface is stale
brain --role vm status --json
```

Run all three even if the first two look clean — `status` carries the
embedder and snapshot detail `doctor` summarizes but doesn't fully quote.
**Keep `--role vm` on the last two in Cowork.** The bootstrap deleted in §1
carried `export BRAIN_ROLE=vm`; with nothing setting it, `config.role()`
defaults to `host`, whose egress cap is the whole vault. On the HOST lane drop
the flag and pass `--vault <dir>` instead.

**These read the STAGING, never a note.** Measured 2026-08-28 against a
workspace with no published snapshot: `doctor` runs to completion and reports
`Snapshot (read-only, .brain/snapshot) ... not-detectable` under a
`STALE: 2 required surface(s)` verdict, and `status --json` returns
`"index": {"error": "OperationalError: unable to open database file"}` with its
`embedder`, `snapshot` and `pending_drafts` blocks intact. An erroring `index`
block is a CORRECT reading of a workspace that holds no local index — quote it;
it is not a broken engine and not a reason to go looking for the files.

## 2b — What the desk answers, and what nothing answers

`doctor` and `status` are LOCAL probes and the desk does not serve either one:
both score `add_read` in `docs/operations/cowork-skill-verb-survey.json`
(VM-allowed over the CLI, never registered as an MCP tool). So in a Cowork
session there is no remote substitute for §2 — if the workspace carries no
staged engine, the honest report is that the staging cannot be inspected from
here, and the owner runs `brain doctor` on the host.

For health the desk does serve `alerts` (the degradation digest) and
`exceptions` / `inbox` (what needs the owner). Call those for "is anything
wrong with the vault?"; §2 answers only "is this workspace staged correctly?".
Never substitute a file read for either.

## 3 — Interpretation table

| Signal | Where | CURRENT looks like | Otherwise means |
|---|---|---|---|
| Engine stamp vs skill bundles | `doctor` rows "Engine version" / "Staged skill bundles" | same version string on both | skew — the engine was re-staged but skills weren't, or vice versa |
| Snapshot generation + age | `doctor` "Snapshot" row, `status.snapshot` | age well under the ~1h hourly-host-maintain ceiling | older than ~1h (doctor gates at 48h) means the host's nightly/hourly job isn't publishing — read/draft is stale, not broken |
| Model cache | `doctor` "Model cache" row | dir present, non-empty, no dangling symlink, files >1MB | missing/empty/dangling ⇒ semantic search silently falls back to hash embeddings |
| Vendored deps ABI | `doctor` "Vendored deps ABI" row | vendor `cp*` tag matches the VM's own Python (`python3 --version`) | mismatch (e.g. vendor cp311, interpreter 3.10) ⇒ import will fail at runtime, not now; `_retired-*/` dirs are quarantined corpses, never live vendor — don't count them |
| Maintain heartbeat | `doctor` "Maintain heartbeat" row | `last_run` recent, keyed on real work completed | stale/missing ⇒ host-side `brain maintain` hasn't run — a VM symptom of a host problem, not a VM bug |
| Embedder liveness | `doctor` "Semantic embedder" row + `status.embedder` line | `real semantic embedder available (<backend>)` | `HashEmbedder` / "FALLING BACK" / `implicit-hash` ⇒ retrieval is silently lexical-only even though model files may be present on disk |

`doctor`'s own exit code already tells you PASS/FAIL for every *gating*
surface (`stale`/`unknown` only — `not-detectable` host-only rows never
gate). Never re-derive that judgment by eyeballing JSON; quote it.

## 4 — Hard rules

- **NEVER pip-install, upgrade, or hand-repair anything inside the VM** —
  no `pip install`, no manually copying model files, no editing vendor
  wheels. The VM is read+draft only (AGENTS.md §6); it cannot fix a STALE
  row, only report it.
- **The fix for any STALE row is always the same sentence**: "the owner
  re-runs the host installer (`tools/cowork_workspace_install.sh`) and
  re-syncs this workspace." Say that verbatim rather than improvising a
  workaround.
- **Never claim a verb is host-only without checking the actual error
  first.** Read the error and name it: `command not found` means
  `brain` is not on this session's `PATH`, which on a post-bootstrap workspace
  is the NORMAL state and says nothing about staging (§1); `OperationalError: unable to open
  database file` means the data is not in the sandbox and the desk is the
  route; `refused on role=vm` is the CLI genuinely restricting the verb.
  Quote whichever you actually got. Inferring an architecture cause from a
  guess is this skill's motivating failure, and all three of those errors
  have been read as it.
- **A diagnosis never writes anything but its own report text** — no
  `draft-capture`, no file edits, no touching `.brain/` beyond reading it.

## 5 — Report format

End with exactly one screen:

```
VERSION-READY: yes|no
- brain --version: <verdict, one line>
- brain doctor: <PASS|FAIL, gating-stale count>
- brain status: <verdict, one line — embedder + snapshot age>

Non-CURRENT rows (verbatim):
<quote each offending doctor row's "detail" field exactly as returned — never paraphrase>
```

If every probe is clean, still show all three lines — a terse "all good"
without the per-probe breakdown is not this skill's contract.

## Not this skill

Host-side trend/health-history across every registered vault (week-over-week
deltas, both scheduled-task heartbeats, hot-queue tail) is `/brainiac-health`
— that runs on the macOS/Windows host broker, not in Cowork, and reads
registry state this VM leg never has access to.
