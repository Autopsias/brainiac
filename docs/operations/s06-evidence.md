# S06 evidence — multi-harness integration (Cowork-Windows-first)

**Session:** S06 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core · **S03** chunked Arctic/ONNX embeddings + incremental sync + snapshot publisher (the dependency the hardening added) · S04 hybrid retrieval · S05 eval harness.
**Model note:** planned model Fable unavailable → executed on **Opus**.
**Result:** 103 tests pass (87 prior + 16 new S06); conventions validator exit 0.

---

## Bottom line

One canonical instruction file (`AGENTS.md`) now drives every harness, and the
PRIMARY surface — **Claude Desktop Cowork on Windows** — has a complete,
**read+draft-only** workspace-install path with a **closed capture loop** (VM
draft → host commit → snapshot publish → the same note retrievable from the VM).
The VM leg is enforced, not just documented: hard tests prove the VM binary
**cannot write notes, cannot open WAL, and cannot resolve a signing key**. MCP is
a single-surface convenience for the pure Chat tab only — a ~50-line, deletable
adapter over the same core + egress gate.

---

## INT-01 — AGENTS.md canonical + per-harness wiring ✅

- **`AGENTS.md` is canonical** and carries the one-paragraph brain-usage note
  (§5 self-discovery + the new per-harness wiring + Cowork-VM blocks).
- **`CLAUDE.md`** imports it via `@AGENTS.md` (Claude Code + Desktop Code tab).
- **`.gemini/settings.json`** sets `contextFileName: AGENTS.md` (Gemini CLI).
- **Codex** reads `AGENTS.md` natively.
- All four command-capable harnesses call `brain` via native shell — **no MCP**.
- Full table + rationale: `docs/harness-wiring.md`.

Proven by `test_claude_md_imports_agents_md`,
`test_gemini_settings_points_at_agents_md`,
`test_agents_md_is_canonical_brain_usage`,
`test_help_advertises_role_and_draft_capture`.

## INT-02 — Cowork-Windows workspace-install path (PRIMARY) ✅

- **Workspace layout** + per-session PATH/model/role re-export documented in
  `docs/cowork-windows-install.md`; scripts: `tools/build_brain_binary.sh`
  (arch-matched ELF, both arches via CI matrix / buildx),
  `tools/cowork_workspace_install.sh` (assemble `.brain/` + first snapshot),
  `tools/cowork_session_bootstrap.sh` (sourceable per-session env).
- **Bundled model, no HF fetch:** `BRAIN_MODEL_CACHE` points fastembed at the
  bundled cache on the mount; ONNX Runtime mmaps it in place (never `/tmp`).
- **Read-mostly index (VirtioFS):** the VM opens ONLY the published read-only
  snapshot (`mode=ro`). The publisher converts the snapshot to a self-contained
  rollback-journal **single file** — no `-wal`/`-shm` sidecars a read-only mount
  couldn't create. (`test_published_snapshot_is_single_self_contained_file`.)

### HARD TEST — VM is read + draft only (consensus + r2 hardening)

| Guarantee | Test |
|---|---|
| VM **cannot write notes** (refused before key resolution) | `test_vm_cannot_write_note_and_never_resolves_key`, `test_cli_vm_refuses_host_commands_without_resolving_key` |
| VM **cannot open WAL** (read-only DB; no sidecars) | `test_vm_index_is_read_only_cannot_write_and_creates_no_wal` |
| VM **cannot resolve a signing key** (no audit chain constructed) | `test_vm_core_constructs_no_audit_chain` + the assert-no-resolve probes above |
| **Capture is the one VM quasi-write** (unsigned, untrusted, not indexed) | `test_draft_capture_stages_unsigned_untrusted_not_indexed` |
| **FULL loop**: VM draft → host commit → snapshot publish → retrievable from VM | `test_full_loop_vm_draft_to_host_commit_to_snapshot_retrievable` |
| **Staleness surfaced**: snapshot generation/age + pending-draft count | `test_status_reports_snapshot_generation_age_and_pending` |

End-to-end CLI transcript: `_evidence/s06/cli-smoke.txt` (rebuild → snapshot →
VM draft-capture → VM write **refused rc=4** → `sync --publish` drains+republishes
→ VM `get vmcap` = `RETRIEVABLE_FROM_VM` → snapshot dir is a single file).

**Threat-model honesty (r2-codex):** there is **no capture daemon and no
dedicated drain task**. The host drains **on invoke**; the **one** sanctioned
scheduled task is the **ux-02 morning brief (s09)**, the guaranteed daily drain
floor. Stated this way in AGENTS.md §6, the spec §4.1, and the install doc.

## INT-03 — optional ~50-line MCP adapter (Chat tab only) ✅

`src/brain/mcp_adapter.py` wraps the SAME `BrainCore` + the SAME
`ClassificationFilter` and exposes ONLY the read verbs. Deletable; not on the
critical path. Optional `[mcp]` extra + `brain-mcp` console script.
Proven by `test_mcp_adapter_applies_default_deny_egress` (same deny-by-default as
the CLI; elevation is the human gate) and `test_mcp_adapter_exposes_only_read_tools`.

---

## Files

**Engine:** `src/brain/config.py` (role + workspace/snapshot/capture-inbox paths),
`src/brain/index.py` (`read_only` connection — `mode=ro`, no WAL),
`src/brain/core.py` (`role`, `RoleError`, host guards, `draft_capture`,
capture-inbox drain, `sync --publish`), `src/brain/snapshot.py` (self-contained
single-file snapshot), `src/brain/embed.py` (`BRAIN_MODEL_CACHE` bundled-model
hook), `src/brain/cli.py` (`--role`, VM allow-list gate, `draft-capture`,
`sync --publish`), `src/brain/mcp_adapter.py` (new, optional).
**Wiring:** `AGENTS.md` (canonical), `CLAUDE.md`, `.gemini/settings.json`.
**Docs:** `docs/harness-wiring.md`, `docs/cowork-windows-install.md`,
`docs/substrate-spec.md` §4.1.
**Scripts:** `tools/build_brain_binary.sh`, `tools/cowork_workspace_install.sh`,
`tools/cowork_session_bootstrap.sh`.
**Tests/evidence:** `tests/test_s06_integration.py`, `_evidence/s06/`.
