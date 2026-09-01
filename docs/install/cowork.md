# Cowork — install and first run

> **Entry point / platform picker: [`./README.md`](./README.md)** — Cowork
> always requires the host install (Path A) first; see the picker if you
> haven't done that yet.

**Role:** `vm` (`BRAIN_ROLE=vm`) — **the `brain` CLI is read + draft only.**
No writes, no index rebuild, no signing key, no OS scheduler. This is the one
client whose capability set genuinely differs from the other two — read this
whole page before assuming Cowork can do what the host clients do. Full matrix:
`AGENTS.md` §6 (Host / VM trust split); the CLI-level guarantee (code + tests):
`docs/cowork-windows-install.md`.

> **VULN-3385 (2026-08) — MITIGATED 2026-08-30, the finding stays OPEN at
> reduced severity. This is NOT a closure.** Full record, tests and residual:
> `docs/security/vuln-3385-risk-reduction.md`; the register entry:
> `docs/security-acceptances.md` A-05.
>
> **What was wrong.** The guarantee above bound the `brain` COMMAND, not the
> SESSION. Cowork attached the vault folder itself to the VM, read-write, over
> VirtioFS — so the session could read any vault file with ordinary file
> tools, every tier, bypassing the classification gate entirely and leaving no
> record, and could write into `vault/` with no draft-approval path at all. A
> penetration test demonstrated exactly this: asked for a simple search, the
> agent skipped the CLI and read a Restricted note straight off disk.
>
> **What changed (Closed Stacks, 2026-08-27 → 2026-08-30).** The vault itself
> no longer sits on anything Cowork attaches. The registered workspace
> (`~/CoworkWorkspaces/<name>`) carries the staged engine, model cache, skills
> and routines — no vault tree, no published snapshot, no derived index, no
> note body recoverable by path, glob or content search (re-measured this
> session: `tests/test_direct_file_read_relocated.py` +
> `tests/test_cowork_staging_off_the_mount.py`, 55/55 passed). Every
> VAULT-NOTE verb (`search`, `hybrid-search`, `get`, `read`, `recent`,
> `bases-query`, `dossier`, `grep`, `graph-expand`, `diagnose`) now reaches the
> vault only through the host `brain-mcp` broker, which applies the same
> classification filter as the CLI and writes an audit record **before** it
> returns content — and now does so **fail-closed**: a call whose record
> cannot be written raises instead of handing back the note (re-measured this
> session: `tests/test_mcp_broker_seam.py` + `tests/test_mcp_tool_surface.py`,
> 83/83 passed). Before this plan, the adapter's own record-write path read
> `except Exception: pass  # never let logging fail a read` — a read whose
> record failed to write still succeeded, silently. That line is gone.
>
> **The 14 shipped Cowork skill bundles were also rewritten** (session s06b,
> DESK-07) to call the broker's tools instead of shelling `brain --role vm` on
> a mounted vault — released to the skill marketplace as version 0.20.32. A
> workspace only gets that text when it next refreshes from the marketplace;
> this repo cannot see whether any particular already-installed workspace has.
>
> **What did NOT change — the residual, stated plainly rather than implied
> away.** The broker's classification ceiling for the Cowork leg is the SAME
> full-vault (`MNPI`) default the host itself uses: `$BRAIN_MAX_EGRESS_TIER`
> is SET to `MNPI` on every registered entry — not unset, which is what this
> paragraph said until it was corrected on 2026-08-30; the register and the
> record page had it right and this page did not. Re-measured against the live
> Desktop config: **4** `brain-mcp` entries (one for this repo's own vault
> and three client-named ones, not reproduced here — the client-name gate
> refuses them), all four `MNPI`, none setting `BRAIN_ROLE`. The
> first probe this session ran reported 2 and inspected 2; the conclusion
> survived the recount, the completeness claim did not. `connect.py`'s Desktop-config builder still never writes
> `BRAIN_ROLE` at all — a finding s06b raised and no session in this plan
> closed — so nothing there could tell a Cowork caller apart from a host one
> even if a ceiling existed. No per-caller tier clamp is configured. So **a
> Cowork session can still retrieve the same Restricted-tier note the
> penetration test read** — through the sanctioned tool, filtered at scope and
> logged, where before it was an invisible bypass. That is a mitigation, not a
> fix, which is why this finding stays open rather than closing.
>
> **One more asymmetry, narrower but real — CLOSED 2026-08-31.** The
> fail-closed record write above (`except Exception: pass` removed) fixed the
> MCP broker's own path (`mcp_mediation.py`) and left the CLI's OWN
> record-write swallow untouched, so a read run through the CLI directly could
> still succeed unrecorded if the write failed. That was close to moot for a
> genuinely new Cowork workspace (no local vault data left to read — a CLI verb
> against it exits 3, `tests/test_desk_fails_closed.py`, 13/13 passed) but it
> applied to the host's own shell and to any workspace staged before the
> cutover. The CLI leg now fails closed too: `brain.cli_read_record` holds
> gated output until the SEC-06 record is written, and exits `5` with the
> result withheld when it cannot be. Probed both ways
> (`tests/test_cli_read_record_fails_closed.py`): a forced record failure
> withholds the notes and leaves no row, while `BRAIN_READ_LOG=0`, a verb that
> gates nothing, and an empty result set all still answer normally.
>
> **The original-document path was narrowed, not shipped as first designed.**
> An earlier design (session s05b) would have let a Cowork session request one
> archived original file, staged into a directory bound to that session alone
> behind a lease with an enforced reaper. It was retired by owner ruling
> (2026-08-27): Claude Desktop does not forward a per-session identity the
> broker can trust, so there was no mechanism to bind a staging directory to
> one caller — an unguessable name is not access control. What ships instead:
> `brain authorize-original` is **HOST-ONLY**, refused for `role=vm` before the
> vault even opens, and only RECORDS a disclosure decision (tier + timestamp;
> per the read-log's own contract it never learns *which* document). It hands
> back no bytes. Actual delivery, when it is warranted, is the pre-existing
> host-operator command `brain project --dest <dir> --max-tier <tier>`, run by
> a person outside any Cowork session. **No Cowork session obtains an original
> document through any lease — that path does not exist**, and no page should
> imply otherwise.
>
> **The position this page used to argue, and why it is retired.** Until
> 2026-08-30 this box did not merely record the finding — it ARGUED a stance,
> and a reader who finds only the new text would not know the old one was
> considered. It said the bypass was "a documented limit, not a defect to be
> patched at the CLI", because the gate is **an egress decision, not
> containment**, and "the model will cooperate" is not a control. It said a
> read-only mount is not a mitigation — true then and true now: read-only binds
> Claude's own file tools, never shell commands, and never blocked reads at all.
> And it named **`brain project --dest <dir> --max-tier <tier>`** as the real
> containment for sensitive tiers: attach a filtered COPY, and the excluded
> documents are physically absent from the VM.
>
> That stance is reversed, and the reason is narrow. It was correct while the
> vault sat on the mount, because then the only lever was WHICH documents to
> expose — a classification problem, which is why it resolved to a
> tier-filtered copy. The vault is not on the mount at all now, so containment
> stopped being a classification problem and became a location one. Nothing is
> excluded, because nothing is there.
>
> **Why `brain project` was not made the default,** which is the whole
> justification for the shape chosen: the owner declined it on the ground that
> users cannot classify reliably — a filtered copy is only as good as the
> labels it filters on — and that the product's value depends on full-corpus
> reach. A default that silently narrows what the assistant can see fails
> quietly and in the direction of looking like it works. `brain project`
> remains available and remains correct for a deliberately scoped workspace;
> it is a host-operator choice, not the posture.
>
> **Two residual controls the old text named, still live and still relevant.**
> The egress ceiling is a host-SIGNED owner decision, not an ambient env var a
> process can set for itself (`brain vm-egress-tier`, VULN-3386); and ingested
> source material is scanned for concealed instructions before it can be
> indexed (`brain integrity --injection`, SEC-05). Neither closes VULN-3385;
> both reduce what an attacker does with what they reach.
>
> **Two things this plan does not touch, for completeness.** The host Claude
> Desktop / Claude Code surface is unchanged — this is a Cowork-VM finding
> only — and what a session legitimately does with a document *after* a
> correct disclosure (copy it, quote it, forward it) is untouched by any
> control named here.

## Quickstart — the whole thing in 6 steps (plain language)

Cowork can't install anything itself — its VM only sees the folder you give
it. So the engine is staged **from your computer first**, then Cowork just
opens the folder. The one command that does the staging (plus the nightly
task, the registry rows and the Claude Desktop MCP entry) in a single pass:

```bash quickstart-wire
# On the HOST (Mac/Windows), from a terminal with `brain` on PATH:
brain provision-local <vault> --workspace <workspace> --model-dir <model>
```

`<vault>` is where the notes live, `<workspace>` is the folder Cowork
attaches, and `<model>` is an existing `bge-m3-int8` snapshot to stage from —
omit `--model-dir` once a first vault on this machine has already staged one
(`provision-local` finds it automatically); on a brand-new machine with none
staged yet, get one with `python3 packaging/stage_model.py --repo
Xenova/bge-m3 --out /tmp/bge-m3-int8 --patterns "onnx/model_int8.onnx"
"tokenizer.json" "tokenizer_config.json" "special_tokens_map.json"
"config.json"` first and pass that path.

The one manual step it cannot take for you, printed in its report when
needed:

```bash
# only when provision-local's report carries a `reload` line — run it yourself:
launchctl bootout gui/$UID/<label> 2>/dev/null; launchctl bootstrap gui/$UID <plist> && launchctl print gui/$UID/<label> | grep -c SWEEP
```

That reload merges a changed nightly-task plist into a job launchd already
has loaded — an unattended process cannot reload another job into the login
domain. `bootstrap` alone can answer `Load failed: 5` on a job that is
registered but stopped, which is why the line runs `bootout` first.

In order:

1. **On your computer, in Claude Code** (any directory, with the
   `brainiac-manager` plugin installed — see [`ai-install.md`](./ai-install.md)):
   run **`/brainiac-cowork-setup`**. It asks which folder will be your Cowork
   workspace, then runs the `brain provision-local` command above under the
   hood — staging everything into it (engine, search model, read-only
   snapshot, session prompt), registering the nightly maintenance task, and
   wiring the registry rows and the Claude Desktop MCP entry. It ends by
   printing the exact things to do in Cowork. Prefer the raw command over the
   skill? It's the same block, quoted above.
2. **In Claude Desktop**: open **Cowork** and add that same folder as the
   project folder.
3. **Install the skills** (one-time): step 1 already staged current-version
   `.skill` bundles at `<workspace>/vault/.brain/skills/` — upload them via
   Cowork's **Save-skill** flow (kernel first; extras optional, §2). Prefer
   the Plugins tab instead? **Customize → Plugins →** add
   `Autopsias/brainiac` → install **Brainiac — Kernel Skills** (and
   **Extras**) works too and is documented as an optional alternative (§2) —
   just don't install Brainiac Manager in Cowork either way; its skills
   mutate the host and are useless in the VM.
4. **Paste the session prompt**: `/brainiac-cowork-setup` printed the full
   contents of the staged `cowork-session-prompt.md` — paste it into the
   project's instructions (or the first message of each session). Cowork does
   not read AGENTS.md on its own; this prompt is how the agent learns the
   brain exists (§1).
5. **Use it**: in a Cowork session the agent calls the host `brain-mcp`
   broker's tools — search, get, recent, and draft-capture for new notes. The
   vault itself is not on the mount (§VULN-3385 above), so every read goes
   through the broker, filtered and logged; a draft it captures lands in the
   shared folder, your computer's nightly task signs and indexes it, and the
   next snapshot publish makes it searchable in Cowork (see "Remember" below).
6. **Updating later**: on your computer run `/plugin marketplace update` then
   **`/brainiac-update`** — it re-stages every registered workspace, so the
   Cowork folder gets the new engine/prompt/skills automatically.

Everything below is the detail behind those steps.

## 0 — Before you open Cowork: install the runtime into the workspace (HOST side)

Cowork's Linux VM sandbox mounts only the workspace folder, so the brain
ships **into the workspace** from a HOST machine before the Cowork session
starts. Nothing is installed in the VM: the engine is pure Python and runs
straight from the staged source copy via the VM's own `python3` (a `brain`
shim in `.brain/` wraps `python3 -m brain.cli`). No Docker, no compilers.

