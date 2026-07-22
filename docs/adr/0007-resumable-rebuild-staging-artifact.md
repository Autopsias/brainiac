# 7. A surviving partial rebuild is a staging artifact, not a leak

Date: 2026-07-19

## Status

Accepted — implemented in `s03b` of `_plans/engine-observability-crash-safety-2026-07-19/`.
Resume was reconciled with the atomic-swap invariant (see Consequences); the
"per-batch commits, no resume" fallback was not needed.

## Context

The 2026-07-16 field report recorded a `brain rebuild` on the production vault killed twice at
58+ minutes, which left the **live index wiped** (0 notes / 0 chunks) because
`_create_schema` dropped the live tables first and inserts landed at the end.

The fix (commit `4c62133`) made `rebuild()` build into a temp DB
(`<db>.rebuild.tmp.<pid>`) and atomically `os.replace` it into place on success. Its
regression test, `tests/test_index.py:102`, encodes the guarantee as:

```python
assert list(tmp_path.glob("interrupt.sqlite.rebuild.tmp.*")) == []
```

That is: *no temp artifact may survive an interrupted rebuild.* Teardown enforces it at
three sites — on entry (`index.py:476-478`), in `except BaseException:` (`490-494`), and
in `finally:` (`495-497`).

A subsequent field report (2026-07-19) exposed the remaining cost: a rebuild is
all-or-nothing. The recovery run was killed at minute ~89 and lost **all** of its work,
leaving an empty index — strictly worse than the corrupt one it replaced. Making rebuild
resumable requires a partial temp DB to *survive* a kill, which the test above forbids.

## Decision

Restate the invariant rather than weaken it.

- **Old invariant:** no `*.rebuild.tmp.*` file survives an interrupted rebuild.
- **New invariant:** the live index is never replaced by anything but a **complete** temp
  DB; a surviving partial is a deliberate, named, resumable **staging artifact**.

Concretely:

1. The temp DB gets a **stable name** (no pid suffix) plus a JSON **manifest** recording
   embed model id, embedding **dimension**, schema version, backend name, index-format
   version, and a vault content fingerprint.
2. **The completion marker lives inside the temp DB, not in the manifest.** A row in a
   small `meta` table is written and committed **in the same transaction as the final
   batch**, and the atomic swap is gated on it. The JSON manifest is **advisory
   pre-flight metadata only** — used to reject a partial cheaply before opening the DB.

   > *Corrected 2026-07-19 after dual-model adversarial review.* The first draft of this
   > ADR put `finished: true` in the manifest "written in the same transaction as the
   > final batch". Both reviewers independently identified that as unimplementable: a
   > filesystem JSON write cannot join a SQLite transaction. Worse, the two files have
   > independent durability, so across a power loss "manifest says finished" can be true
   > while the final batch's commit is not on disk — precisely the
   > partial-index-reaches-production case this gate exists to prevent.

3. **The staging artifact is DB + WAL + SHM**, preserved and discarded as a unit. Every
   file-backed index enables WAL (`index.py:274-279`), so per-batch commits may exist only
   in `<temp>-wal` until a checkpoint. Preserving the DB while dropping its WAL would
   yield a marker claiming committed batches the main DB does not contain. A resumed
   artifact is reopened **through SQLite recovery** before any counts are inspected, and
   checkpointed only **after** completion validation.
4. **Durability protocol**, in order: commit final batch + `meta` row → reopen and
   validate the `meta` row → `PRAGMA wal_checkpoint(TRUNCATE)` → **consume the checkpoint
   result and refuse the swap unless it fully truncated** → fsync the temp DB → fsync the
   parent directory → `os.replace` → fsync the parent directory again.

   `wal_checkpoint` does not raise on a partial checkpoint; it returns
   `(busy, log_frames, checkpointed_frames)`, where `busy=1` means a reader blocked
   truncation. Since this design explicitly supports an external observer running
   `SELECT count(*)` against the staging DB, a blocking reader is an expected state, not a
   freak event. Ignoring the result would `os.replace` a main DB whose final committed
   pages still live only in its WAL.
