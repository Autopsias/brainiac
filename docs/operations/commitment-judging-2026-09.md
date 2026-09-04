# Commitment-extraction doctrine — judging run, September 2026

**Run date:** 2026-09-03. **Plan:** `_plans/commitment-judging-2026-09-03` (sessions s01–s06).
**Numbers source of truth:** `_plans/commitment-judging-2026-09-03/_evidence/judging/rates.json`.
Every figure below is read from that file; the file is not read from this one.
`scripts/report.py --recompute` reproduces it byte for byte (7332 bytes).

---

## UPDATE 2026-09-04 (round 7) — recall measured at last: the doctrine catches 1 commitment in 12

Round 7 ran revision 5 over the OTHER 8 meetings (the round-3 corpus) and then,
for the first time since round 3, measured MISSED. **Numbers source of truth:
`_evidence/judging/round6/rates.json`.**

The precision census was NOT run. Pooling round 6's 10 held-out rows with round
7's 16 gives n=26, which clears the 0.2 rule only at 0 or 1 errors. The owner
directed the rulings to MISSED instead, because a doctrine that stages almost
nothing scores well on precision whatever the census says.

### Recall

Pass B is a plain reader with no rulebook, so it is doctrine-independent and the
463 rows built in round 3 are still valid. 11 of revision 5's 16 staged rows
appear in it, leaving an unmatched pool of 452 rows where any miss must be. A
uniform sample of that pool, seed 7, was ruled by the owner.

| | value |
|---|---|
| pass A rows / staged / dropped | 294 / **16** / 278 |
| pass B rows over these 8 meetings | 463 |
| unmatched pool | 452 |
| rulings spent | 20 of 40 |
| confirmed real commitments | **8** (2 unsure) |
| estimated missed commitments | **181** [99 – 277] |
| **recall** | **8.1%** [5.5%, 13.9%] |

The 16 is a CEILING: recall assumes every staged row is a real commitment, which
the un-run precision census could only reduce. Revision 4 staged 39 of 265 turn
pairs (14.7%); revision 5 staged 16 of 294 (5.4%).

**The programme's finding.** Three wording revisions made precision worse.
Changing the unit made it better. But revisions 4 and 5 improved precision mainly
by staging less, and nobody measured that cost until now. A doctrine that catches
one commitment in twelve cannot deploy at any precision.

### The corpus defect, found by every agent independently

All 8 meetings collapse two or more diarized speaker slots onto one name — one
meeting puts five slots on a single name. 67 slots resolve to 50 distinct labels,
and 28 of the 67 are numbered, unnamed speakers. All 8 nonetheless carry
`validation: pass` and `speaker_resolution: owner-verified`.

This matters because revision 5's `no-promisor` rule fires on an UNNAMED
promisor and cannot fire on a WRONGLY NAMED one. A row therefore passes every
rule while carrying attribution nobody can check. The defect pushes both ways: it
removes real commitments from the staged set and it weakens the rows that remain.

**A separate defect sits behind it.** The transcription pipeline's speaker
validation cannot detect a collapse, so it has never reported one. That is a
pipeline defect, not a doctrine defect, and it is the reason the revision loop is
now paused: a revision 6 written against this corpus would fit a transcription
artefact rather than predict anything.

One related measurement, because the same suspicion applied to the new dedupe
rule: 20 of 51 `duplicate` drops repeat verbatim in the transcript, but 18 of
those sit in one meeting where an automatic-transcription loop repeats a
90-second block seven times. Outside it the figure is 2 of 30, so the rule itself
is sound.

### Status

`96eef39` stands. The doctrine is authored, NOT deployed, and `commitments.md`
remains absent from `gearbox-private/main`. The revision loop is PAUSED pending a
decision about the transcripts.

### The nine open revision-5 defects

Do NOT act on these while the loop is paused. They are recorded here because
`rates.json` sits under `_plans/`, which `.gitignore` excludes wholesale — this
file is the only durable copy. Four are re-reported from round 6, which means two
independent rounds found them and neither closed them.

1. no-promisor has no precedence against hypothetical for an unnameable speaker making a modal offer
2. no-promisor is defined against the resolved speaker list, so a promise by a named non-attendee falls to the wrong reason
3. no drop reason fits a promise REFUSED out loud, though the stage-it text names refusal
4. no rule for a DUPLICATED name in the speaker list (distinct from a numbered speaker)
5. no rule for a request directed AT the owner; the stage-it bullet covers only the owner asking others
6. no-creditor vs workstream still has no precedence (re-reported)
7. the dedupe rule still contradicts itself on which restatement to keep (re-reported)
8. counterparty defined two ways: instructions say creditor, section 3 says the other human (re-reported)
9. no drop reason fits a pure forecast that binds nobody

