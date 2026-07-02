# Egress controls + provider posture (SEC-01)

**Session:** S08 · **Date:** 2026-06-27 · Design of record: `_design_profile_a_architecture_v5` §3.

Egress — the model call — is where vault content actually leaves the machine, so
this is the **primary** security workstream (design v5 §3). At-rest is FDE
(`at-rest-posture.md`); the budget goes here.

## 1 · Break the lethal trifecta per execution path

The lethal trifecta = (untrusted content) + (private data) + (outbound channel)
in one agent leg. We break it **structurally**, per execution path:

| Execution path | Untrusted content? | Private data reach | Outbound channel | How the trifecta is broken |
|---|---|---|---|---|
| **Cowork VM leg** (`--role vm`) | yes (ingested/raw, MCP/tool output) | **NO** — reads only the published read-only **snapshot of a projected workspace** (sensitive tiers physically absent) + cannot resolve a signing key | the model (vendor channel) | the leg that reads untrusted content has **no private data** (projection omits Confidential/Restricted/Secret + unlabelled) and **no write/sign privilege** (host-broker only) |
| **Host retrieval leg** | yes | yes (full vault) | the model | the **classification gate** (deny-by-default) caps what `brain` will surface; sensitive tiers require an explicit `--max-tier` **human gate** |
| **Host write/commit** (`write_note`) | — | yes | — | **human-gated + audited** (Ed25519 chain, fails closed without a key); not an agent-facing verb |

The retrieval verbs are **read+draft only**; the one irreversible/outbound action
(commit) is host-broker + audited. The VM (the leg most exposed to untrusted
content) is deliberately the leg with the *least* privilege.

## 2 · Classification gate at the send boundary (deny-by-default)

`brain` declines to surface the most sensitive subset at stdout — the egress
decision point. Tiers low→high: `Public < Internal < Confidential < Restricted
< Secret`. An unlabelled/unrecognised note is treated as **Secret** (default-deny).
Default cap is **Internal**; elevation (`--max-tier`) is the explicit human gate.

**Single chokepoint (SEC-01, r2-codex).** EVERY content-returning subcommand —
`search`/`hybrid-search` (incl. `--rerank`), `grep`, `bases-query`,
`graph-expand`, `get`/`read`, `recent` — routes through ONE helper
(`brain.egress.apply_gate`), shared by the CLI and the optional MCP adapter. A
new content path cannot silently bypass a gate a sibling path enforces. Proven
per-subcommand in `_evidence/s08/egress-per-subcommand.txt` +
`tests/test_egress_per_subcommand.py` (the canonical list is asserted exhaustive).

**This is an egress *decision*, not containment (C-3).** A file-capable harness
reads the Markdown directly and bypasses the filter — proven by
`tests/test_direct_file_read.py`. Real containment of sensitive tiers is
**workspace projection** (`brain project`, physically omits the tiers) + the
host/VM trust split. Per the vault's own C-3 doctrine, a CLI/prompt-layer filter
is defence-in-depth, never the gate.

### Importable-core bypass — resolved posture (HARDENED:claude)

`BrainCore.search/get/recent` return **UNFILTERED** results by design — importing
the core in-process bypasses the gate. Resolution:

- The gate decision lives **at every shipped integration surface** (CLI + MCP),
  funnelled through the single `egress.apply_gate` chokepoint. Untrusted harnesses
  reach the engine **only via the gated CLI boundary or a projected workspace**,
  never by importing the core.
- In-process import is a **trusted-host-only** path. On the host, FDE already
  decrypts every tier to a process running as the user, so an import-level filter
  would add no real containment — the honest control for untrusted code is
  **projection** (physical exclusion), proven by `test_direct_file_read.py`.
- Therefore the posture is: **untrusted code is contained by projection + the VM
  split, not by import-level filtering.** This is the C-3-consistent answer
  ("a CLI/prompt-layer filter is not containment").

## 3 · Human-in-the-loop (HITL)

- **Surfacing sensitive content** (`> Internal`) requires an explicit `--max-tier`
  elevation — a human decision, not a default.
- **`write_note`** (the one irreversible/outbound commit) is a **host-broker
  privilege**, not an agent verb; it Ed25519-signs the audit chain and **fails
  closed** without a key. VM-side capture is a *draft* only (host drains + signs).

