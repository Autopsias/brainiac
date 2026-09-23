# ADR 0012 — How AGENTS.md reaches every harness

- **Status:** Accepted — option B (owner, 2026-09-19). Step 1's model run confirmed it
  the same day: from a trusted nested worktree, with no `-c` trust flag, Codex listed a
  token on the last line of a 127,777-byte `AGENTS.md` (plan *AGENTS.md fits every
  harness*, s03, `_evidence/agents-md-fits-every-harness/s03-probe-after.txt`).
- **Context repo:** Profile A `brain`

Codex reads only the first 32,768 bytes of this repository's `AGENTS.md`. The file is
127,734 bytes. So the second half of section 4 and all of sections 5 to 9 never reach a
Codex session here, and nothing warns.
This record lays out three ways to fix that, what each costs, and one recommendation.

**No rule changes meaning in any option.** Text moves or is reordered. It is never
rewritten. The only new text any option adds is navigation: a pointer line, a table
heading, a stub under a section heading.

---

## 1 · Context

### 1.1 Measured facts

Every fact below names its source. Anything not measured is labelled **unverified**.

| # | Fact | Source |
|---|---|---|
| F1 | Codex is `codex-cli 0.153.4`. `~/.codex/config.toml` sets no `project_doc_max_bytes`. | s01-probe.txt §1; re-read 2026-09-19 with `grep -n project_doc ~/.codex/config.toml` (no match). |
| F2 | Codex loaded exactly the first 32,768 bytes of the project `AGENTS.md`, cut mid-sentence, with no marker and no warning. Two model runs agree. | s01-probe.txt §5 runs 1-2, §6 (byte compare of the session record). |
| F3 | The global `~/.codex/AGENTS.md` (3,112 bytes) does **not** share the project budget. Token T2 at offset 31,022 was listed in both baseline runs. | s01-probe.txt §3, §6. `wc -c ~/.codex/AGENTS.md` = 3,112 on 2026-09-19. |
| F4 | In the real file, byte 32,768 falls on line 517, inside §4 Capture rules. | s01-probe.txt §6; re-measured 2026-09-19 (line 517). |
| F5 | `AGENTS.md` is 127,734 bytes. It was 89,578 bytes at `0b4defc2` (2026-08-22, the context diet that created `AGENTS-core.md`). That is +38,156 bytes in 28 days, about 1,363 bytes a day. | `wc -c AGENTS.md`; `git show 0b4defc2:AGENTS.md \| wc -c`. |
| F6 | Section sizes in UTF-8 bytes: header 1,661 · §1 10,006 · §2 6,525 · §3 1,886 · §4 23,209 · §5 58,374 · §6 10,663 · §7 677 · §8 6,191 · §9 8,542. | Script splitting `AGENTS.md` on `^## ` lines, 2026-09-19. |
| F7 | A project `.codex/config.toml` is ignored whole when trust comes only from `-c` on the command line. That is why s01 run 3 and run 4 showed no end-of-file token. | s01-probe.txt §7, renders A and A'. |
| F8 | With trust written in a config FILE, the project `project_doc_max_bytes = 262144` IS honoured, including from a git worktree nested under the trusted path. | s01-probe.txt §7, renders D, F, G. **Label: local prompt render (`codex debug prompt-input`), not a model run.** |
| F9 | This checkout is trusted in `~/.codex/config.toml` (line 354, `trust_level = "trusted"`). Plan worktrees live under it in `.plan-worktrees/`. | Re-read 2026-09-19 with `grep -n -A1`. |
| F10 | An untrusted directory still loads `AGENTS.md` (cut at 32 KiB). Only `.codex/config.toml` needs trust. | s01-probe.txt §7, renders C and E. |
| F11 | A repo-root `AGENTS.override.md` replaces `AGENTS.md`: the render showed the override's token and not the `AGENTS.md` token. A symlink named `AGENTS.override.md` is followed: the render showed the target file's token. A directory with only `AGENTS.md` showed that file's token (known positive). | Measured for this record, 2026-09-19, three scratch git repos, `codex debug prompt-input "hi"`. **Label: local render, not a model run.** |
| F12 | A client's Codex receives only `BRAIN_USAGE_PARAGRAPH` (390 bytes, measured with `len(BRAIN_USAGE_PARAGRAPH.encode())`), written into the client's own `AGENTS.md` as a marked block. | `src/brain/connect.py:51` and `:126`. |
| F13 | The packaged copy `src/brain/_assets/AGENTS.md` is byte-identical to `AGENTS.md`. It is staged to `<vault>/.brain/AGENTS.md` and inlined in full into the Cowork workspace `CLAUDE.md`. Its reader is Claude in Cowork, where no 32 KiB cut applies. | s01-readers.md §2, §4; `tools/cowork_workspace_install.sh:534-588`. |
| F14 | Cowork auto-loads a workspace-root `CLAUDE.md` but does not expand `@`-imports, so the contract must be inlined. | Comment in `tools/cowork_workspace_install.sh:538-539` ("verified 2026-07-20"). Not re-measured here. |
| F15 | The `.claude/rules/*.md` copies have drifted from `AGENTS.md`. Five paragraphs over 80 characters are not verbatim in `AGENTS.md`: capture-and-invariants 1, pre-commit 1, retrieval-and-security 3. `host-vm-trust.md` (§6) matches. | Paragraph-substring check, 2026-09-19 (method in step 0 below). |
| F16 | `AGENTS-core.md` is 10,491 bytes. Its headings are §1, §5, §8 (twice, a duplicated line at 138 and 140) and "Where the rest lives". | `wc -c`; `grep -n '^## ' AGENTS-core.md`. |
| F17 | About 85 `AGENTS.md §N` references sit in `src`, `tools` and `tests`. | s01-readers.md §6. |

