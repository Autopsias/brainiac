# Synthetic named-entity fixture

**named-entity-golden.json** and **named-entity-qrels.json** are the frozen,
publish-safe S01 evaluation family. Their documents live under
**tests/fixtures/named_entity_vault/**; every name is a Contoso, Northwind, or
Fabrikam placeholder.

They are intentionally separate from **eval/golden_set.json** and
**eval/qrels/**, which are ignored owner-vault runtime artifacts. Do not merge
owner terms into this fixture. S02 combines this family with any local private
baseline only in an uncommitted capture workspace, and checks
**eval/runs/ne-family-freeze.json** before and after that work.

## Synthetic COS capture corpus (REP-03)

**cos-corpus-synthetic.jsonl** is a hand-written capture corpus in the exact
shape `brain.cos_corpus` writes (`CORPUS_SCHEMA` rows + one close record) -
built by `build_cos_corpus_synthetic.py` through the real
`append_thread`/`close_run` writer, not a hand-built lookalike, so it reads
back through `cos_corpus.read_corpus`/`corpus_status` with no special-casing.
It exists so the offline replay harness (REP-01) and the empty-body refusal
guard (WIR-02) can be built and tested with no mailbox anywhere in the loop.

**cos-corpus-synthetic-verdicts.json** is the sibling correctness check: one
expected verdict (`candidate` / `not_candidate` / `refuse`) and a one-line
`why` per `conversation_id`, keyed the same way a corpus row joins back to a
ledger. It is a SEPARATE file rather than an extra field on the corpus row:
`CORPUS_SCHEMA` deliberately carries no verdict (the corpus is evidence of
what a run read, never a judgment about it), so a harness that wants ground
truth reads this file, keyed on the row's own `conversation_id`.

Seven rows cover: one clear candidate (a contract renewal with a rate,
date, and action), three clear non-candidates (newsletter, scheduling
chatter, an automated notification), one row with an EMPTY body
(`body_opened: false` - the WIR-02 negative control: the judge must REFUSE
this row, never verdict it), one unusually long thread (23 replies, ~6.7k
chars) to exercise the extraction window, and one row whose subject reads as
urgent but whose body has no substance (the case a subject-only judge gets
wrong). Every name is a Contoso/Northwind/invented-person placeholder - never
a real counterparty, project codename, or person. Regenerate with
`python3 eval/fixtures/build_cos_corpus_synthetic.py`.

## cos-doctrine-v5.40-ext3.md

The Phase 1.6 doctrine as it shipped **before v5.42 (EXT-06)**, copied
verbatim out of `.claude/skills/chief-of-staff/SKILL.md` at git `d3032c6`
(`kernel_version: chief-of-staff v5.40`, `extraction_rules_version: ext-3`) —
the "before" side of S09/MEA-02's lift measurement, so
`eval/configs/cos-replay-ext3-700.json` can point a replay's `doctrine_path`
at the old text and `eval/configs/cos-replay-ext4-current.json` at the shipped
one. Not a synthetic fixture and not editable: it is EVIDENCE, and an edit
would silently turn that comparison into a comparison with something else, so
`tests/test_cos_replay.py` pins the extracted section's sha256
(`ababeb97…`) plus the two markers that must be ABSENT from it
(`BODY_EXTRACT_BUDGET`, `THE PRIORITY INVARIANT` — both v5.42 additions).
Regenerate only from git, never by hand.
