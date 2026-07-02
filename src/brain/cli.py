"""`brain` — the one universal interface any tool/harness can call.

THIS is the integration surface (not BrainCore, not MCP). It returns sourced
results as JSON and applies the deny-by-default classification filter as the
FINAL stage before stdout. A harness self-discovers the whole contract from
`brain --help` after reading one paragraph in AGENTS.md.

    brain search <query> [--json] [-k N] [--rerank] [--max-tier TIER]
    brain hybrid-search <query> ...        # alias of search (fused RRF BM25+dense)
    brain grep <pattern> [--regex] [-k N]  # lexical-first, NO embedding
    brain bases-query --where k=v [-k N]   # structured frontmatter view, NO embedding
    brain graph-expand <id...> [--depth D] # wikilink-BFS + PPR, DISCOVERY-ONLY
    brain get <id> [--json] [--max-tier TIER]
    brain read <id>                        # alias of get
    brain recent [--json] [-n N] [--max-tier TIER]
    brain draft-capture [--id ID] [--source]   # VM-side capture: stage a DRAFT
    brain status [--json]                  # snapshot gen/age + pending drafts
    brain sync [--publish]                 # incremental upsert + drain drafts [HOST]
    brain snapshot [--dest DIR]            # publish read-only snapshot        [HOST]
    brain rebuild [--vault DIR]            # rebuild the derived index (safe)
    brain project --dest DIR [--max-tier TIER]   # real containment: filtered copy
    brain write <relpath> [--reason R]     # host-broker, audited, fails closed
    brain verify-audit [--json]            # verify the Ed25519 chain

Trust role (--role / $BRAIN_ROLE, default host): the Cowork Linux VM runs
``--role vm`` — a READ + DRAFT surface. It may run the read tools + ``status`` +
``draft-capture`` ONLY; the [HOST] commands (write/rebuild/sync/snapshot/project/
verify-audit) are refused on the VM. The VM opens only the read-only published
snapshot (never WAL) and never resolves a signing key. See AGENTS.md §6.

Egress: results are filtered to ``--max-tier`` (default: Internal). Unlabelled
or unrecognised notes are treated as Secret and withheld (default-deny). Surfacing
Restricted/Secret requires an explicit ``--max-tier`` elevation — the human gate.
The same filter is reused by the optional MCP adapter (a thin wrapper over this).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import classification as cls
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

examples:
  brain grep "sqlite-vec" --json
  brain bases-query --where type=note --where classification=Internal --json
  brain search "arctic embed" --rerank --json
  brain graph-expand brain-engine --depth 2 --json
  brain get arctic-embed-choice --json
  brain recent -n 5 --max-tier Confidential
  brain --vault ./vault rebuild
  brain --vault ./vault project --dest /tmp/vm-workspace --max-tier Internal

egress filter (deny-by-default):
  tiers low->high: Public < Internal < Confidential < Restricted < Secret
  default --max-tier is Internal; unlabelled notes => Secret => withheld.
  the filter is the final stage before stdout. it is an egress DECISION, not
  containment — a file-capable harness reads Markdown directly; use
  `brain project` (a filtered workspace copy) for real containment.
  JSON `egress.total` INCLUDES withheld notes by design (it is an audit count,
  not a leak of content); `egress.surfaced` is what was printed.
"""


def _emit(obj: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
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
    sp.add_argument("-k", type=int, default=50, help="max results (default: 50)")
    add_common(sp)

    sp = sub.add_parser("graph-expand", help="wikilink-BFS + PPR multi-hop expansion — DISCOVERY-ONLY (RET-03)")
    sp.add_argument("seeds", nargs="+", help="seed note id(s)")
    sp.add_argument("--depth", type=int, default=2, help="BFS hop depth (default: 2)")
    sp.add_argument("-k", type=int, default=10, help="max candidates (default: 10)")
    sp.add_argument("--no-ppr", action="store_true", help="BFS only, skip Personalized PageRank")
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

    sp = sub.add_parser("write", help="host-broker: write a note (audited, fails closed)")
    sp.add_argument("relpath")
    sp.add_argument("--content", default=None, help="content (default: read stdin)")
    sp.add_argument("--reason", default="")
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
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "digest",
        help="weekly digest: notes added/updated in the past N days (UX-02)",
    )
    sp.add_argument("--days", type=int, default=7, help="lookback period in days (default: 7)")
    sp.add_argument("--json", action="store_true")

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
VM_ALLOWED = frozenset({
    "search", "hybrid-search", "grep", "bases-query", "graph-expand",
    "get", "read", "recent", "status", "draft-capture",
    "capture", "brief", "digest",
})


def main(argv: list[str] | None = None) -> int:
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
        items = core.bases_query(filters, k=args.k)
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
            args.seeds, depth=args.depth, k=args.k, use_ppr=not args.no_ppr)
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
        res = core.draft_capture(content, ident=args.id, is_source=args.source)
        _emit(res if args.json else
              f"staged draft {res['id']} -> {res['draft']} "
              f"(signed={res['signed']}, indexed={res['indexed']}); "
              f"host drain will sign + index + snapshot", args.json)
        return 0

    if cmd == "rebuild":
        res = core.rebuild()
        _emit(res if args.json else
              f"indexed {res['indexed']} notes ({res['chunks']} chunks) via "
              f"{res['backend']} [{res['embed_model']} d={res['embed_dim']}] -> {res['db']}",
              args.json)
        return 0

    if cmd == "sync":
        res = core.sync(drain=not args.no_drain, publish=args.publish)
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
        res = core.publish_snapshot(args.dest)
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
            ix, sn = res.get("index", {}), res.get("snapshot", {})
            _emit(None, False,
                  f"index: {ix.get('notes','?')} notes / {ix.get('chunks','?')} chunks "
                  f"[{ix.get('embed_model','?')} d={ix.get('embed_dim','?')}]\n"
                  f"snapshot: {sn.get('snapshot','?')} "
                  + (f"gen {sn.get('generation')} age {sn.get('age_human')}"
                     if sn.get('snapshot') == 'present' else ''))
        return 0

    if cmd == "project":
        from .projection import project_workspace

        res = project_workspace(core.vault, args.dest, max_tier=args.max_tier).to_dict()
        _emit(res if args.json else
              f"projected {res['copied']} notes (<= {res['max_tier']}) to {res['dest']}; "
              f"excluded {res['excluded']} ({res['excluded_unlabelled']} unlabelled)",
              args.json)
        return 0

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

    if cmd == "brief":
        res = core.brief(max_recent=args.n, drain=not args.no_drain)
        if args.json:
            _emit(res, True)
        else:
            from .brief import format_brief
            _emit(None, False, format_brief(res))
        return 0

    if cmd == "digest":
        res = core.digest(days=args.days)
        if args.json:
            _emit(res, True)
        else:
            from .brief import format_digest
            _emit(None, False, format_digest(res))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
