# Install Brainiac — choose your platform

> **This page is the detailed, platform-by-platform matrix.** If you'd rather:
> a **visual walk-through** → [`../install-guide.html`](../install-guide.html);
> have your **AI assistant do it for you** → paste
> [`LLM-INSTALL.md`](LLM-INSTALL.md)'s two-line prompt; or see **every doc** →
> [`../README.md`](../README.md) (the documentation map).

Installing Brainiac puts three things in place: the **`brain` CLI engine**
(search/index/audit), your **vault** (plain Markdown notes on your own disk),
and a set of **skills** the AI harness uses to talk to it.

**The two-layer model, in one paragraph:** skills install via a plugin
marketplace wherever a plugin system exists (Claude Code, and Claude
Desktop's Plugins tab). The **engine and your vault always live on your own
computer** — the host. A sandboxed surface like Cowork (Claude Desktop's
disposable Linux VM) can install skills, but it can never host the engine or
own the vault, because nothing inside it survives past the session. Instead,
the host stages a read-only copy for Cowork to read from and a draft inbox
for it to write to. This is why "why can't the plugin install everything
inside Cowork?" has one answer: **Cowork has no persistent disk** — the host
is the only place the engine and vault can live.

## Pick your platform

| I use... | Follow |
|---|---|
| Claude Code — terminal, or Claude Desktop's **Code tab** | [Path A](#path-a--claude-code-host) |
| Claude Desktop with **Cowork** | [Path A](#path-a--claude-code-host), then [Path B](#path-b--cowork-claude-desktop) |
| Claude Desktop's pure **Chat tab** (can't run a command) | [Path A](#path-a--claude-code-host) first (the engine), then [Path G](#path-g--claude-desktop-chat-tab-mcpb) |
| Codex | [Path C](#path-c--codex) |
| Gemini CLI | [Path D](#path-d--gemini-cli) |
| Windows, no AI client yet (plain PowerShell) | [Path E](#path-e--windows-powershell) |
| Any OS, Node.js already on hand, no AI client yet | [Path F](#path-f--npx-any-os-with-nodejs) |

**Cowork always requires Path A first.** The engine and the vault live on the
host; there is no Cowork-only install. If you only use Cowork, you (or
someone with terminal access) still has to run Path A once on a real machine
before Path B can do anything.

---

## Path A — Claude Code (host)

Works whether you're in a terminal (`claude`) or Claude Desktop's **Code
tab** — both are Claude Code under the hood. The only difference is *how you
add the marketplace* in step 2: the `/plugin` slash command exists **only in
the terminal CLI** — in the Desktop app it returns "/plugin isn't available
in this environment" ([anthropics/claude-code#42142](https://github.com/anthropics/claude-code/issues/42142)).
Desktop users add it through the **Plugins tab UI** instead; the installed
plugins are shared (`~/.claude/plugins/`), so either route serves both
surfaces.

1. Open Claude Code.
2. Add the plugin marketplace (public repo, no clone, no credentials):

   **Terminal CLI:**

   ```
   /plugin marketplace add Autopsias/brainiac
   ```

   **Claude Desktop (Code tab or Cowork):** Customize → **Plugins** → add
   marketplace → enter `Autopsias/brainiac`. All three plugins appear under
   a "brainiac" marketplace chip (verified live 2026-07-04). Install from
   the same UI, then skip step 3.

3. Install the lifecycle plugin:

   ```
   /plugin install brainiac-manager@brainiac
   ```

4. Run the installer, pointing at your vault:

   ```
   /brainiac-install <path-to-your-vault>
   ```

   What this does: installs the `brain` CLI engine straight from **PyPI**
   (`brainiac-cli` — tries `uv tool install`, then `pipx`, then `pip
   --user`, first success wins, no clone), verifies `brain search` works,
   registers the nightly maintenance task (per-vault launchd label
   `com.brainiac.nightly.<id>`), provisions the audit signing key
   (idempotent — never rotates an existing key), records the vault in your
   workspace registry, and prints an explicit ✅/❌ report for every step.

   **No PyPI access, or contributing to Brainiac itself?** Ask for the
   dev/offline path and the skill falls back to `git clone` +
   `./install.sh --dev` (editable install) instead — the clone is
   dev/offline-only now, never the default.

   **About the path you give:** treat it as the **workspace** folder — the
   vault always lives at `<path>/vault` (the installer creates it; this
   matches what Cowork setup and the VM session prompt expect, so the same
   folder works everywhere). If you already have a vault, give the folder
   *containing* it, laid out as `<workspace>/vault`. `brain init --full
   --apply` (which this step runs) scaffolds the **overlay** layer
   (voice/brand/keywords/people) into the vault, and — only when
   `vault/brain/` is genuinely empty — seeds it with 3 generic sample notes
   (a welcome note, a `concept` note, and their wikilinked partner). Never
   overwrites a vault that already has notes.

   `brain init --full --apply` **indexes the seeded notes in the same call**,
   so the vault is searchable immediately — `brain search "welcome" --json`
   (matching the seeded sample notes) is a safe first query to confirm it
   worked. (If you ever seed or add notes some other way and a search comes
   back empty, `brain rebuild` rebuilds the derived index from `vault/`.)

   **Already have a folder of Markdown notes** (an Obsidian vault, a plain
   notes export)? `brain init --full --import-from <dir>` stages a *copy* of
   it into `vault/inbox/` and runs the standard ingest drain — prints a
   dry-run manifest (file count/bytes/extensions) first, then `--yes` to
   actually stage + ingest (host only; refused on a Cowork/VM session).

   At the end it asks one question: *"Do you also use Claude Desktop's
   Cowork and want this brain available there?"* Say yes to run
   `/brainiac-cowork-setup` for you (this is Path B) — say no and you're
   done.

5. **Optional — daily-use skills:**

   ```
   /plugin install brainiac-kernel@brainiac
   ```

   Adds the daily-use kernel skills (search/curate/capture/promote). Add
   `brainiac-extras@brainiac` too if you want the optional
   admin skills.

**Fallback (offline / credential-restricted):**
`git clone https://github.com/Autopsias/brainiac.git ~/brainiac` then
`/plugin marketplace add ~/brainiac` (local-path add) — no ambient network
auth needed. That's the fallback for the *skills* marketplace; the engine
itself falls back the same way, via `./install.sh --dev` from that same
clone (see step 4 above).

**The clone is dev/offline-only, never load-bearing for a normal install.**
On the default PyPI path there is no clone to protect — `brain` is a normal
pip-managed console script, safely removed/reinstalled via
`/brainiac-uninstall` / `/brainiac-update` regardless of what happens to any
folder on disk. (Only the `--dev` editable path ties `brain` to a checkout —
see that skill step for the "don't delete the clone" caveat, which applies
there and only there.)

### Updating

```
brain doctor        # read-only: "am I current?" across every surface (v0.10.0+)
/brainiac-update    # runs the whole update, then self-verifies
```

`brain doctor` reports each surface's version and what's stale — engine
install (with the detected channel: `uv tool` / `pipx` / `pip --user` /
editable-checkout), CLI plugins, staged Cowork workspaces, marketplace
cache, Desktop/Cowork store — with the exact fix command per row, and
mutates nothing. Pass `--check-registry` to add the one row that compares
the repo's latest release tag, your installed version, and the latest
version actually published on PyPI (a single cached HTTPS read; off by
default).

`/brainiac-update` **self-executes** the update rather than printing a checklist:
marketplace refresh → downgrade-safe CLI-plugin reinstall → **channel-aware**
engine reinstall (runs `uv tool upgrade` / `pipx upgrade` / `pip install
--user --upgrade`, whichever channel is actually live) → re-stage of every
registered Cowork workspace (skipped gracefully if you have none and no
local checkout) → nightly-task refresh if it changed → a final `brain
doctor` verify, then a before→after version table and one pass/fail. It
auto-handles the reconciliation downgrade (installed newer than the
marketplace) with a clean reinstall. `brain update --dry-run` previews
every decision without changing anything. Your notes, `raw/`, audit chain,
and runtime state are never touched.

### Removing

```
/brainiac-uninstall
```

Removes this host's nightly maintenance task(s), the engine (channel-aware —
`uv tool uninstall` / `pipx uninstall` / `pip uninstall` / the legacy
`~/.brainiac/venv`, whichever applies), and this host's registry entries.

---

## Path B — Cowork (Claude Desktop)

**Requires Path A done first** — this stages a copy of what Path A already
installed; it does not install the engine itself.

1. On the **host**, in Claude Code, run:

   ```
   /brainiac-cowork-setup
   ```

   It asks one question: *"Which folder is your Cowork workspace?"* — this
   is the folder Cowork will mount; the vault lives at `<workspace>/vault`
   inside it.

   - Point it at **your real vault's parent folder** if you want Cowork
     reading and drafting into the *same brain* you use on the host. This is
     what most people want.
   - Point it at a **new empty folder** only if you deliberately want a
     separate, empty brain for Cowork — it will not see any of your existing
     notes.

   The skill stages the embedding model, installs a zero-install pure-Python
   runtime + `brain` shim into the workspace (no Docker, no compilers),
   registers the vault's nightly host maintenance task, and records the
   workspace in the registry.

2. In **Claude Desktop → Cowork**, add that folder as the project folder.

3. **Customize → Plugins → add marketplace `Autopsias/brainiac`** → install
   **Profile A — Kernel Skills** (`brainiac-extras` optional). **Do not
   install Brainiac Manager in Cowork** — it's host-only lifecycle tooling
   (install/update/uninstall/cowork-setup) that mutates things Cowork can't
   reach; it simply can't do its job from inside the VM.

   Fallback if the Plugins tab isn't reachable (offline session, blocked
   marketplace fetch): upload the `.skill` zips staged at
   `<workspace>/vault/.brain/skills/` via Cowork's Save-skill upload flow
   (kernel first, extras optional).

4. Paste the session prompt — printed by step 1, and also saved at
   `<workspace>/vault/.brain/routines/cowork-session-prompt.md` — into the
   Cowork project's custom instructions.

5. Test it: in the Cowork session, ask it to run `brain status` or search
   the brain.

**What Cowork can and can't do:** it's a **read + draft** surface — it reads
the host's published read-only snapshot and can stage draft notes/sources via
`brain draft-capture`. It never signs, indexes, or commits. The host drains
and commits Cowork's drafts (on the next `brain sync`, or via the nightly
maintenance task) and republishes the snapshot so the note becomes visible
back in Cowork.

---

## Path C — Codex

Codex has no plugin system, so this is a one-command engine install, no
clone required:

```
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh
bash /tmp/brainiac-install.sh
```

Tries `uv tool install`, then `pipx`, then `pip install --user` — first
success wins. Pass `--with-ocr` to also install the OCR toolchain
(scanned-PDF ingestion) visibly; skip it and scanned PDFs just quarantine
until you do.

**Skills.** Codex has no marketplace/plugin system to install into — its
`.agents/skills/` directory is repo-local, so it needs the repo present
regardless of engine channel:

```
git clone https://github.com/Autopsias/brainiac.git ~/brainiac
```

Skills auto-load from `~/brainiac/.agents/skills/` once Codex's working
directory sees that checkout — nothing else to wire up. (If you're
contributing to Brainiac itself, or have no PyPI access, run
`./install.sh --dev` from that same clone instead of the curl'd command
above, for an editable install.) For the guided fallback prompt (useful if
you want Codex to drive the whole install interactively), see the Codex
appendix in [`ai-install.md`](./ai-install.md).

---

## Path D — Gemini CLI

Same one-command engine install as Path C, no clone required:

```
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh
bash /tmp/brainiac-install.sh
```

Gemini CLI reads `AGENTS.md` via its `.gemini/` `contextFileName` setting —
that's the one file it needs, and it self-discovers the whole interface from
it; there's nothing skill-shaped to install. Point `contextFileName` at a
clone's `AGENTS.md` (`git clone https://github.com/Autopsias/brainiac.git
~/brainiac`) if you want Gemini CLI to read the conventions file directly
rather than relying on the engine's own `--help` output. Full per-harness
wiring table: [`../harness-wiring.md`](../harness-wiring.md).

---

## Path E — Windows (PowerShell)

A native PowerShell installer, at parity with `install.sh` — no WSL, no
Git Bash, no Claude Code required. Use this if you're setting up the engine
directly on a Windows host (including as the **host** side of a
[Cowork-Windows](../cowork-windows-install.md) setup, which needs a host
install to exist before it can stage anything).

```powershell
irm https://raw.githubusercontent.com/Autopsias/brainiac/main/install.ps1 -OutFile install.ps1
.\install.ps1
```

PyPI-first, same as `install.sh`: tries `uv tool install`, then `pipx`, then
`pip install --user`, first success wins, each attempt visibly reported.
Puts `brain` on your **User PATH** (additive — never overwrites your
existing PATH, safe to re-run). Semantic search downloads its embedding
model (~300 MB) lazily on first real use, or run `brain warmup` up front.
Unlike `install.sh --with-ocr`, it never runs a package manager for you — it
just prints the `winget`/`choco` command for the OCR toolchain, since
scanned PDFs degrade to a metadata-only quarantine without it rather than
failing anything.

**Contributing to Brainiac, or no PyPI access?** Clone and pass `-Dev` for
an editable install from the checkout — creates a private venv under
`%USERPROFILE%\.brainiac\venv` (or `$env:BRAINIAC_HOME\venv`) and also
builds a lexical-only index for the checkout's bundled sample vault:

```powershell
git clone https://github.com/Autopsias/brainiac.git
cd brainiac
.\install.ps1 -Dev
```

**Nightly maintenance (Task Scheduler).** `install.ps1` only sets up the
engine — it doesn't register the drain/sign/reindex/brief job. Do that once
per vault with:

```powershell
.\scripts\install-brief-windows.ps1 -VaultPath C:\path\to\your\vault
```

This registers a per-vault Windows Scheduled Task (named `brain-daily-brief-<8-hex-slug>`,
so multiple vaults never clobber each other's job — see the script's own
header comment for the uninstall command and its threat model for the audit
signing key). **Known gap:** this registers only the nightly umbrella task —
the second locked host task (`brain-synthesis`, weekly) has no Windows
registration path yet (macOS's installer registers both); see
`docs/release-runbook.md` §7.8.

Already using Claude Code on the same machine? Path A's `/brainiac-install`
does the equivalent orchestration (PATH + nightly task + audit key) in one
step and works natively on Windows now too (PyPI-first, no bash required)
— use whichever surface you're already in.

---

## Path F — npx (any OS with Node.js)

One command, cross-platform, no bash/PowerShell script required — useful when
Node.js (>=18) is already on the machine and you'd rather skip a shell
script entirely:

```
npx brainiac-install
```

Same PyPI-first chain as Paths A/E (`uv tool install` → `pipx install` →
`python3 -m pip install --user`, first success wins), then it verifies
`brain --version`, offers to initialize a vault (`brain init --full
--apply`, prompting for a workspace path — pass `--vault <path>` to skip the
prompt), and can wire one client in the same command:

```
npx brainiac-install --vault ~/my-brain --client claude-code
```

`npx brainiac-install --dry-run` prints the exact command plan without
running anything — the script itself makes no network calls (npx still
fetches the package the first time). It never pipes a remote script into a
shell and carries no runtime dependencies; see
`packaging/npm/brainiac-install/README.md` for the full contract. This path
is **for machines that already have Node** — it doesn't install Node itself;
a machine with only Python (no Node) should use Path A/E's shell/PowerShell
installer instead.

---

## Path G — Claude Desktop Chat tab (.mcpb)

**Requires Path A (or C/E/F) done first** — the Chat tab is the one surface
that **can't run a command**, so it never installs the engine itself; it
only talks to whatever engine is already on your machine.

1. Make sure the engine is installed (`brain --version` works in a terminal —
   if not, run Path A/E/F first).
2. Download `brainiac.mcpb` (built from `packaging/mcpb/build.sh` in this
   repo, or from a release) and **double-click it**, or drag it onto the
   Claude Desktop window, or **Settings → Extensions → Advanced settings →
   Install Extension…**.
3. Review the permissions prompt and finish install. Claude Desktop will
   prompt for the **Vault path** (required — point it at the folder
   containing `vault/brain`, `vault/raw`; there is no "default vault"
   registry to fall back to, so this can't be left blank) and you may
   optionally narrow the **Max egress tier** in the extension's settings.

What this is: a thin Node.js stdio shim (Claude Desktop only bundles
Node, not Python) that spawns your **host-installed** `brain-mcp` and pipes
stdio straight through — it never vendors its own copy of the engine, and it
exposes the exact same read-only verb set (`search`/`get`/`recent`/
`dossier`/`bases-query`) and classification egress gate as the CLI. If the
engine isn't installed, the extension fails to start with: *"Install the
engine first: npx brainiac-install."*

**Pick ONE registration path — never both.** `brain connect --client
claude-desktop` (the CLI-managed route) and this .mcpb extension both
register a `brainiac` MCP server with Claude Desktop; the app does not
reconcile the two if both exist. `brain doctor` flags it under "Desktop MCP
registration (mcpb vs claude_desktop_config.json)" with the exact removal
command for each — `brain connect --client claude-desktop --remove` for the
config.json stanza, or Settings → Extensions → Brainiac → Remove for the
.mcpb.

---

## FAQ

**Why do I need Claude Code for a Cowork install?**
Cowork is an ephemeral, sandboxed Linux VM — nothing installed inside it
survives, and it can't hold the audit signing key or write to your real
vault. The engine and the vault must live on a persistent machine (the
host), and Claude Code is how you drive that host install.

**What's the difference between the three plugins?**
`brainiac-manager` is host-only lifecycle tooling (install/update/uninstall/
cowork-setup) — it mutates your machine, so it never belongs in Cowork.
`brainiac-kernel` is the daily-use skill set (search, curate, capture,
promote) — install it wherever you actually work, including Cowork.
`brainiac-extras` is optional admin/maintenance skills on top of the
kernel.

**How do I update?**
Run `brain doctor` (read-only) to see what's stale, then `/brainiac-update` on
the host to become current. `/brainiac-update` self-executes the marketplace
refresh, the downgrade-safe plugin reinstall, the engine reinstall, and a
re-stage of every registered Cowork workspace (no separate Cowork update step),
then verifies with `brain doctor` and prints a before→after table. `brain
update --dry-run` previews it without changing anything.

**How do I uninstall?**
`/brainiac-uninstall` on the host (Claude Code) — idempotent, safe to
re-run. On any other terminal (Codex, Gemini CLI, plain shell), remove the
engine directly with whichever channel installed it: `uv tool uninstall
brainiac-cli`, `pipx uninstall brainiac-cli`, or `python3 -m pip uninstall
brainiac-cli` (`brain doctor` reports the detected channel if you're not
sure). `npx brainiac-install` doesn't add a separate uninstall command of its
own — it's a bootstrap for one of those same three channels, so the same
per-channel command removes it. Unwire an AI client's config/CLAUDE.md/
AGENTS.md changes with `brain connect --client <name> --remove`. Remove the
Desktop Chat tab `.mcpb` extension via Settings → Extensions → Brainiac →
Remove. None of these touch your vault, notes, or audit signing key.

**Do I need a plugin at all?**
No — `./install.sh` (the [`README.md`](../../README.md) one-command,
no-clone path) works everywhere and installs the exact same PyPI package the
plugin's `/brainiac-install` skill runs under the hood. The plugin exists to
save you the manual PATH/registration/audit-key/registry steps, not to gate
the install behind it. Plain `pip install brainiac-cli[mcp]` (or `uv tool
install brainiac-cli[mcp]`) works too, without `install.sh`'s
channel-fallback convenience — that's also what the Chat tab's `.mcpb`
extension spawns under the hood (see [Path G](#path-g--claude-desktop-chat-tab-mcpb)),
so a Chat-tab-only user still needs one of these engine installs done first.

**Do I need to clone the repo?**
No, not for a normal install — the engine (`brainiac-cli`) is a published
PyPI package with `brain` as its console command. Only two things still need
a real checkout: contributing to Brainiac itself (`--dev`/`-Dev`, an
editable install), and Cowork setup (`/brainiac-cowork-setup` needs
`stage_model.py` + `cowork_workspace_install.sh`, which aren't wheel-packaged
yet — see `docs/release-runbook.md` for the tracked follow-up). Everything
else — install, update, uninstall, daily use — never touches a clone.

---

Already set up and starting a **second** vault/project on the same install?
See [`second-vault.md`](./second-vault.md) — `$BRAIN_VAULT`, re-running
`brain init --full`, the per-vault overlay, and the shared-index /
fixed-scheduled-task gotchas. For the durable "what runs where and why"
picture (not step-by-step): [`new-owner.md`](./new-owner.md).
