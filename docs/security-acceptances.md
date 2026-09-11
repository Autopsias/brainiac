# Accepted security findings

Findings a reviewer will raise again, with the ruling that says why they stay.

This file exists because an external scan cannot see a decision. Three of the
fourteen findings in the 2026-08-25 Codex round were already settled — one of
them on the same day the commit that "introduced" it landed — and without a
written record each future scan re-raises them, each triage costs the same
reading, and a real finding sits in the same list as a resolved one.

**What belongs here:** a risk the owner chose to carry, with the reasoning and
whatever bounds it. **What does not:** anything still being argued, and anything
where "accepted" means "not looked at yet". An entry with no ruling behind it is
a defect being laundered into a decision.

Each entry names its mitigation separately from its acceptance, because the two
decay differently: a mitigation can silently stop working while the ruling still
reads as current.

---

## Closed 2026-09

Findings closed by the `security-followup-2026-09-01` plan, one line each so
the next assessment starts from what actually shipped rather than the plan's
intent. s01 recorded the owner's per-finding rulings in
`_evidence/security-followup/approvals.json`.

This index was written by s11. The plan's remaining sessions (s05c, s05d
and the s12 acceptance review) had not run when it was written, so a line
here reflects the state at s11 — s12 is the session that reconciles it.

- **M-2** (s06) — the outbound term guard (SEC-07) now watches every tool that
  can send text out (`WebSearch|WebFetch|Bash|PowerShell|Write|Edit|
  NotebookEdit|Read|Grep|Glob|mcp__.*`), fails CLOSED on a `check-egress`
  error, and the repo-local `Bash(python3 *)` allow that let a shell command
  bypass it entirely is removed.
- **M-3** (s05, s05b) — the concealment scan at the handler boundary detects
  white-on-white text, sub-2px text, off-page text and non-English hidden
  instructions; hardened over six adversarial rounds to 0/538 over-fire on the
  reference corpus, with a per-document `full`/`unknown`/`incomplete` coverage
  attestation derived from what the walker actually read.
- **M-4** (s07) — the Cowork sandbox bootstrap refuses to run without a
  matching `SHA256SUMS` file; the plugin marketplace ref is pinned to a tag
  (`v0.20.35`) instead of a moving branch.
- **M-5** (s11, this session) — the vendor posture register said all three AI
  vendors were unverified and should run only against a projected workspace;
  practice (A-01) is the full vault to all three. Retired per the owner's
  ruling (Option A) rather than rewritten — see the note at A-01 and
  `docs/operations/egress-provider-posture.md`.
- **M-6** — the owner refused replacing the weekly synthesis model's free-text
  session-memory writes with a fixed-shape summary line (loss L5). **Not
  closed** — recorded as an accepted risk at **A-08**.
- **M-7** (s03) — the audit chain hashes raw bytes, not text-mode-translated
  ones, and its own log reader splits strictly on `b"\n"`, so a CR-only edit
  or an embedded `0x0b` can no longer hide from `verify-audit`;
  `verify-audit --allow-empty` covers a vault with no signed note yet.
- **M-8** (s04) — frontmatter gets a line-anchored split, and a Word or
  PowerPoint file whose UNCOMPRESSED size exceeds 256 MB is quarantined. The
  third leg — rewriting a sandbox `type: decision` draft to `type: note` — was
  refused (loss L7). **Partially closed** — the size bound shipped; the
  rewrite is recorded as an accepted risk at **A-07**.
- **M-9** (s02) — the Cowork VM's trust anchor (`pinned-verify.json`) now
  stages inside the workspace runtime dir, beside the signed summary it
  verifies, instead of inside the vault's own runtime folder.
- **Low-01** (s09) — a live argv leak in `audit.py::provision_signing_key`
  fixed (moved to stdin); `$BRAINIAC_ENGINE_SRC` is now uid-checked; the `uv`
  bootstrap in `install.sh`/`install.ps1` is checksum-verified. A plaintext
  backup's missing-manifest restore was refused (loss L8). **Partially
  closed** — recorded as an accepted risk at **A-10**.
- **Low-02** (s10) — `BRAIN_READ_LOG=0` now prints a notice instead of
  silently disabling the broker's own read record; the broker's
  `alerts`/`exceptions` verbs strip absolute vault paths; the full exceptions
  page applies `$BRAIN_DEFAULT_MAX_TIER`; `<title>` strips control characters;
  an ingest stem that slugs to empty gets a hash suffix. (Also fixed along the
  way, not part of the plan's original scope: the OOXML member-count cap did
  not bound memory until the count was checked before `zipfile.ZipFile()`
  construction rather than after.)
- **Low-03** (s10b) — the OCR page budget now counts images, not pages, so a
  text-only page spends nothing against it. Declassification alerting and the
  signed ceiling's staleness clock were confirmed already shipped, not
  rebuilt.
- **Low-04** (s09b) — every `actions/checkout` step across 9 GitHub Actions
  workflows (11 steps) now sets `persist-credentials: false`.

---

## A-01 · MCP defaults to the full vault for host clients

**Raised as:** "MCP default now exposes full vault to LLM clients" (high),
against `9f9284b`, 2026-08-17.

**Ruling:** the owner's, 2026-08-17 — the same day. The host MCP server reads
the full vault. `HOST_MCP_DEFAULT_MAX_TIER` is deliberate, not a regression.

**The reasoning:** the classification gate exists to bound what leaves the host
toward an *untrusted* leg. A host MCP client is the owner's own desktop
application reading the owner's own vault on the owner's own machine; capping it
at Internal by default made the owner's tools worse at their job without moving
anything across a trust boundary.

**Corrected 2026-09-01.** This paragraph used to end "`--role vm` still
defaults to Internal, which is where the boundary actually is". That sentence
was FALSE on the live path and it is worth keeping the correction visible: the
Cowork leg does not reach the vault through `--role vm` at all. It arrives
through a Claude Desktop MCP stanza that carries no role, so it reads at the
HOST ceiling — the full vault — exactly like the owner's own Desktop app.
`connect.py` has never written a `BRAIN_ROLE`, so there was never a moment when
that sentence was true of Cowork.

**And the owner ruled 2026-09-01 that this is intended, not a gap.** Reading a
high-tier note is what Brainiac is FOR: "the purpose of brainiac is to work and
access all kind of information the user has access to". The trust boundary is
not what an agent may READ. It is what can LEAVE without the owner deciding.
That is why the residual is tracked as a prompt-injection question (A-06) and
not as a read-ceiling one.

**What still bounds it:** `--max-tier` and `$BRAIN_DEFAULT_MAX_TIER` narrow any
individual client, and the VM leg's own default is unchanged.

**What would reopen it:** the host MCP server becoming reachable by anything the
owner does not control — a remote transport, a shared machine, a client that
relays to a third party. Note that a sandboxed agent reading through the broker
is NOT that case by itself; it becomes that case if the sandbox gains an
outbound channel the owner does not review (A-06).

