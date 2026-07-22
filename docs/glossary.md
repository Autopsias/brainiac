# Glossary

Short definitions for the jargon this repo uses. Linked from first use in
README.md, AGENTS.md, and docs/install/*.

- **PARA** — Projects / Areas / Resources / Archive: the only folder taxonomy
  under `vault/brain/`. Notes are flat within each of the four folders — no
  further nesting or numbering.
- **classification tiers** — the five-level sensitivity ladder every note's
  frontmatter must declare, low to high: `Public < Internal < Confidential <
  Restricted < MNPI`.
- **MNPI** — Material Non-Public Information, the most restrictive
  classification tier. A note with a missing or unrecognised classification
  is treated as MNPI by default (deny-by-default) and withheld until a human
  explicitly raises the tier.
- **egress gate** — the deny-by-default filter every `brain` read
  (`search`/`get`/`recent`/...) runs just before printing results: it drops
  anything above the caller's allowed classification tier so the model only
  ever sees what it's cleared to see.
- **Cowork** — Claude Desktop's Linux VM sandbox execution mode; one of the
  three ways to run `brain`, restricted to `vm` role (read + draft only).
- **host-broker** — the trusted side of the host/VM split (your Mac/Windows
  machine, EDR-visible) that alone may sign the audit chain, write to the
  index, and run maintenance commands. The VM never holds this privilege.
- **overlay** — the per-owner personalization layer at `<vault>/overlay/`
  (voice, brand, keywords, people) that makes the generic substrate "yours"
  without hard-coding identity into `vault/brain/` or the kernel skills.
- **(lethal) trifecta** — the dangerous combination of (untrusted content) +
  (private data) + (an outbound channel) in one execution path; the design
  breaks at least one leg of this triangle everywhere it could otherwise
  form.
- **drain-on-invoke** — the host's pattern for committing VM-staged drafts:
  there is no background daemon: the next time the host runs `brain sync`, it
  drains, signs, and indexes any pending drafts from `capture-inbox/`.
- **snapshot** — the read-only, generation-stamped copy of the index that the
  host publishes (`brain sync --publish` / `brain snapshot`) for the VM to
  read; the VM never touches the live index or WAL directly.
- **WAL** — SQLite's Write-Ahead Log journaling mode, used by the live index
  on the host. Only the host opens the index writably; the VM reads a
  WAL-free snapshot.
- **FDE** — Full-Disk Encryption (FileVault on macOS, BitLocker on Windows):
  the at-rest baseline the security posture assumes.
- **EDR** — Endpoint Detection & Response, the corporate security agent on a
  managed machine. The host is EDR-visible; the Cowork VM is EDR-blind,
  which is why it is restricted to read + draft.
- **ZDR** — Zero Data Retention: the contractual term under which the model
  vendor does not retain or train on submitted content.
- **VirtioFS** — the shared-folder filesystem between the host and the
  Cowork VM; the one channel through which the VM sees the workspace.
