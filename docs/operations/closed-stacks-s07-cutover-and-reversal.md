# Moving a vault off its Cowork mount — cutover and reversal

**Status: EXECUTED 2026-08-29, by a procedure this file did not originally
contain.** §A below is what actually ran and how to reverse it. §0–§6 are the
VAULT-MOVE procedure, written and adversarially reviewed on 2026-08-29 before
the owner ruled it out; they were NOT executed. They are kept because they are
still the procedure if a vault itself ever has to move, and because the review
findings folded into them are real.

---

## A · What was actually executed (owner Ruling 1, 2026-08-29)

The owner ruled that **the mount is emptied by RE-REGISTERING, not by moving
the vault**. The registry keys a Cowork mount on `workspace_path`, not
`vault_path` (`~/.brainiac/workspaces.json`), so registering a new, minimal
workspace and ceasing to attach the old folder removes the vault AND every
residual in one step — and needs no per-tree retention ruling for the 645 MB of
non-vault material (76 .docx, 37 .pdf) the vault-move procedure would have left
behind on the mount.

**The consequence that matters: the cutover is DATA-FREE.** Nothing moves. The
vault stays exactly where it was, byte for byte. `config.index_dir()` keys on the persistent `vault-id` (`<vault>/.brain/vault-id`,
here `7f0d4a0d366d8bb3`), falling back to an absolute-path hash only when no id
exists. The vault path AND the id are both unchanged, so the derived index keeps
its identity either way and the long-lived `brain-mcp` brokers keep answering
from the correct place — the frozen-index trap §4 was built to catch cannot
fire, because nothing a broker resolves has changed.
`config.snapshot_dir()` still falls back to `<vault>/.brain/snapshot`, which is
off the new mount by construction.

### A.1 · What ran

    OLDWS=<the folder Cowork attached>       # a workspace holding the vault
    VAULT=$OLDWS/vault                       # unchanged
    NEWWS=$HOME/CoworkWorkspaces/<name>      # the minimal replacement
    REG=$HOME/.brainiac/workspaces.json
    REGBAK=$REG.pre-s07-cutover              # cp -n, never overwritten

The two folder names are this host's, so they are not written here — the
shipped tree bakes in no operator path. Print them from the registry:

    python3 -c "import json,pathlib;rows=json.loads((pathlib.Path.home()/'.brainiac/workspaces.json').read_text())['entries'];print(sorted({r['workspace_path'] for r in rows if r['target']=='cowork-vm'}))"

The verbatim originals, with this host's real paths, are in
`_evidence/s07/reversal-recorded-before-the-change.md` (evidence files are
excluded from the public export).

1. Writers quiesced for the switch window only (10:12:28Z → 10:24:06Z). The
   `launchctl` state found BEFORE the change is recorded verbatim in
   `_evidence/s07/launchd-state-before.txt`; only the two `StartInterval=3600`
   jobs were touched, and both were restored and then **verified by running
   them** (`launchctl kickstart -p`, then observing a real bash parent with a
   real Python child: 64199→64207 and 64297→64316). A job's *status* is not
   evidence that it runs.
2. `$NEWWS` created and staged with
   `tools/cowork_workspace_install.sh "$VAULT" <model-cache> <dist> "$NEWWS"`
   — the four-argument form. 848 MB, no snapshot database, no note bodies.
3. `$REG` backed up to `$REGBAK` (`cp -n`), then this vault's entries
   for THIS host repointed from `$OLDWS` to `$NEWWS`.
4. `brain doctor`'s VULN-3385 row for `$NEWWS` verified **current**; the row
   for `$OLDWS` still reports the leak, which is correct — that folder still
   holds the vault and is simply no longer attached.

### A.2 · THE REVERSAL — recorded BEFORE the change

Written to `_evidence/s07/reversal-recorded-before-the-change.md` at
2026-08-29T10:1x UTC, before the first mutating command. Reproduced here with
`$OLDWS`/`$NEWWS`/`$REG`/`$REGBAK` from A.1 in place of this host's literal
paths; the evidence file holds the verbatim original. Four lines:

```bash
# 1. restore the registry from the backup taken before the rewrite.
#    The backup is written BEFORE the rewrite and is never overwritten by a
#    re-run (`cp -n`), so a second cutover attempt cannot destroy the original.
cp -f "$REGBAK" "$REG"

# 2. confirm the registry is back: this must print the OLD workspace path for
#    EVERY row naming this vault (the filter is not host-scoped, so on this
#    host that is 4 rows — 2 under a former hostname, 2 for this machine — not
#    2). None may still read $NEWWS.
VAULT="$VAULT" REG="$REG" python3 -c "import json,os;print([e['workspace_path'] for e in json.load(open(os.environ['REG']))['entries'] if e['vault_path']==os.environ['VAULT']])"

# 3. remove the staged workspace. It is a directory this session CREATED, and
#    every byte in it is a copy of something in the repo or in <vault>/.brain
#    — WITH ONE EXCEPTION: vault/.brain/capture-inbox/ is the VM's draft_capture
#    drop, and an unsigned draft there exists nowhere else until the host drains
#    it. It is empty as of the cutover, but if the walk (§CUT-03) ran a capture
#    skill first, drain and confirm empty BEFORE deleting:
#        ls -A "$NEWWS/vault/.brain/capture-inbox"
#    (run `brain write` / the host drain on any draft first). Then:
rm -rf "$NEWWS"

# 4. confirm the old staging is intact and was never touched.
ls "$OLDWS/vault/.brain/engine/brain/_version.py"
```

