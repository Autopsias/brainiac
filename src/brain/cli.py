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
import os
import sys
import time
from typing import Any

from . import __version__, classification as cls
from . import connect as _connect
from . import core as core_mod
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

ADR-0008 exact identity:
  search/hybrid-search use fused RRF(k=60): BM25 + dense + a bounded exact
  alias/title leg. The exact leg never trusts FTS token-OR membership for phrase
  claims: aliases/titles are normalized identity projections, title phrases are
  verified as contiguous title-token spans, and keyword_exact uses a literal
  boundary verifier. Emergency retrieval rollback is immediate:
    BRAIN_EXACT_LEG_ENABLED=0 <restart the invoking process>
  No rebuild is needed. With the switch off, exact ranking injection/pinning/
  collision slot normalization are disabled, while already-surfaced organic
  hits can still carry truthful evidence/create_safety labels.

BR-03 rerank default (owner ruling 2026-08-04, window revised same day):
  search/hybrid-search rerank ON by default, cross-encoder candidate window
  20 — measured on the 66-query golden set (eval/FOLLOWUPS.md #4): mrr@10
  0.267 -> 0.411, hit@1 0.212 -> 0.349, for ~5.5s p50 / 8.2s p95 added
  latency the owner has explicitly accepted for the quality gain. The window
  briefly shipped at 50 on a latency figure that turned out to be the
  window-20 row mislabelled; at 50 it really costs p50 68.0s and 85% of
  queries blow the 30s timeout and get the BARE ordering anyway
  (eval/FOLLOWUPS.md #6). Window 20 is the latency actually accepted and it
  reranks every query. The CEILING stays 50, so BRAIN_RERANK_TOP /
  BRAIN_RERANK_MAX still opt into the wide pass deliberately. Skippable per
  call (--no-rerank) or globally:
    BRAIN_RERANK_DISABLED=1 <restart the invoking process>
  An explicit --rerank/--no-rerank always wins over the env var. A slow
  rerank call is also caller-bounded: it degrades to the pre-rerank order
  after $BRAIN_RERANK_TIMEOUT_S (default 30s) rather than hanging the
  caller — the model can't be interrupted mid-call, so the slow call keeps
  running in the background and its result is discarded (BrainIndex._rerank_impl).
  At window 20 the slowest golden query measures 10.5s, so 30s is ~2.9x the
  measured worst case; raise it alongside any wide-candidate pass.
  BRAIN_RERANK_MAX raises the window ceiling further; BRAIN_RERANK_TOP
  overrides the requested window size itself.

RK-02 adaptive rerank gate (2026-08-04):
  reranking is default-on but not always worth what it costs. When ADR-0008
  pins a UNIQUE full alias/title owner, rank 1 is already decided and the
  cross-encoder may not touch it — so search SKIPS reranking on those
  queries. Measured on the same 66-query golden set
  (eval/runs/rerank-gate-calibration-2026-08-04.json): the 7 queries it
  fires on score IDENTICALLY to always-on — recall@10/recall@20/mrr@10/hit@1
  all +0.0000 (checked against both the window-20 and window-50 arms) —
  while their latency drops from a 6.2s median to a 200ms median at the
  shipped window 20. Force unconditional reranking back on per
  call with --no-rerank-gate, or globally with
    BRAIN_RERANK_GATE_DISABLED=1 <restart the invoking process>
  An explicit --rerank-gate/--no-rerank-gate always wins over the env var.
  `search --explain --json` reports the decision under
  ranking.rerank_gate (enabled/skipped/reason).

RET-01 zone-authority prior — a measured OPT-IN, off by default (s06, 2026-08-04):
  RRF sums 1/(60+rank) across legs, so a document present weakly in TWO legs
  outranks one present strongly in ONE. That is why a Portuguese question
  against English notes loses: its BM25 leg finds nothing (no shared tokens),
  the dense leg has the answer at median rank 2, and the fused ranking exits
  it at median rank 52 (eval/FOLLOWUPS.md #7). The engine carries a
  counterweight — a multiplicative per-zone boost applied AFTER fusion, only
  to dense-leg-only hits (scope=semantic_only, so an exact/lexical hit's OWN
  factor stays 1.0). That protects the exact hit's SCORE, not its RANK:
  boosting its neighbours can still out-rank it, and measurably does —
  lexical_identifier mrr@10 falls 0.750 -> 0.725 at W=3.0 (and 0.631 at 5.0)
  while its recall@10 holds flat. It is NEUTRAL out of the box.
  Arm it per vault, keyed on that vault's ZONE NAMES:
    BRAIN_ZONE_WEIGHTS='{"brain": 2.5, "raw": 1.0}' <restart the process>
  Measured on the 66-query golden set, weight selected on its pre-registered
  TRAIN half and read ONCE on the held-out half (eval/FOLLOWUPS.md #9,
  eval/zone_prior_calibration.py): held-out mrr@10 0.198 -> 0.386, paired
  permutation p=0.011; cross_lingual_pt_en recall@10 0.000 -> 0.458 at W=3.0.
  NO REBUILD — it is a query-time multiplier, it never touches embed_model /
  embed_dim; unset it and the next process is back to shipped behaviour.
  It ships OFF because 66 queries can resolve the DIRECTION but not the
  CONSTANT: the train half's argmax is 3.0, the held-out half's is 5.0, and
  the observed effect sits below the minimum this n could reliably detect
  (MDE 0.199 at 80% power). 2.0-3.0 is the evidenced range, not a calibrated
  value. Costs at 3.0: temporal mrr@10 0.431 -> 0.344 (and one gold document
  out of the top 10), cross-lingual-ES recall@10 0.250 -> 0.167, identifier
  precision as above. At 4.0-5.0 those become a collapse — identifier mrr@10
  0.665/0.631, temporal mrr@10 0.317/0.179, cross-lingual-ES recall@10 0.000
  at 4.0 — while the aggregate barely moves. That is the ceiling.
  CONFIRM IT ENGAGED: `search --explain --json` reports each hit's
  zone.applied / zone.factor (an unknown zone key resolves to 1.0). A
  malformed BRAIN_ZONE_WEIGHTS / BRAIN_ZONE_SCOPE prints one stderr warning
  and is dropped; an unrecognised scope fails safe to semantic_only.
  NOTE for tuning: _resolve_zone prefers a note's `source_zone:` frontmatter
  and falls back to the flattened `brain`/`raw` zone column. On a vault whose
  notes carry no `source_zone` (the reference vault: 0 of its 2,570 indexed
  notes), keys other than `brain`/`raw` are silently a no-op. Zone factors
  must be finite and within [1e-6, 1e6]. BRAIN_ZONE_SCOPE = all |
  semantic_only (default semantic_only); BRAIN_ZONE_SOURCE_MODE=column
  disables the frontmatter lookup.

--variant (RET-05 multi-query fan-out, wired 2026-08-09):
  ask the SAME question in several phrasings and fuse the answers:
    brain search "what did we decide about pricing" \
        --variant "o que decidimos sobre precos" --explain --json
  Each variant runs its own full hybrid search (shallow, per_query_k 20 — a
  wider per-variant fetch measurably HURTS: 20->80 dropped fan-out recall
  0.736->0.625) and the result lists are Reciprocal-Rank-Fused into one
  ranking. The ORIGINAL query is always variant 0 and keeps every ADR-0008
  guarantee: only IT can pin rank 1 or report create_safety `exists`, so a
  mistranslated variant that happens to match an unrelated note's title can
  never tell a capture agent that note already exists (it is capped at
  `probable`). The egress gate applies ONCE, to the pooled result.
  TWO CONSTANTS: the INNER per-variant fusion is unchanged (RRF_K_FUSE, with
  rrf_k=60 as ADR-0008's exact-leg key); the OUTER pooling constant is
  separate and moves only via BRAIN_MULTI_RRF_K (default 60) — deliberately
  not a flag, since a non-60 rrf_k would silently disable the exact leg.
  Guards: identical variants deduplicate, an empty variant contributes
  nothing, variants past BRAIN_MULTI_MAX_VARIANTS (default 4) are dropped
  from the TAIL and reported (supply variants in descending vault-language
  prevalence), BRAIN_MULTI_GUARD=1 arms the correlated-vote guard (a document
  some variant ranked in its top 3 outranks one no variant did), and
  BRAIN_VARIANTS_ENABLED=0 is the kill switch.
  --rerank-fused runs the cross-encoder once over the POOLED candidates
  against the original query. Owner ruling 2026-08-12: it is ON BY DEFAULT
  whenever 2+ variants survive the guards, and NEVER on a single query — the
  single-query ranking is untouched. Evidence: +0.0643 recall@10 over the
  shipped configuration (p 0.0284, 6 wins / 1 loss), TRAIN-HALF ONLY and not
  confirmed on a held-out half. Costs one extra cross-encoder pass (~5-25s) on
  fan-out calls. Opt out per call with --no-rerank-fused;
  BRAIN_RERANK_FUSED_DISABLED=1 is the global kill switch (restart the invoking
  process; no rebuild).
  CONFIRM IT ENGAGED: `--explain --json` reports `variants`
  (count, dropped sets, both constants, guard, pin, rerank gate, and each
  variant's rank/contribution for every surfaced hit).

evidence/create_safety:
  every surfaced search hit carries one evidence label: alias_hit,
  exact_title_match, title_phrase_match, keyword_exact, high_vector_match, or
  weak_semantic. create_safety is exists/probable/unknown. `exists` is reserved
  for one visible unique full alias/title owner; alias/title collisions, retired
  owners, or any full owner withheld by egress degrade the public answer without
  exposing hidden ids, owner counts, ranks, titles, or a collision label.

duplicate-family collapse (HYG-01):
  the same bytes indexed twice (a copy and its date-stamped re-ingest) split one
  document's ranking signal across two near-identical vectors. Ranked retrieval
  folds such a family into its canonical member BEFORE the legs are scored, so
  the canonical inherits the family's best rank instead of two halves; the
  absorbed ids ride on the surviving hit as `duplicates`, never as extra slots.
  DELIBERATELY NARROW: a family needs byte-identical bodies AND an explicit
  owner-accepted supersession link AND >= $BRAIN_FAMILY_MIN_BODY (1024) bytes of
  body. Name/near-body similarity is NOT collapsed — AGENTS.md §4 CUR-01 makes
  that a PROPOSAL for the owner, not a ranking-time identity claim — and short
  bodies are refused because `[no text detected]` extraction stubs share one
  hash across unrelated documents. Two genuine revisions are never one family.
  `search --explain --json` reports ranking.family_collapse
  (enabled/collapsed/declined); `BRAIN_FAMILY_COLLAPSE_DISABLED=1` is the
  rollback and needs no rebuild.

explain / diagnose:
  `search --explain` emits gated per-hit attribution (lexical/dense/exact
  contributions, raw RRF, zone/staleness, rerank, pin, near-dup) plus a bounded
  candidate digest whose ids are also egress-surfaced. `diagnose` runs the same
  production ranking and then reports a target's stage presence/rank/cutoff;
  if the target is above the egress cap, the target prints only as `withheld`.
  rerank is skippable and bounded to the top 10-50 (ON by default at window
  20 — see BR-03
  above); unique full identities are pinned outside the reranker, collision
  groups keep live-before-retired order only inside the slots the reranker
  selected, and reranker scores remain separate from RRF scores.

host query log / replay:
  host query capture is post-egress, best-effort, and HOST ONLY. It writes raw
  queries under the resolved host app-data index directory at `query-log/`,
  deliberately outside vault/ and vault/.brain/, with owner-only directories
  and files; unsafe symlink/env overrides into the vault are refused. Retention
  removes whole month JSONL files. VM role cannot read, write, resolve, or
  replay the host ledger. `brain eval replay --against FILE --json` never
  recaptures; it reports top1/Jaccard/rank/latency telemetry split into
  vault_same and drift_or_mixed. Thresholds apply only to vault_same because the
  log has no target qrels.

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

retrieval discipline (non-negotiable; details in AGENTS.md §5):
  - every search hit carries `type` — the AUTHORITY signal. A `type: decision`
    hit IS the recorded decision layer; a `type: source` hit (memos, decks,
    drafts) is material under consideration and NEVER overturns a decision on
    its own. Conflict between a newer source and a decision note? Report the
    tension — never promote the proposal.
  - decision-state questions ("what have we decided", "latest decisions",
    "current state of X"): `brain dossier "<question>" --json` is the
    ONE-CALL sweep — decision layer + sources + tensions (newer sources
    post-dating a decision) + freshness, retired versions pre-excluded.
    (`bases-query --where type=decision --latest-only` remains the raw
    probe.) The newest DOCUMENT VERSION is not the newest DECISION STATE.
  - react to the search response's `freshness` block ("N sources newer than
    your newest hit"): probe past your hits (recent / --latest-only / a
    narrower search) before answering a "latest/current" question.
  - a `-- N withheld` egress line means elevate --max-tier, not "vault empty".

examples:
  brain grep "sqlite-vec" --json
  brain bases-query --where type=note --where classification=Internal --json
  brain bases-query --latest-only --where type=note --json
  brain bases-query --as-of 2026-03-01 --json
  brain search "arctic embed" --rerank --explain --json
  brain diagnose "Café Aurora" --target cafe-aurora --json
  brain eval replay --against ~/.local/share/brainiac/<vault-id>/query-log/2026-07.jsonl --json
  brain graph-expand brain-engine --depth 2 --json
  brain get arctic-embed-choice --json
  brain recent -n 5 --max-tier Confidential
  brain --vault ./vault rebuild
  brain --vault ./vault supersede arctic-embed-choice e5-small-choice --reason "switched embedder"
  brain --vault ./vault project --dest /tmp/vm-workspace --max-tier Internal

egress filter (deny-by-default below the cap):
  tiers low->high: Public < Internal < Confidential < Restricted < MNPI
  default --max-tier: full vault (MNPI) on host, Internal on --role vm;
  unlabelled notes rank as MNPI (withheld at any lower cap).
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


def _excluded_note(res: dict[str, Any]) -> str:
    """INT-03: name the machine-output files this build left OUT of the index.
    Silence is what let an unvalidated tree sit in retrieval unnoticed."""
    n = res.get("excluded_machine_output") or 0
    if not n:
        return ""
    from .notes import MACHINE_OUTPUT_DIRS

    return (f"; excluded {n} machine-output file(s) "
            f"({'/'.join(MACHINE_OUTPUT_DIRS)}, not knowledge — never indexed)")


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
    if (report.get("withheld", 0) > 0 and max_tier != cls.TIERS[-1]
            and not _SUPPRESS_ELEVATION_HINT):
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


def _egress_footer(report: dict) -> str:
    """The `-- N/M surfaced; K withheld` line, plus the elevation hint when the
    gate withheld anything (RET-08). One renderer so every read surface nudges
    identically."""
    line = (f"-- {report['surfaced']}/{report['total']} surfaced; "
            f"{report['withheld']} withheld (max-tier={report['max_tier']})")
    if report.get("hint"):
        line += f"\n-- {report['hint']}"
    return line


def _variant_block(fanout: dict, allowed_ids: set[str], *, explain: bool) -> dict:
    """Project a RET-05 fan-out trace onto the ALREADY-GATED result.

    The trace is built pre-egress, so every id in it (per-variant orders,
    per-variant contributions, the pin) is filtered to ``allowed_ids`` here — a
    withheld note must not leak through the fan-out attribution any more than
    through the ranking itself. Variant TEXTS are the caller's own input, never
    vault content, so they are echoed verbatim.
    """
    dropped = {key: value for key, value in fanout["dropped"].items()
               if key == "max_variants" or value}
    block = {
        "used": fanout["variants"],
        "count": fanout["variant_count"],
        "dropped": dropped,
    }
    if not explain:
        return block
    block.update({
        # Both constants, side by side: the outer one is what this layer pools
        # at, the inner one is ADR-0008's pin that gates the exact leg.
        "fanout_k": fanout["fanout_k"],
        "inner_rrf_k": fanout["inner_rrf_k"],
        "exact_leg_enabled": fanout["exact_leg_enabled"],
        "per_query_k": fanout["per_query_k"],
        "guard": fanout["guard"],
        "rerank_fused": fanout["rerank_fused"],
        "rerank_fused_source": fanout["rerank_fused_source"],
        "rerank_gate": fanout["rerank_gate"],
        # A pin is only ever the ORIGINAL query's unique identity owner, and it
        # is named here only when that note also survived egress.
        "pin": (fanout["pin"] if fanout["pin"]["id"] in allowed_ids
                else {**fanout["pin"], "id": None}),
        "per_variant": [
            {**entry, "order": [i for i in entry["order"] if i in allowed_ids]}
            for entry in fanout["per_variant"]
        ],
        "contributions": {
            note_id: [{**c, "contribution": round(c["contribution"], 6)} for c in contribs]
            for note_id, contribs in fanout["contributions"].items()
            if note_id in allowed_ids
        },
    })
    return block


def _render_variant_block(block: dict) -> list[str]:
    """The text-mode fan-out footer — what ran, and what was dropped."""
    lines = [f"-- fan-out: {block['count']} variant(s): "
             + "; ".join(repr(v) for v in block["used"])]
    for key in ("duplicate", "over_cap", "kill_switch"):
        dropped = block["dropped"].get(key)
        if dropped:
            lines.append(f"-- dropped ({key}): " + "; ".join(repr(v) for v in dropped))
    if "fanout_k" in block:
        guard = block["guard"]
        gate = block["rerank_gate"]
        lines.append(
            f"-- pooled at fanout_k={block['fanout_k']} "
            f"(inner rrf_k={block['inner_rrf_k']}, "
            f"exact_leg={'on' if block['exact_leg_enabled'] else 'off'}, "
            f"per_query_k={block['per_query_k']}); "
            f"guard={'on' if guard['enabled'] else 'off'}; "
            f"rerank_fused={'on' if block['rerank_fused'] else 'off'} "
            f"[{block.get('rerank_fused_source', 'caller')}] "
            f"(gate: {gate['reason']}); pin={block['pin']['id']}")
        for note_id, contribs in block["contributions"].items():
            votes = " ".join(f"v{c['variant']}@{c['rank']}" for c in contribs)
            lines.append(f"--   {note_id}: {votes}")
    return lines


def _render_explain_hit(hit: dict) -> list[str]:
    """Readable ADR-0008 attribution for one already-gated search result."""
    explain = hit.get("explain") or {}
    lines = [
        f"[{hit.get('source', '?')}] {hit.get('id', '?')}  "
        f"final-rank={explain.get('final_rank')}  "
        f"pre-rerank={explain.get('pre_rerank_score')}"
    ]
    for name in ("lexical", "dense", "exact"):
        leg = explain.get(name)
        if leg is None:
            lines.append(f"  {name}: not available")
            continue
        details = " ".join(f"{key}={value}" for key, value in leg.items())
        lines.append(f"  {name}: {details}")
    zone = explain.get("zone", {})
    stale = explain.get("staleness", {})
    duplicate = explain.get("near_duplicate", {})
    pin = explain.get("pin", {})
    lines.append(
        "  raw_rrf={raw} zone={zone} (applied={applied}, scope={scope}) "
        "staleness={staleness}".format(
            raw=explain.get("raw_rrf_score"), zone=zone.get("factor"),
            applied=zone.get("applied"), scope=zone.get("scope"),
            staleness=stale.get("factor"),
        )
    )
    lines.append(
        "  duplicate: exempt={exempt} suppressed={suppressed}; "
        "pin: eligible={eligible} applied={applied}".format(
            exempt=duplicate.get("exempt"), suppressed=duplicate.get("suppressed"),
            eligible=pin.get("eligible"), applied=pin.get("applied"),
        )
    )
    if explain.get("rerank_score") is None:
        lines.append("  rerank: not scored (no numeric score is combined with RRF)")
    else:
        lines.append(
            f"  rerank: score={explain['rerank_score']} rank={explain['rerank_rank']} "
            "(separate cross-encoder scale)"
        )
    lines.append(f"    {hit.get('snippet', '')}")
    return lines


def _render_diagnose(diag: dict, report: dict) -> str:
    """Readable target-miss result without exposing a withheld target."""
    if diag.get("verdict") == "withheld":
        return _egress_footer(report) + "\nVERDICT: withheld by egress gate"
    trace = diag.get("trace", {})
    lines = [f"target: {diag.get('target')}"]
    for name, stage in trace.get("stages", {}).items():
        lines.append(
            f"  {name}: candidate={stage.get('candidate')} rank={stage.get('rank')} "
            f"matched={stage.get('matched')} cutoff={stage.get('cutoff')}"
        )
    if trace.get("first_missed_cutoff"):
        cutoff = trace["first_missed_cutoff"]
        lines.append(f"  first missed cutoff: {cutoff['stage']} (limit={cutoff['cutoff']})")
    attribution = trace.get("attribution")
    if attribution:
        lines.extend(_render_explain_hit({"id": diag.get("target"), "explain": attribution}))
    verdict = diag.get("verdict", "candidate-miss")
    lines.append(_egress_footer(report))
    lines.append(f"VERDICT: {verdict}")
    return "\n".join(lines)


def _capture_rerank_metadata(core: Any, trace: Any | None, args: Any) -> dict[str, Any]:
    """Small, safe record of the rerank mode used for a captured query."""
    requested = bool(getattr(args, "rerank", False))
    applied = bool(getattr(trace, "rerank_applied", False))
    model = None
    if applied:
        cache = getattr(getattr(core, "index", None), "_reranker_cache", None)
        if isinstance(cache, tuple) and len(cache) == 2:
            model = getattr(cache[1], "model_id", None) or cache[0]
    top_n = int(getattr(args, "rerank_top", 0) or 0) if applied else 0
    return {"requested": requested, "applied": applied,
            "model": str(model) if model else None, "top_n": top_n}


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
            "--max-tier", default=None, choices=cls.TIERS,
            help="egress cap; results above this tier are withheld "
                 f"(default: {cls.DEFAULT_MAX_TIER} on host, "
                 f"{cls.VM_DEFAULT_MAX_TIER} on --role vm)",
        )

    def add_search(name: str, help_text: str) -> None:
        # EPILOG is entirely about the ranking these two verbs run (exact leg,
        # rerank, rerank gate, zone prior, evidence labels, explain/diagnose),
        # and AGENTS.md/CHANGELOG send readers to `brain search --help` for it.
        # Attaching it here is what makes that pointer true.
        sp = sub.add_parser(name, help=help_text, epilog=EPILOG,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.add_argument("query")
        sp.add_argument("-k", type=int, default=10, help="max results (default: 10)")
        # BR-03 (owner ruling 2026-08-04): rerank ships ON by default. default=None
        # (not True) so _main can tell "not typed" apart from an explicit --rerank,
        # which is what lets an explicit flag win over $BRAIN_RERANK_DISABLED.
        sp.add_argument("--rerank", dest="rerank",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="re-order the top results with the cross-encoder (RET-02); "
                             "ON by default (window 20) — skippable, degrades to the "
                             "pre-rerank order if the model is absent or a call exceeds "
                             "its timeout budget. --no-rerank opts out per call; "
                             "BRAIN_RERANK_DISABLED=1 is the global kill switch "
                             "(mirrors BRAIN_EXACT_LEG_ENABLED) — an explicit "
                             "--rerank/--no-rerank always wins over the env var")
        sp.add_argument("--rerank-top", type=int, default=20,
                        help="rerank window, clamped to 10-50 by default "
                             "(BRAIN_RERANK_MAX raises the ceiling further; "
                             "default: 20 — a wider window costs strongly "
                             "super-linearly, see eval/FOLLOWUPS.md #6)")
        # RK-02 (2026-08-04): default=None means "not typed", so an explicit
        # flag can win over $BRAIN_RERANK_GATE_DISABLED — same shape as --rerank.
        sp.add_argument("--rerank-gate", dest="rerank_gate",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="adaptive rerank gate (RK-02): skip the "
                             "cross-encoder on a query whose rank 1 is already "
                             "fixed by a unique exact-identity pin, where it is "
                             "measurably worth nothing. ON by default; "
                             "--no-rerank-gate (or BRAIN_RERANK_GATE_DISABLED=1) "
                             "forces unconditional reranking back on")
        sp.add_argument("--rrf-k", type=int, default=60,
                        help="Reciprocal Rank Fusion constant (default: 60)")
        # RET-05 fan-out (wired 2026-08-09). Repeatable; the ORIGINAL query stays
        # the first variant, so identity/create-safety semantics keep their
        # trusted anchor. The OUTER pooling constant is deliberately NOT a CLI
        # arg — it moves only via $BRAIN_MULTI_RRF_K, so no flag can be typed
        # that silently disables the ADR-0008 exact leg.
        sp.add_argument("--variant", action="append", default=None, metavar="TEXT",
                        help="an alternative phrasing of the same question "
                             "(repeatable) — each is searched separately and the "
                             "result lists are rank-fused into one ranking "
                             "(RET-05). Use it when the question's words are "
                             "yours rather than the notes' (a translation, a "
                             "synonym expansion). Identical variants are "
                             "deduplicated; variants past "
                             f"{core_mod.MULTI_MAX_VARIANTS} "
                             "($BRAIN_MULTI_MAX_VARIANTS) are dropped from the "
                             "tail and reported; BRAIN_VARIANTS_ENABLED=0 is the "
                             "kill switch")
        sp.add_argument("--rerank-fused", dest="rerank_fused",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="with --variant: run the cross-encoder ONCE over the "
                             "pooled candidates against the original query "
                             "(RET-05b) instead of leaving the fused RRF order. "
                             "ON by default whenever 2+ variants are supplied, and "
                             "NEVER on a single query (owner ruling 2026-08-12); "
                             "--no-rerank-fused opts out per call and "
                             "BRAIN_RERANK_FUSED_DISABLED=1 is the global kill "
                             "switch")
        sp.add_argument("--explain", action="store_true",
                        help="show per-stage RRF/zone/staleness attribution for each "
                             "egress-surfaced result (ADR-0008)")
        add_common(sp)

    sp = sub.add_parser(
        "eval",
        help="host-only retrieval evaluation utilities (real-query replay never writes the ledger)",
        description=(
            "Host-only retrieval evaluation. Real-query replay reads a private "
            "query-log export and never appends capture records. It has no "
            "target qrels: matching live-index fingerprints are reported as "
            "vault_same ranking/configuration signals; changed fingerprints are "
            "drift_or_mixed and remain report-only."
        ),
    )
    eval_sub = sp.add_subparsers(dest="eval_cmd", required=True)
    replay = eval_sub.add_parser(
        "replay",
        help="replay a host query-log export: stability, overlap, rank movement, and latency",
        description=(
            "Replay a private host query-log JSONL export without writing any "
            "new capture records. Reports Jaccard@k, top-1 stability, rank "
            "movement, candidate-digest presence, and latency delta, separated "
            "into vault_same and drift_or_mixed fingerprint groups. The log has "
            "no target qrels. Thresholds are optional and are evaluated only "
            "over vault_same records; an empty comparable subset exits successfully."
        ),
    )
    replay.add_argument("--against", required=True,
                        help="host query-log JSONL month file to replay")
    replay.add_argument("--fail-under-top1", type=float, default=None,
                        help="fail only if vault_same top-1 stability is below [0,1]; "
                             "drift_or_mixed rows are report-only")
    replay.add_argument("--fail-under-jaccard", type=float, default=None,
                        help="fail only if vault_same Jaccard@k is below [0,1]; "
                             "the log has no target qrels")
    replay.add_argument("--json", action="store_true")

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
    sp.add_argument("--no-seed-vault", dest="seed_vault", action="store_false",
                    help="[--full] do NOT seed a genuinely empty vault with the 3 "
                         "generic sample notes")
    sp.add_argument("--import-from", default=None,
                    help="[--full, host only] guided first-ingest: stage an existing "
                         "folder of documents (e.g. an Obsidian vault) into this "
                         "vault's inbox/ and run the standard ingest drain. Prints a "
                         "dry-run manifest (file count/bytes/extensions) first; pass "
                         "--yes to actually stage + ingest. Refused on --role vm.")
    sp.add_argument("--yes", action="store_true",
                    help="[--import-from] skip the interactive y/N confirmation")
    sp.add_argument("--import-force", action="store_true",
                    help="[--import-from] override the default safety caps "
                         "(5000 files / 500 MB)")
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
    sp.add_argument("--check-registry", action="store_true",
                    help="host only: add the 'PyPI registry drift' row (repo tag / "
                         "installed / latest-published-on-PyPI) via a single cached "
                         "HTTPS metadata read. Off by default — this is the only "
                         "network call `doctor` ever makes, and only with this flag.")

    sp = sub.add_parser(
        "alerts",
        help="READ-ONLY degradation digest every harness can call: auto-update "
             "state, weekly-synthesis task health, engine-feedback backlog, the "
             "owner-decision queue, and the notify markers `brain maintain` "
             "writes. Pure file reads — no index, embedder, network or key. "
             "role=vm reads only its own vault and REPORTS the two host-home "
             "sources it cannot reach, so silence never means 'could not look'.",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--one-line", action="store_true",
                    help="emit the single-line banner form a SessionStart hook "
                         "injects (empty output when all clear)")

    sp = sub.add_parser(
        "mcp-config",
        help="print the MCP-client config entry to run brain-mcp against this "
             "vault (paste into Claude Desktop / Claude Code mcpServers). "
             "Read-only; no index or key touched.",
    )
    sp.add_argument("--name", default="brainiac",
                    help="server name/key in the config (default: %(default)s) — "
                         "use a distinct name per vault")
    sp.add_argument("--max-tier", default="Internal",
                    help="egress ceiling for this MCP server (default: %(default)s "
                         "= conservative LLM-facing cap; explicitly raise only "
                         "for an approved server)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "connect",
        help="SUI-02: wire ONE client (claude-code|claude-desktop|codex|gemini) to "
             "this vault — shows a diff, asks before touching any user config file, "
             "idempotent (re-run says 'already connected'). Host-only, "
             "self-executing (not print-only — that's `mcp-config`). "
             "`--remove` unwires the same client.",
    )
    sp.add_argument("--client", required=True, choices=list(_connect.CLIENTS))
    sp.add_argument("--target", default=".",
                    help="project directory being wired (default: cwd) — where "
                         "CLAUDE.md/AGENTS.md/.gemini/settings.json live")
    sp.add_argument("--name", default="brainiac",
                    help="MCP server name for --client claude-desktop (default: %(default)s)")
    sp.add_argument("--max-tier", default="Internal",
                    help="egress ceiling baked into the claude-desktop MCP stanza "
                         "(default: %(default)s = conservative cap, matches "
                         "`mcp-config`)")
    sp.add_argument("--marketplace-source", default=_connect.DEFAULT_MARKETPLACE_SOURCE,
                    help="source passed to `claude plugin marketplace add` for "
                         "--client claude-code (default: %(default)s)")
    sp.add_argument("--remove", action="store_true", help="unwire this client instead of wiring it")
    sp.add_argument("--yes", action="store_true",
                    help="skip the interactive y/N confirmation (required when not a TTY)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "update",
        help="the ONE 'get current' command (ADR-0005 Ruling 3, UP-01/UP-02): "
             "marketplace refresh -> downgrade-safe CLI-plugin reinstall -> "
             "engine venv refresh -> workspace re-stage -> `brain doctor` "
             "verify, one before->after table, one pass/fail (host only)",
    )
    sp.add_argument("--marketplace", default="brainiac",
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
    add_search("search", "fused RRF(60) BM25 + dense + exact alias/title retrieval — "
                         "hits carry type/date/is_latest_version/evidence/"
                         "create_safety; --explain emits gated attribution")
    add_search("hybrid-search", "alias of `search`: fused RRF(60) BM25 + dense "
                                "+ exact alias/title leg (ADR-0008)")

    sp = sub.add_parser(
        "diagnose",
        help="ADR-0008 target miss tracer: run production search unchanged, then "
             "report the target's gated per-stage presence/cutoff; withheld "
             "targets print only the `withheld` sentinel",
    )
    sp.add_argument("query")
    sp.add_argument("--target", required=True, help="note id to trace (egress-gated)")
    sp.add_argument("-k", type=int, default=10, help="production max results (default: 10)")
    sp.add_argument("--rerank", action="store_true",
                    help="diagnose the same cross-encoder rerank path search/hybrid-search "
                         "run by default (this diagnostic itself stays opt-in)")
    # 20, matching search/hybrid-search: a diagnostic that reproduces a
    # DIFFERENT window than production explains the wrong ranking. (It was 15
    # while search's default was also 15; BR-03 moved search to 50 and left
    # this behind, so the two only agree again now.)
    sp.add_argument("--rerank-top", type=int, default=20,
                    help="production rerank window, clamped to 10-50 by default "
                         "(default: 20 — same as search/hybrid-search)")
    sp.add_argument("--rrf-k", type=int, default=60,
                    help="production Reciprocal Rank Fusion constant (default: 60)")
    add_common(sp)

    sp = sub.add_parser(
        "dossier",
        help="RET-10: the ONE-CALL retrieval sweep for decision-state questions — "
             "decision-layer hits + corroborating sources + TENSIONS (newer "
             "sources post-dating a recorded decision) + freshness, with "
             "retired versions already excluded. Prefer this over plain "
             "search when the question is 'what have we decided / what's "
             "the current state'",
    )
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=12, help="max live hits (default: 12)")
    add_common(sp)

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

    sp = sub.add_parser(
        "unsupersede",
        help="host-broker: BREAK the <old-id> -> <new-id> supersession link — "
             "both sides, signed. The audited undo for a wrong auto-link "
             "(ENF-01)",
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
        "inbox",
        help="the Tier-2 owner-decision queue: list open questions, or record "
             "an answer (--answer KEY --value TEXT). HOST-ONLY.",
    )
    sp.add_argument("--answer", default=None, metavar="KEY",
                    help="record an answer to the open question with this key")
    sp.add_argument("--value", default=None, metavar="TEXT",
                    help="the answer text (required with --answer)")
    add_common(sp)

    sp = sub.add_parser(
        "retro",
        help="retro fold: scan this vault's maintenance output for engine "
             "failure signatures and write engine-feedback prompts. HOST-ONLY.",
    )
    add_common(sp)

    # -- COS host-engine capabilities (CUT-01E) ----------------------------
    sp = sub.add_parser(
        "cos-propose",
        help="VM-ALLOWED: drop ONE unsigned COS proposal into the proposal-drop "
             "dir (which `brain sync` NEVER reads). Only the host broker's "
             "validate -> owner-inbox-batch -> accept flow can move it toward "
             "the signed write path. --kind correction drops a correction "
             "request (JSON: round, msg_key, corrected_bucket, corrected_tier) "
             "into verdict-drop/ instead.",
    )
    sp.add_argument("--id", default=None, help="note id (default: frontmatter or content hash)")
    sp.add_argument("--kind", default="proposal", choices=("proposal", "correction"))
    sp.add_argument("--content", default=None, help="content (default: read stdin)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-run-begin",
        help="HOST-ONLY: freeze the run manifest for a COS run BEFORE it "
             "starts (run id, executing SKILL.md path + digest, bundle + "
             "extraction-rules versions, expected artifacts). Every later "
             "claim stamps candidates from THIS record, never from whatever "
             "skill happens to be deployed at claim time.",
    )
    sp.add_argument("--run-id", default=None,
                    help="<YYYY-MM-DD>-run<N> (default: one past the highest "
                         "run number on disk)")
    sp.add_argument("--lane", default=None,
                    choices=("codex-automation", "cowork-desktop"),
                    help="assert which surface executes (default: resolve it; "
                         "an unresolvable lane REFUSES rather than guesses)")
    sp.add_argument("--skill", default=None,
                    help="assert the executing SKILL.md path outright")
    sp.add_argument("--attended", action="store_true",
                    help="a human is about to approve this run's plan and "
                         "watch it apply. REFUSES a dirty or non-git working "
                         "tree: the manifest's `git_commit` is the record of "
                         "WHICH CODE he approved, and it says nothing if "
                         "uncommitted edits sat beside it.")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-corpus-check",
        help="HOST-ONLY (WIR-02): the gate a run passes BEFORE it judges. "
             "Reports how many of this run's captured threads carry body "
             "text, and REFUSES (exit 3) when none does — the judge's input "
             "is the body, so no bodies is a MISSING INPUT, never a quiet "
             "night. Some bodyless rows are normal and pass.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-corpus-append",
        help="HOST-ONLY (WIR-01): save the text a run just READ, as it reads "
             "it. ONE row per in-scope thread: --conversation-id with the "
             "extracted message text on stdin for a thread whose body was "
             "opened, or --bodyless <id>... in one call for the threads that "
             "were enumerated and never opened. The ledger keeps the verdict; "
             "this keeps the input the verdict was made from.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--conversation-id", default=None,
                    help="the ONE thread this text belongs to — the join key "
                         "back to that run's ingestion ledger")
    sp.add_argument("--bodyless", nargs="+", default=None, metavar="CONV_ID",
                    help="conversation ids enumerated but NOT opened (unread, "
                         "over-cap, no body access on the lane, page not "
                         "visible) — one empty row each")
    sp.add_argument("--text", default=None,
                    help="the extracted message text (default: read stdin)")
    sp.add_argument("--sender", default=None)
    sp.add_argument("--sent", default=None, help="ISO date or datetime")
    sp.add_argument("--subject", default=None)
    sp.add_argument("--read-lane", default=None,
                    help="the elected observation lane, as the ledger names it")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-corpus-close",
        help="HOST-ONLY (WIR-01): close this run's corpus (write-once from "
             "here, and only a CLOSED corpus is ever deleted by retention). A "
             "closed corpus with 0 rows is a quiet night; an unclosed one is a "
             "capture stage that died.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-corpus-reopen",
        help="HOST-ONLY: retract a close that certified ZERO rows, after a "
             "lane failure turned out to be transient (run 68). A close "
             "carrying rows is final and is refused — capture the rest of the "
             "night under a new run id.",
    )
    sp.add_argument("--run-id", required=True, help="<YYYY-MM-DD>-run<N>")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-broker",
        help="HOST broker step (also wired into `brain maintain`): claim + "
             "validate proposal drops, expire/requeue, consume owner-inbox "
             "answers (only ACCEPTED candidates move to capture-inbox for "
             "signing), release due holds, enqueue at most one signed batch, GC.",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-correct",
        help="HOST-only correction of record: append ONE correction_events row "
             "(round, msg_key, corrected_bucket, corrected_tier). Append-only; "
             "rejects unknown (un-ledgered) and duplicate keys.",
    )
    sp.add_argument("--round", type=int, required=True, dest="round_")
    sp.add_argument("--msg-key", required=True)
    sp.add_argument("--bucket", required=True, help="corrected_bucket")
    sp.add_argument("--tier", required=True, help="corrected_tier")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-evidence",
        help="HOST-only trust-gate evidence signer: `sign` writes a bundle + "
             "Ed25519-signed versioned manifest (bundle/model version, snapshot "
             "generation, dataset window, source-ledger hash) under the "
             "host-private evidence dir; `verify` re-checks signature + hashes "
             "(a stale/edited JSON fails).",
    )
    sp.add_argument("action", choices=("sign", "verify"))
    sp.add_argument("--bundle-version", default=None)
    sp.add_argument("--model-version", default=None)
    sp.add_argument("--dataset-window", default=None)
    sp.add_argument("--file", action="append", default=[], dest="files",
                    help="payload file to include (repeatable)")
    sp.add_argument("--name", default="evidence")
    sp.add_argument("--dir", default=None, help="[verify] bundle dir to verify")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-priority-map",
        help="HOST-only: generate the VM-readable shared/priority-map.md from "
             "type:person/company notes via a host-produced filtered projection "
             "(default tier policy: the FULL vault, NOT capped to Internal) + "
             "owner overrides from the overlay cos/ category.",
    )
    sp.add_argument("--max-tier", default=None, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-report",
        help="HOST-only: shadow-mode calibration report — rounds completed, "
             "per-bucket precision, from the verdict-drop shadow ledger x "
             "correction_events (calibration = reduce(verdicts, corrections)).",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-ingest-sweep",
        help="HOST-only (also wired into `brain maintain`): claim VM "
             "ingest-manifest lines (drop/ingest-manifest/) and MOVE "
             "exact-filename matches from an explicitly configured dedicated "
             "host-only staging dir ($BRAIN_COS_DOWNLOADS_DIR) into "
             "<vault>/inbox/ for normal signed ingest. Disabled when unset; "
             "shared ~/Downloads and symlinked dirs are refused. Basename-only "
             "filenames and file symlinks are "
             "refused, 200MB cap, append-only claims (idempotent); files the "
             "manifest does not name are never touched.",
    )
    sp.add_argument("--downloads-dir", default=None,
                    help="dedicated host-only download staging dir to sweep "
                         "(default: $BRAIN_COS_DOWNLOADS_DIR; unset disables)")
    sp.add_argument("--dry-run", action="store_true",
                    help="report matches without moving or claiming anything")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-hold",
        help="HOST-only auto-capture hold store: `add` parks an item UNSIGNED "
             "until --not-before; a due item enters capture-inbox (then the "
             "signed drain) only after expiry. `cancel` is atomic against a "
             "concurrent release. `release-due` is also run by the broker fold.",
    )
    sp.add_argument("action", choices=("add", "list", "cancel", "release-due"))
    sp.add_argument("--id", default=None)
    sp.add_argument("--not-before", default=None,
                    help="[add] ISO timestamp before which the item must NOT be signed")
    sp.add_argument("--content", default=None, help="[add] content (default: read stdin)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "cos-spine",
        help="HOST-only commitment spine (SP-01/SP-02): `record` appends ONE "
             "event (created/rescheduled/completed/cancelled/corrected/"
             "reopened) — commitment-kind ingestion candidates are recorded "
             "automatically on owner acceptance; this is for the other two "
             "named sources (calendar follow-ups, drafts ledger). `radar` "
             "prints late/at-risk open commitments. `render` regenerates the "
             "VM-readable shared/spine-summary.md projection (also run every "
             "broker fold). `grounding-pack` regenerates the BAK-01 "
             "shared/grounding-pack.md projection — Internal-safe POINTERS to "
             "documents above the VM leg's egress ceiling, from the host-private "
             "host/grounding-pack-ids.txt list (also run every broker fold).",
    )
    sp.add_argument("action", choices=("record", "radar", "render", "grounding-pack"))
    sp.add_argument("--event", default="created", choices=(
        "created", "rescheduled", "completed", "cancelled", "corrected", "reopened"))
    sp.add_argument("--id", dest="commitment_id", default=None,
                    help="[record] existing commitment id (any event but 'created')")
    sp.add_argument("--direction", default=None, choices=("owed_by_me", "owed_to_me"))
    sp.add_argument("--counterparty", default=None)
    sp.add_argument("--text", default=None)
    sp.add_argument("--topic", default=None)
    sp.add_argument("--due", default=None, help="ISO timestamp")
    sp.add_argument("--source-ref", default=None)
    sp.add_argument("--note", default=None)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "draft-capture",
        help="VM-side capture: stage a candidate note as a plain DRAFT "
             "(no sign, no index, no WAL) for the host to drain later",
    )
    sp.add_argument("--id", default=None, help="note id (default: from frontmatter or content hash)")
    sp.add_argument("--source", action="store_true", help="stage as a raw/ source (vs a brain/ note)")
    sp.add_argument("--content", default=None, help="note text (default: read stdin)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "provision-request",
        help="VM-side (PRV-10): stage a new-vault provisioning request marker "
             "for the host to complete (no key, no launchd, no registry)",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "provision-drain",
        help="HOST-broker (PRV-10): scan registered-workspace parents for "
             "pending provision requests and complete each (init --full "
             "--apply + sync --publish + model + registry); also runs as a "
             "fold on the hourly maintain daily branch",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("rebuild", help="rebuild the derived index from vault/ (always safe)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--progress", action="store_true",
                    help="force stderr progress lines even when stderr isn't a TTY "
                         "(same as BRAIN_PROGRESS=1)")

    sp = sub.add_parser(
        "warmup",
        help="HOST-ONLY (S02/CS-01): resolve + download the live embedding "
             "model now (stderr progress), instead of deferring to the first "
             "real semantic search. Never on role=vm — the VM model is "
             "pre-staged by the host and HuggingFace is off its egress "
             "allowlist. Does not rebuild the index; run `brain sync` after "
             "if `brain status` reported embedder: pending.",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--progress", action="store_true",
                    help="force stderr progress lines even when stderr isn't a TTY "
                         "(same as BRAIN_PROGRESS=1)")

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
    sp.add_argument("--progress", action="store_true",
                    help="force stderr progress lines even when stderr isn't a TTY "
                         "(same as BRAIN_PROGRESS=1)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "snapshot", help="publish a read-only, generation-stamped index snapshot (host)")
    sp.add_argument("--dest", default=None, help="snapshot dir (default: vault/.brain/snapshot)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "restore-index",
        help="fast-recover the live index from the published snapshot (host) — "
             "seconds, no re-embed; use instead of `rebuild` when the index is corrupt/empty")
    sp.add_argument("--force", action="store_true",
                    help="restore even if the live index has MORE notes than the snapshot "
                         "(the snapshot is older — you may lose notes)")
    sp.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "status", help="report index stats + read-only snapshot generation/age")
    sp.add_argument("--snapshot-dest", default=None)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project", help="write a classification-filtered copy of the vault (real containment)")
    sp.add_argument("--dest", required=True, help="destination directory (recreated each run)")
    # project builds a workspace for an UNTRUSTED leg — it keeps the
    # conservative Internal default even now that the host read surface
    # defaults to the full vault (a full-vault containment copy is an
    # explicit choice, never a default).
    sp.add_argument("--max-tier", default="Internal", choices=cls.TIERS)
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
    sp.add_argument("--check-content", action="store_true",
                    help="also flag notes whose current bytes differ from the "
                         "last signed content hash (detects post-commit edits)")
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
    sp.add_argument("--max-tier", default=None, choices=cls.TIERS)
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
    sp.add_argument("--max-tier", default=None, choices=cls.TIERS)
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--html", action="store_true",
        help="write a self-contained, overlay-branded HTML digest to .brain/brief/ "
             "(host-only — a new file-egress surface, ADR-0003 Ruling c; refused on role=vm)",
    )

    sp = sub.add_parser(
        "health-report",
        help="render the static HTML health report (verdict + act-now + "
             "maintain/index/trend tables) to .brain/brief/health-latest.html "
             "(host-only — refused on role=vm)",
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "graph-report",
        help="render the static HTML graph explorer (WebGL link graph + 3D "
             "semantic map) to .brain/graph/graph-explorer.html "
             "(host-only — refused on role=vm)",
    )
    sp.add_argument("--json", action="store_true")

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
        "sweep-workspace",
        help="WSP-01: move SETTLED top-level files (mtime older than --age-days) "
             "from configured working folder(s) into <vault>/inbox/ for the "
             "standard ingest drain — the lifecycle for session-artifact dumping "
             "grounds. Sources: --dir (repeatable) or $BRAIN_WORKSPACE_SWEEP_DIRS. "
             "Subdirectories and dotfiles are never touched; already-ingested "
             "content dedups by hash downstream. Runs inside the nightly "
             "maintain automatically when configured (host-only)",
    )
    sp.add_argument("--dir", action="append", default=None, dest="dirs",
                    help="workspace folder to sweep (repeatable; default: "
                         "$BRAIN_WORKSPACE_SWEEP_DIRS)")
    sp.add_argument("--age-days", type=int, default=None,
                    help="settled threshold in days (default: "
                         "$BRAIN_WORKSPACE_SWEEP_AGE_DAYS or 14)")
    sp.add_argument("--dry-run", action="store_true",
                    help="report what would move; touch nothing")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "maintain",
        help="the umbrella: THE single sanctioned host task (brain-nightly) — "
             "workspace sweep (when configured) + sync --publish + brief + "
             "recommendations-aging fold, plus date-gated "
             "health/integrity/digest(+curate+promote-scan)/graphify branches; "
             "due-since-last-run catch-up + single-runner lock (ADR-0003 Ruling 5/d)",
    )
    sp.add_argument("--dry-run", action="store_true",
                    help="skip sync/drain/publish/signing; still runs the real "
                         "read-only health/integrity probes for any due branch")
    sp.add_argument("--date", default=None,
                    help="YYYY-MM-DD override for date-gate testing (default: today)")
    sp.add_argument("--allow-future-date", action="store_true",
                    help="permit a --date AFTER the wall clock (needed only for "
                         "deliberate future date-gate exercises; default: refuse)")
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
    sp.add_argument("--progress", action="store_true",
                    help="force stderr progress lines even when stderr isn't a TTY "
                         "(same as BRAIN_PROGRESS=1)")
    add_common(sp)

    return p


