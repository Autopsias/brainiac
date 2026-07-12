# Install Brainiac — 3 commands, no terminal skills needed

> **Entry point / platform picker: [`./README.md`](./README.md)** — start
> there if you haven't already picked your path.

If you use **Claude Code**, the fastest path is the `brainiac-manager`
plugin. It installs the engine from PyPI (no clone), verifies search,
registers nightly maintenance, provisions the audit key, and asks you the
one Cowork question — all in one command.

> **Not Cowork, and not the Chat tab.** Cowork runs inside an ephemeral Linux
> sandbox — nothing installed there survives, so it can never be the host
> install. Cowork gets its own setup afterwards via `/brainiac-cowork-setup`
> (see [`cowork.md`](./cowork.md)). The plain Claude Desktop Chat tab can't
> run commands at all — use Claude Code, Codex (fallback below), or a
> colleague running `./install.sh`.

## The 3 commands

```text
claude> /plugin marketplace add Autopsias/brainiac                # public repo — no clone, no creds needed
claude> /plugin install brainiac-manager@brainiac
claude> /brainiac-install
```

`/brainiac-install` then asks you one question: *"Do you also use Claude
Desktop's Cowork and want this brain available there?"* Answer yes and it
runs `/brainiac-cowork-setup` for you; answer no and you're done.

**Note:** `/brainiac-install` installs the `brain` CLI engine straight from
**PyPI** (`brainiac-cli` — tries `uv tool install`, then `pipx`, then `pip
--user`, first success wins) — no clone. A checkout is only needed for two
things: contributing to Brainiac itself (`--dev`, an editable install) and
the one-time workspace-registry write (a tracked packaging gap — see
`docs/install/README.md` Path A step 5).

**Fallback (offline / credential-restricted environments):**
`git clone https://github.com/Autopsias/brainiac.git ~/brainiac` then
`/plugin marketplace add ~/brainiac` (local-path add) works with zero
ambient network auth — that's the fallback for the *skills* marketplace; the
engine itself falls back the same way, via `./install.sh --dev` from that
same clone.

## Updating Brainiac

```text
claude> /plugin marketplace update
claude> /brainiac-update
```

This self-executes the whole update: marketplace refresh, then a
**channel-aware** engine reinstall (`uv tool upgrade` / `pipx upgrade` /
`pip install --user --upgrade`, whichever channel installed it), a
downgrade-safe CLI-plugin reinstall, a re-stage of every registered Cowork
workspace, a nightly-task refresh if it changed, and a final `brain doctor`
verify — then a before→after version table and one pass/fail. `brain update
--dry-run` previews every decision without changing anything.

## Removing Brainiac

```text
claude> /brainiac-uninstall
```

Removes this host's nightly maintenance task(s), the engine (channel-aware —
`uv tool uninstall` / `pipx uninstall` / `pip uninstall`, whichever
channel installed it), and this host's registry entries. Never touches a
vault, its notes, or the audit signing key — it prints what it left behind
(vaults, key location) so you can archive deliberately.

**Not using Claude Code, or want to uninstall by hand?** Run the command for
whichever channel `install.sh`/`install.ps1` used: `uv tool uninstall
brainiac-cli`, `pipx uninstall brainiac-cli`, or `python3 -m pip uninstall
brainiac-cli`. `brain doctor` reports the detected channel if you're not
sure which one applies. Unwire an AI client with `brain connect --client
<name> --remove`; remove the Desktop Chat tab extension via Settings →
Extensions → Brainiac → Remove.

## Any other AI assistant: paste TWO lines, it does the rest

Every AI client that can run commands on your computer (Codex, Gemini CLI,
Claude Code without the plugin, …) can execute the whole install from the
**machine runbook** — a step-by-step instruction set written *for the
assistant*, with detection logic, verification gates, a failure playbook,
and a strict four-question budget (most installs ask you just one: where
the vault should live). Paste this:

```text
Install Brainiac for me. Your complete instruction set is here — fetch it and
follow it exactly, asking me only the questions it allows:
https://raw.githubusercontent.com/Autopsias/brainiac/main/docs/install/LLM-INSTALL.md
```

No web access? Point it at a local copy instead: clone
`https://github.com/Autopsias/brainiac.git` and say *"Read
docs/install/LLM-INSTALL.md from ~/brainiac and follow it."* The runbook
covers macOS, Linux, Windows, sandboxed assistants (it refuses to install
inside a throwaway VM and hands you the commands instead), existing vaults
(the `brain rebuild` gotcha), imports, maintenance-task registration, and
the Cowork question — end to end.

## After it's installed

- **Day-to-day**: you never run `brain` yourself — your AI assistant does,
  through its shell. Just ask it questions; the repo's `AGENTS.md` teaches
  it the rest.
- **A new project/vault?** Say: *"Set up a new Brainiac vault at
  ~/vaults/<name> following docs/install/second-vault.md"* — each vault
  automatically gets its own index and audit chain (0.3.0+), so there is
  nothing to configure.
- **Cowork?** `/brainiac-install` already offers it. Say yes, and the
  assistant prepares everything. You then do exactly two things — add the
  folder it names inside Cowork, and paste the one block it prints into
  that Cowork project's custom instructions. Done. (Deep detail, only
  if you want it: [`cowork.md`](./cowork.md).)
- **Per-client details** (Claude Code vs Codex vs Cowork, who may write vs
  only read): `docs/install/README.md`.
