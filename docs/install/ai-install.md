# Install Brainiac — 3 commands, no terminal skills needed

> **Entry point / platform picker: [`./README.md`](./README.md)** — start
> there if you haven't already picked your path.

If you use **Claude Code**, the fastest path is the `brainiac-manager`
plugin. It clones the repo, runs the installer, verifies search, registers
nightly maintenance, provisions the audit key, and asks you the one Cowork
question — all in one command.

> **Not Cowork, and not the Chat tab.** Cowork runs inside an ephemeral Linux
> sandbox — nothing installed there survives, so it can never be the host
> install. Cowork gets its own setup afterwards via `/brainiac-cowork-setup`
> (see [`cowork.md`](./cowork.md)). The plain Claude Desktop Chat tab can't
> run commands at all — use Claude Code, Codex (fallback below), or a
> colleague running `./install.sh`.

## The 3 commands

```text
claude> /plugin marketplace add Autopsias/brainiac                # public repo — no clone, no creds needed
claude> /plugin install brainiac-manager@profile-a-marketplace
claude> /brainiac-install
```

`/brainiac-install` then asks you one question: *"Do you also use Claude
Desktop's Cowork and want this brain available there?"* Answer yes and it
runs `/brainiac-cowork-setup` for you; answer no and you're done.

**Note:** `/brainiac-install` still clones `~/brainiac` itself as the
canonical code checkout — the plugin only carries the lifecycle skills, not
the code. Git clone is no longer a prerequisite step for you to run by hand.

**Fallback (offline / credential-restricted environments):**
`git clone https://github.com/Autopsias/brainiac.git ~/brainiac` then
`/plugin marketplace add ~/brainiac` (local-path add) works with zero
ambient network auth.

## Updating Brainiac

```text
claude> /plugin marketplace update
claude> /brainiac-update
```

This re-pulls `~/brainiac`, reinstalls the engine venv, re-stages every
registered Cowork workspace, refreshes the nightly maintenance task if it
changed, and prints a per-workspace pass/fail report.

## Removing Brainiac

```text
claude> /brainiac-uninstall
```

Removes this host's nightly maintenance task(s) and venv, and drops this
host's registry entries. Never touches a vault, its notes, or the audit
signing key — it prints what it left behind (vaults, key location) so you
can archive deliberately. `~/brainiac` and the plugin itself are left for
you to remove by hand (the skill prints those commands too).

## Fallback: agents without plugin support (e.g. Codex)

Codex (and any other AI client that runs commands directly on your computer
but doesn't support Claude Code plugins) can still do the whole install —
paste this prompt instead:

```text
Please install the Brainiac second-brain CLI for me:

0. First, check where your shell actually runs. If you cannot run commands
   directly on MY computer — because you run in a sandbox or VM (e.g. you
   are Cowork or a cloud agent) — do NOT install anything there: it would
   be wiped and never reach my PATH. Instead, give me this exact block to
   paste into my own Terminal, then guide me through the remaining steps
   and verify from my pasted output:

     cd "$HOME/brainiac" 2>/dev/null || gh repo clone Autopsias/brainiac "$HOME/brainiac"
     cd "$HOME/brainiac" && ./install.sh

1. Get the code: if it is already cloned somewhere on this machine, use
   that copy; otherwise clone it with an authenticated client
   (`gh repo clone Autopsias/brainiac ~/brainiac` — the repo is private
   pre-release, so an anonymous git clone will fail).
2. Run its ./install.sh from the repo root. It creates a private Python
   venv, installs the `brain` CLI at full capacity, links it onto my PATH,
   and builds the search index for the bundled sample vault (this downloads
   an embedding model once — a few hundred MB).
3. Verify the install: run `brain search "arctic-embed vs e5" --json` from
   the repo root and confirm it returns JSON results with classification
   tiers and an egress block, with no embedder warning.
4. Read the repo's AGENTS.md so you know the note conventions and the
   security rules for future sessions.
5. Report the scheduled-maintenance status in one line: whether the nightly
   drain/sign/reindex task got registered (name + vault it covers), or if
   it's missing/skipped, say so explicitly and what I must decide
   (see SECURITY.md) — never leave this silent.
6. Tell me, in plain language: where my notes will live, where the index
   lives, and the three commands I'm most likely to want next.
7. Then ask me ONE question: "Do you also use Claude Desktop's Cowork and
   want this brain available there?" If I say no, you're done. If I say
   yes, do the ENTIRE Cowork preparation yourself with no further
   questions except which folder is my workspace:
   - Stage the embedding model with packaging/stage_model.py, then run
     tools/cowork_workspace_install.sh <my workspace>/vault <model dir>.
     No Docker, no compilers, and NOTHING gets installed in the Cowork
     sandbox — the pure-Python engine is staged into the workspace and
     runs directly with the sandbox's python3.
   - Register the host maintenance task for that vault:
     BRAIN_VAULT=<my workspace>/vault brain init --full (as host, from the
     repo clone). This registers the per-vault nightly label — the ONE
     sanctioned daily OS task that drains Cowork drafts, signs them,
     re-indexes, and republishes the snapshot. Show me its report; if it
     reports a missing signing key or a skipped registrar, explain what
     that means and what I must decide (see SECURITY.md) — do not silently
     leave maintenance unregistered.
   - If the registrar warns that no audit signing key exists, create it in
     the same session: generate an Ed25519 key with brain.audit's
     generate_key_pem() and store it in the OS secret store (macOS:
     `security add-generic-password -s profile-a-brain-audit-key -a $USER -w
     "<PEM>"`; Windows: Credential Manager) — tell me in one sentence what
     the key does (signs every committed note so the audit chain is
     tamper-evident) and let me approve the permission prompt when it
     appears. Never print or persist the PEM anywhere else.
   - When it finishes, print for me, in the chat: (a) the exact folder I
     must add inside Cowork, (b) the full contents of
     <workspace>/vault/.brain/routines/cowork-session-prompt.md so I can
     copy it straight from this conversation, and (c) the list of the 10
     kernel/extras skill files in <workspace>/vault/.brain/skills/ with one
     line on how to add them (Cowork Save-skill upload — a drag-and-drop
     only I can do).
   My only remaining manual work must be: add the folder in Cowork, paste
   that one block into the Cowork project's custom instructions (or as my
   first message there), and optionally upload the skill zips. Task
   TRIGGERS on the Cowork side (the registrar prompt) stay optional —
   mention them, don't walk me through unless I ask. Also mention that in
   Claude Code, working inside the repo clone auto-loads the skills, and
   the brainiac-manager plugin (see the 3-command flow at the top of this
   page) installs them for sessions in other folders.

If anything fails, show me the exact error and fix it before moving on.
```

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