Each one is a case the rulebook cannot decide, not a case it decides wrongly. A
revision 6 must answer them, and it cannot be written against this corpus while
the speaker labels collapse.

---

## UPDATE 2026-09-04 (round 6) — three defects closed, and the corpus ran out

Revision 5 keeps revision 4's turn-pair unit unchanged and closes three defects
the extraction agents found: a named `no-promisor` drop reason, a deduplication
rule, and a creditor that need not be the owner.

The held-out corpus is the 9 meetings of rounds 1-2. The defects were found on
the OTHER 8 meetings, so they are genuinely held out here. **Numbers source of
truth: `_evidence/judging/round5/rates.json`.**

| | revision 2 | revision 3 | revision 4 | **revision 5** |
|---|---|---|---|---|
| INVENTED | 0.474 (9/19) | 0.727 (8/11) | 0.282 (11/39) | **0.100 (1/10)** |
| Wilson 95% | [0.273, 0.683] | [0.434, 0.903] | [0.165, 0.438] | **[0.018, 0.404]** |

**The like-for-like comparison.** Revision 3 and revision 5 were both measured
on THESE 9 meetings. On one corpus the rate went from 0.727 (8/11) to 0.100
(1/10). That is the cleanest doctrine comparison the programme has produced.
Revision 4's 0.282 came from the other 8 meetings and is not directly
comparable to either.

**The run cannot decide, and this was knowable in advance.** Revision 5 staged
only 10 rows. At n = 10 the Wilson upper bound is **0.278 even for a perfect
0 of 10**, so no outcome on this corpus clears the 0.2 threshold. To decide at
a true rate near 0.10 takes roughly 60 staged rows:

| staged rows | wrong (at 10%) | Wilson upper |
|---|---|---|
| 20 | 2 | 0.301 |
| 40 | 4 | 0.231 |
| 60 | 6 | 0.201 |
| 70 | 7 | 0.192 |

**Revision 5 is much more conservative, and the cost is unmeasured.** It staged
10 rows from 266; revision 4 staged 39 from 265 on the other corpus. The two new
rules account for 49 drops — `duplicate` 31, `no-promisor` 18. Fewer wrong
stages is what the threshold rewards, but pass B was not run, so how many REAL
commitments the new rules refuse is not measured. **A doctrine that stages
almost nothing scores well on precision and may still be useless.** MISSED is
now the open risk, not INVENTED.

**Drop error: 2 overturns of 9** — `no-creditor` and `uncertain` overturned;
`duplicate`, `hypothetical`, `third-party` and `workstream` held; `inferred`,
`no-promisor` and `past-tense` came back unsure. **Indicative only**: 1 row per
drop reason, below the non-vacuity floor of 5.

**Closing three defects exposed ten more.** Four were reported by two or three
agents independently, which makes them defects in the text rather than one
reader's quirk:

1. **The deduplication rule contradicts itself.** It says keep the statement
   with the most detail AND emit the LATER statements as `duplicate`. When the
   later statement is the more detailed one, those disagree.
2. **`no-creditor` versus `workstream` has no precedence rule** when both a
   creditor and a finishable deliverable are absent. Three agents each invented
   the same tie-break, and each said another reader could split it differently.
3. **No drop reason fits an obligation satisfied INSIDE the meeting** — "vou-te
   dar uma estimativa", delivered in the next turn. Agents spread these across
   `past-tense`, `no-creditor` and `workstream`.
4. **The deduplication rule cannot distinguish a speaker restating a promise
   from an ASR artifact repeating a line.**

Six more are listed in `round5/rates.json`, including a collision between the
deduplication rule and the turn-pair rule (a request turn is both a candidate
row and the `context_quote` of its assent), a `speaker` field that cannot hold
the two named promisors of a request addressed to "vocês", and a `no-promisor`
reason that covers neither a corporate promisor nor an unnamed addressee.

**What the run verified about itself.** Two extraction agents hit a shared
scratchpad filename collision and initially read the WRONG transcript. Both
detected it and redid the work. The orchestrator's own verifier re-checks every
`source_quote` and non-null `context_quote` against the transcript named in
`frame.json` and found **0 defects across all nine files**, which clears the run
independently of what any agent reported about itself.

