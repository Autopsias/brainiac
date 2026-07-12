# LLM-INSTALL.md — Brainiac install runbook for AI assistants

> **You are an AI assistant with shell access, and a human asked you to
> install Brainiac.** This file is your complete instruction set. Follow it
> top to bottom. Do not improvise alternative install methods; do not skip
> verification gates. Everything here is verified against the shipped code.

**Prime directive: detect, don't ask.** You may ask the human ONLY the
questions in §Q (four, and most runs need just one or two). Every other fact
you need — OS, shell, whether you're sandboxed, network, an existing install
— you can and must detect yourself.

---

## Q · The ONLY questions you may ask (verbatim, when their trigger hits)

| # | Trigger | Question |
|---|---|---|
| Q1 | always (unless already told) | "Where should your brain live? Give me a folder — I'll use `<folder>/vault`. (Default: `~/brainiac-workspace`)" |
| Q2 | the chosen vault folder already contains notes | "That folder already has notes. Index them as-is, or import them properly through the ingestion pipeline (signed, classified)? [index-as-is / import / fresh-folder]" |
| Q3 | after verification passes | "Do you also use Claude Desktop's Cowork (the Linux-VM sandbox mode) and want this brain available there? [yes/no]" |
| Q4 | after verification passes | "Semantic search needs a one-time ~465 MB model download. Download now, or later on first use? [now/later]" |

Bundle Q3+Q4 into one message. Everything else: decide from detection and
tell the human what you decided in one line each.

## 0 · Detect your execution context (before ANY install command)

Run these probes and branch:

```bash
uname -s 2>/dev/null || echo Windows      # OS
echo $HOME; whoami                        # real user?
```

- **You are sandboxed** (Cowork, a cloud agent, a container — anything where
  commands do NOT run on the human's own machine): **STOP. Do not install
  anything** — it would be wiped and never reach their PATH. Print the §1
  commands as a copy-paste block for the human's own terminal, then guide
  them and verify from their pasted output. (Cowork users: after the host
  install is done by a host-side assistant, the Cowork leg is set up with
  `/brainiac-cowork-setup` — see `cowork.md`.)
- **You are Claude Code running on the host:** prefer the plugin path (§2) —
  it bundles lifecycle management. The script path (§1) also works.
- **Any other assistant with real host shell access** (Codex, Gemini CLI,
  etc.): use the script path (§1).
- **Already installed?** `command -v brain && brain doctor --json` — if
  healthy, skip to §4 (vault setup) or §7 (add another vault).

## 1 · Install the engine — script path (macOS / Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh
bash /tmp/brainiac-install.sh
```

Installs `brainiac-cli[mcp]` from PyPI trying `uv tool install` → `pipx` →
`pip install --user` (first success wins; it reports the channel). **No model
download happens now** — install never blocks on the network beyond PyPI.

**Windows (PowerShell, no WSL):**

```powershell
irm https://raw.githubusercontent.com/Autopsias/brainiac/main/install.ps1 -OutFile install.ps1
.\install.ps1
```

If it reports a PATH update, tell the human to open a NEW terminal (or open a
new shell yourself) before the next step.

**GATE 1:** `brain --version` prints a version. If `command not found`:
re-check PATH (`uv tool` installs to `~/.local/bin`; Windows needs the new
terminal). Do not proceed until this passes.

## 2 · Install the engine — Claude Code plugin path (preferred inside Claude Code)

```text
/plugin marketplace add Autopsias/brainiac
/plugin install brainiac-manager@brainiac
/brainiac-install <workspace>          # does §1+§4+§5+§6 in one command
```

If `/brainiac-install` completed successfully, skip to §Q3/Q4 — it already
initialized the vault, registered maintenance, and provisioned the audit key.
Offline / no-GitHub fallback: `git clone https://github.com/Autopsias/brainiac.git ~/brainiac`
then `/plugin marketplace add ~/brainiac`, or `~/brainiac/install.sh --dev`.

## 3 · No network to PyPI at all? (rare)

`git clone https://github.com/Autopsias/brainiac.git ~/brainiac && ~/brainiac/install.sh --dev`
— editable install; builds a lexical-only index of the bundled sample vault
(`BRAIN_EMBEDDER=hash`), zero model download. If even the clone is
impossible, stop and tell the human what network access is required.

## 4 · Create / attach the vault (ask Q1 now if you haven't)

Set the vault path for every command: `export BRAIN_VAULT=<folder>/vault`
(PowerShell: `$env:BRAIN_VAULT='<folder>\vault'`).

```bash
brain init --full --apply
```

Branch on what the folder was:

- **Empty / new folder** → this scaffolds + seeds 3 sample notes + **indexes
  them**. Done — searchable immediately.
- **Folder already has notes** → `init` scaffolds but **deliberately skips
  indexing a non-empty vault**. Ask Q2, then:
  - *index-as-is* → `brain rebuild` (REQUIRED — without it the first search
    returns nothing).
  - *import* → `brain init --full --import-from <their-folder> --yes`, then
    `brain rebuild` if the target vault was non-empty. Warn in one line:
    imported notes without a `classification:` label are invisible to search
    until classified (deny-by-default).
  - *fresh-folder* → re-ask Q1 with a different path.

**GATE 2:** `brain search "welcome" --json` (fresh vault) or a term you KNOW
is in their notes (existing vault) returns JSON hits with `classification`
tiers and an `egress` block. Empty results on a non-empty vault ⇒ you skipped
`brain rebuild` — run it, retest. Do not proceed until this passes.

## 5 · Register the hourly maintenance task

- The plugin path already did this. The script path on macOS/Linux registers
  it during `brain init --full`; **verify, never assume**:
  `brain doctor --check-registry` — it must show a `brain-nightly` task
  covering this vault.
- **Windows:** the umbrella task needs the explicit step
  `.\scripts\install-brief-windows.ps1 -VaultPath <folder>\vault` (from a repo
  clone). The weekly `brain-synthesis` task is NOT auto-registered on Windows
  — say so in your report; do not silently skip it.
- If the registrar reports a **missing audit signing key**, provision it in
  this session (macOS: `security add-generic-password -s
  profile-a-brain-audit-key -a $USER -w "<PEM>"` with a fresh Ed25519 PEM;
  Windows: Credential Manager). Tell the human in ONE sentence what it does
  (signs every committed note so the write history is tamper-evident), let
  them approve any OS prompt, and never print or persist the PEM anywhere.

**GATE 3:** `brain status` runs clean and reports snapshot + pending-draft
state; `brain doctor` has no FAIL lines.

## 6 · Wire the human's AI clients

For each client the human actually uses (you usually know which one YOU are):

```bash
brain connect claude-code        # and/or: claude-desktop | codex | gemini
```

Claude Desktop's *chat* tab (can't run a shell) instead uses the `.mcpb`
bundle: Settings → Extensions → add `dist/brainiac.mcpb` (read-only bridge).

