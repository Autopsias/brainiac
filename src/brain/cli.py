"""`brain` — the one universal interface any tool/harness can call.

THIS is the integration surface (not BrainCore, not MCP). It returns sourced
results as JSON and applies the deny-by-default classification filter as the
FINAL stage before stdout. A harness self-discovers the whole contract from
`brain --help` after reading one paragraph in AGENTS.md.

    brain init --validate-overlay [--overlay-dir DIR]   # PER-02: validate the
                                            # per-user overlay/{voice,brand,
                                            # keywords,people}/ layer (minimal
                                            # slice; full init lands later)
    brain search <query> [--json] [-k N] [--rerank] [--max-tier TIER]
    brain hybrid-search <query> ...        # alias of search (fused RRF BM25+dense)
    brain grep <pattern> [--regex] [-k N]  # lexical-first, NO embedding
    brain bases-query --where k=v [-k N]   # structured frontmatter view, NO embedding
    brain bases-query --latest-only        # TMP-02: exclude superseded notes
    brain bases-query --as-of YYYY-MM-DD   # TMP-02: point-in-time view
    brain supersede <old-id> <new-id> [--reason R]   # retire old-id -> new-id [HOST]
    brain graph-expand <id...> [--depth D] # wikilink-BFS + PPR, DISCOVERY-ONLY
    brain graphify [--force] [--dry-run]   # monthly discovery graph build [HOST]
    brain get <id> [--json] [--max-tier TIER]
    brain read <id>                        # alias of get
    brain recent [--json] [-n N] [--max-tier TIER]
    brain draft-capture [--id ID] [--source]   # VM-side capture: stage a DRAFT
    brain status [--json]                  # snapshot gen/age + pending drafts
    brain doctor [--json]                  # health + version table, ALL surfaces (read-only)
    brain sync [--publish]                 # incremental upsert + drain drafts [HOST]
    brain snapshot [--dest DIR]            # publish read-only snapshot        [HOST]
    brain rebuild [--vault DIR]            # rebuild the derived index (safe)
    brain project --dest DIR [--max-tier TIER]   # real containment: filtered copy
    brain ingest [--dry-run]                # host-broker: drain <vault>/inbox/ (ING-01/03)
    brain ingest-transcript <path> --origin O [--language L]   # host-broker (ING-04)
    brain write <relpath> [--reason R]     # host-broker, audited, fails closed
    brain verify-audit [--json]            # verify the Ed25519 chain

Trust role (--role / $BRAIN_ROLE, default host): the Cowork Linux VM runs
``--role vm`` — a READ + DRAFT surface. It may run the read tools + ``status`` +
``draft-capture`` ONLY; the [HOST] commands (write/rebuild/sync/snapshot/project/
verify-audit) are refused on the VM. The VM opens only the read-only published
snapshot (never WAL) and never resolves a signing key. See AGENTS.md §6.

Egress: results are filtered to ``--max-tier`` (default: Internal). Unlabelled
or unrecognised notes are treated as MNPI and withheld (default-deny). Surfacing
Restricted/MNPI requires an explicit ``--max-tier`` elevation — the human gate.
The same filter is reused by the optional MCP adapter (a thin wrapper over this).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__, classification as cls
from . import egress
from .core import BrainCore

EPILOG = """\
note: --vault (and $BRAIN_VAULT) is a TOP-LEVEL option — it must come BEFORE the
      subcommand. `brain --vault ./vault rebuild`, not `brain rebuild --vault …`.
      With $BRAIN_VAULT set, you can omit it entirely.

agentic tool surface (RET-04 — compose these; lexical-first, embed lazily):
  grep / bases-query never embed (cheap first probe); hybrid-search embeds the
  query only on semantic escalation; graph-expand is DISCOVERY-ONLY (its derived
  wikilink graph is never authoritative — confirm candidates with get/read).

temporal-intent routing (TMP-03): when a question is really about TIME —
"latest", "current version", "as of <date>", "previous version" — probe the
temporal query surface FIRST, before plain semantic search:
  brain bases-query --latest-only --json          # "what's current" / "latest"
  brain bases-query --as-of 2026-03-01 --json      # "as of <date>" / point-in-time
  brain get <id> --json                            # inspect previous_version /
                                                     # superseded_by / is_latest_version
                                                     # on any single hit ("previous version")
search/get results also carry `is_latest_version` on every hit, so even a plain
semantic-search agent can prefer the current claim without a second round-trip.

examples:
  brain grep "sqlite-vec" --json
  brain bases-query --where type=note --where classification=Internal --json
  brain bases-query --latest-only --where type=note --json
  brain bases-query --as-of 2026-03-01 --json
  brain search "arctic embed" --rerank --json
  brain graph-expand brain-engine --depth 2 --json
  brain get arctic-embed-choice --json
  brain recent -n 5 --max-tier Confidential
  brain --vault ./vault rebuild
  brain --vault ./vault supersede arctic-embed-choice e5-small-choice --reason "switched embedder"
  brain --vault ./vault project --dest /tmp/vm-workspace --max-tier Internal

egress filter (deny-by-default):
  tiers low->high: Public < Internal < Confidential < Restricted < MNPI
  default --max-tier is Internal; unlabelled notes => MNPI => withheld.
  the filter is the final stage before stdout. it is an egress DECISION, not
  containment — a file-capable harness reads Markdown directly; use
  `brain project` (a filtered workspace copy) for real containment.
  JSON `egress.total` INCLUDES withheld notes by design (it is an audit count,
  not a leak of content); `egress.surfaced` is what was printed.
