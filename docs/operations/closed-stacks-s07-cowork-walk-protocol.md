# CUT-03 — the 14-skill walk, and the VM-side read that closes VULN-3385

**Who runs this: the owner, once, in a live Cowork session.** Every other part
of s07 ran on the host. This part cannot: a Cowork session runs in a cloud
sandbox reached through the Claude Desktop UI, and the host has no API, CLI or
socket that can start one, attach a folder to one, or read what one executed.
That was checked before this file was written — `tools/cowork_session_bootstrap.sh`
runs INSIDE the VM, and nothing under `~/Library/Application Support/Claude`
exposes a session lane.

This file exists so the walk is a **paste-and-read exercise**, not a judgement
call. Paste each block, read the verdict line.

---

## 0 · Before you start — attach the new folder

Print the two folders THIS host registered — the OLD one to detach, the NEW
one to attach. They are not written into this file, so the shipped tree bakes
in no operator path and no workspace name:

```bash
python3 - <<'PY'
import json, pathlib
rows = json.loads((pathlib.Path.home() / ".brainiac/workspaces.json").read_text())["entries"]
# Only the vault this cutover touched: it is the one with BOTH a replacement
# workspace and an original one. Any other registered vault is none of this
# file's business, and printing it would tell you to detach the wrong folder.
by_vault = {}
for r in rows:
    if r["target"] == "cowork-vm":
        by_vault.setdefault(r["vault_path"], set()).add(r["workspace_path"])
printed = False
for vault, ws in sorted(by_vault.items()):
    # THE PROPERTY, NOT THE NAME. Until 2026-08-30 this line tested for the
    # literal "/CoworkWorkspaces/", which is an operator's folder name -- it
    # contradicted the note above about baking in no workspace name, and it
    # printed nothing at all for an operator who chose a different one. The
    # relocated workspace is the one that does NOT contain the vault; the
    # old, co-located one is the one that does.
    new_ws = {w for w in ws if not (vault + "/").startswith(w.rstrip("/") + "/")}
    old_ws = ws - new_ws
    if not (new_ws and old_ws):
        continue                     # not cut over; leave it alone
    printed = True
    for w in sorted(old_ws):
        print("OLD - detach ", w)
    for w in sorted(new_ws):
        print("NEW - attach ", w)
# The fallback is keyed on what the loop ACTUALLY printed, not on a separate
# count. It used to test `len(ws) > 1`, which is a different question: a vault
# carrying two OLD workspaces and no replacement satisfied it, printed no row,
# and printed no message either -- silent empty output, in a protocol whose
# whole method is paste-and-read.
if not printed:
    print("no cut-over vault found - no vault has BOTH an old and a new workspace")
    for vault, ws in sorted(by_vault.items()):
        print("  seen:", vault, "->", sorted(ws))
    if not by_vault:
        print("  the registry holds no cowork-vm rows at all")
PY
```

In Claude Desktop, detach the OLD folder from Cowork and attach the NEW one.
Both are GUI actions. Until the detach happens the finding is not closed on the
live session, however green the host's `brain doctor` row is: the host proves
what the NEW folder contains, and the owner proves which folder is ATTACHED.

---

## 1 · Prove which bundles the session is actually executing

The staged bundles were re-measured on the host at 2026-08-29T11:1xZ, by
extracting each `.skill` zip (a `.skill` is a ZIP — grepping its raw bytes
searches compressed data and returns a meaningless all-clear) and hashing the
member `SKILL.md`. **14 of 14 matched the worktree source when the walk ran,
all stamped 0.20.32.**

**One row has moved since, deliberately, and the STAGED side moved with it.**
The column below is the staged bundle's hash frozen at the moment of the walk.
It is a HISTORICAL RECORD of what was exercised, and it is never rewritten.

`task-registrar/SKILL.md` has been edited twice since the walk, both times by
the review of this session: on 2026-08-30 to correct an arithmetic error
("three weeks" for a seven-week span), and again the same day to close the
adversarial round's finding that its budget guard compared two numbers from the
same file and so passed on synchronized drift. `tools/package_clients.py` was
then re-run, which REBUILDS the staged bundle from canonical — so the row now
reads `5f4accd745a536af` on both the worktree and the staged side, and
`9006952350f691e7` below is held by neither.

Re-measured 2026-08-30 across all fourteen bundles (sha256 of the `SKILL.md`
inside each `dist/cowork-skills/*.skill`, first 16): **13 still equal the
walk-time hash; `task-registrar` is the one that does not.** That is a
post-walk source edit followed by a rebuild, not stale staging. It does mean
the walked bundle for that one row cannot be re-materialised from the current
tree, which is stated here rather than left for a reader to discover.