## 4 · Provider posture — VERIFY per vendor (HARDENED:claude)

**We hold NO model API keys.** Egress rides the **desktop app vendor's enterprise
agreement** (GV.SC). The openness thesis invites *multiple* harnesses — and each
vendor is a **DISTINCT contract**: OpenAI (Codex), Google (Gemini CLI), Anthropic
(Claude). A no-train/ZDR claim for one says nothing about another, and "the
desktop app has enterprise terms" does not automatically cover **tool-call/API
egress of vault content and Secret specifically**.

**Posture rule (machine-readable register: `docs/harness-allowlist.json`):**

- Openness ≠ "any app." A harness is **ALLOWED** to run against the **full vault**
  only when its vendor's no-train/ZDR scope is **contractually VERIFIED** to cover
  tool-call/API egress of vault content **and** Secret (the bar is the register's
  `bar` array).
- Until VERIFIED, a harness is **PENDING → default-deny**: it may run **only
  against a projected, sensitive-tier-free workspace** (`brain project --max-tier
  Internal`). REJECTED is the same restriction, permanently.
- **val-03's cross-harness test set MUST equal the VERIFIED subset** of this
  register (not an arbitrary "any app" list).

**Current state (all three PENDING — we do NOT assert coverage we cannot confirm):**

| Harness | Vendor | Bar met? | Verification step | Owner | Lead |
|---|---|---|---|---|--:|
| claude-desktop | Anthropic | **PENDING** | Example Corp Legal confirm the signed Anthropic enterprise agreement's no-train + ZDR clauses cover tool-call egress of vault content + Secret | maintainer / Example Corp Legal | ~3 wks |
| codex-cli | OpenAI | **PENDING** | Confirm OpenAI tenant no-train default applies to Codex CLI tool-call egress + ZDR covers Secret | maintainer / Example Corp Cyber | ~3 wks |
| gemini-cli | Google | **PENDING** | Confirm the Workspace/Vertex tier is a no-train **paid** tier (NOT consumer Gemini) + DPA covers tool-call egress + Secret | maintainer / Example Corp Cyber | ~3 wks |

> **We have NO live access to the signed contracts in this build.** Where coverage
> cannot be verified, the verification step + owner are stated explicitly and the
> default is the **allowlist (projection) posture** — never an asserted "covered."
> When a contract is verified, flip that entry's `posture_status` to `VERIFIED`
> and re-run the allowlist tests. The cyber-review (PW-1) confirms the verifications.

## 5 · MCP supply-chain (only if the optional adapter is used)

The optional Chat-tab MCP adapter (`brain.mcp_adapter`) shares the SAME chokepoint
and exposes ONLY read verbs. MCP/connector responses are **untrusted-in** — data,
never instructions; a tool response may never auto-trigger a privileged action.
Pin the adapter dependency if the adapter is deployed.

**Server-side egress ceiling (hardening pass).** The CLI's `--max-tier` elevation
is a human-gated flag someone typed on a terminal; the MCP transport has no
equivalent "a person is watching this request" signal. A caller-supplied
`max_tier` argument to the adapter is therefore now **clamped server-side** to
`min(requested_rank, ceiling_rank)`, where the ceiling is read from
`$BRAIN_MAX_EGRESS_TIER` (default `Internal`, unset/unrecognised fails closed to
the default — never fails open). A caller can always request something
*narrower* than the ceiling; it can never request higher just by asking. This
closes a real gap: previously any MCP client could pass `max_tier="Secret"` and
receive it with no operator control at all.

**This clamp is still a decision, not containment (C-3) — same caveat as §2.**
A compromised or malicious MCP client's *process* can still read the Markdown
directly off disk if it has filesystem access; the clamp only bounds what the
adapter itself will hand back through the tool-call response. For that reason
**the MCP path should run against a *projected* (sensitive-tier-free) index**
(`brain project --max-tier Internal`, see §4's projection posture) wherever the
Chat-tab surface is exposed to a harness whose vendor posture is not yet
`VERIFIED` — the ceiling clamp and physical projection are complementary, not
either/or.
