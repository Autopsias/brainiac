# Cowork-Windows workspace-install path (INT-02 — PRIMARY surface)

Claude Desktop **Cowork on Windows** is the largest user population and the
**primary** surface for this framework. Its shell runs in a **local Linux VM**
that mounts **only the workspace folder** over **VirtioFS**. There is no package
install, no HuggingFace egress, and no host toolchain inside the VM. So the brain
ships **into the workspace** as a self-contained, arch-matched build the VM can
see and run.

## Workspace layout

The Cowork sandbox sees the workspace root. We place the runtime under
`vault/.brain/` (gitignored, and excluded from indexing by `scan_vault`):

```
<workspace>/
└── vault/
    ├── brain/  raw/                     ← Markdown truth (the second brain)
    └── .brain/                          ← runtime, shipped into the workspace
        ├── bin/
        │   ├── brain-linux-x86_64       ← arch-matched Linux ELF (Intel/AMD VM)
        │   └── brain-linux-aarch64      ← arch-matched Linux ELF (ARM VM)
        ├── brain                        ← symlink → the arch that matches `uname -m`
        ├── model/                       ← bundled Arctic-embed fastembed cache (model.onnx + tokenizer)
        ├── snapshot/
        │   ├── index.snapshot.sqlite    ← READ-ONLY published index (single file)
        │   └── snapshot.manifest.json   ← generation id + age + counts + sha256
        └── capture-inbox/               ← WRITABLE: VM drops drafts here
```

> The task shorthand "`./.brain/{brain,index.sqlite,model.onnx}`" maps to the
> above: the VM-readable index **is** `snapshot/index.snapshot.sqlite` (a
> read-only snapshot, never a writable WAL DB). `BRAIN_RUNTIME_DIR` /
> `BRAIN_SNAPSHOT_DIR` / `BRAIN_CAPTURE_INBOX` can relocate any of these to a
> workspace-root `.brain/` if a deployment prefers that.

## Five build/runtime rules (all load-bearing)

1. **Arch-matched Linux ELF, both arches.** Cowork VMs are Linux on either
   `x86_64` or `aarch64`. Ship **both**; the per-session bootstrap symlinks
   `brain` → the one matching `uname -m`. Build matrix + recipe:
   `tools/build_brain_binary.sh`.
2. **Bundle the ONNX model.** HuggingFace is **not** on the VM egress allowlist,
   so the Arctic-embed model (the fastembed cache layout: `model.onnx` +
   tokenizer/config) is shipped in `.brain/model/`. Point fastembed at it with
   `BRAIN_MODEL_CACHE=<.brain>/model` (no network fetch — the embedder reads the
   bundled cache, never downloads).
3. **mmap the model from the mount — never copy to `/tmp`.** ONNX Runtime
   memory-maps the model file in place from the VirtioFS-mounted cache dir.
   Copying to `/tmp` wastes the ephemeral VM disk and breaks the "model lives in
   the workspace" invariant.
4. **Keep the index read-mostly (VirtioFS cache coherency).** The VM opens ONLY
   the published **read-only snapshot** (`mode=ro`, no WAL, no `-wal`/`-shm`
   sidecars — the publisher converts the snapshot to a self-contained
   rollback-journal file). A read-mostly single file avoids VirtioFS
   write-cache-coherency hazards between concurrent Cowork sessions.
5. **Re-export PATH each session.** The VM filesystem persists but the shell
   environment does not. Run the one-liner below at the start of every Cowork
   session (or source `tools/cowork_session_bootstrap.sh`).

### Per-session bootstrap (paste once per Cowork session)

```bash
export BRAIN_VAULT="$PWD/vault"
export BRAIN_ROLE=vm                                   # read + draft only
export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"    # bundled fastembed cache (no HF fetch)
ln -sf "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
brain status                                           # snapshot gen/age + pending drafts
```

## The VM is read + DRAFT only (hard guarantee)

`--role vm` (or `BRAIN_ROLE=vm`) makes `brain` a **read + draft** surface. The VM
binary **MUST NOT** write notes, open the index in WAL/write mode, or resolve a
signing key — enforced in code and proven by tests
(`tests/test_integration.py`):

- **Cannot write notes** — `write` (and any host-broker verb) is refused with rc
  4 **before** any signing-key resolution.
- **Cannot open WAL** — the index is opened `mode=ro`; any write raises
  `OperationalError` and no `-wal`/`-shm` sidecar is ever created.
- **Cannot resolve a signing key** — the VM `BrainCore` constructs **no** audit
  chain at all; `resolve_signing_key` is never reached on any VM path.

## Capture loop — VM draft → host commit → snapshot publish → retrievable

This is the full PRIMARY-surface loop (proven end-to-end by
`test_full_loop_vm_draft_to_host_commit_to_snapshot_retrievable`):

```
[Cowork VM]  brain draft-capture            → vault/.brain/capture-inbox/<id>.md
             (status: draft, provenance.trust: untrusted; NO sign/index/WAL)
                      │  (shared VirtioFS mount — the host sees it)
                      ▼
[HOST]       brain sync --publish
             1. DRAIN capture-inbox/ + .brain/drafts/  → Ed25519-sign + promote
                into raw/ (source) or brain/resources/ (note)              [DRAIN-ON-INVOKE]
             2. incremental upsert by path+hash (reuses idx-03)
             3. atomically PUBLISH a new generation-id snapshot into .brain/snapshot/
                      │
                      ▼
[Cowork VM]  brain get <id>  →  the SAME note is now retrievable from the snapshot
```

- **No capture daemon, no dedicated drain task.** The host drains **on invoke**:
  every host `brain sync` (and any host command that syncs) drains pending
  drafts first. The **one** sanctioned scheduled task is the **ux-02 morning
  brief/digest (s09)**, which doubles as the **guaranteed daily drain floor** —
  so a draft captured on the VM is committed and republished by the next brief at
  the latest. This is **one brief task**, not "no scheduled task".
- **Snapshot staleness is a surfaced state, not a silent loss.** After the host
  drains+indexes it republishes the snapshot (monotonic `generation`); the VM's
  `brain status` reports the snapshot generation + age + pending-draft count, so a
  session can see whether its read-only view is fresh.

## Install / refresh (run on the HOST)

```bash
# 1. build both Linux ELFs (cross-build or per-arch CI) — see the script
tools/build_brain_binary.sh            # → dist/brain-linux-x86_64, brain-linux-aarch64

# 2. assemble the workspace .brain/ (binaries + model + first snapshot)
tools/cowork_workspace_install.sh  <workspace>/vault  /path/to/model.onnx
```

`cowork_workspace_install.sh` is idempotent: re-running it refreshes the binaries
and republishes the snapshot. The Markdown in `vault/` remains the single source
of truth — `.brain/` is always rebuildable from it.
