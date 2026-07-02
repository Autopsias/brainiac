# Paste-ready Cowork registrar prompt — brain on-invoke VM tasks (s07 / TSK-04)

Paste this entire block into a Cowork chat session that has the
scheduled-tasks MCP tools available (`list_scheduled_tasks`,
`create_scheduled_task`, `update_scheduled_task`). It registers
3 POKE-ONLY triggers — none of them auto-fire on a cron, so
this paste never increases the Cowork/VM OS-scheduled-task count above the
locked **0** (docs/cutover/persistence-budget.md). They exist purely so the
analyst can re-fire a named, idempotently-registered prompt instead of
retyping it.

Before registering anything, confirm `BRAIN_VAULT` (or the projected
`brain --vault` path the Cowork sandbox uses) is set in the trigger's own
command lines, not assumed from the session environment — Cowork sessions are
ephemeral and do not inherit a persistent shell profile.

For EACH of the 3 tasks below, run the 5-step idempotent
sequence (list -> create-if-absent / update-if-present -> never delete):

### `brain-promotion-scan` — promotion-scan

**Manifest source:** routines/manifest.json id `promotion-scan` (disposition: ON-INVOKE, cadence: on-invoke (Cowork, surfaced at bootstrap or fired manually -- never OS-scheduled, per the locked VM=0 budget))

Idempotent registration steps for this trigger:
1. Call `list_scheduled_tasks` and search for a task/trigger named `brain-promotion-scan`.
2. If ABSENT: call `create_scheduled_task` with name=`brain-promotion-scan`. Do **NOT**
   set a cron/schedule expression — leave it poke-only / fire-on-demand. The
   locked persistence budget (docs/cutover/persistence-budget.md THE LOCK) caps
   Cowork/VM OS-scheduled-task count at exactly **0**; this trigger exists only
   so the prompt below can be re-fired by name instead of retyped.
3. If PRESENT: call `update_scheduled_task` on the existing entry to adopt /
   refresh its prompt body to the block below (do not create a duplicate).
4. **Never call `delete_scheduled_task`.** To retire this trigger later, call
   `update_scheduled_task(enabled=false)` instead.
5. **#29022 caveat:** `create_scheduled_task` is sometimes not injected by the
   MCP layer (silently no-ops). After step 2, re-run `list_scheduled_tasks`
   and confirm `brain-promotion-scan` now appears. If it does not, fall back to the
   Cowork Schedule UI and register the same prompt body manually.

**Trigger prompt body (what `brain-promotion-scan` runs when manually fired):**

```
brain bases-query --where status=candidate --max-tier Internal --json   # find candidates (VM-allowed, no index-open) 
brain search "<topic>" --max-tier Internal --json                         # corroborate 
brain draft-capture --content "<proposal>"                                # stage an unsigned DRAFT, drained+signed by the next host `brain sync`
```

PF-02 export-egress gate (docs/cutover/export-egress-gate.md) — already satisfied
by this block: every `brain` call above carries `--max-tier Internal`; no
personal names appear (role-title only); this is a read/draft operation, not a
`brain project`-style export, so no `brain snapshot` step is required before it
— but if you extend this trigger to ship results outside the Cowork session
(an email, a doc, a paste to another tool), run `brain snapshot` first and
record the gate evidence per export-egress-gate.md Step D before doing so.

### `brain-autoresearch-cascade` — autoresearch-cascade

**Manifest source:** routines/manifest.json id `autoresearch-cascade` (disposition: ON-INVOKE, cadence: on-invoke (host or Cowork, on demand -- never OS-scheduled))

Idempotent registration steps for this trigger:
1. Call `list_scheduled_tasks` and search for a task/trigger named `brain-autoresearch-cascade`.
2. If ABSENT: call `create_scheduled_task` with name=`brain-autoresearch-cascade`. Do **NOT**
   set a cron/schedule expression — leave it poke-only / fire-on-demand. The
   locked persistence budget (docs/cutover/persistence-budget.md THE LOCK) caps
   Cowork/VM OS-scheduled-task count at exactly **0**; this trigger exists only
   so the prompt below can be re-fired by name instead of retyped.
3. If PRESENT: call `update_scheduled_task` on the existing entry to adopt /
   refresh its prompt body to the block below (do not create a duplicate).
4. **Never call `delete_scheduled_task`.** To retire this trigger later, call
   `update_scheduled_task(enabled=false)` instead.
5. **#29022 caveat:** `create_scheduled_task` is sometimes not injected by the
   MCP layer (silently no-ops). After step 2, re-run `list_scheduled_tasks`
   and confirm `brain-autoresearch-cascade` now appears. If it does not, fall back to the
   Cowork Schedule UI and register the same prompt body manually.

**Trigger prompt body (what `brain-autoresearch-cascade` runs when manually fired):**

```
brain search "<query>" --max-tier Internal --json
brain hybrid-search "<query>" --rerank --max-tier Internal --json
brain draft-capture --content "<research note>"
```

PF-02 export-egress gate (docs/cutover/export-egress-gate.md) — already satisfied
by this block: every `brain` call above carries `--max-tier Internal`; no
personal names appear (role-title only); this is a read/draft operation, not a
`brain project`-style export, so no `brain snapshot` step is required before it
— but if you extend this trigger to ship results outside the Cowork session
(an email, a doc, a paste to another tool), run `brain snapshot` first and
record the gate evidence per export-egress-gate.md Step D before doing so.

### `brain-ingestion-digest-weekly` — ingestion-digest-weekly

**Manifest source:** routines/manifest.json id `ingestion-digest-weekly` (disposition: FOLD, cadence: Sunday branch of brain-nightly for the scheduled emission (folded, host, zero extra OS entries); on-demand VM form is on-invoke)

Idempotent registration steps for this trigger:
1. Call `list_scheduled_tasks` and search for a task/trigger named `brain-ingestion-digest-weekly`.
2. If ABSENT: call `create_scheduled_task` with name=`brain-ingestion-digest-weekly`. Do **NOT**
   set a cron/schedule expression — leave it poke-only / fire-on-demand. The
   locked persistence budget (docs/cutover/persistence-budget.md THE LOCK) caps
   Cowork/VM OS-scheduled-task count at exactly **0**; this trigger exists only
   so the prompt below can be re-fired by name instead of retyped.
3. If PRESENT: call `update_scheduled_task` on the existing entry to adopt /
   refresh its prompt body to the block below (do not create a duplicate).
4. **Never call `delete_scheduled_task`.** To retire this trigger later, call
   `update_scheduled_task(enabled=false)` instead.
5. **#29022 caveat:** `create_scheduled_task` is sometimes not injected by the
   MCP layer (silently no-ops). After step 2, re-run `list_scheduled_tasks`
   and confirm `brain-ingestion-digest-weekly` now appears. If it does not, fall back to the
   Cowork Schedule UI and register the same prompt body manually.

**Trigger prompt body (what `brain-ingestion-digest-weekly` runs when manually fired):**

```
brain digest --days 7 --json
```

PF-02 export-egress gate (docs/cutover/export-egress-gate.md) — already satisfied
by this block: every `brain` call above carries `--max-tier Internal`; no
personal names appear (role-title only); this is a read/draft operation, not a
`brain project`-style export, so no `brain snapshot` step is required before it
— but if you extend this trigger to ship results outside the Cowork session
(an email, a doc, a paste to another tool), run `brain snapshot` first and
record the gate evidence per export-egress-gate.md Step D before doing so.

---
**Summary you should report back after running this:** which of the
3 triggers were CREATED vs UPDATED (adopted), and whether the
#29022 verify-after-create check passed for each, or required the Schedule UI
fallback.