The verdict rule below is written against the STAGED side, and this is the
reason it needs saying out loud.

| skill | VERSION | staged `SKILL.md` sha256 (first 16) |
|---|---|---|
| autoresearch | 0.20.32 | `7bb1a01b8029419c` |
| brain-inbox | 0.20.32 | `7fea8af7e64e9dfd` |
| chief-of-staff | 0.20.32 | `c8a92ea49cfd4db1` |
| curation | 0.20.32 | `cad2b3660c9c32bb` |
| graph-explorer | 0.20.32 | `8a6a9a27255581db` |
| improve | 0.20.32 | `20df1768f02c7039` |
| kb-curator | 0.20.32 | `1d05740553afae3c` |
| promote | 0.20.32 | `4595f4e08d9db877` |
| save-conversation | 0.20.32 | `d98344d21a706981` |
| task-registrar | 0.20.32 | `9006952350f691e7` |
| vault-eval | 0.20.32 | `44cec9bb3d61514c` |
| vault-ingestion | 0.20.32 | `bd009f3c3b64b60e` |
| vm-doctor | 0.20.32 | `c674eedd879b2b91` |
| voice | 0.20.32 | `af61cad10ac01023` |

Re-derive the same table on the host at any time:

```bash
cd <repo>
python3 - <<'PY'
import hashlib, zipfile
from pathlib import Path
import json
rows = json.loads((Path.home() / ".brainiac/workspaces.json").read_text())["entries"]
# THE SAME SELECTION AS §0, not a weaker one. §0 picks the vault that holds
# BOTH an old (co-located) and a new (relocated) workspace; a host with a
# second workspace that was never co-located satisfies "vault not inside
# workspace" too, and hashing THAT one would prove the wrong bundles. So this
# repeats §0's rule in full, and refuses when it is ambiguous.
by_vault = {}
for r in rows:
    if r["target"] == "cowork-vm":
        by_vault.setdefault(r["vault_path"], set()).add(r["workspace_path"])
cands = []
for vault, ws in by_vault.items():
    new_ws = {w for w in ws
              if not (vault.rstrip("/") + "/").startswith(w.rstrip("/") + "/")}
    old_ws = ws - new_ws
    if new_ws and old_ws:
        cands.extend(new_ws)
if len(cands) != 1:
    raise SystemExit(
        f"expected exactly one cut-over workspace, found {len(cands)}: {cands}"
        " — run §0 first and resolve it by hand before hashing anything")
D = Path(cands[0]) / "vault/.brain/skills"
for p in sorted(D.glob("*.skill")):
    with zipfile.ZipFile(p) as z:
        md = next(m for m in z.namelist() if m.endswith("SKILL.md"))
        v  = next((z.read(m).decode().strip() for m in z.namelist()
                   if m.endswith("VERSION")), "?")
        print(p.stem, v, hashlib.sha256(z.read(md)).hexdigest()[:16])
PY
```

**What that host table proves, and what it does not.** It proves the 14 bundles
STAGED into the new workspace are byte-identical to the worktree source. It does
NOT prove which bundle a Cowork session EXECUTES: a `.skill` is a ZIP the client
installs from an uploaded copy, and hashing files on the mount cannot interrogate
the loader (the mount does not even contain an unzipped `SKILL.md` — `find` over
it returns nothing). Execution provenance comes from the running skill itself,
two ways, both in the walk below:

1. **Version, through the broker.** Every skill routes its reads through the
   `brain-mcp` broker (DESK-07). `vm-doctor` (row 1) reports `brain --version`;
   it must read **0.20.32**. A session running a pre-cutover bundle reports the
   old version here, because the old bundles reference `$BRAIN_VAULT`/`$PWD/vault`
   paths that no longer resolve and fail differently.
2. **Behaviour that only the new bundle has.** The pre-cutover bundles read the
   vault OFF THE MOUNT (**12 of 14** named `$BRAIN_VAULT`, the variable that
   pointed at the vault on the mount — measured, not recalled:
   `for s in <the 14>; do git show 59f9ae4^:.claude/skills/$s/SKILL.md |
   grep -q BRAIN_VAULT && echo $s; done | wc -l` returns 12. An earlier draft
   of this line said "11 of 14 referenced a direct `/vault/.brain` path"; that
   number cited no command and no predicate reproduces it — the literal string
   `vault/.brain` appears in **zero** of the 14 pre-DESK-07 bundles); the
   staged ones read through the broker. So a skill that returns note content at
   all — `kb-curator` audit, `vault-eval`, `brain-inbox` — is running a bundle
   that reaches the vault through the desk, which the old ones cannot do once the
   folder no longer carries the vault. A skill that ERRORS trying to open a
   `/vault/.brain` path is a stale bundle: stop and re-upload.

