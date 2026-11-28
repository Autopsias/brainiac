# Brainiac documentation — map

Every document, grouped by the job it does for you. The **Kind** column follows
[Diátaxis](https://diataxis.fr/): a *how-to* gets a task done, a *reference*
states facts to look up, an *explanation* gives the mental model, and a
*walk-through* teaches by leading you through the steps. **Language** says who
the page is written for: a general reader (Plain), an engineer (Technical), or
both (Mixed). HTML pages open in a browser; Markdown pages read on GitHub.

Public reference docs ship in the clean-room export (`Autopsias/brainiac`);
private/operational docs (COS specs and owner-corpus migration records) stay in
the private repo per `tools/export_cleanroom.py`.

## Start here

| Doc | Kind | Language | What it is |
|---|---|---|---|
| [`../README.md`](https://github.com/Autopsias/brainiac/blob/main/README.md) | how-to | Plain | What Brainiac is and the fastest way to install it |
| [`install/README.md`](install/README.md) | how-to | Mixed | Install steps for every platform: Claude Code, Cowork, Codex, Gemini, Windows, npx, the Chat tab |
| [`install/LLM-INSTALL.md`](install/LLM-INSTALL.md) | how-to | Plain | The runbook an AI assistant follows to install Brainiac for you |
| [`install-guide.html`](install-guide.html) | walk-through | Plain | The install as a guided page with a decision tree and diagrams |
| [`install/new-owner.md`](install/new-owner.md) | explanation | Plain | What runs where, what `brain init` does, and why Cowork needs a host |

## Install and operate (public)

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
| [`repo-hygiene.md`](repo-hygiene.md) | how-to | Technical | Maintainer runbook for cleaning plan worktrees and security-scan artifacts |
| [`soak-runbook.md`](soak-runbook.md) | how-to | Technical | Throwaway soak-vault runbook for end-to-end validation |

## Understand it (public reference & explanation)

| Doc | Kind | Language | What it is |
|---|---|---|---|
| [`architecture-overview.html`](architecture-overview.html) | explanation | Plain | Components, data flows and the trust model, with diagrams |
| [`security-overview.html`](security-overview.html) | explanation | Mixed | The controls, the threat model and the residual-risk list |
| [`deployment-authorization-memo.html`](deployment-authorization-memo.html) | explanation | Plain | The sign-off decision for an organisation: authorize, conditions, who signs |
| [`../AGENTS.md`](https://github.com/Autopsias/brainiac/blob/main/AGENTS.md) | reference | Mixed | The contract every assistant reads: note shape, the four verbs, capture rules, security posture |
| [`substrate-spec.md`](substrate-spec.md) | reference | Technical | The normative spec: zones, protocol, egress gate, validation |
| [`substrate-readiness.md`](substrate-readiness.md) | reference | Mixed | Why substrate readiness is distinct from operational cutover |
| [`classification-scheme.md`](classification-scheme.md) | reference | Mixed | The five classification tiers and the deny-by-default rule |
| [`harness-wiring.md`](harness-wiring.md) | reference | Technical | Which client reads which file, the 17-tool `brain-mcp` desk parity matrix, and repo hooks |
| [`query-variant-entry-points.md`](query-variant-entry-points.md) | reference | Technical | Where the §5 multilingual query-variant contract reaches across CLI, MCP, and skills |
| [`okf-lint-profile.md`](okf-lint-profile.md) | reference | Technical | Optional Open Knowledge Format (OKF) lint profile and boundary adapter |
| [`glossary.md`](glossary.md) | reference | Plain | One-line definitions of the words these docs use |
| [`decisions-and-evidence-map.md`](decisions-and-evidence-map.md) | reference | Mixed | Single-source-of-truth map across `docs/adr/`, `_decisions/`, `docs/security-acceptances.md`, and `_evidence/` |
| [`adr/`](adr/) | explanation | Technical | Decision records: why things are the way they are |

## Security, operations, and historical records (public)

| Doc | Kind | Language | What it is |
|---|---|---|---|
| [`../SECURITY.md`](https://github.com/Autopsias/brainiac/blob/main/SECURITY.md) | reference | Mixed | Vulnerability reporting, supported versions, audit-key rotation |
| [`SECURITY_NOTES.md`](SECURITY_NOTES.md) | reference | Technical | Triaged static-scanner findings, per site |
| [`security-acceptances.md`](security-acceptances.md) | explanation | Mixed | Accepted residual risks (`A-01`..`A-NN`), each with its reasoning |
| [`security/`](security/README.md) | reference | Technical | What may be published under `docs/security/`, and the worked example |
| [`session-memory.md`](session-memory.md) · [`ingestion.md`](ingestion.md) · [`deliverables-shelf.md`](deliverables-shelf.md) | reference | Technical | Session memory, the ingestion lanes, the deliverables shelf |
| [`operations/`](operations/) | how-to | Technical | Runbooks: macOS notarization, Windows packaging, the owner drain, the off-host watchdog |
| [`operations/codex-synthesis.md`](operations/codex-synthesis.md) | how-to | Technical | Opt-in Codex Sunday source-linking adapter, scope, consent and launchd setup |
| [`install/plugin-distribution.md`](install/plugin-distribution.md) | explanation | Technical | The original plugin-distribution design note (partly superseded; kept for the ADRs that cite it) |
| [`clean-room-log.md`](clean-room-log.md) | reference | Technical | Clean-room provenance log documenting the `basic-memory` AGPL boundary |
| [`sqlite-vec-spike.md`](sqlite-vec-spike.md) | reference | Technical | Early `sqlite-vec` dependency spike and validation record |
| [`benchmark-results.md`](benchmark-results.md) | reference | Technical | Historical BEIR SciFact benchmark record (retired embedding/reranker stack) |
| [`closed-plans-history.md`](closed-plans-history.md) | reference | Mixed | Historical closed-plan notes archived out of `CLAUDE.md` |
| [`substrate-history.md`](substrate-history.md) | reference | Technical | Historical rationale, incident post-mortems, and benchmark deltas archived out of `AGENTS.md` and `cli_help.py` |

## Private & operational docs (excluded from public clean-room export)

Private operational docs exist only in the private repository and are excluded from public clean-room exports per `tools/export_cleanroom.py`.

---

**Reading order by role.** *Installing it:* README → `install/README.md` →
`install/new-owner.md`. *Reviewing it for an organisation:*
`architecture-overview.html` → `security-overview.html` →
`deployment-authorization-memo.html` → `managed-deployment-runbook.html`.
*Building on it:* `AGENTS.md` → `substrate-spec.md` → `harness-wiring.md` →
`decisions-and-evidence-map.md` → `adr/`.
