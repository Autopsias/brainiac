---
name: vm-doctor
description: "In-Cowork (VM-leg) health and version diagnosis for a Brainiac workspace: bootstraps the session PATH/PYTHONPATH if the engine isn't wired up yet, then runs the three VM-allowed read-only probes (`brain --version`, `brain doctor`, `brain status --json`) and reports one VERSION-READY verdict with a per-surface breakdown. Triggers: \"is the brain healthy\", \"which version is running\", \"run a diagnosis\", \"vm doctor\", \"check the engine\", \"is semantic search working\", \"is cowork version-ready\", \"brain doctor\" (when the command isn't on PATH), \"is the vault stale in cowork\". NOT for host-side trend/health-history readouts across every registered vault — that's the host's `/brainiac-health`; this skill is the in-VM, single-workspace lane, read+draft only, and never installs, upgrades, or troubleshoots vendor/model files itself."
---

# vm-doctor (Cowork VM-leg health check)

**Motivating failure (2026-07-20):** a Cowork session was asked to run
`brain doctor`. The staged engine wasn't on PATH yet (session bootstrap not
loaded), and instead of bootstrapping, the agent invented a wrong claim
("doctor is a host-only verb") and gave up. `doctor` **is** in the CLI's
`VM_ALLOWED` list (`src/brain/cli.py`, `run_doctor_vm`) — it is a full
VM-leg health table, read-only, safe to run any number of times. This
skill's job: make the diagnosis deterministic so no future session
rationalizes "command not found" into a wrong architecture claim.

## Checklist

1. Bootstrap the shell (skip if `brain --version` already resolves)
2. Run the three probes in order
3. Read the interpretation table below against each probe's output
4. Apply the hard rules (never troubleshoot the fix yourself)
5. Emit the one-screen report

## 1 — Bootstrap (filesystem persists across a Cowork session; shell env does not)

Run this once at the start of any session that hasn't already exported it —
`command -v brain` failing, or `brain: command not found`, is the tell:

```bash
export BRAIN_VAULT="$PWD/vault"
export BRAIN_ROLE=vm
export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"
export PYTHONPATH="$BRAIN_RUNTIME_DIR/engine:$BRAIN_RUNTIME_DIR/vendor/$(uname -m):$PYTHONPATH"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
```

A "command not found" after this still fails means the workspace was never
staged (see Hard rules) — it does NOT mean the verb is host-only. Check
`--help`/the error text before naming an architecture cause.

## 2 — The three probes, in order

```bash
brain --version
brain doctor            # VM-allowed, read-only; exits non-zero if any gating surface is stale
brain status --json
```

Run all three even if the first two look clean — `status` carries the
embedder and snapshot detail `doctor` summarizes but doesn't fully quote.

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
  first.** `command not found` means bootstrap wasn't loaded (§1), not that
  the verb is restricted. If genuinely refused, the CLI says so explicitly
  (`refused on role=vm`) — quote that message, don't infer the reason.
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