**Reorder alone cannot fix this.** §5 is 58,374 bytes on its own, larger than the whole
32,768-byte budget. No ordering of the sections puts all of them inside the limit.

**What Codex sees today.** The header, §1, §2, §3 and the first 12,690 bytes of §4
(32,768 minus the 20,078 bytes before §4). It never sees §5 to §9. That includes §6
Host / VM trust split and §8 Before you commit.

### 1.2 Who reads each file at startup

| File | Harness that reads it at startup | Size limit that applies |
|---|---|---|
| `AGENTS.md` (repo root) | Codex in this repo; Gemini CLI via `.gemini/settings.json`; the Desktop Code tab. Claude Code reads `AGENTS-core.md` instead. | Codex: 32,768 bytes, measured (F2). Gemini CLI and Desktop Code tab: **unverified**. |
| `src/brain/_assets/AGENTS.md` | No harness reads it at startup. It is a shipping copy that feeds the two rows below. | None at startup. |
| A client's `<vault>/.brain/AGENTS.md` | Not auto-loaded. The Cowork session prompt points at it as the paste-it-yourself fallback. | None measured. It is read on demand. |
| The Cowork `BRAIN-CONTRACT` block in the workspace `CLAUDE.md` | Claude in Cowork, auto-loaded. | No 32 KiB cut (F13). Any Cowork limit is **unverified**. |
| A client's own `AGENTS.md` marked block (`brain connect`) | The client's Codex. | 32,768 bytes, but the block is 390 bytes (F12). |

**So the Codex cut harms sessions in THIS repository only.** No client's Codex reads the
full contract. Every option below is judged with that in mind: clients gain nothing from
any of them.

**What this record does not cover.** Gemini CLI and the Desktop Code tab also read
`AGENTS.md`. They may have their own limits. This record did not measure them.

### 1.3 The budget formula every guard uses

No guard compares against a bare 32,768. Every guard and every proof uses:

> **usable project bytes = configured budget − shared global bytes − headroom margin**

- **configured budget:** 32,768, or under option B the tracked `project_doc_max_bytes`.
- **shared global bytes:** **0.** s01 T2 was listed, so the global `~/.codex/AGENTS.md`
  does not share the budget (F3). If a later Codex release changes that, this term
  becomes the global file's byte size, and the guard must be re-derived.
- **headroom margin:** named per option below.

All sizes are UTF-8 bytes (`len(path.read_bytes())`), never characters.

---

## 2 · Two rules that hold in every option

1. **Section numbers do not change.** About 85 `AGENTS.md §N` references point at them
   (F17). A moved section keeps its number and leaves its heading in place.
2. **Each section has ONE canonical home**, named per option. The `.claude/rules/*.md`
   files stay copies of that home. Every option adds a copy guard, because nothing
   enforces the copies today and five paragraphs have already drifted (F15).

---

## 3 · The three options

### Option A — `AGENTS.md` becomes the short core for every harness

#### A.1 What changes

