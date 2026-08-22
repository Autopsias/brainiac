"""`brain` — the one universal interface any tool/harness can call.

THIS is the integration surface (not BrainCore, not MCP). It returns sourced
results as JSON and applies the deny-by-default classification filter as the
FINAL stage before stdout. A harness self-discovers the whole contract from
`brain --help` after reading one paragraph in AGENTS.md.

    brain init --validate-overlay [--overlay-dir DIR]   # PER-02: validate the
                                            # per-user overlay/{voice,brand,
                                            # keywords,people}/ layer (minimal
                                            # slice; full init lands later)
    brain search <query> [--json] [-k N] [--no-rerank] [--explain] [--max-tier TIER]
                                            # rerank is ON by default (window 20, BR-03);
                                            # --no-rerank / $BRAIN_RERANK_DISABLED=1 opt out
    brain hybrid-search <query> ...        # alias of search (RRF BM25+dense+exact)
    brain diagnose <query> --target ID     # gated target-miss tracer
    brain eval replay --against FILE.jsonl # host-only private query-log replay
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
    brain alerts [--json] [--one-line]     # degradation digest for a session start —
                                            # pure file reads, VM_ALLOWED
    brain install-hook [--json]            # place + register the SessionStart alert
                                            # hook in ~/.claude [HOST]
    brain health-report [--json]           # static HTML health page -> .brain/brief/
                                            # health-latest.html [HOST]
    brain graph-report [--json]            # static HTML graph explorer -> .brain/graph/
                                            # graph-explorer.html [HOST]
    brain sync [--publish]                 # incremental upsert + drain drafts [HOST]
    brain snapshot [--dest DIR]            # publish read-only snapshot        [HOST]
    brain rebuild [--vault DIR]            # rebuild the derived index (safe)
    brain project --dest DIR [--max-tier TIER]   # real containment: filtered copy
    brain ingest [--dry-run]                # host-broker: drain <vault>/inbox/ (ING-01/03)
    brain ingest-transcript <path> --origin O [--language L]   # host-broker (ING-04)
    brain write <relpath> [--reason R]     # host-broker, audited, fails closed
    brain verify-audit [--json]            # verify the Ed25519 chain
    brain connect --client <c> [--remove]  # SUI-02, host-broker: wire/unwire ONE
                                            # client (claude-code|claude-desktop|
                                            # codex|gemini) — diff-first, asks
                                            # before touching any user config file
    brain mcp-config [--json]              # PRINT-ONLY equivalent for the
                                            # claude-desktop MCP stanza (paste it
                                            # yourself instead of `connect` writing it)

Trust role (--role / $BRAIN_ROLE, default host): the Cowork Linux VM runs
``--role vm`` — a READ + DRAFT surface. It may run the read tools + ``status`` +
``draft-capture`` ONLY; the [HOST] commands (write/rebuild/sync/snapshot/project/
verify-audit) are refused on the VM. The VM opens only the read-only published
snapshot (never WAL) and never resolves a signing key. See AGENTS.md §6.

Egress: results are filtered to ``--max-tier``. Default on the trusted host:
the FULL vault (MNPI) — narrow with ``--max-tier`` or ``$BRAIN_DEFAULT_MAX_TIER``.
Default on ``--role vm``: Internal — the untrusted leg keeps the conservative
cap, and elevating it is the explicit human gate. Unlabelled or unrecognised
notes rank as MNPI (default-deny at any cap below MNPI). The same filter is
reused by the optional MCP adapter (a thin wrapper over this).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__, classification as cls
from . import egress
from .core import BrainCore

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


def _excluded_note(res: dict[str, Any]) -> str:
    """INT-03: name the machine-output files this build left OUT of the index.
    Silence is what let an unvalidated tree sit in retrieval unnoticed."""
    n = res.get("excluded_machine_output") or 0
    if not n:
        return ""
    from .notes import MACHINE_OUTPUT_DIRS

    return (
        f"; excluded {n} machine-output file(s) "
        f"({'/'.join(MACHINE_OUTPUT_DIRS)}, not knowledge — never indexed)"
    )


# Set True by main() on role=vm: the untrusted leg must not be told to
# "re-run with --max-tier Restricted" — that hint is the self-elevation nudge
# codex flagged, and the VM ceiling clamp makes the instruction a no-op anyway.
_SUPPRESS_ELEVATION_HINT = False


def _filter_dicts(items: list[dict], max_tier: str) -> tuple[list[dict], dict]:
    # THE single egress chokepoint — every content-returning subcommand routes
    # through egress.apply_gate so a new content path cannot silently bypass the
    # deny-by-default gate (SEC-01, r2-codex). The MCP adapter shares it too.
    surfaced, report = egress.apply_gate(items, max_tier)
    # Actionable-elevation nudge (RET-08): a starved result at the default
    # Internal cap reads to the agent as "the vault is empty" and drives it to
    # web search — leaking internal topics outward. Say WHY it's thin and HOW to
    # elevate, in the report dict so it surfaces in BOTH --json (agent-facing)
    # and the text footer. The tier stays the human gate; this only signposts it.
    if (
        report.get("withheld", 0) > 0
        and max_tier != cls.TIERS[-1]
        and not _SUPPRESS_ELEVATION_HINT
    ):
        report["hint"] = (
            f"{report['withheld']} note(s) withheld above the {max_tier} cap — "
            f"re-run with --max-tier Restricted (or MNPI for the most sensitive) "
            f"to include them, rather than treating the vault as empty."
        )
    return surfaced, report


def _freshness_block(core: Any, surfaced: list[dict], max_tier: str) -> dict | None:
    """RET-09: the "the vault continues past your hits" signal. Computed from
    the surfaced hits' valid-time dates; None when no hit carries a date (a
    hitless or dateless result has nothing to compare against). The hint only
    renders when newer material actually exists — an agent answering a
    "latest/current" question must probe past its hits before declaring the
    answer current (this is the exact failure of the 2026-07 G&P benchmark:
    a coherent-but-stale curated answer with newer sources sitting in raw/)."""
    dates = [h.get("date", "") for h in surfaced if h.get("date")]
    if not dates:
        return None
    try:
        fresh = core.source_freshness(max(dates), max_tier)
    except Exception:  # noqa: BLE001 — a freshness probe must never break search
        return None
    if fresh.get("newer_count", 0) > 0:
        fresh["hint"] = (
            f"{fresh['newer_count']} note(s)/source(s) carry dates newer than your "
            f"newest hit ({fresh['newest_hit_date']}; vault newest "
            f"{fresh['vault_newest']}). For 'latest/current' questions, probe past "
            f"these hits (brain recent, bases-query --latest-only, or a narrower "
            f"search) before treating this result as the current state."
        )
    return fresh



def build_parser() -> argparse.ArgumentParser:
    """Build the public parser while preserving command registration order."""
    from .cli_cmds import PARSER_GROUPS

    parser = argparse.ArgumentParser(
        prog="brain",
        description="Local any-LLM second brain — search/get/recent over Markdown, "
        "sourced JSON out, deny-by-default classification filter.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"brain {__version__}")
    parser.add_argument(
        "--vault", default=None, help="vault root (default: $BRAIN_VAULT or ./vault)"
    )
    parser.add_argument(
        "--role",
        default=None,
        choices=("host", "vm"),
        help="trust role (default: $BRAIN_ROLE or host). 'vm' = read+draft only: "
        "the host-broker commands are refused and the index opens read-only.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    for parser_group in PARSER_GROUPS:
        parser_group.add_parser(subparsers)
    return parser


def _make_core(args: Any, role: str) -> BrainCore:
    """Construct BrainCore with the resolved role. Tolerant of a test double that
    patched ``cli.BrainCore`` with a vault-only signature (back-compat)."""
    try:
        return BrainCore(vault=args.vault, role=role)
    except TypeError:
        return BrainCore(vault=args.vault)


def _cmd_connect(args: Any) -> int:
    """Compatibility facade for the client-wiring group's connect command."""
    from . import config
    from .cli_cmds import CommandContext
    from .cli_cmds.connect import command_connect

    role = config.role(getattr(args, "role", None))
    return command_connect(
        args,
        CommandContext(role=role, config=config, core=None),
    )


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
VM_ALLOWED = frozenset(
    {
        "init",  # filesystem-only overlay validation; safe on either role
        "doctor",  # read-only version/health inspection; no index/key touched
        # The degradation digest. VM_ALLOWED on purpose: it is the ONE surface that
        # tells a Cowork session its vault is degraded, and it reads only plain
        # files on the shared mount that `maintain` already wrote. Its host-home
        # sources are unreachable there and are REPORTED as such, never skipped.
        "alerts",
        # The page `alerts` points at. VM_ALLOWED for the same reason
        # `alerts` is: a Cowork session must be able to READ what needs
        # the owner, and on that leg it reads only the tier-gated mount
        # copy `maintain` already wrote there. It never writes, and
        # never reaches the host-only full page.
        "exceptions",
        "mcp-config",  # prints a config string; no index/key/vault read
        "search",
        "hybrid-search",
        "diagnose",
        "dossier",
        "grep",
        "bases-query",
        "graph-expand",
        "get",
        "read",
        "recent",
        "status",
        "draft-capture",
        "capture",
        "brief",
        "digest",
        # CUT-01E: the ONE COS ingress a VM holds — an UNSIGNED drop into a dir
        # `sync` never reads. Every other cos-* verb (broker, correct, evidence,
        # ingest-sweep (v2.1 host downloads sweeper),
        # priority-map, hold) is host-broker only and refused here.
        "cos-propose",
        # PRV-10: the new-vault request marker. A plain-file drop the host drain
        # completes; the VM leg still never signs, registers, or touches the
        # registry (`provision-drain` stays host-broker only).
        "provision-request",
    }
)