**Caveats.** Diarization is poor across this corpus: duplicated speaker entries,
bare `Speaker N` labels, and turns attributed to a speaker who plainly did not
say them, so `speaker` and `direction` are unreliable. One meeting carries a
stuck-decoder artifact — 1016 of its 1439 turns are the same repeated line.

---

## UPDATE 2026-09-04 (round 5) — the unit change, measured held-out (superseded above)

Revision 4 changed one thing and only one thing: **the unit**. Pass A now reads
each candidate sentence together with the turn before it and the turn after it,
and judges the obligation from that window. It records the turn that supplies
the deliverable as `context_quote`. No wording rule was added or relaxed.

The held-out corpus is the 8 meetings of round 3 — a different set from the 9
that produced the finding. Pass B was not run: the question is precision only,
and a census of every staged row answers it directly. **Numbers source of truth:
`_evidence/judging/round4/rates.json`.**

| | revision 1 | revision 2 | revision 3 | **revision 4** |
|---|---|---|---|---|
| INVENTED | 0.000 (0/5) | 0.474 (9/19) | 0.727 (8/11) | **0.282 (11/39)** |
| Wilson 95% | [0.000, 0.434] | [0.273, 0.683] | [0.434, 0.903] | **[0.165, 0.438]** |

**The doctrine still MUST NOT deploy.** The rule is stated against the interval,
not the point, and 0.438 is above 0.2. But this is the first variant whose
interval is not wholly above the threshold, and the drop from 0.727 to 0.282 is
the largest single effect any of the five rounds measured.

Pass A emitted 265 rows, staged 39, dropped 226. The adjudication was 46 blind
rulings — all 39 staged rows as a census, plus 1 row per drop reason for
blinding, shuffled with seed 7 — with 1 unsure. The unsure row stays in the
denominator and is not counted as an error; without it the rate is 11/38 = 0.289.

**The window does real work, and it also invents.** 19 of the 39 staged rows
exist only because pass A read the neighbouring turn, and 31 drops were decided
by the window. Among those 19, 7 were wrong; among the 20 rows that stand on
their own, 4 were wrong. Both cells are small and neither carries an interval.
This says where the errors sit. It does not measure a difference.

**Drop error: 3 overturns of 7** — `inferred`, `no-creditor` and `uncertain`
were each overturned; `hypothetical`, `past-tense`, `third-party` and
`workstream` held. **Indicative only**: 1 row per drop reason is far below the
non-vacuity floor of 5, so no rate and no interval come from it.

**Three defects in revision 4, found by the extraction agents, not by the
rulings.**

1. No drop reason covers a promisor the transcript cannot name. One agent routed
   those rows to `inferred`, so this round's `inferred` count is impure.
2. The doctrine says nothing about an obligation repeated inside one meeting.
   Two agents deduplicated in opposite directions, so the staged count is partly
   agent-dependent.
3. Revision 3's stage-it rule still names the owner as the creditor, so a
   directive at a named person who owes somebody else is dropped. This is
   under-inclusive. It only removes rows, so it does not inflate INVENTED.

**The stability check.** Four already-ruled rows were re-asked at the end of this
census: **3 of 4 matched**. The row that flipped was "we will confirm if we
already have a contract signed with the vendor or not". Five of the eight
extraction agents also reported scrambled speaker labels, so `speaker` and
`direction` are unreliable this round.

---

## UPDATE 2026-09-04 (round 4) — the held-out test of revision 3 (superseded above)

Revision 3 tightened §2 in three places, each named by a number from round 3:
a named creditor is required (`no-creditor`), a direction of work is not a
deliverable (`workstream`), and a request aimed at a named person is not an
inference. Replayed by hand against round 3's own 19 rulings it predicted
INVENTED 0.200. **That prediction was an in-sample fit and it was wrong.**

Round 4 is the held-out test. The 9 meetings of rounds 1-2 were judged under the
ORIGINAL doctrine and had never seen a revision. Pass A re-ran over them with
revision 3; pass B was not run, because the question is precision only and a
census of every staged row answers it directly. **Numbers source of truth:
`_evidence/judging/round3/rates.json`.**

| | revision 1 | revision 2 | revision 3 predicted | **revision 3 measured** |
|---|---|---|---|---|
| INVENTED | 0.000 (0/5) | 0.474 (9/19) | 0.200 (2/10), in-sample | **0.727 (8/11)** |
| Wilson 95% | [0.000, 0.434] | [0.273, 0.683] | — | **[0.434, 0.903]** |

