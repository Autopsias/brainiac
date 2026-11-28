---
name: vm-doctor
description: "In-Cowork (VM-leg) health and desk-MCP diagnosis for a Brainiac workspace: checks desk MCP tool responsiveness (`alerts`, `vault_languages`, `exceptions`) and workspace config first, then inspects local staging ONLY when on-disk staged artifacts (`.brain/snapshot/`, `.brain/published/`, or `.brain/bin/`) are present (never using `PATH` as the mode gate), and reports one clear health verdict. Triggers: \"is the brain healthy\", \"which version is running\", \"run a diagnosis\", \"vm doctor\", \"check the engine\", \"is semantic search working\", \"is cowork version-ready\", \"brain doctor\", \"is the vault stale in cowork\", \"is the engine staged in this workspace\". NOT for host-side trend/health-history readouts across every registered vault — that's the host's `/brainiac-health`; this skill is the in-VM, single-workspace lane, read+draft only, and never installs, upgrades, or troubleshoots vendor/model files itself."
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
| `supersede`, `unsupersede` (owner-declared; a pair the vault does not relate is PROPOSED to the owner, not applied) | `supersede`, `unsupersede` |

Those seventeen names are the whole desk, and they are the only route to note
CONTENT this session may use. **"Not on the desk" is not the same as "refused
here"**, and conflating the two is how a session talks itself into a workaround:
ten of the CLI's 23 VM-allowed verbs have no desk tool (`brief`,
`check-egress`, `cos-propose`, `digest`, `doctor`, `draft-capture`, `init`,
`mcp-config`, `provision-request`, `status`), and four desk tools (`inbox`,
`supersede`, `unsupersede`, `vault_languages`) are not VM-allowed on the CLI
(`brain.cli.VM_ALLOWED` is the list of record). Of those ten, only
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

**Motivating failure (2026-07-20, updated for desk-MCP workspaces):** An early
Cowork session saw `brain: command not found` and invented a wrong claim
("doctor is a host-only verb"). Later, once workspaces cut over to the host-run
`brain-mcp` desk broker and moved the vault off the mount, using `PATH` lookup
as a mode gate broke both ways: it failed on healthy desk-wired workspaces and
mislabeled staged workspaces whose engine is intentionally off `PATH`. This
skill checks the **desk MCP tools first** (the live transport for every Cowork
workspace) and detects local staging from **on-disk staged artifacts** under
`.brain/` (never from `PATH`).

## Checklist

1. Decide which lane you are in (§1) — in Cowork, bootstrap nothing
2. Check **desk MCP responsiveness** and workspace contract (§2)
3. Check whether **on-disk local staging artifacts** (`.brain/snapshot/`,
   `.brain/published/`, or `.brain/bin/`) are present; inspect local staging
   ONLY if present (§3)
4. Read the interpretation table against whatever ran (§4)
5. Apply the hard rules (never troubleshoot or repair inside the VM) (§5)
6. Emit the one-screen report (§6)

## 1 — Which lane you are in (decide this first)

**Cowork (VM leg): start with the desk, bootstrap nothing.** Every Cowork
workspace reaches the vault through the host-run `brain-mcp` desk broker. A
relocated desk-wired workspace has NO local vault snapshot and NO `brain` on
`PATH` — that is the **healthy, expected state**, not a fault. Never add a
bootstrap block (`export BRAIN_VAULT="$PWD/vault"`, `BRAIN_RUNTIME_DIR`, or a
`PATH` prepend) and never copy one out of an older bundle.