**This ruling superseded the vendor posture register (M-5, 2026-09-04).**
`docs/harness-allowlist.json` and `docs/operations/egress-provider-posture.md`
§4 said all three AI harnesses were unverified PENDING and should run only
against a projected workspace — a document that never agreed with this
ruling. Loss L4 (`approvals.json`, decided 2026-09-02, Option A) retired the
register rather than rewriting it to match: `egress.is_allowed()` (the only
code that read it as a gate) and its test are removed, and the JSON file is
marked `retired: true` pointing back here.

---

## A-02 · The COS model legs keep a filesystem read primitive

**Raised as:** "COS model leg can read arbitrary host files" (critical), against
`4c9354b`, 2026-08-17.

**Ruling:** the owner's — recorded in `tools/cos_nightly.sh` at the
`MODEL_TOOLS` gate, under "THE THIRD CHANNEL IS STILL OPEN, DELIBERATELY". The
`Read,Glob` grant stays, so the rest of the grounding design's D12/D12a
(`--tools ""`, the category leg's prompt on stdin, a scratch cwd outside the
repo) is not shipped.

**The reasoning is measured, not assumed.** The finding is correct on its
mechanics and the design's own probe proved it first: with this exact grant, a
leg whose working directory was an empty temp workspace read an absolute path
outside it and printed the token. **Working directory scopes nothing.** The
comment in that file used to claim the leg could not reach "one byte of this
disk"; that clause was false, and it now says so.

**What still bounds it:** writing is closed, not merely discouraged — a blanket
`Edit(//**)` deny drops every file-editing tool, tested against a known positive
that provably writes. Two of the three context channels are closed on the legs
themselves: `--setting-sources ""` stops the SessionStart hooks injecting vault
session memory into every leg, and `--no-session-persistence` stops each leg's
stdin being persisted as a transcript outside every retention clock. Grounding
runs in front of the open read primitive, and `cos_judge.py --judge` validates
every verdict against a closed vocabulary, so injection can bend a verdict but
not smuggle one past the gate.

**What would reopen it:** the legs being fed content from a wider surface than
the owner's own mailbox, or the judge's closed vocabulary being relaxed.

---

## A-03 · The nightly updates itself without a human present

**Raised as:** "Unattended nightly update enables supply-chain RCE" (high),
against `8ff852d`, 2026-07-27.

**Status: mitigated 2026-08-25, and the acceptance narrowed to what the
mitigation does not cover.** It stood as "accepted, and NOT mitigated" until the
owner ruled on it that day.

**The reasoning:** auto-update is the feature. A second brain that silently runs
a version from weeks ago is the failure this was built to prevent, and requiring
a human at 02:00 means no update ever happens. The finding is right that a
compromised index, account, or plugin marketplace executes code as the local
user, with the vault, the index and the signing key environment in reach — and
right that the post-update doctor check cannot help, because install hooks have
already run by then.

**The owner's ruling, 2026-08-25:** require an attestation match before an
UNATTENDED upgrade, and leave the attended one alone.

**What bounds it now.** PyPI accepts a PEP 740 attestation only from a Trusted
Publisher, so "did this build come from the same repository and workflow as
every other release" is a question the index can answer and an account takeover
cannot fake. `brain.attestation.verify_publisher` asks it before
`_maybe_auto_update` installs anything; a version that carries no attestation,
carries one naming a different publisher, or cannot be checked at all is held
(`auto_update: "held"`, notify key `update:attestation-held`) and the owner is
told to run `brain update` himself. Every uncertainty fails CLOSED, including an
unreachable index — the cost of that is one skipped upgrade.

**The attended path is deliberately NOT gated.** `brain update` calls
`brain_update.run_update` directly and never touches `_maybe_auto_update`, so
when the owner directs it the whole chain runs end to end. His direction is the
authority; the risk this guards is unattended execution, not upgrading. The
split is asserted, not assumed —
`tests/test_attestation_gate.py::test_the_attended_path_is_not_gated`.

**What is still accepted.** The gate proves the ORIGIN of the build, never its
CONTENT: a compromised repository or workflow publishes an attested malicious
wheel and passes. It also cannot help until a release actually carries an
attestation — every version up to and including 0.20.29 predates publishing from
CI, so the first attested release is the first one this can admit, and until then
every unattended upgrade is held. `requirements.lock` is hash-pinned and audited
weekly by `supply-chain.yml`, but the update path installs the published wheel,
not the lock.

**Still worth revisiting, in order of cost:** pinning the update channel to a
known index; holding unattended updates to a version already seen for N days.
Neither is implemented.

---

## A-04 · A deliverable's tier marker is trusted from the drop tree

**Raised as:** "Untrusted deliverable tier marker can downgrade ingestion"
(medium), against `0714942`, 2026-08-25.

**Status: mitigated 2026-08-26 with a DETECTION control, and the acceptance is
the prevention that is deliberately not built.**

**The finding is correct and was reproduced before anything was decided.** With
no control file the lane admits a drop at `MNPI`; one line of `Internal` in
`inbox/_deliverables/.classification` takes it to `Internal`, and it stays there
for every future drop into that folder. A malformed value already fails closed
(EXC-01) — only a well-formed LOWER value is trusted. That tree is the same one
the payload arrives in, so anything able to write the vault can plant it.

**Why it is worse than its severity suggests.** The result is that the HOST
signs a note at the lower tier. That is the host-only signing boundary, the same
one that made the sign-drain finding a real gap rather than an entry in this
file.

**Why raise-only is not the fix.** `MNPI` is the top tier, so the control file
exists ONLY to declassify. A rule admitting only higher values does not repair
the feature, it removes it — every deliverable would be `MNPI` forever, and the
lane's whole purpose is letting an owner say "this project's outputs are fine at
Internal".

**The owner's ruling, 2026-08-26:** honour the tier, and report every
declassification. Visibility rather than prevention, which is the same posture
already chosen for the larger sandbox question (VULN-3385): where a control
cannot prevent, it must at least make the event impossible to miss.

**Re-checked 2026-08-30, Closed Stacks close-out.** This citation named
VULN-3385 as the precedent for "visibility over prevention", on the
assumption that Closed Stacks might CLOSE 3385 by making prevention
available there — which would have weakened this entry's precedent. It did
not: s01's checkpoint measured that no per-caller ceiling exists for the
Cowork leg (`docs/operations/closed-stacks-s01-foundation-evidence.md` §4),
so VULN-3385 stayed OPEN as a mitigation, in the exact same
detection-not-prevention posture this entry cites (see A-05 below and
`docs/security/vuln-3385-risk-reduction.md`). **The citation is REAFFIRMED on its
own merits, not weakened** — no change needed to this entry.