Pass A emitted 314 rows over 314 candidate sentences, staged 11, dropped 303.
The adjudication was 25 blind rulings — all 11 staged rows plus 2 rows from each
of the 7 drop reasons, shuffled with seed 7 — with 0 unsure.

**Three doctrine variants have now been measured and all three fail the 0.2
threshold.** The in-sample estimate missed the held-out value by a factor of
3.6, which is what an in-sample estimate is for. The pattern across rounds is
not that one rule is wrong: each revision fixed the rule the previous round
indicted and the headline rate did not improve.

**What survived.** The 3 rows ruled real are all a concrete deliverable stated
in the first person, one of them with a date. The 8 rejected include a modal
offer ("posso partilhar"), a bare assent, a truncated sentence, and several
sentences whose obligation lives in the surrounding turn rather than in the
sentence itself. **The unit is wrong: pass A judges one sentence, and a promise
is made across a turn pair.** No wording of §2 reaches that.

**Drop error: 0 overturns of 14** — `no-creditor`, `workstream`, `inferred`,
`hypothetical`, `past-tense`, `third-party` and `uncertain` all clean. This is
**indicative only**: 2 rows per stratum is far below the non-vacuity floor of 5,
and no weighted rate is computed from strata whose weights run 1.5 to 42.5.

**One threat to the comparison, stated plainly.** Round 3's census and round 4's
census were rated on different days and no intra-rater check was run this round.
Round 1 measured intra-rater agreement at 4/4. Comparing 0.474 with 0.727
assumes a rater stability that was not re-measured.

---

## UPDATE 2026-09-03 (round 3) — eight recovered meetings, and a measured INVENTED (superseded above)

The usability rule counted the `speakers:` list without deduplicating it, so 8
meetings were rejected because the owner's name appeared 2-5 times — a
diarization artifact, not two people. `scripts/select_meetings.py` gained an
opt-in `--dedupe-speakers`; with the flag off the round-1 evidence reproduces
byte for byte, with it on the usable set goes 10 -> 18. The 8 recovered meetings
(6684 turns) were never judged before. Pass A ran on the REVISED §2, pass B ran
plain. **Numbers source of truth: `_evidence/judging/round2/rates.json`;
rulings in `_evidence/judging/round2/adjudication.json`.**

Both agree strata were a full census (19 + 24 = 43 rulings, weight 1.0, no
sampling error). The 443-row `disagree-A-refused` stratum was not sampled.

| Rate | Value | Real rulings | Wilson 95% |
|---|---|---|---|
| **INVENTED** — staged rows the owner ruled *not* a commitment | **0.474 (9/19)** | 19 | **[0.273, 0.683]** |
| **MISSED** | not measured | 0 | — |
| **Drop error on rows pass A looked at** | 0.167 (4/24) | 24 | [0.067, 0.359] |

**INVENTED is measured, and it fails the 0.2 threshold outright.** The whole
interval sits above 0.2. This is not an under-powered result: it is a census of
every row pass A staged, 0 unsure, and the lower bound alone is above the
threshold. **The doctrine must not deploy on this evidence.**

**The revision fixed the two rules it targeted and broke precision doing it.**
Of 24 ruled drops: `past-tense` 0 overturns of 5, `unnamed-counterparty` 0 of 2,
`hypothetical` 0 of 2, `third-party` 0 of 2, `uncertain` 0 of 1 — and
`inferred` **4 overturns of 12**. So `inferred` is now the only drop rule the
rulings indict, and the two rules round 2 indicted are clean.

**Where the 9 false stages came from.** Split the 19 staged rows by counterparty:

| counterparty | staged | ruled *not* a commitment |
|---|---|---|
| a group, or `unnamed` (the relaxed rule) | 7 | **5 (0.71)** |
| a named individual | 12 | 4 (0.33) |

The relaxed counterparty rule is the worse half, but both halves fail. Six of
the nine false stages are in-progress status reports — "I am trying to activate
X", "we are doing a version 2", "I am going to do the diagnosis" — which the
new past-tense wording explicitly tells pass A to stage. The owner ruled them
not ledger entries. **The revision traded a drop error of 0.25 for a stage
error of 0.47.**

Reader agreement over the 486 diff rows: Po **0.088**, Gwet's AC1 **−0.823**
(A called 19 rows a promise, B called 462). Pass B stays far more liberal than
pass A, as in round 1.

Everything below is the earlier record. The round-2 block's headline
("INVENTED 0.000, 0/5, census") is superseded: that census held 5 rulable rows
from 9 meetings, this one holds 19 from 8 new meetings, and the sign flips.