def _make_core(args: Any, role: str) -> BrainCore:
    """Construct BrainCore with the resolved role. Tolerant of a test double that
    patched ``cli.BrainCore`` with a vault-only signature (back-compat)."""
    try:
        return BrainCore(vault=args.vault, role=role)
    except TypeError:
        return BrainCore(vault=args.vault)


def _connect_confirm(preview: str, args: Any) -> bool:
    """The one confirmation gate every `connect` mutation goes through:
    --yes always proceeds; --json never prompts (automation must pass --yes
    explicitly, same contract as `init --import-from`); otherwise prompt on a
    real TTY, and refuse (caller prints the preview + exits non-zero) when
    neither holds."""
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        return False
    sys.stdout.write(preview + "\n")
    ans = input("Proceed? [y/N] ").strip().lower()
    return ans in ("y", "yes")


def _connect_file_step(plan: "_connect.ConnectPlan", args: Any, *, remove: bool) -> dict:
    """Diff -> confirm -> write, for one file-based connect plan (both the
    Markdown marked-block clients and the JSON-merge clients share this)."""
    if plan.action == "noop" and not remove:
        return {"path": str(plan.target_path), "action": "noop",
                "already_connected": True, "diff": ""}
    if plan.action == "noop" and remove:
        return {"path": str(plan.target_path), "action": "noop",
                "detail": "nothing to unwire", "diff": ""}
    if not _connect_confirm(plan.diff or f"(would create {plan.target_path})", args):
        return {"path": str(plan.target_path), "action": plan.action,
                "diff": plan.diff, "confirmed": False,
                "detail": "not confirmed — pass --yes to proceed non-interactively"}
    if remove:
        _connect.apply_remove_marked_block(plan)
    else:
        if plan.target_path.suffix == ".json":
            _connect.apply_json_merge(plan)
        else:
            _connect.apply_marked_block(plan)
    return {"path": str(plan.target_path), "action": plan.action,
            "diff": plan.diff, "confirmed": True}