**Verdict rule for §1.** Re-derive the table with the block above and compare
it to the STAGED column here — the payload the session actually executes. All
14 must match. Do NOT compare against the worktree source: a source file the
repository edits after a walk diverges legitimately (see `task-registrar`
above) and would halt the walk over a change that never reached the sandbox.
If a STAGED row differs, the staged artefact is wrong and there is
nothing to walk — fix staging first. Given 14/14 staged, §3 row 1 (`vm-doctor`
= 0.20.32) plus the through-the-broker behaviour of the content skills is what
proves the SESSION is on them.

---

## 2 · The VM-side read that actually closes the finding

Criterion 1 is about a shell command inside the VM, not about what the host says
a directory contains. **The verdict is the canonical scanner, not a hand-rolled
grep** — an anchored `grep '^(id|classification): '` counts `CLAUDE.md`,
`AGENTS.md` and the engine's own `init_samples.py` as hits and returns a
guaranteed false FAIL on the correctly-staged folder (measured 2026-08-29). The
scanner (`brain.cowork_leak_scan.recoverable_artifacts`) already encodes all
seven artefact classes and the template/package exemptions, and its host-side
run over this exact folder is `_evidence/s07/cut02-live-probe.txt` (four states,
each all-clear preceded by a known positive in the SAME shape).

### 2a · Resolve the mount — abort, never `head -1`

```bash
# Every non-pseudo mount this sandbox holds. A `head -1` here is a false
# all-clear generator: an empty match scans nothing and reads as clean, and a
# SECOND attached folder is exactly the bypass this must catch.
mount | grep -vE ' (proc|sysfs|tmpfs|devpts|cgroup2?|overlay|devfs|securityfs|debugfs|autofs) '
```

Read that list yourself. **Exactly one** attached workspace folder must appear,
and it must be the new one. If zero appear, the folder is not attached — stop.
If more than the new workspace appears, VULN-3385 is NOT closed by this cutover:
another folder reaches note content by another route. Set `WS` to the one you
confirmed, by hand:

```bash
WS=/path/you/just/confirmed          # NOT `ls ... | head -1`
# `exit 1`, not a bare echo. Until 2026-08-30 this line only PRINTED on a bad
# path, and §2b is a separate paste: a mistyped WS therefore reached §2b,
# whose `os.makedirs(..., exist_ok=True)` created the tree, planted the two
# bodies in it, found them, found nothing else, and printed PASS. Measured
# with WS=/tmp/s07typo. A verdict that a typo can produce is not a verdict.
test -d "$WS" || { echo "WS is not a directory — stop"; exit 1; }
# And it must be the MOUNT, not any directory. The attached workspace carries
# the staged engine; a scratch path does not.
test -d "$WS/vault/.brain/engine" || {
  echo "WS holds no staged engine at vault/.brain/engine — this is not the"
  echo "attached workspace. Stop and re-read the mount list above."; exit 1; }
```

### 2b · The assertion: the canonical scanner, known positive first

