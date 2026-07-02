# At-rest posture — FDE baseline + dormant conditional encryption (SEC-02)

**Session:** S08 · **Date:** 2026-06-27 · Design of record: `_design_profile_a_architecture_v5` §2.

## Baseline (the documented at-rest control): FDE + OS permissions

On a **single-user machine with FileVault/BitLocker on**, broad app/file-level
encryption above full-disk encryption is **over-engineering** (design v5 §2;
maintainer-confirmed: **no off-device sync today**). The documented at-rest control is:

- **Full-disk encryption** — FileVault (macOS) / BitLocker (Windows). This is the
  NIST CSF 2.0 **PR.DS-01** endpoint answer ("use full disk encryption to protect
  data stored on user endpoints").
- **OS file permissions** on the vault + the derived index dir (app-data, not a
  Controlled-Folder-Access path — `brain.config.index_dir`).

FDE protects the bounded threat it names: a **powered-off/locked, lost/stolen**
device. Once logged in, the volume is transparently decrypted — a process running
as the user (including a prompt-injected agent) reads plaintext regardless of FDE.
That exposure is the **egress path**, addressed in `egress-provider-posture.md`,
**not** by encrypting the disk harder.

## The dormant conditional encryption module (`brain.encryption`)

Built, **OFF by default** (`is_enabled()` → False unless `BRAIN_ENCRYPTION=on`).
AES-256-GCM (authenticated). It exists so the flip is a config change, not a
rebuild. The ONE caller that uses it unconditionally is the **off-device backup**
(`brain.backup`, SEC-03) — the one place encryption genuinely matters — which
forces encryption even while the at-rest flag stays off.

### Flip-list — when at-rest app-encryption IS warranted

Encryption stays OFF unless one of these holds (codified in
`brain.encryption.FLIP_LIST`):

1. **Off-device backup or cloud sync** of a decrypted-readable copy
   (iCloud/OneDrive/Dropbox/network share) — the strongest, most common trigger.
2. **Genuinely regulated data** (PCI mandated; Secret/PII under a flagged regime).
3. **Shared / multi-user machine** (OS permissions are the weak point).
4. **A cyber team contractually mandates** app-level encryption at rest.

When a trigger fires: set `BRAIN_ENCRYPTION=on`, provision the encryption key in
the OS secret store (below), and the encryption primitives engage.

## Key custody — OS secret store, NO file fallback (both keys)

Two distinct keys, same custody discipline (resolution precedence, first hit wins,
**no bare-file fallback** — fail closed):

| Key | Purpose | Module | Env override | Keychain service |
|---|---|---|---|---|
| **Ed25519 signing key** | audit-chain signatures (SEC-03) | `brain.audit` | `BRAIN_AUDIT_KEY_PEM` / `_CMD` | `profile-a-brain-audit-key` |
| **AES-256 encryption key** | conditional at-rest + off-device backup | `brain.encryption` | `BRAIN_ENCRYPTION_KEY` / `_CMD` | `profile-a-brain-encryption-key` |

Resolution order for both: `env PEM/key` → `env CMD` (custody backends, e.g.
`age`-decrypted) → **macOS Keychain** (`security find-generic-password`) →
**Windows Credential Manager** (`keyring`) → **fail closed**. There is
intentionally **no `.key`-file fallback** — a write/encrypt with no resolvable key
**refuses** rather than producing an unsigned/plaintext artifact. Verified:
`tests/test_audit_chain.py::test_no_key_fails_closed`,
`tests/test_encryption_module.py::test_no_key_fails_closed`.

> **macOS gain (narrow but real):** a Keychain ACL bound to the **signed**
> `brain` binary means another binary requesting the key triggers a user prompt.
> Weaker on Windows (DPAPI). This depends on the signing identity (external
> pre-work PW-2, Azure Trusted Signing).

## What is NOT done (honest scope)

- App-level encryption of the live vault is **NOT** enabled (no trigger fired;
  maintainer confirmed no off-device sync). The module is dormant by design — not a
  gap.
- The Keychain ACL "bound to signed binary" leg is **pending the signing identity**
  (PW-2). Until then key custody is Keychain/Credential-Manager + env, no file
  fallback — which is the load-bearing property.
