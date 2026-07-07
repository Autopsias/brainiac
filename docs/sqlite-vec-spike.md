# sqlite-vec dependency spike (CORE-01, r2-codex)

**Run:** 2026-06-27 · macOS 25.5.0 arm64 · CPython 3.14.4 · system sqlite 3.53.0
**Gate:** done **before** CORE-01 declared complete, per the hardening note
("Add an early dependency spike for sqlite-vec (pre-v1, breaking changes
expected)… Define an adapter interface with a fallback vector backend BEFORE any
retrieval code is written.").

## Why this spike exists
sqlite-vec is **pre-v1** (tested here: `v0.1.9`). Breaking changes are expected,
the extension may fail to load on a locked Windows install or an unbuilt Cowork
VM, and SQLCipher compatibility is unproven. So retrieval must NOT depend on
sqlite-vec directly — it depends on the `VectorBackend` adapter
(`src/brain/vectors.py`), with a guaranteed pure-Python fallback.

## Results (live, this host)

| Probe | Result | Notes |
|---|---|---|
| pip install `sqlite-vec` | ✅ `0.1.9` | wheel installs cleanly into a venv |
| extension loads | ✅ | `enable_load_extension(True)` → `sqlite_vec.load(conn)` |
| `vec_version()` | ✅ `v0.1.9` | |
| vector recall (KNN) | ✅ | `vec0` MATCH returns nearest rowid at distance 0.0; correct ordering |
| WAL behaviour | ✅ | `PRAGMA journal_mode=WAL` engages; `-wal` sidecar created |
| snapshot | ✅ | `sqlite3 .backup()` online-snapshots a vec0 DB; rows survive |
| loadable-extension support in stock CPython sqlite3 | ✅ | `hasattr(conn,'enable_load_extension')` true on this build |
| SQLCipher compat | ⏳ DEFERRED | `pysqlcipher3` absent. At-rest encryption is **conditional** (FDE baseline, design v5 §6); SQLCipher is an S08 concern. **Mitigation:** the brute-force backend stores vectors as plain BLOBs in a normal table — no loadable extension — so it works under ANY sqlite build incl. a future SQLCipher one. SQLCipher is therefore not on the critical path for CORE-01. |
| Windows-locked install | ⏳ NOT TESTABLE HERE | macOS host. **Mitigation:** adapter + fallback; `get_backend("auto")` degrades to brute force if the extension can't load. To be re-probed on a Windows host (S06/S08). |
| Linux Cowork aarch64 build | ⏳ NOT TESTED HERE | **Mitigation:** same fallback; the manylinux wheel is published by upstream — re-probe in the Cowork VM. |

## Adapter decision (the durable outcome)
`src/brain/vectors.py` defines `VectorBackend` (Protocol) with two
implementations, selected by `get_backend()`:

- **`SqliteVecBackend`** — `vec0` virtual table; used when the extension loads.
- **`BruteForceBackend`** — vectors as BLOBs + Python cosine; **no extension,
  works everywhere** (incl. SQLCipher), correct (slower at scale). The
  guaranteed fallback.

The index/retrieval code (`src/brain/index.py`) imports ONLY the adapter, never
`sqlite_vec`. Both backends pass the identical retrieval contract test
(`tests/test_index.py`, parametrized over available backends). This satisfies
"define the adapter + fallback BEFORE writing retrieval code."

## Open items routed forward
- SQLCipher compat probe → **S08** (at-rest/egress).
- Windows-locked-install + Cowork-aarch64 extension-load probes → re-run on
  those hosts; until then `auto` selection covers the failure mode by design.
- Pin `sqlite-vec` (pre-v1) once a known-good version is chosen for distribution.