**Host (the owner's Mac): run the CLI probes directly.** `brain` is already on
`PATH`; run `brain --version`, `brain doctor --vault <dir>`, and `brain status
--json --vault <dir>` directly (dropping `--role vm`).

## 2 — Desk MCP & workspace checks (Cowork primary path)

Run these **three bodyless desk MCP tool calls** first. None of them returns
note content, all three are served by the 17-tool `brain-mcp` broker
(`src/brain/mcp_adapter.py`), and together they prove the host broker is alive,
bound to the vault, and reporting health:

1. **`alerts`** — returns the host degradation digest (current findings, owner
   inbox count, engine feedback backlog, update and synthesis heartbeats). A
   clean response proves the desk broker is responsive and reading host state.
2. **`vault_languages`** — returns the derived language census from the live
   host index. A non-error response proves the broker can read the vault index.
3. **`exceptions`** (or its alias `inbox`) — returns pending owner-actionable
   items.

Also verify the **workspace contract** (metadata only — never open notes under
`brain/` or `raw/`):
- Confirm the workspace-root `CLAUDE.md` carries the inlined `BRAIN-CONTRACT`
  block (the session itself can answer `contract?` → `[brain contract loaded]
  [contract inlined]`).

**What the desk does NOT serve (`src/brain/mcp_adapter.py:149-175`):** `doctor`
and `status` are local CLI verbs, never registered as MCP tools. On a relocated
desk workspace with no local snapshot, `doctor` and `status` cannot be called
over the desk — do **not** treat their absence as a failure. If the owner needs
host-side engine/index version details, report that the owner can run `brain
doctor` on the host.

**If the desk tools are absent or fail to respond:** report `DESK-READY: no`
with the exact tool error (e.g. tool not announced by Claude Desktop, or stdio
broker error). Do **not** fall back to shelling out for note content or hunting
for a local binary.

## 3 — Local staging inspection (ONLY when on-disk staged artifacts are present)

Detect whether this workspace is **desk-only** or **staged** strictly from
**on-disk staged artifacts** under the workspace — **never** from whether
`brain` is on `PATH`. Because this skill forbids exporting a staged directory
onto `PATH` and the workspace installer never writes to a system `PATH`
location, a staged workspace's engine is off `PATH` by design; gating mode on
`PATH` would mislabel a staged workspace as desk-only.

Check for on-disk staging directories or snapshot files (metadata only — never
open notes under `brain/` or `raw/`):
- `<workspace>/.brain/snapshot/` (or `<workspace>/vault/.brain/snapshot/`)
- `<workspace>/.brain/published/` (or `<workspace>/vault/.brain/published/`)
- `<workspace>/.brain/bin/` (or `<workspace>/vault/.brain/bin/`)

- **If NONE of those on-disk staged artifacts exist:** treat the workspace as a
  **desk-MCP-only workspace**, skip the rest of §3, and report
  `Local staging: not-applicable (desk-only workspace; no on-disk staged snapshot or .brain/bin/ found)`.
  The desk MCP results from §2 (`alerts`, `vault_languages`, `exceptions`) are
  the complete diagnosis.
- **If on-disk staged artifacts ARE present (legacy pre-cutover staged
  workspace):** classify the workspace as **staged**, never desk-only.
  1. Inspect the on-disk staging metadata directly (artifact paths, snapshot
     file presence and mtime via `stat` / `ls -la` on `.brain/snapshot/`,
     `.brain/published/`, or `.brain/bin/` — never modifying `PATH` and never
     opening note files under `brain/` or `raw/`).
  2. If a `brain` executable is already invocable directly (without modifying
     `PATH` or bootstrapping), run the three read-only staging probes in order:

```bash
brain --version
brain --role vm doctor   # read-only; exits non-zero if any gating surface is stale
brain --role vm status --json
```

  If staged artifacts are present on disk but `brain` is not directly invocable
  without bootstrapping `PATH`, do **not** modify `PATH` and do **not** call
  the workspace desk-only: report the on-disk staging metadata (paths and
  snapshot mtimes) and note that full CLI probe output requires running
  `brain doctor` on the host.

**Keep `--role vm` on the last two in Cowork** (without it `config.role()`
defaults to `host`, whose egress cap is the whole vault). On the **host** lane
drop `--role vm` and pass `--vault <dir>` instead.

**These read STAGING metadata, never a note.** If `doctor` reports
`Snapshot (read-only, .brain/snapshot) ... not-detectable` or `status --json`
returns `"index": {"error": "OperationalError: unable to open database file"}`,
that is a CORRECT reading of a workspace with no local snapshot — quote it; it
is not a broken engine and not a reason to look for vault files.

## 4 — Interpretation table

### Desk MCP signals (all Cowork workspaces)

| Signal | Where | HEALTHY looks like | Otherwise means |
|---|---|---|---|
| Desk transport liveness | `alerts`, `vault_languages`, `exceptions` tool calls | All three return structured JSON without transport error | Broker down, not configured in Claude Desktop, or `.mcpb` / `claude_desktop_config.json` mismatch — owner runs `brain doctor` on host |
| Degradation digest | `alerts` output | Zero critical alerts; host heartbeats present | Active host degradation (stale maintain/synthesis, findings, or backlog) — quote the exact alert lines |
| Live index reachability | `vault_languages` output | Language census returned with note counts | Host index unavailable or unbuilt — owner runs `brain doctor` / `brain sync` on host |
| Workspace contract | `CLAUDE.md` `BRAIN-CONTRACT` block | Inlined contract present | Workspace instructions drifted — owner re-runs workspace setup |

### Local staging signals (ONLY when §3 ran)

| Signal | Where | CURRENT looks like | Otherwise means |
|---|---|---|---|
| Engine stamp vs skill bundles | `doctor` rows "Engine version" / "Staged skill bundles" | same version string on both | skew — the engine was re-staged but skills weren't, or vice versa |
| Snapshot generation + age | `doctor` "Snapshot" row, `status.snapshot` | age well under the ~1h hourly-host-maintain ceiling | older than ~1h (doctor gates at 48h) means the host's nightly/hourly job isn't publishing — local staging is stale |
| Model cache | `doctor` "Model cache" row | dir present, non-empty, no dangling symlink, files >1MB | missing/empty/dangling ⇒ local semantic search silently falls back to hash embeddings |
| Vendored deps ABI | `doctor` "Vendored deps ABI" row | vendor `cp*` tag matches the VM's own Python (`python3 --version`) | mismatch ⇒ import will fail at runtime; `_retired-*/` dirs are quarantined corpses, never live vendor |
| Maintain heartbeat | `doctor` "Maintain heartbeat" row | `last_run` recent, keyed on real work completed | stale/missing ⇒ host-side `brain maintain` hasn't run |
| Embedder liveness | `doctor` "Semantic embedder" row + `status.embedder` line | `real semantic embedder available (<backend>)` | `HashEmbedder` / "FALLING BACK" / `implicit-hash` ⇒ lexical-only fallback |

When §3 runs, `doctor`'s own exit code already tells you PASS/FAIL for every
*gating* surface (`stale`/`unknown` only — `not-detectable` host-only rows
never gate). Never re-derive that judgment by eyeballing JSON; quote it.

## 5 — Hard rules

- **NEVER pip-install, upgrade, or hand-repair anything inside the VM** —
  no `pip install`, no manually copying model files, no editing vendor
  wheels. The VM is read+draft only (AGENTS.md §6); it cannot fix a STALE
  row or restart the host broker, only report it.
- **The fix for any STALE staging row is always the same sentence**: "the owner
  re-runs the host installer (`tools/cowork_workspace_install.sh`) and
  re-syncs this workspace." For any desk-broker error: "the owner runs `brain
  doctor` on the host to verify `brain-mcp` registration and vault health."
- **Never claim a verb is host-only without checking the actual error first.**
  Read the error and name it: `command not found` means `brain` is not on this
  session's `PATH` (normal on a desk-wired workspace); `OperationalError:
  unable to open database file` means no local snapshot is in the sandbox and
  the desk is the route; `refused on role=vm` is the CLI genuinely restricting
  the verb; "tool not available" means the `brain-mcp` server is not connected
  in Claude Desktop. Quote whichever you actually got.
- **A diagnosis never writes anything but its own report text** — no
  `capture`, no `draft-capture`, no file edits, no touching `.brain/` beyond
  reading it.

## 6 — Report format

End with exactly one screen:

```
DESK-READY: yes|no
- desk alerts: <verdict, one line — alert count or exact error>
- desk vault_languages: <verdict, one line — census summary or exact error>
- desk exceptions: <verdict, one line — pending count or exact error>
- workspace contract: <present|missing>

LOCAL STAGING (detected from on-disk .brain/snapshot/, .brain/published/, or .brain/bin/ — never from PATH):
- mode: desk-only (no on-disk staged artifacts) | staged (on-disk artifacts present)
- status: not-applicable (desk-only workspace) | VERSION-READY: yes|no | staged-off-PATH (on-disk metadata inspected; host `brain doctor` needed for full probe)
  [if §3 found on-disk staged artifacts, include:]
  - on-disk artifacts: <paths and snapshot/published mtimes>
  - brain --version: <verdict, one line, or not-invoked (off-PATH)>
  - brain doctor: <PASS|FAIL, gating-stale count, or not-invoked (off-PATH)>
  - brain status: <verdict, one line — embedder + snapshot age, or not-invoked (off-PATH)>
  - Non-CURRENT rows (verbatim): <quote each offending doctor row's "detail" field>
```

Always show all four desk lines — a terse "all good" without the per-probe
breakdown is not this skill's contract.

## Not this skill

Host-side trend/health-history across every registered vault (week-over-week
deltas, both scheduled-task heartbeats, hot-queue tail) is `/brainiac-health`
— that runs on the macOS/Windows host broker, not in Cowork, and reads
registry state this VM leg never has access to.