def _utf8_stdio() -> None:
    """Emit UTF-8 regardless of the platform's locale encoding.

    A Windows console/pipe defaults to cp1252, and `_emit` writes JSON with
    ``ensure_ascii=False`` deliberately (readable non-ASCII). Any note carrying
    a character outside Latin-1 therefore raised UnicodeEncodeError *while
    printing* -- caught by main()'s guard below and returned as a bare exit 3.
    That is the 2026-07-30 distribution-matrix Windows failure, and the same
    crash for a real Windows user searching an em-dash-free-but-∈-carrying
    vault. The payload is fine; the stream is what has to change.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # StringIO and other non-TextIOWrapper stand-ins
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # detached/closed stream — nothing to do
            pass


def main(argv: list[str] | None = None) -> int:
    global _SUPPRESS_ELEVATION_HINT
    _utf8_stdio()
    try:
        return _main(argv)
    except Exception as exc:  # H-4: top-level guard -- never a raw traceback
        raw_args = argv if argv is not None else sys.argv[1:]
        as_json = "--json" in raw_args
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if as_json
            else f"{exc.__class__.__name__}: {exc}",
            as_json,
        )
        return 3
    finally:
        # The VM hint-suppression flag is INVOCATION-scoped (set by _main per
        # role): reset it here so it never leaks into a later main() call or a
        # direct _filter_dicts caller in the same process (e.g. across tests).
        _SUPPRESS_ELEVATION_HINT = False


def _prepare_invocation(args: Any, config: Any) -> str:
    """Resolve role-sensitive defaults before the trust-boundary check."""
    if getattr(args, "rerank", "unset") is None:
        from .rerank import rerank_enabled

        args.rerank = rerank_enabled()
    role = config.role(getattr(args, "role", None))
    if getattr(args, "max_tier", "unset") is None:
        args.max_tier = (
            cls.VM_DEFAULT_MAX_TIER if role == config.ROLE_VM else cls.DEFAULT_MAX_TIER
        )
    global _SUPPRESS_ELEVATION_HINT
    _SUPPRESS_ELEVATION_HINT = role == config.ROLE_VM
    if role == config.ROLE_VM and hasattr(args, "max_tier"):
        args.max_tier = cls.clamp_to(str(args.max_tier), cls.vm_egress_ceiling())
    config.apply_role_embedder_policy(role)
    return role


def _refuse_vm_command(args: Any, role: str, config: Any) -> int | None:
    """Apply the single VM command gate before any BrainCore construction."""
    if role != config.ROLE_VM or args.cmd in VM_ALLOWED:
        return None
    msg = {
        "error": "role_forbidden",
        "role": role,
        "cmd": args.cmd,
        "detail": f"'{args.cmd}' is a host-broker command; the VM leg is read + draft only "
        f"(allowed: {sorted(VM_ALLOWED)})",
    }
    as_json = getattr(args, "json", False)
    _emit(
        msg
        if as_json
        else f"refused: '{args.cmd}' is host-broker only (role=vm is read+draft). "
        "Run it on the host.",
        as_json,
    )
    return 4


def _main(argv: list[str] | None = None) -> int:
    from . import config
    from .cli_cmds import COMMAND_GROUPS, CORELESS_COMMANDS, CommandContext

    args = build_parser().parse_args(argv)
    role = _prepare_invocation(args, config)
    refused = _refuse_vm_command(args, role, config)
    if refused is not None:
        return refused

    core = None
    if args.cmd not in CORELESS_COMMANDS:
        try:
            core = _make_core(args, role)
        except Exception as exc:  # pragma: no cover - construction is cheap/stable
            as_json = getattr(args, "json", False)
            _emit(
                {"error": type(exc).__name__, "detail": str(exc)}
                if as_json
                else f"init failed: {exc}",
                as_json,
            )
            return 3

    group = COMMAND_GROUPS.get(args.cmd)
    if group is None:  # pragma: no cover - argparse owns the command vocabulary
        return 2
    ctx = CommandContext(role=role, config=config, core=core)
    return group.run(args, ctx)



# The --help epilog text lives in cli_help.py and the read-surface render
# helpers in cli_render.py since the 2026-08-16 size ratchet; re-exported so
# every `brain.cli.<name>` caller (and the cli_cmds ``shared`` bindings) is
# unchanged.
from .cli_help import EPILOG as EPILOG  # noqa: E402,F401  (facade re-export)
from .cli_render import (  # noqa: E402,F401  (facade re-export)
    _capture_rerank_metadata as _capture_rerank_metadata,
    _egress_footer as _egress_footer,
    _render_diagnose as _render_diagnose,
    _render_explain_hit as _render_explain_hit,
    _render_variant_block as _render_variant_block,
    _variant_block as _variant_block,
)


# The ``__main__`` guard lives at the very END of the module, AFTER the
# facade re-exports above: under runpy (``python -m brain.<mod>``) the guard
# fires at its source position, and every facade name must already be bound
# by then (2026-08-16 size-ratchet fix).
if __name__ == "__main__":
    raise SystemExit(main())
