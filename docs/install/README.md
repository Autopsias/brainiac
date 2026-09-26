# Install Brainiac — choose your platform

> Prefer a guided page with a decision tree? [`../install-guide.html`](../install-guide.html).
> Want your AI assistant to do it? Paste the two-line prompt from
> [`LLM-INSTALL.md`](LLM-INSTALL.md). Every other document:
> [`../README.md`](../README.md).

An install puts three things in place: the **`brain` engine** (search, index,
audit), your **vault** (plain Markdown notes on your own disk), and the
**skills** an AI assistant uses to talk to it. The engine and the vault always
live on a persistent machine, the host. A sandbox such as Cowork can hold
skills but never the engine or the vault, because nothing inside it survives
the session. The full model is in [`new-owner.md`](./new-owner.md).

## Pick your platform

| I use... | Follow |
|---|---|
| Claude Code, in a terminal or in Claude Desktop's **Code tab** | [Path A](#path-a--claude-code-host) |
| Claude Desktop with **Cowork** | [Path A](#path-a--claude-code-host), then [Path B](#path-b--cowork-claude-desktop) |
| Claude Desktop's **Chat tab** only (it cannot run a command) | [Path A](#path-a--claude-code-host) for the engine, then [Path G](#path-g--claude-desktop-chat-tab-mcpb) |
| Codex | [Path C](#path-c--codex) |
| Gemini CLI | [Path D](#path-d--gemini-cli) |
| Windows, no AI client yet (PowerShell) | [Path E](#path-e--windows-powershell) |
| Any OS with Node.js, no AI client yet | [Path F](#path-f--npx-any-os-with-nodejs) |

**Cowork always needs Path A first.** There is no Cowork-only install.

---

## Path A — Claude Code (host)

The same steps work in a terminal and in Claude Desktop's Code tab. Both run
Claude Code.

**Install in one place, not two.** A plugin you add under Customize › Plugins
on claude.ai or in Claude Desktop belongs to your Claude account. Claude Code
shows that copy as `@synced` and loads it on every machine where you sign in.
A plugin you add in the terminal stays on that machine. Pick one route. The
account copy changes only when you press Update in Customize › Plugins.

1. Install the installer plugin. In a terminal session (Claude Code 2.1.275 or
   later):

   ```text
   /plugin install brainiac-manager --marketplace Autopsias/brainiac
   ```

   On an older version, run `/plugin marketplace add Autopsias/brainiac`,
   then `/plugin install brainiac-manager@brainiac`. In Claude Desktop, open
   Customize › Plugins, add the marketplace `Autopsias/brainiac`, and install
   **Brainiac Manager — host lifecycle** from there.

2. Run the installer, pointing at your workspace folder:

   ```text
   /brainiac-install <path-to-your-workspace>
   ```

   The vault is created at `<path>/vault`. If you already have a vault, give
   the folder that contains it. The installer:

   - installs the engine from PyPI (`brainiac-cli`), trying `uv tool install`,
     then `pipx`, then `pip --user`, and stops at the first success;
   - verifies that `brain search` works;
   - runs `brain init --full --apply`, which scaffolds the overlay, seeds three
     sample notes in an empty vault and indexes them, provisions the audit
     signing key when none exists, and registers the maintenance task
     (`com.brainiac.nightly.<id>` on macOS);
   - records the vault in your workspace registry and prints a ✅/❌ line per
     step.

   It never overwrites a vault that already holds notes, and it never rotates
   an existing key. At the end it asks one question: *Do you also use Cowork
   and want this brain available there?* Yes runs Path B for you.

3. Try it: `brain search "welcome" --json` returns the seeded notes.

4. Optional daily-use skills:

   ```text
   /plugin install brainiac-kernel --marketplace Autopsias/brainiac
   ```

   Add `brainiac-extras` the same way for the optional maintenance skills.

**Already have a folder of Markdown notes?** `brain init --full --import-from
<dir>` stages a copy into `vault/inbox/` and runs the ingest. It prints a
dry-run manifest first; add `--yes` to run it. Host only.

**A vault with notes but an empty search?** `brain init` does not index a
non-empty vault. Run `brain rebuild` once.

**No PyPI access, or contributing to Brainiac?** Clone the repository and ask
the installer for the dev path: `./install.sh --dev` makes an editable install
from the checkout. For the plugins, `/plugin marketplace add ~/brainiac`
(a local path) works with no network access.

### Updating

```text
brain doctor        # read-only: what is stale, and the fix for each row
/brainiac-update    # runs the whole update, then verifies
```

`brain doctor` reports every surface with its version: the engine and its
install channel, the plugins, the staged Cowork workspaces, the marketplace
cache. It changes nothing. `--check-registry` adds one row that compares your
version with the latest release on PyPI.

`/brainiac-update` refreshes the marketplace, reinstalls the plugins, upgrades
the engine through the channel that installed it, restages every registered
Cowork workspace, refreshes the maintenance task when it changed, and ends
with `brain doctor`. `brain update --dry-run` shows the plan without changing
anything. Your notes, the audit chain and the signing key are never touched.

### Removing

```text
/brainiac-uninstall
```

Removes this host's maintenance task, the engine (through whichever channel
installed it) and this host's registry entries. It never touches a vault.

---

## Path B — Cowork (Claude Desktop)

Requires Path A. This path stages a copy of what Path A installed; it does not
install the engine.

1. On the host, in Claude Code, run:

   ```text
   /brainiac-cowork-setup
   ```

   It asks one question: *Which folder is your Cowork workspace?* Point it at
   the parent folder of your real vault, so Cowork reads and drafts into the
   same brain. Point it at a new empty folder only when you want a separate,
   empty brain for Cowork. The skill stages the embedding model and a
   zero-install runtime into the workspace, registers the vault's maintenance
   task on the host, and records the workspace in the registry.

2. In Claude Desktop › Cowork, add that folder as the project folder.

3. Under Customize › Plugins, add the marketplace `Autopsias/brainiac` and
   install **Brainiac — Kernel Skills**. Add extras if you want them. The Manager
   skills change the host and fail inside the sandbox, so they are of no use
   in Cowork.

   If the Plugins tab is not reachable, upload the `.skill` zips staged at
   `<workspace>/vault/.brain/skills/` through Cowork's Save-skill flow, kernel
   first.

4. Cowork loads the workspace's `CLAUDE.md` on its own. Send the message
   `contract?` in a Cowork session; a healthy session answers
   `[brain contract loaded]`. If it does not, paste the session prompt saved
   at `<workspace>/vault/.brain/routines/cowork-session-prompt.md` into the
   project's custom instructions.

5. Test it: ask the Cowork session to run `brain status` or to search the
   brain.

Cowork is a read-and-draft surface. It reads the host's published snapshot
and stages drafts with `brain draft-capture`. It never signs, indexes or
commits. The host's maintenance task drains the drafts and republishes the
snapshot. Full detail: [`cowork.md`](./cowork.md).

---

## Path C — Codex

Codex has no plugin system, so the engine installs with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh
bash /tmp/brainiac-install.sh
```

It tries `uv tool install`, then `pipx`, then `pip install --user`. On a
machine with no Python, it fetches `uv` from `astral.sh` first and says so;
`--no-uv-bootstrap` forbids that download. `--with-ocr` also installs the OCR
toolchain for scanned PDFs; without it, scanned PDFs wait in quarantine.

Then create the vault:

```bash
BRAIN_VAULT=~/brain/vault brain init --full --apply
```

**Skills.** Codex reads skills from a repository checkout, so clone it once:

```bash
git clone https://github.com/Autopsias/brainiac.git ~/brainiac
```

The skills load from `~/brainiac/.agents/skills/` when Codex's working
directory sees that checkout. `brain connect --client codex` adds the
brain-usage block to a project's `AGENTS.md`. For a guided install driven by
Codex itself, see the Codex appendix in [`ai-install.md`](./ai-install.md).

---

## Path D — Gemini CLI

Same engine install as Path C, then `brain init --full --apply` as above.
Gemini CLI reads `AGENTS.md` through its `contextFileName` setting;
`brain connect --client gemini` writes that setting for a project. The
per-client table is in [`../harness-wiring.md`](../harness-wiring.md).

---

## Path E — Windows (PowerShell)

A native PowerShell installer, with no WSL, Git Bash or Claude Code needed.
Use it to set up the engine on a Windows host, including the host side of a
[Cowork on Windows](../cowork-windows-install.md) setup.

```powershell
irm https://raw.githubusercontent.com/Autopsias/brainiac/main/install.ps1 -OutFile install.ps1
.\install.ps1
$env:BRAIN_VAULT = "$HOME\brain\vault"; brain init --full --apply
```

It tries `uv tool install`, then `pipx`, then `pip install --user`, and
fetches `uv` first on a machine with no Python (`-NoUvBootstrap` forbids
that). It adds `brain` to your User PATH without overwriting it. For the OCR
toolchain it prints the `winget` or `choco` command instead of running it.

**The maintenance task.** `install.ps1` sets up the engine only. The task
script ships in the repository, so clone it once
(`git clone https://github.com/Autopsias/brainiac.git`), then register the
task once per vault from that checkout:

```powershell
.\scripts\install-brief-windows.ps1 -VaultPath C:\path\to\your\vault
```

This creates a Windows Scheduled Task named `brain-daily-brief-<id>`. Known
gap: the weekly `brain-synthesis` task has no Windows registration yet.

**Contributing, or no PyPI access?** Clone the repository and run
`.\install.ps1 -Dev` for an editable install in a private venv.

If Claude Code runs on the same machine, Path A's `/brainiac-install` does all
of this in one step and works on Windows.

---

## Path F — npx (any OS with Node.js)

```text
npx brainiac-install --vault ~/my-brain --client claude-code
```

It runs the same PyPI chain, verifies `brain --version`, runs `brain init
--full --apply` against `<path>/vault`, and can wire one client through
`brain connect`. `--dry-run` prints the plan and runs nothing. The script has
no runtime dependencies and makes no network calls of its own.

It needs Node.js 18 or later **and** `uv`, `pipx` or Python already on the
machine. It does not fetch a Python toolchain; on a machine without one, use
Path C or Path E. Full contract:
[`../../packaging/npm/brainiac-install/README.md`](../../packaging/npm/brainiac-install/README.md).

---

## Path G — Claude Desktop Chat tab (.mcpb)

Requires an engine install first (Path A, C, E or F). The Chat tab cannot run
a command, so it talks to the engine already on your machine.

1. Check that `brain --version` works in a terminal.
2. Download `brainiac.mcpb` from the
   [latest release](https://github.com/Autopsias/brainiac/releases/latest)
   and double-click it, or use Settings › Extensions › Advanced settings ›
   Install Extension. The same small file works on macOS and Windows.
3. Accept the permissions prompt, then set the **Vault path** to the folder
   that contains `vault/brain` and `vault/raw`. You may narrow the **Max
   egress tier** in the extension's settings.

The extension is a thin Node.js shim that spawns your host-installed
`brain-mcp`. It exposes the CLI's read verbs (`search`, `get`, `recent`, `dossier`,
`bases-query` and the rest), three non-read tools (`capture` stages an
unsigned draft; `supersede` and `unsupersede` retire or restore a note
version), and the same classification filter as the CLI. It never exposes
`write`, `rebuild`, `ingest` or any other host-broker verb. If
the engine is missing, it stops with *Install the engine first*.

**Pick one registration path, never both.** `brain connect --client
claude-desktop` and the `.mcpb` extension both register a `brainiac` server
with Claude Desktop. `brain doctor` flags a double registration and names the
removal command for each.

---

## FAQ

**Why does Cowork need Claude Code?** Cowork is a disposable sandbox. Nothing
installed inside it survives, and it cannot hold the signing key or write to
your vault. The engine and the vault live on the host, and Claude Code is how
you drive the host install.

**What are the three plugins?** `brainiac-manager` installs, updates, checks
and removes the engine; it changes your machine, so it never belongs in
Cowork. `brainiac-kernel` holds the daily-use skills; install it wherever you
work, Cowork included. `brainiac-extras` holds optional maintenance skills.
Each plugin folder has a README that lists what it runs and fetches.

**How do I update?** `brain doctor` to see what is stale, then
`/brainiac-update` on the host, or `brain update` in any terminal.

**How do I uninstall?** `/brainiac-uninstall` on the host. Without Claude
Code, remove the engine through the channel that installed it:
`uv tool uninstall brainiac-cli`, `pipx uninstall brainiac-cli` or
`python3 -m pip uninstall brainiac-cli`. Unwire a client with
`brain connect --client <name> --remove`. Remove the Chat tab extension under
Settings › Extensions. None of these touch your vault or the signing key.

**Do I need a plugin at all?** No. `install.sh`, `install.ps1` and
`npx brainiac-install` install the same PyPI package the plugin uses. The
plugin saves you the PATH, task, key and registry steps.

**Do I need to clone the repository?** Only to contribute (an editable
install) or to set up Cowork, whose staging scripts are not in the wheel yet.

---

A second vault on the same install: [`second-vault.md`](./second-vault.md).
The durable picture of what runs where: [`new-owner.md`](./new-owner.md).
