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
| `MNPI` | 4 | Material non-public information (legal/regulatory) | human-gated; default tier for unlabelled |

`Public < Internal < Confidential < Restricted < MNPI`.

## Default-deny rule (load-bearing)

> **A note whose `classification:` is missing, empty, or not one of the five
> recognised values is treated as `MNPI` (rank 4, the most restrictive) at every
> surfacing boundary.**

Consequences:

- An **unlabelled** note ranks as MNPI, the most-restrictive tier. On any
  *capped* surface (the VM leg's `Internal` default, an explicit
  `--max-tier`, MCP's ceiling) it is therefore invisible — fail-closed, not
  fail-open. Note the trusted-host default is the **full vault** (owner
  decision 2026-07-10), so on an uncapped host read an unlabelled note is
  *surfaced* (as MNPI-ranked), not hidden — see
  `docs/security-overview.html` §2.1.
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
   max. Unlabelled ⇒ treated as MNPI ⇒ dropped on any capped surface
   (surfaced only where the cap is the full vault, i.e. the trusted-host
   default).
3. Surfacing `Restricted`/`MNPI` content, and any irreversible/outbound action,
   requires **human-in-the-loop**.

## Assigning a tier

- Pick the **lowest tier that is still honest** about the harm-if-leaked.
- When unsure between two tiers, pick the **higher** (fail-closed).
- For the host/VM split (substrate-spec §4): a Cowork-VM session, being
  EDR-blind, should be capped at a **lower** max tier than a host session by
  policy — but that policy is enforced at the gate (S08), not in this scheme.

## OPEN: the same document indexed twice at two different tiers (2026-08-10)

**Status: measured, undecided. No owner ruling exists.** Found by
`_plans/anylang-query-variants-2026-08-09` s02 while measuring something else;
confirmed by that plan's acceptance review. Out of scope for that plan, recorded
here because the finding is about this scheme, not about retrieval.

On the reference deployment, of 282 `<ingest-date>-<slug>` / `<slug>` name-twin
pairs, **201 carry the date-prefixed copy at a LOWER classification than the
plain copy** — 133 `Internal`-vs-`Restricted`, 49 `Internal`-vs-`Confidential`,
19 `Internal`-vs-`MNPI`. On a random 8-pair sample the normalized bodies are
**88–99% similar** at 12–57 KB.

**What that means, precisely.** The gate is behaving correctly: it applies each
note's own label. The defect is upstream — two notes carrying nearly the same
content disagree about the harm-if-leaked. The consequence is that a reader
capped at `Internal` can read the substance of a `Restricted` document through
its twin, without the gate ever being wrong about a single note.

This is an **ingest consistency** question, not an egress one. It also
independently rules out any ranking-time merge of *name-similar* documents:
collapsing such a pair would assert an identity the tiers themselves contradict
(which is why the shipped family collapse requires byte-identity plus an
owner-accepted supersession link, `src/brain/index/`).

**Before acting, re-measure** — the counts above are a dated snapshot. The shape
of the decision is: which copy carries the honest tier, and does ingest re-label
the twin, refuse it, or link it. Nothing is in flight.

## Relationship to the at-rest posture

Classification drives **egress** (what is surfaced). It is *separate* from
at-rest encryption, which is FDE-baseline (substrate-spec §6). The optional
app-layer encryption module currently protects **backups only**
(`brain backup`), not the live index/vault/audit chain — see
`docs/security-overview.html` §6.8. Classification's primary job is the
**surfacing gate**, not disk encryption.