`AGENTS.md` shrinks to a core that fits the limit. It keeps the header, §1, §2, §3 and
§7 in full (20,755 bytes, measured). §4, §5, §6, §8 and §9 move verbatim into
per-section documents, for example `docs/agents/04-capture-rules.md`. Each moved
section leaves its heading in `AGENTS.md`, plus a stub that names its document and the
task that requires it. Estimated core size: about 23 KB. **Estimate, not measured on a
built file.**

Canonical homes: header, §1, §2, §3, §7 in `AGENTS.md`; §4, §5, §6, §8, §9 in their
`docs/agents/` document. The `.claude/rules` copies become copies of those documents.

#### A.2 Effect on the tests in s01-readers.md

- `test_agents_md_is_canonical_brain_usage` **goes red.** All four pinned tokens sit
  in §5 (byte offsets 44,567 to 88,264). The test must read the core plus the §5
  document.
- `test_framework_sync` mirror tests pass only if the mirror check grows to cover every
  `docs/agents/` document.
- `test_update_restage` (RET-08) still passes on `AGENTS.md`. It must grow a byte-equal
  check for each staged document.
- `test_init_full`, the `BRAIN-CONTRACT` fixture tests, the `@AGENTS-core.md` import
  test and the Gemini settings test are unaffected.

#### A.3 Effect on the packaged copy and on clients who already received it

The packaged copy becomes the core, so every shipping path must carry the moved
documents. `brain update` re-stages only `.brain/AGENTS.md` today
(`src/brain/update_channels.py:229`). The workspace `CLAUDE.md` block is re-synced only
by `tools/cowork_workspace_install.sh`. **If the install step inlines only the core,
every Cowork session loses the trust rules in §6.** The Cowork "contract probe" in the
generated banner checks that "§6 Host / VM trust split" is visible, so it would also
fail. A client who already received the full copy keeps it until the next install run.
Clients gain nothing: Cowork has no 32 KiB cut, and a client's Codex never read this file.

#### A.4 Effect on each open branch that edits these files

- `plan/commitments-identity-2026-08-26` (parked) adds 2,408 bytes to §2. §2 stays
  inline, so the merge is textual, but it spends most of the headroom.
- `worktree-improve-claude-md-plan-state` edits a `CLAUDE.md` status paragraph near
  line 40. No overlap with the line-6 fix below. Clean merge **unverified**.

#### A.5 What it costs a Codex session in bytes

About 23 KB at startup, less than today's 32,768. Each on-demand document adds its own
size when opened: §5 58,374, §6 10,663, §8 6,191 (F6).

#### A.6 Steps

0. **Reconcile the copies.** For each drifted paragraph (F15), show the owner both
   versions. The canonical `AGENTS.md` text wins unless the owner rules otherwise. Check:
   the paragraph-substring script (each copy paragraph over 80 characters, frontmatter
   and the "Moved verbatim" banner removed, must be a substring of its canonical home)
   reports 0.
1. Create `docs/agents/04-…`, `05-…`, `06-…`, `08-…`, `09-…`, each holding its section
   byte for byte. Check: concatenating the core's kept spans and the documents in
   section order reproduces today's `AGENTS.md` minus the moved bodies, with `cmp`.
2. Replace each moved body in `AGENTS.md` with its heading plus a trigger stub. Codex
   has no path-scoped loading, so each stub names the task, for example:
   "Before any change under `src/brain/` or `vault/.brain/`, read
   `docs/agents/06-host-vm-trust.md`." A document nothing triggers is never read.
3. Add each document to `ENGINE_ASSET_FILES` in `tools/package_clients.py:143`. Check:
   `tools/package_clients.py --validate-only` passes and `src/brain/_assets/docs/agents/`
   holds every document.
4. Stage each document in `src/brain/vmstaging.py:119` and
   `src/brain/update_channels.py:229`. Check: `test_update_restage` extended with a
   byte-equal assertion per document passes.
5. Change the `BRAIN-CONTRACT` inline step in `tools/cowork_workspace_install.sh`
   (both the `awk` re-sync and the generated-file branch) to inline the core followed
   by every document. Check: a test runs the script against a tmp workspace and finds
   the §6 heading and the last line of each document inside the block.
6. Extend `check_agents_md_mirror` in `tools/framework_sync.py` to every document.
7. Point `test_agents_md_is_canonical_brain_usage` at the core plus the §5 document.
8. Point each `.claude/rules/*.md` copy at its new home and add the copy guard from
   step 0 as a test.
