# Query-variant entry points — where the §5 variant contract can and cannot reach

**CON-01, 2026-08-09.** No engine default can manufacture a translation. If a
harness does not read the language census, translate, and pass variants, it
stays single-query no matter what the engine defaults to — so "variants by
default" is a property of the CALLER layer, and this file is the audit of that
layer. Every place a `brain` search can be issued is listed: either it can
carry variants (with the end-to-end test that proves it does) or it is
recorded as out of reach, with the reason.

The contract itself is `AGENTS.md` §5 retrieval discipline, rule 3. The census
that switches it on is `brain.language` (`brain status --json` ->
`index.languages`).

---

## 1 · Reachable — variant-capable, with an end-to-end proof

| Entry point | Surface | Proof |
|---|---|---|
| `brain search` / `brain hybrid-search` (host) | repeatable `--variant TEXT` -> `core.search_multi` (`src/brain/cli.py`) | `tests/test_language_census.py::test_route_cli_submits_more_than_one_variant` + its single-query control |
| The same two verbs on the **Cowork VM leg** | `search` / `hybrid-search` are in `VM_ALLOWED` (`src/brain/cli.py`) and take the identical code path — no role branch exists between the flag and `search_multi` | same test (the CLI path IS the VM path; the role gate only decides whether the verb runs at all) |
| **MCP adapter** (`src/brain/mcp_adapter.py`) — the Desktop Chat tab | `search(query, variants=[...])`, plus a `vault_languages` tool so the one surface that cannot run a shell command can still READ the census | `::test_route_mcp_adapter_submits_more_than_one_variant`, `::test_route_mcp_adapter_publishes_the_census` |
| **Skills that search** — `promote`, `save-conversation`, `vault-ingestion`, `vault-eval`, `kb-curator`, `autoresearch` | each carries the same one-paragraph contract, verbatim, so a grep proves it landed | `grep -c "variant contract (AGENTS.md §5 rule 3)" .claude/skills/*/SKILL.md` |

Before CON-01 the MCP adapter had **no variant parameter at all**
(`def search(query: str, k: int = 10, max_tier: str = …)`), so the Chat tab
could not issue variants under any engine default. That was the hole; it is
closed.

## 2 · Out of reach — named, with the reason

| Entry point | Why it cannot carry variants |
|---|---|
| `.claude/skills/chief-of-staff/SKILL.md` (the 7th searching skill) | **Not a technical limit — a concurrency one.** A concurrent session held the file (and its three generated mirrors) during this session, and s03 was instructed not to touch it. Its search guidance is a single line; adding the same block is a one-paragraph follow-up, listed in the s03 closeout. |
| `brain dossier` (CLI + MCP) | `core.dossier` takes one query and splits the result by `type`, not a ranking fan-out. Extending it means fanning out INSIDE the decision/source split — a ranking change to the decision layer, out of this plan's scope. Agents needing variants on a decision-state question run `dossier` first and a `search --variant` pass beside it. |
| `brain diagnose` | Deliberately single-query: it exists to explain how ONE query ranked through the production stages. A fan-out would have no single ranking to explain. |
| `brain grep`, `bases-query`, `graph-expand` | Not text-relevance queries at all — a literal/regex scan, a structured frontmatter filter, and a wikilink walk. Variants are meaningless for all three; language never enters their matching. |
| `maintenance.py` self-test probe (`hybrid_search("brain", k=1)`) | A fixed-string latency probe, not a retrieval question. Fanning it out would measure something different every night. |
| `golden_probe.py` decision-state probes | Fixed probe queries whose whole purpose is comparability across runs; changing what they issue invalidates the trend. |
| `eval/capture_run.py` (driven by `autoresearch`) | Measurement harness. Variants are an ARM of the s04 measurement, deliberately set by that session, never a default here. |

## 3 · What fraction of real query traffic the reachable routes represent

**Measured, not estimated — 89.9%.** Host-side query capture is ON by default
(`BRAIN_QUERY_CAPTURE_ENABLED`, unset = enabled; the VM leg never captures), so
the reference deployment has a real per-mode ledger under
`<index dir>/query-log/`. Counting `mode` over every captured row (2026-07 and
2026-08 files, 119 rows, taken 2026-08-09 — counts only; no query text left the
host):

| mode | rows | share | variant-capable |
|---|--:|--:|---|
| `search` | 107 | 89.9% | **yes** (CLI + MCP) |
| `dossier` | 12 | 10.1% | no — see §2 |

So the routes that can carry variants account for **~90% of real query
traffic**, and the entire remainder is one verb: `dossier`. Two caveats a ship
ruling should carry:

- The ledger records what reached the CAPTURE seam (CLI and MCP `search` /
  `dossier`); the fixed-query instruments in §2 do not appear in it, which is
  correct — they are not questions anyone asked.
- 119 rows is a small denominator from one deployment. It is enough to say
  `dossier` is a real but minority share, not enough to put a tight interval
  on it.

Structurally, every interactive harness on this deployment reaches a
variant-capable surface: Claude Code, Codex, Gemini CLI, the Desktop *Code* tab
and the Cowork VM all shell out to the `brain` CLI
(`docs/harness-wiring.md`), and the Chat tab — the sole MCP consumer — now has
the parameter it lacked. **"Ship as default" is therefore a claim about ~90% of
traffic, not 100%**, and closing the last 10% means teaching `dossier` to fan
out, which is a change to the decision layer and belongs to its own session.
