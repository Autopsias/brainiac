# Security notes — confirmed scanner false positives

This file records source patterns that a static security scanner (bandit /
semgrep / ruff's `S`-prefixed flake8-bandit rules / equivalent) will flag, that
we reviewed and confirmed are **false positives** for this codebase — plus the
`# nosec` / `# noqa: S*` anchor and one-line rationale left at each site so the
suppression is traceable back to this document instead of a bare, unexplained
`# nosec`.

If a NEW instance of one of these patterns shows up in a scan, don't assume it
is covered by this note — re-verify the specific call site against the
reasoning below before adding another anchor. The reasoning is pattern-specific
context (who controls the input), not a blanket license for the pattern.

## 1 · `subprocess.run(cmd, shell=True, ...)` — `audit.py` / `encryption.py`

**Where:** `src/brain/audit.py::_pem_from_cmd` (signing-key custody via
`$BRAIN_AUDIT_KEY_CMD`) and `src/brain/encryption.py::resolve_encryption_key`
(encryption-key custody via `$BRAIN_ENCRYPTION_KEY_CMD`).

**Scanner class:** command injection via `shell=True` (bandit B602 / ruff
`S602`).

**Why it's a false positive:** in both cases `cmd` is the *literal value of an
operator-set environment variable* — `BRAIN_AUDIT_KEY_CMD` /
`BRAIN_ENCRYPTION_KEY_CMD` — never a value derived from a note body, a CLI
query argument, an MCP tool-call payload, or any other content that could
plausibly be attacker-influenced. `shell=True` is a deliberate design choice
so an operator's custody backend can be an arbitrary shell pipeline (e.g.
`age -d -i ~/.age/id key.pem.age`), which a list-form `subprocess.run(["age",
...])` cannot express as cleanly. Whoever can set that environment variable in
the process's environment already has full code-execution equivalent to
whatever the command does — `shell=True` adds no *additional* attack surface
over "an attacker can set your env vars," which is already a full compromise
regardless of this code.

**Anchors:** `# noqa: S602` (ruff) + `# nosec B602` (bandit) at both call
sites, each with an inline one-line rationale pointing back here.

## 2 · String-built SQL — `index.py`

**Where:** `BrainIndex._next_rowid` (`f"...FROM {table}"`),
`BrainIndex._apply_zone_authority`'s zone-lookup (`f"...WHERE rowid IN
({qmarks})"`), and `BrainIndex.bases_query`'s dynamic `WHERE`/`ORDER BY`
clause construction (`f"{key} = ?"` / `f"ORDER BY {order_col} ..."`).

**Scanner class:** hardcoded/string-built SQL expressions (bandit B608 —
flags f-string/`.format()`/`%`-built SQL regardless of whether the
interpolated value is actually attacker-reachable).

**Why it's a false positive, per site:**

- `_next_rowid(table)` — `table` is never user input. Both call sites pass a
  hardcoded literal (`"chunks"` or `"notes"`); there is no code path where a
  request argument reaches this parameter.
- The zone-lookup `qmarks` — only the literal placeholder characters (`"?"`
  repeated `len(rids)` times) are interpolated into the SQL text; the actual
  *values* (`rids`) are passed as bound query parameters to
  `self.conn.execute(sql, rids)`, never string-formatted in.
- `bases_query`'s `where.append(f"{key} = ?")` / `f"ORDER BY {order_col} ..."`
  — `key` and `order_col` are only ever interpolated **after** an explicit
  `if key in cols` / `order_by if order_by in cols else "updated"` check
  against a fixed, hardcoded column allowlist (`cols = {"id", "title",
  "type", "classification", "zone", "path", "created", "updated"}`). An
  unrecognised column name is dropped (filters) or silently defaulted
  (order-by) — it never reaches the SQL text. Every *value* (`val`, `k`) is a
  bound parameter, never interpolated.

In short: **columns/table names that reach an f-string are allowlisted (or
hardcoded literals); every user-supplied VALUE is a bound parameter.** That
combination is exactly the safe pattern this scanner rule cannot statically
distinguish from real string-built SQL injection — hence the false positive.

**Anchors:** `# nosec B608` at each interpolation site, each with an inline
one-line rationale.

## 3 · SHA1 — `index.py`

**Where:** `BrainIndex._plan_note`'s synthetic-id disambiguator:
`hashlib.sha1(row["path"].encode()).hexdigest()[:8]`.

**Scanner class:** use of a broken/weak hash function (bandit B303/B324 —
flags any `hashlib.sha1`/`hashlib.md5` call regardless of purpose).

**Why it's a false positive:** this SHA1 use is a **non-security,
content-addressed de-duplication suffix** — when two notes would otherwise
resolve to the same non-unique id stem (e.g. many `SKILL.md` / `_index.md`
files sharing a filename across different paths), an 8-hex-char SHA1 digest of
the note's path is appended to keep `notes.id` unique in the derived index.
Nothing security-relevant depends on SHA1's collision resistance or preimage
resistance here — a collision would, at worst, produce a duplicate synthetic
id for two paths, which is a correctness bug (an extremely unlikely one, and
retrieval already keys on `path`/`Hit.path`, not the synthetic id), not a
security bypass. This is a deliberate use-case distinction, not an oversight:
`brain.audit` and `brain.encryption` (the actual security-relevant hashing —
audit-chain hash-linkage, backup-manifest integrity) both use SHA256
(`hashlib.sha256`), never SHA1/MD5.

**Anchor:** `# nosec B303 B324` at the call site, with an inline one-line
rationale.

## 4 · Path-traversal / tar-extraction hardening (for context, not a false positive)

Unlike the three patterns above, `src/brain/backup.py::restore_backup`'s tar
extraction is a **confirmed real finding**, hardened (not suppressed) in the
same pass that produced this document: unvalidated `tarfile.extractall()` is a
classic path-traversal / symlink-escape vector. The fix uses the stdlib
`filter="data"` extraction filter (Python 3.12+, backported to the 3.8.17 /
3.9.17 / 3.10.12 / 3.11.4+ security-release lines) with a manual
file-or-directory-only + traversal-check fallback for older interpreters. See
`src/brain/backup.py` and `tests/test_backup_restore.py`
(`test_restore_rejects_symlink_member_escaping_dest*`) for the fix and its
test coverage. Listed here only so a reader of this document doesn't wonder
why tar extraction *isn't* in the false-positive list above.
