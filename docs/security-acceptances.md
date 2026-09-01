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

## A-01 · MCP defaults to the full vault for host clients

**Raised as:** "MCP default now exposes full vault to LLM clients" (high),
against `9f9284b`, 2026-08-17.

**Ruling:** the owner's, 2026-08-17 — the same day. The host MCP server reads
the full vault. `HOST_MCP_DEFAULT_MAX_TIER` is deliberate, not a regression.

**The reasoning:** the classification gate exists to bound what leaves the host
toward an *untrusted* leg. A host MCP client is the owner's own desktop
application reading the owner's own vault on the owner's own machine; capping it
at Internal by default made the owner's tools worse at their job without moving
anything across a trust boundary. `--role vm` still defaults to Internal, which
is where the boundary actually is.

**What still bounds it:** `--max-tier` and `$BRAIN_DEFAULT_MAX_TIER` narrow any
individual client, and the VM leg's own default is unchanged.

**What would reopen it:** the host MCP server becoming reachable by anything the
owner does not control — a remote transport, a shared machine, a client that
relays to a third party.

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

**Status: MITIGATED 2026-08-30 (Closed Stacks, sessions s01–s07). The finding
stays OPEN at reduced severity — this is NOT a closure**, per s01's own
checkpoint (approved 2026-08-27T22:55:03): no per-caller tier ceiling was found
reachable for the Cowork leg, and the plan's own acceptance criteria say
plainly that outcome is a mitigation, not a closure.

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

**What is still accepted, stated plainly:**
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
  difference a mitigation buys.
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

Full record for a reader who did not follow the work:
`docs/security/vuln-3385-risk-reduction.md`.

---

## Not accepted, and deliberately absent

The synthesis sign-drain finding ("Synthesis sign-drain signs untrusted
LLM-written notes", critical, `c9ba28c`) is **not** here. It was a real gap and
it is fixed: `brain write --untrusted-author` now applies the audited draft
path's controls before signing, and `draft_drain.sanitize_untrusted_note` is the
single implementation both paths call.
