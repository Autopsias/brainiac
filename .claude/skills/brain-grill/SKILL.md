---
name: brain-grill
description: "Asks the owner, now and in this session, the questions the vault's interview lane has open — a decision with newer sources against it, a source that reads like a decision, a commitment past its date, a source nothing cites, a central note gone stale — and applies each answer at once through `brain interview --answer`. The nightly asks the same questions on the morning sheet; this is the manual \"ask me now\" fallback, host-only. Use when the user says \"grill me about the vault\", \"interview me\", \"what does the brain not know\", \"brain grill\", or types `/brain-grill`. Not for plans or designs: that is `/grill-me`."
disable-model-invocation: true
---

# /brain-grill — ask me now

Host-only: `brain interview` reads `.brain/interview/state.json` and writes
notes through `write_note`, both host surfaces. On a Cowork VM leg, say so
and stop. `$BRAIN_VAULT` is the vault.

The questions come from the interview lane (`docs/cos-interview.md`), never
from this skill: every one cites evidence the folds computed, cannot be
answered from the vault, and names the note the answer changes. The lane's
budget still applies — at most 3 new questions a day, 5 open, the same
evidence never twice, a skip silences it for 30 days.

## Checklist

- [ ] 1 List: `brain --vault "$BRAIN_VAULT" interview --json` → `open`
- [ ] 2 If `open` is empty: `brain --vault "$BRAIN_VAULT" interview --generate --json` (up to a minute; the two dossier sweeps need the embedder), then list again. Still empty means the lane has nothing to ask today — say so and stop.
- [ ] 3 Ask one question per turn with `AskUserQuestion`
- [ ] 4 Record each answer at once with `--answer`
- [ ] 5 Report the applied lines and the record id

## 3 · Ask

For each open row, one `AskUserQuestion`:

- header: the shape word (Tension, Decision, Late, Orphan, Stale)
- question: the row's `question`, then one line `Evidence: <id> (<date>), …`
- options: the row's option `label`s in order; the automatic "Other" is
  where a note goes (a date for `reschedule`, what changed for `changed` or
  `outdated`)

Record the owner's words verbatim; never paraphrase a note.

## 4 · Record

```bash
brain --vault "$BRAIN_VAULT" interview --answer <key> --action <action> --note "<the owner's words, or empty>" --json
```

`<action>` is the option's `action` code, never its label. The command
applies the change the row's `change` line promised — an owner-review line,
an owner-update section, a new decision note, a Sources line, a commitment
event — closes the question, and drops `owner-interview-<date>.md` into
`inbox/` for the normal ingest. Read the JSON: `applied`, `skipped`, `failed`
(a failed action leaves the question open and says why), `record`
(`ingested`/`quarantined` counts).

## 5 · Report

One table: `# | shape | note | applied line`. Then the record's ingest
outcome. The next morning sheet shows the same lines under "From your last
answers".

## Gotchas

- `--action reschedule` needs a `YYYY-MM-DD` in the note; without one the
  command refuses and the question stays open (measured 2026-09-09).
- Do not build questions of your own here: a question this lane did not
  draw has no key, no evidence hash and no applier, and the sheet will never
  know it was asked.
