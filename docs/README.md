# Brainiac documentation — map

Everything here, grouped by what you're trying to do. **HTML docs** open in a
browser (dark/light aware, printable); **MD docs** read on GitHub or in an
editor. "Language" says whether a doc is written for a general reader (Plain),
an engineer (Technical), or both (Mixed).

## Start here

| Doc | For | Language | What it is |
|---|---|---|---|
| [`../README.md`](../README.md) | everyone | Plain | What Brainiac is and the fastest way to install it |
| [`install-guide.html`](install-guide.html) | installing it yourself | Plain | End-to-end install — new machine, existing machine, every path, with diagrams |
| [`install/LLM-INSTALL.md`](install/LLM-INSTALL.md) | "just do it for me" | Plain | Paste-and-go runbook your AI assistant executes end-to-end |
| [`../AGENTS.md`](../AGENTS.md) | AI assistants + power users | Mixed | The canonical contract: note shape, the four verbs, capture rules, security posture |

## Install & operate

| Doc | For | Language | What it is |
|---|---|---|---|
| [`install/README.md`](install/README.md) | picking your platform | Mixed | Platform-by-platform matrix (Claude Code · Cowork · Codex · Gemini) |
| [`install/second-vault.md`](install/second-vault.md) | a 2nd/3rd project | Plain | Point the same install at another vault — no reinstall |
| [`install/cowork.md`](install/cowork.md) | Claude Desktop Cowork | Mixed | Set up the read+draft sandbox leg |
| [`managed-deployment-runbook.html`](managed-deployment-runbook.html) | IT / endpoint ops | Technical | Copy-paste MDM steps for a hardened, managed-fleet install |

## Understand it — for architecture & security teams

| Doc | For | Language | What it is |
|---|---|---|---|
| [`architecture-overview.html`](architecture-overview.html) | IT architects | Plain | Components, data flows, trust model — in plain language with diagrams |
| [`security-overview.html`](security-overview.html) | cyber / security | Mixed | The controls, threat model, and an honest residual-risk list |
| [`deployment-authorization-memo.html`](deployment-authorization-memo.html) | approvers | Plain | The sign-off decision: authorize / conditions / who signs |
| [`substrate-spec.md`](substrate-spec.md) | engineers | Technical | The normative spec — zones, protocol, gate, validation |
| [`classification-scheme.md`](classification-scheme.md) | anyone touching tiers | Mixed | The five egress tiers + the deny-by-default rule |
| [`glossary.md`](glossary.md) | everyone | Plain | One-line definitions for the jargon (PARA, MNPI, egress gate, Cowork, …) |

## Reference & operations

| Doc | What it is |
|---|---|
| [`harness-wiring.md`](harness-wiring.md) | Which client reads which file / uses which surface (per-harness matrix) |
| [`../SECURITY.md`](../SECURITY.md) | Vulnerability reporting, supported versions, audit-key rotation |
| [`SECURITY_NOTES.md`](SECURITY_NOTES.md) | Triaged static-scanner findings (accepted false positives, per site) |
| [`session-memory.md`](session-memory.md) · [`ingestion.md`](ingestion.md) · [`corpus-migration.md`](corpus-migration.md) | Operational internals |
| [`adr/`](adr/) · [`release-runbook.md`](release-runbook.md) · [`dependency-inventory.md`](dependency-inventory.md) | Decision records + release/dependency process |

---

**Reading order by role.** *Installing it:* README → install-guide.html → (managed
fleet? managed-deployment-runbook.html). *Reviewing it:* architecture-overview.html
→ security-overview.html → deployment-authorization-memo.html. *Building on it:*
AGENTS.md → substrate-spec.md → harness-wiring.md.
