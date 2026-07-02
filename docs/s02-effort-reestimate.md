# S02 effort re-estimate + design/core/CLI split decision (r2-codex / r2-verify-r1)

**Date:** 2026-06-27 · **Decision owner:** S02 execution (confirmed scope from
the human checkpoint: Profile A supersedes; repo = `profile-a-brain/`).

## Why a re-estimate was required
The original plan budgeted S02 against a **fork basic-memory** baseline. The
Phase-2 hardening (r2-codex, FLEET) **removed the fork**: the core is built FROM
SCRATCH with basic-memory as a clean-room reference only. The fork budget is no
longer the baseline, and "everything downstream depends transitively on this,"
so S02 had to be re-estimated on the from-scratch budget and the design/core/CLI
split decided explicitly.

## Decision: keep S02 as ONE session, internally split into three tracks
The from-scratch surface is larger than a fork, but bounded and well-specified by
design v5 + S01's substrate spec. Rather than split S02 into three plan sessions
(which would ripple the manifest/dependency graph), S02 was executed as one
session with three **internal tracks**, each independently testable:

| Track | Deliverable | Status |
|---|---|---|
| **Design** | sqlite-vec spike + `VectorBackend`/`Embedder` adapter interfaces + fallback backends, decided BEFORE retrieval code | ✅ `docs/sqlite-vec-spike.md`, `src/brain/vectors.py`, `src/brain/embed.py` |
| **Core** | files+SQLite engine (FTS5 + vector backend, single disposable index under app-data), audit chain | ✅ `src/brain/index.py`, `notes.py`, `config.py`, `audit.py`, `core.py` |
| **CLI** | `brain` contract (search/get/recent/+) with `--json`, self-describing `--help`, deny-by-default filter before stdout, projection containment | ✅ `src/brain/cli.py`, `classification.py`, `projection.py` |

### Rationale for not splitting into 3 plan sessions
1. The tracks share one package and one test suite; splitting would add
   cross-session handoff cost without reducing risk.
2. The riskiest unknown (sqlite-vec pre-v1) was retired by the spike + adapter
   on day one, collapsing the from-scratch uncertainty.
3. The from-scratch core is **smaller** than feared: ~12 focused modules,
   stdlib-first, ~37 tests. A fork would have carried AGPL conveyance risk +
   vendoring boundary-policing cost that outweighs the from-scratch write cost.

## Revised effort (actuals vs fork baseline)
- **Fork baseline (superseded):** vendor + subprocess-boundary + import-allow-list
  policing + AGPL boundary tests. Estimated higher *ongoing* cost (boundary must
  be re-checked every release) and legal exposure on distribution.
- **From-scratch actual (this session):** design+core+CLI+tests+evidence landed
  in one session. Higher one-time write cost, **near-zero ongoing legal/boundary
  cost**, and a green code-origin gate that is cheap to keep green.

## Net
From-scratch was the right call under FLEET: comparable one-time cost, lower
lifetime cost, no AGPL conveyance risk. S02 stays a single session; the three
tracks are the internal structure, each evidenced separately. No downstream
manifest change required.