**What bounds it now.** Every drop admitted below the lane default is recorded
on the ingest report and shaped into an `action_required` finding carrying a
`notify_key`, so it reaches `brain alerts` at session start rather than dying in
a launchd log. The operator recognises a declassification they made, and sees
one they did not. The finding reports COUNTS AND TIERS ONLY — never the project
folder, never the filename — because that text is persisted verbatim into
`.brain/notify-sent/current.json`, which a Cowork VM session can read, and a
`_deliverables/<project>/` folder name is exactly where a client name sits. That
is the escalation the quarantine banner carried until 2026-08-25.

**What is still accepted: the plant itself is not prevented.** A control file
written by something other than the owner is honoured, and the note is signed at
its tier. Detection is same-run, not pre-write, so the window is one ingest.

**The prevention that is available, and was deliberately deferred.** Require the
control file to be host-signed, reusing `brain.vm_ceiling`'s machinery — the
pinned-anchor verify already built for VULN-3386, and the public key
`brain audit-pubkey` already exports. It is roughly fifty lines plus a verb. It
was not taken now because it is a BREAKING change for every existing
deployment: unsigned markers stop working and those folders return to `MNPI`
until re-staged. Worth taking if a reviewer declines the detection control.

**What would reopen it:** the drop tree becoming writable by anything further
from the owner than a local sandbox, or a declassification arriving that the
alerts channel did not surface.

---

## A-05 · VULN-3385 — the Cowork mount read/write bypass

**Raised as:** a penetration test asked for a simple search; the Cowork agent
skipped the `brain` CLI and read a Restricted-tier note straight off disk. The
sandbox had the vault folder attached read-write over VirtioFS, so any ordinary
file tool bypassed the classification gate entirely, left no record, and could
write into `vault/` with no draft-approval path.

**Status: CLOSED 2026-09-01 by owner ruling. The bypass is fixed; what was
being tracked as its residual was never a defect.**

It read MITIGATED-AND-OPEN from 2026-08-30 until 2026-09-01. That status came
from s01's checkpoint (approved 2026-08-27T22:55:03), whose acceptance criteria
made closure conditional on a per-caller tier ceiling for the Cowork leg. No
such ceiling exists, so the finding stayed open against that test.

**The owner ruled 2026-09-01 that the test was the wrong one.** Retrieving a
high-tier note is functionality, not a vulnerability — the ruling already
recorded at A-01, applied here. The incident was that a Cowork agent read a
note *off disk*: unfiltered, unrecorded, with write access, and with no way to
see it had happened. Every part of that is fixed and re-measured (below). What
remained was an agent obtaining a note through the sanctioned, filtered, logged
broker — which is the product working.

**The one real residual moved, it did not disappear.** If an agent may read
anything the owner may read, the question that matters is whether it can send
anything out without the owner deciding. That is a prompt-injection question,
and it is now tracked on its own terms at **A-06**, where the controls built
for it (SEC-07, the two unattended injection alarms, the self-filling decoder
ring) actually belong. Keeping A-05 open was using a closed bypass as a
stand-in for a live injection question, which made both harder to reason about.

**What would reopen A-05 specifically:** a vault folder attached to a Cowork
workspace again, or any path that reads a note without passing the broker's
classification filter and read record. `tests/test_direct_file_read_relocated.py`
and `tests/test_cowork_staging_off_the_mount.py` fail if either returns.

**What is fixed, re-measured at register-writing time (2026-08-30):**
- The vault is off the Cowork mount. The registered workspace is data-free —
  nothing moved, the old attachment was simply detached and a new, vault-less
  one registered in its place (owner Ruling 1, 2026-08-29). No note body is
  recoverable from the new workspace by path, glob, or content search
  (`tests/test_direct_file_read_relocated.py` + `tests/test_cowork_staging_off_the_mount.py`,
  55/55 passed).
- Every vault-note read verb now reaches the vault only through the host
  `brain-mcp` broker, which applies the same classification filter as the CLI
  (`tests/test_mcp_broker_seam.py` + `tests/test_mcp_tool_surface.py`, 83/83
  passed).
- The broker's audit-record write is fail-CLOSED: a read whose SEC-06 record
  cannot be written now raises instead of returning content silently. Before
  this plan the same path read `except Exception: pass  # never let logging
  fail a read`; that line is gone from the broker leg.
- The 14 shipped Cowork skill bundles were rewritten to call the broker's
  tools instead of shelling a CLI verb against a mounted vault (s06b,
  `tests/test_cowork_skill_bundles.py` + `tests/test_cowork_skill_verbs.py`,
  50/50 passed), released to the skill marketplace as version 0.20.32.