**Easiest:** if you already have the `brainiac-manager` plugin (see
[`ai-install.md`](./ai-install.md)), just run `/brainiac-cowork-setup` and
answer which folder is your workspace — it does everything below in one
shot and prints the exact instructions to finish in Cowork.

Doing it by hand instead:

```bash
# On the HOST (Mac/Windows), from a clone of this repo:
cd brainiac
python3 packaging/stage_model.py --repo Xenova/bge-m3 --out /tmp/bge-m3-int8 \
  --patterns "onnx/model_int8.onnx" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "config.json"
tools/cowork_workspace_install.sh <vault> /tmp/bge-m3-int8 dist <workspace>
```

**Pass all four arguments.** `<vault>` is the directory that holds `brain/`
and `raw/` — wherever it lives. `<workspace>` is the folder Cowork
ATTACHES. They used to be the same place, one level apart, and this
document showed the two-argument form that derived the second from the
first. Closed Stacks s07 moved the vault OFF the mount to close
VULN-3385, and the installer's refusal — the one that stops a snapshot of
every note body being published back into the attached folder — can only
run when it is told both places. Omit the **fourth argument** on a
relocated vault and the refusal never fires.

(Frozen Linux ELFs via `tools/build_brain_binary.sh` remain an optional
fallback for locked-down VMs without `python3` — most users never need
them.)

