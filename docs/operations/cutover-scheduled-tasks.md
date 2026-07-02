# Cutover — scheduled-task rewrite list

**Hook for the follow-on operational-cutover plan (NOT executed here).** The 10
live Example Corp scheduled tasks (`90 System/_maintenance_automation.md`) that touch
Smart Connections / Bases / the cascade, and the rewrite each needs. Every
rewrite MUST go through `/skill-creator` (Skill Rule) + the four-step disposition
phase + three-block report + §14 OpEx metering (outcomes contract), and is
maintainer-gated to deploy. Order: do the shared rule-B change (cascade Step 1 →
`brain search`) FIRST; most tasks inherit retrieval from it.

| Task (cron) | SC/Bases/cascade touch | Rewrite | Risk |
|---|---|---|---|
| `example-vault-health` (Mon 09:00) | §9 `mcp__smart-connections__stats`; §7 `_bases_verifier.py`; §5/§8 `.obsidian/plugins/` hash; smart-env shape via `_smoke_test_retrieval.py` | §9 → `brain status`; smart-env shape → `brain selftest`; KEEP plugin-hash (residual Obsidian/Bases core); retire Bases verifier with Bases | MED — health is the drift tripwire; dual-run both checks during overlap |
| `example-vault-integrity-scan` (Tue 09:00) | §A near-dup uses **SC embeddings** (cosine) | repoint to `brain` sqlite-vec vectors (direct, no MCP) | MED — embedding model differs (e5-small → Arctic); re-baseline near-dup bands |
| `example-vault-inbox-ingest` | writes notes → SC re-indexes | pipeline output → `brain sync` (incremental indexer is the drain) | LOW — additive |
| `example-vault-daily-check` | `_index.md` + Bases freshness | `_index.md` regen STAYS; Bases-freshness → `brain status` | LOW |
| `example-chief-of-staff-nightly` (05:00) | retrieval over the cascade | rides the rule-B change (Step 1 → `brain search`) | MED — answer quality depends on the eval gate passing first |
| `example-vault-graphify-discovery` (monthly) | discovery graph (no SC) | relate to `brain graph-expand`; likely KEEP as-is (discovery-only) | LOW |
| `example-vault-recommendations-aging` | none direct | no change | NONE |
| `example-vault-handoff-freshness` (daily) | none direct | no change | NONE |
| `example-vault-write-audit` | audit chain + git | no change (brain has its own chain; this audits the vault) | NONE |
| graph-health (folded into health §10) | scored eval | point the scored eval at this golden set + `gate.py` | LOW |

**Sequencing:** (1) land the cascade-rule change + verify the eval gate is green
on the real corpus; (2) rewrite `health` §9 + `integrity-scan` §A (the two real
SC couplings); (3) the rest inherit. **Deploy gate:** each rewritten SKILL.md
ships conforming to the eval+memory contract; deploy only after a dual-run period
shows parity (see `cutover-retirement-and-dualrun.md`).
