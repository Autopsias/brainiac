# Frontmatter is stored as one JSON column in the index, not one column per key

**Status:** accepted (2026-09-11, landed with *One Email, One Note* as `518bc8c1`;
proposed 2026-09-10 as item PV-01)

The index `notes` table stores fixed columns only, so a note's `provenance.sender`,
`provenance.sent` and `provenance.subject` never reach a result row. The Cowork VM reads
`.brain/index.snapshot.sqlite` and nothing else, so re-reading the note file at `get`
time cannot work on that leg — the fields have to be IN the index. We store the whole
parsed frontmatter as one `frontmatter` TEXT column holding `json.dumps(meta,
sort_keys=True)`, and read individual keys with `json_extract` at row-shaping time.

## Considered options

- **One typed column per provenance key.** Rejected: `provenance.*` is a deliberately
  open vocabulary — `tools/validate.py:322` warns rather than errors on an unrecognised
  subkey, so the schema is meant to grow. Every new fact would then be another migration.
- **Read the note file at row-shaping time.** Rejected: the VM has the sqlite snapshot
  and no note files.

## Consequences, and why this is worth an ADR rather than a comment

The decision is expensive to reverse in ONE specific way: it bumps the index schema
version, and a schema bump forces a **full rebuild of every registered vault**. Measured
2026-09-10: the reference vault holds 4,551 notes / 128,714 chunks, and CONTEXT.md's *Rebuild*
entry puts a full rebuild of a vault that size in **hours, not minutes** (the one window
ever captured extrapolates to 8.57 h). Two vaults are registered. So both adopting this
and undoing it cost a working day of machine time each — which is the reason to record
the choice now rather than rediscover it.

The egress gate is unaffected: it filters on `classification` before any row leaves, and
the frontmatter column changes nothing about tiering.