This one script lands the **full** operational layer in one pass, not just
the engine:

```
<workspace>/vault/.brain/
├── engine/          staged pure-Python engine source (what the `brain` shim runs)
├── vendor/<arch>/   per-architecture semantic deps (onnxruntime, tokenizers, ...)
├── bin/             per-arch frozen Linux ELFs — OPTIONAL fallback for VMs without python3
├── model/           bundled bge-m3-int8 ONNX model (no HF fetch needed in the VM)
├── snapshot/        read-only index.snapshot.sqlite + manifest
├── skills/          the 10 .skill bundles, REBUILT at the current version every run
└── routines/        routines/manifest.json + the Cowork registrar paste-prompt + a brain-init report
```

...and it also runs `brain init --full` as the VM client (`BRAIN_ROLE=vm`),
so the `overlay/` layer is scaffolded and validated with **no host
mutation** before you ever open the Cowork session.

**One command, both artifacts current (cw-02).** Every run of this script
rebuilds `dist/cowork-skills/*.skill` from the checkout's current version —
never just "if the directory happens to be empty" — and aborts if the
freshly-staged skill bundle's version doesn't match the freshly-staged
engine's version stamp. So re-running this one script after a
`git pull`/version bump refreshes the engine **and** the skills together;
there is no separate "now go reinstall the skills" step. `brain doctor`
(below) reports the staged skill-bundle version per workspace, so a stale
Cowork skill set shows up as a visible `⚠️`, not a silent gap.