There is no step 5. The vault is not read for content, the index is not
rebuilt, the audit chain is not written to, and the old `<vault>/.brain`
staging tree is left in place and working. **One file under the OLD vault WAS
rewritten by staging** and the reversal does not restore it:
`<vault>/.brain/pinned-verify.json` (the VM's identity anchor), which
`exceptions_verify.stage_pin` writes to `brain_runtime_dir(vault)` — i.e. under
the vault, not into the new workspace. It is an idempotent rewrite of the same
public key + vault_id the nightly re-stamps anyway, so there is nothing to undo;
it is named here only because "never touched" would otherwise be false.

**The one thing the reversal does not undo, and does not need to.** Staging
runs `brain sync` and `brain snapshot --dest <vault>/.brain/snapshot`. Both
republish IN PLACE at the existing path, and both are what the hourly
`com.brainiac.nightly.*` job does anyway.

**The step only the owner can take, in BOTH directions.** Cowork's folder
attachment is a Claude Desktop GUI action. The host cannot attach or detach a
folder, and cannot see from here which folders a live Cowork session has
mounted. Detaching `$OLDWS` and attaching `$NEWWS` — and the reverse — are the
owner's.

---

## The vault-move procedure (§0–§6) — RECORDED, NOT EXECUTED

Superseded by §A. Everything below was written before Ruling 1 and describes
moving the 3.2 GB vault itself. Three independent adversarial reviews on
2026-08-29 (a Claude fork, a Codex `xhigh` pass, and the plan's own
`llm-review-high` gate) raised 33 findings between them (13 + 12 + 8, with
overlap); the mechanical ones are fixed in the text below. Six were recorded as
prerequisites the re-run owned, and Ruling 1 dissolved or the re-run closed all
six:

1. **No phase journal; the procedure is not resumable.** DISSOLVED — §A moves
   nothing, so there is no partial-move state to resume from.
2. **The VM's degradation feed follows the vault off the mount.** DISSOLVED —
   the vault does not move, and DESK-07 already put `alerts`, `exceptions` and
   `inbox` on the broker's 15-tool surface, so the VM reads the feed through
   the desk rather than off the mount.
3. **`brain update` does not carry `$BRAIN_SNAPSHOT_DIR`.** CLOSED — the
   snapshot directory is now a registry field (`workspaces.py`), and
   `update_channels._workspace_sync()` carries it into the child environment
   (and STRIPS an inherited one when the registry has none, so an operator's
   shell cannot leak a path into a sync).
4. **The documented setup route bypasses the refusal.** CLOSED — all three
   copies of `brainiac-cowork-setup`, both copies of `setup-cowork`, and
   `docs/cowork-windows-install.md` now show the four-argument invocation and
   say why the fourth is not optional. Pinned by
   `tests/test_cowork_documented_setup_route.py` (7 tests), which counts the
   arguments on every runnable invocation in every shipped document. It pinned
   18 at the time this was written; the One Command per Vault plan (s03,
   2026-08-31) replaced the hand-paste route in the two `brainiac-cowork-setup`
   surfaces and retired `setup-cowork`, so five of those documents no longer
   show a runnable invocation at all and left the enumeration. What the file
   still guards is unchanged; the replacement coverage for the retired rows is
   `tests/test_install_docs_name_the_command.py`.
5. **The Cowork skill bundles are not published.** CLOSED by owner Ruling 2 —
   refresh by the STAGED path, not a public release. All 14 bundles were
   rebuilt and staged, and verified 14/14 by extracting each zip and comparing
   the member `SKILL.md` sha256 against the worktree source.
6. **This procedure moves the VAULT, and the mount holds more than the vault.**
   DISSOLVED — §A detaches the whole folder, vault and residuals together.

**What it closes.** VULN-3385: a Cowork sandbox reads the attached workspace
folder with ordinary file tools, so every note body underneath it bypasses the
classification egress gate and the SEC-06 read record. Measured on the live
reference mount 2026-08-29: `vault/brain` (1098 `.md`), `vault/raw` (3047
`.md`), and `vault/.brain/snapshot/index.snapshot.sqlite`, whose `notes` table
carries **3105 non-empty bodies** that `strings` alone recovers.

**Paths are parameters, not constants.** This file ships in the clean-room
export, so it names no operator home and no workspace by name. **Write these to
`cutover-env.sh` and `source` it at the top of §2, §3 and §5** — §5 is read
alone, hours later, after something went wrong, and a fence that depends on
variables set two hundred lines above aborts on `set -u` at the first line that
matters:

**Every value in it is a LITERAL, resolved once, before anything moves.** An
earlier draft made `cutover-env.sh` recompute `$IDX` from `BRAIN_VAULT="$OLD"
brain status` every time it was sourced. §3 moves `vault-id` out of `$OLD`, and
§1.1 of this document is the reason that matters: a vault with no `vault-id`
resolves to a DIFFERENT, path-hashed index directory. So §5 — sourced hours
later, after the move — would have resolved `$SNAP` to a directory that does
not exist, and `mv "$SNAP" …` would have aborted under `set -e` after the notes
were already back and before the snapshot and the four config files were
restored. A reversal that stops half way is worse than one that never starts.

```bash
# Run these THREE lines by hand first; everything else is generated from them.
WS=<the attached Cowork workspace folder>        # `jq -r '.[].workspace_path' ~/.brainiac/workspaces.json`
OLD="$WS/vault"
NEW="$HOME/BrainVaults/$(basename "$WS")/vault"

# The derived index directory. Read it from `brain status`, NOT from
# `brain doctor --json | jq -r .index_dir` — doctor's JSON has no `index_dir`
# key, `jq -r` on a missing key prints the STRING "null" and exits 0, and
# `set -euo pipefail` does not catch that. The earlier draft of this file made
# exactly that mistake, and the consequence was not cosmetic: `$SNAP` became
# the relative path `null/snapshot`, so §3 would have moved
# `index.snapshot.sqlite` — 3105 non-empty MNPI bodies — into a directory
# called `null` under whatever the operator's shell happened to be in, and
# exported `BRAIN_SNAPSHOT_DIR=null/snapshot` for every publish afterwards.
# `BRAIN_VAULT` matters too: without it `brain status` resolves the CURRENT
# DIRECTORY as the vault and `.index.db` is `null` again.
IDX="$(dirname "$(BRAIN_VAULT="$OLD" brain status --json | jq -r '.index.db')")"
case "$IDX" in
  /*) ;;
  *) echo "IDX did not resolve to an absolute path: '$IDX' — stop." >&2; exit 1 ;;
esac

# Now FREEZE all five as literals. Note the unquoted heredoc delimiter: the
# shell expands them here, once, and never again.
cat > "$HOME/cutover-env.sh" <<EOF
WS="$WS"
OLD="$OLD"
NEW="$NEW"
IDX="$IDX"
SNAP="$IDX/snapshot"
EOF
grep -q '\$(' "$HOME/cutover-env.sh" && {
  echo "cutover-env.sh still contains a command substitution — it must hold" >&2
  echo "literals only, or §5 resolves different paths than §3 used. STOP." >&2
  exit 1; }
source "$HOME/cutover-env.sh"
```

Verified on the reference host 2026-08-29: `BRAIN_VAULT="$OLD" brain status
--json | jq -r '.index.db'` prints
`…/Library/Application Support/profile-a-brain/vaults/vault-7f0d4a0d/index.sqlite`.
`BRAIN_VAULT` matters — run without it, `brain status` resolves the CURRENT
DIRECTORY as the vault and `.index.db` is `null` again.

---

## 0 · Designated paths

| what | before | after |
|---|---|---|
| attached workspace (unchanged) | `$WS` | same |
| **vault** | `$WS/vault` | **`$NEW`** |
| VM staging (unchanged place) | `$WS/vault/.brain` | same — engine, model, vendor, bin, skills, routines, capture-inbox |
| published snapshot | `$WS/vault/.brain/snapshot` | `$SNAP` (via `$BRAIN_SNAPSHOT_DIR`) |
| derived index (unchanged) | `$IDX` | same |
| **the ADR-0010 shelf** | `$WS/brain-deliverables` | **`$NEW/../brain-deliverables`** — 119 files, 83 MB, header declares `Highest classification here: MNPI`; `brain status --json` reports it at `deliverables.path`, so the engine WRITES here |
| **`migration/`** | `$WS/migration` | **`$NEW/../migration`**, or deleted — 46 of 46 `.md` carry vault note frontmatter |

`$HOME` and the workspace's parent are on the same device (`stat -f %d` →
identical on the reference machine, measured 2026-08-29), so every move below is
a **rename**: instantaneous, atomic, and needing no free space. That matters —
the reference volume is at **97 %** (829 Gi used of 926 Gi, 32 Gi free).
**Check it first**, because a cross-device `mv` silently becomes a copy:

```bash
# EVERY destination, not just $HOME. $SNAP derives from the index directory,
# which `$BRAIN_INDEX_DIR` may point at another volume entirely — and that is
# the move carrying 3105 MNPI bodies. Compare the device of each destination's
# nearest EXISTING ancestor, since $NEW and $SNAP do not exist yet.
_dev() { d="$1"; while [ ! -e "$d" ]; do d="$(dirname "$d")"; done; stat -f %d "$d"; }
src_dev="$(_dev "$WS")"
for dest in "$HOME" "$NEW" "$SNAP"; do
  [ "$(_dev "$dest")" = "$src_dev" ] || {
    echo "CROSS-DEVICE: $dest is on another volume; the mv below becomes a" >&2
    echo "copy-then-unlink. STOP." >&2; exit 1; }
done
```

A warning printed above two hundred lines of output is not a guard, so this
exits. On a cross-device layout the move is a 3.3 GB copy against a volume this
document records at 97 % full with 32 GiB free, and there is no point part-way
through a copy at which either §3 or §5 is a correct thing to run. An operator
who deliberately overrides it owes themselves a free-space precondition first:
`df -g "$HOME" | awk 'NR==2 && $4 < 10 {exit 1}'`.

---

## 1 · Two things s01's JOB-4 table does not say, and both are load-bearing

The plan says to move each residual exactly as s01 JOB 4's absolute-path table
specifies. Executing it literally would break the vault. The table covers the
large directories; it has **no row** for the small files at the root of
`vault/.brain/`, and two of them decide whether this move is cheap or ruinous.

### 1.1 `vault-id` must travel WITH the vault

`config.index_dir()` keys the per-vault app-data directory on the persistent
`vault/.brain/vault-id` file when one exists, and falls back to a hash of the
absolute path when one does not. The reference vault has had a `vault-id` since
2026-07-13. **Probed 2026-08-29** by pointing `index_dir()` at two synthetic
vaults:

```
with vault-id copied : …/profile-a-brain/vaults/vault-7f0d4a0d   (== the live one)
without vault-id     : …/profile-a-brain/vaults/vault-c2ef78e5   (a new, empty dir)
```