def _cmd_provision(cmd: str, args: Any, role: str) -> int:
    """PRV-10 dispatch: VM request-marker write, or the host drain."""
    from . import provision

    if cmd == "provision-request":
        res = provision.write_request(config.vault_root(args.vault), role=role)
        _emit(res if args.json else
              f"provision request: {res['status']}"
              + (f" — {res['note']}" if res.get("note") else ""), args.json)
        return 0

    res = provision.drain()
    if args.json:
        _emit(res, True)
    else:
        lines = [f"provision drain: {len(res['handled'])} request(s) handled, "
                 f"{len(res['roots'])} root(s) scanned"]
        for h in res["handled"]:
            lines.append(f"  {h.get('vault')}: {h.get('status')}"
                         + ("" if h.get("ok") else " (NOT ok)"))
        for s in res["stuck_claims"]:
            lines.append(f"  STUCK claim (crashed drain?): {s}")
        _emit(None, False, "\n".join(lines))
    return 0 if all(h.get("ok") for h in res["handled"]) else 1


def _cmd_connect(args: Any) -> int:
    from pathlib import Path

    from . import config

    client = args.client
    remove = args.remove
    target_dir = Path(args.target).resolve()
    vault = str(Path(config.vault_root(args.vault)).resolve())
    steps: list[dict] = []
    ok = True

    if client == "claude-desktop":
        path = _connect.claude_desktop_config_path()
        if remove:
            found = _connect.plan_restore_from_backup(path)
            if not found["ok"]:
                steps.append({"path": str(path), "action": "noop", "detail": found["reason"]})
            elif _connect_confirm(f"restore {path} from backup {found['backup']}", args):
                _connect.apply_restore_from_backup(path, Path(found["backup"]))
                steps.append({"path": str(path), "action": "restore",
                              "backup": found["backup"], "confirmed": True})
            else:
                steps.append({"path": str(path), "action": "restore", "confirmed": False,
                              "detail": "not confirmed — pass --yes to proceed non-interactively"})
                ok = False
        else:
            plan = _connect.plan_claude_desktop(path, vault, args.name, args.max_tier)
            step = _connect_file_step(plan, args, remove=False)
            steps.append(step)
            ok = step.get("already_connected") or step.get("confirmed", False)

    elif client == "gemini":
        path = target_dir / ".gemini" / "settings.json"
        if remove:
            found = _connect.plan_restore_from_backup(path)
            if not found["ok"]:
                steps.append({"path": str(path), "action": "noop", "detail": found["reason"]})
            elif _connect_confirm(f"restore {path} from backup {found['backup']}", args):
                _connect.apply_restore_from_backup(path, Path(found["backup"]))
                steps.append({"path": str(path), "action": "restore",
                              "backup": found["backup"], "confirmed": True})
            else:
                steps.append({"path": str(path), "action": "restore", "confirmed": False,
                              "detail": "not confirmed — pass --yes to proceed non-interactively"})
                ok = False
        else:
            plan = _connect.plan_gemini(path)
            step = _connect_file_step(plan, args, remove=False)
            steps.append(step)
            ok = step.get("already_connected") or step.get("confirmed", False)

    elif client == "codex":
        path = target_dir / "AGENTS.md"
        if remove:
            plan = _connect.plan_remove_marked_block(path)
            step = _connect_file_step(plan, args, remove=True)
        else:
            plan = _connect.plan_marked_block(path)
            step = _connect_file_step(plan, args, remove=False)
        steps.append(step)
        ok = step.get("already_connected") or step.get("confirmed", False) or step["action"] == "noop"

    elif client == "claude-code":
        if remove:
            available = _connect.claude_plugin_cli_available()
            if available and _connect_confirm(
                    f"claude plugin uninstall {_connect.KERNEL_PLUGIN}@{_connect.MARKETPLACE_NAME}", args):
                result = _connect.run_claude_code_plugin_uninstall(
                    claude_home=Path.home() / ".claude")
                steps.append({"kind": "plugin", "confirmed": True, **result})
                ok = result["ok"]
            elif not available:
                steps.append({"kind": "plugin", "detail": "`claude plugin` CLI not available; "
                              f"run manually: claude plugin uninstall "
                              f"{_connect.KERNEL_PLUGIN}@{_connect.MARKETPLACE_NAME}"})
            else:
                steps.append({"kind": "plugin", "confirmed": False,
                              "detail": "not confirmed — pass --yes to proceed non-interactively"})
                ok = False
            path = target_dir / "CLAUDE.md"
            plan = _connect.plan_remove_marked_block(path)
            step = _connect_file_step(plan, args, remove=True)
            steps.append(step)
            ok = ok and (step.get("already_connected") or step.get("confirmed", False) or step["action"] == "noop")
        else:
            claude_home = Path.home() / ".claude"
            already_installed = _connect.is_plugin_installed(claude_home)
            if already_installed:
                steps.append({"kind": "plugin", "action": "noop", "already_connected": True})
            else:
                available = _connect.claude_plugin_cli_available()
                cmds = _connect.claude_code_plugin_commands(args.marketplace_source)
                preview = "\n".join(" ".join(c) for c in cmds)
                if not available:
                    steps.append({
                        "kind": "plugin", "action": "manual",
                        "detail": "`claude` plugin CLI not detected/usable — run these two "
                                  "commands yourself (guided, not one-command, for this client):",
                        "commands": [" ".join(c) for c in cmds],
                    })
                elif _connect_confirm(preview, args):
                    result = _connect.run_claude_code_plugin_install(
                        marketplace_source=args.marketplace_source, claude_home=claude_home)
                    steps.append({"kind": "plugin", "confirmed": True, **result})
                    ok = result["ok"]
                else:
                    steps.append({"kind": "plugin", "confirmed": False, "commands": [" ".join(c) for c in cmds],
                                  "detail": "not confirmed — pass --yes to proceed non-interactively"})
                    ok = False
            path = target_dir / "CLAUDE.md"
            plan = _connect.plan_marked_block(path)
            step = _connect_file_step(plan, args, remove=False)
            steps.append(step)
            ok = ok and (step.get("already_connected") or step.get("confirmed", False))

    report = {"client": client, "removed": remove, "steps": steps, "ok": ok}
    if args.json:
        _emit(report, True)
    else:
        lines = [f"brain connect --client {client}{' --remove' if remove else ''} — "
                 f"{'OK' if ok else 'INCOMPLETE'}"]
        for step in steps:
            if step.get("already_connected"):
                lines.append(f"  {step.get('path', step.get('kind'))}: already connected")
            elif step.get("confirmed"):
                lines.append(f"  {step.get('path', step.get('kind'))}: wired")
            else:
                lines.append(f"  {step.get('path', step.get('kind'))}: {step.get('detail', step.get('action'))}")
                if step.get("diff"):
                    lines.append(step["diff"])
                if step.get("commands"):
                    lines.extend(f"    {c}" for c in step["commands"])
        _emit(None, False, "\n".join(lines))
    return 0 if ok else 2


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
    # The degradation digest. VM_ALLOWED on purpose: it is the ONE surface that
    # tells a Cowork session its vault is degraded, and it reads only plain
    # files on the shared mount that `maintain` already wrote. Its host-home
    # sources are unreachable there and are REPORTED as such, never skipped.
    "alerts",
    "mcp-config",  # prints a config string; no index/key/vault read
    "search", "hybrid-search", "diagnose", "dossier", "grep", "bases-query", "graph-expand",
    "get", "read", "recent", "status", "draft-capture",
    "capture", "brief", "digest",
    # CUT-01E: the ONE COS ingress a VM holds — an UNSIGNED drop into a dir
    # `sync` never reads. Every other cos-* verb (broker, correct, evidence,
    # ingest-sweep (v2.1 host downloads sweeper),
    # priority-map, hold) is host-broker only and refused here.
    "cos-propose",
    # PRV-10: the new-vault request marker. A plain-file drop the host drain
    # completes; the VM leg still never signs, registers, or touches the
    # registry (`provision-drain` stays host-broker only).
    "provision-request",
})


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
        _emit({"error": type(exc).__name__, "detail": str(exc)} if as_json
              else f"{exc.__class__.__name__}: {exc}", as_json)
        return 3
    finally:
        # The VM hint-suppression flag is INVOCATION-scoped (set by _main per
        # role): reset it here so it never leaks into a later main() call or a
        # direct _filter_dicts caller in the same process (e.g. across tests).
        _SUPPRESS_ELEVATION_HINT = False