## 7 · Ask Q3 + Q4 (one message), then finish

- **Q4 = now:** `brain warmup` (downloads multilingual-e5-small, ~465 MB,
  progress on stderr) then `brain sync` (re-embeds the index).
  `brain status` must show `embedder: ready`. **later:** say that the first
  semantic query triggers the same download automatically.
- **Q3 = yes:** follow `docs/install/cowork.md` (host stages the engine +
  model into the workspace; NOTHING installs inside the sandbox; the human's
  only manual steps are adding the folder in Cowork and pasting one prompt
  block you print for them). In Claude Code, `/brainiac-cowork-setup` does
  this end-to-end.

## 8 · Final report (always, exactly this shape)

Tell the human, in plain language, one line each:

1. Engine version + install channel (`brain --version`, which of uv/pipx/pip).
2. Where their notes live (`<folder>/vault`) and where the index lives
   (per-user app-data dir; it's a disposable cache — `brain rebuild` recreates it).
3. Maintenance: the `brain-nightly` task name + vault it covers — or exactly
   what was NOT registered and what the human must decide (never silent).
4. Embedder state: `ready` or `pending` (+ what triggers the download).
5. The three commands they'll want next: `brain search "…" --json`,
   `brain status`, `brain --help`.
6. Anything you decided on their behalf (one line per decision).

## 9 · Failure playbook

| Symptom | Fix |
|---|---|
| `brain: command not found` after install | new terminal (PATH), or `~/.local/bin` not on PATH — add it |
| first search returns `[]` on a vault with notes | you skipped `brain rebuild` on a non-empty vault (§4) — run it |
| `EmbedderUnavailable` on search | semantic path needs the model: `brain warmup && brain sync`; lexical `brain grep` works meanwhile |
| PyPI unreachable | §3 dev fallback |
| `init --import-from` refused | you're on a VM/sandbox leg — imports are host-only (§0 applies) |
| registrar skipped / no signing key | §5 — provision the key, re-run `brain init --full`; never leave it silent |
| Windows: task not visible | run PowerShell step in §5 from a repo clone; check Task Scheduler for the brain-nightly label |

Any other error: show the human the exact error text, fix the root cause,
re-run the gate you were on. Never mark the install done with a failing gate.

---

*Deep references (read only if needed): platform matrix
`docs/install/README.md` · second vault `docs/install/second-vault.md` ·
Cowork `docs/install/cowork.md` · conventions your future sessions must
follow: `AGENTS.md` (read it once after installing — it is the contract).*