- **Both live Cowork workspaces on this host are now cut over** (added
  2026-08-30, after the plan's acceptance review). The plan cut over one
  workspace; the host registry named three. The acceptance review found a
  SECOND live registration whose vault still sat inside the attached folder,
  and the mount-leak scan reported 5 recoverable artefacts there. It was cut
  over the same way, its workspace registered, and re-measured: both live
  workspaces now report `current`, `cut_over=True`, 0 artefacts, so the
  keep-it-off gate is armed on both. Two STALE `cowork-vm` registry rows over
  the old folders were deliberately left in place — they were the only signal
  that would fire if those folders were still attached, and only the owner can
  detach them in the Cowork application. **Superseded the same day, and this
  sentence outlived it:** the owner attached both new workspace folders in the
  Cowork app and DETACHED the old ones, so the four rows naming them (2
  `cowork-vm`, 2 `host`) were removed. Re-read 2026-09-01,
  `~/.brainiac/workspaces.json` holds four entries and none names an old
  folder; the signal is absent because the condition it watched for is gone,
  not because it was lost. Record:
  `_plans/closed-stacks-2026-08-27/_evidence/s09/second-workspace-cutover.txt`
  (its own UPDATE paragraph carries the detach).

**What remains true after the closure, and where each part is now tracked.**
None of this is a defect of A-05; it is the design the closure rests on, kept
here so a reader is not surprised by it.
- **No per-caller tier ceiling exists.** The broker's egress ceiling
  (`mcp_verbs._egress_ceiling_tier`) reads only the process-wide
  `$BRAIN_MAX_EGRESS_TIER` env var — it cannot tell a Cowork caller from a
  host one. Re-measured live (2026-08-30, after review): all FOUR `brain-mcp` entries in this machine's Claude Desktop config carried `BRAIN_MAX_EGRESS_TIER=MNPI` (full vault) and none set `BRAIN_ROLE`. **Re-measured 2026-09-01, and it had drifted:** ONE stanza read `Internal` while a SECOND stanza over the very same vault read `MNPI` — one vault answering two ways depending on which entry asked, which is the "answers from scraps" failure `connect.py`'s own comment names. The owner re-ran `brain connect --max-tier MNPI` the same day and all four read `MNPI` again. **No entry has ever set `BRAIN_ROLE`, so the finding itself never moved** — only the count did, twice, which is why it is re-read rather than quoted. (Stanza names are deliberately not written here: they carry a client identifier, and this file ships).
  The first probe reported 2 and inspected 2 — two client-named entries
  were never looked at. They carry the same values, so the conclusion held,
  but a count is a measurement and this one was short.
  `connect.py`'s Desktop-config builder never writes `BRAIN_ROLE` at all — a
  finding s06b raised for a later session, and no session in this plan
  closed it. **A Cowork session can still retrieve the same Restricted-tier
  note the penetration test read** — through the sanctioned, filtered,
  logged tool now, rather than an invisible bypass, which is the entire
  difference a mitigation buys. **Re-measured 2026-09-01 after 0.20.35:
  unchanged** — `connect.py` still writes only `BRAIN_MAX_EGRESS_TIER` and
  never a role, and all four live stanzas read `MNPI` with no role set.
- **The outbound channel is now guarded on the HOST leg only (SEC-07,
  0.20.35).** `brain check-egress` refuses a tool-call argument carrying a
  term the vault classifies `Confidential` or above, wired as a Claude Code
  `PreToolUse` hook, with a ring the nightly fold derives from the vault's own
  `project` notes so it is not left empty. The two indirect-injection
  detectors (SEC-05 concealed instructions, SEC-06 bulk reads) also moved out
  of the CLI body into the unattended Tuesday fold. **This does not narrow
  A-05 and does not move its status.** It closes an exfiltration channel the
  acceptance never covered, and it is ENFORCED only where Claude Code hooks
  run — the host. On the Cowork leg the rule ships as prose in the staged
  `AGENTS.md` and nothing fires it automatically, so the leg this entry is
  about holds an instruction, not a mechanism.
- **Two broker-side leaks the S01 probes measured as live — CLOSED
  2026-08-30, same day, owner option A.** The owner kept the full-vault
  ceiling (the ruling above stands) and had both leaks closed in code rather
  than accepted. (a) The EXISTENCE ORACLE: `get`/`read` now return the
  absent-shaped `egress` report whenever nothing surfaced, so a clamped caller
  reads the same answer for a withheld id and a missing one; the host read
  record still counts the withheld note. Wiring a per-caller ceiling later no
  longer arms a metadata leak. (b) `vault_languages` now gates the note list
  under its `max_tier`, leaves a SEC-06 row, and counts only admitted notes —
  it left `BODYLESS_TOOLS`, so the "a record of every read" guarantee holds
  without a qualifier. `tests/test_closed_stacks_s01_egress_probes.py` asserts
  the FIX now, with a known negative beside each. Still accepted, recorded not
  changed: the same counter on `bases_query`'s predicate and the ranked verbs
  (survey `recorded_not_changed[0]`).
- ~~**The CLI's own record-write path is still fail-open.**~~ **CLOSED
  2026-08-31**, after this register entry was written. `cli.py` kept the same
  `except Exception: pass` the broker leg dropped, which was moot for a
  genuinely new Cowork workspace (no local vault data left to read — a CLI verb
  against it exits 3, `tests/test_desk_fails_closed.py`, 13/13 passed) but true
  for the host's own shell, the leg `CLAUDE.md` points every session at.
  `brain.cli_read_record` now holds a gated verb's output until its SEC-06
  record is written and exits `5` with the result withheld when it cannot be;
  a verb that gates nothing streams unchanged.
  `tests/test_cli_read_record_fails_closed.py` (7 tests) probes both
  directions. Left as a struck-through line rather than deleted: this register
  is the record of what was accepted and when, and a silently vanished bullet
  reads as one that was never raised.
- **No per-session original-document hand-off exists.** The design that would
  have staged one archived file per session behind a lease was retired by
  owner ruling (2026-08-27): Claude Desktop forwards no per-session identity
  the broker can trust, so there is no way to bind a staging directory to one
  caller. `brain authorize-original` is host-only and records a disclosure
  decision only, never bytes; actual delivery is a host operator running
  `brain project --dest <dir> --max-tier <tier>` by hand.

**What would reopen it further, or close it:** a per-caller tier ceiling
actually wired to the broker's registration (the `BRAIN_ROLE` gap above is
the first blocker), or a real per-session hand-off mechanism, which needs
Claude Desktop to forward an identity the broker can trust — neither of which
exists today. This list also named "the CLI leg's own audit write made
fail-closed" until 2026-08-31; that one is now done, and the finding still
does not close, which is the honest measure of how much it was worth.

**Owner ruling 2026-08-31, on the first item.** The per-caller ceiling was put
to the owner and DECLINED: the full-vault default stands, as ruled on
2026-08-10 and again on 2026-08-17. The reasoning is unchanged — the broker
cannot distinguish a Cowork caller from a host one, so clamping the sandbox
clamps the owner's own Desktop sessions with it, and the part that mattered
(an invisible bypass became a filtered, logged door) is already done. Do not
re-raise this without new information.

**And 2026-09-01, on what that decline meant.** Until this date the record
read the decline as leaving "no path to a closure", making A-05 the terminal
state of VULN-3385. The owner ruled the opposite: a per-caller ceiling was
never the right closure test, because reading a high-tier note is the product
working (A-01). The bypass is what was reported and the bypass is fixed. The
live question is what can LEAVE without the owner deciding, and that is A-06.

Full record for a reader who did not follow the work:
`docs/security/vuln-3385-risk-reduction.md`.

---

## A-06 · Indirect prompt injection on the Cowork leg

**Raised as:** the residual left when A-05 closed. If a sandboxed agent may
read anything the owner may read (A-01), the question that matters is no longer
what it can READ. It is whether text inside an ingested document can make it
send something out without the owner deciding.

**Ruling:** the owner's, 2026-09-01. Accepted at LOW severity, on the reasoning
below, with the controls listed and their limits stated.

**Why it is low — the outbound paths are narrow, and the main one is
owner-gated.**
- The vault is off the Cowork mount, so an injected instruction cannot read a
  note off disk (A-05).
- The normal way anything leaves is a document the agent drafts and the OWNER
  reads and sends. The owner's own framing, and it is the right one: "it's no
  different from email in that part". A human is at the gate.
- The Cowork VM runs behind an **egress allowlist**, not open internet. Our own
  install lane depends on this: HuggingFace is not on it, which is why the ONNX
  model is bundled into the workspace rather than downloaded
  (`docs/cowork-windows-install.md`, rule 2). A sandboxed process cannot post a
  document to an arbitrary host.

**What is built against it.**
- **SEC-07 (0.20.35)** — `brain check-egress` refuses a tool-call argument
  carrying a term the vault classifies `Confidential` or above, wired as a
  Claude Code `PreToolUse` hook. It exists because a tool call's ARGUMENTS are
  not a document and nobody reviews them: a query string leaves before any
  result comes back.
- **The decoder ring fills itself (2026-09-01)** — a nightly fold derives the
  guard's terms from the vault's own `project` notes classified `Confidential`
  or above, so the guard is not inert on a vault nobody curates.
- **Two detectors now run unattended** — the SEC-05 concealed-instruction
  corpus re-scan and the SEC-06 bulk-read alarm moved out of the `brain
  integrity` CLI body into the Tuesday fold, each with its own `notify_key`.
