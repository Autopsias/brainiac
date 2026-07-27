---
name: graph-explorer
description: "Open THIS vault's visual graph explorer — the self-contained HTML page (WebGL link graph + 3D semantic map) at .brain/graph/graph-explorer.html — by re-rendering it fresh from the live index and sending it to the user to view. Triggers: \"show me the graph\", \"open the vault explorer\", \"graph report\", \"graph explorer\", \"visualize the vault\", \"semantic map\", \"link graph\", \"see the vault structure\", \"where's the html graph view\". Host-only: `brain graph-report` is refused on role=vm, so in a Cowork VM session this skill reports that the view is a host-side artifact instead. NOT for the graphify discovery build (rebuilding inferred edges — that's `brain graphify`, monthly/host), and NOT for `brain health-report` (the text health readout, which only LINKS to this page)."
---

# graph-explorer (open the vault's visual graph + semantic map)

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
