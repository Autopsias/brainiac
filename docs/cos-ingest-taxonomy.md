# The ingest/no-ingest category taxonomy (TAX-01)

**What this is.** The owner-facing vocabulary that decides *what kind of thing*
a chief-of-staff ingestion candidate is, and therefore whether the run keeps
it automatically, proposes it for review, or never manufactures a candidate
from it at all.

**What this is not.** It is not a second evidence rule, not a second
classification scheme, and not a filter on what the run may *read*. Phase 1.6
scope (`act` threads + `read` threads at P0/P1) is unchanged; this taxonomy
runs *inside* that scope, after extraction, on candidates that already have a
quote.

Its home is `overlay/cos/ingest.md` (schema + absent-semantics:
`overlay/template/cos/ingest.md`, documented in `overlay/README.md`). The
kernel carries the MECHANISM; the categories an owner actually recognises are
overlay DATA, exactly like the keyword→tier table in Phase 1.6 rule 4.

---

## 1 · The three dispositions

| Disposition | Meaning | Effect on Phase 1.6 |
|---|---|---|
| `always` | This kind of content is worth durable memory whenever it appears. | The candidate is **auto-ELIGIBLE**: it skips the "is this worth proposing?" judgement and goes straight to the batch (or, where ING-04 auto-capture has graduated this pattern, straight to capture under its undo window). |
| `propose` | Sometimes worth keeping, sometimes noise — the owner decides. | Normal Phase 1.6 behaviour: staged via `cos-propose`, decided in the owner's batch. **This is the fail-closed default** for anything unrecognised. |
| `never` | Content of this kind is structurally not durable memory. | **ZERO candidates.** Not staged, not proposed, not queued for tomorrow. The thread still appears in the ingestion ledger (rule 8) with `disposition: "no-substance"` so the phase's proof-of-work is complete — silence is still a FAIL, a categorised skip is not. |

### The two hard rules

1. **`never` produces zero candidates — not even proposed ones.** A `never`
   category is not "propose with a low prior"; it is an instruction that no
   `cos-propose` drop is written. The only trace is the ledger row.

2. **`always` means auto-eligible, NEVER evidence-exempt.** Phase 1.6 rule 2
   stands untouched: *no source quote ⇒ no candidate*. An `always` category
   with no quotable span produces a `held` ledger row exactly as before. The
   taxonomy can raise a candidate's standing; it can never invent one.

---

## 2 · Classification interaction — a category may raise a tier, never lower it

Phase 1.6 rule 4 is the authority on classification: **most-restrictive
default** (`MNPI`) unless an explicit `overlay/keywords/` entry maps the
topic/counterparty to a named lower tier.

The keyword mapping is matched **only against host-verified text** — subject
and sender the host's own `.eml` handler parsed out of the archived original
(`provenance.verified`). A subject/sender/filename/body a VM authored is a
CLAIM, never a lowering input, or a producer could plant a mapped term in its
own `provenance.subject` to reach a lower, already-graduated evidence key. A
proposal drop and a swept attachment therefore stay at `MNPI`.

A category may declare an OPTIONAL `min_tier` — a **floor**, resolved as:

```
effective_tier = max(verified_keywords_mapping_or_MNPI, category_min_tier)
```

`max` over `Public < Internal < Confidential < Restricted < MNPI`. So:

- A category **never** overrides the keywords mapping **downward**. A category
  marked `min_tier=Internal` on a thread the keyword table puts at
  `Restricted` still ships `Restricted`.
- A category with no `min_tier` changes nothing at all.
- The floor exists for the one direction that is safe: content whose *kind*
  is inherently sensitive (a contract version, a board pack) can be pinned
  above whatever a generic keyword rule would have allowed.

---

## 3 · Lanes

Ingestion has two independent evidence lanes, and a category applies to one or
both:

- `text` — substance extracted from the message body (Phase 1.6, quote-gated,
  bound by the read-state invariant and lane-portable evidence access).
- `attachment` — a file that reaches `vault/inbox/` via the attachment lane and
  is ingested as an immutable `raw/` source.
- `both` — the category is meaningful on either lane.

A category scoped to one lane is simply not consulted on the other; the other
lane's default (`propose`) applies.

---

## 4 · The categories

Twelve categories. The `id` is the stable identifier used in
`overlay/cos/ingest.md` — see §6 for what a rename costs.