---

## UPDATE 2026-09-03 (round 2) — the census, and what it changed (superseded above)

The owner ruled the 11 rows the flat cap never showed him: a **census** of both
agree strata (`agree-promise` 6/6, `agree-not-promise` 5/5, weight 1.0). The
disagree stratum keeps its 40/343 sample (weight 8.575). 51 rulings total.
**Numbers source of truth for this section:**
`_evidence/judging/rates-stratified.json`; rulings in
`_evidence/judging/adjudication-stratified.json`.

| Rate | Value | Real rulings | Wilson 95% |
|---|---|---|---|
| **INVENTED** — staged rows the owner ruled *not* a commitment | **0.000 (0/5)** | 5 | [0.000, 0.434] |
| **MISSED** — weighted | 0.914 (53.45 / 58.45) | 13 | see caveat |
| **Drop error on rows pass A looked at** | **0.250 (3/12)** | 12 | [0.089, 0.532] |

**INVENTED is measured, not undefined.** It is a census of every row pass A
staged, so there is no sampling error in it at all: of the 6 staged rows, 5 were
rulable and the owner confirmed all 5. Pass A invented nothing. But 5 rulings is
exactly the non-vacuity floor, so the interval still reaches 0.434 and **cannot
clear a 0.2 threshold**. The corpus ceiling stands: precision here is
measurable but not decision-grade, and no re-run of this corpus fixes that.

**MISSED is no longer structurally forced**, but its interval is not
decision-grade either: the value is weighted while the Wilson interval is the
unweighted one on 13 rulings, and the 8.575 design weight makes true uncertainty
wider than that interval shows. Do not quote a MISSED interval.

**The census found a defect the disagreement sample could not see.** Both passes
dropped the same rows as `past-tense`, so those rows landed in
`agree-not-promise` and no disagreement sample could ever reach them. The owner
overturned two of them, and was unsure on a third:

- one reporting that the speaker has already asked two named colleagues for
  something and is now waiting on them ("… já os pedi.");
- one reporting that the speaker created a change request that "está na fase de
  desenho" — it exists and is unfinished.

(Quoted in fragments, without the participant names they contain; the full rows
are `5309a60847fc` and `aea1a8988731` in the evidence sheet.)

Both report the speaker's OWN work as started and unfinished. The rule reads the
tense; what makes a row a ledger entry is whether the work is still open.

Overturns by drop reason, all 51 rulings: `past-tense` **2 yes** / 3 no / 1
unsure · `unnamed-counterparty` **1 yes** / 0 no · `hypothetical` 0/3 ·
`inferred` 0/1 · `uncertain` 0/2 + 1 unsure.

**So `past-tense` — not `unnamed-counterparty` — is now the worst rule.** The
proposed fix for both is
`_evidence/judging/doctrine/commitments-REVISED-section2.md`. It is authored,
NOT deployed; `96eef39` still stands and the doctrine remains absent from
`gearbox-private/main`.

Everything below this block is the round-1 record and is left unaltered. Where
it says INVENTED is `undefined (0/0)`, or that the drop error is 0.125 (1/8),
that was true of the 40-row flat cap and is superseded by this section.

---

## The two headline rates (round 1, superseded above)

| Rate | Weighted value | Real rulings | Wilson 95% (on the real rulings) |
|---|---|---|---|
| **INVENTED** — staged rows the owner ruled *not* a commitment | **undefined (0/0)** | 0 | — |
| **MISSED** — owner-confirmed commitments the doctrine did not stage | **1.000** (51.45 / 51.45 weighted; 6/6 raw) | 6 | [0.610, 1.000] |

Both are the weighted (inverse-probability) form, which is the headline everywhere
in this report. Raw and weighted coincide here for a reason worth stating: every
ruled row came from a single bucket, so the weight (8.575) cancels top and bottom.

**Neither number decides anything, and the reason is the same for both.**
The owner's 40-row adjudication cap (seed 7) drew all 40 rows from the
`disagree-A-refused` bucket, which is 343 of the 354 sheet rows (96.9%). Not one
of the 6 rows pass A actually *staged* was put to him.

- INVENTED therefore has no denominator at all: `undefined (0/0)`. It is not
  "low"; it is unmeasured.
- MISSED is 1.000 **by construction, not by finding**. Once no staged row is
  ruled, every row the owner ruled `yes` is necessarily a row A did not stage, so
  the ratio is forced to 1. `rates.json` carries this as
  `missed.weighted.structurally_forced: true`. Read it as "the draw carried no
  information about recall", not as "the doctrine missed everything".

