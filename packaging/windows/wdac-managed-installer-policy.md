# WDAC Managed-Installer policy — config + step-by-step (PW-3)

**Status: PENDING EXTERNAL (PW-3) — authored by S07, applied by Example Corp EUC.**

This is the policy change that makes "IT approves in one step" true. It does NOT
allow `brain` by hash or by publisher — it tells WDAC to **trust whatever the
Intune Management Extension (IME) installs**. So every Managed-Installer-deployed
app (not just `brain`) is trusted by provenance.

## What to change

1. **Designate the Intune Management Extension as a Managed Installer.** In the
   WDAC *Managed Installer* AppControl policy, add the IME
   (`Microsoft.Management.Services.IntuneWindowsAgent.exe`) to the managed
   installer rule collection (Microsoft ships a reference managed-installer
   policy; merge it). This is an **org-level** AppLocker/WDAC policy edit.
2. **Enable the Managed Installer option** in the base WDAC policy
   (`Enabled:Managed Installer` rule-option 13) so the "installed by a managed
   installer" origin attribute is honored.
3. **Ring-test** on the PW-4 device group before broad deployment.

## Why this and not allow-by-hash / allow-by-publisher

- **Allow-by-hash** breaks on every rebuild (the one-dir hashes change each build
  — see `_evidence/s07/sha256-artifacts.txt`). Unmaintainable.
- **Allow-by-publisher** still needs the signing cert (PW-2) AND a policy edit
  per publisher; Managed-Installer trust subsumes it and is one rule.
- **Managed-Installer trust** is provenance-based: "IME installed it ⇒ trust it."
  One rule, survives rebuilds, no per-version maintenance. This is the
  load-bearing control (consensus HARDENED note), above SmartScreen reputation.

## Dependency

Anchoring the install still wants a **valid Authenticode signature** (PW-2) so
the binary is not separately flagged by Defender/SmartScreen during the IME
install. PW-3 depends on PW-2 existing (a signing identity to point examples at),
though the Managed-Installer rule itself trusts by IME provenance, not by cert.

## Decision to confirm at booking (HARDENED:claude)

**Single managed device (personal tool) vs fleet deployment.** This changes the
blast radius of the policy edit and the scope of val-01:
- *Single device:* scope the Managed-Installer rule to one device group; val-01 =
  clean install on that one device.
- *Fleet:* org-wide policy; val-01 = clean install across the pilot ring, plus a
  rollback plan if a ring device quarantines. Default per design v5: **start
  single-device pilot, expand to fleet only after val-01 passes.**