Carry the file and the index survives the move untouched — no re-embed. Leave
it behind and the engine mints a fresh empty directory: a full rebuild of
3105+ notes, **and** the COS approved queue, attachment anchors and capture
corpus at the old directory (`index_dir()`'s own docstring: "NOT ENTIRELY
DISPOSABLE") stop being reachable from this vault.

### 1.2 The VM-facing files under `.brain/` must STAY

`brain --role vm alerts` — the one command every Cowork session is required to
run first — reads `notify-sent/current.json`, `engine-feedback/*.md`,
`exceptions.json`, `pinned-verify.json` and `maintain-state.json` from the
staging root. `exceptions.html` is the page the owner opens. None carries a note
body; all must remain under `$WS/vault/.brain/`.

### 1.3 The `.brain/` root entries the table above does not rule on

Ruled here, so the re-run has one list to execute rather than a judgment call
per file. "STAYS" means the VM needs it and it carries no note body.

| entry | ruling | why |
|---|---|---|
| `AGENTS.md`, `brain` (shim), `bin/`, `engine/`, `model/`, `vendor/`, `skills/`, `routines/`, `capture-inbox/`, `vm-egress-tier` | STAYS | s01 JOB 4, unchanged |
| `vm-boundary-probe.sh`, `vm-selftest.sh` | STAYS | the VM runs them |
| `notify-sent/`, `engine-feedback/`, `exceptions.json`, `exceptions.html`, `maintain-state.json` | STAYS | the files `brain --role vm alerts` reads (§1.2), plus the page the owner opens |
| `vault-id` | **GOES** | §1.1 — the index directory follows it |
| `snapshot/` | **GOES** | §1.4 — 3105 note bodies |
| `capture-inbox-resolved/` | **GOES** | holds the bodies of drafts already drained |
| `cos/` | **GOES** | 60 MB, 433 `.md` of which **429** carry vault note frontmatter at `classification: MNPI` — mostly rejected COS proposals under `cos/host/proposals/claim-quarantine`. §3 already moves it; this row exists because the table is what an operator reads to decide, and it was the one entry §3 moved without a ruling |
| `audit-drift-dispositions.json` | **GOES** | host-private by INT-02: it decides whether tampering counts as explained, and a match needs only path + issue + hash, all known to whoever edited the note |
| `ingest-manifest.json`, `health-history.jsonl`, `health-sparse.jsonl`, `maintain-state.json.bak-pre-retention-rerun` | **GOES** | host maintenance state; nothing on the VM leg reads them |
| `writer.lock` | **LEAVE IN PLACE** | dated 2026-08-03, vestigial: INT-05 moved the real lock under `$HOME/Library/Application Support/profile-a-brain/locks/` and no code path reads this one. It is not a sync that died holding a lock and nothing is blocked by it. Deleting a pre-existing file the session did not create needs the owner's word |

One symlink worth recording because s01 flagged it as needing work and it does
not: **`$WS/AGENTS.md` -> `vault/.brain/AGENTS.md` is RELATIVE, and it points
into the staging root, which STAYS.** It survives the move untouched. s01's
"retarget if `.brain` moves" does not apply, because `.brain` does not move.

One absence worth recording rather than fixing here: **`pinned-verify.json` is
not present** on the reference mount. `brain --role vm alerts` verifies the
signed exceptions summary against it, so a Cowork session reports `unreachable`
rather than a count. That predates this session and is not s07's to close.

### 1.4 The `snapshot/` conflict resolves as GOES

s01 recorded `snapshot/` as both "must stay (the VM read surface)" and "must go
(435 MNPI bodies)". **It goes.** s06b moved all 14 Cowork skills onto the
broker's MCP tools, so the VM no longer reads a local snapshot. Publish it to
`$BRAIN_SNAPSHOT_DIR` instead — a host-only directory. Note that
`config.snapshot_db_path()` does *not* follow `$BRAIN_INDEX_DIR`, so this
override has to be set explicitly wherever `brain snapshot`/`sync --publish`
runs, including the launchd jobs.

---

## 2 · Preconditions (all four, or do not start)

1. **The DESK-07 skill bundles are published and installed.** Measured
   2026-08-29: `Autopsias/brainiac` HEAD is `39e0e31c…`, the same sha s01
   recorded on 2026-08-27 — nothing from s06b is published — and **0 of 14**
   staged `.skill` bundles match the migrated worktree source, while **11 of 14**
   still reference `$BRAIN_VAULT` / `$PWD/vault` / `$BRAIN_RUNTIME_DIR`. Moving
   the vault under those bundles breaks every one of them. This is the blocker
   that closed s07.
2. **Quiesced.** No `com.brainiac.*` launchd job loaded (`launchctl list`), and
   no `brain sync`/`rebuild`/`maintain` running.
3. **Brokers accounted for.** `ps` shows the live `brain-mcp` children of
   Claude Desktop. Measured 2026-08-29: **8** Python brokers under 8
   `disclaimer` parents, from **4** Desktop MCP entries — two of which expose
   the SAME vault.
4. **A verified inventory**, captured *before* the move:

**Count the zones that MOVE, and exclude `.brain/`.** A peer session measured
the trap on 2026-08-27: a scan that skips `cos-ops/` and `inbox/` but not
`.brain/` counted **390 extra `.md` files**, most of them rejected COS proposals
under `.brain/cos/host/proposals/`, which carry real note frontmatter. A line
count alone cannot expose a scope error, so print the FILE count beside anything
else you count.

```bash
find "$OLD/brain" -name '*.md' | wc -l           # 1098 on the reference vault, 2026-08-29
find "$OLD/raw"   -name '*.md' | wc -l           # 3047
find "$OLD" -name '*.md' -not -path "$OLD/.brain/*" | wc -l   # 4791 = the vault proper
find "$OLD" -name '*.md' | wc -l                 # 5318 = the same plus .brain — NOT the vault
ls "$OLD/raw/originals" | wc -l                  # 1564
```

The byte manifest covers **only what moves**, and that is not a shortcut — the
reversal RE-STAGES `.brain/` (§5 step 4), so `.brain/engine`, `.brain/skills`
and `.brain/routines` legitimately change bytes. A manifest over the whole of
`$OLD` would make a correct reversal produce a non-empty `diff`, which is a
false alarm an operator has no way to tell from a real one. It also avoids
hashing the ~2.1 GB of model and vendor staging that never moves.

```bash
manifest() {                       # $1 = vault root, $2 = output file
  root="$1"; out="$2"; list=""
  for p in brain raw overlay inbox cos-ops .brain/vault-id \
           .brain/capture-inbox-resolved .brain/curation .brain/graph .brain/cos \
           .brain/exports .brain/brief .brain/memory .brain/eval; do
    if [ -e "$root/$p" ]; then list="$list $p"; fi
  done
  ( cd "$root" && find $list -type f -print0 | sort -z | xargs -0 shasum -a 256 ) > "$out"
}
manifest "$OLD" "$HOME/brain-cutover-manifest-before.txt"
```

It builds the list from what EXISTS first, deliberately: `find` given a missing
starting point returns non-zero, and under `set -euo pipefail` that aborts the
whole script at the one step whose job is to make the move reversible. **Run
2026-08-29 against the live reference vault: exit 0, 8892 files, 24 s.**

---

## 3 · The cutover

```bash
set -euo pipefail
source "$HOME/cutover-env.sh"

# (0) BACK UP THE FOUR CONFIGURATION FILES FIRST. This is the real point of no
#     return, and it is not the `mv`. A rename is reversible for as long as
#     $NEW is intact; a hand edit to a config file has no recorded prior state,
#     and §5 would otherwise reverse it from this document's memory.
#     It REFUSES to overwrite. Every attempt used to `cp` into the same fixed
#     paths, so a retry after a hand edit replaced the pre-cutover originals
#     with post-cutover state, and §5 would then "restore" the broken config.
BK="$HOME/brain-cutover-backup"
[ -e "$BK" ] && {
  echo "$BK already exists. It is the ONLY record of the pre-cutover config." >&2
  echo "Move it aside deliberately before retrying; do not overwrite it." >&2
  exit 1; }
mkdir -p "$BK"
cp ~/.brainiac/workspaces.json "$BK/"
cp ~/Library/Application\ Support/Claude/claude_desktop_config.json "$BK/"
cp ~/Library/LaunchAgents/com.brainiac.*.plist "$BK/"
chmod -R a-w "$BK"

# (1) quit Claude Desktop — it owns every broker process — and WAIT for the
#     brokers to exit. A broker mid-`sync` races the rename.
osascript -e 'quit app "Claude"'
for _ in $(seq 60); do pgrep -qf brain-mcp || break; sleep 1; done
pgrep -qf brain-mcp && { echo "brokers still alive after 60s — stop." >&2; exit 1; }

# (2) refuse a destination that is not clean. An aborted earlier attempt leaves
#     $NEW/brain in place, and `mv` would then nest it as $NEW/brain/brain
#     silently — after which §5 moves the wrong tree back.
#     It covers EVERY destination step (3) and step (4) write, not just the
#     five note directories: $SNAP, the vault-id, the shelf and migration/ each
#     nest silently too.
for d in "$NEW/brain" "$NEW/raw" "$NEW/overlay" "$NEW/inbox" "$NEW/cos-ops" \
         "$NEW/.brain/vault-id" "$SNAP" \
         "$NEW/../brain-deliverables" "$NEW/../migration"; do
  [ -e "$d" ] && { echo "destination not clean: $d exists — stop." >&2; exit 1; }
done

#     …and refuse a PARTIAL source. Step (3) moves five directories in one `mv`
#     and `mv` is not atomic across arguments: with `cos-ops` missing it moves
#     four and exits 1, leaving the corpus split across $OLD and $NEW with no
#     record of which phase it reached. Check every source first.
for d in brain raw overlay inbox cos-ops; do
  [ -e "$OLD/$d" ] || { echo "source missing: $OLD/$d — stop before moving" >&2
                        echo "anything; step (3) would move the others and abort." >&2
                        exit 1; }
done

# (3) create the destination and move the NOTES and the host-side runtime.
mkdir -p "$NEW/.brain"
mv "$OLD/brain" "$OLD/raw" "$OLD/overlay" "$OLD/inbox" "$OLD/cos-ops" "$NEW/"
mv "$OLD/.brain/vault-id" "$NEW/.brain/vault-id"          # §1.1 — not optional
for d in curation graph cos exports brief memory eval cos-ops \
         capture-inbox-resolved audit-drift-dispositions.json \
         ingest-manifest.json health-history.jsonl health-sparse.jsonl \
         maintain-state.json.bak-pre-retention-rerun; do
  [ -e "$OLD/.brain/$d" ] && mv "$OLD/.brain/$d" "$NEW/.brain/$d"
done
mkdir -p "$(dirname "$SNAP")" && mv "$OLD/.brain/snapshot" "$SNAP"

# (4) move the two things that are NOT under $OLD and that §6's scanner will
#     otherwise report — correctly — the moment the vault is relocated.
[ -e "$WS/brain-deliverables" ] && \
  mv "$WS/brain-deliverables" "$NEW/../brain-deliverables"   # ADR-0010: <vault>/../
[ -e "$WS/migration" ] && mv "$WS/migration" "$NEW/../migration"

# (5) everything still under $OLD/.brain is the VM staging and STAYS.

# (6) point the host at the new vault.
export BRAIN_VAULT="$NEW" BRAIN_SNAPSHOT_DIR="$SNAP"
brain doctor --json | jq '.rows[] | select(.surface|test("VULN-3385"))'
```

**Step (3) renames the snapshot DIRECTORY.** The obvious form —
`mv "$OLD/.brain/snapshot"/* "$SNAP"/` — silently skips dot-entries and fails
outright on an empty directory, and neither failure announces itself. Probed on
the reference mount: the snapshot directory holds `export-snapshot.json`,
`index.snapshot.sqlite` and `snapshot.manifest.json` and no dot-entries, so the
glob would not have bitten today. That is not a reason to keep it.

Then, still before restarting anything:

* **registry** — `~/.brainiac/workspaces.json`: set `vault_path` to `$NEW` on
  every entry whose `workspace_path` is `$WS` (4 entries on the reference
  machine), leaving `workspace_path` alone.
* **Desktop MCP config** — `~/Library/Application Support/Claude/claude_desktop_config.json`:
  set `BRAIN_VAULT` to `$NEW` and add `BRAIN_SNAPSHOT_DIR` for every entry that
  serves this vault.
* **launchd plists** — add `BRAIN_SNAPSHOT_DIR` and update `BRAIN_VAULT` in
  every plist that names the old path. Find them rather than trusting a list;
  three matched on the reference machine (the per-vault nightly, the COS
  nightly, and the retrieval watch):

  ```bash
  grep -rl "$OLD" ~/Library/LaunchAgents/*.plist
  ```
* **staging** — re-run the installer with the new four-argument form, which is
  what the relocation support is for:

```bash
tools/cowork_workspace_install.sh "$NEW" "$OLD/.brain/model" dist "$WS"
```

`$1` is still **the vault** — that never changed. What changed is that the
script no longer assumes the vault lives in the workspace: `$4` is the attached
folder, defaulting to `$VAULT/..` so every existing two- and three-argument
invocation behaves exactly as before. With `$4` given, the staging root comes
from `brain.cowork_staging.staging_root()` (the same resolver
`src/brain/update_channels.py` uses), and the script refuses outright — exit 2,
artefacts named — if the attached folder still yields a note body.

* **`brain connect`** — re-run it, then relaunch Claude Desktop.

---

## 4 · Verification (the live process, not a status proxy)

**The failure mode is not the one the plan predicted, and it is worse.**
Because `index_dir()` is keyed on `vault-id` (§1.1) and `BrainCore.get()` reads
the *index*, not the file, a broker still holding the OLD vault path resolves
the SAME index directory and keeps answering **correctly**. It will not error,
and no query can tell you whether it was restarted. So verify the process:

```bash
open -a Claude                     # §3 quit it; the brokers only exist after this
n=$(pgrep -f brain-mcp | wc -l)
[ "$n" -gt 0 ] || { echo "no brokers running — relaunch Desktop first" >&2; exit 1; }
ps -ax -o pid=,ppid=,lstart=,command= | grep '[b]rain-mcp'     # all newer than the relaunch
for p in $(pgrep -f brain-mcp); do
  ps eww "$p" | tr ' ' '\n' | grep -E '^BRAIN_(VAULT|SNAPSHOT_DIR)=' || echo "pid $p: NEITHER var set"
done
```

**The count comes first, and it is not pedantry.** With no brokers running the
`for` loop body never executes, the output is empty, and "every one printed the
new path" is vacuously true — an all-clear that equals no input. It is also the
EXPECTED state at this point, because §3 step (1) quit Desktop, so a check
without the guard would be run against zero processes on the first attempt.

Every one must print the new path, and every pid must post-date the relaunch.
Two stated limits: `ps eww` prints the environment space-separated, so a value
containing a space cannot be read this way — `$SNAP` lives under `Application
Support`, so read `BRAIN_SNAPSHOT_DIR` from the Desktop config and the plists
instead of from `ps`. And the `|| echo` is what keeps a broker with NEITHER
variable set from passing silently.

Then, through the broker in a real Claude Desktop session:

1. `search` for a **sentinel note written after the move** — it cannot exist in
   the old location, so a stale index or a stale copy cannot answer it;
2. `get` that note by id;
3. `recent`, and check the sentinel is at the top.

And on the mount itself:

```bash
python3 -m brain.cowork_staging --vault "$NEW" --workspace "$WS"   # exit 0, prints the staging root
brain doctor --json | jq -r '.rows[] | select(.surface|test("VULN-3385")) | .status'
```

That row is `current` when the folder yields nothing, and `stale` — a **gating**
status, so the run fails — if anything puts a body back. See §6.

---

## 5 · The reversal, recorded before the move

Every step above is a rename on one device, so the reversal is the same renames
backwards. It costs no copy and no rebuild.

```bash
set -euo pipefail
source "$HOME/cutover-env.sh"     # §0 wrote it; this fence is READ ALONE, so
                                  # it defines its own $WS/$OLD/$NEW/$IDX/$SNAP
manifest() {                       # §2's function, repeated for the same reason
  root="$1"; out="$2"; list=""
  for p in brain raw overlay inbox cos-ops .brain/vault-id \
           .brain/capture-inbox-resolved .brain/curation .brain/graph .brain/cos \
           .brain/exports .brain/brief .brain/memory .brain/eval; do
    if [ -e "$root/$p" ]; then list="$list $p"; fi
  done
  ( cd "$root" && find $list -type f -print0 | sort -z | xargs -0 shasum -a 256 ) > "$out"
}

# (1) quit Claude Desktop, and wait for the brokers, exactly as §3 does —
#     INCLUDING the abort. This fence waited and then moved regardless, so a
#     broker mid-`sync` raced the reversal's renames.
osascript -e 'quit app "Claude"'
for _ in $(seq 60); do pgrep -qf brain-mcp || break; sleep 1; done
pgrep -qf brain-mcp && { echo "brokers still alive after 60s — stop." >&2; exit 1; }

#     …and refuse to start unless the frozen environment and the backup are
#     both present. Everything below depends on them; discovering that at the
#     `cp` on line 3 of step (3) means the notes have already moved.
for f in "$HOME/cutover-env.sh" \
         "$HOME/brain-cutover-backup/workspaces.json" \
         "$HOME/brain-cutover-backup/claude_desktop_config.json" \
         "$HOME/brain-cutover-manifest-before.txt"; do
  [ -e "$f" ] || { echo "reversal prerequisite missing: $f — stop." >&2; exit 1; }
done
[ -d "$NEW" ] || { echo "$NEW is gone; §5 restores bytes, not a backup." >&2; exit 1; }

#     …and refuse if anything was RECREATED at the destination between the two
#     runs. §3 step (2) refuses a non-empty destination for exactly this reason
#     and the reversal needs the same guard: `mv` into an existing directory
#     nests silently ($OLD/brain/brain), and the step (5) manifest diff would
#     only report it AFTER the renames, to be untangled by hand.
for d in "$OLD/brain" "$OLD/raw" "$OLD/overlay" "$OLD/inbox" "$OLD/cos-ops" \
         "$OLD/.brain/vault-id" "$OLD/.brain/snapshot" \
         "$WS/brain-deliverables" "$WS/migration"; do
  [ -e "$d" ] && { echo "$d already exists — $OLD is not the empty shell §3 left." \
                        "Inspect it by hand; do NOT run this fence." >&2; exit 1; }
done

# (2) move the notes and the host-side runtime back.
mv "$NEW/brain" "$NEW/raw" "$NEW/overlay" "$NEW/inbox" "$NEW/cos-ops" "$OLD/"
mv "$NEW/.brain/vault-id" "$OLD/.brain/vault-id"
for d in curation graph cos exports brief memory eval cos-ops \
         capture-inbox-resolved audit-drift-dispositions.json \
         ingest-manifest.json health-history.jsonl health-sparse.jsonl \
         maintain-state.json.bak-pre-retention-rerun; do
  [ -e "$NEW/.brain/$d" ] && mv "$NEW/.brain/$d" "$OLD/.brain/$d"
done
mv "$SNAP" "$OLD/.brain/snapshot"
[ -e "$NEW/../brain-deliverables" ] && \
  mv "$NEW/../brain-deliverables" "$WS/brain-deliverables"
[ -e "$NEW/../migration" ] && mv "$NEW/../migration" "$WS/migration"

# (3) RESTORE the four configuration files from the backup §3 step (0) took.
#     Copying them back beats re-applying edits from memory: this document
#     cannot know what else those files contained.
cp "$HOME/brain-cutover-backup/workspaces.json" ~/.brainiac/
cp "$HOME/brain-cutover-backup/claude_desktop_config.json" \
   ~/Library/Application\ Support/Claude/
cp "$HOME/brain-cutover-backup"/com.brainiac.*.plist ~/Library/LaunchAgents/
unset BRAIN_SNAPSHOT_DIR BRAIN_VAULT

# (4) re-stage in the historical co-located form and reconnect. Both the
#     script path and `dist` are RELATIVE: run this from the engine checkout
#     (the same directory §3's staging step was run from), not from $HOME.
tools/cowork_workspace_install.sh "$OLD" "$OLD/.brain/model" dist
brain connect
open -a Claude

# (5) confirm the reversal — the SAME manifest() function from §2, same paths.
manifest "$OLD" "$HOME/brain-cutover-manifest-after-reversal.txt"
diff "$HOME/brain-cutover-manifest-before.txt" "$HOME/brain-cutover-manifest-after-reversal.txt"
```

`diff` empty means every moved file came back with the same bytes at the same
relative path. It deliberately says nothing about `.brain/` staging, which the
reversal re-stages on purpose.

**What the safety property actually is, stated precisely, because the previous
wording of this paragraph was wrong in the most dangerous direction.** It said
"the source is removed only after §4 passes", which describes a
copy-then-verify-then-delete procedure. §3 does not do that. §3 RENAMES. From
step (3) onward there is exactly one copy of the corpus and it is at `$NEW`;
there is no second copy, no later deletion step, and no moment at which an
untouched original exists. Nothing is DESTROYED — a rename is reversible, which
is why §5 restores bytes rather than a backup — but §5 is the only way back,
and it depends on `$NEW` being intact. Treat `$NEW` as the corpus from step (3)
onward, not as a copy of it.

The reversal deliberately does NOT touch the derived index — it never moved
(§1.1).

**It does not re-enable the launchd schedule either, and that is a
PREREQUISITE FOR CALLING THE ROLLBACK COMPLETE, not a footnote.** Step (3)
copies the plists back; a plist on disk is a file, not a running job. A host
whose config is restored and whose `brain-nightly` is still unloaded looks
reverted and is not: nothing sweeps, ingests, drains, syncs or republishes, and
the only symptom is a snapshot that quietly stops advancing. Re-enabling is a
separate step because s07's dispatch forbade this session from touching launchd
at all, not because it is optional. Whoever runs §5 finishes with:

```bash
launchctl print "gui/$(id -u)/com.brainiac.nightly" >/dev/null 2>&1 \
  || echo "brain-nightly is NOT loaded — the rollback is not complete."
```

and reloads every job §3's quiesce unloaded before declaring the host restored.

---

## 6 · Keeping it off

Moving the vault once is not the property. The property is that the vault is
not on the mount, and it survives a reinstall, a restage, a crash or a manual
copy. Two mechanisms own it.

**A refusal at every staging entry point — and there are THREE, not two.**
`brain.cowork_leak_scan` carries the one scanner; `brain.cowork_staging` carries
the relocation-aware `staging_root()` and the refusal built on it.

| entry point | reached by | how it refuses |
|---|---|---|
| `tools/cowork_workspace_install.sh` | this runbook, `/brainiac-cowork-setup` | `python3 -m brain.cowork_staging --vault … --workspace …`, exit 2 |
| `brain.update_channels.stage_engine_and_skills` | `/brainiac-update` | `assert_relocated_vault_leaves_no_note_bodies` |
| `brain.provision._stage_cowork_runtime` | PRV-10 provision-drain | passes the workspace as the installer's `$4`, so the first row's refusal applies |

**The third row is a hole peer review found on 2026-08-29, and it is worth
stating why it was invisible.** `provision.py` called the installer with THREE
arguments. The installer puts its entire refusal behind `if [ -n "$4" ]`, and
its else branch ASSUMES the attached folder is `$VAULT/..`. So the provisioning
lane skipped the refusal, and on a relocated vault it resolved the staging root
to `<vault>/.brain` — off the mount, invisible to the Cowork sandbox the lane
exists to serve — after which its own stamp check verified that same wrong-side
path and reported `staged`. Both are fixed: the workspace is passed, and the
stamp now checks the staging root. Scope, stated rather than inflated: this bites
a vault being provisioned, not an already-registered one, because
`_provision_one` returns `already-registered` before it reaches the staging call.

**A recurring `brain doctor` row**, `Cowork workspace note-body leak
(VULN-3385)`, one per registered workspace folder. It scans the whole attached
folder (not the registered `vault_path`, which would miss a copy dropped
anywhere else) for seven artefact classes, each proven by reading the data
rather than by the presence of a directory:

| class | proven by |
|---|---|
| `vault_tree` | a `brain/` or `raw/` directory holding at least one `.md` |
| `snapshot` | a sqlite database whose `notes` table has non-empty bodies, on a `snapshot` path |
| `derived_index` | the same query, anywhere else |
| `escaping_symlink` | a symlink out of the folder whose target is a note zone or a body-carrying database |
| `staged_original` | a non-empty `originals/` directory (no lease mechanism exists — s05b was retired 2026-08-27 — so anything found is abandoned by definition) |
| `note_body_dump` | any directory whose `.md` files open with vault note frontmatter (`id:` plus `classification:`/`type:`) — on the FRONTMATTER, not on a directory name |
| `deliverables_shelf` | a `brain-deliverables/` directory: the ADR-0010 shelf, whose payload is the archived original behind each note |

**The last two classes are why this check can fail at all.** With the first
five, every finding on the live reference mount — the two note zones and the
snapshot database — is something §3 MOVES. So the row would have gone `current`
the moment the cutover ran, while two things stayed on the mount and stayed
readable by `cat` from any Cowork session. Both were measured 2026-08-29:

| what stays behind | size | measured |
|---|---|---|
| `$WS/migration/` (`decisions-staged`, `samples`) | 1.0 MB | 46 of 46 `.md` carry vault note frontmatter |
| `$WS/brain-deliverables/` | 83 MB | the ADR-0010 shelf; its own header declares `Highest classification here: MNPI` |

**So §3 is not finished until both are moved.** Neither is under `$OLD`, so the
move commands do not reach them:

```bash
mv "$WS/migration" "$NEW/../migration"                 # or delete, if it is dead staging
mv "$WS/brain-deliverables" "$NEW/../brain-deliverables"   # ADR-0010 wants it beside the vault
```

The shelf regenerates at `<vault>/../brain-deliverables` on the next
`brain maintain`, so moving it is what stops the nightly writing a second copy
off-mount while the on-mount one is left to rot.

**Seventeen** artefacts are reported on the reference mount, re-measured
2026-08-29 after the adversarial round removed the scanner's depth cap and its
prune list (2.9 s in-process; observed wall clock ranges to ~40 s under disk
contention, which is I/O, not CPU). Fourteen are inside `$OLD` and leave with
it; the remaining three are the two `migration/` subdirectories and the shelf —
exactly what the two `mv` lines above clear. Run the scan again after them and
the row is `current` because the folder is empty of note bodies, not because the
check stopped looking.

**The status split, stated so it is auditable.** The row is `stale` — a gating
status, which fails the run — only once every vault registered against that
folder lives outside it. While a vault is still inside, the same finding is
reported in full at `manual-required`: visible, with the remediation, but not
gating. That is deliberate. Before the cutover the finding *is* VULN-3385,
which is open and known; gating on it would red `brain doctor` and every `brain
update` (which exits 1 on any gating row) on this machine until the cutover
lands, for a condition no operator can clear at the doctor prompt. **Until a
vault is relocated, this row cannot fail a run.**

`tests/test_cowork_staging_off_the_mount.py` probes every class with a known
positive *and* a known negative, and pins the flip from reported to failing. It
also carries one test per free all-clear the 2026-08-29 adversarial round bought
— a note at depth 7, a note under a directory named `vendor`, a note filed
fourth in a directory, a body renamed `.txt`, a notes database renamed
`payload.bin`, and a symlink to an ordinary directory of notes. Every one of
those returned NO FINDINGS while `cat` still printed the body; each was
re-verified as a KNOWN NEGATIVE by restoring the old behaviour in-process and
confirming the marker goes invisible again.

---

### What this closes, and what it does not — the sentence s08 must inherit

**Accurate:** *the vault is off the mount, and a recurring check fails if a
note body returns to it.*

**Not accurate:** *VULN-3385 is closed.* The remediation in this document is
scoped to `vault/`, and the attached folder is `workspace_path` — the whole
root, not the vault. Moving `vault/` relocates ~3.3 GB of ~3.5 GB and removes
one child; it does not empty the mount. Measured at the reference root
2026-08-29, still readable by a Cowork session with `cat` after the move:

| tree | files | size | binary documents |
|---|---|---|---|
| `deliverables/` | 325 | 46 MB | 31 `.docx`, 17 `.pdf` |
| `brain-deliverables/` | 119 | 83 MB | 44 `.docx`, 16 `.pdf` |
| `migration/` | 66 | 1.0 MB | — |
| `_cos_build/` | 75 | 11 MB | — |
| `tmp/` | 56 | 19 MB | 1 `.docx`, 4 `.pdf` |

§3 moves `brain-deliverables/` and `migration/`. The rest stays, and
`automation_discovery/` (485 MB) is not even in that table. **The scanner in
this section cannot see most of it**: it proves a leak from note frontmatter or
from a sqlite `notes` table, and a `.docx` has neither. That is a deliberate
scope, not an oversight — deciding whether an arbitrary Word document is
vault-derived is not a check that can be made to fail honestly — but it means
the check going `current` is evidence about NOTE BODIES and nothing else.

**s01's JOB-4 artefact does not resolve this.** It is
`_evidence/s01/job4-mount-inventory.md`, and it is an INVENTORY: every mount-root
entry by absolute path and size, including all six trees above. It assigns no
MOVES/STAYS disposition to any of them. The plan spec calls it "s01's JOB-4
absolute-path table" and treats it as the authority for what stays and what
goes; the rows exist, the dispositions do not. **That gap is the plan's to
close, not this document's** — s07 raised it rather than widening its own scope
to sweep six trees nobody classified.

**There may be a stronger shape,** and the re-run should price it rather than
inherit this one by default: move the WORKSPACE and re-register it, instead of
moving the vault out of the workspace. Same effort, and it leaves behind no
mount whose remaining contents nobody enumerated. This document does not adopt
it because the plan's disposition authority (above) does not cover the residual
trees either way — that is a decision for whoever closes the gap.
