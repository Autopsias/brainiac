# Publish via clean-room export, not history rewrite

The local repo's git history retains the internal "Acme" codename, build-session
IDs, and hardening archaeology in every commit; scrubbing the working tree
(sessions s06–s08 of the 2026-07-03 open-release plan) cannot remove them from
history. Rather than rewriting history with filter-repo (error-prone, breaks
local archaeology), the project publishes exclusively through a **clean-room
export**: a squashed single-commit tree pushed to the public remote. This is
already wired — the `disabled-public-DO-NOT-PUSH` remote's `master` is one
commit (`4559784 "Initial public release"`) and its push URL is disabled
(`DISABLED://cleanroom-export-only-see-plan`). Consequence: local `master` is
never pushed anywhere public, and every publish-readiness check (codename grep,
session-ID grep, link check, secret scan) must run against the regenerated
export tree — a working-tree grep alone does not prove the shipped artifact is
clean.

Release-N (added by adversarial review 2026-07-03): the export is produced by
`tools/export_cleanroom.py` (deterministic include/exclude + manifest; built in
plan session s09). Subsequent releases are new squashed export commits layered
on the public branch (export → commit → tag `vN`), never a force-replace and
never a push of local history; each release gets a CHANGELOG entry and tag so
SECURITY.md's supported-versions stanza has something to reference.
