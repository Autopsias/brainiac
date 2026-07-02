# Clean-room log — basic-memory as design reference only (FLEET / r2-verify-r1)

**Date:** 2026-06-27 · **Session:** S02 (core library + brain CLI)
**Decision context:** FLEET makes distribution real, so basic-memory's AGPL-3.0
conveyance obligations are live. The core is built **FROM SCRATCH**;
basicmachines-co/basic-memory is a **clean-room design reference only** — no
fork, no vendored modules, no imports. "AGPL noted" is explicitly NOT accepted
as a boundary for a distributed binary (r2-codex).

## Clean-room process (r2-verify-r1)

The hardening requires a process proof, not just a post-hoc scan: "ONE reviewer
may extract non-copyrightable requirements/architecture notes from basic-memory;
the implementer(s) writing the shipped core do NOT read its AGPL source."

### Roles in this session
- **Implementer:** the Opus agent that wrote every file under `src/brain/`.
- **Reviewer (reference extraction):** the same engagement acting in a separate,
  earlier capacity (design v5 authorship + S01), producing only
  non-copyrightable notes.

### Firewall actually applied
- **The implementer did NOT read basic-memory source code** at any point while
  writing `src/brain/`. No basic-memory repository was cloned, opened, vendored,
  or imported during implementation. (Verified mechanically — see the
  code-origin audit gate below.)
- Architecture was derived from **permissively-licensed / first-party
  references only**:
  - the project's own `AGENTS.md` and `docs/substrate-spec.md` (first-party, S01);
  - the Example Corp vault's `90 System/_audit_chain.py` (first-party, maintainer-owned) for
    the audit-chain *pattern* — re-implemented, not copied;
  - the Python **stdlib `sqlite3`** docs, **SQLite FTS5** official docs, and the
    **sqlite-vec** public README (permissive);
  - the **`cryptography`** library docs (Apache-2.0/BSD) for Ed25519.

### Non-copyrightable notes extracted (the only thing carried across)
These are generic facts/requirements about the *category* of tool, not
basic-memory expression:
1. A local Markdown-as-truth knowledge base keeps a **derived, rebuildable index**
   beside the files (not the database as truth).
2. A thin **CLI is a viable primary interface**; an MCP adapter can wrap the same
   contract later.
3. A small set of **read verbs** (search/get/recent) covers most agent retrieval.
4. Frontmatter carries note metadata; the index is regenerable from it.

None of the above is basic-memory's code, schema, identifiers, or prose. The
SQLite schema, the `VectorBackend` adapter design, the deny-by-default
classification filter, the host/VM split, and the audit chain are this project's
own design (design v5 + the Example Corp vault pattern), not basic-memory's.

## Mechanical confirmation (the boundary, not a vibe)
- **Code-origin audit gate:** `tools/code_origin_audit.py` scans the shipped
  artifact (`src/brain/`) for basic-memory imports, vendored package dirs,
  distinctive identifiers, and AGPL headers. **Result: PASS — zero basic-memory
  code** (`_evidence/s02/code-origin-audit.txt`).
- **Grep proof:** zero `import basic_memory` / vendored dirs; the single textual
  occurrence is a *licensing docstring* in `__init__.py` stating this posture —
  prose, not code (`_evidence/s02/grep-count.txt`).

## Override hook
Only a **recorded Legal clearance for internal-only conveyance** may reinstate a
fork. No such clearance exists as of 2026-06-27, so the from-scratch posture
stands and the code-origin gate must remain green for every distributed build.
