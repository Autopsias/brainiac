# Decisions and evidence — single-source-of-truth map

Brainiac keeps architectural decisions, operational owner rulings, accepted
security residuals, and session verification artifacts in **four separate
stores**. Each store has one job and one clean-room export posture
(`tools/export_cleanroom.py`). Put a new record in the store that matches its
job — never duplicate a decision across two stores or dump raw evidence into
an ADR.

## The four stores at a glance

| Store | Purpose & authority | Clean-room public export | Naming / format |
|---|---|---|---|
| [`docs/adr/`](adr/) | **Architecture Decision Records.** Durable technical design contracts explaining *why* a subsystem is built the way it is (context, options considered, ruling, consequences). | **Public by default** — except private/COS ADRs explicitly excluded in `tools/export_cleanroom.py` (`0003-parity-architecture.md`, `0013-public-export-excludes-cos.md`, `0014-sheet-session-listener.md`, `0015-cos-extraction-and-off-mount-state.md`). | Sequential 4-digit prefix: `NNNN-kebab-title.md` (zero duplicate prefixes). |
| `_decisions/` | **Private owner rulings.** Plan-time policy or ship/no-ship rulings made by the owner against live corpus or eval data (e.g. cross-tier rulings, fusion weights, invariant closure rulings). | **Never exported** (`EXCLUDE_PREFIXES` in `tools/export_cleanroom.py`). May cite private corpus metrics or internal plan context. | `<plan-or-topic>-<session>-<slug>.md` |
| [`docs/security-acceptances.md`](security-acceptances.md) | **Accepted residual security findings (`A-01`..`A-NN`).** Risks a security reviewer or static scanner will raise again, paired with the exact scope accepted, measured controls, and owner ruling date. | **Public** — ships in the clean-room export so external auditors and scanners see why each residual stays. Must never contain real client/counterparty identifiers. | Numbered `## A-NN · <title>` sections in one canonical file. |
| `_evidence/` | **Immutable verification artifacts.** Raw command outputs, before/after diffs, test logs, benchmark runs, and probe transcripts captured during plan sessions to prove a change worked. | **Never exported** (`EXCLUDE_PREFIXES` in `tools/export_cleanroom.py`; sibling `docs/operations/*-evidence.md` files are also dropped by `EXCLUDE_SUFFIXES`). | `_evidence/<plan-or-session>/<artifact>.{txt,md,json}` |

## Choosing the right store

1. **You changed system architecture, protocol, or a cross-module invariant** →
   write or amend an ADR in `docs/adr/NNNN-<slug>.md` using the next unused
   4-digit number.
2. **The owner made a plan-time judgment call about live vault data, eval
   thresholds, or rollout scope** → record it in `_decisions/` (private). If
   the ruling also changes a durable public design contract, summarize the
   generic contract in `docs/adr/` without copying private corpus identifiers.
3. **A security review or scanner surfaced a residual risk the owner accepts**
   → add or update an `A-NN` entry in `docs/security-acceptances.md` (sanitized
   for public export). False positives from static scanners (ruff/bandit/semgrep)
   go in [`docs/SECURITY_NOTES.md`](SECURITY_NOTES.md) instead.
4. **You ran a probe, benchmark, migration check, or verification gate** →
   save the raw output under `_evidence/` (never inside `docs/adr/` or
   `docs/security-acceptances.md`, which may cite the evidence path).

## Relationship to `AGENTS.md` and `docs/closed-plans-history.md`

- `AGENTS.md` is the **active operational contract** every assistant loads at
  startup. It links to ADRs for rationale rather than duplicating them.
- [`docs/closed-plans-history.md`](closed-plans-history.md) holds historical
  plan summaries moved out of `CLAUDE.md` to keep startup context lean; it is
  an archive, not a decision authority.