9. Rewrite `CLAUDE.md` lines 5-15, which say Codex reads the full file.
10. **Add the size guard** `tests/test_agents_md_fits_codex.py`:
    - reads `AGENTS.md`;
    - limit = 32,768 − 0 − **4,096** margin = **28,672 bytes**;
    - measures the UTF-8 byte offset where each required span ENDS: the header box, each
      of the nine `## N ·` heading lines, and each trigger stub. The last span ends at the
      file's end, so the whole core must fit;
    - fails when any end offset exceeds 28,672.
    - **Known positive:** a planted core of 28,673 bytes must fail. **Known negative:**
      the real core after step 2 must pass.
    - **When growth reaches the margin:** move a whole paragraph or subsection into its
      section document. Never trim wording.

**Startup control:** the heading `## 2 · Note shape (frontmatter schema)` (the core keeps
§2), plus a planted token near the start of a scratch copy of the core.

**Proof.** In a scratch git repo holding copies of the core and the documents:
- *startup-only probe:* tokens planted at the start of the core and at its last line;
  the prompt forbids opening files; Codex must quote the §2 heading and list both
  tokens;
- *controlled probe:* tokens planted at the end of the §6 and §8 documents; Codex may
  open ONLY those two documents and must list both tokens. This probe cannot show that
  Codex opens them unprompted; step 2's trigger stubs are what cover that.

### Option B — raise `project_doc_max_bytes` in the tracked `.codex/config.toml`

#### Status from s01, stated exactly

s01 run 3 and run 4 (model runs) did **not** show the end-of-file token. s01 then showed
why: those runs passed trust with `-c`, and that path ignores the project config
entirely (F7). Local renders with trust written in a config file DID honour the raised
limit, in a nested worktree too (F8). **So option B is not disproved. It is supported by
renders only, and no model run has confirmed it yet.** Step 1 below is that model run.

#### B.1 What changes

One line in the tracked `.codex/config.toml`: `project_doc_max_bytes = 262144`. That
value is the one the s01 render proved (render B and D). Nothing moves. `AGENTS.md`
stays the one canonical home for every section; `AGENTS-core.md` and the
`.claude/rules` files stay copies.

#### B.2 Effect on the tests in s01-readers.md

None. No pinned file changes. The new guard and the copy guard are added.

#### B.3 Effect on the packaged copy and on clients who already received it

None. `.codex/config.toml` is not in `ENGINE_ASSET_FILES`, so nothing shipped changes.
Clients gain nothing, and lose nothing. The public export is exclude-list based
(`tools/export_cleanroom.py`), so the line reaches the public mirror. It helps a public
cloner only if they trust their checkout (F10).

#### B.4 Effect on each open branch that edits these files

None. No branch edits `.codex/config.toml`. The parked branch's +2,408 bytes fit well
inside the headroom.

#### B.5 What it costs a Codex session in bytes

Every Codex session in this repo carries the whole file: 127,734 bytes today, against
32,768 now. That is +94,966 bytes per session start. It grows with the file, about
1,363 bytes a day (F5). The token cost is **unverified**; s01 recorded input tokens only
for the whole prompt.

It works only where the checkout is trusted in a config FILE (F7, F8). This checkout and
its nested worktrees are (F9, s01 render F). A `codex exec -c 'projects…trust_level…'`
call, like s01 run 4, gets the old cut. So does an untrusted clone.

A third case gets the old cut too: `codex exec --ignore-user-config`, and any run with
`CODEX_HOME` pointed elsewhere. That flag skips `$CODEX_HOME/config.toml`, where the
trust entry lives. It is how the plan-execute Codex lane starts every session
(`~/.claude/skills/plan-execute/scripts/codex_command.py`, line 173). So every
Codex-backed plan session in this repo still loses sections 5 to 9, silently. Measured
2026-09-19 (s03 rework) by local render with `codex debug prompt-input` on codex-cli
0.153.4, no model call. An empty `CODEX_HOME` stands in for the flag.

Update 2026-09-19 (owner): the plan-execute lane (gearbox `755624fa`) and the
plan-harden Codex review (gearbox `4b3cc034`) now pass `-c project_doc_max_bytes=262144`
on their own command line, so this case no longer applies to them.

| Render | Prompt bytes | `## 4 · ` | `## 9 · ` |
|---|---|---|---|
| real `CODEX_HOME` | 165,046 | present | present |
| empty `CODEX_HOME` | 66,921 | present | **absent** |
| empty `CODEX_HOME` plus `-c project_doc_max_bytes=262144` | 163,418 | present | present |

