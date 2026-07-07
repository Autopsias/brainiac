# Soak Runbook

The soak vault is a throwaway vault outside the repo at `~/brainiac-soak-vault`,
already scaffolded and keyed (`brain init --full --apply --no-register-tasks`,
audit key present in Keychain under `profile-a-brain-audit-key`, index built,
snapshot published). It is isolated from the shared nightly task and from any
real vault — nothing here risks real data. See `_evidence/soak/scaffold-status.json`
for the provisioning proof.

## 1. Drop files here

```
~/brainiac-soak-vault/inbox/
```

Drop 20-50 real documents covering this format checklist:

- [ ] PDF — normal
- [ ] PDF — scanned (image-only, no text layer)
- [ ] PDF — password-protected
- [ ] DOCX — with tables
- [ ] XLSX — with formulas
- [ ] PPTX
- [ ] EML — with attachments
- [ ] HTML
- [ ] ZIP — including nested archive members (zip-in-zip)
- [ ] Images (PNG/JPG)
- [ ] Hostile filenames — colons (`:`), quotes (`"`), unicode (e.g. `café-résumé.pdf`),
      and at least one very long filename (200+ chars)

## 2. Run the drain

```
BRAIN_VAULT=~/brainiac-soak-vault brain sync
```

(or wait for the vault's own drain if a schedule is later attached — the
soak vault itself has no scheduled task registered, so drains are manual.)

## 3. Observation checklist

After each drain, verify:

- [ ] **Signed vs quarantined** — every clean input produced a signed note
      under `raw/`; every hostile/malformed input landed in the quarantine
      dir with a specific `reason` (not a generic catch-all) — check
      `brain status --json` and the quarantine directory listing.
- [ ] **Dedup on re-drop** — dropping the exact same file a second time
      produces `"duplicate": true` / no new note, no re-sign, no re-hash
      churn (see SOAK-01 fix: collision path reuses the known sha256).
- [ ] **Archive immutability** — re-dropping a file whose archived original
      already exists with *different* bytes at the same target path
      quarantines as `archive_collision` and never overwrites the original.
- [ ] **Drain-on-sync latency** — time from drop to indexed/searchable
      (`brain sync` → `brain status` notes/chunks count increases) is
      reasonable for the batch size; note any outliers.

Do not soak-test against any real vault. Report findings back with the
`brain status --json` diff (before/after) and the quarantine reason
histogram.