### The one rate this run *can* support

Of the 40 ruled rows, 9 are sentences pass A emitted and then rejected under a
named §2 drop reason. Eight of those were rulable (one `unsure`), and the owner
overturned exactly one:

> **Pass A's drop error on rows it actually looked at: 0.125 (1/8), Wilson 95% [0.022, 0.471].**

The single overturn was dropped for **`unnamed-counterparty`**. Ruled drop
reasons: `past-tense` 3 no, `hypothetical` 2 no, `inferred` 1 no, `uncertain`
1 no + 1 unsure, `unnamed-counterparty` 1 **yes**.

### Numbers behind the rates

- 9 meetings, 354 diff rows, 40 rulings: 6 `yes`, 32 `no`, 2 `unsure`.
- **Unsure share: 0.050 (2/40).** Low — the sentences shown were mostly rulable
  on their own.
- **Yield: 0.667 staged rows per meeting (6 staged / 9 meetings).** Descriptive
  count, no interval, by design.
- Pass A emitted 80 candidates across the 9 meetings: 6 staged, 74 dropped.
- Drop reasons over A's 74 drops: `hypothetical` 20, `inferred` 16,
  `third-party` 12, `past-tense` 10, `unnamed-counterparty` 9, `uncertain` 7.
- Per-meeting rows / rulings / staged:

  | meeting | diff rows | ruled | A-staged |
  |---|---|---|---|
  | m-2026-06-23T1604-500 | 53 | 5 | 2 |
  | m-2026-06-25T0000-1439 | 63 | 6 | 2 |
  | m-2026-06-26T0000-419 | 27 | 2 | 0 |
  | m-2026-07-01T0000-797 | 34 | 5 | 1 |
  | m-2026-07-07T1105-408 | 31 | 7 | 0 |
  | m-2026-07-14T1424-675 | 25 | 2 | 0 |
  | m-2026-07-14T1629-561 | 40 | 5 | 1 |
  | m-2026-07-14T1934-817 | 36 | 5 | 0 |
  | m-2026-07-17T1509-614 | 45 | 3 | 0 |

### Sampling fractions: design vs realised

s04's manifest offered a census of every bucket (`fraction_design` 1.0
everywhere). s05's 40-row cap changed that, and the realised inclusion
probability is recorded nowhere but here:

| bucket | rows | design fraction | ruled | **realised fraction** | weight |
|---|---|---|---|---|---|
| `agree-promise` | 6 | 1.0 | 0 | **0.000** | — |
| `agree-not-promise` | 5 | 1.0 | 0 | **0.000** | — |
| `disagree-A-refused` | 343 | 1.0 | 40 | **0.1166** | 8.575 |

They differ, and they differ enormously. A weighted rate computed from the
design fraction after that cap would have been inflated by the cap factor with
nothing downstream able to re-derive the truth. Weights here use the realised
fraction only.

### "Refused" is not "never looked"

Of the 343 `disagree-A-refused` rows, only **69** are sentences pass A emitted
and rejected. The other **274** are sentences A's extractor never emitted at all
(`match_rule: "b-only-unmatched"`). The 40 rulings split 9 / 31 the same way.
Five of the six `yes` rulings fall in the *never emitted* group — an extractor
coverage gap upstream of §2's drop rules, not a §2 judgment error. Pooling the
two would report "the doctrine refused a real commitment" for sentences the
doctrine never saw.

### Stability checks

- **Leave one meeting out** (recompute both weighted rates 9 times, dropping one
  meeting each): MISSED range **[1.000, 1.000]** across all 9 — unchanged,
  because it is structurally forced. INVENTED undefined in all 9 (`n_defined: 0`).
- **MISSED leave-one-`agree-not-promise`-yes-out sensitivity:** not exercised —
  `n_droppable: 0`. No `agree-not-promise` row was ruled, so MISSED carries no
  evidence from the agreement sample at all. This is the sensitivity the plan
  most wanted, and the draw removed the ability to compute it.
- **Reader agreement** (pass A vs pass B row classification, n = 354):
  raw agreement Po **0.031**, **Gwet's AC1 −0.938**. Marginals: A called 6 rows
  a promise and 348 not; B called 349 a promise and 5 not; both agreed "promise"
  on 6 and "not a promise" on 5. AC1 was pre-specified in `sample-manifest.json`
  before any result was seen (Cohen's kappa is unstable when one class is rare,
  which is exactly this data). The two readers barely agree because pass B is
  vastly more liberal than pass A.
