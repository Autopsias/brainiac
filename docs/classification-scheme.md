# Classification scheme + default-deny

Every note in `vault/` carries a `classification:` frontmatter field. It is the
**egress-decision input** for the `brain` engine: `search`/`get`/`recent` filter
results by classification before anything is surfaced to a model. This is the
mechanism S08 (egress gate) is built on.

## The five tiers (ordered, low → high sensitivity)

| Tier | Rank | Meaning | Egress default |
|---|---|---|---|
| `Public` | 0 | Shareable externally; no harm if leaked | surfaceable |
| `Internal` | 1 | Org-internal; routine business | surfaceable |
| `Confidential` | 2 | Limited audience; harm if leaked | surfaceable to authorised legs only |
| `Restricted` | 3 | Tightly held; significant harm | human-gated egress |
| `Secret` | 4 | Insider nonpublic information (legal/regulatory) | human-gated; default tier for unlabelled |

`Public < Internal < Confidential < Restricted < Secret`.

## Default-deny rule (load-bearing)

> **A note whose `classification:` is missing, empty, or not one of the five
> recognised values is treated as `Secret` (rank 4, the most restrictive) at every
> surfacing boundary.**

Consequences:

- An **unlabelled** note is invisible to ordinary retrieval — it is never
  surfaced to a model without an explicit human gate. Fail-closed, not
  fail-open.
- This makes **bulk migration a lifecycle prerequisite, not an afterthought**:
  an imported corpus with no `classification:` is *invisible* until classified.
  Mass-classification is therefore part of corpus migration
  (`corpus-migration.md`), not optional polish.
- The validator (`tools/validate.py`) reports every note that would be
  default-denied so the gap is visible at commit time.

## How the gate uses it

1. Caller (an LLM leg / interaction) has a **max allowed tier** for this
   execution path (set by the trifecta-break design, S08).
2. `search`/`get`/`recent` drop any result whose tier **exceeds** the caller's
   max. Unlabelled ⇒ treated as Secret ⇒ dropped for all but a human-gated path.
3. Surfacing `Restricted`/`Secret` content, and any irreversible/outbound action,
   requires **human-in-the-loop**.

## Assigning a tier

- Pick the **lowest tier that is still honest** about the harm-if-leaked.
- When unsure between two tiers, pick the **higher** (fail-closed).
- For the host/VM split (substrate-spec §4): a Cowork-VM session, being
  EDR-blind, should be capped at a **lower** max tier than a host session by
  policy — but that policy is enforced at the gate (S08), not in this scheme.

## Relationship to the at-rest posture

Classification drives **egress** (what is surfaced). It is *separate* from
at-rest encryption, which is FDE-baseline + conditional (substrate-spec §6).
A `Restricted`/`Secret` tier is one of the flip-list triggers for conditional
app-encryption when a regulated regime applies — but classification's primary
job is the **surfacing gate**, not disk encryption.