```bash
# The staged engine is on the mount; run the real predicate with it.
ENG="$WS/vault/.brain/engine"

# KNOWN POSITIVE, in the SAME predicate the assertion uses, on the SAME tree,
# including a note body inside a SQLite database (the shape a Markdown-only
# probe misses). A probe that never fired is not a probe.
python3 - "$ENG" "$WS" <<'PY'
import sys, sqlite3, tempfile, os
sys.path.insert(0, sys.argv[1])
from brain.cowork_leak_scan import recoverable_artifacts
ws = sys.argv[2]
# REFUSE TO CREATE THE THING UNDER TEST. §2a now exits on a bad WS, but this
# block is a separate paste and has to survive being run on its own: with
# `exist_ok=True` and no check, a mistyped WS made this script build a fresh
# tree, plant two bodies in it, find them, find nothing else, and print PASS.
if not os.path.isdir(os.path.join(ws, "vault", ".brain", "engine")):
    raise SystemExit(f"{ws} holds no staged engine — this is not the attached "
                     "workspace; re-run 2a and set WS by hand")
# plant one Markdown body and one SQLite body under the mount
d = os.path.join(ws, "vault", ".brain", "routines")
os.makedirs(d, exist_ok=True)
md = os.path.join(d, "S07VMPROBE.md")
db = os.path.join(d, "S07VMPROBE.bin")

# REFUSE A PRE-EXISTING MARKER. If either path is already there, an earlier run
# of this probe did not clean up, and overwriting it destroys the evidence of
# that. Stop and say so.
for _f in (md, db):
    if os.path.exists(_f):
        raise SystemExit(f"{_f} already exists — an earlier run of this probe "
                         "left it on the mount. Delete it from the HOST, "
                         "confirm why, then re-run.")

# THE `try` OPENS BEFORE THE FIRST WRITE, and that is the whole point. Until
# 2026-08-30 the Markdown plant and the entire SQLite setup ran ABOVE the
# `try:`, so anything failing between them -- a read-only mount, a full disk, a
# pre-existing `notes` table making CREATE TABLE raise -- left a
# `classification: Restricted` note body on the live attached folder with no
# cleanup at all. Reproduced by the adversarial round with a pre-existing
# table. The `finally` now covers every byte this block writes.
survivors = []
pos = None
try:
    open(md, "w").write(
        '---\nid: vmprobe\nclassification: Restricted\n---\n\nS07VMPROBE body\n')
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE notes(id TEXT, body TEXT)")
    c.execute("INSERT INTO notes VALUES(?,?)", ("p", "S07VMPROBE body"))
    c.commit()
    c.close()
    pos = recoverable_artifacts(ws)
finally:
    # OSError, not FileNotFoundError. A blocked remove raises PermissionError,
    # which a narrow `except FileNotFoundError` lets escape the `finally`
    # entirely -- and then the planted Restricted body stays on the attached
    # folder: the exact condition this probe exists to prove absent, created by
    # the probe.
    for _f in (md, db):
        try:
            os.remove(_f)
        except FileNotFoundError:
            pass
        except OSError as exc:
            survivors.append(f"{_f} ({exc.__class__.__name__}: {exc})")
    if survivors:
        # PRINTED FROM INSIDE THE `finally`, so it is still printed when the
        # scan above raised. Outside it, a raising scan propagated and the
        # operator saw a traceback while Restricted bodies sat on the mount.
        print("CLEANUP FAILED — these planted artefacts are STILL ON THE MOUNT:")
        for s in survivors:
            print("   ", s)
        print("Delete them from the HOST before trusting anything below.")

# A failed cleanup ENDS the probe. A verdict printed over surviving planted
# bodies is a verdict about a folder this script just contaminated.
if survivors:
    raise SystemExit("cleanup failed — no verdict; see the block above")

neg = recoverable_artifacts(ws)
_kinds = {a.kind for a in pos}
print("KNOWN POSITIVE:", len(pos), "artefact(s) —", sorted(_kinds))
print("ASSERTION     :", len(neg), "artefact(s) —", [str(a.path) for a in neg])
# BOTH detectors, not just one. Two bodies were planted -- a Markdown file and
# a SQLite row -- and `assert pos` passes when only one of them is found. The
# SQLite detector could stop firing entirely and this block would still print
# PASS, which is the failure this whole probe exists to rule out.
assert {"note_body_dump", "derived_index"} <= _kinds, (
    "the probe did not fire on both planted bodies — do NOT trust the "
    f"assertion; fired on {sorted(_kinds)}")
print("PASS" if not neg else "FAIL — a note body is recoverable from the mount")
PY
```

**Verdict rule for §2.** `KNOWN POSITIVE` must name **both** `note_body_dump`
and `derived_index` — one planted Markdown body and one planted SQLite body,
each found by its own detector. A count of 1 is a FAIL of the probe, not a
pass: it means one of the two detectors went silent. `ASSERTION` must be **0**. Any path printed
on the `ASSERTION` line is a real leak and s07 is not closed.

### 2c · The direct pen-test read (illustrative, not the verdict)

Reproduce what the pen tester did — ordinary file tools, no engine — as a sanity
check that the mount really is what the scanner says. This is NOT the pass/fail
gate (2b is), because a raw grep both over- and under-matches; run it to SEE the
folder, not to judge it.

```bash
# Note-shaped Markdown, by content. Expect ONLY the engine's placeholder
# templates (they open `id: "{{id}}"`, no note content).
grep -rla --binary-files=text -nE '^id: ' "$WS" 2>/dev/null \
  | grep -v '/_assets/templates/' | head
```

The host-side proof over the SAME folder with the SAME scanner is
`_plans/closed-stacks-2026-08-27/_evidence/s07/cut02-live-probe.txt` (an
absolute path in the plan directory, outside this worktree) — four states, each
all-clear preceded by a known positive in the same shape.

---

## 3 · The walk — 14 skills, one row each

Invoke each skill the way a person would, in the session, against the new
layout. **A skill that degrades gracefully still counts as BROKEN if it lost a
capability it had before the cutover.** Record the invocation issued, the
output returned, and the sha from §1.

**PRECONDITION, measured 2026-08-29.** Quit and reopen the desktop app before
the walk. A `brain-mcp` broker loads its tool table at process start; the four
brokers running that morning started at 08:55 and the release that ADDED the
`alerts`, `exceptions` and `inbox` tools finished installing at 10:03. The old
processes kept serving the six retrieval verbs, and a session correctly reported
that no `alerts` tool existed — while the installed package registered 15. A
stale broker is indistinguishable from a missing feature; restart first, then
believe the tool list.

