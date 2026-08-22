"""Register installation health commands."""

from __future__ import annotations


def _add_init(sub) -> None:
    sp = sub.add_parser(
        "init",
        help="first-run setup: --validate-overlay (PER-02 shape check) or --full (INS-02 install orchestration: overlay + task registration)",
    )
    sp.add_argument(
        "--validate-overlay",
        action="store_true",
        help="validate the per-user overlay/{voice,brand,keywords,people}/ layer",
    )
    sp.add_argument(
        "--full",
        action="store_true",
        help="full first-run orchestration: detect client, scaffold+validate the overlay, and drive per-client scheduled-task registration (host = launchd/Task Scheduler directly; Cowork/VM = paste-prompt)",
    )
    sp.add_argument(
        "--overlay-dir",
        default=None,
        help="overlay dir override (default: $BRAIN_OVERLAY_DIR or <vault>/overlay)",
    )
    sp.add_argument(
        "--no-scaffold-overlay",
        dest="scaffold_overlay",
        action="store_false",
        help="[--full] do NOT scaffold empty overlay categories from the template",
    )
    sp.add_argument(
        "--template-dir",
        default=None,
        help="[--full] overlay template dir (default: <repo>/overlay/template)",
    )
    sp.add_argument(
        "--no-register-tasks",
        dest="register_tasks",
        action="store_false",
        help="[--full] skip the per-client scheduled-task registration step",
    )
    sp.add_argument(
        "--apply",
        action="store_true",
        help="[--full, host only] actually invoke the OS installer script (default: dry-run read-only probe). Ignored on the VM leg.",
    )
    sp.add_argument(
        "--manifest",
        default=None,
        help="[--full] task manifest path (default: installed/repo routines/manifest.json)",
    )
    sp.add_argument(
        "--save-cowork-prompt",
        default=None,
        help="[--full, cowork] also write the Cowork paste-prompt to this file",
    )
    sp.add_argument(
        "--no-seed-vault",
        dest="seed_vault",
        action="store_false",
        help="[--full] do NOT seed a genuinely empty vault with the 3 generic sample notes",
    )
    sp.add_argument(
        "--import-from",
        default=None,
        help="[--full, host only] guided first-ingest: stage an existing folder of documents (e.g. an Obsidian vault) into this vault's inbox/ and run the standard ingest drain. Prints a dry-run manifest (file count/bytes/extensions) first; pass --yes to actually stage + ingest. Refused on --role vm.",
    )
    sp.add_argument(
        "--yes",
        action="store_true",
        help="[--import-from] skip the interactive y/N confirmation",
    )
    sp.add_argument(
        "--import-force",
        action="store_true",
        help="[--import-from] override the default safety caps (5000 files / 500 MB)",
    )
    sp.add_argument("--json", action="store_true")


def _add_doctor(sub) -> None:
    sp = sub.add_parser(
        "doctor",
        help="READ-ONLY health + version table across every surface: engine, index/snapshot schema, CLI + Desktop plugin stores, staged workspaces, marketplace cache freshness (ADR-0005 Ruling 2). role=vm gets the staged-workspace-only subset (engine stamp, skill bundles, snapshot, model cache, maintain heartbeat) plus a host-only-surfaces list, instead of crashing or host checks",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--check-registry",
        action="store_true",
        help="host only: add the 'PyPI registry drift' row (repo tag / installed / latest-published-on-PyPI) via a single cached HTTPS metadata read. Off by default — this is the only network call `doctor` ever makes, and only with this flag.",
    )


def _add_alerts(sub) -> None:
    sp = sub.add_parser(
        "alerts",
        help="READ-ONLY degradation digest every harness can call: auto-update state, weekly-synthesis task health, engine-feedback backlog, the owner-decision queue, and whatever is degraded RIGHT NOW per the findings feed `brain maintain` rewrites each run. Pure file reads — no index, embedder, network or key. role=vm reads only its own vault and REPORTS the two host-home sources it cannot reach, so silence never means 'could not look'.",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--one-line",
        action="store_true",
        help="emit the single-line banner form a SessionStart hook injects (empty output when all clear)",
    )


def _add_exceptions(sub) -> None:
    sp = sub.add_parser(
        "exceptions",
        help="show, open, or print the page that says what needs you — the "
             "one `brain maintain` rewrites every run. Host: sweeps the "
             "workspace registry and reports EVERY vault. VM: its own vault, "
             "the tier-gated mount copy. A vault whose nightly has not "
             "written a summary reports UNKNOWN, never a zero.",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--open", dest="open_page", action="store_true",
        help="hand the page to the desktop browser (macOS/Windows/Linux). A "
             "sandbox with no browser reports that and prints the path.",
    )
    sp.add_argument(
        "--text", action="store_true",
        help="print the page as plain text — for a harness with no browser "
             "(Codex in a terminal, a Cowork sandbox)",
    )


def _add_install_hook(sub) -> None:
    sp = sub.add_parser(
        "install-hook",
        help="HOST: place the SessionStart alert hook in ~/.claude/hooks/ and "
             "register it in settings.json so every Claude Code session gets "
             "the `brain alerts` banner. Idempotent; only ever ADDS its own "
             "entry and never touches permissions. `brain update` runs this "
             "too — call it directly on a first install.",
    )
    sp.add_argument(
        "--claude-home",
        default=None,
        help="Claude Code config dir (default: ~/.claude)",
    )
    sp.add_argument("--json", action="store_true")


def _add_mcp_config(sub) -> None:
    sp = sub.add_parser(
        "mcp-config",
        help="print the MCP-client config entry to run brain-mcp against this vault (paste into Claude Desktop / Claude Code mcpServers). Read-only; no index or key touched.",
    )
    sp.add_argument(
        "--name",
        default="brainiac",
        help="server name/key in the config (default: %(default)s) — use a distinct name per vault",
    )
    sp.add_argument(
        "--max-tier",
        default=None,
        help="egress ceiling for this MCP server (default: the host full-vault tier, same as the CLI — narrow it here for a capped server)",
    )
    sp.add_argument("--json", action="store_true")


def _add_provision_request(sub) -> None:
    sp = sub.add_parser(
        "provision-request",
        help="VM-side (PRV-10): stage a new-vault provisioning request marker "
             "for the host to complete (no key, no launchd, no registry)",
    )
    sp.add_argument("--json", action="store_true")


def _add_provision_drain(sub) -> None:
    sp = sub.add_parser(
        "provision-drain",
        help="HOST-broker (PRV-10): scan registered-workspace parents for "
             "pending provision requests and complete each (init --full "
             "--apply + sync --publish + model + registry); also runs as a "
             "fold on the hourly maintain daily branch",
    )
    sp.add_argument("--json", action="store_true")


def add_parser(sub) -> None:
    _add_init(sub)
    _add_doctor(sub)
    _add_alerts(sub)
    _add_exceptions(sub)
    _add_install_hook(sub)
    _add_mcp_config(sub)
    _add_provision_request(sub)
    _add_provision_drain(sub)
