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
- **alias** — an optional, owner-curated identity string in brain-note
  frontmatter. Aliases are normalized with NFC + casefold + whitespace collapse
  for retrieval, but displayed exactly as authored. Shared aliases are
  collision warnings, not validation errors.
- **exact leg** — ADR-0008's bounded third RRF candidate list for exact aliases,
  exact titles, and verified contiguous title phrases. It is calibrated only at
  `rrf_k=60` and can be rolled back immediately with
  `BRAIN_EXACT_LEG_ENABLED=0` plus process restart.
- **evidence label** — the per-hit match explanation emitted by
  `search`/`hybrid-search`: `alias_hit`, `exact_title_match`,
  `title_phrase_match`, `keyword_exact`, `high_vector_match`, or
  `weak_semantic`.
- **create_safety** — the conservative per-hit create/no-create signal:
  `exists`, `probable`, or `unknown`. `exists` is only for one visible unique
  alias/title owner; collisions or withheld identity owners degrade the public
  answer without exposing hidden-owner details.
- **query log** — host-only real-query capture under the resolved app-data
  index directory's `query-log/`, outside `vault/` and outside `vault/.brain/`.
  It stores post-egress raw query telemetry for replay, with owner-only
  permissions and whole-month retention.
- **vault_same** — a replay class for query-log records whose captured vault
  fingerprint matches the current live index, making ranking/configuration
  thresholds meaningful.
- **drift_or_mixed** — a replay class for records whose fingerprint no longer
  matches the current live index. These rows are reported for context but are
  not threshold-gated because the log has no target qrels.
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