Idempotent — re-run it any time to refresh binaries, skills, and republish
the snapshot. The Markdown vault is always the source of truth; `.brain/` is
always rebuildable from it.

## 1 — Teach the agent (project instructions) + per-session bootstrap

Cowork **auto-loads a workspace-root `CLAUDE.md`** at session start (same
loader as Claude Code, but without `@import` expansion — so the installer
stages the full conventions contract **inlined** there). Verify anytime by
sending the message `contract?` — a healthy session answers
`[brain contract loaded] [contract inlined]`.

If the probe gets no markers (older Cowork build, or a workspace staged
before this change), use the **fallback channel**: the installer also stages
`<workspace>/vault/.brain/routines/cowork-session-prompt.md` — a prompt
block that bootstraps the env AND points the agent at the contract. Put it
in the Claude Desktop project's custom instructions (once per project) or
paste it as the first message of each session. Source doc:
`docs/install/cowork-session-prompt.md`.

**The broker layout, in one paragraph.** The vault itself is not on the
Cowork mount — only the staged engine, model cache, skills and routines are
(§VULN-3385 box above). So there is no local `brain` binary and no local
snapshot for a Cowork session to read against on a relocated (post-cutover)
workspace: every vault-note verb (search, get, recent, dossier, grep,
graph-expand, draft-capture, …) reaches the vault only through the **host
`brain-mcp` broker**, which applies the same classification filter as the
CLI and writes an audit record before it returns content. Readiness is: the
broker's tools answer with real vault content — not a printed snapshot
generation, which is what the old co-located bootstrap checked and what a
relocated workspace no longer has to show. The full session prompt (what to
tell the agent, and how to verify the broker is answering) is
`docs/install/cowork-session-prompt.md`, staged into every workspace at
`<workspace>/vault/.brain/routines/cowork-session-prompt.md`.

