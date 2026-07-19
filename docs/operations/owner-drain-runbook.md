# Owner drain runbook — chief-of-staff inbox-zero (one page)

*Plain language. Twice a day, about 5 minutes each time. Written for the
owner, not for an engineer.*

**Dated transitional note (2026-07-19):** prioritization (the chips below)
is NEW — the chip system only went live 2026-07-18. Expect some backlog
noise this first careful week while chips catch up on older mail and you
clear what's been sitting. Don't expect a spotless inbox on day one; expect
it to visibly tighten week over week.

---

## The one rule

**If it needs you, it has a chip. If it has no chip, you don't need to
look — for mail the nightly run has already processed.**

That last clause matters — see Part 2 below. The rule is not "the inbox is
always fully triaged"; it's "everything the nightly has already seen and
decided is action-worthy is chipped, and nothing else needs your eyes."

## The drain is TWO parts, every time

### Part 1 — empty the chips, in order: P0 → P1 → P2

| Chip | Color | Meaning |
|---|---|---|
| `P0 · Now` | red | Needs you today, urgently — usually a direct ask with a deadline from a key person. |
| `P1 · Today` | orange | Needs you today — a direct ask on you, not necessarily urgent-urgent. |
| `P2 · This week` | blue | Everything else that needs action but isn't time-critical. |

Work red first, then orange, then blue. A chip clears itself automatically
once you've replied and there's nothing open left in that thread (the
nightly re-checks this every run) — you don't have to un-chip anything by
hand. If a chip is still there, something is still open.

**Max chip latency: one nightly run, plus whatever the cron happens to
drift that day.** The chips you see this morning reflect mail as of last
night's run — not the literal present moment. A message that arrived five
minutes before you opened your inbox has not been triaged yet; that's Part 2.

### Part 2 — skim today's unprocessed arrivals at the top of the Inbox

Anything that landed **after** last night's run has no chip yet — not
because it was judged unimportant, but because nobody has looked at it. Give
the top of the Inbox (newest-first) a quick skim each drain. This is the
only part of the drain where "no chip" does NOT mean "safe to ignore" — it
means "not yet seen."

## What to do when something looks wrong

- **A chip you don't understand** — read the thread; if it's genuinely not
  action-worthy, leave it, it will re-level or clear on its own within a
  few nights (the lifecycle reconciliation runs every night). Don't hand-edit
  Outlook categories — the nightly owns that field and a hand edit will be
  overwritten.
- **Something urgent has NO chip and you just noticed it in Part 2** — handle
  it directly; nothing about the drain stops you from acting on mail
  yourself. The chip system augments you, it doesn't gate you.
- **A chip cleared itself and you're not actually done with it** — reply or
  flag again; the system watches for exactly this ("clear-quality
  contradiction") and will re-surface it in the morning brief if you follow
  up within 3 days of the clear.
- **Chips pile up faster than you can clear them** — the weekly Sunday
  review now watches for this directly (stale-chip digest, chips older than
  14 days; and a drain-vs-add check over any 2-week window) and will queue
  you ONE actionable question rather than leaving it to rot silently. You
  don't have to notice this yourself.
- **Anything archived automatically that you didn't expect** — every
  auto-archived row is undo-capable and named in the morning brief's
  overnight ledger; nothing is deleted, only moved to the Archive folder.

## Where this fits

Full standing-system record (what's live, what's shadow, every knob, how to
revert each piece): [`INBOX-ZERO-CLOSEOUT.md`](../../INBOX-ZERO-CLOSEOUT.md).