- Ingest marks third-party text `content_trust: untrusted-source`, and the
  SEC-06 read record is fail-closed on both the broker and the CLI.
- **The inbound mirror, closed 2026-09-01.** This entry reasons about what
  can LEAVE. The 2026-09-01 assessment found the opposite direction open: the
  broker's `capture` tool ran `core.capture()` at host role for every client,
  Cowork included, and that branch called `write_note` directly — a sandbox
  capture landed signed and indexed with no untrusted-author checks, and
  `write_note` has no existence check, so it could REPLACE an existing note,
  `raw/` included. Both legs now commit through the one draft lane
  (`draft_drain`): the host stages and drains in the same call, so a capture
  is still retrievable at once, but an existing id is refused
  (`duplicate-id`), a forged `provenance.verified` is stripped, and the id
  cannot escape its subtree. The CLI read leg was brought to broker parity
  the same day: `grep`/`graph-expand` drop above-ceiling notes before
  matching, and `get` answers a withheld id exactly as an absent one
  (owner-approved; until then it exited 2 and printed the tier).

**What is accepted, stated plainly — five limits, none of them guesses.**
1. **SEC-07 does not run inside Cowork.** It is enforced where Claude Code
   hooks run, which is the host. On the Cowork leg `check-egress` is
   `VM_ALLOWED` but nothing fires it, and the rule ships as prose in the staged
   `AGENTS.md`. That is an instruction to the model, not a control — the same
   soft guarantee `brain --role vm alerts` already depends on.
2. **The guard matches declared terms on word boundaries.** It cannot see a
   paraphrase, an undeclared codename, or a fact the model restates in its own
   words. It is a floor, not a seal.
3. **On the host, the union follows what a call NAMES on disk, and the primary
   read channel names nothing** (s06 rounds 7-8, 2026-09-03). Each call resolves
   its vault from the host registry, matching the working directory, any
   absolute path in a structured field, and every absolute-looking token in a
   `Bash` command — so `--vault "/alpha/vault"`, `cat /alpha/vault/note.md` and
   `cd /alpha/vault` all count as entering, and every vault a session enters is
   kept in a per-session union, so leaving one does not release its terms. But a
   session also reads through the desk (the host-run `brain-mcp` broker), and an
   MCP read carries no directory and no path — so content from a vault the
   session never entered on disk is held with that vault absent from the union,
   and its terms do not refuse. A path assembled from a shell variable is
   outside it for the same reason. Narrowing this needs the broker to report
   which vault it served, and it is the same shape as limit 1: the guard is a
   floor where the hook fires, never a perimeter.
4. **The guard's control files live in the account it guards** (s06 round 8,
   both reviewers, 2026-09-03). The `.engine` pin names the executable the guard
   trusts, `~/.brainiac/workspaces.json` decides which vault answers, and
   `egress-sessions/<id>` holds every vault this session entered. All three are
   ordinary owner-writable files, and a local `Bash` command sends nothing off
   the host, so writing them used to pass unexamined: one `printf /bin/true >`
   into the pin is a permanent kill switch that `brain doctor` reports healthy,
   and one `rm` erases the union with no way to tell an erased session from a
   quiet one. The guard now REFUSES a call that names any of those directories
   as this call spells them, raw or resolved. That is a floor and nothing more:
   a path built from a variable, or a helper script that writes it, still gets
   through. The real boundary is structural — a `PreToolUse` hook runs as the
   account it guards, so an agent with arbitrary local shell can reach anything
   that account can. Closing the class needs control-plane storage the model
   process cannot write, which Claude Code does not offer today.
5. **The union is keyed on the payload's `session_id`, and the subagent
   boundary is untested** (s06 round 8, Claude, confidence 0.55). Containment
   holds while every call in one conversation presents the same id. Nothing
   measures what a dispatched agent or a fork presents. If a subagent's calls
   carry a distinct id, a subagent that reads alpha and reports back leaves the
   parent free to send alpha's terms — round 6's hole one boundary over. This is
   recorded rather than fixed because measuring it needs a current guard
   installed on a host running subagents, which s06 did not have.
6. **The egress allowlist is Anthropic's and cannot be verified from this
   repository.** We know an allowlist exists because our own install lane works
   around it. We do not know what is on it, we cannot see which tools a Cowork
   session is given, and either can change without notice. The honest claim is
   "the sandbox restricts egress", never "the sandbox has no outbound channel".
   A visible injected instruction also still enters the vault as
   `instruction_only` (flagged, not quarantined — presenter notes tripped the
   stricter rule), so there the model's own compliance is the only control.

**What would reopen it, i.e. what makes this stop being low:** a Cowork session
gaining an outbound channel the owner does not review — an unrestricted network
path, a messaging or upload tool, or an MCP server that relays. Any of those
turns the accepted residual into a live finding, because the owner-at-the-gate
assumption is what makes it low. Wiring the SEC-07 guard into the Cowork leg as
a mechanism rather than prose would narrow limit 1, and needs a hook surface
Cowork does not offer today.

---

## A-07 · A sandbox draft may declare itself a `decision`

**Raised as:** M-8's third leg, 2026-09-02. `brain dossier` answers
decision-state questions by separating a DECISION layer from the sources under
it, and membership of that layer is exact equality on `type: decision`
(`core/_retrieval.py`). A draft captured on the untrusted Cowork leg carries
`provenance.trust: untrusted`, and nothing looked at its `type`. So text a
sandboxed agent wrote — including text an ingested document told it to write —
can arrive holding the standing of a settled decision rather than a proposal.

The proposed fix was a rewrite: in the untrusted lane, `type: decision` becomes
`type: note` and the drain records `provenance.rewritten_type: decision`
alongside `provenance.trust`.

**Ruling: the owner REFUSED it, 2026-09-02 (loss L7).** Their words: *"Size bound
only. Keep the ability to create a decision note from the sandbox; apply the
256 MB uncompressed quarantine bound to Office files."* The approval was
recorded as `modified`, and its `modified_spec` — the binding sentence — reads
*"Quarantine a Word or PowerPoint file whose uncompressed size exceeds 256 MB;
leave sandbox drafts that declare type decision unchanged."* The size bound
shipped; the rewrite was not built (`grep -rn "rewritten_type" src tests tools`
returns zero hits, checked 2026-09-02 — the only occurrences anywhere are the
two in this entry).

**The reasoning.** The sandbox is where the owner works. A decision reached
there is still their decision, and a lane that silently demotes it produces a
worse failure than the one it prevents: the note reads as a proposal, the
dossier's decision layer is missing something real, and nothing in the note
says why. Reclassifying a person's own conclusion is not a control the machine
gets to apply on its own.

**Two lanes reach this, and they are not equally bounded — say so.**
- **The Cowork/VM lane is bounded by a human.** The VM only ever calls
  `draft_capture`, which stages into `capture-inbox/` and never signs, indexes,
  or opens the WAL. A `type: decision` draft from the sandbox enters the
  decision layer only when the HOST commits it — `brain write` / the drain, on
  the owner's machine.