The last row shows the fix for that lane: pass the budget with `-c`. That is a change to
the harness outside this repo, and it is a separate owner decision. This record does not
make it.

#### B.6 Steps

0. Reconcile the copies (as A step 0), and add the copy guard as a test.
1. **Confirm with one model run before changing anything.** Make a disposable
   `git worktree` of this repo under `.plan-worktrees/`, which the existing trust entry
   covers (F9). Plant a token at the start and a token on the last line of that
   worktree's `AGENTS.md`, and add the config line there only. Set `BRAIN_VAULT` to an
   empty scratch directory so the repo's `SessionStart` hook reads no vault memory.
   First render with `codex debug prompt-input` and read the render: it must hold no
   vault content. Then run the s01 prompt with `codex exec --json --sandbox read-only
   </dev/null`, **with no `-c` trust flag**. Check: both tokens listed and the §2
   heading quoted. Remove the worktree afterwards. If the end token is absent, stop:
   option B is disproved, and the owner picks again.
2. Add `project_doc_max_bytes = 262144` to `.codex/config.toml`, with a comment naming
   this ADR.
3. Find every caller that starts Codex in this repo with `-c` trust (the Codex lane,
   `codex:rescue`, any script). Each one either drops the `-c` trust flag or also passes
   `-c project_doc_max_bytes=262144` (render B proved the second form).
4. Rewrite `CLAUDE.md` lines 5-15 to say Codex reads the full file only through this
   setting, and only in a trusted checkout.
5. **Add the size guard** `tests/test_agents_md_fits_codex.py`:
    - reads `.codex/config.toml` with `tomllib` for `project_doc_max_bytes` (missing
      means the default 32,768), and reads `AGENTS.md`;
    - limit = budget − 0 − **32,768** margin = **229,376 bytes** at 262,144;
    - measures the UTF-8 byte offset where the one required span ENDS: the end of §9,
      which is the end of the file;
    - fails when that offset exceeds the limit.
    - **Known positive:** a planted `AGENTS.md` of 229,377 bytes must fail; so must the
      real file with the config line removed. **Known negative:** the real file
      (127,734 bytes) with the line present must pass.
    - **Headroom rule.** The margin is 32,768 bytes, about 24 days of growth at the
      measured rate. Room today is 101,642 bytes, about 75 days. When the guard fails,
      the author does not raise the number alone. They bring the owner a decision card:
      raise the budget by 65,536 (and state the new per-session cost), or move text out
      under option C.

**Startup control:** the heading `## 2 · Note shape (frontmatter schema)`, plus a
planted token near the start of the file.

**Proof:** the startup-only probe of step 1, whose required token sits on the last line
of the file, past today's cut. No controlled probe is needed: nothing is on demand.

### Option C — a Codex-only `AGENTS.override.md` that reuses `AGENTS-core.md`

#### C.1 What changes

A repo-root `AGENTS.override.md` holds the Codex core. Codex then reads that file and
**nothing from `AGENTS.md`** (F11). The core is `AGENTS-core.md` itself, as a symlink
(the render showed Codex follows one, F11) or as a generated byte-identical copy that a
test guards. No new rule text is written. `AGENTS.md` and the packaged copy do not
change.

`AGENTS-core.md` needs two navigation edits, not rule edits. Its pointer table heading
"Loads when touching" becomes an instruction any assistant can follow: "Before you
change these files, read". And the duplicated `## 8` heading line (F16) is removed.

Canonical homes: every section stays in `AGENTS.md`. `AGENTS-core.md`, the override and
the `.claude/rules` files are copies.

**What Codex loses at startup, compared with today.** Today it sees the header, §1, §2,
§3 and part of §4. Under C it sees the header, the short §1, the §5 verb table, the §8
test commands and the pointer table. So §2, §3 and the visible part of §4 leave its
startup text. They stay reachable on demand through `note-shape.md` and
`capture-and-invariants.md`.

**How Codex reaches §5, §6 and §8.** Through the named documents the pointer table
lists: `.claude/rules/retrieval-and-security.md` (§5 sub-sections),
`.claude/rules/host-vm-trust.md` (§6) and `.claude/rules/pre-commit.md` (§8). Whether
Codex's tool output truncates a 55,061-byte file read is **unverified**.

#### C.2 Effect on the tests in s01-readers.md

None. `AGENTS.md` does not change, and `CLAUDE.md` keeps `@AGENTS-core.md`. The override
guard and the copy guard are added.