**The degradation feed lane, before you start §3. SETTLED 2026-08-29 — read
this before you issue anything.** The always-loaded session-memory rule
requires `brain --role vm alerts` at session start. **That instruction is wrong
after this cutover, and the first walk attempt proved it.** The CLI lane reads
six files under `<vault>/.brain` (`pinned-verify.json`, `exceptions.json`,
`vault-id`, `maintain-state.json`, `notify-sent/`, `engine-feedback/`); all six
are present under the old vault and ABSENT from the new minimal workspace
(measured 2026-08-29, both sides). So the CLI reports `unreachable` — correctly,
failing closed rather than fabricating an all-clear.

Two consequences, both findings, neither a reason to stop the walk:

1. **Use the broker's `alerts` TOOL, not the `brain` command.** Post-DESK-07 the
   VM reaches `alerts`, `exceptions` and `inbox` through the broker's 15-tool
   surface, which is why the workspace legitimately carries no feed. The host
   called `dispatch("alerts", {})` on 2026-08-29 and got a real digest back with
   an empty `unreachable` list. Confirm the same from inside the session.
2. **Re-running `tools/cowork_workspace_install.sh` does NOT fix the CLI lane,
   and a session that proposes it is wrong.** `stage_pin` writes the identity
   anchor to `brain_runtime_dir(vault)` (`src/brain/exceptions_verify.py:284`),
   i.e. under the vault — which is no longer on the mount. Re-staging cannot put
   `pinned-verify.json` in the workspace. The real fix is doctrine: the
   session-memory rule and `AGENTS.md` must name the broker tool for a
   cut-over workspace. Raised as a finding for a later session; do not fix it
   inside the walk.

**FINDING (walk, 2026-08-30) — `capture` carries no target zone.**
`promote`'s whole job is putting a note in the RIGHT typed zone, and the seam
cannot express that. `mcp_capture_verbs.dispatch_capture` forwards exactly four
arguments to `core.capture()` — `content`, `note_id`, `note_type`,
`classification`. There is no zone parameter, so a Cowork promotion lands
wherever `core.capture()` routes it, regardless of what the skill decided.
Measured: a promotion the skill reasoned belonged in `brain/projects/` was filed
at `brain/resources/`. This is NOT a cutover regression — the VM leg never held
`brain write` — but it is a real capability gap in the broker's only write verb,
and the skill correctly reported the zone as the broker's call rather than
claiming the placement it proposed. Fix belongs in a later session: either add an
optional zone argument to `capture`, or have `promote` state plainly that the
zone is not its decision from the VM leg.

**FINDING (walk, 2026-08-30) — the eval skills never say which repo root
their paths are relative to.** `autoresearch/SKILL.md` and `vault-eval/SKILL.md`
both write `python3 eval/capture_run.py ...` with an implicit cwd, and neither
names a repo. A Cowork session filling that in reasonably chose the VAULT repo
and handed the owner `cd <vault-repo>` — where none of the four scripts exist.
Measured 2026-08-30: `<vault-repo>/eval/` holds only `golden-probes.json`; the
harness (`capture_run.py`, `harness_direct.py`, `gate.py`, `navigation_stats.py`,
`qrels/qrels.json`) lives in the ENGINE repo. Not a cutover regression — the
ambiguity predates it — but it turns both refusals into unrunnable handbacks.
Fix: name the engine repo root in both skills.

**FINDING (walk, 2026-08-30) — the qrels are keyed to the PRE-MIGRATION vault
layout.** `eval/qrels/qrels.json` keys judgments to legacy Obsidian paths
(`40 Meetings/...`, `50 Sources/...`), bridged by `eval/path_normalize.py`;
`golden_set.json` states the canonical key as "source_path (real owner-vault
relative path)". So a gate result is only meaningful when `--vault` points at
the vault those judgments were built against. The Cowork session raised this
unprompted and was right. Worth an explicit check in the skill's recipe.

**FINDING (walk, 2026-08-30) — `voice` is permanently degraded on the Cowork
leg, by decision.** The overlay is the only place owner identity lives, and it
is deliberately unindexed, so the cutover removed the VM's last route to it
(file access off the mount). Every drafting-facing kernel skill that reads
`overlay/{voice,brand,keywords,people}/` is affected, not just `voice`. The
skill handles it correctly and says so, so this is not a defect — but it IS a
real capability the sandbox lost, and the trade was made knowingly rather than
discovered here. Options if it ever matters: paste the profile into the prompt
(what the skill already suggests), or add an overlay-read desk tool that returns
config rather than notes. Do not solve it by indexing the overlay.