- **Intra-rater agreement (s05):** 4/4 rows agreed on repeat presentation.
- **Known positive (s02):** **MATCH — 1 staged, 4 dropped**, on
  `m-2026-07-14T1516-740`
  (`_evidence/judging/passA/known-positive.md`). Note the qualification recorded
  there: the staged row survived only because the counterparty rule was read by
  intent rather than by the letter — the same `unnamed-counterparty` rule the one
  adjudication overturn indicts.
- **Agreement sample:** 11 rows were pre-selected as the agreement probe; **0**
  of them reached the owner under the cap. The overturn count is 0 out of 0, so
  the Wilson upper bound on pass A's drop error from *that* probe is undefined —
  no information, not a clean zero.

---

## The limit

**A promise neither reader emitted is unmeasurable here.** It cannot appear in
any bucket — not even in `agree-not-promise`, which holds only rows pass A
emitted as drops. MISSED is measured over what pass B or the agreement sample
surfaced, and is therefore a **LOWER BOUND** on the true miss rate. The
agreement sample is the only probe that could have touched the joint blind spot,
it can only surface rows pass A emitted, it holds 11 rows, and this run ruled
none of them; so the joint blind spot is **not measured at all** by this run.

The two readers are the same model family, so their errors are **correlated**,
and two readings are worth materially less than two independent ones. This plan
did **not** quantify that correlation and does not assert a figure for it; the
coefficient quoted during the plan's hardening pass could not be traced to a
source that states it, and an unsourced coefficient printed in a report is worse
than a plain statement of direction. Direction only: the correlation pushes the
true miss rate **above** what is reported here.

**The plan's open question — should pass B have run on a different vendor's
model? — is answered and closed: no.** Running pass B elsewhere would have
exported restricted client transcripts outside this project's data boundary,
which the plan forbids outright. That bar holds regardless of how much
independence a second vendor would have bought — a quantity this plan never
measured and must not assert.

The Wilson intervals above ignore the stratification and the meeting clustering.
They are a **floor** on the uncertainty, not a full account of it.

---

## The corpus ceiling — a finding in its own right

Measured on 2026-09-03 from `_evidence/judging/usable-set.json`:

- The whole corpus is **45** `*_transcript.md` files totalling **34,479 turns**.
  At the project's measured rate of ~1 staged candidate per 1,400 turns, that
  projects **at most ~25 staged candidates even if every file were readable**.
- Only **10** files pass the usability rule (**6,618 turns**, ~4.7 projected
  candidates). This draw took 9 of them (**6,230 turns**, ~4.5 projected).
  Pass A actually staged **6** — close to the projection.
- Relaxing the rule to "owner named at least once" would add the **11**
  duplicate-owner meetings (**9,831 turns**) and reach only **~12** projected
  candidates.
- s01's power table (`_evidence/judging/precision.md`) puts the denominator
  needed at **~20** for a HIGH result and **~40** for a LOW one.

**So no sample this corpus can supply resolves the 0.2 INVENTED rule, at any N.**
More meetings is not a route to a decision here, and this report does not offer
it as one. The owner ruled on 2026-09-03 to proceed at 9 meetings on exactly
this basis.

**MISSED is not bound by that ceiling** — its denominator is fed by pass B's
much longer list (349 rows called a promise), not by pass A's staged rows. MISSED
can still become decision-grade in a future run even though INVENTED cannot.

---

## Decision card

**Which rate is decision-grade?** Neither, and not for the same reason.
INVENTED is `undefined (0/0)` — zero real rulings, below the floor of 5, no
recommendation may rest on it. MISSED has 6 real rulings, clearing the floor,
but its value is structurally forced by the draw and so carries no information
either. The only rate with real content is pass A's drop error on rows it looked
at: **0.125 (1/8), Wilson [0.022, 0.471]** — an interval that straddles 0.2 and
cannot decide the threshold rule.

**The threshold rule cannot be applied.** It is stated against the interval, not
the point: the weighted INVENTED interval does not exist, so the rule neither
passes nor fails. **A deploy recommendation requires the interval to have
resolved, so option (a) is off the table for this run.** The run was
under-powered — and, per the corpus ceiling above, permanently so for INVENTED.

### Options