5. **Resume validation matches every persisted invariant**: schema version, backend name,
   embed model id, **embedding dimension**, and index-format version. Model id alone is
   insufficient — `HashEmbedder(384)` and `HashEmbedder(256)` share
   `model_id == "hash-v1"` (`embed.py:151-154`), the brute-force backend accepts arbitrary
   blob sizes (`vectors.py:168-178`), and cosine silently `zip()`s (`vectors.py:35-42`),
   so a mixed-dimension resume returns silently wrong rankings with no error.
6. Teardown becomes conditional at all three sites (`index.py:476-478` entry-unlink,
   `490-494` `except BaseException:`, `495-497` `finally:`): preserve the partial only
   when it is resumable (`meta` row present, `finished: false`, ≥1 batch committed); still
   delete on any validation mismatch.
7. Validation is **atomic-discard**: when anything about the partial is uncertain, throw
   it away and rebuild from scratch. Never surgically repair a suspect partial DB.
8. Reprocessing a note uses **`_delete_note()` + `_write_planned()` in one transaction**,
   never `INSERT OR REPLACE`. `notes` has no foreign-key cascade to `chunks`
   (`index.py:303-327`) and vectors live in independent backend tables, so replacing a
   note row would leave stale chunks and vectors attached or orphaned — duplicate
   retrieval with no constraint failure.
9. `tests/test_index.py:102` is **updated to assert the new invariant**, not deleted or
   skipped. It remains a post-incident regression test.

## Consequences

**Positive.** A killed rebuild resumes from its last committed batch instead of restarting
— the difference between a 4-minute recovery and a 90-minute one on the real vault. The
live index still cannot be left partial, which was always the property that mattered.
Per-note idempotent writes inside each batch structurally eliminate the stale-row
`UNIQUE constraint failed: notes.rowid` class from the field report.

**Negative.** Disk: a staging artifact (DB + WAL + SHM) can persist between runs, bounded
by one rebuild's size. Failure surface: the swap now depends on a `meta` row inside the
staging DB plus a fully-truncated WAL checkpoint, rather than on control flow reaching the
end of a function — a subtler guarantee, mitigated by tests (h) and (i) in `s03b`, which
assert the swap is refused when the `meta` row says `finished: false` and when a reader
blocks checkpoint truncation.

**Risk accepted.** Resume introduces three in-process counters that must be rehydrated
from the staging DB (`_create_schema` skip, `_seen_ids` reconstruction, `chunk_rowid` from
`MAX(rowid)+1`). The third fails *silently* if missed — resumed batches reuse committed
rowids and mis-link vectors to chunks, producing wrong retrieval results with no crash.
`s03b` carries a dedicated vector↔chunk alignment test for exactly this.

**Session split.** Per-batch commits (`rb-01`, session `s03`) and resume (`rb-02`, session
`s03b`) ship separately. `s03` preserves the existing pid-suffixed temp DB and all three
teardown sites unchanged, so it delivers observable progress without touching crash-safety;
everything in this ADR applies to `s03b`. If `s03b` cannot reconcile resume with the swap
invariant, stopping at `s03` is an acceptable outcome — that is the "per-batch commits with
no resume" alternative below, reached deliberately rather than by failure.

## Alternatives considered

- **Commit batches directly into the live index under WAL.** Simpler resume, but abandons
  the atomic-swap guarantee — the live index becomes observably partial mid-rebuild. This
  is the exact failure the 2026-07-16 fix was written to prevent. Rejected.
- **Per-batch commits with no resume** (accept losing at most one batch on kill). Cheaper
  and lower-risk, but leaves the 90-minute restart cost fully in place, which is the
  motivating pain. Rejected as the target, though it remains the fallback if `s03b` cannot
  reconcile resume with the swap invariant — `s03` alone lands exactly this state.
