# S02 evidence — Core library + brain CLI

**Session:** S02 (CORE-01 / CORE-02 / CORE-03) · **Date:** 2026-06-27
**Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Model note:** planned model Fable was unavailable; executed on Opus. The
cross-model review gate was satisfied by dispatching the second-model review on
**Sonnet** (a different model than Opus) — see `_evidence/s02/second-model-review.md`.

This is the evidence index the plan's `require_evidence` gate consumes. Every
artifact below exists on disk and is non-empty.

## What shipped (design v5 §1–2)

The `brain` engine — the universal foundation (the CLI, **not** MCP):

- **CORE-01 — files + SQLite core (sqlite-vec + FTS5), from scratch.**
  `src/brain/{config,frontmatter,notes,vectors,embed,index}.py`. Markdown is
  truth; a single derived **disposable** index lives under per-user **app-data**
  (`%LOCALAPPDATA%` / `~/Library/Application Support` / `$XDG_DATA_HOME`) — never
  a Controlled-Folder-Access path. Retrieval depends only on the `VectorBackend`
  adapter; `sqlite-vec` is used when loadable, else a pure-Python brute-force
  fallback (SQLCipher-safe). Delete-and-rebuild is always safe.
- **CORE-02 — brain CLI contract + deny-by-default classification filter.**
  `src/brain/{cli,classification,projection}.py`. `search/get/recent` (+ rebuild/
  project/write/verify-audit) with `--json` and a self-describing `--help`. The
  filter is the **final stage before stdout**; unlabelled ⇒ Secret ⇒ withheld. The
  core is importable but **not** the integration surface (in-process bypasses the
  filter — by design).
- **CORE-03 — Ed25519 audit hash-chain on the write path.**
  `src/brain/{audit,core}.py`. Signing key from OS Keychain / Credential Manager
  / injected env PEM — **no file fallback**; the write path **fails closed**
  (no key ⇒ no write). `verify-audit` walks prev_hash + signatures + byte-canonicality.

## Hardening obligations → where satisfied

| Hardening tag | Obligation | Evidence |
|---|---|---|
| r2-codex (FLEET) | from-scratch, no fork/vendor/import; code-origin gate | `tools/code_origin_audit.py`, `_evidence/s02/code-origin-audit.txt` (PASS), `_evidence/s02/grep-count.txt` |
| r2-verify-r1 | clean-room process proof | `docs/clean-room-log.md` |
| codex (spike) | sqlite-vec spike + adapter + fallback BEFORE retrieval code | `docs/sqlite-vec-spike.md`, `src/brain/vectors.py`, `tests/test_index.py` |
| r2-codex/r2-verify-r1 | re-estimate + design/core/CLI split decision | `docs/s02-effort-reestimate.md` |
| consensus | filter ≠ containment; real control + direct-file-read tests | `src/brain/projection.py`, `tests/test_direct_file_read.py` |
| r2-claude | second-model review on a non-Opus model | `_evidence/s02/second-model-review.md` (Sonnet) |
| CORE-03 | Keychain/Cred-Mgr key, no file fallback, fail-closed | `src/brain/audit.py`, `tests/test_audit_chain.py` |

## Tests — 37 passing
`_evidence/s02/pytest-summary.txt` (`37 passed`), `_evidence/s02/pytest-collect.txt`.
- `test_cli_contract.py` — CLI JSON shape, sourced results, egress filter at stdout, help self-description.
- `test_classification_filter.py` — tier ordering, default-deny, elevation gate.
- `test_direct_file_read.py` — **consensus hardening**: cooperative path withholds; full-vault file read exposes (∴ not containment); projection physically excludes sensitive files on disk.
- `test_audit_chain.py` — append/verify, tamper detection, **no-key fails closed (no file written)**.
- `test_index.py` — both backends satisfy one contract; delete-and-rebuild safe.

## Live smoke (real S01 sample vault)
`_evidence/s02/cli-smoke.txt` + `cli-smoke-2.txt`: `brain --help`, `rebuild`
(8 notes via sqlite-vec), `search --json` (sourced hits + egress report),
`recent` (3/5 surfaced, 2 withheld at max-tier=Internal), host-broker `write`
(audited), `verify-audit` (ok), `project` (Restricted note excluded;
SECRET-MARKER count 0 in the projected workspace).

## Decision: the real containment control (consensus hardening)
Chosen **option (a): workspace projection.** The classification filter is the
*egress-decision mechanism* for cooperative `brain` stdout. Real containment for
untrusted/VM harnesses is `brain project` — a filtered copy that physically omits
anything above the cap and all default-denied notes — composed with the host/VM
trust split (substrate-spec §4). Proven on disk by `test_direct_file_read.py`.

## Second-model review response (r2-claude gate)

The Sonnet review (`_evidence/s02/second-model-review.md`) returned **NO-GO** with
5 MED required fixes + recommendations. All were resolved or consciously dispositioned;
the gate then clears to GO. Final suite: **43 passing**.

| Finding | Sev | Disposition |
|---|---|---|
| F-01 EPILOG `--vault` after subcommand fails | MED | **FIXED** — EPILOG documents `--vault` as top-level (before subcommand) + `$BRAIN_VAULT`; test `test_vault_is_top_level_before_subcommand`. |
| F-02 AGENTS.md §5 ↔ CLI surface mismatch | MED | **FIXED** — §5 note: four verbs = trust surface; `brain --help` is the authoritative full command list; `draft_capture` is the VM verb (later session). |
| F-06 phantom audit entry on write failure | MED | **FIXED** — compensating `write_failed` chain entry on post-sign write failure; test `test_write_failure_records_compensating_entry`. |
| F-07 no flock on append (chain-fork race) | MED | **FIXED** — `_exclusive_lock` (fcntl/msvcrt) wraps compute-prev_hash+append; test `test_concurrent_appends_do_not_fork_chain` (12 threads). |
| F-08 no Ed25519 key-type assertion | MED | **FIXED** — `resolve_signing_key` rejects non-Ed25519 PEM as `KeyUnavailable`; test `test_non_ed25519_key_rejected`. |
| F-03 help test token set incomplete | LOW | **FIXED** — extended to project/write/verify-audit/containment. |
| F-04 case-sensitive tier silent-denies | MED | **FIXED (reasoned divergence)** — kept STRICT matching (fail-closed is correct for a security egress filter; case-insensitive would be fail-OPEN), but added a `casing_mismatch` diagnostic so a wrong-case tier surfaces instead of vanishing; test added. |
| F-05 `egress.total` includes withheld count | LOW | **FIXED (doc)** — `--help` now states `egress.total` is an audit count by design. |
| F-09 O(n) `_last_entry` per append | LOW | **DEFERRED (noted in code)** — fine at S02 volumes; tail-seek/cache before cutover. |

## Known limitations / routed forward
- SQLCipher compat + Windows-locked-install + Cowork-aarch64 extension-load
  probes → S08 / re-probe on those hosts (fallback backend covers the failure
  mode meanwhile). See `docs/sqlite-vec-spike.md`.
- Real Arctic-embed embedder is pluggable but not yet wired — `HashEmbedder`
  (deterministic, offline) stands in so the contract + tests run anywhere.
- AGPL conveyance: a fork may only be reinstated by recorded Legal clearance.