**FINDING (walk, 2026-08-30) — FIXED IN THIS SESSION. `task-registrar`'s
budget guard fired only when the manifest was CORRECT.** Its Phase 0 asserted
`lc['host_os_scheduled'] == 1`. The owner amended the lock to 2 on 2026-07-11
(brain-nightly + brain-synthesis-weekly), so from that day the guard raised
AssertionError on a healthy manifest and could never pass. A Cowork session ran
it verbatim and handed the owner a command that fails by construction. Fixed in
all three SKILL.md copies (`.claude/`, `.agents/`, `plugins/brainiac-extras/`):
the assertion now compares `locked_counts` against the tasks actually marked
`os_scheduled`, which is the drift it was reaching for and cannot rot when the
budget is amended again. Probed both ways — passes on the live manifest
(`{host: 2, vm: 0} -> ['brain-nightly', 'brain-synthesis-weekly']`) and still
fires when the declared count is perturbed.

**FINDING (walk, 2026-08-30) — the multilingual variant contract is
under-honoured on the Cowork leg, and nothing enforces it.** Four skills require
every search on a `multilingual: true` vault to carry a variant per entry in
`vault_languages`. The 2026-08-30 walk session read the census, issued ONE
variant query, and dropped it on every call after — self-reported in its own
retrospective. Measured on the reference vault 2026-08-30: 3146 notes, of
which 3010 classified; `vault_languages: [en, pt]`; **586 pt notes = 19.5% of
CLASSIFIED notes, 18.6% of all notes** (136 are unclassified, so the census's
`shares.pt` uses the classified denominator — state which one, because the two
differ by nearly a point). So a monolingual EN session under-retrieves close to
a fifth of the corpus by construction, silently, with no signal that it
happened. The rule lives only in skill prose; the broker's `search` neither
requires nor records a variant. Worth a mechanism rather than more prose — e.g.
`search` stamping which languages a query covered, so the omission is visible in
the result rather than only in a retrospective. (Also noted: `es` has 14 notes,
below the 2% threshold, so it is deliberately outside `vault_languages`.)

