---
name: graph-explorer
description: "Open THIS vault's visual graph explorer — the self-contained HTML page (WebGL link graph + 3D semantic map) at .brain/graph/graph-explorer.html — by re-rendering it fresh from the live index and sending it to the user to view. Triggers: \"show me the graph\", \"open the vault explorer\", \"graph report\", \"graph explorer\", \"visualize the vault\", \"semantic map\", \"link graph\", \"see the vault structure\", \"where's the html graph view\". Host-only: `brain graph-report` is refused on role=vm, so in a Cowork VM session this skill reports that the view is a host-side artifact instead. NOT for the graphify discovery build (rebuilding inferred edges — that's `brain graphify`, monthly/host), and NOT for `brain health-report` (the text health readout, which only LINKS to this page)."
---

# graph-explorer (open the vault's visual graph + semantic map)

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

The `brain` engine ships a **static HTML explorer** of a vault:
`<vault>/.brain/graph/graph-explorer.html` — a single self-contained page
(WebGL wikilink graph + 3D semantic map, no external assets). This skill
regenerates it for whatever vault the session is in and shows it.

**Key fact:** `brain graph-report` re-renders the page from the **live index**
every call, so the link graph and semantic map are always current. Only the
*inferred* edges come from the monthly `graphify` build (`graph.json`,
`authoritative: false`) — you do **not** need to run `graphify` to get a
current view; run it only when the user explicitly wants inferred edges
refreshed.

## Steps

1. **Re-render + capture the path** (host session, from inside the vault repo):

   ```bash
   brain graph-report --json
   ```

   Returns `{"path", "graph_generation", "nodes", "edges", "points"}`. The
   command resolves the vault the same way every other `brain` call in the
   session does (cwd / `$BRAIN_VAULT` / `--vault <dir>`). If the user names a
   different vault, pass `--vault /path/to/vault`.

2. **Send the file to the user** to view inline — use `SendUserFile` with the
   returned `path`, `display: "render"`, and a caption stating node/edge/point
   counts and the graph generation. Do not paste the 4 MB HTML into the chat.

3. **Report the counts** in one line (nodes / edges / semantic points, graph
   generation). If the user wants the inferred-edge layer refreshed first, run
   `brain graphify` (host-only, the monthly discovery build) **before** step 1
   — mention its cost; don't run it by default.

## Hard rules

- **Host-only.** `graph-report` and `graphify` are refused on `role=vm` (not in
  `VM_ALLOWED`), and `.brain/` is host-only by contract — a Cowork VM session
  cannot render or read this page. If `$BRAIN_ROLE=vm` (or the command returns a
  host-only refusal), say so plainly: the graph explorer is a host-side
  artifact; run this on the host, not in Cowork. Do not invent a workaround.
- **Send the file, never dump it.** The page is multi-megabyte self-contained
  HTML; always deliver it via `SendUserFile` (render), never as chat text.
- **Don't auto-run `graphify`.** It's the expensive monthly discovery build.
  `graph-report` alone gives a current view; only run `graphify` on explicit
  request.
