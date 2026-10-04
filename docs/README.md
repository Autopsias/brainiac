# Brainiac documentation — map

Every document, grouped by the job it does for you. The **Kind** column follows
[Diátaxis](https://diataxis.fr/): a *how-to* gets a task done, a *reference*
states facts to look up, an *explanation* gives the mental model, and a
*walk-through* teaches by leading you through the steps. **Language** says who
the page is written for: a general reader (Plain), an engineer (Technical), or
both (Mixed). HTML pages open in a browser; Markdown pages read on GitHub.

## Start here

| Doc | Kind | Language | What it is |
|---|---|---|---|
| [`../README.md`](https://github.com/Autopsias/brainiac/blob/main/README.md) | how-to | Plain | What Brainiac is and the fastest way to install it |
| [`install/README.md`](install/README.md) | how-to | Mixed | Install steps for every platform: Claude Code, Cowork, Codex, Gemini, Windows, npx, the Chat tab |
| [`install/LLM-INSTALL.md`](install/LLM-INSTALL.md) | how-to | Plain | The runbook an AI assistant follows to install Brainiac for you |
| [`install-guide.html`](install-guide.html) | walk-through | Plain | The install as a guided page with a decision tree and diagrams |
| [`install/new-owner.md`](install/new-owner.md) | explanation | Plain | What runs where, what `brain init` does, and why Cowork needs a host |

## Install and operate

| Doc | Kind | Language | What it is |
|---|---|---|---|
| [`install/ai-install.md`](install/ai-install.md) | how-to | Plain | The Claude Code plugin path in three commands, plus update and removal |
| [`install/second-vault.md`](install/second-vault.md) | how-to | Plain | Point the same install at another vault, with `brain provision-local` |
| [`install/cowork.md`](install/cowork.md) | how-to | Mixed | Set up and verify the Cowork read-and-draft leg |
| [`install/cowork-session-prompt.md`](install/cowork-session-prompt.md) | reference | Plain | The fallback prompt for a Cowork project's custom instructions |
| [`cowork-windows-install.md`](cowork-windows-install.md) | reference | Technical | The Cowork runtime: its five rules, the capture loop, the staging commands |
| [`managed-deployment-runbook.html`](managed-deployment-runbook.html) | how-to | Technical | Copy-paste steps for a managed, hardened fleet install |
| [`../plugins/brainiac-manager/README.md`](../plugins/brainiac-manager/README.md) | reference | Plain | What the installer plugin runs, downloads and registers |
| [`../plugins/brainiac-kernel/README.md`](../plugins/brainiac-kernel/README.md) | reference | Plain | The daily-use skills plugin |
| [`../plugins/brainiac-extras/README.md`](../plugins/brainiac-extras/README.md) | reference | Plain | The optional maintenance skills plugin |
| [`../packaging/npm/brainiac-install/README.md`](../packaging/npm/brainiac-install/README.md) | reference | Mixed | The `npx brainiac-install` bootstrap: what it does and never does |
| [`release-runbook.md`](release-runbook.md) | how-to | Technical | How a release is cut, tagged and published |

## Understand it

| Doc | Kind | Language | What it is |
|---|---|---|---|
| [`architecture-overview.html`](architecture-overview.html) | explanation | Plain | Components, data flows and the trust model, with diagrams |
| [`security-overview.html`](security-overview.html) | explanation | Mixed | The controls, the threat model and the residual-risk list |
| [`deployment-authorization-memo.html`](deployment-authorization-memo.html) | explanation | Plain | The sign-off decision for an organisation: authorize, conditions, who signs |
| [`../AGENTS.md`](https://github.com/Autopsias/brainiac/blob/main/AGENTS.md) | reference | Mixed | The contract every assistant reads: note shape, the four verbs, capture rules, security posture |
| [`substrate-spec.md`](substrate-spec.md) | reference | Technical | The normative spec: zones, protocol, egress gate, validation |
| [`classification-scheme.md`](classification-scheme.md) | reference | Mixed | The five classification tiers and the deny-by-default rule |
| [`harness-wiring.md`](harness-wiring.md) | reference | Technical | Which client reads which file, and how `brain connect` wires each one |
| [`glossary.md`](glossary.md) | reference | Plain | One-line definitions of the words these docs use |
| [`adr/`](adr/) | explanation | Technical | Decision records: why things are the way they are |

## Security and operations reference

| Doc | Kind | What it is |
|---|---|---|
| [`../SECURITY.md`](https://github.com/Autopsias/brainiac/blob/main/SECURITY.md) | reference | Vulnerability reporting, supported versions, audit-key rotation |
| [`SECURITY_NOTES.md`](SECURITY_NOTES.md) | reference | Triaged static-scanner findings, per site |
| [`security-acceptances.md`](security-acceptances.md) | explanation | Accepted residual risks, each with its reasoning |
| [`security/`](security/README.md) | reference | What may be published under `docs/security/`, and the worked example |
| [`session-memory.md`](session-memory.md) · [`ingestion.md`](ingestion.md) · [`deliverables-shelf.md`](deliverables-shelf.md) | reference | Session memory, the ingestion lanes, the deliverables shelf |
| [`operations/`](operations/) | how-to | Runbooks: macOS notarization, Windows packaging, the owner drain, the off-host watchdog |
| [`operations/codex-synthesis.md`](operations/codex-synthesis.md) | how-to | Opt-in Codex Sunday source-linking adapter, scope, consent and launchd setup |
| [`install/plugin-distribution.md`](install/plugin-distribution.md) | explanation | The original plugin-distribution design note (partly superseded; kept for the ADRs that cite it) |

---

**Reading order by role.** *Installing it:* README → `install/README.md` →
`install/new-owner.md`. *Reviewing it for an organisation:*
`architecture-overview.html` → `security-overview.html` →
`deployment-authorization-memo.html` → `managed-deployment-runbook.html`.
*Building on it:* `AGENTS.md` → `substrate-spec.md` → `harness-wiring.md` →
`adr/`.
