# RACI · ownership · support model (val-04 — bus-factor & operability)

**Session:** S07 · **Date:** 2026-06-27
**Why this exists (HARDENED:r2-claude):** a fleet product with **bus-factor 1 is
not operable**. Every human checkpoint, the daily drain/brief floor, the post-ship
lifecycle, and user support each need a **named second operator** beyond maintainer.
val-04 requires this ownership + support model to **EXIST** before fleet rollout.

> **Owner placeholders.** Where the real individual is not knowable to the build
> agent, the cell is **`TBD — maintainer to assign`**. These are clearly-marked
> placeholders, **not invented names** — maintainer substitutes real people at the
> before-fleet checkpoint. Role labels (Acme EUC, Acme Cyber) follow the vault
> comms-policy anonymisation.

## RACI — build, ship, operate

Legend: **R** responsible · **A** accountable · **C** consulted · **I** informed.

| Activity | maintainer (CTO) | 2nd operator (TBD) | Acme EUC (Intune) | Acme Cyber (CISO) | Post-ship owner (TBD) |
|---|:--:|:--:|:--:|:--:|:--:|
| Build / package (PKG-01/02) | A | R | C | I | I |
| Azure Trusted Signing onboarding (PW-2) | A | C | R | C | I |
| WDAC Managed-Installer policy (PW-3) | A | I | R | C | I |
| Intune test device + deploy (PW-4) | A | C | R | I | I |
| Cyber review / CSF sign-off (PW-1) | A | I | C | R | I |
| **S10 real-eval gate** decision | A | R | I | C | I |
| **val-01** clean-install acceptance | A | R | R | C | I |
| Daily drain/brief floor (ux-02, S09) run health | I | A/R | C | I | R |
| Brain/index/model **lifecycle** (re-index, model bumps, re-sign) | C | C | C | C | **A/R** |
| User support (triage, how-to, breakage) | I | C | C | I | **A/R** |
| Substrate-abort decision (stay on Obsidian+SC) | **A** | C | I | C | I |

## Human checkpoints — each needs TWO people

| Checkpoint | Primary | Backup (bus-factor fix) |
|---|---|---|
| Before-fleet go/no-go | maintainer | 2nd operator (TBD) |
| S10 real-eval verdict | maintainer | 2nd operator (TBD) |
| Sign + release approval | maintainer | Acme Cyber (TBD named) |
| Production incident / quarantine | Post-ship owner (TBD) | Acme EUC on-call |

## Post-ship lifecycle owner (TBD — maintainer to assign)

Owns, after go-live:
- **Re-index / corpus migration** when the note schema changes (`migrate_corpus.py`).
- **Model lifecycle** — Arctic-embed version bumps require a re-embed + an S05
  non-inferiority eval before shipping a new model cache; never bump silently.
- **Re-sign + re-package** on each release (Azure Trusted Signing renewal,
  notarization re-submit).
- **Snapshot/anchor health** — the off-host chain anchor + backup restore test
  (S08 SEC-03) stay green.

## User-support route (TBD — maintainer to assign)

- **Tier 1:** a named support contact / channel (TBD) for "how do I…" + breakage.
- **Self-serve:** `brain --help`, `docs/cowork-windows-install.md`, this guide set.
- **Escalation:** Tier 1 → post-ship owner → maintainer. Captured so a user is never
  stuck on bus-factor 1.

## Drain/brief floor — explicit second operator

The **ux-02 morning brief (S09)** is the guaranteed daily drain floor. If maintainer
is unavailable, the **2nd operator (TBD)** is accountable for confirming it ran
(or running the host `brain sync --publish` manually). This prevents the
"captured-but-never-committed draft" failure mode from depending on one person.