| id | Disposition | Lane | `min_tier` | What it is |
|---|---|---|---|---|
| `contract-version` | `always` | both | `Confidential` | An executed, redlined, or circulated version of a binding document. *Examples:* a counter-signed supply agreement PDF; an email carrying "attached is v7 with our markup on clause 11". |
| `governance-material` | `always` | both | `Confidential` | Material produced for or by a governing body. *Examples:* a board pack for next week's meeting; oversight-committee minutes recording an approval. |
| `decision-record` | `always` | text | — | A decision that has actually been TAKEN and communicated, not a proposal. *Examples:* "we're going with option B, effective Monday"; "approved — proceed on the revised scope". Maps to the SKILL's `kind: decision`. |
| `commitment` | `always` | text | — | Someone binds themselves (or the owner) to deliver something. *Examples:* "I'll have the redline back to you by Thursday"; "we will hold the price through Q3". Maps to `kind: commitment`, carries `direction`/`counterparty`/`topic` for the SP-01 spine. |
| `key-number` | `always` | both | — | A figure, date, or amount that will be quoted back later. *Examples:* "the cap is 4.2 % of contract value"; "long-stop date is 30 November". Maps to `kind: number`. |
| `regulatory-filing` | `always` | attachment | `Confidential` | A submission to, or determination from, a regulator or authority. *Examples:* a filed response to a market-authority consultation; a tax determination letter. |
| `counterparty-position` | `propose` | text | — | A stated position in a live negotiation — real substance, but volatile, and a position stated is not a position held. *Examples:* "our board won't go below the indexed floor"; "we can't accept joint liability on this". Maps to `kind: position`. |
| `working-draft` | `propose` | both | — | Material explicitly under consideration: memos, option decks, scenario papers. **These describe POSITIONS, never decisions** (AGENTS.md §5 — the decision layer is authoritative in both directions). *Examples:* a "three options for the perimeter" memo; a draft strategy deck marked v0.4. |
| `internal-coordination` | `propose` | text | — | Ordinary internal working traffic that *occasionally* contains a real commitment or number. *Examples:* "who's covering the client call on the 14th?"; a status round-up thread that happens to state a slipped date. |
| `market-digest` | `never` | both | — | Third-party editorial and market summaries. *Examples:* a subscription industry newsletter; a bank's daily commodity note. Public context, freely re-obtainable, not this owner's memory. |
| `system-notification` | `never` | both | — | Machine-generated portal, workflow, and system traffic. *Examples:* an e-signature platform's "document completed" ping; a ticketing system's status-change digest. **Note:** the *attachment* such a notification announces is categorised on its own merits (usually `contract-version`) — the notification email is what is never ingested. |
| `scheduling-logistics` | `never` | text | — | Arranging the meeting, not the meeting's content. *Examples:* "can we move to 15:00?"; travel and room-booking confirmations. |

### Where these came from

The SKILL's four extraction classes (`decision`, `commitment`, `position`,
`number`) map 1:1 onto `decision-record`, `commitment`,
`counterparty-position`, `key-number`. The remaining eight are the
document/traffic kinds those classes arrive inside, plus the three structural
`never` classes.

> **Evidence gap (recorded, not papered over).** The S01 field audit
> (`_evidence/s01/funnel-report.md`) measured the funnel *stage by stage* —
> runs fired, chips assigned, candidates staged, drops written — but it did
> **not** measure a content-category distribution over the mailbox (Phase 1.6
> was staging zero candidates, so there was nothing to distribute). This
> taxonomy is therefore derived from the SKILL's extraction classes and the
> document kinds the audit names in passing, **not** from a measured category
> histogram. The first live runs with `ingest.md` present produce that
> histogram via the ingestion ledger; expect to re-tune dispositions once it
> exists.

---

## 5 · Absent, unparseable, invalid — the strict convention

This file is a **security-relevant gate** (it can suppress candidates
entirely), so its failure semantics are strict and deliberately different from
`auto-archive.md`:

| State of `overlay/cos/ingest.md` | Behaviour |
|---|---|
| **ABSENT** | The whole category feature is **OFF**. No category stamping, no engine refusal, no defect. Phase 1.6 behaves exactly as it did before this feature existed. |
| **UNPARSEABLE** (frontmatter or body won't parse) | **Fail CLOSED: every candidate is treated as `propose`.** Plus a logged defect. Never silent-off, never `always`, never `never`. |
| **A single rule INVALID** (unknown disposition, malformed line) | That rule resolves to `propose`, with a warning. The rest of the file still applies. |

`auto-archive.md`'s keys deliberately do the opposite (`aged_read_lane` ABSENT
⇒ **true**, because the owner explicitly ruled that lane on;
`any_sender_lane` ABSENT ⇒ OFF). The divergence is documented at
`.claude/skills/chief-of-staff/SKILL.md` §"Phase 1.5b … Explicit opt-in key,
ABSENT ⇒ OFF (deliberate reversal of the roster lane's convention)" — each
key's default is argued on its own blast radius, and there is no
overlay-wide default convention to inherit.

---

## 6 · Lifecycle — ids are stable identifiers

- **A category id is a STABLE identifier.** Auto-capture graduation (ING-04)
  accumulates acceptance evidence keyed on the pattern a candidate carries; a
  category id is part of that key.
- **A rename is a NEW id, and its evidence resets by design.** Renaming
  `working-draft` to `draft-material` does not carry the old id's acceptance
  history across — it starts at zero, exactly as a fresh `kernel_version`
  does. This is deliberate: an id whose meaning the owner just changed has not
  earned the old id's track record.
- **Removing a category, or flipping it to `never`, demotes any graduation
  immediately at the next engine load.** No grace period, no drain of
  in-flight auto-capture for that category. The conservative direction is
  always instant.

---

## 7 · Cross-references

- `overlay/template/cos/ingest.md` — the placeholder-only kernel template (the
  schema in executable form)
- `overlay/README.md` §"The `cos/` category" — where the overlay files live and
  which one the skill reads for what
- `.claude/skills/chief-of-staff/SKILL.md` Phase 1.6 — scope, evidence rule,
  classification mechanism, the ingestion ledger
- `AGENTS.md` §5 — the decision layer vs. raw sources (why `working-draft` is
  `propose` and never `always`)
- `src/brain/overlay.py` — the machine shape-check (`brain init
  --validate-overlay`)
- `docs/cos-ops.md` §6c — the ENGINE side of §5 and §6: how a category is
  host-bound at claim time, what graduation is keyed on, and what demotes it