- **The weekly synthesis sign-drain is NOT.** It runs headless and signs what
  it drains through `brain write --untrusted-author`, so an LLM-written
  `type: decision` note lands in the decision layer with no owner in the loop.
  Both lanes share `draft_drain.sanitize_untrusted_note`, which is what stamps
  `provenance.trust: untrusted` — and that sanitiser strips host-only
  provenance, refuses a duplicate id and keeps the id inside its subtree, but
  it deliberately does not touch `type`.

**What that has already produced, measured not guessed.**
`_evidence/security-followup/s04-untrusted-decisions.txt` (2026-09-02) counted
notes carrying BOTH `provenance.trust: untrusted` and `type: decision` across
every registered vault: **32** — 0 in this checkout's `vault/`, and all 32
across the operator's two other registered vaults (31 and 1). So this is not a
hypothetical residual; it is a standing one, and the register has to say the
number rather than imply zero.
Not one of them was mutated by this session: the owner refused the rewrite, and
retro-classifying 32 of their own notes is exactly the call the refusal
reserves to them.

**What still bounds the damage.** `dossier` reports every decision with its
`tensions` — newer sources that post-date it — so a decision the corpus has
moved past shows as contested rather than as quiet authority; and `type` is one
field of a note whose `provenance.trust` is right there beside it, readable by
anyone auditing the layer.

**What would reopen it:** a Cowork draft landing signed with no host step (the
A-06 inbound mirror, closed 2026-09-01 — that is the version of this that was
NOT accepted), or evidence that a specific untrusted-authored `decision` note
changed an answer the owner would have given differently. The 32 above are the
place to look first. The narrower fix available without touching `type` at all,
if the owner ever wants it, is to have `dossier` mark an untrusted-authored
decision in its output — reporting the provenance rather than overriding the
owner's word, which is the thing L7 refused.

---

## A-08 · The weekly synthesis model writes free text into session memory