"""


def _json_default(o: Any) -> Any:
    """Coerce non-JSON-native values to native types for ``json.dump``.

    The dense-retrieval path (``OnnxEmbedder``/near-dup scoring) hands back
    ``numpy`` scalars/arrays despite the ``list[list[float]]`` type contract, and
    stdlib ``json`` cannot serialise ``numpy.float32`` etc. — that crashed
    ``brain integrity --json`` (and would crash ANY ``--json`` subcommand) on the
    first real hit (S11-BUG-01). Duck-typed so no hard ``numpy`` import is needed:
    numpy scalars expose ``.item()`` (→ a native Python scalar), arrays expose
    ``.tolist()``. Sets/tuples degrade to lists. Anything else falls through to
    ``str`` rather than re-raising, so emission never crashes on an odd type."""
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return o.item()
        except (ValueError, TypeError):
            pass
    tolist = getattr(o, "tolist", None)
    if callable(tolist):
        try:
            return o.tolist()
        except (ValueError, TypeError):
            pass
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _emit(obj: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=_json_default)
        sys.stdout.write("\n")
    else:
        sys.stdout.write((human if human is not None else str(obj)) + "\n")


def _filter_dicts(items: list[dict], max_tier: str) -> tuple[list[dict], dict]:
    # THE single egress chokepoint — every content-returning subcommand routes
    # through egress.apply_gate so a new content path cannot silently bypass the
    # deny-by-default gate (SEC-01, r2-codex). The MCP adapter shares it too.
    return egress.apply_gate(items, max_tier)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brain",
        description="Local any-LLM second brain — search/get/recent over Markdown, "
                    "sourced JSON out, deny-by-default classification filter.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"brain {__version__}")
    p.add_argument("--vault", default=None, help="vault root (default: $BRAIN_VAULT or ./vault)")
    p.add_argument(
        "--role", default=None, choices=("host", "vm"),
        help="trust role (default: $BRAIN_ROLE or host). 'vm' = read+draft only: "
             "the host-broker commands are refused and the index opens read-only.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", help="emit JSON")
        sp.add_argument(
            "--max-tier", default=cls.DEFAULT_MAX_TIER, choices=cls.TIERS,
            help="egress cap; results above this tier are withheld (default: %(default)s)",
        )

    def add_search(name: str, help_text: str) -> None:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("query")
        sp.add_argument("-k", type=int, default=10, help="max results (default: 10)")
        sp.add_argument("--rerank", action="store_true",
                        help="re-order the top results with the cross-encoder (RET-02); "
                             "skippable — off by default, degrades to no-op if absent")
        sp.add_argument("--rerank-top", type=int, default=15,
                        help="rerank window, clamped to 10-20 (default: 15)")
        sp.add_argument("--rrf-k", type=int, default=60,
                        help="Reciprocal Rank Fusion constant (default: 60)")
        add_common(sp)

    # -- setup (PER-02 / INS-02) — `brain init` ---------------------------
    # Filesystem + subprocess only: never opens the index, never constructs
    # BrainCore (a brand-new install has no index yet). Two modes:
    #   --validate-overlay : the minimal PER-02 shape check (unchanged).
    #   --full             : INS-02 full first-run orchestration (detect client,
    #                        scaffold+validate overlay, drive task registration).
    sp = sub.add_parser(
        "init",
        help="first-run setup: --validate-overlay (PER-02 shape check) or "
             "--full (INS-02 install orchestration: overlay + task registration)",
    )
    sp.add_argument("--validate-overlay", action="store_true",
                    help="validate the per-user overlay/{voice,brand,keywords,people}/ layer")
    sp.add_argument("--full", action="store_true",
                    help="full first-run orchestration: detect client, scaffold+validate "
                         "the overlay, and drive per-client scheduled-task registration "
                         "(host = launchd/Task Scheduler directly; Cowork/VM = paste-prompt)")
    sp.add_argument("--overlay-dir", default=None,
                    help="overlay dir override (default: $BRAIN_OVERLAY_DIR or <vault>/overlay)")
    sp.add_argument("--no-scaffold-overlay", dest="scaffold_overlay", action="store_false",
                    help="[--full] do NOT scaffold empty overlay categories from the template")
    sp.add_argument("--template-dir", default=None,
                    help="[--full] overlay template dir (default: <repo>/overlay/template)")
    sp.add_argument("--no-register-tasks", dest="register_tasks", action="store_false",
                    help="[--full] skip the per-client scheduled-task registration step")
    sp.add_argument("--apply", action="store_true",
                    help="[--full, host only] actually invoke the OS installer script "
                         "(default: dry-run read-only probe). Ignored on the VM leg.")
    sp.add_argument("--manifest", default=None,
                    help="[--full] task manifest path (default: installed/repo routines/manifest.json)")
    sp.add_argument("--save-cowork-prompt", default=None,
                    help="[--full, cowork] also write the Cowork paste-prompt to this file")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "doctor",
        help="READ-ONLY health + version table across every surface: engine, "
             "index/snapshot schema, CLI + Desktop plugin stores, staged "
             "workspaces, marketplace cache freshness (ADR-0005 Ruling 2). "
             "role=vm gets the staged-workspace-only subset (engine stamp, skill "
             "bundles, snapshot, model cache, maintain heartbeat) plus a "
             "host-only-surfaces list, instead of crashing or host checks",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "update",
        help="the ONE 'get current' command (ADR-0005 Ruling 3, UP-01/UP-02): "
             "marketplace refresh -> downgrade-safe CLI-plugin reinstall -> "
             "engine venv refresh -> workspace re-stage -> `brain doctor` "
             "verify, one before->after table, one pass/fail (host only)",
    )
    sp.add_argument("--marketplace", default="profile-a-marketplace",
                    help="marketplace name to refresh/compare against (default: %(default)s)")
    sp.add_argument("--engine-src", default=None,
                    help="engine checkout to install -e from (default: resolved from "
                         "$BRAINIAC_ENGINE_SRC, else this repo's own root)")
    sp.add_argument("--dry-run", action="store_true",
                    help="run every read/decision step for real but skip every mutating "
                         "call (marketplace update, plugin install/uninstall, pip install, "
                         "workspace re-stage) — prints what WOULD happen")
    sp.add_argument("--skip-capability-probe", action="store_true",
                    help="skip the claude-plugin-CLI preflight probe (debugging only)")
    sp.add_argument("--json", action="store_true")

    # `search` and `hybrid-search` are the SAME fused RRF retrieval (RET-01);
    # the second name is the explicit agentic-tool spelling (RET-04).
    add_search("search", "fused RRF(60) BM25 + dense retrieval (sourced)")
    add_search("hybrid-search", "alias of `search`: fused RRF(60) BM25 + dense (RET-01)")

    sp = sub.add_parser("grep", help="lexical-first exact/regex scan over notes — NO embedding (RET-04)")
    sp.add_argument("pattern")
    sp.add_argument("-k", type=int, default=20, help="max results (default: 20)")
    sp.add_argument("--regex", action="store_true", help="treat pattern as a regex")
    add_common(sp)

    sp = sub.add_parser("bases-query", help="structured frontmatter view over indexed columns — NO embedding (RET-04)")
    sp.add_argument("--where", action="append", default=[], metavar="KEY=VAL",
                    help="exact-match filter on id/title/type/classification/zone/path (repeatable)")
    sp.add_argument("--latest-only", action="store_true",
                    help="TMP-02: exclude notes retired via `brain supersede` "
                         "(is_latest_version: false) — the Latest Only view")
    sp.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="TMP-02: point-in-time view — notes valid on this date "
                         "(effective_date, else document_date, else created; "
                         "excludes anything superseded by then) — the As Of view")
    sp.add_argument("-k", type=int, default=50, help="max results (default: 50)")
    add_common(sp)

    sp = sub.add_parser(
        "supersede",
        help="host-broker: retire <old-id> in favour of <new-id> — both sides "
             "of the version chain, signed (TMP-02, ADR-0003 Ruling 2/8)",
    )
    sp.add_argument("old_id", metavar="old-id")
    sp.add_argument("new_id", metavar="new-id")
    sp.add_argument("--reason", default="")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("graph-expand", help="wikilink-BFS + PPR multi-hop expansion — DISCOVERY-ONLY (RET-03)")
    sp.add_argument("seeds", nargs="+", help="seed note id(s)")
    sp.add_argument("--depth", type=int, default=2, help="BFS hop depth (default: 2)")
    sp.add_argument("-k", type=int, default=10, help="max candidates (default: 10)")
    sp.add_argument("--no-ppr", action="store_true", help="BFS only, skip Personalized PageRank")
    sp.add_argument(
        "--use-inferred", action="store_true",
        help="fold graphify's published INFERRED edges into the traversal too "
             "(GRF-01, optional; host-only, silently ignored on role=vm)",
    )
    add_common(sp)

    sp = sub.add_parser("get", help="fetch one note by id")
    sp.add_argument("id")
    add_common(sp)

    sp = sub.add_parser("read", help="alias of `get`: read one full note by id (RET-04)")
    sp.add_argument("id")
    add_common(sp)

    sp = sub.add_parser("recent", help="list recently updated notes")
    sp.add_argument("-n", type=int, default=10, help="how many (default: 10)")
    add_common(sp)

    sp = sub.add_parser(
        "draft-capture",
        help="VM-side capture: stage a candidate note as a plain DRAFT "
             "(no sign, no index, no WAL) for the host to drain later",
    )
    sp.add_argument("--id", default=None, help="note id (default: from frontmatter or content hash)")
    sp.add_argument("--source", action="store_true", help="stage as a raw/ source (vs a brain/ note)")
    sp.add_argument("--content", default=None, help="note text (default: read stdin)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("rebuild", help="rebuild the derived index from vault/ (always safe)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "sync",
        help="incremental upsert by path+hash + delete-propagation (no full rebuild); "
             "drains capture drafts first (host)",
    )
    sp.add_argument("--no-drain", action="store_true",
                    help="skip the host capture drain (read-only/VM leg)")
    sp.add_argument("--publish", action="store_true",
                    help="republish the read-only snapshot after reconcile so the VM's "
                         "next read sees the just-committed note (closes the capture loop)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "snapshot", help="publish a read-only, generation-stamped index snapshot (host)")
    sp.add_argument("--dest", default=None, help="snapshot dir (default: vault/.brain/snapshot)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "status", help="report index stats + read-only snapshot generation/age")
    sp.add_argument("--snapshot-dest", default=None)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project", help="write a classification-filtered copy of the vault (real containment)")
    sp.add_argument("--dest", required=True, help="destination directory (recreated each run)")
    sp.add_argument("--max-tier", default=cls.DEFAULT_MAX_TIER, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "ingest",
        help="host-broker: drain <vault>/inbox/ — extract to Markdown, archive "
             "originals immutably, commit through the signed write path (ING-01)",
    )
    sp.add_argument("--dry-run", action="store_true",
                    help="report what would happen; no moves, no writes, no signing")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "ingest-transcript",
        help="host-broker: promote one transcript .md into raw/ with explicit "
             "provenance (ING-04) — origin is a source audio/video path, or 'verbal'",
    )
    sp.add_argument("path", help="path to the transcript .md file")
    sp.add_argument("--origin", required=True,
                    help="source audio/video file path, or the literal string 'verbal'")
    sp.add_argument("--language", default=None, help="ISO 639-1 code (default: detected from filename)")
    sp.add_argument("--document-date", default=None, dest="document_date",
                    help="YYYY-MM-DD the underlying meeting/recording happened (optional)")
    sp.add_argument("--classification", default="Internal", choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("write", help="host-broker: write a note (audited, fails closed)")
    sp.add_argument("relpath")
    sp.add_argument("--content", default=None, help="content (default: read stdin)")
    sp.add_argument("--reason", default="")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "audit-key",
        help="host-broker: provision the audit signing key (create-if-absent, NEVER rotates)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("verify-audit", help="verify the Ed25519 audit chain")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("anchor", help="publish the signed chain head OFF-HOST (host; SEC-03)")
    sp.add_argument("--anchor-dir", required=True, help="off-host append-only anchor dir")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("verify-anchor", help="verify the live chain vs the off-host anchor (detect rewrite)")
    sp.add_argument("--anchor-dir", required=True)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("backup", help="encrypted off-device backup of the Markdown truth (host; SEC-03)")
    sp.add_argument("--dest", required=True, help="off-device destination dir")
    sp.add_argument("--no-encrypt", action="store_true",
                    help="write a PLAINTEXT archive (discouraged off-device; default encrypts)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("restore", help="restore (decrypt) a backup archive (host)")
    sp.add_argument("--archive", required=True)
    sp.add_argument("--dest", required=True, help="restore destination dir")
    sp.add_argument("--json", action="store_true")

    # -- UX layer (UX-01 / UX-02 / UX-03) ---------------------------------
    sp = sub.add_parser(
        "capture",
        help="capture a note: HOST signs+writes+syncs; VM drops unsigned draft to capture-inbox/ (UX-01)",
    )
    sp.add_argument("--id", default=None, help="note id (default: derived from content hash)")
    sp.add_argument("--type", default=None, dest="note_type",
                    help="note type (default: note)")
    sp.add_argument("--classification", default=None, choices=cls.TIERS,
                    help="classification tier (default: Internal)")
    sp.add_argument("--content", default=None, help="note text (default: read stdin)")
    sp.add_argument("--reason", default="", help="audit reason (host only)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "brief",
        help="morning brief: drains pending captures (host) + quiet index summary (UX-02)",
    )
    sp.add_argument("-n", type=int, default=5, help="max recent notes to show (default: 5)")
    sp.add_argument("--no-drain", action="store_true",
                    help="skip the capture drain (VM / read-only mode)")
    sp.add_argument("--max-tier", default=cls.DEFAULT_MAX_TIER, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--html", action="store_true",
        help="write a self-contained, overlay-branded HTML brief to .brain/brief/ "
             "(host-only — a new file-egress surface, ADR-0003 Ruling c; refused on role=vm)",
    )

    sp = sub.add_parser(
        "digest",
        help="weekly digest: notes added/updated in the past N days (UX-02)",
    )
    sp.add_argument("--days", type=int, default=7, help="lookback period in days (default: 7)")
    sp.add_argument("--max-tier", default=cls.DEFAULT_MAX_TIER, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--html", action="store_true",
        help="write a self-contained, overlay-branded HTML digest to .brain/brief/ "
             "(host-only — a new file-egress surface, ADR-0003 Ruling c; refused on role=vm)",
    )

    # -- maintenance rituals (CUT-03) — HOST-broker only, refused on role=vm --
    sp = sub.add_parser(
        "check", help="daily-check fold: index reconcile + drain drafts + status (host)")
    sp.add_argument("--dry-run", action="store_true", help="report only; no sync/drain")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "health", help="health fold: status + audit-chain verify + substrate self-test (host)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "curate",
        help="curation fold: refresh-index + unclassified-notes lint + "
             "stale-wikilink-target detection + age x centrality revisit sample (host); "
             "orphan/contradiction/callout lint stay vault-overlay (no brain equivalent)",
    )
    sp.add_argument("--dry-run", action="store_true", help="report only; no refresh-index")
    sp.add_argument("-k", type=int, default=50, help="max findings (default: 50)")
    add_common(sp)

    sp = sub.add_parser(
        "integrity",
        help="integrity-scan fold: audit-chain verify + corpus-wide near-dup scan "
             "directly over the brain vector backend (host; G1)",
    )
    sp.add_argument("--min-score", type=float, default=0.95,
                    help="near-dup cosine threshold (default: 0.95)")
    sp.add_argument("-k", type=int, default=5, help="ANN probe depth per note (default: 5)")
    add_common(sp)

    sp = sub.add_parser(
        "promote-scan",
        help="promotion-scan fold: triage raw/ sources not yet promoted to a "
             "typed brain/ note (host; promotion itself stays a human gate)",
    )
    sp.add_argument("-k", type=int, default=50, help="max candidates (default: 50)")
    add_common(sp)

    sp = sub.add_parser(
        "maintain",
        help="the umbrella: THE single sanctioned host task (brain-nightly) — "
             "sync --publish + brief + recommendations-aging fold, plus date-gated "
             "health/integrity/digest(+curate+promote-scan)/graphify branches; "
             "due-since-last-run catch-up + single-runner lock (ADR-0003 Ruling 5/d)",
    )
    sp.add_argument("--dry-run", action="store_true",
                    help="skip sync/drain/publish/signing; still runs the real "
                         "read-only health/integrity probes for any due branch")
    sp.add_argument("--date", default=None,
                    help="YYYY-MM-DD override for date-gate testing (default: today)")
    sp.add_argument("--min-score", type=float, default=0.95,
                    help="near-dup cosine threshold on a due Tuesday branch (default: 0.95)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "graphify",
        help="graphify discovery build: derived, non-authoritative graph "
             "(wikilinks + capped embedding-neighbour INFERRED edges) + "
             "human-review link candidates (host; ADR-0003 Ruling 6/(a))",
    )
    sp.add_argument("--force", action="store_true",
                    help="bypass the corpus-drift gate and rebuild anyway")
    sp.add_argument("--dry-run", action="store_true",
                    help="build + report only; never publish graph.json")
    sp.add_argument("-n", type=int, default=20, help="max candidates to surface (default: 20)")
    add_common(sp)

    return p


def _make_core(args: Any, role: str) -> BrainCore:
    """Construct BrainCore with the resolved role. Tolerant of a test double that
    patched ``cli.BrainCore`` with a vault-only signature (back-compat)."""
    try:
        return BrainCore(vault=args.vault, role=role)
    except TypeError:
        return BrainCore(vault=args.vault)


# Commands the read+draft-only VM leg may run. Everything else is host-broker.
# capture/brief/digest are included because BrainCore routes correctly by role:
#   capture → draft_capture (VM), write_note (host)
#   brief/digest → read-only stats (VM), drain+stats (host)
# DECISION (H-1, s02): brief/digest STAY in VM_ALLOWED. Gating their output and
# VM membership are separate questions — this call is now explicit rather than
# left implicit. Rationale: once routed through egress.apply_gate (this
# session), brief/digest are exactly as safe on the VM leg as `recent` /
# `search` — read-only, no signing key touched, deny-by-default classification
# filter applied before the summary is assembled. Revisit ONLY if a future
# brief/digest field starts drawing from an ungated source (e.g. raw audit/WAL
# internals) — that would need its own gate or host-only demotion, not a
# blanket VM_ALLOWED removal.
# The CUT-03 maintenance rituals (check/health/curate/integrity/promote-scan/
# maintain) are DELIBERATELY ABSENT here: task-disposition.md calls every one
# of them a write ritual (regen index, sign+drain, query the audit chain), so
# they are host-broker only — refused on role=vm at this gate (defense in
# depth on top of each BrainCore method's own _require_host()).
VM_ALLOWED = frozenset({
    "init",  # filesystem-only overlay validation; safe on either role
    "doctor",  # read-only version/health inspection; no index/key touched
    "search", "hybrid-search", "grep", "bases-query", "graph-expand",
    "get", "read", "recent", "status", "draft-capture",
    "capture", "brief", "digest",
})


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception as exc:  # H-4: top-level guard -- never a raw traceback
        raw_args = argv if argv is not None else sys.argv[1:]
        as_json = "--json" in raw_args
        _emit({"error": type(exc).__name__, "detail": str(exc)} if as_json
              else f"{exc.__class__.__name__}: {exc}", as_json)
        return 3


def _main(argv: list[str] | None = None) -> int:
    from . import config

    args = build_parser().parse_args(argv)
    role = config.role(getattr(args, "role", None))
    cmd = args.cmd

    # VM trust gate: refuse host-broker commands on the VM leg BEFORE constructing
    # BrainCore — no index open, no key resolution on a disallowed verb.
    if role == config.ROLE_VM and cmd not in VM_ALLOWED:
        msg = {
            "error": "role_forbidden",
            "role": role,
            "cmd": cmd,
            "detail": f"'{cmd}' is a host-broker command; the VM leg is read + draft only "
                      f"(allowed: {sorted(VM_ALLOWED)})",
        }
        _emit(msg if getattr(args, "json", False) else
              f"refused: '{cmd}' is host-broker only (role=vm is read+draft). "
              f"Run it on the host.", getattr(args, "json", False))
        return 4

    # `init` is filesystem-only (PER-02 minimal slice) — dispatch BEFORE
    # BrainCore construction so a brand-new install (no index yet) still works.
    if cmd == "init":
        from . import overlay as ov

        if args.full:
            from . import init as brain_init

            report = brain_init.run_full_init(
                vault=args.vault,
                overlay_dir=args.overlay_dir,
                role=role,
                scaffold=args.scaffold_overlay,
                template_dir=args.template_dir,
                register_tasks=args.register_tasks,
                apply=args.apply,
                manifest=args.manifest,
                save_cowork_prompt=args.save_cowork_prompt,
            )
            _emit(report if args.json else None, args.json,
                  None if args.json else brain_init.render_human(report))
            return 0 if report["ok"] else 1

        if not args.validate_overlay:
            detail = ("brain init: choose a mode — --validate-overlay (PER-02 shape "
                      "check) or --full (INS-02 full install orchestration: "
                      "overlay + per-client task registration). "
                      "Run: brain init --validate-overlay | brain init --full")
            _emit({"error": "no_mode", "detail": detail} if args.json else detail,
                  args.json)
            return 2
        path = ov.overlay_dir(args.vault, args.overlay_dir)
        report = ov.validate_overlay(path)
        if args.json:
            _emit(report, True)
        else:
            lines = [f"overlay: {report['overlay_dir']}", f"valid: {report['valid']}"]
            for cat, info in report["categories"].items():
                status = "ok" if not info["issues"] else "ISSUES"
                lines.append(f"  {cat}/: {status} ({info['file_count']} file(s))")
                for issue in info["issues"]:
                    lines.append(f"    - {issue}")
            _emit(None, False, "\n".join(lines))
        return 0 if report["valid"] else 1

    # `doctor` is pure filesystem/subprocess inspection (ADR-0005 Ruling 2) —
    # dispatch BEFORE BrainCore construction, same reasoning as `init`: it
    # must work even against a vault with no index built yet, and it never
    # touches the vault at all.
    if cmd == "doctor":
        from . import doctor as brain_doctor

        # Role-aware (2026-07-07 addendum, ADR-0005 Ruling 2): the VM leg only
        # ever sees the staged zero-install copy, so it gets its own surface
        # set. Structural fallback covers the staged shim, which invokes
        # `python3 -m brain.cli "$@"` directly and never sets $BRAIN_ROLE.
        vm_posture = role == config.ROLE_VM or brain_doctor.looks_like_vm_stage()
        report = (brain_doctor.run_doctor_vm(vault=args.vault) if vm_posture
                  else brain_doctor.run_doctor())
        _emit(report if args.json else None, args.json,
              None if args.json else brain_doctor.render_human(report))
        return 0 if report["ok"] else 1

    # `update` is the UP-02 single top-level entry point: it self-executes
    # (never just prints instructions) and is host-broker only — it mutates
    # the CLI plugin store, the engine venv, and staged workspaces, none of
    # which the VM leg may touch.
    if cmd == "update":
        from . import update as brain_update

        report = brain_update.run_update(
            marketplace_name=args.marketplace,
            engine_src=args.engine_src,
            dry_run=args.dry_run,
            skip_capability_probe=args.skip_capability_probe,
        )
        if args.json:
            _emit(report, True)
        else:
            lines = [f"brain update — {'DRY RUN — ' if args.dry_run else ''}"
                     f"{'PASS' if report['ok'] else 'FAIL/INCOMPLETE'}", ""]
            for step_name, step_val in report["steps"].items():
                lines.append(f"[{step_name}]")
                lines.append(step_val if isinstance(step_val, str) else json.dumps(step_val, indent=2))
                lines.append("")
            if report.get("before_after_rendered"):
                lines.append(report["before_after_rendered"])
                lines.append("")
            lines.append(f"notes: {report.get('notes', '')}")
            if report.get("residual_human_steps"):
                lines.append("")
                lines.append("Residual human step(s):")
                for step in report["residual_human_steps"]:
                    lines.append(f"  - {step}")
            _emit(None, False, "\n".join(lines))
        return 0 if report["ok"] else 1

    try:
        core = _make_core(args, role)
    except Exception as exc:  # pragma: no cover - construction is cheap/stable
        _emit({"error": type(exc).__name__, "detail": str(exc)} if getattr(args, "json", False)
              else f"init failed: {exc}", getattr(args, "json", False))
        return 3

    if cmd in ("search", "hybrid-search"):
        hits = [h.to_dict() for h in core.hybrid_search(
            args.query, k=args.k, rerank=args.rerank,
            rerank_top=args.rerank_top, rrf_k=args.rrf_k)]
        surfaced, report = _filter_dicts(hits, args.max_tier)
        if args.json:
            _emit({"query": args.query, "rerank": args.rerank,
                   "results": surfaced, "egress": report}, True)
        else:
            lines = [f"[{h['source']}] {h['id']}  ({h['classification'] or 'UNLABELLED'})"
                     f"  {h['score']}\n    {h['snippet']}" for h in surfaced]
            footer = f"-- {report['surfaced']}/{report['total']} surfaced; " \
                     f"{report['withheld']} withheld (max-tier={report['max_tier']})"
            _emit(None, False, "\n".join(lines + [footer]) if lines else footer)
        return 0

    if cmd == "grep":
        items = core.grep(args.pattern, k=args.k, regex=args.regex)
        surfaced, report = _filter_dicts(items, args.max_tier)
        if args.json:
            _emit({"pattern": args.pattern, "results": surfaced, "egress": report}, True)
        else:
            lines = [f"{h['id']} ({h['classification'] or 'UNLABELLED'}) "
                     f"x{h['match_count']}\n    {h['snippet']}" for h in surfaced]
            footer = f"-- {report['surfaced']}/{report['total']} surfaced; " \
                     f"{report['withheld']} withheld (max-tier={report['max_tier']})"
            _emit(None, False, "\n".join(lines + [footer]) if lines else footer)
        return 0

    if cmd == "bases-query":
        filters: dict[str, str] = {}
        for clause in args.where:
            if "=" in clause:
                key, val = clause.split("=", 1)
                filters[key.strip()] = val.strip()
        items = core.bases_query(filters, k=args.k, latest_only=args.latest_only, as_of=args.as_of)
        surfaced, report = _filter_dicts(items, args.max_tier)
        if args.json:
            _emit({"filters": filters, "results": surfaced, "egress": report}, True)
        else:
            lines = [f"{h['id']}  type={h.get('type','?')}  ({h['classification'] or 'UNLABELLED'})"
                     for h in surfaced]
            footer = f"-- {report['surfaced']}/{report['total']} surfaced; " \
                     f"{report['withheld']} withheld (max-tier={report['max_tier']})"
            _emit(None, False, "\n".join(lines + [footer]) if lines else footer)
        return 0

    if cmd == "graph-expand":
        res = core.graph_expand(
            args.seeds, depth=args.depth, k=args.k, use_ppr=not args.no_ppr,
            use_inferred=getattr(args, "use_inferred", False))
        # Egress-gate the DISCOVERY candidates: a withheld note must not leak via
        # the graph surface either. Filter on each candidate's classification.
        surfaced, report = _filter_dicts(res.get("results", []), args.max_tier)
        res["results"] = surfaced
        res["egress"] = report
        if args.json:
            _emit(res, True)
        else:
            lines = [f"[graph] {h['id']}  ({h['classification'] or 'UNLABELLED'})  "
                     f"hops={h.get('hops')}  ppr={h.get('ppr')}" for h in surfaced]
            head = (f"-- DISCOVERY-ONLY (non-authoritative); seeds="
                    f"{res.get('resolved_seeds')}; method={res.get('method')}")
            footer = f"-- {report['surfaced']}/{report['total']} surfaced; " \
                     f"{report['withheld']} withheld (max-tier={report['max_tier']})"
            _emit(None, False, "\n".join([head] + lines + [footer]))
        return 0

    if cmd in ("get", "read"):
        note = core.get(args.id)
        items = [note] if note else []
        surfaced, report = _filter_dicts(items, args.max_tier)
        if not note:
            _emit({"error": "not_found", "id": args.id} if args.json else f"not found: {args.id}",
                  args.json)
            return 1
        if not surfaced:
            msg = {"error": "withheld_by_egress_filter", "id": args.id, "egress": report}
            _emit(msg if args.json else f"withheld by egress filter: {args.id} "
                  f"(classification={note.get('classification') or 'UNLABELLED'}, "
                  f"max-tier={args.max_tier})", args.json)
            return 2
        _emit(surfaced[0] if args.json else
              f"# {surfaced[0]['title']}  ({surfaced[0]['classification']})\n{surfaced[0]['body']}",
              args.json)
        return 0

    if cmd == "recent":
        items = core.recent(limit=args.n)
        surfaced, report = _filter_dicts(items, args.max_tier)
        if args.json:
            _emit({"results": surfaced, "egress": report}, True)
        else:
            lines = [f"{it['updated']}  {it['id']}  ({it['classification'] or 'UNLABELLED'})"
                     for it in surfaced]
            lines.append(f"-- {report['surfaced']}/{report['total']} surfaced; "
                         f"{report['withheld']} withheld (max-tier={report['max_tier']})")
            _emit(None, False, "\n".join(lines))
        return 0

    if cmd == "draft-capture":
        content = args.content if args.content is not None else sys.stdin.read()
        try:
            res = core.draft_capture(content, ident=args.id, is_source=args.source)
        except ValueError as exc:  # unsafe id / traversal -> fail closed (C-1)
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"draft refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"staged draft {res['id']} -> {res['draft']} "
              f"(signed={res['signed']}, indexed={res['indexed']}); "
              f"host drain will sign + index + snapshot", args.json)
        return 0

    if cmd == "rebuild":
        try:
            res = core.rebuild()
        except Exception as exc:  # H-4: no raw tracebacks from maintenance cmds
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"rebuild failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"indexed {res['indexed']} notes ({res['chunks']} chunks) via "
              f"{res['backend']} [{res['embed_model']} d={res['embed_dim']}] -> {res['db']}",
              args.json)
        return 0

    if cmd == "sync":
        try:
            res = core.sync(drain=not args.no_drain, publish=args.publish)
        except Exception as exc:  # H-4
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"sync failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        # C8: sync's "ingest" sub-report carries the identical promoted-note
        # list (with real classifications) that `brain ingest --json` already
        # routes through the egress gate — sync --json printed it RAW, a
        # second content-returning surface bypassing the single chokepoint.
        ingest_res = res.get("ingest") or {}
        if ingest_res.get("processed"):
            surfaced, egress_report = _filter_dicts(ingest_res["processed"], cls.DEFAULT_MAX_TIER)
            ingest_res["processed"] = surfaced
            ingest_res["egress"] = egress_report
        # E4: "duplicates" carries `existing_id` — a real note id (of a note
        # that may sit above the max tier) — so it is exactly as much a
        # content-returning surface as "processed" and must go through the
        # same gate, not leak raw.
        if ingest_res.get("duplicates"):
            dup_surfaced, dup_egress = _filter_dicts(ingest_res["duplicates"], cls.DEFAULT_MAX_TIER)
            ingest_res["duplicates"] = dup_surfaced
            ingest_res["duplicates_egress"] = dup_egress
        if args.json:
            _emit(res, True)
        else:
            d = res.get("drain", {})
            snap = res.get("snapshot")
            tail = (f"; snapshot gen {snap['generation']}" if snap else "")
            _emit(None, False,
                  f"sync [{res['mode']}]: +{res.get('added',0)} ~{res.get('updated',0)} "
                  f"-{res.get('deleted',0)} ={res.get('unchanged',0)} "
                  f"({res['chunks']} chunks); drained {d.get('promoted',0)} "
                  f"(skipped {d.get('skipped',0)})" + tail)
        return 0

    if cmd == "snapshot":
        try:
            res = core.publish_snapshot(args.dest)
        except Exception as exc:  # H-4
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"snapshot failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"published snapshot gen {res['generation']} "
              f"({res['notes']} notes, {res['chunks']} chunks) -> {res['snapshot_db']}",
              args.json)
        return 0

    if cmd == "status":
        res = core.status(args.snapshot_dest)
        if args.json:
            _emit(res, True)
        else:
            ix, sn, ver = res.get("index", {}), res.get("snapshot", {}), res.get("version", {})
            skew_lines = []
            if ver.get("index_newer_than_binary"):
                skew_lines.append(
                    f"  WARNING: index schema_version {ver.get('index_schema_version')} > "
                    f"binary SCHEMA_VERSION {ver.get('binary_schema_version')} — "
                    "index was built by a newer brain; update the engine "
                    "(or run `brain sync --rebuild` to force a downgrade)")
            if ver.get("snapshot_newer_than_binary"):
                skew_lines.append(
                    f"  WARNING: snapshot schema_version {ver.get('snapshot_schema_version')} > "
                    f"binary SCHEMA_VERSION {ver.get('binary_schema_version')} — "
                    "snapshot is newer than this CLI; update the engine")
            _emit(None, False,
                  f"brain {ver.get('package_version','?')}\n"
                  f"index: {ix.get('notes','?')} notes / {ix.get('chunks','?')} chunks "
                  f"[{ix.get('embed_model','?')} d={ix.get('embed_dim','?')}]\n"
                  f"snapshot: {sn.get('snapshot','?')} "
                  + (f"gen {sn.get('generation')} age {sn.get('age_human')}"
                     if sn.get('snapshot') == 'present' else '')
                  + ("\n" + "\n".join(skew_lines) if skew_lines else ""))
        return 0

    if cmd == "project":
        from .projection import project_workspace

        res = project_workspace(core.vault, args.dest, max_tier=args.max_tier).to_dict()
        _emit(res if args.json else
              f"projected {res['copied']} notes (<= {res['max_tier']}) to {res['dest']}; "
              f"excluded {res['excluded']} ({res['excluded_unlabelled']} unlabelled)",
              args.json)
        return 0

    if cmd == "ingest":
        try:
            res = core.ingest_dropzone(dry_run=args.dry_run)
        except Exception as exc:  # RoleError -> fail closed, zero side effects
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"ingest refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        # Egress (ADR-0003 Ruling 8): the report lists promoted note ids +
        # classifications, so it joins the content-returning surface — route
        # the processed list through the same gate as curate/integrity.
        if not args.dry_run and res.get("processed"):
            surfaced, egress_report = _filter_dicts(
                res["processed"],  # each entry already carries its real
                                    # promoted-note classification (pipeline.py)
                cls.DEFAULT_MAX_TIER,
            )
            res["processed"] = surfaced
            res["egress"] = egress_report
        # E4: "duplicates" carries `existing_id` (a real note id, possibly
        # above max tier) via `existing_id`/`classification` — C8 only routed
        # "processed" through the gate, leaving this sub-list to bypass it.
        if not args.dry_run and res.get("duplicates"):
            dup_surfaced, dup_egress = _filter_dicts(res["duplicates"], cls.DEFAULT_MAX_TIER)
            res["duplicates"] = dup_surfaced
            res["duplicates_egress"] = dup_egress
        if args.json:
            _emit(res, True)
        else:
            _emit(None, False,
                  f"ingest [dry_run={res['dry_run']}]: "
                  f"processed={len(res.get('processed', []))} "
                  f"quarantined={len(res.get('quarantined', []))} "
                  f"duplicates={len(res.get('duplicates', []))} "
                  f"skipped={len(res.get('skipped', []))}")
        return 0

    if cmd == "ingest-transcript":
        try:
            res = core.ingest_transcript(
                args.path, origin=args.origin, language=args.language,
                document_date=args.document_date, classification=args.classification,
            )
        except Exception as exc:  # RoleError -> fail closed, zero side effects
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"ingest-transcript refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        # Egress (ADR-0003 Ruling 8, mirrors `ingest`): a fresh promotion's
        # result carries a real note id + classification, so it joins the
        # content-returning surface even though it is a single dict, not a
        # list — reuse the same gate via a one-element wrap.
        if res.get("ok") and not res.get("duplicate") and res.get("id"):
            surfaced, egress_report = _filter_dicts([res], cls.DEFAULT_MAX_TIER)
            res = surfaced[0] if surfaced else {"withheld": True, "reason": "above max-tier"}
            res["egress"] = egress_report
        if args.json:
            _emit(res, True)
        else:
            if not res.get("ok"):
                _emit(None, False, f"ingest-transcript failed: {res.get('reason')}")
            elif res.get("duplicate"):
                _emit(None, False, f"ingest-transcript: duplicate of raw/{res.get('existing_id')}.md")
            else:
                _emit(None, False, f"ingest-transcript: {res.get('note')} (origin={args.origin})")
        return 0 if res.get("ok", True) else 3

    if cmd == "write":
        content = args.content if args.content is not None else sys.stdin.read()
        try:
            res = core.write_note(args.relpath, content, reason=args.reason)
        except Exception as exc:  # KeyUnavailable / ValueError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"write refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else f"wrote {res['written']} (audited)", args.json)
        return 0

    if cmd == "supersede":
        try:
            res = core.supersede(args.old_id, args.new_id, reason=args.reason)
        except Exception as exc:  # RoleError / ValueError / KeyUnavailable -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"supersede refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"superseded {res['old_id']} -> {res['new_id']} (both sides signed)",
              args.json)
        return 0

    if cmd == "audit-key":
        from . import audit
        try:
            res = audit.provision_signing_key()
        except Exception as exc:  # KeyUnavailable -> report, don't traceback
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"audit key: unavailable ({exc})", args.json)
            return 1
        _emit(res if args.json else
              f"audit key: {res['status']} ({res.get('source') or res.get('store')})",
              args.json)
        return 0

    if cmd == "verify-audit":
        res = core.verify_audit()
        _emit(res if args.json else
              f"audit chain: {res['status']} ({res['entries_checked']} entries, "
              f"{len(res['errors'])} errors)", args.json)
        return 0 if res["status"] in ("ok", "empty") else 1

    if cmd == "anchor":
        res = core.anchor_chain(args.anchor_dir)
        rec = res["record"]
        _emit(res if args.json else
              f"anchored head {rec['head'][:16]}… @ {rec['entry_count']} entries "
              f"-> {res['anchor_log']}", args.json)
        return 0

    if cmd == "verify-anchor":
        res = core.verify_anchor(args.anchor_dir)
        _emit(res if args.json else
              f"anchor: {res['status']} ({res['checked']} records checked, "
              f"{len(res['divergences'])} divergences)", args.json)
        return 0 if res["status"] in ("ok", "no-anchor") else 1

    if cmd == "backup":
        try:
            res = core.backup(args.dest, encrypt=not args.no_encrypt)
        except Exception as exc:  # EncryptionKeyUnavailable etc. -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"backup refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"backup ({'encrypted' if res['encrypted'] else 'PLAINTEXT'}) "
              f"{res['files']} files -> {res['archive']} "
              f"(sha256 {res['plaintext_sha256'][:16]}…)", args.json)
        return 0

    if cmd == "restore":
        try:
            res = core.restore(args.archive, args.dest)
        except Exception as exc:
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"restore failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"restored {res['files']} files -> {res['dest']} "
              f"(sha256 {res['plaintext_sha256'][:16]}…)", args.json)
        return 0

    # -- UX layer (UX-01 / UX-02 / UX-03) ---------------------------------

    if cmd == "capture":
        content = args.content if args.content is not None else sys.stdin.read()
        try:
            res = core.capture(
                content,
                note_id=args.id,
                note_type=args.note_type,
                classification=args.classification,
                reason=args.reason,
            )
        except Exception as exc:
            _emit(
                {"error": type(exc).__name__, "detail": str(exc)} if args.json
                else f"capture failed ({type(exc).__name__}): {exc}",
                args.json,
            )
            return 3
        if args.json:
            _emit(res, True)
        elif res.get("signed"):
            _emit(None, False,
                  f"captured {res['id']} -> {res['path']} "
                  f"(signed=True, indexed=True)")
        else:
            _emit(None, False,
                  f"draft staged {res['id']} -> {res['draft']} "
                  f"(signed=False — VM; host drain will sign + index)")
        return 0

    if cmd in ("brief", "digest") and getattr(args, "html", False):
        # ADR-0003 Ruling c / HARDENED:codex-verify-r1: the HTML file is a NEW
        # file-egress surface a stdout gate doesn't cover, so it is HOST-ONLY.
        # Refuse BEFORE any write is attempted — the VM leg (read+draft) never
        # gains a filesystem write surface, even though `brief`/`digest`
        # text/json mode stays VM_ALLOWED.
        if role == config.ROLE_VM:
            msg = {
                "error": "role_forbidden", "role": role, "cmd": f"{cmd} --html",
                "detail": f"'{cmd} --html' writes a file — host-only; the VM leg "
                          "is read+draft only and never gains a filesystem write surface.",
            }
            _emit(msg if args.json else
                  f"refused: '{cmd} --html' is host-only (role=vm cannot write files). "
                  "Run it on the host.", args.json)
            return 4

    if cmd == "brief":
        if getattr(args, "html", False):
            res = core.brief_html(max_recent=args.n, drain=not args.no_drain, max_tier=args.max_tier)
            if args.json:
                _emit(res, True)
            else:
                _emit(None, False, f"brief HTML written -> {res['path']} (latest: {res['latest_path']})")
            return 0
        res = core.brief(max_recent=args.n, drain=not args.no_drain, max_tier=args.max_tier)
        if args.json:
            _emit(res, True)
        else:
            from .brief import format_brief
            _emit(None, False, format_brief(res))
        return 0

    if cmd == "digest":
        if getattr(args, "html", False):
            res = core.digest_html(days=args.days, max_tier=args.max_tier)
            if args.json:
                _emit(res, True)
            else:
                _emit(None, False, f"digest HTML written -> {res['path']} (latest: {res['latest_path']})")
            return 0
        res = core.digest(days=args.days, max_tier=args.max_tier)
        if args.json:
            _emit(res, True)
        else:
            from .brief import format_digest
            _emit(None, False, format_digest(res))
        return 0

    # -- maintenance rituals (CUT-03) --------------------------------------
    from . import maintenance as maint

    if cmd == "check":
        res = core.check(dry_run=args.dry_run)
        if args.json:
            _emit(res, True)
        else:
            head = f"check [dry_run={res['dry_run']}]"
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(res["outcomes"]))
        return 0

    if cmd == "health":
        res = core.health()
        if args.json:
            _emit(res, True)
        else:
            st = res.get("selftest", {})
            head = (f"health: probe_ok={st.get('probe_ok')} "
                    f"backend={st.get('vector_backend')} model={st.get('embed_model')}")
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(res["outcomes"]))
        return 0

    if cmd == "curate":
        res = core.curate(dry_run=args.dry_run, k=args.k)
        surfaced, report = _filter_dicts(res["unclassified_notes"], args.max_tier)
        action_required = [
            maint.action_required_item(
                f"{n['id']} has a missing/invalid classification frontmatter value",
                "default-deny would withhold this note (treated as MNPI) until fixed",
                f"add classification: <Tier> to {n['path']}'s frontmatter",
                n["path"],
            )
            for n in surfaced
        ]

        # stale wikilink targets — gate on the FROM note, and the TARGET note
        # too when it resolved (both must clear the cap, same discipline as
        # near_dup's pair gating).
        stale_nodes: dict[str, dict] = {}
        for s in res["stale_links"]:
            stale_nodes[s["from"]["id"]] = s["from"]
            if s.get("target"):
                stale_nodes[s["target"]["id"]] = s["target"]
        surfaced_stale_nodes, stale_report = _filter_dicts(list(stale_nodes.values()), args.max_tier)
        surfaced_stale_ids = {n["id"] for n in surfaced_stale_nodes}
        gated_stale = [
            s for s in res["stale_links"]
            if s["from"]["id"] in surfaced_stale_ids
            and (s.get("target") is None or s["target"]["id"] in surfaced_stale_ids)
        ]
        action_required += [
            maint.action_required_item(
                f"{s['from']['id']} links to {s['target_text']!r} which "
                + ("no longer resolves to any note" if s["reason"] == "vanished"
                   else f"has moved to {s['target']['path']}"),
                "a wikilink whose target vanished or moved to archive/ leads somewhere outdated",
                "repoint the link, update the target, or accept it as an intentional historical reference",
                s["from"]["path"],
            )
            for s in gated_stale
        ]

        # revisit sample — informational triage list, gated the same way.
        surfaced_revisit, revisit_report = _filter_dicts(res["revisit_sample"], args.max_tier)

        outcomes = maint.build_outcomes(res["auto_fixed"], action_required, [])
        if args.json:
            _emit({**res, "unclassified_notes": surfaced, "stale_links": gated_stale,
                  "revisit_sample": surfaced_revisit, "egress": report,
                  "stale_egress": stale_report, "revisit_egress": revisit_report,
                  "outcomes": outcomes}, True)
        else:
            head = (f"curate [dry_run={res['dry_run']}] -- {report['surfaced']}/{report['total']} unclassified surfaced, "
                    f"{len(gated_stale)} stale link(s), {len(surfaced_revisit)} revisit candidate(s)")
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(outcomes))
        return 0

    if cmd == "integrity":
        res = core.integrity(min_score=args.min_score, k=args.k)
        pairs = res["near_dup_pairs"]
        nodes = {}
        for p in pairs:
            nodes[p["a"]["id"]] = p["a"]
            nodes[p["b"]["id"]] = p["b"]
        surfaced_nodes, report = _filter_dicts(list(nodes.values()), args.max_tier)
        surfaced_ids = {n["id"] for n in surfaced_nodes}
        gated_pairs = [p for p in pairs if p["a"]["id"] in surfaced_ids and p["b"]["id"] in surfaced_ids]
        action_required = [maint.action_required_item(
            f"{p['a']['id']} <-> {p['b']['id']} score={p['score']}",
            "de-dup is a human merge/keep judgment, never auto-merged",
            "review both notes; merge or explicitly mark distinct",
            f"{p['a']['path']} | {p['b']['path']}",
        ) for p in gated_pairs]
        if res.get("audit_issue"):
            action_required.insert(0, res["audit_issue"])
        outcomes = maint.build_outcomes([], action_required, res["blocked"])
        pair_report = {"total_pairs": len(pairs), "surfaced_pairs": len(gated_pairs),
                       "withheld_pairs": len(pairs) - len(gated_pairs), "max_tier": args.max_tier}
        if args.json:
            _emit({"ritual": "integrity", "min_score": res["min_score"],
                  "audit": res["audit"], "near_dup_pairs": gated_pairs,
                  "egress": pair_report, "outcomes": outcomes}, True)
        else:
            head = f"integrity -- {pair_report['surfaced_pairs']}/{pair_report['total_pairs']} near-dup pairs surfaced"
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(outcomes))
        return 0

    if cmd == "promote-scan":
        res = core.promote_scan(k=args.k)
        surfaced, report = _filter_dicts(res["candidates"], args.max_tier)
        action_required = [maint.action_required_item(
            f"{n['id']} is an un-promoted raw/ source",
            "promotion is a human gate (P-10-style); never automatic",
            "review for promotion into a typed brain/ note (brain capture / brain write)",
            n["path"],
        ) for n in surfaced]
        outcomes = maint.build_outcomes([], action_required, [])
        if args.json:
            _emit({"ritual": "promote-scan", "candidates": surfaced,
                  "pending_drafts": res["pending_drafts"], "egress": report,
                  "outcomes": outcomes}, True)
        else:
            head = (f"promote-scan -- {report['surfaced']}/{report['total']} candidates surfaced; "
                    f"{res['pending_drafts']} pending draft(s)")
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(outcomes))
        return 0

    if cmd == "maintain":
        parsed_date = None
        if args.date:
            import datetime as _dt
            parsed_date = _dt.date.fromisoformat(args.date)
        res = core.maintain(dry_run=args.dry_run, today=parsed_date, min_score=args.min_score)
        if args.json:
            _emit(res, True)
        else:
            head = (f"maintain [dry_run={res['dry_run']}] {res['date']} ({res['weekday']}) "
                    f"branches_due={res['branches_due']}")
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(res["outcomes"]))
        return 0

    if cmd == "graphify":
        res = core.graphify(force=args.force, dry_run=args.dry_run,
                             max_tier=args.max_tier, candidate_limit=args.n)
        if args.json:
            _emit(res, True)
        elif res.get("skipped"):
            _emit(None, False,
                  f"graphify: skipped ({res['skipped']}) — generation {res.get('generation')}")
        elif res.get("status") in ("build_failed", "invalid_artifact"):
            _emit(None, False, f"graphify: {res['status']} — {res.get('error') or res.get('problems')}")
        else:
            corpus = res["corpus"]
            build = res["build"]
            lines = [
                f"-- DISCOVERY-ONLY (non-authoritative); generation={res.get('generation')} "
                f"published={res.get('published')} dry_run={res.get('dry_run', False)}",
                f"-- notes={corpus['note_count']} explicit={corpus['explicit_edge_count']} "
                f"inferred={corpus['inferred_edge_count']} duration={build['duration_seconds']}s",
            ]
            for c in res.get("candidates", []):
                lines.append(f"[graph] {c['from']} <-> {c['to']}  score={c['score']}  {c.get('reason', '')}")
            lines.append(f"-- {res['egress']['surfaced']}/{res['egress']['total']} candidates surfaced; "
                         f"{res['egress']['withheld']} withheld (max-tier={args.max_tier})")
            _emit(None, False, "\n".join(lines))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