| # | skill | invoke it by | what "working" means |
|---|---|---|---|
| 1 | `vm-doctor` | "run vm-doctor" | **CORRECTED 2026-08-30.** The three staging probes do NOT run, and that is the pass. The installer stages the engine at `<vault>/.brain/brain` and never on `PATH`; the only thing that ever put `brain` on `PATH` was the per-session bootstrap the skill deleted on 2026-08-28. So `command -v brain` finding nothing is the NORMAL staged state, not evidence of an unstaged workspace. WORKING = the skill reports a NON-`yes` VERSION-READY verdict — its own report format at `vm-doctor/SKILL.md:216` is `VERSION-READY: yes|no`, so a session following its contract prints `no`; this row said `unknown` until 2026-08-30, which is a value the skill never emits and would have marked a compliant session BROKEN — says verbatim that no `brain` is on this session's `PATH` and that staging cannot be confirmed from here, falls through to the desk digest, and points the owner at the host for the version. BROKEN = it hunts for the binary under the mount, exports a `PATH`, bootstraps a vault path, or reports "no staged engine". |
| 2 | `brain-inbox` | "what's in the owner inbox?" | lists the open owner questions (or a clean zero) — reached via the broker's `inbox` tool, added by DESK-07 |
| 3 | `graph-explorer` | "open the graph explorer" | **CORRECTED 2026-08-30.** The page is a HOST artefact and always was — the skill's own description says `brain graph-report` is refused at role=vm and that in a Cowork session it must report the view as host-side instead. This is NOT a cutover regression. WORKING = it declines, says why (host-broker verb, not one of the desk's tools, `.brain/` host-only by contract), and hands the owner the host command. BROKEN = it tries to read a copy off the mount, or claims the page is missing. |
| 4 | `curation` | "run a curation pass" | **CORRECTED 2026-08-30.** `curate` is a host-broker verb and is named as such in the desk block the skill itself pins; it was never available on the VM leg, so this is NOT a cutover regression. WORKING = it declines, names `curate` as host-broker and absent from the desk, hands over the host command, and notes that the Sunday nightly already runs §C and §E. BROKEN = it invents a substitute, reads the vault off the mount, or reports a curation result it did not obtain. |
| 5 | `kb-curator` | "audit the vault" | **CORRECTED 2026-08-30 (second review pass).** The `audit` mode IS `brain health --json`, which folds `status` + `verify-audit` + a probe search, and the skill's own line 45 names `ingest`, `curate`, `health`, `integrity`, `verify-audit` and `graph-report` as host-broker. None of the six is among the desk's 15 tools, so "returns statistics over `vault/brain` + `vault/raw`" is a criterion the VM leg cannot satisfy by contract - and it is NOT a cutover regression. WORKING = it declines, names the mode's host-broker verb, and hands over the host command; offering the desk-reachable substitutes it CAN run (`bases-query` enumeration, `grep`) and labelling them as not an audit is a bonus. BROKEN = it reports health or integrity figures it could not obtain, or reads the vault off the mount. |
| 6 | `promote` | "promote `2026-08-30-f9-placeholder-chase-2026-08-23`" | **CORRECTED 2026-08-30.** The audited write path is `brain write`, a host-broker privilege. The broker registers 14 read tools plus exactly one write verb, `capture`, which stages an UNSIGNED draft — `mcp_adapter.WRITE_TOOLS == ("capture",)`. So a VM leg can never complete the move into `brain/projects|areas|resources/`; that is the design, not a cutover regression. Name a REAL draft when invoking it — an unqualified "promote this" tests nothing. WORKING = the skill generates the frontmatter, runs the duplicate check against the index through the broker, stages the draft via `capture`, and names the host command that finishes the promotion. Refusing to guess which note was meant also counts as working. BROKEN = it invents a target, writes into the PARA zone off the mount, or reports a promotion it did not perform. |
| 7 | `save-conversation` | "save this analysis as a note" | writes a typed note; auto-classifies the tier. **CHECKED AND LEFT AS WRITTEN 2026-08-30 (second review pass).** This row is NOT the same case as row 6, and the difference is worth stating because a reviewer read them as identical. `capture` IS the broker's one write verb (`mcp_adapter.WRITE_TOOLS == ("capture",)`), and `mcp_capture_verbs.dispatch_capture` says in its own docstring that `core.capture()` is "signed immediately on the host, staged unsigned in `capture-inbox/` on the VM leg". The broker RUNS ON THE HOST, so a Cowork session's capture is signed and indexed - which is what the walk observed on 2026-08-30 (two notes written, signed and indexed). Row 6 fails not because `capture` cannot write but because PROMOTION additionally needs a target zone, and `dispatch_capture` forwards no such argument. |
| 8 | `vault-ingestion` | "capture `deliverables/<client>_ai_strategy_6pager_v62.docx` (the real filename carries the client name; it is a denylisted term, so it is redacted here — use the actual file when running the walk)" | **CORRECTED 2026-08-30.** "A binary lands in `raw/originals`" is not testable from Cowork. `ingest` is a host-broker verb and is NOT among the broker's 15 tools (14 read + `capture`), so the VM leg cannot run the drop-zone pipeline at all. Name a REAL source, as with row 6. WORKING = the skill recognises the shape, says the `.docx` lane is `brain ingest` and host-only, hands over the exact host command, and states the standing rule that the same session writes the citing `brain/` note. BROKEN = it claims to have ingested the binary, reads it off the mount, or routes a binary through `capture` as if it were text. The pasted-text/URL lane THROUGH `capture` is the part the VM can actually execute; test that separately. |
| 9 | `vault-eval` | "run the retrieval eval" | **CORRECTED 2026-08-30.** The eval is host-only at every layer, and none of it is a cutover regression. `brain-golden-probe` is a binary; `eval/capture_run.py`, `harness_direct.py`, `gate.py` and `navigation_stats.py` are repo scripts outside any connected folder; `brain status --json` is VM-allowed on the CLI but is not one of the 15 desk tools; and the baseline and trace write to `_evidence/eval/` in the repo. The qualitative cascade's five dimensions DO run on desk-served read tools, but step 3 needs `navigation_stats.py` and step 5 writes a non-regenerable trace, so a desk-only run is an anecdote. WORKING = the skill declines, names the blocker PER LAYER, hands over the four host commands with the exit-code rule (1 = abort, 2 = undecided, not a pass), and refuses a half-run rather than reporting unrecorded scores. Offering to author the fixed question set FIRST — the anti-cherry-pick rule — is a bonus, not a requirement. BROKEN = it scores the cascade from the desk and reports it as an eval, or reaches for `--allow-drift`. |
| 10 | `autoresearch` | "tune the retriever" | **CORRECTED 2026-08-30.** Host-only for the same reason as row 9 — the loop IS the harness, and `capture_run.py` / `harness_direct.py` / `gate.py` have no desk equivalent. The desk DOES expose `hybrid_search` with `rrf_k`, `rerank`, `rerank_top` and `k`, so an improvised few-query comparison is reachable and is exactly what the skill's own guardrail forbids: a candidate that looks better without a gate PASS and a positive delta is reverted. WORKING = it declines, names the harness as the blocker, explicitly refuses the eyeball substitute and cites the guardrail, and hands over the host recipe with the one-knob-per-iteration bound. BROKEN = it runs a few queries at a changed `rrf_k` and recommends a value. |
| 11 | `voice` | "rewrite <a real paragraph> in my voice" + the voice gist pasted in | **CORRECTED 2026-08-30.** "Reads the overlay's voice layer" is precisely what a Cowork session cannot do. `notes.scan_vault` skips `overlay/` by design — it is config, not knowledge — so it has NEVER been in the index and no desk tool returns it; measured, the index holds zones `brain` and `raw` only, and 0 of the vault's 10 overlay files. Before the cutover the VM read those files off the mount; now it cannot. The skill's own Phase 0 was already updated for this and says so verbatim: "HOST LANE, and the Cowork lane has no substitute". WORKING = it names the gap, offers to work from a pasted profile or to draft neutral AND LABEL IT, and asks for the missing text. BROKEN = it goes looking for the directory, substitutes a `search` for the overlay, or returns a rewrite claiming the owner's voice without it. Supply a REAL paragraph; a bare "this" stalls it. |
| 12 | `task-registrar` | "register the scheduled tasks" | **CORRECTED 2026-08-30.** Phases 0 and 1 read `routines/manifest.json` and `scripts/register_tasks.py` in the ENGINE repo, so they are host-only. Phase 3 IS the Cowork leg's job — the VM's only "apply" is a human pasting the dry run's prompt into a session. WORKING = it reports the account's current trigger list, declines to improvise triggers the manifest does not declare, names the manifest as the source of truth, and asks for the dry-run prompt. Two bonus signals seen 2026-08-30: naming the poke-only design (no cron on the VM, ever — T1053.005) rather than quietly wiring one, and stating that an empty VM list says NOTHING about host-side registration. BROKEN = it invents plausible triggers from the skill description, or reads an empty list as "nothing is scheduled anywhere". |
| 13 | `improve` | "run a retrospective" | **CORRECTED 2026-08-30.** It cannot review AGENTS.md, `docs/`, `tools/validate.py` or `_evidence/_improve_learnings.md` — all engine-repo files, none connected — so Phase 0 (prior acceptance rates) and Phase 2 (config audit) are host-only, and Phase 5 ("apply on Accept") cannot write. Phase 3, conversation analysis, is the whole of what the VM leg can do. WORKING = it names the skipped phases rather than proceeding as if the data were there, answers the forced Phase 1 choice itself, and returns findings only. BROKEN = it reports acceptance rates it could not read, or claims to have applied a change. |
| 14 | `chief-of-staff` | "run the chief-of-staff pass" | **CORRECTED 2026-08-30 (third review pass).** `SKILL.md` is superseded; `DOCTRINE.md` v7.3 is the running doctrine, and its operative phases need `cos-run-begin`, `cos-broker`, `cos-corpus-*` and `cos-evidence` — every one of them on `vm-boundary-probe.sh`'s own `must_refuse` list — plus the host mailbox lane. So "executes DOCTRINE.md" is a criterion the VM leg cannot satisfy by contract, and this is NOT a cutover regression; it is the same class as rows 4, 5, 9 and 10. WORKING = it names DOCTRINE.md v7.3 as the running doctrine rather than SKILL.md, declines the run, names the host-broker verbs its phases need, and warns about the run-manifest mis-stamp. BROKEN = it follows the superseded SKILL.md, or reports a pass it did not run. |

**The originals hand-off, in the same session** (CUT-03 requires it, and no
host-side test can prove it — `tests/test_originals_staging.py` stops at the
broker boundary):

1. Request an original in each of the 12 document formats FROM the session.
2. Confirm each file actually opens.
3. Confirm cleanup: the staged original is gone when the hand-off ends.

**Not tested here: per-session isolation of a staged original.** An earlier
draft of this step asked for a second concurrent session that cannot read the
first's document. The owner RETIRED that property on 2026-08-27 (s05b, criterion
4's per-session isolation clause struck — the broker cannot distinguish two
Cowork sessions, and `src/brain/cowork_staging.py` records that there is no
lease or reaper). Demanding it would fail the walk for a property the design
deliberately does not provide. If a lease mechanism is ever built, the
isolation check belongs with it, not here.

**A walk that marks even one skill broken FAILS s07.** Report the breakage;
do not fix it in the session — the fix belongs in a rework of the session that
owns that verb.

---

## 4 · What to hand back

One table with 14 rows: skill, invocation issued, output summary, executed
bundle sha, WORKING/BROKEN. Plus the §2 verdict, and the §1 mismatch list if
any. That is the run record CUT-03 asks for and s09's criterion 2 reads.