## 2 — Get the skills into Cowork: one host command (DEFAULT), Plugins tab (OPTIONAL)

**Default: the one-command host path (cw-01/cw-02).** Step 0 above already
staged current-version `.skill` bundles at `<workspace>/vault/.brain/skills/`
— this is the path the host can fully guarantee and re-verify on every run
(ADR-0005 Ruling 4 + the s04 empirical addendum: the Claude Desktop / Cowork
plugin store has **no supported CLI, config, or import** a host script can
drive, so a staged filesystem artifact is the only thing a host command can
promise stays current). Upload them via Cowork's Save-skill flow directly, or
have `/brainiac-cowork-setup` walk you through it (its final report prints
the exact upload order). **Order: kernel first, extras optional** (mirrors
the Claude Code marketplace split in `docs/operations/cutover-s08-evidence.md`):

```
kernel:  kb-curator.skill  promote.skill  vault-ingestion.skill  vault-eval.skill  save-conversation.skill  overlay-style.skill
extras:  curation.skill  improve.skill  task-registrar.skill  autoresearch.skill
```

**Optional: Plugins tab, from inside a live Cowork session.**
`Autopsias/brainiac` is public (flipped 2026-07-04), so Cowork's Customize →
Plugins tab can sync the marketplace directly — verified live the same day
(all three plugins visible: "Brainiac Manager — host lifecycle", "Brainiac
— Kernel Skills", "Brainiac — Extras"). In Cowork: Customize → Plugins →
add marketplace `Autopsias/brainiac` → install `brainiac-kernel` (and
`brainiac-extras` if wanted); `/plugin marketplace update` inside that
session then keeps it current — see the
[ADR-0002 addendum](../adr/0002-cowork-plugin-skill-delivery.md). This is a
genuinely convenient path **when you're already in the session**, but it's a
click-through a human drives, not something `tools/cowork_workspace_install.sh`
can trigger or verify from the host — that's exactly why it stays optional
rather than becoming what a Cowork update depends on. Use it if you like the
one-time setup; skip it and the staged zips above are still enough on their
own, every time.

**Keeping the Plugins-tab install current later:** if you did use this path,
`brain doctor` on the host can tell you when it drifts (`Desktop/Cowork
plugin store (<plugin>)` rows go `manual-required` with an installed-vs-SSOT
detail) but it cannot fix it — the Desktop store has no host-reachable CLI.
Refreshing it is a **Cowork-session-only** step: `/brainiac-update`'s "Cowork
skill refresh" section drives `/skill-creator` to repackage the stale
skill(s) and present them for "Save and Replace", then re-checks `brain
doctor` afterward to confirm the click actually took (Cowork's "Save and
Replace" is known to silently no-op sometimes — Anthropic #46844/#46836 —
so never trust the click alone).

## 3 — Register the on-invoke Cowork triggers (paste-ready prompt)

Paste `docs/operations/cowork-task-registrar-prompt.md` (or the file written
by `--save-cowork-prompt`, or the one already staged at
`<workspace>/vault/.brain/routines/`) into a Cowork chat session that has the
scheduled-tasks MCP tools. It registers exactly **3 poke-only triggers** —
`brain-promotion-scan`, `brain-autoresearch-cascade`,
`brain-ingestion-digest-weekly` — following a strict list → create-if-absent
/ update-if-present → **never delete** sequence, and never sets a cron
expression on any of them:

```bash
# Regenerate the prompt fresh at any time (read-only, no mutation):
cd profile-a-brain
python3 scripts/register_tasks.py --dry-run --client cowork --save-cowork-prompt /tmp/cowork-prompt.txt
```

**Verify after pasting:** re-run `list_scheduled_tasks` and confirm all 3
triggers appear with no schedule expression (heed the
[#29022](https://github.com/anthropics/claude-code/issues/29022) caveat —
`create_scheduled_task` sometimes silently no-ops; fall back to the Cowork
Schedule UI if a trigger didn't take). Confirm the VM OS-scheduled count
stays at **0** — that is `routines/manifest.json`'s locked
budget (`locked_counts.vm_os_scheduled`), and this registration must never
add a cron entry on the VM side.

## Remember: Cowork is read+draft — its tasks are brief/digest, not host maintain

This is the single most important thing to internalize about the Cowork
client, and the reason its onboarding differs structurally from Claude Code
CLI / Codex:

- **No `brain-nightly` here.** The one sanctioned OS-scheduled task lives
  only on the **host** (`launchd` / Task Scheduler) — the VM has no
  scheduler and registers nothing autonomous, ever.
- **The 3 Cowork triggers are poke-only** (fired by a human, by name, via
  the Cowork "run now" equivalent) — convenience aliases for retyping a
  prompt, not cron entries.
- **Verbs available to `BRAIN_ROLE=vm`:** `search`, `hybrid-search`, `grep`,
  `bases-query`, `graph-expand`, `get`, `read`, `recent`, `status`,
  `draft-capture`, `capture`, `brief`, `digest`, `init` — every host-broker
  write/maintenance verb (`write`, `rebuild`, `sync`, `snapshot`, `project`,
  `verify-audit`, `anchor`, `verify-anchor`, `backup`, `restore`) is refused
  **before** the index is even opened (`{"error": "role_forbidden"}`, exit
  4) — this is enforced in code, not just documented, and proven by
  `tests/test_integration.py`.
- **Captures are unsigned DRAFTS.** `brain draft-capture` stages a draft in
  `capture-inbox/`; it is drained + Ed25519-signed only by the next **host**
  `brain sync` (which the host's `brain-nightly` task already runs daily).
  There is no capture daemon and no VM-side signing key — ever.
- **Snapshot staleness is visible, not silent.** `brain status` on the VM
  reports the snapshot's generation and age, so a session can tell whether
  its read-only view is fresh or waiting on the next host drain.

## Verify

On a **relocated (post-cutover) workspace**, verify from inside the Cowork
session by asking the agent to run a broker search/get/recent and checking
the result carries real vault content and an egress/classification block —
there is no local `brain` on PATH to check directly (see the broker-layout
paragraph in §1). On the **host**, or on a workspace that has not yet moved
off the mount, the CLI checks still apply:

```bash
brain status                          # snapshot gen/age, pending drafts
brain search "<something>" --json     # egress-gated read
brain write foo.md                    # MUST fail: role_forbidden, exit 4
```

On the **host**, `brain doctor` reports the staged engine AND staged
skill-bundle version for every registered Cowork workspace — a stale skill
set after a version bump shows up as `⚠️ stale`, telling you to re-run
`tools/cowork_workspace_install.sh` (or `/brainiac-update`) rather than
staying silently out of date.

## Cross-references

- `docs/adr/0005-update-versioning-ux.md` Ruling 4 + s04 addendum — why the
  staged `.skill` path is canonical/host-guaranteed and the Desktop Plugins
  tab is documented as optional (empirical: the Desktop/Cowork plugin store
  has no scriptable host-side CLI/config/import)
- `docs/cowork-windows-install.md` — the full Cowork build/runtime spec (5 load-bearing rules, the capture loop, the install/refresh commands)
- `AGENTS.md` §6 — full access matrix (all clients)
- `docs/operations/cowork-task-registrar-prompt.md` — the exact paste-ready prompt
- `routines/manifest.json` — THE LOCK, `locked_counts` (1 host, 0 VM)
- `.claude/skills/brainiac-cowork-setup/SKILL.md` — the guided walkthrough of steps 0–2 above (`setup-cowork` retired 2026-08-30, this page is its replacement)
- `.claude/skills/task-registrar/SKILL.md` — the registrar that generates the paste-prompt
