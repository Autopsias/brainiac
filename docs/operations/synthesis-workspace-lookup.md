# One `--workspace`, two different concepts

**Status:** open defect, worked around. Recorded 2026-09-08 so the workaround
is not removed as redundant, and so the next person to touch
`provision-local` knows what they are holding.

## What happened

The weekly synthesis (`scripts/brain-synthesis.sh`) skipped every registered
vault from 2026-08-31 to 2026-09-07. It logged one line per vault and reported
the run complete:

    2026-09-06 08:00:01 SKIP /Users/…/<vault name>/vault: no kb-curator skill
      in /Users/…/CoworkWorkspaces/<vault name>
    2026-09-06 08:00:01 synthesis run complete

Nothing failed. Nothing alerted on the synthesis itself. The finding arrived
two weeks later, from a different surface: `invariant:unlinked_sources` on one of the registered vaults. BAK-04's linking lane writes a worklist
(`.brain/curation/unlinked-sources.json`) that only the weekly synthesis
session consumes, so a synthesis that never runs shows up as a link lane that
never drains.

## The cause

`brain provision-local <vault> --workspace <dir>` writes that ONE value into
BOTH registry rows — `provision_wire.py:386` (`target="host"`) and
`provision_wire.py:390` (`target="cowork-vm"`) call `_wire_registry` with the
same `workspace` argument. But the two rows mean different things:

| Row | What its `workspace_path` means |
|---|---|
| `host` | the directory the HOST session runs in. It holds an unpacked `.claude/skills/` (or `.agents/skills/`) tree. |
| `cowork-vm` | the COWORK workspace. A Cowork workspace never holds an unpacked skills directory — its skills ship as `.skill` zips into `<vault>/.brain/skills/` and go through Cowork's own Save-skill upload flow. |

Point one field at a Cowork workspace and the host loses its skill directory.
The synthesis script looked only at the host row's `workspace_path`, found no
`kb-curator` there, and skipped.

## Why the registry was not simply re-pointed

Re-pointing the host row to the real host workspace looks like the smaller
fix. It is not, for two reasons, both checked in the code:

1. `doctor_wiring._workspace_for` (`src/brain/doctor_wiring.py:75`) reads the
   HOST row's `workspace_path` and checks wires 2, 5 and 6 against it —
   including the Cowork staging marker. Re-point the host row and `brain
   doctor` reports wire 2 missing on a vault that is correctly staged.
2. `workspaces.upsert_entry`'s upsert key includes `workspace_path`, so an
   upsert with a different path ADDS a second host row rather than updating
   the existing one.

`brain provision-local --workspace <host dir>` was rejected for the same
reason as (1): it writes the new value into the `cowork-vm` row too.

## The workaround that shipped

`scripts/brain-synthesis.sh` (and its `src/brain/_assets/` mirror) now looks
for the skill in the registered workspace FIRST, then beside the vault
(`dirname "$VAULT"`), and skips only when neither has it. A vault with no
`kb-curator` skill anywhere still skips, so a genuine absence never becomes a
run. Checked by `tests/test_synthesis_skill_lookup.py`, three cases.

## The real fix, when someone takes it

Give the host row its own field. Either a second parameter
(`provision-local --host-workspace <dir>` beside `--workspace`), or a
`host_workspace_path` key that falls back to `workspace_path` when absent.
Then `doctor_wiring._workspace_for` keeps reading the Cowork value it needs,
and the host session gets the directory it actually runs in.

Until then: two concepts share one field, and the synthesis script's fallback
is what keeps the weekly session alive.