def _main(argv: list[str] | None = None) -> int:
    from . import config

    args = build_parser().parse_args(argv)
    # BR-03 (owner ruling 2026-08-04): rerank default-on for search/hybrid-search.
    # --rerank uses BooleanOptionalAction with default=None so it's only left
    # None here when the user typed neither --rerank nor --no-rerank; `diagnose`
    # still uses a plain store_true (default False), so this never fires there.
    # Precedence: an explicit --rerank/--no-rerank always wins; absent that,
    # BRAIN_RERANK_DISABLED=1 is the global kill switch (mirrors
    # BRAIN_EXACT_LEG_ENABLED's env contract); absent both, the default is ON.
    if getattr(args, "rerank", "unset") is None:
        from .rerank import rerank_enabled

        args.rerank = rerank_enabled()
    role = config.role(getattr(args, "role", None))
    # Role-aware egress default (owner decision, 2026-07-10): the trusted host
    # surfaces the FULL vault unless --max-tier changes it; the untrusted VM
    # leg starts at Internal. argparse leaves max_tier None only when the flag
    # was not typed; the VM-specific hard clamp is applied immediately below.
    if getattr(args, "max_tier", "unset") is None:
        args.max_tier = (cls.VM_DEFAULT_MAX_TIER if role == config.ROLE_VM
                         else cls.DEFAULT_MAX_TIER)
    # VM egress ceiling (codex 2026-07-19): on the untrusted leg, --max-tier is
    # a value the LLM can set itself, and the starvation hint nudges it to. Clamp
    # any VM-side max_tier to the operator-set ceiling so a typed --max-tier MNPI
    # cannot self-elevate past it; the hint is also suppressed for role=vm below.
    global _SUPPRESS_ELEVATION_HINT
    _SUPPRESS_ELEVATION_HINT = (role == config.ROLE_VM)  # deterministic per call
    if role == config.ROLE_VM and hasattr(args, "max_tier"):
        args.max_tier = cls.clamp_to(str(args.max_tier), cls.vm_egress_ceiling())
    # DV-03: the VM leg fails closed on a dead embedder rather than silently
    # answering semantic queries with random hash vectors (no-op when a real
    # embedder is present or hash was chosen explicitly).
    config.apply_role_embedder_policy(role)
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

            import_report: dict[str, Any] | None = None
            if args.import_from:
                # ONB-01: refused BEFORE any filesystem side effect -- no
                # dry-run scan, no read of the import folder at all. `init`
                # itself is VM_ALLOWED (filesystem-only overlay setup), but
                # --import-from drives the host-only ingest drain, so it
                # gets its own gate here, same shape as the general VM
                # trust-gate refusal above.
                if role == config.ROLE_VM:
                    msg = {
                        "error": "role_forbidden", "role": role, "cmd": "init --import-from",
                        "detail": "'init --import-from' stages + ingests a folder via "
                                  "the host ingest drain; the VM leg is read + draft "
                                  "only. Run it on the host.",
                    }
                    _emit(msg if args.json else
                          "refused: 'init --import-from' is host-broker only "
                          "(role=vm is read+draft). Run it on the host.", args.json)
                    return 4
                try:
                    dry_run = brain_init.build_import_dry_run(
                        args.import_from, args.vault, force=args.import_force)
                except brain_init.ImportSafetyError as exc:
                    _emit({"error": "import_safety", "detail": str(exc)} if args.json
                          else f"import refused: {exc}", args.json)
                    return 2
                proceed = args.yes
                dry_run_printed = False
                if not proceed and not args.json and sys.stdin.isatty():
                    sys.stdout.write(brain_init.render_import_dry_run(dry_run) + "\n")
                    dry_run_printed = True
                    ans = input("Proceed with staging + ingest? [y/N] ").strip().lower()
                    proceed = ans in ("y", "yes")
                if not proceed:
                    if args.json:
                        _emit({"action": "init-import-dry-run", "manifest":
                               {k: v for k, v in dry_run.items() if k != "_files"},
                               "hint": "re-run with --yes to stage + ingest"}, True)
                    else:
                        human = "aborted: pass --yes to proceed non-interactively"
                        if not dry_run_printed:
                            human = brain_init.render_import_dry_run(dry_run) + "\n\n" + human
                        _emit(None, False, human)
                    return 2
                import_report = brain_init.stage_and_ingest_import(
                    args.import_from, args.vault, role, force=args.import_force)

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
                seed_vault=args.seed_vault,
            )
            if import_report is not None:
                report["import"] = import_report
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
            for warning in report.get("warnings", []):
                lines.append(f"  warning: {warning}")
            _emit(None, False, "\n".join(lines))
        return 0 if report["valid"] else 1

    # `doctor` is pure filesystem/subprocess inspection (ADR-0005 Ruling 2) —
    # dispatch BEFORE BrainCore construction, same reasoning as `init`: it
    # must work even against a vault with no index built yet, and it never
    # touches the vault at all.
    if cmd == "alerts":
        from . import alerts as brain_alerts

        # A vault is OPTIONAL here, and demanding one was a real bug: the host
        # role sweeps the workspace registry and never needed a vault at all,
        # so `brain alerts` from any directory that is not a vault exited 3 and
        # the Codex hook reported "cannot check" (measured 2026-08-14). Resolve
        # leniently and let `collect` say what it could not reach — on the VM
        # leg an unresolved vault is a REPORTED gap, never a cheerful
        # "no alerts".
        try:
            alerts_vault = config.vault_root(args.vault)
        except config.VaultNotFoundError:
            alerts_vault = None
        report = brain_alerts.collect(role=role, vault=alerts_vault)
        if args.one_line:
            banner = brain_alerts.one_line(report)
            if banner:
                print(banner)
        else:
            _emit(report if args.json else None, args.json,
                  None if args.json else brain_alerts.render_human(report))
        # Exit 0 even with findings: this runs at session start in every
        # harness, and a non-zero exit reads as "the check itself broke".
        return 0

    if cmd == "doctor":
        from . import doctor as brain_doctor

        # Role-aware (2026-07-07 addendum, ADR-0005 Ruling 2): the VM leg only
        # ever sees the staged zero-install copy, so it gets its own surface
        # set. Structural fallback covers the staged shim, which invokes
        # `python3 -m brain.cli "$@"` directly and never sets $BRAIN_ROLE.
        vm_posture = role == config.ROLE_VM or brain_doctor.looks_like_vm_stage()
        if vm_posture:
            report = brain_doctor.run_doctor_vm(vault=args.vault)
        else:
            registry_fetch = None
            if getattr(args, "check_registry", False):
                def registry_fetch():  # noqa: E306 - single cached HTTPS read, opt-in only
                    return {"pypi_version": brain_doctor.fetch_pypi_latest_version()}
            report = brain_doctor.run_doctor(registry_fetch=registry_fetch, vault=args.vault)
        _emit(report if args.json else None, args.json,
              None if args.json else brain_doctor.render_human(report))
        return 0 if report["ok"] else 1

    # `mcp-config` prints the MCP-client entry to run brain-mcp against this
    # vault (host-side stdio server, same pattern as Smart Connections' MCP).
    # Pure string generation — no BrainCore, no index, no key.
    if cmd == "mcp-config":
        from pathlib import Path

        vault = str(Path(config.vault_root(args.vault)).resolve())
        # Same entry shape `brain connect --client claude-desktop` WRITES
        # (connect.mcp_server_entry) — one builder, so print-only and
        # write-for-real can never drift apart (SUI-02 reconciliation).
        entry = _connect.mcp_server_entry(vault, args.name, args.max_tier)
        if args.json:
            _emit(entry, True)
        else:
            body = json.dumps(entry, indent=2)
            _emit(None, False,
                  "Add this inside \"mcpServers\" in your MCP client config, then "
                  "restart the client:\n"
                  "  Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json\n"
                  "  Claude Code:    ~/.claude.json (or `claude mcp add`)\n\n" + body)
        return 0

    # `connect` (SUI-02) — universal per-client wirer. Dispatched BEFORE
    # BrainCore construction, same reasoning as `doctor`/`mcp-config`: it
    # never touches the index/key/vault at all, only user config files +
    # (claude-code only) the `claude` CLI. Already refused on role=vm at the
    # VM_ALLOWED gate above, before this line is ever reached.
    if cmd == "connect":
        return _cmd_connect(args)

    # PRV-10 — both dispatched BEFORE BrainCore construction (filesystem +
    # subprocess only; a brand-new vault has no index yet). `provision-request`
    # is VM_ALLOWED; `provision-drain` is host-broker and refused on role=vm
    # by the VM_ALLOWED gate above.
    if cmd in ("provision-request", "provision-drain"):
        return _cmd_provision(cmd, args, role)

    # `update` is the UP-02 single top-level entry point: it self-executes
    # (never just prints instructions) and is host-broker only — it mutates
    # the CLI plugin store, the engine venv, and staged workspaces, none of
    # which the VM leg may touch.
    if cmd == "update":
        from . import update as brain_update

        if config.is_managed() and not args.dry_run:
            _emit("brain update is disabled on a managed endpoint "
                  "($BRAIN_MANAGED) — updates are deployed centrally. "
                  "Use --dry-run to preview what a managed rollout would change.",
                  args.json)
            return 1
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

    if cmd == "eval":
        # `eval` is absent from VM_ALLOWED, so this host-only replay branch is
        # reached only after the pre-core trust gate above.  It invokes the
        # engine directly rather than the CLI search path, therefore it can
        # never append a new real-traffic capture record while replaying.
        from . import querylog

        if args.eval_cmd == "replay":
            try:
                report, thresholds_failed = querylog.replay(
                    core, args.against,
                    fail_under_top1=args.fail_under_top1,
                    fail_under_jaccard=args.fail_under_jaccard,
                )
            except (querylog.ReplayDataError, ValueError) as exc:
                payload = {"error": "replay_data", "detail": str(exc)}
                _emit(payload if args.json else f"replay error: {exc}", args.json)
                return 2
            _emit(report if args.json else None, args.json,
                  None if args.json else json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if thresholds_failed else 0

    if cmd in ("search", "hybrid-search"):
        # S02/CS-01: check BEFORE the search call — a cold-start index built
        # with the offline hash placeholder degrades to FTS-only inside
        # BrainIndex._dense_ranked (see its docstring); surface that here so
        # the agent/user sees WHY results look lexical-only rather than
        # concluding the vault is thin.
        from . import querylog

        embedder_pending = core.embedder_pending()
        # The host capture default needs the bounded S03 trace/digest.  On the
        # VM (or with the host kill switch off), normal searches retain the
        # lightweight trace-free path unless the user explicitly asks for
        # --explain.  `capture_requested` is a pure role/env check — it never
        # touches a host ledger before the response has passed egress.
        capture_enabled = querylog.capture_requested(role)
        capture_started = time.perf_counter() if capture_enabled else None
        trace = None
        fanout = None
        # RET-05: the ORIGINAL query is always variant 0 — every identity and
        # create-safety guarantee is anchored to it (see core.search_multi).
        # With no --variant this branch is not taken at all, so the single-query
        # ranking is untouched, byte for byte.
        variants = [args.query] + list(getattr(args, "variant", None) or [])
        if len(variants) > 1:
            fan_hits, fanout = core.search_multi(
                variants, k=args.k, rerank=args.rerank,
                rerank_top=args.rerank_top, rrf_k=args.rrf_k,
                rerank_gate=args.rerank_gate,
                rerank_fused=args.rerank_fused, return_trace=True,
            )
            hits = [hit.to_dict() for hit in fan_hits]
        elif args.explain or capture_enabled:
            trace_hits, trace = core.hybrid_search_with_trace(
                args.query, k=args.k, rerank=args.rerank,
                rerank_top=args.rerank_top, rrf_k=args.rrf_k,
                rerank_gate=args.rerank_gate,
            )
            hits = [hit.to_dict() for hit in trace_hits]
        else:
            hits = [h.to_dict() for h in core.hybrid_search(
                args.query, k=args.k, rerank=args.rerank,
                rerank_top=args.rerank_top, rrf_k=args.rrf_k,
                rerank_gate=args.rerank_gate)]
        surfaced, report = _filter_dicts(hits, args.max_tier)
        # ADR-0008: identity ownership is computed before egress, but its
        # create/no-create conclusion must be finalized after the gate so a
        # withheld collision can only yield the conservative ``unknown`` enum.
        identity_redacted_ids = core.annotate_create_safety(
            args.query, surfaced, args.max_tier,
        )
        freshness = _freshness_block(core, surfaced, args.max_tier)
        notice = (
            "embedder pending — dense/semantic ranking is skipped (FTS-only "
            "results) until the real model is applied to this index; run "
            "`brain warmup` then `brain sync`." if embedder_pending else None
        )
        if args.explain and trace is not None:
            for final_rank, hit in enumerate(surfaced, start=1):
                explain = trace.explain_for_id(
                    hit["id"], final_rank,
                    redact_identity=hit["id"] in identity_redacted_ids,
                )
                hit["explain"] = explain
        if args.explain and trace is not None:
            payload = {
                "query": args.query,
                "ranking": {
                    "rrf_k": trace.rrf_k,
                    "exact_leg_enabled": trace.exact_leg_enabled,
                    "rerank_requested": trace.rerank_requested,
                    "rerank_applied": trace.rerank_applied,
                    # RK-02: why the cross-encoder did or didn't run. Read this
                    # rather than inferring a skip from requested-but-not-applied,
                    # which also covers an absent model and a timeout fallback.
                    "rerank_gate": trace.rerank_gate,
                    # HYG-01: how many byte-identical, owner-superseded
                    # duplicate families this query folded into their canonical
                    # member (and how many it saw but a guard declined).
                    "family_collapse": trace.family_collapse,
                },
                "results": surfaced,
                # This bounded projection is built only from IDs that survived
                # the output gate; S04 can reuse the same safe object for host
                # query capture without ever seeing a withheld candidate.
                "candidate_digest": trace.compact_digest({hit["id"] for hit in surfaced}),
                "egress": report,
            }
        else:
            payload = {"query": args.query, "rerank": args.rerank,
                       "results": surfaced, "egress": report}
        if fanout is not None:
            payload["variants"] = _variant_block(
                fanout, {hit["id"] for hit in surfaced}, explain=args.explain,
            )
        # Shared post-egress serialization seam (ADR-0008 S04): only the
        # already-gated rows plus S03's safe digest reach the host ledger.
        # querylog swallows any containment/permission/append failure and
        # increments its local counter, so a healthy search never fails merely
        # because observability is unavailable.
        # A fan-out query is deliberately NOT captured: the ledger's only
        # consumer is `brain eval replay`, which re-runs each record as the
        # single query it stores and would report a fan-out ranking as drift
        # (and its schema refuses any mode label outside search/hybrid-search/
        # dossier). Capturing it would corrupt a replay, not enrich it.
        if capture_enabled and capture_started is not None and fanout is None:
            capture_top, capture_digest = querylog.projection_from_gated(
                surfaced, trace=trace, redacted_ids=identity_redacted_ids,
            )
            querylog.capture_post_egress(
                vault=core.vault, role=role, index=core.index, query=args.query,
                mode=cmd, k=args.k, rrf_k=args.rrf_k,
                exact_leg_enabled=bool(getattr(trace, "exact_leg_enabled", False)),
                rerank=_capture_rerank_metadata(core, trace, args),
                latency_ms=(time.perf_counter() - capture_started) * 1000,
                top=capture_top, candidate_digest=capture_digest,
                max_tier=args.max_tier,
            )
        if args.json:
            if freshness:
                payload["freshness"] = freshness
            if notice:
                payload["embedder_notice"] = notice
            _emit(payload, True)
        else:
            if args.explain and fanout is None:
                lines = [line for hit in surfaced for line in _render_explain_hit(hit)]
            else:
                lines = [f"[{h['source']}] {h['id']}  <{h.get('type') or '?'}>"
                         f"  ({h['classification'] or 'UNLABELLED'})"
                         f"  {h.get('date') or 'undated'}  "
                         f"{h['score'] if h.get('score') is not None else 'redacted'}"
                         f"\n    {h['snippet']}"
                         for h in surfaced]
            footer = _egress_footer(report)
            if fanout is not None:
                footer += "\n" + "\n".join(_render_variant_block(payload["variants"]))
            if notice:
                footer += f"\n-- {notice}"
            if freshness and freshness.get("hint"):
                footer += f"\n-- {freshness['hint']}"
            _emit(None, False, "\n".join(lines + [footer]) if lines else footer)
        return 0

    if cmd == "diagnose":
        # The trace-returning call executes the same production candidate cut,
        # scoring, suppression and reranking as search.  Only after that work
        # is complete do we inspect the requested target out of band.
        trace_hits, trace = core.hybrid_search_with_trace(
            args.query, k=args.k, rerank=args.rerank,
            rerank_top=args.rerank_top, rrf_k=args.rrf_k,
        )
        hits = [hit.to_dict() for hit in trace_hits]
        surfaced, report = _filter_dicts(hits, args.max_tier)
        core.annotate_create_safety(args.query, surfaced, args.max_tier)
        final_ranks = {hit["id"]: rank for rank, hit in enumerate(surfaced, start=1)}
        diagnosis = core.diagnose_target(
            args.query, args.target, max_tier=args.max_tier, trace=trace,
            final_rank=final_ranks.get(args.target),
        )
        if args.json:
            payload = {**diagnosis, "egress": report}
            # The strict withheld response intentionally omits query/target
            # metadata beyond the public sentinel and aggregate gate count.
            if diagnosis.get("verdict") != "withheld":
                payload = {"query": args.query, **payload}
            _emit(payload, True)
        else:
            _emit(None, False, _render_diagnose(diagnosis, report))
        return 0

    if cmd == "dossier":
        from . import querylog

        capture_enabled = querylog.capture_requested(role)
        capture_started = time.perf_counter() if capture_enabled else None
        res = core.dossier(args.query, k=args.k)
        decisions, drep = _filter_dicts(res["decisions"], args.max_tier)
        sources, srep = _filter_dicts(res["sources"], args.max_tier)
        # The targeted decision-layer probe can add a hit outside hybrid's
        # normal pool. Finalize the same post-egress identity conclusion over
        # both dossier layers so a withheld identity owner stays unknown.
        core.annotate_create_safety(args.query, decisions + sources, args.max_tier)
        report = {
            "total": drep["total"] + srep["total"],
            "surfaced": drep["surfaced"] + srep["surfaced"],
            "withheld": drep["withheld"] + srep["withheld"],
            "withheld_unlabelled_default_deny":
                drep["withheld_unlabelled_default_deny"]
                + srep["withheld_unlabelled_default_deny"],
            "max_tier": args.max_tier,
        }
        if (report["withheld"] > 0 and args.max_tier != cls.TIERS[-1]
                and not _SUPPRESS_ELEVATION_HINT):
            report["hint"] = (
                f"{report['withheld']} note(s) withheld above the "
                f"{args.max_tier} cap — re-run with a higher --max-tier.")
        freshness = _freshness_block(core, decisions + sources, args.max_tier)
        payload = {"query": res["query"], "decisions": decisions,
                   "sources": sources,
                   "retired_excluded": res["retired_excluded"],
                   "egress": report}
        if freshness:
            payload["freshness"] = freshness
        if capture_enabled and capture_started is not None:
            # A dossier composes hybrid candidates with a targeted decision
            # probe, so it has no one production trace to expose.  The shared
            # serializer still produces the bounded S03-compatible final-list
            # digest from its gated decision/source response.
            capture_top, capture_digest = querylog.projection_from_gated(
                decisions + sources,
            )
            querylog.capture_post_egress(
                vault=core.vault, role=role, index=core.index, query=args.query,
                mode="dossier", k=args.k, rrf_k=60,
                exact_leg_enabled=os.environ.get("BRAIN_EXACT_LEG_ENABLED", "1").strip().lower()
                not in {"0", "false", "no", "off"},
                rerank={"requested": False, "applied": False, "model": None, "top_n": 0},
                latency_ms=(time.perf_counter() - capture_started) * 1000,
                top=capture_top, candidate_digest=capture_digest,
                max_tier=args.max_tier,
            )
        if args.json:
            _emit(payload, True)
        else:
            lines = [f"== decision layer ({len(decisions)}) =="]
            for h in decisions:
                lines.append(f"  {h['id']}  ({h['classification']})  {h.get('date') or 'undated'}")
                for x in h.get("tensions", []):
                    ident = x.get("identity", "")
                    caveat = (" [identity: %s — title/calendar-derived, weigh accordingly]" % ident
                              if ident and ident not in ("content-verified", "filename")
                              else "")
                    lines.append(f"    !! newer source post-dates this decision: "
                                 f"{x['id']} ({x['date']}){caveat} — report the tension, "
                                 f"never promote the proposal")
            lines.append(f"== sources under consideration ({len(sources)}) ==")
            lines += [f"  {h['id']}  <{h.get('type') or '?'}>  {h.get('date') or 'undated'}"
                      for h in sources]
            if res["retired_excluded"]:
                lines.append(f"-- {res['retired_excluded']} retired version(s) excluded")
            footer = _egress_footer(report)
            if freshness and freshness.get("hint"):
                footer += f"\n-- {freshness['hint']}"
            _emit(None, False, "\n".join(lines + [footer]))
        return 0

    if cmd == "grep":
        items = core.grep(args.pattern, k=args.k, regex=args.regex)
        surfaced, report = _filter_dicts(items, args.max_tier)
        if args.json:
            _emit({"pattern": args.pattern, "results": surfaced, "egress": report}, True)
        else:
            lines = [f"{h['id']} ({h['classification'] or 'UNLABELLED'}) "
                     f"x{h['match_count']}\n    {h['snippet']}" for h in surfaced]
            footer = _egress_footer(report)
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
            footer = _egress_footer(report)
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
            footer = _egress_footer(report)
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
            lines.append(_egress_footer(report))
            _emit(None, False, "\n".join(lines))
        return 0

    if cmd == "inbox":
        if args.answer is not None:
            if not args.value:
                _emit(None, False, "error: --answer KEY requires --value TEXT")
                return 2
            matched = core.answer_question(args.answer, args.value)
            if args.json:
                _emit({"answered": matched, "key": args.answer}, True)
            else:
                _emit(None, False,
                      (f"recorded answer to {args.answer}" if matched
                       else f"no open question with key {args.answer}"))
            return 0 if matched else 1
        questions = core.open_questions()
        if args.json:
            _emit({"open": questions, "count": len(questions)}, True)
        elif not questions:
            _emit(None, False, "inbox: 0 owner decisions pending.")
        else:
            lines = [f"{len(questions)} owner decision(s) pending:\n"]
            for q in questions:
                lines.append(f"[{q.get('key')}] {q.get('question')}")
                if q.get("context"):
                    lines.append(f"    context: {q['context']}")
                for opt in q.get("options", []):
                    mark = " (default)" if opt == q.get("default") else ""
                    lines.append(f"    - {opt}{mark}")
                lines.append(f"    answer: brain inbox --answer {q.get('key')} --value '<option>'\n")
            _emit(None, False, "\n".join(lines))
        return 0

    if cmd == "retro":
        res = core.retro()
        if args.json:
            _emit(res, True)
        else:
            fnd = res["findings"]
            if not fnd:
                _emit(None, False, "retro: no engine failure signatures found.")
            else:
                lines = [f"retro: {len(fnd)} signature(s) found:"]
                for sig, ev in fnd.items():
                    lines.append(f"  - {sig}: {len(ev)} instance(s)")
                if res["feedback_written"]:
                    lines.append(f"wrote engine-feedback: {', '.join(res['feedback_written'])}")
                _emit(None, False, "\n".join(lines))
        return 0

    if cmd == "cos-propose":
        content = args.content if args.content is not None else sys.stdin.read()
        try:
            if args.kind == "correction":
                res = core.cos_propose_correction(json.loads(content))
            else:
                res = core.cos_propose(content, ident=args.id)
        except (ValueError, TypeError) as exc:  # unsafe id / bad payload -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-propose refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"dropped unsigned {args.kind} -> {res.get('proposal') or res.get('drop')} "
              f"(the host broker + owner inbox gate what gets signed)", args.json)
        return 0

    if cmd == "cos-run-begin":
        try:
            res = core.cos_run_begin(run_id=args.run_id, lane=args.lane,
                                     skill_path=args.skill,
                                     attended=bool(getattr(args, "attended",
                                                           False)))
        except Exception as exc:  # RoleError / unresolvable lane -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-run-begin refused ({type(exc).__name__}): {exc}",
                  args.json)
            return 3
        _emit(res if args.json else
              f"run {res['run_id']} begun: {res.get('bundle_version')} "
              f"(ext {res.get('extraction_rules_version')}) from {res['skill_path']} "
              f"[{res['skill_sha256'][:12]}…]", args.json)
        return 0

    if cmd == "cos-corpus-check":
        try:
            res = core.cos_corpus_check(args.run_id)
        except Exception as exc:  # NoBodiesToJudge / RoleError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-corpus-check REFUSED ({type(exc).__name__}): {exc}",
                  args.json)
            return 3
        _emit(res if args.json else
              f"cos-corpus-check: {res['judgeable']} of {res['rows']} row(s) "
              f"carry body text ({res['bodyless']} bodyless) — judging may "
              f"proceed over the {res['judgeable']} bodied row(s)", args.json)
        return 0

    if cmd == "cos-corpus-append":
        try:
            if bool(args.conversation_id) == bool(args.bodyless):
                raise ValueError(
                    "give exactly ONE of --conversation-id (one thread whose "
                    "body was opened, its text on stdin) or --bodyless (the "
                    "threads that were enumerated and never opened)")
            if args.conversation_id:
                text = args.text if args.text is not None else sys.stdin.read()
                if not text.strip():
                    # A row asserting an opened body with nothing in it is
                    # exactly run 65's shape — a read that did not happen,
                    # recorded as one that did.
                    raise ValueError(
                        f"no message text for {args.conversation_id!r}. A row "
                        f"claiming an opened body with nothing in it is a read "
                        f"that did not happen; use --bodyless for a thread "
                        f"that was never opened.")
                rows = [{"conversation_id": args.conversation_id, "text": text,
                         "sender": args.sender, "sent": args.sent,
                         "subject": args.subject, "read_lane": args.read_lane,
                         "body_opened": True}]
            else:
                rows = [{"conversation_id": c, "text": "",
                         "read_lane": args.read_lane, "body_opened": False}
                        for c in args.bodyless]
            res = core.cos_corpus_append(args.run_id, rows)
        except Exception as exc:  # CorpusRefused / RoleError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-corpus-append REFUSED ({type(exc).__name__}): {exc}",
                  args.json)
            return 3
        _emit(res if args.json else
              f"cos-corpus-append: {res['appended']} row(s) -> {res['run']} "
              f"({res['chars']} chars of message text)", args.json)
        return 0

    if cmd == "cos-corpus-close":
        try:
            res = core.cos_corpus_close(args.run_id)
        except Exception as exc:  # CorpusClosed / RoleError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-corpus-close REFUSED ({type(exc).__name__}): {exc}",
                  args.json)
            return 3
        _emit(res if args.json else
              f"cos-corpus-close: {res['run']} closed with {res['rows']} row(s) "
              f"— read-only from here; retention deletes it whole", args.json)
        return 0

    if cmd == "cos-corpus-reopen":
        try:
            res = core.cos_corpus_reopen(args.run_id)
        except Exception as exc:  # CorpusRefused / RoleError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-corpus-reopen REFUSED ({type(exc).__name__}): {exc}",
                  args.json)
            return 3
        _emit(res if args.json else
              f"cos-corpus-reopen: {res['run']} is open again — {res['reason']}"
              f". The false close stays on the file; keep appending, then "
              f"close for real.",
              args.json)
        return 0

    if cmd == "cos-broker":
        try:
            res = core.cos_broker_fold()
        except Exception as exc:  # RoleError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-broker refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        if args.json:
            _emit(res, True)
        else:
            claimed = res.get("claimed", {}) or {}
            consumed = res.get("consumed", {}) or {}
            batch = res.get("batch", {}) or {}
            _emit(None, False,
                  f"cos-broker: claimed={len(claimed.get('claimed', []))} "
                  f"rejected={len(claimed.get('rejected', []))} "
                  f"accepted->capture-inbox={len(consumed.get('accepted', []))} "
                  f"holds-released={len(res.get('holds_released', []))} "
                  f"batch-enqueued={batch.get('enqueued', False)} "
                  f"errors={len(res.get('errors', []))}")
        return 0 if not res.get("errors") else 1

    if cmd == "cos-ingest-sweep":
        try:
            res = core.cos_ingest_sweep(downloads_dir=args.downloads_dir,
                                        dry_run=args.dry_run)
        except Exception as exc:  # RoleError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-ingest-sweep refused ({type(exc).__name__}): {exc}",
                  args.json)
            return 3
        _emit(res if args.json else
              f"cos-ingest-sweep{' (dry-run)' if args.dry_run else ''}: "
              f"moved={len(res['moved'])} unmatched={len(res['unmatched'])} "
              f"refused={len(res['refused'])} "
              f"already-claimed={res['already_claimed']}", args.json)
        return 0

    if cmd == "cos-correct":
        try:
            res = core.cos_correct(args.round_, args.msg_key, args.bucket, args.tier)
        except Exception as exc:  # RoleError / ValueError -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-correct refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"correction recorded: round={res['round']} msg={res['msg_key']} "
              f"-> {res['corrected_bucket']}/{res['corrected_tier']}", args.json)
        return 0

    if cmd == "cos-evidence":
        try:
            if args.action == "sign":
                missing = [f for f in ("bundle_version", "model_version", "dataset_window")
                           if not getattr(args, f)]
                if missing:
                    raise ValueError(f"sign requires --{missing[0].replace('_', '-')}")
                from pathlib import Path as _P
                res = core.cos_evidence_sign(
                    bundle_version=args.bundle_version,
                    model_version=args.model_version,
                    dataset_window=args.dataset_window,
                    files=[_P(f) for f in args.files], name=args.name)
            else:
                if not args.dir:
                    raise ValueError("verify requires --dir")
                res = core.cos_evidence_verify(args.dir)
        except Exception as exc:
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-evidence refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        if args.action == "verify":
            _emit(res if args.json else
                  f"evidence: {'VALID' if res['ok'] else 'INVALID'} "
                  f"({len(res['errors'])} error(s))"
                  + ("".join(f"\n  - {e}" for e in res["errors"])), args.json)
            return 0 if res["ok"] else 1
        _emit(res if args.json else
              f"signed evidence bundle -> {res['dir']}", args.json)
        return 0

    if cmd == "cos-priority-map":
        try:
            res = core.cos_priority_map(max_tier=args.max_tier)
        except Exception as exc:
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-priority-map refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"priority map -> {res['path']} ({res['people']} people, "
              f"{res['companies']} companies, {res['withheld']} withheld at "
              f"max-tier={res['max_tier']})", args.json)
        return 0

    if cmd == "cos-report":
        try:
            res = core.cos_report()
        except Exception as exc:
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-report refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"cos-report: rounds={res['rounds_completed']} "
              f"verdicts={res['verdicts']} corrections={res['corrections']} "
              f"overall-bucket-precision={res['overall_bucket_precision']}", args.json)
        return 0

    if cmd == "cos-hold":
        try:
            if args.action == "add":
                if not getattr(args, "not_before", None):
                    raise ValueError("add requires --not-before <ISO timestamp>")
                content = args.content if args.content is not None else sys.stdin.read()
                res: Any = core.cos_hold_add(content, not_before=args.not_before,
                                             ident=args.id)
                human = (f"held {res['id']} until {res['not_before']} "
                         f"(unsigned; enters capture-inbox only after expiry)")
            elif args.action == "list":
                res = {"holds": core.cos_hold_list()}
                human = "\n".join(
                    f"{h.get('id')}  not_before={h.get('not_before')}  due={h.get('due')}"
                    for h in res["holds"]) or "no holds"
            elif args.action == "cancel":
                if not args.id:
                    raise ValueError("cancel requires --id")
                undo = core.cos_hold_undo(args.id)
                res = {**undo, "cancelled": undo["undone"]}
                if undo["undone"]:
                    human = (f"undo of {undo['id']} from state "
                             f"{undo['state_before']}: {undo['action']}")
                    if undo.get("demoted"):
                        human += (f" (category {undo['demoted']['category']} "
                                  f"demoted from auto-ingest)")
                else:
                    human = (f"nothing to undo for {undo['id']} "
                             f"(state={undo['state_before']})")
            else:  # release-due
                released = core.cos_hold_release_due()
                res = {"released": released}
                human = (f"released {len(released)} due hold(s) into the "
                         f"approved queue (host-only; signed on the next drain)"
                         if released else "no due holds")
        except Exception as exc:
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-hold refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else human, args.json)
        if args.action == "cancel" and not res.get("cancelled"):
            return 1
        return 0

    if cmd == "cos-spine":
        try:
            if args.action == "record":
                res = core.cos_spine_record(
                    event=args.event, direction=args.direction,
                    counterparty=args.counterparty, text=args.text, topic=args.topic,
                    due=args.due, source_ref=args.source_ref, note=args.note,
                    commitment_id=args.commitment_id)
                human = (f"{res['id']}: {args.event} -> status={res['status']} "
                         f"due={res.get('due')}")
            elif args.action == "radar":
                res = core.cos_spine_radar()
                human = (f"late={len(res['late'])} at_risk={len(res['at_risk'])}" +
                         "".join(f"\n  LATE  {r['id']} {r['counterparty']} due={r['due']}"
                                for r in res["late"]) +
                         "".join(f"\n  RISK  {r['id']} {r['counterparty']} due={r['due']}"
                                for r in res["at_risk"]))
            elif args.action == "grounding-pack":
                res = core.cos_grounding_pack()
                human = (f"rendered {res['path']} (documents={res['documents']} "
                         f"requested={res['requested']} missing={len(res['missing'])})")
            else:  # render
                res = core.cos_spine_render()
                human = f"rendered {res['path']} (open={res['open']} late={res['late']} at_risk={res['at_risk']})"
        except Exception as exc:
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"cos-spine refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else human, args.json)
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
        if getattr(args, "progress", False):
            os.environ["BRAIN_PROGRESS"] = "1"
        try:
            res = core.rebuild(json_mode=args.json)
        except Exception as exc:  # H-4: no raw tracebacks from maintenance cmds
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"rebuild failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              f"indexed {res['indexed']} notes ({res['chunks']} chunks) via "
              f"{res['backend']} [{res['embed_model']} d={res['embed_dim']}] -> {res['db']}"
              + _excluded_note(res),
              args.json)
        return 0

    if cmd == "warmup":
        if getattr(args, "progress", False):
            os.environ["BRAIN_PROGRESS"] = "1"
        try:
            res = core.warmup(json_mode=args.json)
        except Exception as exc:  # H-4: no raw tracebacks from maintenance cmds
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"warmup failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        _emit(res if args.json else
              (f"embedder {res['model_id']} already cached "
               if res["already_cached"] else f"downloaded embedder {res['model_id']} ")
              + f"({res['elapsed_s']}s). Run `brain sync` to apply it to the index "
              + "if `brain status` shows embedder: pending.",
              args.json)
        return 0

    if cmd == "sync":
        if getattr(args, "progress", False):
            os.environ["BRAIN_PROGRESS"] = "1"
        try:
            res = core.sync(drain=not args.no_drain, publish=args.publish, json_mode=args.json)
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
            reb = res.get("rebased", 0)
            reb_note = (f"; vault root changed — rebased {reb} path(s), no re-embed"
                        if reb else "")
            _emit(None, False,
                  f"sync [{res['mode']}]: +{res.get('added',0)} ~{res.get('updated',0)} "
                  f"-{res.get('deleted',0)} ={res.get('unchanged',0)} "
                  f"({res['chunks']} chunks); drained {d.get('promoted',0)} "
                  f"(skipped {d.get('skipped',0)})" + reb_note + tail
                  + _excluded_note(res))
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

    if cmd == "restore-index":
        try:
            res = core.restore_index_from_snapshot(force=args.force, dry_run=args.dry_run)
        except Exception as exc:  # H-4
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"restore-index failed ({type(exc).__name__}): {exc}", args.json)
            return 3
        if args.json:
            _emit(res, True)
        elif res.get("dry_run"):
            _emit(f"[dry-run] would restore {res['snapshot_notes']} notes from the snapshot "
                  f"(live index now: {res['live_notes_before']}) — nothing written", False)
        else:
            _emit(f"restored index from snapshot: {res['live_notes_after']} notes "
                  f"(prior index backed up at {res['backup']})", False)
        return 0

    if cmd == "status":
        res = core.status(args.snapshot_dest)
        if args.json:
            _emit(res, True)
        else:
            ix, sn, ver = res.get("index", {}), res.get("snapshot", {}), res.get("version", {})
            emb = res.get("embedder", {})
            emb_line = f"embedder: {emb.get('state','?')} [{emb.get('model_id','?')}]"
            if emb.get("state") == "pending":
                hint = emb.get("download_size_hint")
                emb_line += (" — run `brain warmup`"
                             + (f" ({hint} download)" if hint else "")
                             + " then `brain sync` for real semantic search")
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
            # LIVENESS (HARDENED:claude-2): an unanswered COS ingestion batch
            # is not an error anywhere — it just quietly re-kills the funnel
            # behind the one-open-batch backpressure. Say so out loud.
            live = (res.get("cos") or {}).get("batch_liveness") or {}
            if live.get("alert"):
                skew_lines.append(f"  WARNING: {live['alert_text']}")
            # R8 (2026-07-30 review): the JSON status and the morning brief both
            # carry `unstamped_batched`, but `brain status` — the primary human
            # diagnostic — printed nothing, so an operator read a healthy status
            # while EVERY candidate was being diverted for a missing stamp.
            if live.get("unstamped_batched"):
                skew_lines.append(
                    f"  WARNING: {live['unstamped_batched']} COS candidate(s) sent to "
                    f"the owner batch for a missing category/ruleset stamp — "
                    f"pattern auto-capture {live.get('pattern_autocapture', 'suspended')}")
            # INS-01: a run the host validator could not certify. Loud here
            # because run 59 skipped its whole self-eval and NOTHING noticed —
            # an instrument that only writes a log is the failure being fixed.
            if live.get("run_validity_text"):
                skew_lines.append(f"  WARNING: {live['run_validity_text']}")
            # STA-01: same treatment for a candidate the host could not
            # attribute to a VALID run — quarantined, never silently bound.
            if live.get("quarantine_text"):
                skew_lines.append(f"  WARNING: {live['quarantine_text']}")
            _emit(None, False,
                  f"brain {ver.get('package_version','?')}\n"
                  f"index: {ix.get('notes','?')} notes / {ix.get('chunks','?')} chunks "
                  f"[{ix.get('embed_model','?')} d={ix.get('embed_dim','?')}]\n"
                  f"{emb_line}\n"
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

    if cmd == "unsupersede":
        try:
            res = core.unsupersede(args.old_id, args.new_id, reason=args.reason)
        except Exception as exc:  # RoleError / ValueError / KeyUnavailable -> fail closed
            _emit({"error": type(exc).__name__, "detail": str(exc)} if args.json
                  else f"unsupersede refused ({type(exc).__name__}): {exc}", args.json)
            return 3
        # Report what actually happened. `unsupersede` repairs the successor
        # OPPORTUNISTICALLY (`new_write` stays None when nothing on that side
        # named old_id), so "both sides signed" was a false audit assurance
        # precisely on the malformed one-sided chains this verb exists for
        # (adversarial review round 3, 2026-08-10).
        if res.get("new_write"):
            how = (f"both sides signed: dropped "
                   f"{', '.join(res.get('cleared_keys') or []) or 'no keys'} "
                   f"from {res['new_id']}")
        else:
            kept = res.get("new_previous_version_kept")
            how = (f"ONE side signed ({res['old_id']} only) — {res['new_id']} "
                   + (f"names {kept!r} as its predecessor, not {res['old_id']}, "
                      "so it was left untouched"
                      if kept else
                      f"never named {res['old_id']} as its predecessor, so "
                      "there was nothing to clear"))
        _emit(res if args.json else
              f"unlinked {res['old_id']} -> {res['new_id']} ({how})",
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
        res = core.verify_audit(check_content=args.check_content)
        text = (f"audit chain: {res['status']} ({res['entries_checked']} entries, "
                f"{len(res['errors'])} errors)")
        # INT-02: never let a signature-only pass read as a content all-clear.
        unexplained = res.get("content_drift_unexplained", 0)
        text += (f"\ncontent drift: {res.get('content_drift_count', 0)} note(s) changed "
                 f"since signing, {unexplained} unexplained")
        if not args.check_content:
            text += " — run `brain verify-audit --check-content --json` for the list"
        elif res.get("content_drift"):
            for rec in res["content_drift"]:
                mark = rec.get("disposition") or "UNEXPLAINED"
                text += f"\n  {mark:<24} {rec['issue']:<14} {rec['path']}"
        _emit(res if args.json else text, args.json)
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

    if cmd == "health-report":
        res = core.health_report()
        if args.json:
            _emit(res, True)
        else:
            _emit(None, False,
                  f"health report [{res['verdict']}] written -> {res['path']}"
                  + (f" ({len(res['act_now'])} item(s) need attention)" if res["act_now"] else ""))
        return 0 if res["verdict"] != "BROKEN" else 1

    if cmd == "graph-report":
        res = core.graph_report()
        if args.json:
            _emit(res, True)
        else:
            _emit(None, False,
                  f"graph report written -> {res['path']} "
                  f"(gen {res['graph_generation']}, {res['nodes']} nodes, "
                  f"{res['edges']} edges, {res['points']} points)")
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

    if cmd == "sweep-workspace":
        from pathlib import Path

        env_dirs, env_age = maint.workspace_sweep_config()
        dirs = [Path(d).expanduser() for d in args.dirs] if args.dirs else env_dirs
        age = args.age_days if args.age_days else env_age
        if not dirs:
            _emit({"error": "no_dirs",
                   "detail": "no workspace dirs: pass --dir or set "
                             f"${maint.WORKSPACE_SWEEP_DIRS_ENV}"} if args.json
                  else f"no workspace dirs: pass --dir or set "
                       f"${maint.WORKSPACE_SWEEP_DIRS_ENV}", args.json)
            return 2
        res = maint.sweep_workspace(dirs, Path(core.vault) / "inbox", age,
                                    dry_run=args.dry_run)
        if args.json:
            _emit(res, True)
        else:
            _emit(None, False,
                  f"sweep-workspace [dry_run={res['dry_run']}] age>{res['age_days']}d: "
                  f"{len(res['swept'])} swept, {res['skipped_active']} still active, "
                  f"{len(res['missing_dirs'])} missing dir(s), "
                  f"{len(res['errors'])} error(s)"
                  + ("\nnext: `brain sync --publish` (or the nightly) drains "
                     "inbox/ into signed raw/ notes" if res["swept"] and not res["dry_run"] else ""))
        return 0

    if cmd == "maintain":
        parsed_date = None
        if args.date:
            import datetime as _dt
            parsed_date = _dt.date.fromisoformat(args.date)
            # Field bug 1 (2026-07-13): a `brain maintain --date <future>` run
            # against a LIVE vault stamped future-dated hot.md idempotency keys,
            # briefs and digests — which then SUPPRESS the legitimate real run
            # for that date and shadow its outputs. A future --date is only ever
            # a deliberate date-gate exercise; refuse it by default so the leak
            # can't happen by accident. (A stuck OS clock can't be caught here —
            # date.today() would already be wrong — but that produces one bad
            # date, not the observed sequence, which was --date leakage.)
            if parsed_date > _dt.date.today() and not args.allow_future_date:
                _emit(None, False,
                      f"refusing --date {parsed_date.isoformat()}: it is AFTER the "
                      f"wall-clock date {_dt.date.today().isoformat()}. A future date "
                      f"would poison hot.md/brief/digest for that day and suppress the "
                      f"real run. Pass --allow-future-date only for a deliberate "
                      f"date-gate exercise on a throwaway vault.")
                return 2
        res = core.maintain(dry_run=args.dry_run, today=parsed_date, min_score=args.min_score)
        if args.json:
            _emit(res, True)
        else:
            head = (f"maintain [dry_run={res['dry_run']}] {res['date']} ({res['weekday']}) "
                    f"branches_due={res['branches_due']}")
            _emit(None, False, head + "\n" + maint.render_outcomes_markdown(res["outcomes"]))
        return 0

    if cmd == "graphify":
        if getattr(args, "progress", False):
            os.environ["BRAIN_PROGRESS"] = "1"
        res = core.graphify(force=args.force, dry_run=args.dry_run,
                             max_tier=args.max_tier, candidate_limit=args.n,
                             json_mode=args.json)
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