**(a) Deploy — revert `96eef39` in `gearbox-private`.** *Not available this run.*
Even setting the missing evidence aside, this exact doctrine text carries two
known defects that a deploy would re-ship
(`_evidence/judging/doctrine/provenance.txt`):
1. **Origin-note truncation** (`commitments.md:87`) — `origin_note` is sanitized
   to `[A-Za-z0-9][A-Za-z0-9._-]{0,118}`, a 118-character cap on the transcript
   note id, which the sibling plan diagnosed as guaranteeing a **dead transcript
   pointer** for every candidate whose id exceeds 118 chars after sanitization.
   The fix exists only on the unmerged branch `plan/commitments-identity-2026-08-26`.
2. **Dead `--run-kind` flag** (`commitments.md:151`, `SKILL.md:441/509/575`,
   `run_kind` at `SKILL.md:523`) — the doctrine instructs
   `brain cos-run-begin --run-kind transcript --attended`, and the installed
   engine has no `--run-kind` flag. Following the text literally fails at that
   step.
   *Trade-off:* ships a diagnosed dead pointer and a step that cannot run, on
   evidence that does not exist.
   *Command, if it is ever taken:*
   `git -C ~/gearbox-private revert 96eef39` (owner-only; requires both defects
   fixed and `--run-kind` shipped first).

**(b) Revise §2 first — the `unnamed-counterparty` drop rule.** *Recommended.*
It is the one drop reason the rulings indict (the single overturn of 8 rulable
looked-at drops), and independently the one rule s02's known-positive check had
to read by intent rather than by the letter to make the check run at all. Two
independent signals, same rule.
*Trade-off:* costs a doctrine edit and a re-run of pass A, and rests on **one**
overturn — thin evidence, honestly labelled, but the only signal this run
produced.

**(c) Do not deploy, change nothing.** *Trade-off:* zero cost and zero risk, and
leaves the doctrine's precision permanently unmeasured, since the corpus cannot
supply the denominator.

### Recommendation

**(b), and re-run the adjudication without the bucket-blind cap.** The 40-row
cap sampling flat across 354 rows drew zero of the 6 staged rows and zero of the
11 agreement rows; a stratified cap that forces every `agree-promise` and
`agree-not-promise` row into the sheet costs the owner 11 extra rulings and is
the difference between an undefined INVENTED and a measured one. That change is
free and is the highest-value fix available. Alongside it, revise the
`unnamed-counterparty` rule in §2.

### If he does nothing

The doctrine stays authored-not-deployed on `gearbox-private` commit
`dd470db88d9f8d205f5d88b2d764609827224235`, behind revert `96eef39`, with both
known defects intact. Nothing breaks; nothing improves; the measurement stays
undone.

---

## Evidence — confidentiality gate and greps

All three run over the two published files, staged, from the repo root.

**1. `python3 tools/check_client_names.py`** — exit 0, output verbatim:

```
client-name gate: scanned 2 file(s) against 266 term(s)
```

Scanned-N (2) and against-M (266) are both above zero, so the gate ran rather
than skipping. That line is what is checked, not the exit status: a missing
denylist also exits 0, printing `SKIPPED`, so a skipped gate and a clean gate
are indistinguishable from the exit code alone.

**The gate is proven able to fire, on this very report.** The first draft wrote
the transcripts-root directory basename out literally inside the grep-2 command
line below. The gate hit it — `1 hit(s) in staged files`, exit 1, pointing at
the line — and the line was reworded to name the root indirectly. That is a
known positive, not a hypothetical: this check's all-clear is not the all-clear
of an empty input.

**What the gate cannot do:** it matches only terms in an external client
denylist, so it cannot recognise a meeting title or an attendee name. Those are
protected by the id-only convention and by greps 2 and 3, not by the tool.

**2. Transcripts-root basename.** The literal basename is deliberately not
written into this file (see above); the check resolves it from
`_evidence/judging/paths.json` at run time and greps both published files for
it:

```
transcripts-root basename check: 2 file(s) -> 0 hit(s)
```

**3. No `speaker`, `counterparty` or `to_whom` string present in `diff.jsonl`
appears in either published file.** The candidate list is built in memory from
`diff.jsonl` and never written to disk — that list is itself the thing being
protected. Whole-word, case-insensitive:

```
speaker/counterparty leak check: 30 distinct name(s) from diff.jsonl vs 2 file(s) -> 0 hit(s)
known-positive probe: the matcher FIRES on a string that does contain one of the 30
```

The probe line matters for the same reason as the gate's: a name-matcher that
returns "clean" because its name list came back empty would look identical to
one that genuinely found nothing.

All meetings in this report are named by id only
(e.g. `m-2026-07-14T1424-675`).