#### C.3 Effect on the packaged copy and on clients who already received it

None. The override is not in `ENGINE_ASSET_FILES`. Clients gain nothing and lose
nothing. The override reaches the public export (exclude-list based). Whether the export
copies a symlink as a link or as text is **unverified**; step 3 checks it.

#### C.4 Effect on each open branch that edits these files

`plan/commitments-identity-2026-08-26` adds one line to the `AGENTS-core.md` pointer
table (hunk at line 166). The table-heading edit in step 1 touches the same table, so
expect a small hand-resolved conflict. `worktree-improve-claude-md-plan-state` is
unaffected.

#### C.5 What it costs a Codex session in bytes

10,491 bytes at startup, 22,277 fewer than today. On-demand reads add their size when
triggered: `retrieval-and-security.md` 55,061, `host-vm-trust.md` 11,045,
`pre-commit.md` 5,588, `capture-and-invariants.md` 23,733, `note-shape.md` 8,746.

#### C.6 Steps

0. Reconcile the copies (as A step 0), and add the copy guard as a test. This matters
   most here, because Codex would read the copies, not `AGENTS.md`.
1. Edit `AGENTS-core.md`: make the pointer table read as instructions, one row per
   document with the task that requires it, and drop the duplicated `## 8` line. Check:
   every file the table names exists (a test).
2. Create `AGENTS.override.md` as a symlink to `AGENTS-core.md`. Check: `readlink`
   prints `AGENTS-core.md`.
3. Run `tools/export_cleanroom.py` into a scratch directory. If the symlink arrives as
   anything but a link or a byte-identical file, replace it with a generated copy, and
   make the guard assert byte equality instead.
4. Rewrite `CLAUDE.md` lines 5-15: Codex reads `AGENTS.override.md`, not `AGENTS.md`.
5. **Add the size guard** `tests/test_agents_md_fits_codex.py`:
    - reads `AGENTS.override.md` (resolved through the link) and `AGENTS-core.md`;
    - limit = 32,768 − 0 − **4,096** margin = **28,672 bytes**;
    - measures the UTF-8 byte offset where each required span ENDS: the header box,
      each `## N ·` heading, and the last row of the pointer table. The last row sits
      near the file's end, so the whole core must fit;
    - fails when any end offset exceeds 28,672, or when the override is not
      `AGENTS-core.md` (not the link target, or not byte-identical).
    - **Known positive:** a planted override of 28,673 bytes must fail; so must an
      override that differs from `AGENTS-core.md` by one byte. **Known negative:** the
      real override after step 2 must pass.
    - **When growth reaches the margin:** move a paragraph out of `AGENTS-core.md`
      into its rules copy. Never trim wording.

**Startup control:** the heading `## 1 · The substrate in one screen` (the override
carries no §2), plus a planted token near the start of a scratch copy.

**Proof.** In a scratch git repo holding copies of the override and the rules files
(an untrusted directory loads the override, F10, F11):
- *startup-only probe:* tokens at the start and on the last line of the override; the
  prompt forbids opening files; Codex must quote the §1 heading, list both tokens, and
  must NOT list a token planted in the scratch `AGENTS.md`;
- *controlled probe:* tokens at the end of the §6 text (`host-vm-trust.md`) and the §8
  text (`pre-commit.md`); Codex may open ONLY those two files and must list both
  tokens. As in A, this cannot show that Codex opens them unprompted.

---

## 4 · Recommendation

**Recommended: option B, gated on its step 1 model run.**

- It is the only option in which Codex sees every rule, as every other harness does.
  That is what `CLAUDE.md` already promises.
- It moves no text, changes no shipped file and touches no open branch.
- Its limits are known and bounded: trusted checkouts only, and +94,966 bytes per
  session. The guard turns growth into an owner decision instead of a silent cut.
- A costs the most and gives clients nothing. It rewires four shipping paths, and one
  mistake strips the trust rules from every Cowork session.
- C is the fallback if step 1 disproves B. Its weakness is that Codex must choose to open
  §6 and §8 itself, and no probe can prove it does.

**If nothing is done:** Codex in this repo keeps working without §5 to §9, including the
host/VM trust split and the before-you-commit rules. It gets no warning. As text is
added before byte 32,768, more of §4 falls past the cut too. `CLAUDE.md` line 6 stays
false.

---

Decision: B — raise project_doc_max_bytes in the tracked .codex/config.toml, gated on its step 1 model run (owner, 2026-09-19).
