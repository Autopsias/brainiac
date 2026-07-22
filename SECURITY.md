# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately via GitHub's **private
vulnerability reporting** on this repository (Security tab → "Report a
vulnerability") rather than opening a public issue. Include repro steps,
affected version/commit, and impact. This is a small, single-maintainer
project — expect an acknowledgement, not an SLA; there is no bug bounty.

## Supported versions

Public releases are tagged `v<semver>` on this repository and published to
PyPI as `brainiac-cli`; versioning follows `pyproject.toml`'s
`[project].version` and `CHANGELOG.md`. Only the **latest tagged release**
is supported — there is no back-porting of fixes to older tags. Each release
is produced via the clean-room export described in
`docs/adr/0001-publish-via-clean-room-export.md`.

## Audit-key rotation runbook

The audit chain (`src/brain/audit.py`) is signed with a single Ed25519 key
resolved from the OS secret store (macOS Keychain / Windows Credential
Manager / `$BRAIN_AUDIT_KEY_PEM` / `$BRAIN_AUDIT_KEY_CMD`), fail-closed with
**no file fallback**. `brain verify-audit` verifies the chain **under the key
currently resolvable** — reading `src/brain/audit.py` and
`src/brain/anchor.py`, there is no cross-signing or old→new key-boundary
verification implemented. Do not assume `verify-audit` can validate a chain
signed by more than one key.

Given that constraint, the supported rotation procedure is:

1. **Freeze the old chain.** With the old key still resolvable, append one
   final signed entry recording the rotation (e.g. via `brain write` with a
   reason noting "key rotation"), then run `brain verify-audit` to confirm
   the old chain is internally consistent end to end.
2. **Anchor the old chain's head off-host**, if you use `brain anchor`
   (`src/brain/anchor.py`), so the pre-rotation head is independently
   attested before the key changes.
3. **Retain the old chain read-only.** Move or archive the old chain log
   (do not delete it) — it remains the audit trail for everything signed
   before rotation.
4. **Generate a new Ed25519 keypair** (`brain.audit.generate_key_pem()` or
   equivalent) and install the new private key in the OS secret store /
   env-injection path, replacing the old one.
5. **Start a fresh chain under the new key.** New writes begin a new chain
   from `NULL_PREV_HASH` — there is no linkage back into the old chain's
   hash sequence.

**Limitation, stated plainly:** this is two independently-verifiable chains
(old, frozen; new, ongoing), not one continuously-verifiable chain across the
rotation boundary. `verify-audit` never validates an old→new cross-signature
because none is produced. If continuous cross-boundary verification is later
needed, that requires new code (e.g. having the new key co-sign a pointer to
the old chain's final head) — do not claim this repo supports it until that
code exists and is covered by a test.