**Raised as:** M-6's second leg, 2026-09-02. The weekly synthesis session runs
headless, reads the vault, and is handed `Edit` on `<vault>/.brain/memory/**`
(`scripts/brain-synthesis.sh:174`, and its byte-identical `_assets` twin). Its
prompt tells it to queue dated entries into `hot.md` when the maintain
heartbeat looks stale (`brain-synthesis.sh:56` — "the scheduled umbrella cannot
report its own death, so this weekly session is its watchdog"). Session memory
is exactly what the next session's start hook pastes back in. So a model that
read an ingested document has a pen on the file the next session reads first.

The proposed fix was to take the pen away: the model reports through its run
output, and the engine writes a fixed-shape summary line instead.

**Ruling: the owner REFUSED it, 2026-09-02 (loss L5).** Their words: *"Refused.
Keep today's behaviour: the weekly synthesis model keeps writing free text into
session memory. Recorded as an accepted risk."* The grant stands, no
`synthesis_memory.py` was written, the watchdog duty stays with the model, and
no test asserts the grant is absent — a test demanding that would fail
correctly-built work.

**The reasoning.** The free text is the value. A fixed-shape line can say
"3 invariants regressed"; it cannot say what the run actually noticed, and the
whole point of `hot.md` is a record a human *may* read (the PUSH interaction
model, 2026-07-13). Trading the one surface that carries judgment for a counter
is a worse system, and the owner is the one who reads it.

**What bounds it, and this is the part that changed on 2026-09-04.** The
residual was never "the model writes prose"; it was "that prose reaches the
next session as instructions". Until M-6's fence work that was a live path: the
injection block was wrapped in triple backticks, so a line of three backticks
in `handoff.md` closed it and everything after arrived as prose the model reads
as instruction — measured, `_evidence/security-followup/s08-fence-before.txt`.
Both session-start hooks now delimit with a marker minted per run, which the
writing model cannot know and the sanitizer strips if a line carries it. The
model still holds the pen; what it writes can no longer END the data block
early and gain the authority of the text around it.

**Do not read that as more than it is — both reviewers of 2026-09-04 said so
independently, and they were right.** A marker fence closes STRUCTURAL escape.
It does not make a model immune to a directive sitting *inside* a block that is
labelled as data: the hook concatenates the notes into `additionalContext`,
which arrives as text in the model's context, not as a parsed data structure
the runtime enforces. Content that never touches the delimiters can still say
something a session chooses to act on. The label and the five sanitiser regexes
are what argue against that, and an argument is not a boundary.

The one fix that WOULD be a boundary is not injecting model-authored free text
at all — which is exactly loss L5, and the owner refused it above. So this is
the shape of the residual, stated plainly: the channel is structurally sound,
the content is trusted by convention, and that is a deliberate trade the owner
made with the reasoning three paragraphs up.

Two narrowings worth recording, both measured 2026-09-04. The pen covers three
files — `handoff.md`, `hot.md`, `lessons.md` — but the start hook injects only
`handoff.md` (and the recent-commit list). `hot.md` and `lessons.md` are
scaffolded by the hook if absent and never read into context by it, so they
reach a session only when one opens them deliberately, outside this fence.

Two narrower bounds were already there and still are: the grant is `Edit`, not
`Write`, so the session can rewrite the three scaffolded files but cannot
create new ones under `.brain/memory/`; and the session runs sandboxed with
`Bash`, `WebFetch`, `WebSearch` and `Agent` denied and every network domain
blocked, so what it writes is the only thing it can do with what it read.

**What would reopen it:** a session-start path that injects session memory
without the marker fence (a third harness copying the old hook, or a consumer
reading `handoff.md` directly), or evidence that a synthesis-written entry
steered a later session — the fence makes that observable, since anything
claiming authority now sits visibly inside a labelled data block.

---

## A-09 · Windows hosts get no SEC-07 egress guard

**Raised** by the s08 adversarial gate, round 3 (Codex, HIGH), 2026-09-04.
**Accepted** the same day, as the least-bad of two measured states.

The guard is a POSIX shell script whose engine and registry pins must be
absolute paths starting with `/`. The installer writes them as NATIVE paths, so
on Windows they read `C:\Users\...` and the guard refuses — fail-closed, by
design. Its matcher covers `WebSearch|WebFetch|Bash|PowerShell|Write|Edit|
NotebookEdit|Read|Grep|Glob|mcp__.*`, so this is not a degraded guard. It is a
host on which every useful tool call exits 2, arriving through `brain update`,
which runs the installer unconditionally.

Measured on a POSIX box by writing each pin in the Windows shape, against the
real guard with a real registry:

    control, POSIX pins                     rc=0
    Windows-style engine pin                rc=2
    Windows-style registry pin              rc=2

**What was done.** `install()` now DECLINES to place or register the guard when
`os.name == "nt"`, and reports `skipped-unsupported-os` with `ok = False` and a
symptom naming the gap. The other hooks still install: the session-memory hook
is plain shell with no pins, and losing it would cost the owner their
degradation banner for no reason. Regression test:
`tests/test_session_hook.py::test_the_egress_guard_is_not_installed_on_windows`,
which also asserts the POSIX install still happens.

**What is accepted.** A Windows host runs with NO egress classification on
outbound tool calls. That is a real hole, and it is why the row reports
`ok = False` every session rather than being silently skipped.

**Why not fix it properly.** Normalising the pins into whichever form the
host's bash wants — Git Bash `/c/...`, MSYS, WSL — cannot be verified from a
POSIX box, and the guard's containment logic makes POSIX assumptions well
beyond the pins. An unverified workaround inside a fail-closed security path is
worse than a stated gap. Closing this needs a real Windows host and an
acceptance test proving one local call passes and one protected egress call is
blocked after install and after update. The only Windows CI signal this project
has runs on the export repo and does not exercise the hook installer.

---

## A-10 · A plaintext backup restores with no manifest check

**Raised as:** the Low-tier finding paired with `backup.py`'s restore path,
2026-09-02. A plaintext backup restores whether or not its manifest file is
present and matches — an old or hand-copied backup with no manifest, or one
whose manifest was edited, restores exactly like a verified one.

**The proposed fix:** refuse to restore a plaintext backup unless its manifest
is present and matches, with `--no-manifest` as an explicit opt-out for the
case a manifest genuinely does not exist.

**Ruling: the owner REFUSED it, 2026-09-02 (loss L8).** Their words: *"Refused.
Keep today's behaviour: a plaintext backup restores with no manifest check.
Recorded as an accepted risk."* `backup.py` is untouched (s09, 2026-09-04).

**The reasoning.** Not recorded beyond the refusal itself — the loss framed the
trade as "you lose the ability to restore an old backup whose manifest is
missing without passing `--no-manifest`", i.e. the fix's own escape hatch was
judged not worth the default friction it adds to every other restore.

**What is accepted.** Restoring a plaintext backup gives no signal that its
contents match what was actually backed up — a corrupted, truncated, or
hand-edited backup restores silently, indistinguishable from a good one.

**What would reopen it:** a restore from an unverified backup producing a
vault state the owner did not expect, or a decision to add the manifest check
back as opt-in rather than default.

---

## A-11 · The `.xlsx` expansion residual sits inside L7's own boundary

**Raised as:** a carried-forward finding from s11 (`CARRIED-FORWARD.md`),
2026-09-04. `src/brain/ingest/handlers/xlsx.py` bounds only the COMPRESSED
file on disk — `MAX_XLSX_BYTES` (100 MB, `xlsx.py:21`, quarantine reason
`file_too_large`) — then calls `openpyxl.load_workbook()` **twice**
(`xlsx.py:33` and `:39`, once for cached values and once for raw formulas),
so the uncompressed expansion an attacker controls is paid twice and bounded
never. `docx.py` and `pptx.py` route through the shared
`ooxml_expansion_gate()` (`base.py:135`), which bounds the UNCOMPRESSED
size at `MAX_OOXML_UNCOMPRESSED_BYTES` (256 MB) before construction; `xlsx.py`
does not call it and never has.

**This is a residual to record, not a gap to close.** Loss L7's `modified_spec`
(2026-09-02) is explicit: *"Quarantine a Word or PowerPoint file whose
uncompressed size exceeds 256 MB…"* — Word and PowerPoint only. `.xlsx` was
never in scope for this plan's size bound, so its absence here is not an
unresolved item from L7; it is a standing gap the owner has not been asked
about.

**Why two size constants that look like the same thing measure different
things.** `MAX_TOTAL_DECLARED_BYTES` (500 MB, `zip.py:27`) is the generic
`.zip` handler's cap on the SUM of every member's DECLARED (pre-decompress)
size across an ordinary `.zip` archive ingested directly — it has no OOXML
awareness and applies to `.zip` files, not to `.docx`/`.pptx`/`.xlsx` (which
are also zip containers, but are routed through their own dedicated
handlers, never through `zip.py`). `MAX_OOXML_UNCOMPRESSED_BYTES` (256 MB,
`base.py:61`) is a narrower, OOXML-specific bound on cumulative UNCOMPRESSED
bytes actually read back out of a `.docx`/`.pptx` archive, enforced by
`ooxml_expansion_gate()` before `python-docx`/`python-pptx` ever constructs
an in-memory tree. They are not redundant: one guards a generic zip's
declared-size total, the other guards actual decompression cost for the two
Office formats the owner approved a bound for. `.xlsx` sits in neither.

**What is accepted.** An `.xlsx` file under 100 MB compressed can still expand
to an arbitrarily large in-memory workbook, twice, with no cap — the same
class of resource-exhaustion risk L7 closed for `.docx`/`.pptx`.

**What would reopen it:** the owner asking for `.xlsx` to be bounded the same
way, or a measured incident (OOM, hang) traced to an oversized `.xlsx`
ingest — at which point applying `ooxml_expansion_gate()` (or an
`.xlsx`-specific equivalent, since `openpyxl`'s streaming reader does not
expose the same zip-member introspection `base.py` uses) to `xlsx.py` closes
this the same way `docx.py`/`pptx.py` were closed.

---

## A-12 · The concealment verdict is a declaration, not a provenance claim

**Raised as:** four adversarial-review rounds against s05c, 2026-09-04.
Owner ruling the same day, after the round-4 verdict and a three-option card.

**What the verdict is.** Ingest searches a document for text hidden from a
human reader and records the finding in the note's `injection_assessment.*`
frontmatter. `sync` and `rebuild` both project that record into one
`notes.concealment` column through `injection_fold.retrieval_verdict`
(`index/_planning.py:44`), and every read verb carries it on every row.

**What was attempted, and why it was withdrawn.** `clean` was designed to be
a positive assurance backed by the audit chain. Each review round found a
different way to mint it on bytes that had not earned it, and each fix created
the next defect:

| Round | `clean` was granted to | Fixed by |
|---|---|---|
| 1 | MALFORMED frontmatter | fail closed on a non-integer count |
| 2 | bytes `sync` had REFUSED (VULN-3387) | a content_hash guard on the backfill |
| 3 | bytes the audit chain never SIGNED | `_demote_unaudited` on the sync path |
| 4 | any note after `brain rebuild`, and any note once `audit.jsonl` was unreadable | — withdrawn instead |

Round 4's two defects were reproduced by execution before the ruling:
`brain rebuild` re-derives the verdict through the same `_plan_note` with no
audit facts at all (and `sync` escalates into `rebuild` on a schema mismatch
or an embed-model change, `index_stages/sync.py:74,84`), and a `chmod 000` on
`audit.jsonl` made round 3's gate inert — which for this field means granting
the assurance rather than refusing a downgrade. Transcript and probe output:
`_plans/security-followup-2026-09-01/_evidence/security-followup/s05c-surface-inventory.txt`.

**What is accepted.** `clean` means the note's frontmatter declares a
completed scan that found nothing. Nothing verifies those bytes were signed,
so an out-of-band edit of `injection_assessment.hidden` to `0` makes a note
read `clean`, and the same edit erases a real `hidden:<n>`. `clean` is not
evidence a note is unaltered and no control may be built on it. The three
conventions files, the `retrieval_verdict` docstring, the eight MCP tool
descriptions and the CHANGELOG all say so, and
`tests/test_concealment_retrieval_c_series.py::test_e_clean_is_not_a_provenance_claim`
pins it so the limit stays visible rather than latent.

**What is NOT affected.** The VULN-3387 downgrade guard is a separate control
over the same bytes and is untouched — an unexplained classification downgrade
is still refused unless the chain or an owner disposition explains those exact
bytes. `verify-audit --check-content` still reports the edit as unexplained
drift. What the concealment vocabulary buys is a reader who is TOLD when the
record says text was hidden.

**What would reopen it:** applying the projection guard at the ONE point that
produces the verdict (`_plan_note`) rather than at its callers, with `sync`
AND `rebuild` supplying audit facts and an unreadable chain failing closed.
Round 4's reviewers both recommended that shape. It was not taken because it
is the fifth patch in a class where three of the previous four created the
next defect, and because the assurance it protects is one no surface prints.

## A-13 · The VM egress ceiling verifies with a key the VM can rewrite

**Raised as:** Codex Security finding `2daadd6f` ("VM can forge the signed
egress ceiling via its mutable anchor"), medium, against `39e0e31` (v0.20.31).
Re-checked 2026-09-05 on master `1f5c2e71`; owner ruling the same day.

**What the finding says, and it is right.** `vm_ceiling.resolved_ceiling`
verifies `vm-egress-tier.signed` with the public key in `pinned-verify.json`,
and both files resolve through `cowork_staging.staging_root` or
`$BRAIN_RUNTIME_DIR` — the staging directory on the mount, which the sandbox
can write, or a directory the sandbox names. The `vault_id` check compares the
two files with each other. A VM session that generates its own Ed25519 pair,
writes both files and asks for `--max-tier MNPI` gets the clamp raised to
`MNPI` with provenance `signed-file`. Reproduced by execution 2026-09-05 at
the redirected path AND at the default staging root. No host key is needed.

**What is accepted, stated plainly.** The signed ceiling is a preference the
host records, not a confidentiality boundary. A control on the VM leg that
reads its own root of trust from the mount cannot be one: every staged file
sits where the sandbox can write, and Cowork offers no location it cannot.
`vm_ceiling`'s docstring, the bootstrap comment and the 0.20.31 CHANGELOG
entry said "a source the VM cannot forge"; that sentence is withdrawn, and
`tests/test_vm_ceiling.py::test_the_anchor_is_forgeable_from_the_mount` pins
the limit so it stays visible rather than latent (the same shape as A-12).

**Why the forged ceiling reaches no data.** Since A-05 closed (2026-09-01)
the vault and the published snapshot are OFF the Cowork mount: the installer
refuses to publish a snapshot inside an attached workspace, and both live
workspaces on the reference host carry no snapshot, no index and no anchor
(`brain doctor`, the VULN-3385 leak row, 2026-09-05). The role=vm CLI
therefore has no note body to surface at any ceiling. Cowork reads go through
the host `brain-mcp` broker at the HOST ceiling, with no per-caller ceiling
by the A-01 ruling. On a co-located workspace (still supported by the
installer) the Markdown is on the mount and the CLI gate was never the
boundary there. The one remaining consumer of the anchor is
`brain --role vm alerts`, whose summary the sandbox could equally forge —
that limit is already stated in `exceptions_verify.py`.

**What would reopen it:** a note body, a snapshot or a derived index landing
inside an attached Cowork workspace again (`tests/test_cowork_staging_off_the_mount.py`
fails), or a per-caller ceiling being introduced on the broker leg that
reads its decision from the mount.

---

## A-14 · The host serves a note's current bytes, signed or not

**Raised as:** the pentest team's re-test of VULN-3387, 2026-09-11: "the
vulnerability is fixed, but only for role VM. For role HOST the changed,
unsigned content is visible after `brain sync` and `brain sync --publish`. Is
this the expected behaviour?" — with the note that the change is to the note's
CONTENT, not its classification. Reproduced by execution the same day on a
scratch vault with the installed engine (0.20.37); owner ruling the same day.

**What the finding says, and it is right.** VULN-3387's fix has two halves.
`sync` refuses an unsigned classification DOWNGRADE and keeps the signed tier
(`index_stages/sync.py:_refused_downgrade`). `snapshot` withholds from the VM
copy any note whose current bytes match nothing the chain signed and no
disposition explains (`snapshot.py:_withhold_paths`) — content edits included.
Neither half touches the host's own index: after a body edit on disk, host
`get` returns the edited body, `sync --publish` records `withheld_drift: 1`,
`--role vm get` returns `not_found`, `verify-audit --check-content` reports
`1 unexplained`, and `doctor` carries the warning row.

**What is accepted, stated plainly.** The host reads the file, because the
file is the truth and the index a disposable cache. The owner edits notes in
an editor and those edits never pass the signing path; a host that refused
unsigned bytes would drop every hand edit from retrieval until re-signed, and
would not stop the only actor it could apply to — whoever can write the host
vault as the owner can also run `brain write` as the owner. The residual is
therefore: a process running with the owner's permissions can rewrite a note
and the host serves the rewrite. It is bounded by detection (INT-02: the
chain keeps the signed hash, the drift reads `unexplained` until ruled on)
and, since 2026-09-11, by the hit itself: the egress chokepoint stamps
`drift: unexplained` (or `explained`, once triaged) on every surfaced note
whose current bytes differ from what the chain signed
(`egress.py:_mark_drift`, `core/_audit.py:drift_marker`). Negative-only by
design — an ABSENT field is not an assurance, and covers a path the chain
never bound, the VM leg, an unreadable chain or an unreadable file; the read
path mints no positive provenance claim (A-12).
`tests/test_drift_inline_marker.py` pins both the marker and its silence.

**What is NOT affected.** The VM half is unchanged: the Cowork leg has had no
write access to the vault since A-05 closed (2026-09-01), and a drifted note
never reaches its snapshot. The injection question a rewritten note raises
(the row still reads `content_trust: curated`) is A-06's, and A-06's controls
apply unchanged.

**What would reopen it:** a note body reaching the VM snapshot with
`withheld_drift` at 0 while `verify-audit` reports it unexplained
(`tests/test_snapshot_drift_withhold.py` fails), or a host read surface that
bypasses `egress.apply_gate` and so carries no marker.

---

## Not accepted, and deliberately absent

The synthesis sign-drain finding ("Synthesis sign-drain signs untrusted
LLM-written notes", critical, `c9ba28c`) is **not** here. It was a real gap and
it is fixed: `brain write --untrusted-author` now applies the audited draft
path's controls before signing, and `draft_drain.sanitize_untrusted_note` is the
single implementation both paths call.
