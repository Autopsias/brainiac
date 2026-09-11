# The vault asks the owner — the interview lane (INT-01)

**Status: shipped 2026-09-09.** The first night (2026-09-09 02:00) stopped at
the door (Outlook not signed in) and the lane's first guard skipped it; the
guard now runs the lane on a door-closed night too, and the day's three
questions were drawn by hand at 07:12 and phrased 3/3 on sonnet. Owner ruling
(2026-09-08): the interview must be agent-initiated and agent-managed; the
owner never starts it and never picks a subject. He answers on the morning
sheet, the one page he already opens.

## The loop

1. **Detect** — inside the nightly's `chain_finish`, before the sheet build,
   `brain interview --nightly` runs. It first APPLIES the answers found on
   consumed marks files (below), expires questions past their date, then draws
   today's questions from five signals the vault already computes:

   | shape | signal | the change an answer makes |
   |---|---|---|
   | `tension` | `dossier` tensions: a latest decision with newer sources (≤2 decisions swept a night — the only detector that needs the embedder) | an owner-review line on the decision note, or an owner-update section naming what changed |
   | `decision` | `decision_capture_scan`: a fresh source with decision language and no decision note | a new `type: decision` note in `brain/resources/`, anchored to the source (`source: [[id]]`), inheriting its tier |
   | `late` | `spine.radar` late commitments | a `completed` / `cancelled` / `rescheduled` spine event |
   | `orphan` | the linking lane's `.brain/curation/unlinked-sources.json`, with the nearest notes (hybrid search) as `link:<id>` options | a `## Sources` line on the note the owner picked; `noise` is never asked again |
   | `stale` | `index.revisit_sample` (PageRank-weighted central notes) | an owner-review line, or an owner-update section |

   Shapes are drawn round-robin so one noisy detector never crowds the others
   out. Every detector failure is a host-authored line in the result, never an
   exception — nothing here can kill the night.

2. **Phrase** — one small model leg (`interview_leg` in `tools/cos_nightly.sh`,
   sonnet, `--max-turns 4`, the pinned `MODEL_TOOLS` boundary) rewords the
   questions and option labels from a host-authored prompt
   (`$EV/interview/prompt.txt`: evidence, a 600-char excerpt of the note in
   doubt, the host wording). `tools/cos_interview_cli.py --score` parses its
   stdout off the pipe with the judgment leg's envelope reader and takes a
   rewording only when it kept every option ACTION and every `[[note-id]]`.
   The host wording stands whenever the leg fails; the sheet never waits on
   the model.

3. **Ask** — the sheet state carries `questions` (`cos-sheet-state/4`), and
   the page renders "The vault asks" right under "What your marks changed":
   one radio per option, an optional note box, and the change the answer
   makes, in words, before the owner picks. **Save my marks** writes the
   answers into the same `cos-marks-<date>.json` as `answers:
   [{key, action, note}]`, inside `content_sha256`.

4. **Capture and apply** — the 07:00 read-back files the marks as before;
   `record_marks` copies the answers onto the consumed-sheets row (exactly as
   `feedback_text` travels). The next night, `interview_apply.apply_answers`
   executes each one through `write_note` / `supersede`-grade paths
   (signed, audited), closes the question, and drops an
   `owner-interview-<date>.md` record into `inbox/` for the normal ingest, so
   the interview itself becomes a searchable `raw/` source. A failing action
   leaves the question OPEN and the sheet shows it again. The lines applied
   appear on the next sheet under "From your last answers".

## The budget — what keeps it from becoming a nag

| rule | value | where |
|---|---|---|
| new questions a day | 3 | `interview.MAX_PER_DAY` |
| open at once | 5 | `interview.MAX_OPEN` |
| a question expires after | 14 days | `interview.EXPIRE_DAYS` |
| the same evidence | never asked twice once answered | `interview.blocked` |
| a skip or an expiry silences that evidence for | 30 days | `interview.SUPPRESS_DAYS` |
| nothing answered for | 7 days → one question a day, and the sheet says so | `interview.QUIET_AFTER_DAYS` |
| one open question per target note | — | `interview.blocked` |

`skip` is every question's default and a real answer: it changes nothing.

## Where things live

- lane state: `<vault>/.brain/interview/state.json` (0600, host-only, never
  indexed) — every row ever asked with its status, the last applied lines,
  `last_answer_on`;
- the leg's evidence: `$EV/interview/{prompt.txt,leg.stderr}` (D14 row 7b), or
  `<vault>/.brain/interview/leg/` on a night with no child run (a door-closed
  night still asks: the questions come from the vault, not the mailbox);
- the answers in flight: the consumed-sheets ledger under `feedback_dir`;
- off switch: `COS_INTERVIEW=0`; model: `COS_INTERVIEW_MODEL` (sonnet);
  turns: `COS_INTERVIEW_MAX_TURNS` (4).

`brain interview` (no flags) lists the open questions; `--apply`,
`--generate` and `--date` run the halves by hand. The `/brain-grill` skill is
the manual "ask me now" fallback and reads this lane's state; it is not the
primary path.

## Stated limits

- `orphan` → `noise` records the answer and stops asking, but the source stays
  in the linking lane's population; the lane has no "owner-declared noise"
  exclusion yet.
- `tension` → `changed` writes the owner's words into the decision note; it
  does not mint a superseding decision note — that still needs a session (or
  a `decision` question on the newer source).
- The phrasing leg persists model-authored question text into the lane state
  and onto the sheet, the same standing as the judge's summaries already on
  the page.
