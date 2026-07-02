# ASR / Controlled-Folder-Access design rules (PKG-02)

**Session:** S07 · **Date:** 2026-06-27
**Goal:** the app never gets quarantined or blocked by Microsoft Defender
**Attack-Surface-Reduction (ASR)** rules or **Controlled Folder Access (CFA)** on
a locked Acme endpoint. These are *design* rules the code already follows
(verified against the S02/S06 implementation) plus the packaging choices that
keep them true.

## The five rules (all load-bearing)

### R1 — All derived state in `%LOCALAPPDATA%`, never Documents / OneDrive
CFA protects `Documents`, `Desktop`, `Pictures`, and **OneDrive-synced** folders;
an unknown process writing there is blocked. So **every** derived/runtime artifact
(the SQLite index, snapshots, capture inbox, logs, model cache) lives under
`%LOCALAPPDATA%`:
- Windows install target: `%LOCALAPPDATA%\Programs\brain` (`install.cmd`).
- Runtime/index: `%LOCALAPPDATA%\brain\` via `BRAIN_RUNTIME_DIR` (default already
  app-data, not the vault, not Documents).
- **The Markdown vault** is the *only* user-facing data and is read/written by
  the user's editor (Obsidian/VS Code), not by `brain` writing into a CFA folder.
  `brain write` is host-broker and targets the vault path the user configured; if
  that path is under Documents/OneDrive, document the **CFA allow-rule** for the
  signed `brain.exe` (see R5) — do not rely on it silently.

### R2 — Launch as ONE signed binary; do work IN-PROCESS
ASR rule **"Block process creations originating from PSExec and WMI"** and the
broader **child-process** heuristics flag apps that spawn `python.exe`, `cmd.exe`,
`powershell.exe`, or script shims. So:
- `brain` ships as a **single frozen binary** (PyInstaller one-dir) — there is no
  `python.exe` child, no `.bat`/`.ps1` shim in the hot path.
- The **retrieval + capture hot path** (search/get/recent/grep/bases-query/
  graph-expand/draft-capture) runs **entirely in-process** — verified: it imports
  no `subprocess`/`Popen`/`os.system` (`_evidence/s07/no-subprocess-spawn.txt`).
- **Honest exception — host key custody.** `audit.py`/`encryption.py` may invoke
  a key-custody backend when resolving a signing/encryption key before an
  *audited write* on the **host**. On **Windows** the default custody is the
  in-process `keyring` lib (Windows Credential Manager / DPAPI) — **no child
  process**. macOS uses the `security` binary (darwin-only). The only way to get
  a shell child is the **explicit opt-in** `*_CMD` custody env var. All of this is
  **host-only and off the read path**; per S06 the Cowork VM resolves no key and
  spawns nothing. So on a managed Windows endpoint, neither the common user path
  nor the default sign path spawns an interpreter/shell child.
- The Desktop app / shell launches `brain.exe` **directly**, not via a wrapper
  script. (`install.cmd` runs once at install time under the IME Managed
  Installer — not in the app's runtime path.)

### R3 — No one-file self-extraction
ASR **"Block executable content unless it meets a prevalence/age/trust
criterion"** and generic Defender heuristics flag a PE that drops and runs code
from `%TEMP%`. One-file PyInstaller does exactly that on every launch. **One-dir**
(R-encoded in every `.spec`, `upx=False`) never self-extracts.

### R4 — No macro / Office-spawned execution, no LOLBins
`brain` is never invoked from an Office macro, `mshta`, `rundll32`, `regsvr32`, or
other LOLBin. The only sanctioned entry points are: the user's shell PATH (added
by `install.cmd`), and the **one sanctioned scheduled task** (ux-02 brief, built
in S09) which invokes the signed `brain.exe` **directly** (no `cmd /c` wrapper).

### R5 — If a CFA allow-rule is unavoidable, request it explicitly for the SIGNED binary
If the user's vault genuinely lives under a CFA-protected folder, add a CFA
"Allow an app through Controlled folder access" entry for the **signed**
`brain.exe` (publisher-pinned once PW-2 lands), deployed via the same Intune
policy. This is an explicit, reviewable rule — never a silent dependency.

## Mapping to the build

| Rule | Enforced by | Evidence |
|---|---|---|
| R1 app-data only | `BRAIN_RUNTIME_DIR` default + `install.cmd` target | `docs/cowork-windows-install.md`; `install.cmd` |
| R2 single signed binary, in-process | PyInstaller one-dir; `--role` code paths; no `subprocess` of interpreters | `_evidence/s07/no-subprocess-spawn.txt` |
| R3 no self-extraction | one-dir spec, `upx=False` | `packaging/*/brain-*.spec` |
| R4 no LOLBin entry | S09 task spec invokes `brain.exe` directly | S09 task SPEC (built S09) |
| R5 CFA allow (if needed) | Intune CFA policy for signed exe | PW-3 policy (PENDING) |

## Defender / ASR validation (PENDING PW-4 device)
On the managed test device: enable ASR rules in **audit** mode, install via IME,
run the four interactions + the S09 brief, and confirm **zero** ASR audit events
and **zero** CFA blocks. Capture the Defender timeline as evidence (see
`_evidence/s07/defender-sandbox-report.md`). This is the
**internal-sandbox / Defender-for-Endpoint** evidence that replaces a public
VirusTotal upload (HARDENED:codex — public upload would disclose the private
binary + bundled model before approval).
