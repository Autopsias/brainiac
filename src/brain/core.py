"""BrainCore — the engine. Importable, but NOT the integration surface.

CRITICAL CONTRACT: the read verbs here (``search``/``get``/``recent``) return
**UNFILTERED** results. The deny-by-default classification filter lives in the
CLI (brain.cli), applied as the final stage before stdout. Importing BrainCore
in-process therefore BYPASSES the egress filter — by design. This is exactly why
the filter is an egress-decision mechanism, not containment: real containment is
workspace projection (brain.projection) + the host/VM trust split.

The write verb (``write_note``) is a HOST-BROKER privilege: it appends to the
Ed25519 audit chain (CORE-03) and fails closed if no signing key resolves.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from . import classification
from . import config
from . import frontmatter
from . import provenance
from .audit import AuditChain, KeyUnavailable
from .index import BrainIndex, Hit
from .lock import WriterLockBusy, _atomic_temp_path, vault_writer_lock
from .notes import note_from_text, safe_slug, sha256_text


# -- RET-05 fan-out knobs (see BrainCore.search_multi) ----------------------
# The OUTER pooling constant. NOT `index.RRF_K_FUSE` and NOT the inner
# `RRF_K_EXACT` pin: the fan-out layer has never been measured (RET-11 closed
# the inner single-query layer), so it keeps the value it has always pooled at
# and s04 measures the alternatives as pre-registered arms.
MULTI_RRF_K = 60
# Variant cap. The SELECTION policy lives with the caller (s03's language
# census supplies variants in descending prevalence); this only bounds how many
# inner searches one call may fan out to, and the dropped tail is reported.
MULTI_MAX_VARIANTS = 4
# Correlated-vote guard band: "some variant ranked it this high" (rank-space
# only — there is deliberately no score threshold anywhere in this guard).
MULTI_GUARD_STRONG_RANK = 3


def _env_switch(name: str, default: bool) -> bool:
    """Kill-switch env contract, identical in shape to BRAIN_EXACT_LEG_ENABLED."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_positive_int(name: str, default: int) -> int:
    """A positive int from the environment; anything else is loudly ignored."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        print(f"brain: ignoring {name}={raw!r} (want a positive integer); "
              f"using {default}", file=sys.stderr)
        return default
    return value


def _contained_in(target: Path, base: Path) -> bool:
    """True iff RESOLVED ``target`` is strictly inside RESOLVED ``base``.

    Uses Path.relative_to on resolved paths — never string-prefix checks
    (sibling-directory bypass, e.g. ``vault-x`` matching ``vault``). Resolving
    also follows symlinks, so a symlink inside the vault pointing outside it
    fails containment. Path.resolve() is non-strict, so a not-yet-existing
    target (draft-capture writes NEW files) resolves fine.
    """
    target = target.resolve()
    base = base.resolve()
    if target == base:
        return False
    try:
        target.relative_to(base)
    except ValueError:
        return False
    return True


def _fsync_dir_strict(d: Path) -> None:
    """fsync a directory ENTRY and RAISE if it fails.

    ``cos._fsync_dir`` swallows every ``OSError`` from both the open and the
    fsync. That is the right call for a best-effort flush, and the wrong one
    to build a durability DECISION on: ``supersede`` unlinks its crash journal
    on the strength of "both notes are on disk", and a silently-failed fsync
    means it isn't. One extra directory fsync is microseconds; a silent one is
    a lost rollback record (adversarial review round 3, 2026-08-10)."""
    if os.name == "nt":
        return          # Windows has no directory descriptors to sync
    dfd = os.open(d, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _write_atomic_durable(path: Path, data: bytes, *, mode: int) -> None:
    """Replace ``path`` with ``data`` atomically and DURABLY, or raise.

    Same shape as ``cos._write_atomic`` — unpredictable temp name,
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` (a pre-created symlink at the temp name is
    how a predictable ``<target>.tmp`` gets an attacker's file overwritten),
    regular-file check, short-write loop, ``fsync``, ``os.replace``, parent
    fsync. It is a SEPARATE function on purpose, and the reason is layering,
    not preference: a vault note write must not route through a
    chief-of-staff helper. Pointing ``write_note`` at ``cos._write_atomic``
    made every note write intercept a COS *test double*
    (``test_cos_approved_queue.py`` monkeypatches that symbol to inject a
    staging crash), so an unrelated COS test started crashing the drain. A
    shared primitive whose substitutions are scoped to one subsystem is not
    actually shared.

    Unlike ``cos._fsync_dir``, the parent fsync RAISES on failure —
    ``supersede`` unlinks its crash journal on the strength of "both notes are
    on disk", and a silently-failed fsync means they are not."""
    import stat as _stat

    tmp = _atomic_temp_path(path)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(tmp, flags, 0o600)
    closed = False
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"refusing to write {tmp.name}: not a regular file")
        view = memoryview(data)
        while view:                       # os.write may write only part of it
            n = os.write(fd, view)
            if n <= 0:
                raise OSError(f"write made no progress on {tmp.name} "
                              f"({len(view)} bytes left)")
            view = view[n:]
        os.fsync(fd)
        if mode != 0o600:
            os.fchmod(fd, mode)           # on the FD — the name is never re-resolved
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_dir_strict(path.parent)


def _write_note_durable(target: Path, content: str) -> None:
    """Atomically replace ``target`` with ``content``, durably.

    ``0o644``: a vault note is ordinary readable content, not host-private
    state (the 0o600 default belongs to the COS queues and the crash journal)."""
    _write_atomic_durable(target, content.encode("utf-8"), mode=0o644)


def _stamp_draft_frontmatter(content: str, note_id: str, is_source: bool) -> str:
    """Return ``content`` with draft markers ensured (idempotent, non-clobbering).

    Guarantees the staged file carries frontmatter with an ``id``, ``status:
    draft`` and ``provenance.trust: untrusted`` so (a) the host drain's
    ``load_note`` can read it and (b) any reader can see it is an uncommitted,
    untrusted draft. Existing keys are never overwritten — capture is additive.
    """
    meta, body = frontmatter.parse_text(content)
    if not content.startswith("---") or not meta:
        # No (or unparseable) frontmatter — synthesise a minimal block.
        dtype = "source" if is_source else "note"
        return (
            f"---\nid: {note_id}\ntype: {dtype}\nstatus: draft\n"
            f"provenance.trust: untrusted\n---\n\n{content.lstrip()}\n"
        )
    block, after = content.split("---", 2)[1], content.split("---", 2)[2]
    additions = []
    if "id" not in meta:
        additions.append(f"id: {note_id}")
    if "status" not in meta:
        additions.append("status: draft")
    if "provenance.trust" not in meta:
        additions.append("provenance.trust: untrusted")
    if not additions:
        return content
    new_block = block.rstrip("\n") + "\n" + "\n".join(additions) + "\n"
    return f"---{new_block}---{after}"


def _audit_status_summary(audit_res: dict[str, Any]) -> str:
    """One line naming WHAT is wrong with the chain — signature/linkage errors
    and, since INT-02, content drift. "status=content_drift (0 error(s))" alone
    reads like a chain with nothing wrong."""
    parts = [f"audit chain status={audit_res.get('status')}",
             f"({len(audit_res.get('errors', []))} chain error(s)"]
    unexplained = audit_res.get("content_drift_unexplained")
    if unexplained:
        parts.append(f", {unexplained} of {audit_res.get('content_drift_count')} "
                     f"signed note(s) changed after signing with no disposition")
    return "".join(parts) + ")"


class RoleError(RuntimeError):
    """A host-broker operation was attempted from the read+draft-only VM leg.

    The VM leg (``role=vm``) may never write notes, mutate/WAL the index, publish
    a snapshot, or resolve a signing key. These ops fail with RoleError BEFORE
    any signing-key resolution or index write is attempted (S06 hard guarantee).
    """


class SupersedePreconditionFailed(ValueError):
    """A caller's out-of-band ``expect`` preconditions no longer hold.

    Raised INSIDE ``supersede``'s writer lock, before the first signed write,
    so a proposal decided against one state of the vault can never apply
    against another (VER-02: an owner-accepted supersede proposal sits in the
    queue while the nightly folds keep running).
    """


class SupersedeJournalUnreadable(RuntimeError):
    """The crash journal for an unfinished ``supersede``/``unsupersede`` exists
    but cannot be parsed, so the pre-transaction content of a half-written
    version chain is not recoverable automatically.

    Fail closed: the journal is PRESERVED and every supersession verb refuses
    until a human repairs the two notes and removes it. Its own class because
    the nightly dedup fold swallows a per-pair `Exception` and moves on, which
    would turn a sticky, vault-wide refusal into a silent zero — the fold
    re-raises this one instead of counting it as "nothing to merge".
    """


class SupersedeNotDurable(RuntimeError):
    """This platform cannot prove the crash journal reached the disk, so the
    supersession is refused before anything is signed.

    The journal is the ONLY record of both notes' pre-transaction bytes, and
    ``supersede`` writes two signed notes on the strength of it. Where the
    write cannot be shown durable, the failure mode is a signed half-chain
    with no rollback record — strictly worse than not superseding at all
    (adversarial review round 4, 2026-08-10). Windows is the case that trips
    it: no directory descriptor to fsync, and CPython's ``os.replace`` passes
    ``MOVEFILE_REPLACE_EXISTING`` without ``MOVEFILE_WRITE_THROUGH``, so the
    move is not guaranteed to reach disk before it returns. Refusing by name
    is the fallback the review named; a durable Windows replace is a real fix
    that needs a Windows host to test, and this refusal is what says so out
    loud instead of pretending the guarantee holds.
    """


def _require_durable_replace(what: str) -> None:
    """Raise :class:`SupersedeNotDurable` unless an atomic replace on this
    platform can be PROVEN to have reached the disk."""
    if os.name == "nt":
        raise SupersedeNotDurable(
            f"refusing to {what}: durability cannot be established on this "
            f"platform (os.name={os.name!r}). Windows has no directory fsync "
            f"and CPython's os.replace is not write-through, so a power loss "
            f"can lose the crash journal that a signed half-chain would be "
            f"rolled back from. Nothing was written.")


def _mkdir_durable(d: Path) -> None:
    """``mkdir(parents=True, exist_ok=True)``, with every directory entry it
    actually CREATES fsynced into its own parent.

    ``_write_atomic_durable`` fsyncs the file and the directory it lands in,
    which is enough only when that directory already existed. On the FIRST
    transaction after the journal store is created, the directory entry itself
    is not durably anchored, so a power loss can take the whole journal with
    it (adversarial review round 4). Fsyncing the ancestry costs microseconds
    once."""
    created: list[Path] = []
    p = d
    while not p.exists():
        created.append(p)
        if p.parent == p:
            break
        p = p.parent
    d.mkdir(parents=True, exist_ok=True)
    for made in reversed(created):        # shallowest first
        _fsync_dir_strict(made.parent)


class BrainCore:
    def __init__(
        self,
        vault: str | Path | None = None,
        index: BrainIndex | None = None,
        audit_log: str | Path | None = None,
        *,
        role: str | None = None,
    ) -> None:
        self.role = config.role(role)
        self.vault = config.vault_root(vault)
        if index is not None:
            self.index = index
        elif self.role == config.ROLE_VM:
            # VM leg reads ONLY the published read-only snapshot — never the
            # authoritative writable index, never WAL.
            self.index = BrainIndex(db_path=config.snapshot_db_path(self.vault),
                                    read_only=True)
        else:
            # Field bug 3: before opening the index/audit dir, migrate a legacy
            # absolute-path-keyed dir onto the move-stable vault-id key so a
            # vault move never re-embeds or forks the audit chain. Best-effort.
            config.migrate_index_location(self.vault)
            self.index = BrainIndex(db_path=config.index_path(self.vault))
        if self.role == config.ROLE_VM:
            # No signing surface AT ALL on the VM: the audit chain (and thus
            # resolve_signing_key) is simply not constructed here.
            self.audit = None
        else:
            log = Path(audit_log) if audit_log else config.default_audit_log(self.vault)
            self.audit = AuditChain(log)

    def _require_host(self, op: str) -> None:
        if self.role != config.ROLE_HOST:
            raise RoleError(
                f"role={self.role!r} may not {op}; this is a host-broker privilege "
                "(the VM leg is read + draft only). Run on the host."
            )

    # -- read verbs (UNFILTERED — see module docstring) -------------------
    def search(self, query: str, k: int = 10) -> list[Hit]:
        return self.index.search(query, k)

    def source_freshness(self, newest_hit_date: str, max_tier: str) -> dict[str, Any]:
        """RET-09 freshness signal: count + newest date of notes whose
        valid-time date is strictly newer than ``newest_hit_date``, at the
        caller's egress cap. See ``BrainIndex.freshness``."""
        return self.index.freshness(newest_hit_date, max_tier)

    def hybrid_search(
        self, query: str, k: int = 10, *, rerank: bool = False, rerank_top: int = 15,
        rrf_k: int = 60, rerank_gate: bool | None = None,
    ) -> list[Hit]:
        """Fused RRF(k) BM25 + dense retrieval (RET-01), optional skippable
        reranker (RET-02), RK-02 adaptive rerank gate. UNFILTERED — the CLI
        applies the egress gate."""
        return self.index.hybrid_search(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
            rerank_gate=rerank_gate,
        )

    def hybrid_search_with_trace(
        self, query: str, k: int = 10, *, rerank: bool = False,
        rerank_top: int = 15, rrf_k: int = 60, rerank_gate: bool | None = None,
    ):
        """Production hybrid search plus opt-in, pre-egress S03 attribution.

        Callers must still route hits through the CLI's egress gate before
        serialising either a full explanation or the compact capture digest.
        """
        return self.index.hybrid_search_with_trace(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
            rerank_gate=rerank_gate,
        )

    def diagnose_target(
        self, query: str, target_id: str, *, max_tier: str, trace: Any,
        final_rank: int | None,
    ) -> dict[str, Any]:
        """Run the S03 target probe after an unchanged production search."""
        return self.index.diagnose_target(
            query, target_id, max_tier=max_tier, trace=trace, final_rank=final_rank,
        )

    def annotate_create_safety(
        self, query: str, surfaced: list[dict[str, Any]], max_tier: str
    ) -> set[str]:
        """Finalize ADR-0008 create safety after the CLI egress decision.

        The engine can identify a full alias/title owner, but only the egress
        boundary knows whether every owner is visible at the caller's cap.
        """
        return self.index.annotate_create_safety(query, surfaced, max_tier)

    def hybrid_search_graph(
        self, query: str, k: int = 10, *, rerank: bool = False, rerank_top: int = 15,
        rrf_k: int = 60, depth: int = 2, graph_weight: float = 0.5,
        seed_flat_top: int = 3, flat_pool: int = 30, return_trace: bool = False,
    ):
        """Gated graph-augmented multi-hop retrieval (RET-06).

        Single-hop queries pass through to ``hybrid_search`` UNCHANGED (the gate
        does not fire); multi-hop-shaped queries (>= 2 named non-hub entities)
        get a wikilink-graph expansion fused into the flat ranking. DISCOVERY-
        ONLY (RET-03): the graph never overrides an authoritative flat hit. See
        ``brain.multihop``. UNFILTERED — the CLI applies the egress gate."""
        return self.index.hybrid_search_graph(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
            depth=depth, graph_weight=graph_weight, seed_flat_top=seed_flat_top,
            flat_pool=flat_pool, return_trace=return_trace,
        )

    def search_multi(
        self, queries: "list[str]", k: int = 10, *, rerank: bool = False,
        rerank_top: int = 15, rrf_k: int = 60, per_query_k: int | None = None,
        rerank_fused: bool | None = None, fused_pool: int = 20,
        rerank_gate: bool | None = None, fanout_k: int | None = None,
        max_variants: int | None = None, guard: bool | None = None,
        return_trace: bool = False,
    ) -> "list[Hit] | tuple[list[Hit], dict[str, Any]]":
        """Multi-query fan-out (RET-05) — the AGENTIC retrieval primitive.

        Run ``hybrid_search`` for EACH query variant and Reciprocal-Rank-Fuse the
        result lists into one ranking. This is the recovery for cross-boundary
        misses (query-language ≠ document-language; query-vocabulary ≠
        note-vocabulary): an agent issues the original query PLUS reformulations
        (e.g. a cross-lingual rephrase, a synonym expansion, a HyDE answer) and
        this fuses them. A PT query and its EN rephrase reach the same EN-content
        note through different legs; RRF promotes the note that appears across
        lists. Empirically this TIES Smart Connections on monolingual PT
        (0.736 vs 0.750) where any single query trails it by ~0.10 — and brain
        already beats SC on EN / cross-lingual / temporal / multi-hop, so fan-out
        closes the one stratum that single-query retrieval lost. See
        docs/operations/s10-agentic-retrieval-analysis.md.

        The caller supplies the variants (the agent/LLM generates them — brain
        stays model-agnostic and offline). A single-element list degrades exactly
        to ``hybrid_search``. UNFILTERED — the CLI applies the egress gate.

        TWO CONSTANTS, DELIBERATELY DECOUPLED (2026-08-09). ``rrf_k`` is the
        INNER per-variant constant and stays pinned at the production value: it
        is ADR-0008's calibration key, so any other value silently drops the
        exact leg from every variant search (``_exact_leg_enabled``). ``fanout_k``
        (env ``$BRAIN_MULTI_RRF_K``) is the OUTER pooling constant used HERE and
        nowhere else. They were one value until this session, which made the
        outer layer impossible to vary without also moving the inner ranking —
        and the outer one runs at 60, the exact constant RET-11 (d5b2c58) proved
        discards answers one leg found first. Owner ruling 2026-08-09: the outer
        layer is NEW territory (RET-11 closed the INNER single-query residual),
        so the default is left where it has always been and s04 MEASURES it
        rather than this session assuming ``RRF_K_FUSE``.

        GUARDS (rank-space only — no score thresholds):
        * degenerate variants are deduplicated after identity normalization, so
          a variant that collapses onto another can never double-count;
        * a variant that returns nothing contributes nothing (no threshold);
        * variants past ``max_variants`` (env ``$BRAIN_MULTI_MAX_VARIANTS``) are
          dropped from the TAIL — the caller supplies variants in descending
          language prevalence (s03's census), so the dropped set is the
          lowest-prevalence languages, deterministically, and it is reported in
          the trace rather than silently truncated;
        * ``$BRAIN_VARIANTS_ENABLED=0`` is the kill switch (mirrors
          ``BRAIN_EXACT_LEG_ENABLED``): every variant past the first is ignored.

        POOLED RERANK IS AUTO-ON FOR FAN-OUT, AND ONLY FOR FAN-OUT (owner
        ruling 2026-08-12, ``_decisions/invariants-s11-ship-ruling.md``).
        ``rerank_fused=None`` (the default) resolves to ON when 2 or more
        distinct variants actually survive the guards above, and to OFF
        otherwise — the single-query path is untouched and keeps the shipped
        single-query rerank behaviour byte for byte. An explicit ``True``/
        ``False`` always wins; absent that, ``$BRAIN_RERANK_FUSED_DISABLED=1``
        is the global kill switch (same env contract as
        ``BRAIN_RERANK_DISABLED`` / ``BRAIN_EXACT_LEG_ENABLED``; rollback needs
        no rebuild, only a restart of the invoking process). EVIDENCE, STATED
        HONESTLY: +0.0643 recall@10 over the SHIPPED configuration (p 0.0284,
        6 wins / 1 loss / 50 ties) — **train-half only, NOT confirmed on a
        held-out half**, which under ``refuse_held_out`` it never can be. The
        famous +0.1667 is a different, mis-attributed comparison: 57 % of it is
        the reranker the vault already ships. Cost: one extra cross-encoder
        pass (~5-25 s) on fan-out calls only.

        CORRELATED-VOTE GUARD (``guard``, env ``$BRAIN_MULTI_GUARD``, default
        OFF). Variants are the SAME question re-expressed, so their votes are
        correlated, not independent evidence: summing them lets a uniformly
        mediocre document present in EVERY list out-accumulate a document one
        variant ranked first — RET-11's breadth-over-strength defect, one layer
        up. The guard is a rank-only partition: a document some variant placed
        in its top ``MULTI_GUARD_STRONG_RANK`` outranks every document no
        variant did; ordering WITHIN each band is untouched RRF. It ships OFF
        because fixing it silently would change every one of s04's arms — it is
        offered to s04 as an on/off arm, measured rather than assumed.

        IDENTITY UNDER VARIANTS (ENG-02). ``create_safety: exists`` — and the
        ADR-0008 rank-1 pin it stands for — are reserved for the ORIGINAL query
        (``variants[0]``). A generated translation can exactly match an
        UNRELATED note title; letting that claim ``exists`` would tell a capture
        agent the note it is about to write already exists and suppress a real
        note. A hit only a later variant surfaced still carries its retrieval
        evidence, but its create-safety is capped at ``probable``.

        Returns the fused hits, or ``(hits, trace)`` when ``return_trace`` —
        the trace carries the per-variant orders, each variant's contribution to
        the fused top-k, the dropped-variant sets, both constants, the guard
        state, the pin and the pooled rerank decision. Its ids are PRE-EGRESS: a
        caller that serialises it must project it onto the gated result first.
        """
        from dataclasses import replace

        from .index import rerank_gate_enabled

        asked = [q for q in (queries or []) if q and q.strip()]
        # Degenerate-variant guard: identity normalization (NFC + casefold +
        # whitespace collapse — the same normalizer the alias/title index uses)
        # is what makes "  Arctic  Embed " and "arctic embed" ONE vote.
        variants: list[str] = []
        dropped_duplicate: list[str] = []
        seen: set[str] = set()
        for q in asked:
            key = frontmatter.normalize_identity(q) or q.strip()
            if key in seen:
                dropped_duplicate.append(q)
                continue
            seen.add(key)
            variants.append(q)
        disabled = not _env_switch("BRAIN_VARIANTS_ENABLED", True)
        dropped_disabled = variants[1:] if disabled else []
        if disabled:
            variants = variants[:1]
        cap = max_variants if max_variants is not None else _env_positive_int(
            "BRAIN_MULTI_MAX_VARIANTS", MULTI_MAX_VARIANTS)
        dropped_over_cap = variants[cap:]
        variants = variants[:cap]
        fk = fanout_k if fanout_k is not None else _env_positive_int(
            "BRAIN_MULTI_RRF_K", MULTI_RRF_K)
        guard_on = guard if guard is not None else _env_switch("BRAIN_MULTI_GUARD", False)
        # S11 ship ruling (2026-08-12): auto-on for real fan-out, never for a
        # single query. Resolved AFTER the dedup/cap/kill-switch guards, so it
        # keys on the variants that will actually be pooled — two variants that
        # collapse to one under identity normalization are one query, and get
        # the single-query behaviour they are.
        rerank_fused_auto = rerank_fused is None
        if rerank_fused_auto:
            rerank_fused = (len(variants) > 1
                            and not _env_switch("BRAIN_RERANK_FUSED_DISABLED", False))
        # Per-query depth is deliberately SHALLOW (≈ k, not a wide over-fetch).
        # RRF over wide per-query lists lets a noise doc present in BOTH lists at
        # low rank (e.g. PT@50 + EN@60) out-accumulate a gold present in only ONE
        # list at high rank (e.g. EN@5) — measured: per_query_k 20→80 drops
        # monolingual_pt fan-out recall 0.736→0.625. Keep each variant's
        # contribution to its genuine top hits. Tunable via per_query_k.
        pk = per_query_k or max(k, 20)
        trace: dict[str, Any] = {
            "variants": list(variants),
            "variant_count": len(variants),
            "dropped": {
                "duplicate": dropped_duplicate,
                "over_cap": dropped_over_cap,
                "kill_switch": dropped_disabled,
                "max_variants": cap,
            },
            "fanout_k": fk,
            "inner_rrf_k": rrf_k,
            "exact_leg_enabled": self.index._exact_leg_enabled(rrf_k),
            "per_query_k": pk,
            "guard": {"enabled": guard_on, "strong_rank": MULTI_GUARD_STRONG_RANK,
                      "demoted": []},
            "rerank_fused": bool(rerank_fused),
            # "auto" = the S11 fan-out default decided it; "caller" = an explicit
            # --rerank-fused/--no-rerank-fused. Without this, a kill-switched run
            # and a caller opt-out are indistinguishable in the trace.
            "rerank_fused_source": "auto" if rerank_fused_auto else "caller",
            "rerank_gate": {"enabled": rerank_gate_enabled(rerank_gate),
                            "skipped": False, "reason": "rerank_fused_off"},
            "pin": {"id": None, "applied": False, "source": "original_query"},
            "per_variant": [],
            "contributions": {},
        }

        def _done(hits: list[Hit]) -> "list[Hit] | tuple[list[Hit], dict[str, Any]]":
            return (hits, trace) if return_trace else hits

        if not variants:
            return _done([])
        if len(variants) == 1:
            return _done(self.hybrid_search(
                variants[0], k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
                rerank_gate=rerank_gate,
            ))
        fused: dict[str, list] = {}  # id -> [fused_score, Hit, best_rank, from_original]
        original_top: Hit | None = None
        for vi, q in enumerate(variants):
            started = time.perf_counter()
            hits = self.hybrid_search(
                q, k=pk, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
                rerank_gate=rerank_gate,
            )
            trace["per_variant"].append({
                "index": vi, "query": q, "returned": len(hits),
                "order": [h.id for h in hits],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            })
            if vi == 0 and hits:
                original_top = hits[0]
            # A variant whose legs returned nothing contributes nothing — the
            # loop below simply never runs for it. No threshold, no score space.
            for rank, h in enumerate(hits, start=1):
                # The OUTER pooling constant. Deliberately `fk`, never `rrf_k`:
                # see the docstring — the inner constant is ADR-0008's pin.
                contrib = 1.0 / (fk + rank)
                trace["contributions"].setdefault(h.id, []).append(
                    {"variant": vi, "rank": rank, "contribution": contrib})
                cur = fused.get(h.id)
                if cur is None:
                    fused[h.id] = [contrib, h, rank, vi == 0]
                else:
                    cur[0] += contrib
                    cur[2] = min(cur[2], rank)
                    cur[3] = cur[3] or vi == 0
        # Stable sort: equal scores keep insertion order (first-variant-first),
        # which is exactly the pre-decoupling behaviour when the guard is off.
        entries = list(fused.values())
        if guard_on:
            before = [e[1].id for e in sorted(entries, key=lambda t: -t[0])]
            ranked = sorted(entries, key=lambda t: (t[2] > MULTI_GUARD_STRONG_RANK, -t[0]))
            after = [e[1].id for e in ranked]
            trace["guard"]["demoted"] = [i for i, j in zip(before, after) if i != j]
        else:
            ranked = sorted(entries, key=lambda t: -t[0])
        # Stamp the fused score so any downstream re-sort preserves fan-out order.
        # ENG-02: cap create-safety for a hit ONLY a generated variant found.
        fused_hits = [
            replace(h, score=s,
                    create_safety=("probable" if not from_original
                                   and h.create_safety == "exists" else h.create_safety))
            for s, h, _best, from_original in ranked
        ]

        # ADR-0008 PIN, pooled (ENG-02). A unique full alias/title owner of the
        # ORIGINAL query is pinned at rank 1 — that guarantee predates fan-out
        # and adding a variant must not be able to demote an exact-identity
        # answer. `create_safety == "exists"` IS that pin (unique owner + full
        # identity evidence), and the cap above already restricted it to variant
        # 0, so a translated title matching an unrelated note can never pin.
        # The pin is expressed in the SCORE as well as the order, so it survives
        # a {path: score} round-trip the same way the rerank re-stamp does.
        pin_id = (original_top.id if original_top is not None
                  and original_top.create_safety == "exists"
                  and original_top.evidence in {"alias_hit", "exact_title_match"}
                  else None)
        trace["pin"]["id"] = pin_id
        if pin_id is not None and fused_hits and fused_hits[0].id != pin_id:
            for pos, hit in enumerate(fused_hits):
                if hit.id == pin_id:
                    top = fused_hits[0].score + 1.0 / (fk + 1)
                    fused_hits.insert(0, replace(fused_hits.pop(pos), score=top))
                    trace["pin"]["applied"] = True
                    break

        # POST-FUSION RERANK (RET-05b) — fan-out maximises deep RECALL (golds the
        # single query missed surface at ranks 11-20), but answer generation reads
        # only the TOP few, where wide recall + RRF + a zone prior inject noise.
        # The cross-encoder reorders the wide fused POOL against the ORIGINAL query
        # (variants[0]) so brain's recall@20 advantage is converted into top-k
        # PRECISION. Without this, fan-out wins recall@20 but loses precision@5 to
        # SC's whole-note embeddings (measured: answer-grounded eval, S10). The
        # rerank is SKIPPABLE (offline/no model -> identity, never an error).
        if rerank_fused and fused_hits:
            # RK-02 one layer up: the gate reads the POOLED pin state, not a
            # per-variant one. `create_safety == "exists"` is exactly ADR-0008's
            # unique-full-owner pin, and the cap above already restricted it to
            # the original query — so a translated title matching an unrelated
            # note can never buy a skip.
            pooled_pin = (fused_hits[0].create_safety == "exists"
                          and fused_hits[0].evidence in {"alias_hit", "exact_title_match"})
            gate_on = trace["rerank_gate"]["enabled"]
            skip = pooled_pin and gate_on
            trace["rerank_gate"] = {
                "enabled": gate_on, "skipped": skip,
                "reason": ("pooled_unique_identity_pin" if skip
                           else "gate_disabled" if not gate_on
                           else "no_pooled_unique_identity_pin"),
            }
            if not skip:
                fused_hits = self.index._apply_rerank(
                    variants[0], fused_hits, None, fused_pool
                )
                # _apply_rerank REORDERS but keeps each hit's (fused) score, so a
                # downstream re-sort by score would undo the rerank. Re-stamp a strictly
                # descending score that encodes the post-rerank RANK, so the cross-encoder
                # order survives any {path: score} round-trip (e.g. the eval harness).
                n = len(fused_hits)
                fused_hits = [replace(h, score=float(n - i))
                              for i, h in enumerate(fused_hits)]
        final = fused_hits[:k]
        keep = {h.id for h in final}
        trace["contributions"] = {i: c for i, c in trace["contributions"].items() if i in keep}
        return _done(final)

    def grep(self, pattern: str, *, k: int = 20, regex: bool = False) -> list[dict[str, Any]]:
        """Lexical-first scan over note bodies — no embedding (RET-04)."""
        return self.index.grep(pattern, k=k, regex=regex)

    def bases_query(
        self, filters: dict[str, str] | None = None, *, k: int = 50,
        latest_only: bool = False, as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Structured frontmatter view over indexed columns — no embedding (RET-04).
        TMP-02: ``latest_only``/``as_of`` are temporal views (Latest Only / As Of)."""
        return self.index.bases_query(filters, k=k, latest_only=latest_only, as_of=as_of)

    def dossier(self, query: str, k: int = 12) -> dict[str, Any]:
        """RET-10: the ONE-CALL retrieval sweep — what a careful agent
        orchestrates by hand (decision layer + corroborating sources +
        contradiction check + version noise handling), composed engine-side
        so even a minimal-path harness gets the full sweep deterministically.

        Motivation (2026-07-11 benchmark series close): on the same
        substrate, the remaining quality gap between harnesses was
        ORCHESTRATION BREADTH — one agent cross-checked newer sources
        against the decision layer and caught superseded thinking; the
        other walked the minimal path and could not see contradictions off
        it. This verb makes the sweep the minimal path.

        Returns (UNFILTERED — callers apply the egress gate):
        - ``decisions``: hits with ``type: decision`` (the authority
          layer), each carrying a ``tensions`` list — NEWER-dated,
          non-decision hits from the same sweep (a proposal/deck that
          post-dates the recorded decision: report the tension, never
          promote the proposal).
        - ``sources``: the remaining live hits (material under
          consideration).
        - ``retired_excluded``: hits dropped because a supersession chain
          retired them (``is_latest_version: false``) — version noise the
          sweep already handled.
        """
        # A DEEP candidate pool: decision notes are scarce and often rank
        # below big source documents on broad queries — the decision layer
        # must never come back empty just because the top-k was crowded
        # (measured on the live corpus: decisions at rank ~30 on a broad
        # decision-state query). Scanning deeper is one indexed query.
        pool = [h.to_dict() for h in self.hybrid_search(query, k=max(k * 2, 60))]
        live = [h for h in pool if h.get("is_latest_version") != "false"]
        retired_excluded = len(pool) - len(live)
        decisions = [h for h in live if h.get("type") == "decision"]
        # RET-10b: MERGE a targeted BM25 probe over the decision layer — the
        # decision layer must never come back empty just because a phrasing
        # shift pushed decision notes below the semantic pool (measured live:
        # a rewording emptied the layer while the notes plainly existed).
        seen_ids = {d["id"] for d in decisions}
        for h in self.index.decision_layer_hits(query, k=max(5, k // 2)):
            hd = h.to_dict()
            if hd["id"] not in seen_ids:
                decisions.append(hd)
                seen_ids.add(hd["id"])
        decisions = decisions[:max(5, k // 2)]
        sources = [h for h in live if h.get("type") != "decision"][:k]
        # Identity-confidence on tension candidates (engine-feedback 2026-07-19):
        # a calendar-asserted transcript whose title the audio doesn't support
        # can post-date a decision and surface as a tension purely on metadata.
        # Carry the source's `identity:` stamp so the caller can discount a
        # title/calendar-derived tension vs a content-verified one. Lazy
        # frontmatter read of the few tension candidates only — no index column.
        _identity_cache: dict[str, str] = {}

        def _identity(s: dict[str, Any]) -> str:
            p = s.get("path", "")
            if p not in _identity_cache:
                try:
                    meta, _ = frontmatter.parse_text(
                        Path(p).read_text(encoding="utf-8", errors="replace"))
                    _identity_cache[p] = str(meta.get("identity", "") or "")
                except OSError:
                    _identity_cache[p] = ""
            return _identity_cache[p]

        for d in decisions:
            d_date = d.get("date") or ""
            d["tensions"] = [
                {"id": s["id"], "date": s.get("date", ""), "type": s.get("type", ""),
                 "identity": _identity(s)}
                for s in sources
                if d_date and s.get("date") and s["date"] > d_date
            ]
        return {
            "query": query,
            "decisions": decisions,
            "sources": sources,
            "retired_excluded": retired_excluded,
        }

    def graph_expand(
        self, seeds: list[str], *, depth: int = 2, k: int = 10, use_ppr: bool = True,
        use_inferred: bool = False,
    ) -> dict[str, Any]:
        """On-demand wikilink-BFS + PPR — DISCOVERY-ONLY (RET-03).

        ``use_inferred`` (GRF-01, ADR-0003 Ruling 6, "Optional"): fold the
        published graphify build's INFERRED edges in as extra traversal
        input. HOST-ONLY read of the graphify artifact — on the VM leg this
        is silently ignored (degrades to the plain wikilink graph) rather
        than reaching for a host-only runtime artifact through the shared
        mount, mirroring the session-memory host-only-by-contract posture
        (ADR-0003 Ruling 4)."""
        extra_edges = None
        if use_inferred and self.role == config.ROLE_HOST:
            from . import graphify as gmod

            extra_edges = gmod.read_published_inferred_edges(
                config.graph_json_path(self.vault))
        return self.index.graph_expand(
            seeds, depth=depth, k=k, use_ppr=use_ppr, extra_edges=extra_edges)

    def get(self, note_id: str) -> dict[str, Any] | None:
        return self.index.get(note_id)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.index.recent(limit)

    # -- VM-side capture (read + DRAFT only; NO sign, NO index, NO WAL) ---
    def capture_inbox_dir(self) -> Path:
        return config.capture_inbox_dir(self.vault)

    def draft_capture(
        self, content: str, *, ident: str | None = None, is_source: bool = False
    ) -> dict[str, Any]:
        """Stage a candidate note as a plain DRAFT — the ONE write a VM leg may do.

        This is the VM-side capture verb (AGENTS.md §5/§6). It writes a plain
        Markdown file into the writable ``capture-inbox/`` on the shared mount and
        stamps ``status: draft`` + ``provenance.trust: untrusted``. It NEVER:
        signs the audit chain, opens the index, writes WAL, or resolves a signing
        key. The draft is NOT authoritative and is NOT surfaced by ``search``
        until the HOST drains it (drain-on-invoke -> sign + index + snapshot).

        Available on BOTH legs (host + VM) — it is the only quasi-write a VM holds.
        """
        meta, _body = frontmatter.parse_text(content)
        note_id = ident or (str(meta.get("id")) if meta and meta.get("id") else None)
        if not note_id:
            # deterministic fallback id from content hash
            note_id = "draft-" + sha256_text(content)[:12]
        # C-1 trust boundary: the id comes from --id or untrusted YAML and
        # becomes a path — refuse anything but a bare slug (fail closed).
        note_id = safe_slug(note_id)
        staged = _stamp_draft_frontmatter(content, note_id, is_source)
        inbox = self.capture_inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / f"{note_id}.md"
        # Belt over the slug check: the resolved target (symlinks followed)
        # must stay inside the inbox.
        if not _contained_in(target, inbox):
            raise ValueError(f"draft target escapes capture inbox: {note_id!r}")
        target.write_text(staged, encoding="utf-8")
        return {
            "draft": str(target),
            "id": note_id,
            "signed": False,
            "indexed": False,
            "authoritative": False,
            "note": "draft staged; host drain-on-invoke will sign + index + snapshot",
        }

    # -- maintenance (HOST-broker only) ----------------------------------
    def rebuild(self, *, json_mode: bool = False) -> dict[str, Any]:
        self._require_host("rebuild the index")
        with vault_writer_lock(self.vault, verb="rebuild"):
            res = self.index.rebuild(self.vault, json_mode=json_mode)
        # INT-01 durability: the index dir is a disposable cache EXCEPT for the
        # approved queue, which is the only copy of owner-accepted content until
        # it is signed. Rebuild guidance ("just delete it and rebuild") is
        # exactly the habit that would destroy it, so never let it be silent.
        try:
            from . import cos as _cos_q

            waiting = len(_cos_q.approved_pending(self.vault))
            # INT-04: the SECOND non-disposable item in this dir. An armed
            # acceptance anchor is the only thing holding its inbox file at the
            # email-derived MNPI floor; lose it and the file ingests at
            # `Internal`, silently. Same warning surface, same reason.
            anchors = _cos_q.attachment_anchors_awaiting_drain(self.vault)
        except Exception:  # noqa: BLE001 — never fail a rebuild on the check
            waiting = anchors = 0
        if waiting or anchors:
            # A `progress_note` here was a TTY-gated whisper: a headless
            # launchd rebuild — the exact context that would then delete the
            # dir — saw nothing at all. Put it in the RESULT, where every
            # caller (JSON, human, scheduled) actually reads it.
            parts = []
            if waiting:
                res["approved_awaiting_signature"] = waiting
                parts.append(f"{waiting} owner-approved item(s) wait in "
                             f"{_cos_q.approved_queue_dir(self.vault)}")
            if anchors:
                res["attachment_anchors_awaiting_drain"] = anchors
                parts.append(f"{anchors} accepted-attachment anchor(s) wait in "
                             f"{_cos_q.attachment_anchor_dir(self.vault)}")
            res["warning"] = (
                " and ".join(parts) + " — NOT rebuildable from vault/. Run "
                "`brain sync` to drain them before deleting or repointing the "
                "index dir.")
            from .progress import progress_note

            progress_note("WARNING: " + res["warning"], json_mode=json_mode,
                          verb="rebuild")
        return res

    def embedder_pending(self) -> bool:
        """True when the index's stored dense vectors were built with a
        DIFFERENT embedder than the one the live runtime would use now (S02/
        CS-01) — e.g. a cold-start install built the index with the offline
        ``hash`` placeholder to avoid a network model download. Read-only,
        cheap (no download): :meth:`BrainIndex.model_matches` only compares
        recorded meta strings against the constructed (not yet loaded)
        embedder's ``model_id``/``dim``."""
        return not self.index.model_matches()

    def warmup(self, *, json_mode: bool = False) -> dict[str, Any]:
        """HOST-ONLY (S02/CS-01): resolve + download the live auto-embedder's
        model weights now, instead of on the first real semantic search.

        huggingface_hub prints its own progress bar to stderr during the
        download (never stdout — keeps ``--json`` output parseable) and
        already file-locks the blob it is writing
        (``huggingface_hub.file_download.WeakFileLock``), so a concurrent
        warmup / first-search / nightly-maintenance embed racing on the same
        cache directory cannot corrupt it — see the closeout note; no extra
        locking is added here.

        Does NOT rebuild the index. If the index was built with a placeholder
        embedder (``embedder_pending()`` was True), run `brain sync` (or
        `brain rebuild`) afterward — `BrainIndex.sync`'s existing model-
        mismatch guard will do a full, now-offline (model already cached)
        re-embed automatically."""
        self._require_host("warm up the embedding model (download)")
        import os
        import time

        from .embed import get_embedder, model_cache_ready
        from .progress import progress_note

        embedder = get_embedder(os.environ.get("BRAIN_EMBEDDER", "auto"))
        was_cached = model_cache_ready(embedder)
        # OB-02: begin/end lines only -- hf_hub prints its own download bar to
        # stderr during the load below (core.py, embed.py), so we narrate
        # start/finish around it rather than duplicating its per-file progress.
        progress_note(f"warmup: resolving {embedder.model_id}"
                       f"{' (cached)' if was_cached else ' (downloading)'}...",
                       json_mode=json_mode, verb="warmup")
        t0 = time.monotonic()
        embedder.embed("warmup")  # triggers the real load/download if needed
        elapsed = time.monotonic() - t0
        # The RERANKER too, since 0.20.1: reranking is default-on (BR-03), and
        # a search is no longer allowed to download its weights mid-query — it
        # degrades to the unreranked order instead. Warmup is now the one place
        # those weights are fetched, so warming only the embedder would leave a
        # user permanently unreranked without ever saying why.
        from .rerank import warm_reranker_weights

        rerank_info: dict[str, Any]
        try:
            progress_note("warmup: resolving the reranker...",
                          json_mode=json_mode, verb="warmup")
            rerank_info = dict(warm_reranker_weights())
        except Exception as exc:
            # Never fail the embedder warm because the optional precision
            # booster could not be fetched — report it and move on.
            rerank_info = {"downloaded": False, "cached": False,
                           "error": f"{type(exc).__name__}: {exc}"}
        progress_note(f"warmup: ready in {elapsed:.1f}s", json_mode=json_mode, verb="warmup")
        return {
            "model_id": embedder.model_id,
            "already_cached": bool(was_cached),
            "elapsed_s": round(elapsed, 2),
            "reranker": rerank_info,
        }

    def drafts_dir(self) -> Path:
        return self.vault / ".brain" / "drafts"

    def _draft_sources(self) -> list[Path]:
        """Both draft drop locations, drained on the host: the legacy
        ``.brain/drafts/`` and the VM-facing ``capture-inbox/``."""
        dirs = [self.drafts_dir(), self.capture_inbox_dir()]
        seen: set[str] = set()
        out: list[Path] = []
        for d in dirs:
            key = str(d.resolve()) if d.exists() else str(d)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out

    def _drain_sources(self) -> tuple[list[tuple[Path, bool, Any]],
                                      list[dict[str, str]]]:
        """``[(dir, is_approved_queue, pubkey)]`` in drain order, plus refusals.

        The broker's HOST-ONLY approved queue is drained FIRST, deliberately: a
        VM draft-capture under the same id would otherwise be signed ahead of
        the bytes the owner approved, and the owner's copy would then lose to
        the duplicate-id guard.

        The verification key is resolved ONCE, here. If it cannot be resolved —
        locked keychain, scheduler running as another user, missing
        ``cryptography``, a rotated key — the queue is not drained AT ALL and
        says ``no-signing-key (fail-closed)``, exactly like the ordinary draft
        path. Verifying per item instead would turn every key outage into a
        pile of security-worded refusals over perfectly good owner-approved
        work."""
        from . import cos as _cos_mod

        out: list[tuple[Path, bool, Any]] = []
        refusals: list[dict[str, str]] = []
        try:
            queue = _cos_mod.approved_queue_root(self.vault)
            out.append((queue, True, _cos_mod.approved_verify_key(self.vault)))
        except _cos_mod.ApprovedQueueUnsafe as exc:
            refusals.append({"draft": "(approved queue)", "source": "approved-queue",
                             "reason": f"not drained (fail-closed): {exc}"})
        except _cos_mod.ApprovedKeyUnavailable as exc:
            refusals.append({"draft": "(approved queue)", "source": "approved-queue",
                             "reason": f"no-signing-key (fail-closed): the approved "
                                       f"queue was left untouched ({exc})"})
        out += [(d, False, None) for d in self._draft_sources()]
        return out, refusals

    def drain_drafts(self) -> dict[str, Any]:
        """drain-on-invoke (HOST only): promote pending capture drafts.

        The incremental indexer IS the capture drain. The host picks up each
        item in the broker's host-only approved queue (INT-01), then each draft
        in ``.brain/drafts/`` AND ``capture-inbox/`` (the VM-facing drop), signs
        + writes it into ``raw/`` (if a source) or ``brain/resources/`` (if
        a note) via the audited host-broker ``write_note``, then removes the
        draft. Idempotent and cheap: empty drop dirs are a no-op. Fails CLOSED —
        if no signing key resolves, drafts are LEFT in place (never promoted
        unsigned) and reported as skipped.

        An APPROVED-QUEUE item is additionally bound to the bytes the owner
        accepted: its payload is read once (no-follow), hashed, and checked
        against an Ed25519-signed anchor the VM cannot reach or forge — twice,
        the second time immediately before ``write_note``, because a
        consume-time check alone is TOCTOU. The buffer that was verified is the
        buffer that gets signed; the path is never re-opened.

        This is NOT a dedicated scheduled task and NOT a daemon: it runs as the
        first step of any host ``sync`` invocation. There is no capture daemon
        and no dedicated drain task — the ONE sanctioned scheduled task is the
        ux-02 brief/digest, which doubles as the guaranteed daily drain floor.
        """
        self._require_host("drain capture drafts (sign + index)")
        from . import cos as _cos_mod

        promoted: list[str] = []
        sources, skipped = self._drain_sources()
        any_dir = False
        for ddir, approved, pubkey in sources:
            if not ddir.is_dir():
                continue
            any_dir = True
            # Every skip carries WHERE it came from, so the maintain report can
            # attribute it by source instead of pattern-matching its prose.
            src_name = "approved-queue" if approved else "capture-inbox"

            def _skip(draft: Path, reason: str, _src: str = src_name) -> None:
                skipped.append({"draft": draft.name, "source": _src,
                                "reason": reason})

            for draft in sorted(ddir.glob("*.md")):
                approved_sha: str | None = None
                if approved:
                    try:
                        content, approved_sha = _cos_mod.read_approved(
                            self.vault, draft, pubkey=pubkey)
                    except _cos_mod.ApprovedRefused as exc:
                        _cos_mod.refuse_approved(self.vault, draft, str(exc))
                        _skip(draft, f"approved-queue refusal (NOT signed): {exc}")
                        continue
                    except _cos_mod.ApprovedKeyUnavailable as exc:
                        # Never quarantine on a key problem — leave it parked.
                        _skip(draft, f"no-signing-key (fail-closed): {exc}")
                        continue
                else:
                    try:
                        content = draft.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as exc:
                        _skip(draft, f"unreadable: {type(exc).__name__}")
                        continue
                note = note_from_text(content, draft, self.vault)
                if note is None:
                    _skip(draft, "no-frontmatter")
                    continue
                # C-2 trust boundary: note.id comes from attacker-controlled
                # draft frontmatter — refuse non-slug ids (fail closed, draft
                # left in place, never signed).
                try:
                    nid = safe_slug(note.id)
                except ValueError as exc:
                    _skip(draft, f"unsafe-id (fail-closed): {exc}")
                    continue
                if approved and nid != draft.stem:
                    # The anchor is keyed on the FILENAME id; a payload whose
                    # frontmatter claims a different id would be signed under an
                    # id nothing approved.
                    _cos_mod.refuse_approved(
                        self.vault, draft,
                        f"frontmatter id {nid!r} != approved id {draft.stem!r}")
                    _skip(draft, f"approved-queue refusal (NOT signed): "
                                 f"id mismatch {nid!r} != {draft.stem!r}")
                    continue
                # COS owner-gate integrity: never sign a draft whose id is still
                # awaiting the owner's accept/reject in the proposal pipeline.
                # Approved-queue items are EXEMPT: they are the gated route's own
                # output. A crash between staging and clearing pending/ leaves the
                # id looking "undecided", and quarantining the owner's approved
                # payload for it would discard the very decision it carries.
                # The gated copy is authoritative; only the owner's answer may
                # promote it (consume_answers moves it here itself on accept).
                # Quarantined rather than skipped-in-place — see
                # cos.quarantine_gate_bypass for why leaving it would let a
                # later REJECT still leak the content on the next drain.
                try:
                    from . import cos as _cos_mod
                    if not approved and nid in _cos_mod.undecided_proposal_ids(self.vault):
                        dest = _cos_mod.quarantine_gate_bypass(self.vault, draft)
                        _skip(draft, f"gate-bypass: {nid!r} is awaiting the owner's "
                                     f"accept/reject — quarantined to "
                                     f"{dest.parent.name}/{dest.name}")
                        continue
                except Exception as exc:  # noqa: BLE001 — never break the drain floor
                    _skip(draft, f"gate-check-failed (fail-closed, left in place): "
                                 f"{type(exc).__name__}: {exc}")
                    continue
                # raw source -> raw/<id>.md ; otherwise a brain note -> resources/.
                if note.type == "source" or note.zone == "raw":
                    rel, subtree = f"raw/{nid}.md", "raw"
                else:
                    rel, subtree = f"brain/resources/{nid}.md", "brain/resources"
                # M-3: a duplicate id would write a second file sharing the
                # same frontmatter `id`, which crashes the next sync on the
                # UNIQUE index constraint (H-2). Check both the live index and
                # the target path before promoting; report as skipped rather
                # than write the collision.
                dest_path = self.vault / rel
                try:
                    already_indexed = self.index.get(nid) is not None
                except Exception:
                    already_indexed = False
                if already_indexed or dest_path.exists():
                    _skip(draft, f"duplicate-id: {nid!r} already exists")
                    continue
                # NOTE (INT-01): `content` was read ONCE above — for an approved
                # item, the exact buffer whose hash the signed anchor covers.
                # The path is never re-opened here; re-opening after verifying
                # would reintroduce the substitution window inside the critical
                # section. Everything below transforms that buffer in memory.
                #
                # codex 2026-07-22: the capture-inbox IS the trust boundary — a
                # draft is untrusted by POSITION, not by what its frontmatter
                # claims. A file written straight into the inbox (bypassing
                # `draft-capture`'s additive stamp) could omit or forge the
                # stamp, so the drain overwrites it unconditionally.
                if frontmatter.split(content) is not None:
                    # Same reasoning one step further (2026-07-30 review): a
                    # HOST-ONLY key is a host assertion, so bytes sitting in
                    # the VM-writable inbox can never carry one either — an
                    # accepted proposal waits here, and `draft-capture` can
                    # overwrite it under the same id before this runs. Strip
                    # unconditionally, and fail closed if a construct resolves
                    # the key anyway rather than signing a forged assertion.
                    try:
                        content = provenance.without_host_only_text(content)
                    except provenance.HostOnlyKeyResidue as exc:
                        _skip(draft, f"host-only provenance forgery: {exc}")
                        continue
                    content = frontmatter.set_keys(
                        content, {"provenance.trust": "untrusted"})
                # INT-01: the two transforms above are SECURITY ones — they
                # remove host assertions untrusted bytes may not carry. Autolink
                # is a convenience one, and it EDITS THE BODY using vault state,
                # so it is skipped for an approved item: the body the owner
                # approved is the body that gets signed, verbatim. (No fold
                # backfills those links today — `graph_hygiene` counts links, it
                # does not create them — and that is the deliberate trade: an
                # approval cannot be re-obtained after the fact, a wikilink can
                # be added by hand or by a later fold.)
                split = None if approved else frontmatter.split(content)
                if split is not None:
                    from . import autolink as _autolink

                    fm_block, body = split
                    linked_body, _added = _autolink.apply_autolinks(
                        body, title=note.title, origin=str(note.meta.get("origin", "")),
                        vault=self.vault,
                    )
                    if linked_body != body:
                        content = f"---{fm_block}---{linked_body}"
                if approved and not _cos_mod.anchor_still_binds(
                        self.vault, nid, approved_sha or "", pubkey=pubkey):
                    # TOCTOU close: re-verified INSIDE the signing critical
                    # section, immediately before the signature, against the
                    # buffer actually in hand.
                    _cos_mod.refuse_approved(
                        self.vault, draft,
                        "the signed approval anchor no longer binds these bytes "
                        "(changed during the drain)")
                    _skip(draft, "approved-queue refusal (NOT signed): "
                                 "anchor changed during the drain")
                    continue
                try:
                    self.write_note(rel, content,
                                    reason=f"drain-on-invoke promote {draft.name}",
                                    subtree=subtree)
                except KeyUnavailable:
                    _skip(draft, "no-signing-key (fail-closed)")
                    continue
                except ValueError as exc:
                    _skip(draft, f"unsafe-path (fail-closed): {exc}")
                    continue
                if approved:
                    if not _cos_mod.clear_approved(self.vault, nid):
                        # Signed, but still queued: the next drain would refuse
                        # it as a duplicate id forever. Say so now.
                        _skip(draft, f"signed as {rel}, but the queue copy could "
                                     f"NOT be removed — delete it by hand")
                else:
                    draft.unlink()
                promoted.append(rel)
        out = {
            "promoted": len(promoted),
            "skipped": len(skipped),
            "details": {"promoted": promoted, "skipped": skipped},
        }
        if not any_dir:
            # Same SHAPE either way: this used to return `details: []`, which
            # silently dropped refusals recorded before any drop dir existed —
            # including "the approved queue was skipped, no signing key".
            out["reason"] = "no-drafts-dir"
        return out

    def ingest_dropzone(self, *, dry_run: bool = False) -> dict[str, Any]:
        """HOST-only: drain ``<vault>/inbox/`` (ADR-0003 Ruling 1 / ING-01).

        Refused on the VM leg BEFORE any filesystem side effect (no key
        lookup, no processing-dir claim, no archive/WAL write) — the same
        fail-closed shape as ``drain_drafts``/``write_note``. Idempotent and
        cheap when the inbox is empty or absent (a directory listing)."""
        self._require_host("ingest the drop zone")
        from .ingest.pipeline import run_ingest

        return run_ingest(self, dry_run=dry_run)

    def ingest_transcript(
        self, path: str | Path, *, origin: str, language: str | None = None,
        document_date: str | None = None, classification: str = "Internal",
    ) -> dict[str, Any]:
        """HOST-only: promote one transcript ``.md`` file into ``vault/raw/``
        with explicit provenance (ADR-0003 Ruling 1 companion / ING-04).

        ``origin`` is the source audio/video file path, or the literal
        string ``"verbal"`` — the generic drop-zone (``ingest_dropzone``)
        cannot express this fact on its own (its own ``origin`` always points
        at an archived COPY of the dropped file). Refused on the VM leg
        BEFORE any filesystem side effect, same fail-closed shape as
        ``ingest_dropzone``/``write_note``."""
        self._require_host("ingest a transcript")
        from .ingest.transcript import ingest_transcript as _ingest_transcript

        return _ingest_transcript(
            self, path, origin=origin, language=language,
            document_date=document_date, classification=classification,
        )

    def sync(self, *, drain: bool = True, publish: bool = False,
             json_mode: bool = False) -> dict[str, Any]:
        """Incremental index reconcile (IDX-03), draining capture drafts AND
        the ingestion drop zone first.

        HOST-broker only (it mutates the index). ``drain`` runs the host capture
        drain + inbox ingest drain before reconciling (ADR-0003 Ruling 1
        amendment: the ingest drain fires on every host ``sync``, not only the
        nightly `maintain` floor); ``publish`` additionally republishes the
        read-only snapshot so a VM session's next read sees the just-committed
        note (closing the capture loop). Set ``drain=False`` only for a host
        read-only reconcile."""
        self._require_host("sync (mutate) the index")
        with vault_writer_lock(self.vault, verb="sync"):
            drain_res = self.drain_drafts() if drain else {"promoted": 0, "skipped": 0, "drain": "off"}
            if drain:
                try:
                    ingest_res = self.ingest_dropzone()
                except Exception as exc:
                    # C2: run_ingest's own per-file retry/quarantine machinery
                    # isolates a single poison file WITHOUT raising, but this is
                    # the last-resort backstop for anything that still escapes it
                    # (e.g. a manifest/failures-file I/O error). ingest_dropzone
                    # ran BEFORE index.sync with no try/except, so any escaping
                    # exception aborted index reconciliation and snapshot
                    # publication on every subsequent sync — one bad drop must
                    # never abort index maintenance.
                    ingest_res = {"processed": [], "error": f"{type(exc).__name__}: {exc}"}
            else:
                ingest_res = {"processed": [], "reason": "drain-off"}
            idx_res = self.index.sync(self.vault, json_mode=json_mode)
            idx_res["drain"] = drain_res
            idx_res["ingest"] = ingest_res
            if publish:
                idx_res["snapshot"] = self.publish_snapshot()
            return idx_res

    def publish_snapshot(self, dest: str | Path | None = None) -> dict[str, Any]:
        """Publish a read-only, generation-stamped snapshot of the authoritative
        host index (atomic). The VM mounts this read-only; it never writes the
        authoritative DB. HOST-broker only."""
        self._require_host("publish a snapshot")
        from .snapshot import publish_snapshot as _publish

        dest_dir = Path(dest) if dest else config.snapshot_dir(self.vault)
        with vault_writer_lock(self.vault, verb="snapshot"):
            return _publish(self.index.db_path, dest_dir).to_dict()

    def restore_index_from_snapshot(
        self, *, force: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Fast index recovery: replace the live index with the published snapshot.

        The snapshot is a complete, read-consistent copy of the authoritative
        index, so restoring from it is O(seconds) — the safe alternative to a
        full re-embed ``rebuild`` when the live index is corrupt or empty (e.g.
        an interrupted rebuild left a half-written DB). HOST-broker only.

        Guards: refuses a missing/empty/unreadable snapshot; refuses to clobber a
        live index that holds MORE notes than the snapshot (the snapshot is
        older — ``sync``/``rebuild`` instead) unless ``force``; backs up the
        current index (reversible ``.pre-restore-*.bak``) before overwriting; and
        verifies the note count post-restore.
        """
        self._require_host("restore the index from a snapshot")
        with vault_writer_lock(self.vault, verb="restore-index"):
            return self._restore_index_from_snapshot_locked(force=force, dry_run=dry_run)

    def _restore_index_from_snapshot_locked(
        self, *, force: bool, dry_run: bool
    ) -> dict[str, Any]:
        import datetime as _dt
        import shutil as _sh
        import sqlite3 as _sq

        idx = config.index_path(self.vault)
        snap = config.snapshot_db_path(self.vault)

        def _count(p: Path):
            if not p.exists():
                return None  # absent
            try:
                c = _sq.connect(f"file:{p}?mode=ro", uri=True)
                try:
                    return int(c.execute("SELECT count(*) FROM notes").fetchone()[0])
                finally:
                    c.close()
            except Exception:
                return -1  # present but unreadable/corrupt

        snap_n = _count(snap)
        if snap_n is None:
            raise FileNotFoundError(f"no snapshot to restore from: {snap}")
        if snap_n <= 0:
            raise ValueError(
                f"snapshot has {snap_n} notes — refusing to restore an empty/corrupt "
                f"snapshot ({snap})")
        live_n = _count(idx)

        if live_n is not None and live_n > snap_n and not force:
            raise ValueError(
                f"live index has {live_n} notes but the snapshot has only {snap_n} — "
                f"restoring would LOSE {live_n - snap_n} note(s). The snapshot is older; "
                f"run `brain sync`/`rebuild` instead, or pass --force to override.")

        plan: dict[str, Any] = {
            "index": str(idx), "snapshot": str(snap),
            "snapshot_notes": snap_n, "live_notes_before": live_n,
        }
        if dry_run:
            plan["dry_run"] = True
            return plan

        config.ensure_index_dir(self.vault)
        backup = None
        if idx.exists():
            stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            backup = idx.with_name(idx.name + f".pre-restore-{stamp}.bak")
            _sh.move(str(idx), str(backup))
        for suf in ("-wal", "-shm"):  # stale sqlite sidecars would mask the copy
            side = idx.with_name(idx.name + suf)
            if side.exists():
                side.unlink()
        _sh.copy2(str(snap), str(idx))

        live_after = _count(idx)
        if live_after != snap_n:
            raise RuntimeError(
                f"post-restore verification failed: index has {live_after} notes, "
                f"expected {snap_n} (backup preserved at {backup})")
        plan.update({"restored": True, "live_notes_after": live_after,
                     "backup": str(backup) if backup else None})
        return plan

    def status(self, snapshot_dest: str | Path | None = None, today: Any = None) -> dict[str, Any]:
        """Report index stats + snapshot generation/age (available on BOTH legs —
        the VM uses it to tell whether its read-only view is fresh or stale, and
        how many drafts are pending)."""
        from . import __version__
        from .index import SCHEMA_VERSION
        from .snapshot import snapshot_status

        dest_dir = Path(snapshot_dest) if snapshot_dest else config.snapshot_dir(self.vault)
        out: dict[str, Any] = {"vault": str(self.vault), "role": self.role}
        try:
            out["index"] = self.index.stats()
        except Exception as exc:  # index/snapshot not built yet
            out["index"] = {"error": f"{type(exc).__name__}: {exc}"}
        # ADR-0004 Ruling 2/8: surface the version everywhere a skew could
        # implicate a failure. `index_newer_than_binary` flags the direction
        # `sync()` cannot silently absorb — an on-disk schema_version GREATER
        # than this binary's SCHEMA_VERSION means an older `brain` met newer
        # state and must not rebuild it downward.
        stored_schema = out["index"].get("schema_version") if isinstance(out.get("index"), dict) else None
        index_newer = False
        if stored_schema is not None:
            try:
                index_newer = int(stored_schema) > SCHEMA_VERSION
            except (TypeError, ValueError):
                index_newer = False
        out["version"] = {
            "package_version": __version__,
            "index_schema_version": stored_schema,
            "binary_schema_version": SCHEMA_VERSION,
            "index_newer_than_binary": index_newer,
        }
        # LIVE embedder surfacing (S11). ``index.embed_model`` above is INDEX
        # METADATA — the model the index was BUILT with; it does NOT prove which
        # embedder would answer a query right now. On a partial install
        # (onnxruntime missing) get_embedder() degrades to HashEmbedder while the
        # metadata still names the prior semantic model. Surface the model_id
        # of the embedder
        # actually constructed, and flag a mismatch loudly so a silent semantic
        # downgrade is visible in `brain status`/`brain health`.
        try:
            live_id = self.index.embedder.model_id
            recorded = out.get("index", {}).get("embed_model")
            matches = recorded is None or recorded == live_id
            out["live_embedder"] = {
                "model_id": live_id,
                "is_hash_fallback": live_id == "hash-v1",
                "matches_index_metadata": matches,
            }
            # `embedder: ready|pending` (S02/CS-01) — the cold-start-friendly
            # summary a human/agent actually wants from `brain status`: is
            # semantic search fully live right now, or is there a deferred
            # download/re-embed step still owed? A deliberate explicit-hash
            # choice ($BRAIN_EMBEDDER=hash) is "ready" (nothing IS pending —
            # same "deliberate, not a fault" posture as `brain doctor`);
            # otherwise pending means either the model isn't cached yet
            # (`brain warmup` needed) or the index still carries placeholder
            # vectors from a cold-start install (`brain sync` needed after).
            import os

            from .embed import ONNX_MODEL_SIZE_HINT, model_cache_ready

            explicit_hash = os.environ.get("BRAIN_EMBEDDER", "").strip().lower() == "hash"
            cached = model_cache_ready(self.index.embedder)
            pending = (not explicit_hash) and (not matches or cached is False)
            out["embedder"] = {
                "state": "pending" if pending else "ready",
                "model_id": live_id,
                "cached": cached,
                "index_matches": matches,
            }
            if pending and cached is False:
                out["embedder"]["download_size_hint"] = ONNX_MODEL_SIZE_HINT
        except Exception as exc:
            out["live_embedder"] = {"error": f"{type(exc).__name__}: {exc}"}
            out["embedder"] = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
        out["snapshot"] = snapshot_status(dest_dir)
        # Mirror of the index check above (Ruling 2's directional fail-fast):
        # a snapshot schema_version GREATER than this binary's SCHEMA_VERSION
        # means an old CLI is reading state a newer engine produced — the CLI
        # command layer (not this report) is what must refuse; this just makes
        # the condition visible before it bites (Ruling 8).
        snap_schema = out["snapshot"].get("schema_version")
        snapshot_newer = False
        if snap_schema is not None:
            try:
                snapshot_newer = int(snap_schema) > SCHEMA_VERSION
            except (TypeError, ValueError):
                snapshot_newer = False
        out["version"]["snapshot_schema_version"] = snap_schema
        out["version"]["snapshot_newer_than_binary"] = snapshot_newer
        out["pending_drafts"] = self._count_pending_drafts()
        # ADR-0008 S04: the host-only raw-query ledger has its own containment
        # and owner-only permissions.  The VM branch in querylog.status()
        # returns before resolving or reading that host path, so status remains
        # a safe VM read surface while still telling the host whether capture is
        # alive, stale, or failing.
        try:
            from . import querylog

            out["query_capture"] = querylog.status(self.vault, role=self.role)
        except Exception as exc:  # noqa: BLE001 — status must never crash on observability
            out["query_capture"] = {"enabled": False, "state": "error",
                                    "reason": f"{type(exc).__name__}"}
        # ADR-0003 Ruling 5/d + HARDENED:premortem — surface `brain maintain`'s
        # own heartbeat (a stale `daily` branch or a repeatedly-failing branch)
        # so a broken nightly is visible here too, not only via the
        # session-start hook's stale-nightly line.
        out["maintain_heartbeat"] = self._maintain_heartbeat_summary(today=today)
        out["graph"] = self._graph_status()
        # CUT-01E: surface the canonical COS ops dir + queue counts. The VM
        # view only reads the zones it may touch (drop/ + shared/).
        try:
            from . import cos as cos_mod

            out["cos"] = cos_mod.status_block(self.vault, self.role)
        except Exception as exc:  # noqa: BLE001 — status must never crash on cos
            out["cos"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    def _count_pending_drafts(self) -> int:
        """Everything waiting for the drain — the stalled-drain tripwire reads
        this. It must include the approved queue (INT-01): owner-approved,
        unsigned content is precisely what a stalled drain must not hide."""
        n = 0
        for ddir in self._draft_sources():
            if ddir.is_dir():
                n += len(list(ddir.glob("*.md")))
        if self.role == config.ROLE_HOST:
            from . import cos as _cos_q

            n += len(_cos_q.approved_pending(self.vault))
        return n

    # -- write verb (HOST-BROKER ONLY; audited; fails closed) ------------
    def write_note(
        self, rel_path: str, content: str, reason: str = "", *,
        subtree: str | None = None,
    ) -> dict[str, Any]:
        """Write a note to the vault and append a signed audit-chain entry.

        Fails closed in BOTH directions:
        - if no signing key resolves (KeyUnavailable), nothing is written;
        - the chain records the write ATTEMPT first, then the OUTCOME. If the
          file write raises after signing (disk full, permission), a compensating
          ``write_failed`` entry is appended so the chain never claims a write
          that didn't land (F-06). The original exception is re-raised.

        Containment (C-2): the RESOLVED target (symlinks followed) must stay
        inside the vault, and — when ``subtree`` is given (e.g. ``"raw"`` or
        ``"brain/resources"`` on the drain/capture paths) — inside that
        SPECIFIC subtree, so a traversal-laden rel_path can never earn an
        Ed25519 signature over an overwrite elsewhere. Refused BEFORE signing.

        HOST-broker only: refused on the VM leg BEFORE any signing-key
        resolution (the VM never holds the audit key).

        DURABLE (ENF-01, adversarial review round 3, 2026-08-10). The file
        write used to be a plain ``Path.write_text``: not atomic, and nothing
        fsynced. ``supersede``/``unsupersede`` then unlinked their crash
        journal — with a *directory* fsync — the moment both calls returned, so
        a power loss could persist the journal's deletion while losing or
        tearing a note write, leaving a signed one-sided chain with no
        recovery record. ``_write_note_durable`` fsyncs the content and the
        parent directory entry before returning, so "``write_note`` returned"
        now means "these bytes survive a power loss" — which is the only thing
        that makes clearing the journal afterwards safe.
        """
        self._require_host("write notes (sign + commit)")
        target = self.vault / rel_path
        if not _contained_in(target, self.vault):
            raise ValueError(f"write target escapes vault: {rel_path}")
        if subtree is not None and not _contained_in(target, self.vault / subtree):
            raise ValueError(f"write target escapes {subtree!r} subtree: {rel_path}")
        target = target.resolve()
        # Append the signed audit entry FIRST; if signing fails, nothing is written.
        content_sha = sha256_text(content)
        try:
            entry = self.audit.append(
                verb="write", path=rel_path,
                reason=reason or f"write_note {rel_path}",
                content_sha256=content_sha,
            )
        except KeyUnavailable:
            raise  # fail closed — no unsigned writes
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_note_durable(target, content)
        except Exception as exc:
            # The signed "write" entry is already in the chain; record the failure
            # so verify-audit shows the attempt did not complete.
            try:
                self.audit.append(
                    verb="write_failed", path=rel_path,
                    reason=f"file write failed after signing: {type(exc).__name__}: {exc}",
                )
            except KeyUnavailable:
                pass  # key vanished mid-op; the original error is what matters
            raise
        return {"written": str(target), "audit": entry}

    # -- supersession (TMP-02, ADR-0003 Ruling 2/8) — HOST-broker only ----
    #: Journal schema version. Bumped when the REQUIRED key set changes, so a
    #: journal written by a future/unknown build is refused rather than
    #: half-understood. v1 is the first format carrying a checksum.
    _SUPERSEDE_JOURNAL_V = 1

    def _supersede_journal_path(self) -> Path:
        """Host-private (ENF-01 round 3) — see ``config.supersede_journal_path``."""
        return config.supersede_journal_path(self.vault)

    @staticmethod
    def _supersede_journal_checksum(journal: dict[str, Any]) -> str:
        """sha256 over the journal's payload with ``checksum`` itself excluded.

        Canonical (``sort_keys``, no spaces) so the digest depends on the
        VALUES, not on dict ordering. This detects a torn or corrupted journal;
        it is not a tamper control — the journal lives off the mount now, and
        being unreachable is what makes it untamperable."""
        import hashlib
        import json

        payload = {k: v for k, v in journal.items() if k != "checksum"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")).hexdigest()

    def _write_supersede_journal(self, journal: dict[str, Any]) -> None:
        """Write the rollback record ONCE, atomically and durably, BEFORE
        either note is touched.

        It used to be written with ``Path.write_text`` and then REWRITTEN after
        the first signed write to advance a ``stage`` field. Both halves were
        wrong. ``write_text`` truncates in place, so a crash during the rewrite
        leaves a truncated file where the only copy of both pre-transaction
        images lived — after one note had already changed. And nothing fsynced,
        so a journal that reached only the page cache is a journal a power loss
        never had (adversarial review round 2, 2026-08-10: a probe with a
        one-sided chain and a truncated journal returned
        ``{'recovered': False, 'reason': 'unreadable journal, discarded'}``
        while the half-chain stayed on disk).

        The stage field is gone with the rewrite: the journal is unlinked only
        after the LAST write returns, so its mere existence already means
        "incomplete", and recovery restores whichever side actually moved. One
        write, no second chance to corrupt it.

        ``_write_atomic_durable`` (module level) does the O_EXCL + O_NOFOLLOW
        staging, short-write loop, fsync, replace and STRICT parent fsync. It
        used to be ``cos._write_atomic``; round 3 moved every note-and-journal
        write off that symbol because a COS test double for it was intercepting
        unrelated vault writes — see ``_write_atomic_durable``.

        It carries a version stamp and a checksum (round 3): recovery REWRITES
        both notes from what it reads here, so "readable JSON" is not a high
        enough bar to act on — see ``_recover_pending_supersede``.

        0o600: this file holds both notes' complete text, at whatever tier they
        carry. It is owner-only, in an owner-only directory, off the mount.

        Durability is CHECKED, not assumed (round 4). Where an atomic replace
        cannot be proven to reach the disk, ``_require_durable_replace``
        refuses the whole supersession by name before anything is signed, and
        ``_mkdir_durable`` anchors a freshly created journal directory in its
        own parent — otherwise the first transaction after creating the store
        writes a durable file into a directory entry that isn't."""
        import json

        _require_durable_replace(f"journal a {journal.get('op')}")
        record = {"v": self._SUPERSEDE_JOURNAL_V, **journal}
        record["checksum"] = self._supersede_journal_checksum(record)
        path = self._supersede_journal_path()
        _mkdir_durable(path.parent)
        config.secure_file_permissions(path.parent, 0o700)   # never raises
        _write_atomic_durable(path, json.dumps(record).encode("utf-8"),
                              mode=0o600)

    def _clear_supersede_journal(self) -> None:
        """Durably forget a FINISHED transaction. The unlink is fsynced for the
        same reason the write is: a directory entry that never reached the disk
        brings the journal back after a power loss, and recovery would then
        "roll back" a supersede that actually completed — both sides differ
        from their recorded ``*_before``, which is exactly the signature of an
        interrupted one.

        STRICTLY fsynced (round 3): ``cos._fsync_dir`` swallows the failure,
        and an unreported one is precisely the case that resurrects the
        journal."""
        path = self._supersede_journal_path()
        path.unlink(missing_ok=True)
        _fsync_dir_strict(path.parent)

    def _recover_pending_supersede(self) -> dict[str, Any] | None:
        """HOST-only. Roll an interrupted ``supersede``/``unsupersede`` back to
        its pre-transaction state — BOTH sides — and clear the journal, so a
        crash mid-transaction can never leave a signed half-chain. Runs at the
        top of every ``supersede``/``unsupersede`` call before any new write.

        The journal survives only when the transaction did NOT complete (it is
        unlinked after the last write returns), so "a journal exists" is the
        whole decision — no stage inference is needed and none is kept. The
        earlier version restored only ``old_before`` on ``stage ==
        "old_written"``, which was correct for a crash BETWEEN the two writes
        and wrong for a crash AFTER the second: the second note kept its new
        content while the first was rolled back, manufacturing exactly the
        one-sided chain ``unsupersede`` exists to repair (HIGH, adversarial
        review 2026-08-10 — crash injection after ``unsupersede``'s second
        signed write left ``old.superseded_by="new"`` with
        ``new.previous_version=None``).

        Each side is restored only when its on-disk content actually differs
        from the recorded ``*_before``, so recovery is idempotent and a
        re-crashed recovery simply resumes.

        **An unreadable journal FAILS CLOSED and is preserved.** It used to be
        deleted and reported as ``recovered: False`` — throwing away the only
        record of the pre-transaction bytes while the half-chain it described
        stayed on disk, and then letting the next call proceed on top of it.
        A journal this path cannot parse is the one case a human must see, so
        it raises and leaves the file where it is.

        **A PARTIAL journal is unreadable too** (round 3). It used to accept
        any non-empty SUBSET of sides, restore only those, and then unlink the
        file regardless — a probe with a journal carrying only ``old_rel`` /
        ``old_before`` returned ``{"restored":["old"], "journal_exists":false,
        "old_superseded_by":"new", "new_previous_version":null}``: it RECREATED
        the one-sided chain this whole guard exists to prevent, and destroyed
        the remaining evidence on the way out. Every field of both sides is
        now required, typed, distinct, vault-contained, id-consistent with the
        recorded pre-image, and covered by the checksum. Anything short of that
        raises and the journal stays.

        **The host-private path is the ONLY path read** (round 4). Recovery
        used to fall back to the pre-2026-08-10 on-mount location when this one
        held nothing, waiving the version stamp and checksum for it because the
        old format had neither. That made ``<runtime>/supersede-pending.json``
        — writable by the untrusted Cowork leg — a way to hand the host two
        arbitrary note bodies at an arbitrary classification and have the
        hourly ``maintain`` sign them. The migration was worth nothing (a
        journal exists only for the seconds a supersession is in flight, and
        none was pending anywhere) and cost a signed MNPI-to-Public downgrade,
        so the fallback is deleted rather than hardened."""
        path = self._supersede_journal_path()
        if not path.exists():
            return None
        import json

        def _unreadable(why: str) -> SupersedeJournalUnreadable:
            return SupersedeJournalUnreadable(
                f"supersede: the crash journal at {path} is unreadable ({why}) "
                f"— it is the ONLY record of the pre-transaction content of a "
                f"supersede/unsupersede that did not finish, so it has been "
                f"PRESERVED and nothing was written. Inspect the two notes it "
                f"names against the audit log, repair them by hand, then delete "
                f"the journal to unblock supersession.")

        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _unreadable(f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(journal, dict):
            raise _unreadable(f"not an object: {type(journal).__name__}")
        self._validate_supersede_journal(journal, unreadable=_unreadable)
        op = str(journal["op"])
        result: dict[str, Any] = {"recovered": True, "op": op, "restored": [],
                                  "journal": str(path)}
        for side in ("old", "new"):
            rel, before = journal[f"{side}_rel"], journal[f"{side}_before"]
            try:
                current = (self.vault / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
            if current == before:
                continue  # this side never moved (or was already restored)
            self.write_note(
                rel, before,
                reason=f"{op}-rollback: {journal.get('old_id')} -> "
                       f"{journal.get('new_id')} (interrupted mid-transaction, "
                       f"restoring {side})",
            )
            result["restored"].append(side)
        result["action"] = ("rolled_back_" + "_".join(result["restored"])
                            if result["restored"] else "nothing_to_roll_back")
        self._clear_supersede_journal()
        return result

    def _validate_supersede_journal(
        self, journal: dict[str, Any], *, unreadable: Any,
    ) -> None:
        """Raise unless ``journal`` describes a COMPLETE two-sided rollback.

        Recovery rewrites signed notes from these bytes, so every one of them
        is checked before a single write: the version stamp and checksum (a
        torn or edited record), both ids present and distinct, both relative
        paths present, distinct and resolving INSIDE the vault (a ``../``
        would make rollback an arbitrary audited overwrite), both pre-images
        present as strings, and each pre-image's own frontmatter ``id``
        matching the id the journal claims for that side — the cheap check
        that the two halves actually belong to the transaction named.

        There is no waiver and no exempt caller (round 4): the legacy on-mount
        reader that needed one is gone."""
        if journal.get("v") != self._SUPERSEDE_JOURNAL_V:
            raise unreadable(
                f"schema version {journal.get('v')!r}, expected "
                f"{self._SUPERSEDE_JOURNAL_V}")
        want = self._supersede_journal_checksum(journal)
        if journal.get("checksum") != want:
            raise unreadable(
                f"checksum mismatch (recorded {journal.get('checksum')!r}, "
                f"computed {want!r}) — the record is torn or was edited")
        if journal.get("op") not in ("supersede", "unsupersede"):
            raise unreadable(f"unknown op {journal.get('op')!r}")
        for key in ("old_id", "new_id", "old_rel", "new_rel",
                    "old_before", "new_before"):
            if not isinstance(journal.get(key), str) or not journal[key].strip():
                raise unreadable(f"missing or non-string {key!r}")
        if journal["old_id"] == journal["new_id"]:
            raise unreadable("both sides name the same id")
        if journal["old_rel"] == journal["new_rel"]:
            raise unreadable("both sides name the same path")
        for side in ("old", "new"):
            rel = journal[f"{side}_rel"]
            if not _contained_in(self.vault / rel, self.vault):
                raise unreadable(f"{side}_rel {rel!r} escapes the vault")
            meta, _ = frontmatter.parse_text(journal[f"{side}_before"])
            got = str(meta.get("id") or "").strip()
            if got != journal[f"{side}_id"]:
                raise unreadable(
                    f"{side}_before carries id {got!r}, but the journal names "
                    f"{journal[f'{side}_id']!r}")

    def recover_pending_supersede(self, *, dry_run: bool = False) -> dict[str, Any] | None:
        """Public preflight: roll back an interrupted supersession, under the
        same single-writer lock the write verbs take. ``None`` when nothing is
        pending; raises :class:`SupersedeJournalUnreadable` when a journal
        exists but cannot be acted on (fail closed, journal preserved).

        Called once at the top of ``maintain`` — see that docstring for why
        leaving it to ``supersede``'s own call site was not enough.

        ``dry_run`` REPORTS a pending journal without writing anything: a
        rollback is two signed note writes, which is exactly what --dry-run
        promises not to do."""
        self._require_host("recover a pending supersede journal (writes notes)")
        path = self._supersede_journal_path()
        if not path.exists():
            return None
        if dry_run:
            return {"recovered": False, "pending": True, "journal": str(path),
                    "action": "dry-run: not recovered"}
        with vault_writer_lock(self.vault, verb="supersede-recover"):
            return self._recover_pending_supersede()

    def supersede(self, old_id: str, new_id: str, *, reason: str = "",
                  expect: dict[str, Any] | None = None) -> dict[str, Any]:
        """Retire ``old_id`` in favour of ``new_id`` — both sides of the version
        chain, written through the audited ``write_note`` path (ADR-0003 Ruling
        2/8). HOST-broker only.

        Refuses BEFORE any signing-key resolution / WAL / index mutation when:
        - ``role != host``;
        - either id does not resolve to an on-disk note, or ``old_id == new_id``;
        - ``old_id`` is already superseded (chain invariant: no re-superseding an
          already-superseded note);
        - ``new_id`` itself already carries ``is_latest_version: false`` (would
          make it a "latest" that is simultaneously retired — refuse creating a
          second latest);
        - the successor's OWN frontmatter has no explicit ``classification`` —
          per the ADR ruling, classification is NEVER inherited implicitly
          across a supersession.

        Atomicity: a pending-operation journal (host-private, off the Cowork
        mount — ``config.supersede_journal_path``) is written before either
        note write and cleared after both are DURABLY committed. A crash between the two signed writes leaves
        a journal that the NEXT ``supersede`` call rolls back (restores the old
        note, then proceeds) before doing anything else — never a signed
        half-chain (HARDENED:codex).

        CC-02 (finding 1, 2026-07-20 dedup batch): the ENTIRE critical section
        (journal recovery, both signed writes, the trailing reindex) runs under
        the SAME bounded single-writer lock ``sync``/``rebuild``/``snapshot``
        use — previously `supersede` wrote both notes completely unlocked and
        only its trailing ``self.sync()`` call ever touched the lock, so a
        concurrent long-running writer (e.g. the hourly sync) could leave a
        `supersede` call blocking silently for minutes with no bounded refusal.
        One acquisition here means a busy writer is named and refused within
        ``$BRAIN_WRITER_LOCK_SECONDS`` (default 30s) — never a multi-minute
        silent block — and the lock's re-entrant depth counter means the
        trailing ``self.sync()`` call's own acquisition is a same-process no-op,
        not a second wait.

        ``expect`` (HARDENED:codex-8) is an OPTIONAL precondition set a caller
        computed OUT of band — content hashes and chain-head values it saw when
        it decided this supersession was correct. Every key present is verified
        **inside this lock, before the first signed write**. A caller that
        re-checks its own preconditions and THEN calls in is TOCTOU: the
        nightly folds hold the same lock and can retire or rewrite either note
        in the gap between that check and this acquisition. Recognised keys —
        ``old_sha256``/``new_sha256`` (``sha256_text`` of the whole note file),
        ``old_superseded_by``, ``old_is_latest_version``,
        ``new_is_latest_version``, ``new_previous_version``. A mismatch raises
        :class:`SupersedePreconditionFailed` and nothing is written.
        """
        self._require_host("supersede notes (writes both sides of a version chain)")
        with vault_writer_lock(self.vault, verb="supersede"):
            return self._supersede_locked(old_id, new_id, reason=reason,
                                          expect=expect)

    @staticmethod
    def _check_supersede_expect(expect: dict[str, Any], *, old_id: str, new_id: str,
                                old_before: str, new_before: str,
                                old_meta: dict[str, Any],
                                new_meta: dict[str, Any]) -> None:
        """Verify a caller's out-of-band preconditions. Raises on ANY mismatch.

        The two content hashes alone are sufficient (frontmatter is inside the
        file, so any chain mutation moves the hash); the chain-head keys are
        kept because they name WHAT drifted, and "the pair was chained while
        the proposal waited" is the case an operator most needs spelled out.
        """
        actual: dict[str, Any] = {
            "old_sha256": sha256_text(old_before),
            "new_sha256": sha256_text(new_before),
            "old_superseded_by": str(old_meta.get("superseded_by") or "").strip(),
            "new_previous_version": str(new_meta.get("previous_version") or "").strip(),
            "old_is_latest_version": str(
                old_meta.get("is_latest_version", "")).strip().lower(),
            "new_is_latest_version": str(
                new_meta.get("is_latest_version", "")).strip().lower(),
        }
        for key, want in expect.items():
            if key not in actual:
                raise SupersedePreconditionFailed(
                    f"supersede: unknown precondition {key!r}")
            got = actual[key]
            if str(want).strip().lower() != str(got).strip().lower():
                raise SupersedePreconditionFailed(
                    f"supersede {old_id} -> {new_id}: precondition {key!r} "
                    f"drifted (expected {want!r}, found {got!r}) — the pair "
                    "changed after the decision was made; nothing was written")

    def _supersede_locked(self, old_id: str, new_id: str, *, reason: str = "",
                          expect: dict[str, Any] | None = None) -> dict[str, Any]:
        self._recover_pending_supersede()

        if old_id == new_id:
            raise ValueError("supersede: a note may not supersede itself")
        old_row = self.index.get(old_id)
        new_row = self.index.get(new_id)
        if not old_row:
            raise ValueError(f"supersede: old note not found: {old_id}")
        if not new_row:
            raise ValueError(f"supersede: new note not found: {new_id}")

        old_path, new_path = Path(old_row["path"]), Path(new_row["path"])
        old_before = old_path.read_text(encoding="utf-8")
        new_before = new_path.read_text(encoding="utf-8")
        old_meta, _ = frontmatter.parse_text(old_before)
        new_meta, _ = frontmatter.parse_text(new_before)

        # Caller preconditions FIRST: a drifted pair gets the precise "this
        # changed under you" error rather than a generic invariant failure.
        if expect:
            self._check_supersede_expect(
                expect, old_id=old_id, new_id=new_id, old_before=old_before,
                new_before=new_before, old_meta=old_meta, new_meta=new_meta)

        # -- chain invariants + classification ruling (refused before any write) --
        if old_meta.get("superseded_by") or str(old_meta.get("is_latest_version", "")).strip().lower() == "false":
            raise ValueError(f"supersede: {old_id!r} is already superseded — no re-superseding")
        if str(new_meta.get("is_latest_version", "")).strip().lower() == "false":
            raise ValueError(
                f"supersede: {new_id!r} is itself already retired "
                "(is_latest_version: false) — refusing to create a second latest"
            )
        if not str(new_meta.get("classification") or "").strip():
            raise ValueError(
                f"supersede: successor {new_id!r} has no explicit classification — "
                "classification is never inherited across a supersession (ADR-0003 Ruling 2b)"
            )

        import datetime as _dt

        today = _dt.date.today().isoformat()
        old_rel = old_path.relative_to(self.vault).as_posix()
        new_rel = new_path.relative_to(self.vault).as_posix()

        self._write_supersede_journal({
            "op": "supersede", "old_id": old_id, "new_id": new_id,
            "old_rel": old_rel, "new_rel": new_rel,
            "old_before": old_before, "new_before": new_before,
        })

        old_after = frontmatter.set_keys(old_before, {
            "superseded_by": new_id, "superseded_date": today, "is_latest_version": False,
        })
        new_after = frontmatter.set_keys(new_before, {
            "previous_version": old_id, "is_latest_version": True,
        })

        old_write = self.write_note(
            old_rel, old_after,
            reason=reason or f"supersede: {old_id} -> {new_id} (retiring {old_id})",
        )
        new_write = self.write_note(
            new_rel, new_after,
            reason=reason or f"supersede: {old_id} -> {new_id} (new head {new_id})",
        )
        self._clear_supersede_journal()

        sync_res = self.sync(drain=False)
        return {
            "old_id": old_id, "new_id": new_id,
            "old_write": old_write, "new_write": new_write,
            "reindexed": {"added": sync_res.get("added", 0), "updated": sync_res.get("updated", 0)},
        }

    # -- the inverse: undoing a WRONG auto-link (ENF-01, 2026-08-10) --------
    SUPERSESSION_KEYS_OLD = ("superseded_by", "superseded_date", "is_latest_version")
    SUPERSESSION_KEYS_NEW = ("previous_version",)

    def unsupersede(self, old_id: str, new_id: str, *, reason: str = "") -> dict[str, Any]:
        """Undo ONE supersession link ``old_id -> new_id``, both sides, through
        the audited ``write_note`` path. HOST-broker only.

        This exists because DDP-01's nightly auto-dedup could create a link
        nothing could undo. It auto-superseded on BODY identity, and an image
        whose OCR extracted to a 122-byte ``[no text detected]`` stub is
        byte-identical to every other such image — so part 1 of a deck retired
        part 2, and nine distinct QR codes became one version family. ENF-01's
        body floor stops NEW ones; those already written needed an audited
        inverse, and `supersede` deliberately refuses to re-supersede an
        already-superseded note, so there was no way back.

        Refuses BEFORE any write unless ``old_id`` actually claims to be
        retired in favour of ``new_id`` (``old.superseded_by == new_id``) —
        that claim is the thing being undone, and without it the caller is
        naming a link that does not exist.

        The successor's side is repaired opportunistically rather than
        demanded, because the chains most needing repair are the malformed
        ones. On the reference vault two notes both declared
        ``superseded_by: …-qr-qa`` while ``qr-qa`` named only one of them as
        its ``previous_version``; requiring reciprocity would have left the
        unreciprocated half permanently unfixable. So ``previous_version`` is
        dropped from ``new_id`` only when it names ``old_id``, and otherwise
        left alone and reported as ``new_previous_version_kept``.

        Both notes come out in their PRE-link shape: the three retirement keys
        are dropped from ``old_id`` (absence of ``is_latest_version`` reads as
        "not retired" per AGENTS.md §2, so it is dropped rather than flipped to
        true). ``new_id``'s own ``is_latest_version`` is left exactly as found
        — it may be the head of an unrelated chain this call has no business
        touching.

        Same single-writer lock and same crash journal as ``supersede`` (a
        crash between the two signed writes is rolled back by the same
        ``_recover_pending_supersede``).
        """
        self._require_host("unsupersede notes (writes both sides of a version chain)")
        with vault_writer_lock(self.vault, verb="unsupersede"):
            return self._unsupersede_locked(old_id, new_id, reason=reason)

    def _unsupersede_locked(self, old_id: str, new_id: str, *,
                            reason: str = "") -> dict[str, Any]:
        self._recover_pending_supersede()

        if old_id == new_id:
            raise ValueError("unsupersede: a note may not supersede itself")
        old_row = self.index.get(old_id)
        new_row = self.index.get(new_id)
        if not old_row:
            raise ValueError(f"unsupersede: old note not found: {old_id}")
        if not new_row:
            raise ValueError(f"unsupersede: new note not found: {new_id}")

        old_path, new_path = Path(old_row["path"]), Path(new_row["path"])
        old_before = old_path.read_text(encoding="utf-8")
        new_before = new_path.read_text(encoding="utf-8")
        old_meta, _ = frontmatter.parse_text(old_before)
        new_meta, _ = frontmatter.parse_text(new_before)

        # AGENTS.md §2 permits a bare id OR a `[[wikilink]]`, and `replaces` is
        # a documented alias of `previous_version` — `notes._bitemporal_link`
        # is the ONE normalization the index already applies to both. Comparing
        # raw frontmatter here refused `superseded_by: [[new]]` outright and
        # left a `replaces:` predecessor link standing after a "successful"
        # repair (adversarial review 2026-08-10).
        from .notes import _bitemporal_link

        def _link(val: object) -> str:
            # A bare-date id (`superseded_by: 2026-05-27`) is parsed by YAML as
            # a date, not a string — those exist in the reference vault, so
            # stringify before normalizing rather than silently reading "".
            return _bitemporal_link(val if isinstance(val, str) or val is None
                                    else str(val))

        if _link(old_meta.get("superseded_by")) != new_id:
            raise ValueError(
                f"unsupersede: {old_id!r} is not superseded by {new_id!r} "
                f"(found {old_meta.get('superseded_by')!r}) — nothing written")
        #: whichever documented predecessor key(s) actually name old_id
        back_keys = tuple(k for k in ("previous_version", "replaces")
                          if _link(new_meta.get(k)) == old_id)
        reciprocal = bool(back_keys)

        old_rel = old_path.relative_to(self.vault).as_posix()
        new_rel = new_path.relative_to(self.vault).as_posix()

        self._write_supersede_journal({
            "op": "unsupersede", "old_id": old_id, "new_id": new_id,
            "old_rel": old_rel, "new_rel": new_rel,
            "old_before": old_before, "new_before": new_before,
        })

        old_after = frontmatter.drop_keys(old_before, self.SUPERSESSION_KEYS_OLD)

        why = reason or f"unsupersede: broke the {old_id} -> {new_id} link"
        old_write = self.write_note(old_rel, old_after, reason=f"{why} (restoring {old_id})")
        new_write = None
        if reciprocal:
            new_write = self.write_note(
                new_rel, frontmatter.drop_keys(new_before, back_keys),
                reason=f"{why} (clearing {new_id})")
        self._clear_supersede_journal()

        sync_res = self.sync(drain=False)
        kept = next((str(new_meta.get(k)) for k in ("previous_version", "replaces")
                     if not reciprocal and new_meta.get(k)), None)
        return {
            "old_id": old_id, "new_id": new_id,
            "old_write": old_write, "new_write": new_write,
            "cleared_keys": list(back_keys),
            "new_previous_version_kept": kept,
            "reindexed": {"added": sync_res.get("added", 0),
                          "updated": sync_res.get("updated", 0)},
        }

    def verify_audit(self, *, check_content: bool = False) -> dict[str, Any]:
        # HOST-broker only: verify() derives the public key via the resolved
        # signing key — the VM leg must never resolve a key.
        self._require_host("verify the audit chain (resolves the signing key)")
        from . import audit as _audit

        res = self.audit.verify()
        # INT-02: the content pass runs on the DEFAULT surface too. A
        # signature-only "ok" reads as a content all-clear while notes signed
        # weeks ago sit changed on disk — so the plain command always reports
        # the count, and `--check-content` only adds the per-note detail.
        summary = _audit.drift_summary(self.vault, self.audit)
        res["content_drift_count"] = summary["total"]
        res["content_drift_unexplained"] = summary["unexplained"]
        if check_content:
            res["content_drift"] = summary["records"]
        if summary["unexplained"] and res["status"] == "ok":
            # signatures fine, but a signed note's bytes changed on disk and
            # nothing has triaged it
            res["status"] = "content_drift"
        return res

    # -- off-host anchor + encrypted backup (HOST-broker only; SEC-03) ----
    def anchor_chain(self, anchor_dir: str | Path) -> dict[str, Any]:
        """Publish the signed chain head to an OFF-HOST append-only store."""
        self._require_host("anchor the audit chain off-host")
        from . import anchor as _anchor

        return _anchor.anchor(self.audit.log_path, Path(anchor_dir))

    def verify_anchor(self, anchor_dir: str | Path) -> dict[str, Any]:
        """Verify the live chain against the off-host anchor (detect rewrite)."""
        self._require_host("verify the off-host anchor")
        from . import anchor as _anchor

        return _anchor.verify_against_anchor(self.audit.log_path, Path(anchor_dir))

    def backup(self, dest_dir: str | Path, *, encrypt: bool = True) -> dict[str, Any]:
        """Create an encrypted off-device backup of the Markdown truth."""
        self._require_host("create an off-device backup")
        from . import backup as _backup

        return _backup.create_backup(self.vault, Path(dest_dir), encrypt=encrypt).to_dict()

    def restore(self, archive: str | Path, dest_dir: str | Path) -> dict[str, Any]:
        """Restore (and decrypt) a backup archive into ``dest_dir``."""
        self._require_host("restore a backup")
        from . import backup as _backup

        return _backup.restore_backup(Path(archive), Path(dest_dir))

    # -- daily-use UX layer (UX-01 / UX-02) --------------------------------

    def capture(
        self,
        content: str,
        *,
        note_id: str | None = None,
        note_type: str | None = None,
        classification: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Unified capture verb (UX-01).

        HOST path: enforce frontmatter → write_note (sign + audit) → incremental
                   sync → note immediately retrievable.
        VM path:   enforce frontmatter → draft_capture (capture-inbox/, unsigned,
                   unindexed) → host drain-on-invoke picks it up on the next run.

        No signing key is ever touched on the VM path. The VM drops an untrusted
        draft; the host validates, signs, and indexes it on drain-on-invoke.
        """
        from . import capture as cap_mod

        override: dict[str, Any] = {}
        if note_id:
            override["id"] = note_id
        if note_type:
            override["type"] = note_type
        if classification:
            override["classification"] = classification

        enforced = cap_mod.enforce(content, override=override or None)

        if self.role == config.ROLE_HOST:
            meta, _body = frontmatter.parse_text(enforced)
            nid = safe_slug(meta.get("id", "capture"))  # same C-1/C-2 trust boundary
            ntype = str(meta.get("type", "note"))
            if ntype == "source":
                rel, subtree = f"raw/{nid}.md", "raw"
            else:
                rel, subtree = f"brain/resources/{nid}.md", "brain/resources"
            write_res = self.write_note(rel, enforced, reason=reason or f"capture {nid}",
                                        subtree=subtree)
            sync_res = self.sync(drain=False)  # note already written; just reconcile
            return {
                "id": nid,
                "path": write_res["written"],
                "signed": True,
                "indexed": True,
                "role": "host",
                "sync": {
                    "added": sync_res.get("added", 0),
                    "updated": sync_res.get("updated", 0),
                },
            }
        else:
            res = self.draft_capture(enforced, ident=None, is_source=False)
            return {
                "id": res["id"],
                "draft": res["draft"],
                "signed": False,
                "indexed": False,
                "role": "vm",
                "note": "draft in capture-inbox/; host drain-on-invoke will sign + index",
            }

    def brief(
        self, *, max_recent: int = 5, drain: bool = True,
        max_tier: str = classification.DEFAULT_MAX_TIER,
    ) -> dict[str, Any]:
        """Generate the morning brief (UX-02).

        Drains pending captures first (HOST only) — making this the guaranteed
        daily drain FLOOR when run as the scheduled task. Always reports the
        pending count BEFORE the drain attempt so a stalled drain is visible
        next morning via the tripwire line.

        VM leg: reports pending count + index stats (read-only view) but cannot
        drain (no signing key).

        The recent-notes list is routed through the SAME egress.apply_gate
        chokepoint as every other read verb (H-1) — a summary surface must not
        leak titles/paths/classification of withheld-tier notes.
        """
        from . import brief as brief_mod
        from . import egress
        from .snapshot import snapshot_status

        pending_before = self._count_pending_drafts()
        drain_res: dict[str, Any] = {"promoted": 0, "skipped": 0}

        if self.role == config.ROLE_HOST and drain:
            try:
                drain_res = self.drain_drafts()
            except Exception as exc:
                drain_res = {"promoted": 0, "skipped": 0, "error": str(exc)}

        try:
            stats = self.index.stats()
        except Exception:
            stats = {"notes": 0, "chunks": 0}

        try:
            recent = self.recent(limit=max_recent)
        except Exception:
            recent = []

        surfaced, egress_report = egress.apply_gate(recent, max_tier=max_tier)

        snap = snapshot_status(config.snapshot_dir(self.vault))
        age_hours: float | None = None
        if snap.get("snapshot") == "present" and snap.get("age_seconds") is not None:
            age_hours = snap["age_seconds"] / 3600

        cos_liveness: dict[str, Any] | None = None
        if self.role == config.ROLE_HOST:
            try:
                from . import cos as cos_mod
                cos_liveness = cos_mod.batch_liveness(self.vault)
            except Exception:  # noqa: BLE001 — a liveness read never breaks the brief
                cos_liveness = None

        result = brief_mod.build_brief(
            index_stats=stats,
            recent_notes=surfaced,
            pending_before_drain=pending_before,
            drain_result=drain_res,
            snapshot_age_hours=age_hours,
            max_recent=max_recent,
            maintain_state=self._load_maintain_state(),
            cos_liveness=cos_liveness,
        )
        result["egress"] = egress_report
        return result

    def digest(
        self, *, days: int = 7, max_tier: str = classification.DEFAULT_MAX_TIER,
    ) -> dict[str, Any]:
        """Generate the weekly digest (UX-02).

        Shows notes from the past ``days`` days. Available on both host and VM
        legs (read-only; reads from the index/snapshot in use for this role).

        The recent-notes list is gated through egress.apply_gate before it is
        built into the digest (H-1) — same chokepoint as every other read verb.
        """
        from . import brief as brief_mod
        from . import egress

        try:
            stats = self.index.stats()
        except Exception:
            stats = {"notes": 0, "chunks": 0}

        try:
            recent = self.recent(limit=500)
        except Exception:
            recent = []

        surfaced, egress_report = egress.apply_gate(recent, max_tier=max_tier)

        result = brief_mod.build_digest(
            index_stats=stats, recent_notes=surfaced, days=days,
            maintain_state=self._load_maintain_state(),
        )
        result["egress"] = egress_report
        return result

    def _autoresearch_status(self, today: Any) -> dict[str, Any]:
        """Maintenance-visibility line data (HARDENED:claude, AUT-01): scan
        ``eval/runs/autoresearch-*.json`` for the newest ``captured``
        timestamp and judge staleness via the pure
        ``maintenance.autoresearch_staleness`` helper. No autoresearch run has
        landed at this session (aut-04 is session s11, after this one) — a
        missing/unreadable artifact is treated as ``never_run``, never an
        error, so the brief still renders."""
        import datetime as _dt
        import json

        from . import maintenance as maint

        runs_dir = Path(__file__).resolve().parents[2] / "eval" / "runs"
        latest: _dt.datetime | None = None
        try:
            for p in runs_dir.glob("autoresearch-*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    ts = _dt.datetime.fromisoformat(str(data.get("captured")))
                except Exception:
                    continue
                if latest is None or ts > latest:
                    latest = ts
        except Exception:
            pass
        return maint.autoresearch_staleness(latest.date() if latest else None, today)

    def brief_html(
        self, *, max_recent: int = 5, drain: bool = True,
        max_tier: str = classification.VM_DEFAULT_MAX_TIER, today: Any = None,
    ) -> dict[str, Any]:
        """Render + write the branded HTML morning brief (AUT-01, ADR-0003
        Ruling c) to ``.brain/brief/``. HOST-ONLY: this writes a FILE, a new
        egress surface the stdout gate does not cover, so every section is
        composed from data already routed through ``egress.apply_gate`` at
        ``max_tier`` (default Internal) before it reaches the pure renderer
        (``brain.brief.render_brief_html``), which does no I/O of its own —
        gathering + gating happens entirely here.
        """
        import datetime as _dt

        from . import brief as brief_mod
        from . import egress
        from . import maintenance as maint
        from . import overlay as ov

        self._require_host("write the HTML morning brief")
        d = today or _dt.date.today()
        flt = classification.ClassificationFilter(max_tier=max_tier)

        base = self.brief(max_recent=max_recent, drain=drain, max_tier=max_tier)

        try:
            stale_links = self.index.stale_wikilink_targets()
        except Exception:
            stale_links = []
        stale_links = [
            s for s in stale_links
            if flt.allows((s.get("from") or {}).get("classification"))
            and (s.get("target") is None or flt.allows((s.get("target") or {}).get("classification")))
        ]

        try:
            revisit_sample = self.index.revisit_sample(today=d, k=10)
        except Exception:
            revisit_sample = []
        revisit_sample, _ = egress.apply_gate(revisit_sample, max_tier=max_tier)

        open_recs: list[dict[str, Any]] = []
        try:
            open_path = config.recommendations_open_path(self.vault)
            if open_path.exists():
                open_recs = maint.parse_recommendation_lines(open_path.read_text(encoding="utf-8"))
        except Exception:
            open_recs = []

        hot_head: list[str] = []
        try:
            hot_path = self._hot_md_path()
            if hot_path.exists():
                hot_head = brief_mod.parse_hot_entries(hot_path.read_text(encoding="utf-8"))[-5:]
        except Exception:
            hot_head = []

        autoresearch = self._autoresearch_status(d)
        brand = ov.resolve_brand(self.vault)

        html_text = brief_mod.render_brief_html(
            base, stale_links=stale_links, revisit_sample=revisit_sample,
            open_recommendations=open_recs, hot_head=hot_head,
            autoresearch=autoresearch, brand=brand,
        )

        out_dir = config.brief_dir(self.vault)
        out_dir.mkdir(parents=True, exist_ok=True)
        dated = out_dir / f"brief-{d.isoformat()}.html"
        latest = out_dir / "brief-latest.html"
        dated.write_text(html_text, encoding="utf-8")
        latest.write_text(html_text, encoding="utf-8")
        return {"path": str(dated), "latest_path": str(latest), "bytes": len(html_text)}

    def digest_html(
        self, *, days: int = 7, max_tier: str = classification.VM_DEFAULT_MAX_TIER,
        today: Any = None,
    ) -> dict[str, Any]:
        """Render + write the branded HTML weekly digest (AUT-03, ADR-0003
        Ruling c) to ``.brain/brief/``. HOST-ONLY (writes a file). Notes are
        already routed through ``egress.apply_gate`` inside ``self.digest()``
        before the pure renderer (``brain.brief.render_digest_html``) formats
        them — the renderer performs no I/O."""
        import datetime as _dt

        from . import brief as brief_mod
        from . import overlay as ov

        self._require_host("write the HTML weekly digest")
        d = today or _dt.date.today()

        base = self.digest(days=days, max_tier=max_tier)
        brand = ov.resolve_brand(self.vault)
        html_text = brief_mod.render_digest_html(base, brand=brand)

        out_dir = config.brief_dir(self.vault)
        out_dir.mkdir(parents=True, exist_ok=True)
        dated = out_dir / f"digest-{d.isoformat()}.html"
        latest = out_dir / "digest-latest.html"
        dated.write_text(html_text, encoding="utf-8")
        latest.write_text(html_text, encoding="utf-8")
        return {"path": str(dated), "latest_path": str(latest), "bytes": len(html_text)}

    def health_report(self, *, today: Any = None) -> dict[str, Any]:
        """Render + write the static HTML health report (``brain
        health-report``) to ``.brain/brief/health-latest.html`` — a single
        colored verdict (HEALTHY/DEGRADED/BROKEN) + an "act now" list +
        maintain-branch/index/snapshot/trend tables, assembled entirely from
        data the engine already collects (maintain-state.json,
        health-history.jsonl, ``brain doctor``, ``status()``). HOST-ONLY:
        writes a file, same posture as ``brief_html``/``digest_html``. See
        ``brain.healthreport`` for the render contract."""
        from . import healthreport as hr

        self._require_host("render the health report")
        data = hr.collect_health_report_data(self, today=today)
        html_text = hr.render_health_report_html(data)

        out_dir = config.brief_dir(self.vault)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "health-latest.html"
        path.write_text(html_text, encoding="utf-8")
        return {"path": str(path), "verdict": data["verdict"], "act_now": data["act_now"]}

    def graph_report(self, *, today: Any = None) -> dict[str, Any]:
        """Render + write the static HTML graph explorer (``brain
        graph-report``) to ``.brain/graph/graph-explorer.html`` — a
        self-contained WebGL link-graph + 3D semantic-map page, rebuilt from
        the graphify discovery build (``.brain/graph/graph.json``,
        ``authoritative: false``) plus the live index. HOST-ONLY: reads the
        writable index, writes a file. See ``brain.graphreport`` for the
        payload/render contract. Never crashes on missing embeddings — see
        that module's ``semantic_note`` degrade path."""
        from . import graphreport as gr

        self._require_host("render the graph report")
        return gr.generate_graph_report(self, today=today)

    # -- maintenance rituals (CUT-03) --------------------------------------
    # check / health / curate / integrity / promote-scan + the `maintain`
    # umbrella. Per routines/manifest.json (disposition field) these are WRITE rituals
    # (regen index, sign+drain, query the audit chain) -> HOST-broker only,
    # never runnable under BRAIN_ROLE=vm. Content-listing returns here
    # (curate/integrity/promote_scan) are UNFILTERED by design (module
    # contract, see top of file) — brain.cli applies the egress gate before
    # surfacing, exactly like the read verbs.

    def check(self, *, dry_run: bool = False) -> dict[str, Any]:
        """daily-check fold: index reconcile + drain drafts + freshness status
        (task-disposition.md row 1). ``dry_run`` skips the mutation and reports
        status only — still a real read against the live index."""
        from . import maintenance as maint

        self._require_host("run the check ritual")
        auto_fixed: list[dict[str, Any]] = []
        action_required: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        sync_res: dict[str, Any] | None = None
        if not dry_run:
            sync_res = self.sync(drain=True, publish=False)
            added = sync_res.get("added", 0)
            updated = sync_res.get("updated", 0)
            deleted = sync_res.get("deleted", 0)
            if added or updated or deleted:
                auto_fixed.append(maint.auto_fixed_item(
                    "sync", str(self.vault),
                    f"index reconciled +{added} ~{updated} -{deleted}"))
            drain = sync_res.get("drain", {}) or {}
            if drain.get("promoted"):
                auto_fixed.append(maint.auto_fixed_item(
                    "drain", str(self.capture_inbox_dir()),
                    f"drained {drain['promoted']} pending capture(s)"))
            details = drain.get("details", {})
            skipped_list = details.get("skipped", []) if isinstance(details, dict) else []
            for skip in skipped_list:
                reason = skip.get("reason", "")
                # Attribute by SOURCE, not by matching words in the reason: an
                # approved-queue item that is skipped for ANY cause (duplicate
                # id, bad frontmatter, a refusal) does not live in capture-inbox,
                # and printing a path that does not exist sends the operator to
                # the wrong directory.
                if skip.get("source") == "approved-queue":
                    from . import cos as _cos_q
                    draft_path = str(_cos_q.approved_queue_dir(self.vault)
                                     / skip.get("draft", ""))
                    refused = "approved-queue refusal" in reason
                    action_required.append(maint.action_required_item(
                        (f"owner-approved candidate {skip.get('draft')} was REFUSED "
                         f"at the signing gate (not signed)" if refused else
                         f"owner-approved candidate {skip.get('draft')} could not "
                         f"be signed"),
                        reason,
                        ("inspect the quarantined *.refused copy and the "
                         "approved-queue-refusal defect row; re-propose if the "
                         "content is still wanted" if refused else
                         "resolve the cause above — the item stays queued and is "
                         "retried on every drain until it is"),
                        draft_path))
                    continue
                draft_path = str(self.capture_inbox_dir() / skip.get("draft", ""))
                if "no-signing-key" in reason:
                    blocked.append(maint.blocked_item(
                        f"capture draft {skip.get('draft')} could not be drained",
                        "no audit signing key resolved",
                        "signing key configured (Keychain/env), then re-run check"))
                else:
                    action_required.append(maint.action_required_item(
                        f"capture draft {skip.get('draft')} could not be drained",
                        reason or "unrecognised draft frontmatter",
                        "fix the draft's frontmatter, then re-run check",
                        draft_path))

        status_res = self.status()
        return {
            "ritual": "check", "dry_run": dry_run,
            "sync": sync_res, "status": status_res,
            "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
        }

    def health(self) -> dict[str, Any]:
        """health fold: index/snapshot status + audit-chain verify + a
        substrate self-test probe (task-disposition.md row 2). Entirely
        READ-ONLY — safe to run under a caller's --dry-run posture too."""
        from . import maintenance as maint

        self._require_host("run the health ritual")
        action_required: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        status_res = self.status()

        audit_res: dict[str, Any] | None = None
        try:
            audit_res = self.verify_audit()
            if audit_res.get("status") not in ("ok", "empty"):
                action_required.append(maint.action_required_item(
                    _audit_status_summary(audit_res),
                    "chain tamper/break needs human judgment, never auto-repaired",
                    "inspect the chain errors; re-link from the last-good entry "
                    "(content drift: `brain verify-audit --check-content --json`)",
                    str(self.audit.log_path) if self.audit else "audit chain"))
        except Exception as exc:
            blocked.append(maint.blocked_item(
                "could not verify the audit chain",
                f"{type(exc).__name__}: {exc}",
                "signing key configured (Keychain/env), then re-run health"))

        selftest: dict[str, Any] = {"probe_ok": False}
        live = status_res.get("live_embedder", {})
        try:
            hits = self.hybrid_search("brain", k=1)
            ix = status_res.get("index", {})
            selftest = {
                "probe_ok": True, "result_count": len(hits),
                "vector_backend": ix.get("vector_backend"),
                "embed_model": ix.get("embed_model"),
                # LIVE embedder actually in use (S11) — distinct from the index's
                # recorded embed_model metadata above.
                "live_embedder": live.get("model_id"),
                "hash_fallback": bool(live.get("is_hash_fallback")),
            }
        except Exception as exc:
            blocked.append(maint.blocked_item(
                "retrieval self-test probe raised",
                f"{type(exc).__name__}: {exc}",
                "investigate the embedder/vector-backend, then re-run health"))
        # A live HashEmbedder on an index built with a real model is a silent
        # semantic downgrade — surface it as ACTION REQUIRED, not a pass.
        if live.get("is_hash_fallback") and not live.get("matches_index_metadata"):
            action_required.append(maint.action_required_item(
                "live embedder is the non-semantic HashEmbedder but the index was "
                f"built with {status_res.get('index', {}).get('embed_model')!r}",
                "retrieval quality is effectively random on this install "
                "(onnxruntime/tokenizers missing or the bge-m3-int8 model absent)",
                "install the 'corporate' extras (onnxruntime + tokenizers) or the "
                "bundled model; set BRAIN_REQUIRE_REAL_EMBEDDER=1 to fail closed",
                "brain status --json (live_embedder block)"))

        return {
            "ritual": "health", "status": status_res, "audit": audit_res,
            "selftest": selftest,
            "outcomes": maint.build_outcomes([], action_required, blocked),
        }

    def _framework_sync_finding(self) -> dict[str, Any] | None:
        """HYG-02 (ADR-0003 Ruling 5): the Monday health branch also runs the
        framework-sync drift audit (canonical .claude/skills/ vs the
        .agents/skills/ + plugins/ mirrors, plus CLAUDE.md's @AGENTS.md
        import) — reported as a health finding, NEVER auto-fixed. Best-effort
        and silent when not applicable: ``tools/framework_sync.py`` is a
        dev-only script that lives in the profile-a-brain source checkout,
        not the installed package, so a generic installed vault (no sibling
        ``tools/`` tree) simply has nothing to compare and skips."""
        from . import maintenance as maint

        repo_root = Path(__file__).resolve().parents[2]
        fsync_path = repo_root / "tools" / "framework_sync.py"
        if not fsync_path.is_file():
            return None
        import sys as _sys
        tools_dir = str(repo_root / "tools")
        inserted = tools_dir not in _sys.path
        if inserted:
            _sys.path.insert(0, tools_dir)
        try:
            import framework_sync as fsync
            report = fsync.audit()
        except Exception as exc:
            return maint.action_required_item(
                f"framework-sync drift audit raised: {type(exc).__name__}: {exc}",
                "could not compare .claude/skills/ against its mirrors",
                "inspect tools/framework_sync.py, then re-run health",
                str(fsync_path))
        finally:
            if inserted and tools_dir in _sys.path:
                _sys.path.remove(tools_dir)
        return maint.framework_sync_finding(report)

    def curate(
        self, *, dry_run: bool = False, k: int = 50, today: Any = None,
    ) -> dict[str, Any]:
        """curation fold (task-disposition.md row 4, extended by AUT-02): the
        refresh-index sub-step folds to ``sync``; unclassified-notes lint,
        stale-wikilink-target detection, and an age x centrality revisit
        sample now run directly against the brain index (ADR-0003 Ruling 5 —
        this SUPERSEDES the curation skill's previously-documented "no brain
        equivalent, G3 not shipped" framing for those two checks). Orphan/
        contradiction/callout lint stay vault-structure overlay tooling with
        NO brain equivalent (G4 RETIRE) — those still route through
        ``.claude/skills/curation``. UNFILTERED findings — the CLI
        egress-gates every one before surfacing."""
        from . import maintenance as maint

        self._require_host("run the curate ritual")
        auto_fixed: list[dict[str, Any]] = []

        sync_res: dict[str, Any] | None = None
        if not dry_run:
            sync_res = self.sync(drain=False)
            added = sync_res.get("added", 0)
            updated = sync_res.get("updated", 0)
            deleted = sync_res.get("deleted", 0)
            if added or updated or deleted:
                auto_fixed.append(maint.auto_fixed_item(
                    "sync", str(self.vault),
                    f"refresh-index +{added} ~{updated} -{deleted}"))

        unclassified = self.index.unclassified_notes(k=k)
        stale_links = self.index.stale_wikilink_targets()
        revisit_sample = self.index.revisit_sample(today=today, k=10)
        return {
            "ritual": "curate", "dry_run": dry_run, "sync": sync_res,
            "unclassified_notes": unclassified,  # UNFILTERED
            "stale_links": stale_links,          # UNFILTERED
            "revisit_sample": revisit_sample,    # UNFILTERED
            "overlay_only_skipped": {
                "orphans": "vault-structure overlay, no brain equivalent (RETIRE)",
                "contradictions": "vault-structure overlay, no brain equivalent (RETIRE)",
                "callouts": "vault-structure overlay, no brain equivalent (RETIRE)",
            },
            "auto_fixed": auto_fixed,
        }

    def integrity(self, *, min_score: float = 0.95, k: int = 5) -> dict[str, Any]:
        """integrity-scan fold (task-disposition.md row 3): audit-chain verify
        + a corpus-wide near-dup scan directly over the brain vector backend
        (brain-cli-gaps.md G1 — no SC/MCP round-trip). READ-ONLY. UNFILTERED
        ``near_dup_pairs`` — the CLI egress-gates BOTH members of every pair
        before surfacing (G1's explicit requirement)."""
        from . import maintenance as maint

        self._require_host("run the integrity ritual")
        blocked: list[dict[str, Any]] = []

        audit_res: dict[str, Any] | None = None
        try:
            audit_res = self.verify_audit()
        except Exception as exc:
            blocked.append(maint.blocked_item(
                "could not verify the audit chain",
                f"{type(exc).__name__}: {exc}",
                "signing key configured (Keychain/env), then re-run integrity"))

        audit_issue: dict[str, Any] | None = None
        if audit_res and audit_res.get("status") not in ("ok", "empty"):
            audit_issue = maint.action_required_item(
                _audit_status_summary(audit_res),
                "chain tamper/break needs human judgment, never auto-repaired",
                "inspect the chain errors; re-link from the last-good entry "
                "(content drift: `brain verify-audit --check-content --json`)",
                str(self.audit.log_path) if self.audit else "audit chain")

        # M-2: `verify()` above only checks linkage + signatures over the
        # entries PRESENT in the log — deleting the tail (never re-signing)
        # still verifies "ok". Folding the off-host anchor check in here is
        # what actually detects a truncated tail (chain_shorter_than_anchor).
        adir = config.anchor_dir()
        if adir is None:
            if audit_issue is None:
                audit_issue = maint.action_required_item(
                    "no off-host anchor configured (BRAIN_ANCHOR_DIR unset)",
                    "verify() alone gives NO tail-truncation guarantee — "
                    "deleting recent audit-log lines still verifies ok",
                    "run `brain anchor --anchor-dir <off-host-dir>` on a "
                    "schedule, then set BRAIN_ANCHOR_DIR so integrity/maintain "
                    "can check it",
                    str(self.audit.log_path) if self.audit else "audit chain")
        else:
            try:
                anchor_res = self.verify_anchor(adir)
            except Exception as exc:
                blocked.append(maint.blocked_item(
                    "could not verify the off-host anchor",
                    f"{type(exc).__name__}: {exc}",
                    "check BRAIN_ANCHOR_DIR is reachable, then re-run integrity"))
            else:
                if anchor_res.get("status") == "divergence":
                    audit_issue = maint.action_required_item(
                        f"audit chain diverges from off-host anchor "
                        f"({len(anchor_res.get('divergences', []))} divergence(s))",
                        "tail truncation or a silent rewrite is possible — "
                        "human judgment, never auto-repaired",
                        "inspect anchor divergences; treat the chain as "
                        "compromised from the first divergent entry_count",
                        anchor_res.get("anchor_log", str(adir)))

        try:
            pairs = self.index.near_dup(min_score=min_score, k=k)
        except Exception as exc:
            pairs = []
            blocked.append(maint.blocked_item(
                "near-dup scan raised",
                f"{type(exc).__name__}: {exc}",
                "investigate the embedder/vector-backend, then re-run integrity"))

        return {
            "ritual": "integrity", "min_score": min_score,
            "audit": audit_res, "audit_issue": audit_issue,
            "near_dup_pairs": pairs,  # UNFILTERED
            "blocked": blocked,
        }

    def promote_scan(self, *, k: int = 50) -> dict[str, Any]:
        """promotion-scan fold (task-disposition.md row 5 — ON-INVOKE triage;
        promotion itself stays a P-10 human gate). Candidates: ``raw/`` zone
        sources not yet promoted into a typed ``brain/`` note. UNFILTERED — the
        CLI egress-gates the candidate list before surfacing."""
        self._require_host("run the promote-scan ritual")
        candidates = self.index.bases_query({"zone": "raw"}, k=k)
        return {
            "ritual": "promote-scan",
            "candidates": candidates,  # UNFILTERED
            "pending_drafts": self._count_pending_drafts(),
        }

    def graphify(
        self, *, force: bool = False, dry_run: bool = False, today: Any = None,
        max_tier: str = classification.VM_DEFAULT_MAX_TIER, candidate_limit: int = 20,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """GRF-01: build the derived, non-authoritative discovery graph
        (ADR-0003 Ruling 6/(a) — supersedes the earlier "documented only"
        disposition). HOST-ONLY: reads the writable index + vectors, writes
        runtime artifacts under ``.brain/graph/``.

        Bounded three ways (Ruling a, ground 2): a corpus-manifest DRIFT GATE
        (``brain.graphify.manifest_unchanged``) skips the rebuild in
        milliseconds when nothing changed (bypass with ``force``); INFERRED
        edges reuse vectors ALREADY in the index (never re-embeds); the
        caller times the build and flags ``action_required`` past the
        5-minute soft budget (target <=60s at the current corpus scale).

        Publication is ATOMIC (HARDENED:codex): the artifact is built and
        schema/cap-validated BEFORE anything touches disk; only a validated
        build replaces the published ``graph.json`` (temp-file + ``os.replace``
        — atomic on POSIX and Windows same-volume). A build that raises, or
        fails validation, writes a SEPARATE ``BUILD_FAILED.json`` marker and
        the published ``graph.json`` (if any) is left completely untouched —
        a partial/failed build is never mistaken for a valid publish.

        Candidate surfacing is egress-gated HERE (before assembly into either
        the CLI's own output or a maintain hot-queue entry) — the same
        doctrine ``graph_expand`` already applies: a withheld note must never
        leak via the graph surface. The full graph.json artifact itself is
        NOT per-item gated (a host-only, gitignored, never-published runtime
        cache — same "egress is the budget, not at-rest" doctrine as the
        writable index and ``.brain/memory/``)."""
        import datetime as _dt
        import json as _json
        import os as _os
        import time as _time

        from . import egress
        from . import graphify as gmod
        from .graph import build_graph
        from .progress import progress_note

        self._require_host("build the graphify discovery graph")
        # [S04 fix 3/4] `today` is threaded EXPLICITLY (the CLI `--as-of` flag,
        # which maintain's bounded child passes) — never from an ambient env
        # var, which would silently leak a stale date into a manual
        # `brain graphify`. A manual run leaves today=None and uses today's date.
        d = today if today is not None else _dt.date.today()
        graph_dir = config.graph_dir(self.vault)
        graph_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = config.graph_manifest_path(self.vault)
        graph_path = config.graph_json_path(self.vault)
        marker_path = config.graph_build_failed_marker_path(self.vault)

        conn = self.index.conn
        new_manifest = gmod.corpus_manifest(conn)

        old_state: dict[str, Any] = {}
        try:
            old_state = _json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old_state = {}

        if not force and gmod.manifest_unchanged(old_state, new_manifest):
            return {
                "ritual": "graphify", "skipped": "unchanged",
                "generation": old_state.get("generation"),
                "built_at": old_state.get("built_at"),
                "note_count": len(new_manifest),
                "published": False,
            }

        t0 = _time.monotonic()
        progress_note(f"graphify: building discovery graph ({len(new_manifest)} notes)...",
                      json_mode=json_mode, verb="graphify")
        try:
            link_graph = build_graph(conn)
            progress_note("graphify: link graph built, computing PageRank + candidates...",
                          json_mode=json_mode, verb="graphify")
            built = gmod.build_graph_artifact(conn, self.index.backend, link_graph, today=d)
        except Exception as exc:
            marker_path.write_text(_json.dumps({
                "status": "build_failed", "error": f"{type(exc).__name__}: {exc}",
                "attempted_at": d.isoformat(),
            }, indent=2), encoding="utf-8")
            # Finding 4 (2026-07-20 dedup batch): a BUILD_FAILED.json marker
            # alone is invisible to anything that doesn't specifically look
            # under .brain/graph/ -- a RAW/manual `brain graphify` invocation
            # (not via `maintain`) never touches maintain-state.json at all,
            # so the failure was otherwise silent to `brain status`/the retro
            # fold. This is a best-effort, ALWAYS-updated record, deliberately
            # separate from maintain()'s own due/streak bookkeeping (`_mark`)
            # so it can never conflict with that branch's monthly-floor logic.
            self._note_graphify_build_outcome(
                status="build_failed", detail=f"{type(exc).__name__}: {exc}",
                attempted_at=d.isoformat())
            return {
                "ritual": "graphify", "status": "build_failed", "published": False,
                "error": f"{type(exc).__name__}: {exc}", "marker": str(marker_path),
            }
        duration = _time.monotonic() - t0
        progress_note(f"graphify: built in {duration:.1f}s", json_mode=json_mode, verb="graphify")

        generation = int(old_state.get("generation") or 0) + 1
        artifact = {
            "schema_version": gmod.GRAPH_SCHEMA_VERSION,
            "generation": generation,
            "built_at": d.isoformat(),
            "authoritative": False,
            "provenance": gmod.PROVENANCE,
            **built,
            "build": {
                "duration_seconds": round(duration, 3),
                "budget_seconds": gmod.DEFAULT_BUDGET_SECONDS,
                "action_required_seconds": gmod.ACTION_REQUIRED_SECONDS,
                "action_required": duration > gmod.ACTION_REQUIRED_SECONDS,
            },
        }

        ok, problems = gmod.validate_artifact(artifact)
        if not ok:
            marker_path.write_text(_json.dumps({
                "status": "invalid_artifact", "problems": problems,
                "attempted_at": d.isoformat(),
            }, indent=2), encoding="utf-8")
            self._note_graphify_build_outcome(
                status="invalid_artifact", detail="; ".join(problems),
                attempted_at=d.isoformat())
            return {
                "ritual": "graphify", "status": "invalid_artifact", "published": False,
                "problems": problems, "marker": str(marker_path),
            }

        candidates = gmod.top_candidates(artifact["edges"], limit=candidate_limit)
        node_lookup = {n["id"]: n for n in artifact["nodes"]}
        touched_ids = {c["from"] for c in candidates} | {c["to"] for c in candidates}
        touched_nodes = [node_lookup[i] for i in touched_ids if i in node_lookup]
        surfaced_nodes, cand_report = egress.apply_gate(touched_nodes, max_tier=max_tier)
        surfaced_ids = {n["id"] for n in surfaced_nodes}
        gated_candidates = [
            c for c in candidates if c["from"] in surfaced_ids and c["to"] in surfaced_ids
        ]

        if dry_run:
            return {
                "ritual": "graphify", "dry_run": True, "published": False,
                "generation": generation, "corpus": artifact["corpus"],
                "build": artifact["build"], "candidates": gated_candidates,
                "egress": cand_report,
            }

        tmp_graph = graph_path.with_suffix(graph_path.suffix + ".tmp")
        tmp_graph.write_text(_json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
        _os.replace(tmp_graph, graph_path)
        marker_path.unlink(missing_ok=True)  # a prior failure marker is now stale

        new_state = {"generation": generation, "built_at": d.isoformat(), "notes": new_manifest}
        tmp_manifest = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        tmp_manifest.write_text(_json.dumps(new_state, indent=2, sort_keys=True), encoding="utf-8")
        _os.replace(tmp_manifest, manifest_path)
        self._note_graphify_build_outcome(status="ok", attempted_at=d.isoformat())

        # Graph explorer regen (part of THIS build, not a separate ritual): the
        # underlying graph.json just changed, so the rendered explorer page is
        # now stale. Best-effort — a render failure must never fail a
        # successful graphify build (mirrors how health_report is wired into
        # `maintain`, core.py's own `_mark`/try-except pattern below).
        try:
            self.graph_report(today=d)
        except Exception:  # noqa: BLE001
            pass

        return {
            "ritual": "graphify", "dry_run": False, "published": True,
            "generation": generation, "path": str(graph_path),
            "corpus": artifact["corpus"], "build": artifact["build"],
            "candidates": gated_candidates, "egress": cand_report,
        }

    def _graph_status(self) -> dict[str, Any]:
        """``brain status`` surfacing of the graphify build's generation/age
        (GRF-02) — reads the SAME manifest ``graphify()`` writes; never
        builds, never mutates."""
        import datetime as _dt
        import json as _json

        try:
            state = _json.loads(config.graph_manifest_path(self.vault).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "never_built"}
        built_at = state.get("built_at")
        age_days = None
        if built_at:
            try:
                age_days = (_dt.date.today() - _dt.date.fromisoformat(built_at)).days
            except ValueError:
                age_days = None
        return {
            "status": "ok", "generation": state.get("generation"),
            "built_at": built_at, "age_days": age_days,
            "note_count": len(state.get("notes") or {}),
        }

    def _run_bounded_graphify(
        self, *, force: bool, dry_run: bool, today: Any, state: dict[str, Any],
        reason: str, builder: Any = None,
    ) -> dict[str, Any]:
        """FRESH-01: run a graphify build IN-PROCESS, attempt-bounded — the ONE
        path both the monthly date-gate FLOOR and the drift-triggered fold
        route through.

        OWNER DECISION 2026-07-11 (SUPERSEDES the earlier
        ``[HARDENED:codex-verify-r1]`` subprocess-wrapper design): the build
        runs via ``self.graphify()`` in-process, against THIS BrainCore's OWN
        index — never a re-invoked ``brain.cli`` child. The subprocess approach
        was reverted after review because it (a) re-resolved its index from the
        environment, so an embedded/injected index built the WRONG corpus and
        unit tests touched the real machine index; (b) needed JSON-over-stdout
        parsing that could latch onto a JSON-shaped noise line; and (c) split
        the ``_graphify_drift`` marker across two processes, causing
        double-counted/clobbered backoff. Its one benefit — a hard KILL of a
        hypothetical C-extension stall — is traded for simplicity and
        correctness: a real corpus builds in ~60s, and the attempt-marker below
        makes a stall non-fatal without a kill.

        Bounding is the ATTEMPT-keyed ``_graphify_drift`` marker + capped
        exponential backoff (``maintenance.graphify_backoff_days``):
        - ``last_attempt`` is persisted BEFORE the build (HARDENED correction
          b), so even a build that hangs (and whose ``maintain`` is later
          killed) leaves an attempt on disk — the next maintain respects the
          cooldown and does NOT re-fire within it.
        - ANY non-publishing, non-skipped, non-preview outcome (an exception, a
          non-dict, or an in-process ``build_failed``/``invalid_artifact``)
          bumps ``consecutive_overruns`` and keeps ``build.action_required`` so
          the alarm layer sees it. ``consecutive_overruns`` resets to 0 only on
          a build that actually publishes or cleanly skips.
        - A dry-run is a PREVIEW: returned as-is (neither pass nor fail for
          backoff); no state is persisted under dry_run.

        ``builder`` is a test-only injection point — a callable
        ``(force, dry_run, today) -> result dict`` standing in for
        ``self.graphify`` so tests drive published/skipped/failed/raising
        outcomes without a real vector build. Defaults to ``self.graphify``
        at the HOST egress default tier (owner ruling 2026-07-10: the host
        default is the full vault — graphify's own signature default is the
        conservative VM tier, which would silently drop hot-queue candidates
        touching Confidential/Restricted/MNPI notes; review finding [1])."""
        if builder is not None:
            build = builder
        else:
            def build(*, force: bool, dry_run: bool, today: Any) -> dict[str, Any]:
                return self.graphify(
                    force=force, dry_run=dry_run, today=today,
                    max_tier=classification.DEFAULT_MAX_TIER)

        # `state` is this process's live dict — the marker in it is
        # authoritative (in-process design; no cross-process copy to re-read).
        marker = dict(state.get("_graphify_drift") or {})
        marker["last_attempt"] = today.isoformat()
        marker["last_reason"] = reason
        state["_graphify_drift"] = marker
        if not dry_run:
            self._save_maintain_state(state)  # ATTEMPT persisted BEFORE the build

        def _bump_and_persist() -> None:
            marker["consecutive_overruns"] = int(marker.get("consecutive_overruns", 0)) + 1
            marker["last_overrun"] = today.isoformat()
            state["_graphify_drift"] = marker
            if not dry_run:
                self._save_maintain_state(state)

        try:
            result = build(force=force, dry_run=dry_run, today=today)
        except Exception as exc:  # noqa: BLE001 — a build error is a failure, never propagate
            _bump_and_persist()
            return {"ritual": "graphify", "invoked": True, "published": False,
                    "reason": reason, "status": "build_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "build": {"action_required": True}}

        if not isinstance(result, dict):
            _bump_and_persist()
            return {"ritual": "graphify", "invoked": True, "published": False,
                    "reason": reason, "status": "bad_result",
                    "build": {"action_required": True}}
        result["invoked"] = True
        result["reason"] = reason

        # A dry-run is a PREVIEW (published False by design): neither pass nor
        # fail for backoff, and no state persisted under dry_run.
        if dry_run or result.get("dry_run"):
            return result
        if result.get("published") or result.get("skipped"):
            marker["consecutive_overruns"] = 0
            marker["last_success"] = today.isoformat()
            state["_graphify_drift"] = marker
            self._save_maintain_state(state)
            return result

        # published False, not skipped, not a preview: an in-process
        # build_failed/invalid_artifact — a failure for backoff/escalation.
        _bump_and_persist()
        result.setdefault("build", {})["action_required"] = True
        result.setdefault("status", "build_not_published")
        return result

    def _run_golden_probe(
        self, *, probes_path: Path, timeout_seconds: int | None = None,
        codex_call: Any = None, self_call: Any = None,
    ) -> dict[str, Any]:
        """WD-03: the Sunday cross-family golden-probe EXECUTION. Codex (the
        family that did NOT build the retrieval engine) shells the SAME
        ``brain`` CLI the probes exercise — this is cross-family EXECUTION of
        a deterministic scorer, correction 5: NEVER "independent
        verification" / "Codex grades retrieval". A shared retrieval bug is
        invisible to both invokers; only the INVOKER differs, not the
        measurement.

        Codex runs READ-ONLY (``--sandbox read-only``) and ONLY executes the
        scorer; it never asserts a decision. Any parse/shape/range failure,
        non-zero codex exit, or timeout falls back to running the probe
        runner directly (subprocess) and returns ``{"runner": "self",
        "degraded": True}`` — NEVER a codex-sourced score from unvalidated
        output (exit-0-with-garbage is the trap this two-stage validation
        exists for).

        ``codex_call``/``self_call`` are test-only injection points —
        ``(argv: list[str], timeout: int) -> (returncode, stdout, stderr)`` —
        standing in for the two subprocess invocations so tests never spawn a
        real codex (or python) child process. Production default shells out
        via ``subprocess.run``."""
        import json as _json
        import shlex as _shlex
        import subprocess as _subprocess
        import sys as _sys

        from . import maintenance as maint

        timeout = timeout_seconds if timeout_seconds is not None else maint.golden_codex_timeout_seconds()
        # PATH-independent brain command (review fix [2]): under launchd's
        # minimal PATH a bare `brain` isn't resolvable, so pin golden_probe to
        # the SAME interpreter running maintain via `-m brain.cli`. Used by
        # BOTH the codex-exec prompt's runner and the self-run fallback below.
        brain_cmd = _shlex.join([_sys.executable, "-m", "brain.cli"])

        def _default_call(argv: list[str], to: int) -> tuple[int, str, str]:
            try:
                proc = _subprocess.run(argv, capture_output=True, text=True, timeout=to)
                return proc.returncode, proc.stdout, proc.stderr
            except _subprocess.TimeoutExpired as exc:
                return -1, "", f"timeout after {to}s: {exc}"
            except OSError as exc:  # e.g. `codex` not on PATH
                return -1, "", f"{type(exc).__name__}: {exc}"

        codex_call = codex_call or _default_call
        self_call = self_call or _default_call

        # Pass the ABSOLUTE interpreter (has `brain` importable) so BOTH the
        # codex prompt's outer `-m brain.golden_probe` AND its inner
        # `--brain-cmd` are PATH-independent (re-review: a bare outer `python3`
        # ModuleNotFound'd on uv/pipx installs).
        prompt = maint.build_codex_golden_prompt(probes_path, Path(self.vault), _sys.executable)
        codex_argv = [
            "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
            "-C", str(self.vault), "--json", prompt,
        ]
        codex_error: str | None = None
        rc, stdout, stderr = codex_call(codex_argv, timeout)
        if rc == 0:
            final_text = maint.parse_codex_final_message(stdout)
            if final_text is None:
                codex_error = "no agent_message event in codex --json stream"
            else:
                try:
                    doc: Any = _json.loads(final_text)
                except ValueError as exc:
                    codex_error = f"final message is not JSON: {exc}"
                    doc = None
                if codex_error is None:
                    shape_err = maint.validate_golden_probe_doc(doc)
                    if shape_err:
                        codex_error = f"invalid golden-probe doc: {shape_err}"
                    else:
                        return {
                            "score": doc.get("score"), "disposition": doc.get("disposition"),
                            "exit_code": doc.get("exit_code"), "runner": "codex", "degraded": False,
                        }
        else:
            codex_error = f"codex exec exited {rc}: {(stderr or stdout or '').strip()[:300]}"

        # -- fall back to the self-run (subprocess, same probes file/vault) --
        self_argv = [
            _sys.executable, "-m", "brain.golden_probe", str(probes_path),
            "--vault", str(self.vault), "--brain-cmd", brain_cmd,
        ]
        rc2, stdout2, stderr2 = self_call(self_argv, timeout)
        try:
            doc2: Any = _json.loads(stdout2)
        except ValueError:
            doc2 = None
        shape_err2 = (maint.validate_golden_probe_doc(doc2) if doc2 is not None
                      else f"non-JSON self-run output (rc={rc2}): "
                           f"{(stderr2 or stdout2 or '').strip()[:300]}")
        if shape_err2:
            return {
                "score": None, "disposition": "transient", "exit_code": maint.GOLDEN_EXIT_TRANSIENT,
                "runner": "self", "degraded": True,
                "error": f"self-run also failed: {shape_err2} (codex: {codex_error})",
            }
        return {
            "score": doc2.get("score"), "disposition": doc2.get("disposition"),
            "exit_code": doc2.get("exit_code"), "runner": "self", "degraded": True,
            "codex_error": codex_error,
        }

    # -- maintain: lock + state-file helpers (ADR-0003 Ruling 5/d, HARDENED:codex) --
    def _acquire_maintain_lock(
        self, lock_path: Path, *, stale_after_seconds: float = 2 * 3600,
    ) -> dict[str, Any] | None:
        """Best-effort single-runner lock. Returns the lock-info dict on
        success, or ``None`` if another live-looking ``maintain`` run holds it
        (caller should skip the run, never block/wait). A lock older than
        ``stale_after_seconds`` — far beyond the ADR's ~60s/5min graphify
        budget — is treated as an abandoned crash and broken automatically."""
        import json as _json
        import os as _os
        import time as _time

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        info = {"pid": _os.getpid(), "started": _time.time()}
        try:
            fd = _os.open(str(lock_path), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
            with _os.fdopen(fd, "w") as fh:
                fh.write(_json.dumps(info))
            return info
        except FileExistsError:
            pass
        existing = self._read_maintain_lock(lock_path)
        started = existing.get("started")
        if isinstance(started, (int, float)) and (_time.time() - started) > stale_after_seconds:
            lock_path.unlink(missing_ok=True)
            return self._acquire_maintain_lock(lock_path, stale_after_seconds=stale_after_seconds)
        return None

    def _read_maintain_lock(self, lock_path: Path) -> dict[str, Any]:
        import json as _json

        try:
            return _json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _release_maintain_lock(self, lock_path: Path) -> None:
        lock_path.unlink(missing_ok=True)

    def _mark_writer_busy(
        self, state: dict[str, Any], branch: str, d: Any, exc: "WriterLockBusy"
    ) -> None:
        """CC-02 skip contract (s02 owns this definition; s05 consumes it).

        A clean writer-busy skip is NOT a failure and NOT work completed:
        - status literal ``skipped-writer-busy``
        - refresh ``last_attempt``; NEVER increment ``consecutive_failures``
        - record ``writer_busy_since`` (first consecutive skip) + holder
          (pid/verb) metadata
        - increment a SEPARATE ``consecutive_skips`` counter
        - NEVER touch ``last_run`` / ``last_successful_index_run`` -- those
          mean work completed, and are what liveness detection keys on.
        """
        prev = state.get(branch) if isinstance(state.get(branch), dict) else {}
        entry = dict(prev)
        entry["last_attempt"] = d.isoformat()
        entry["status"] = "skipped-writer-busy"
        entry["consecutive_skips"] = int(prev.get("consecutive_skips", 0)) + 1
        entry["writer_busy_since"] = prev.get("writer_busy_since") or d.isoformat()
        entry["writer_busy_holder"] = {
            "pid": exc.holder.get("pid"), "verb": exc.holder.get("verb"),
        }
        # Left exactly as they were -- a skip is not a failure.
        entry["consecutive_failures"] = int(prev.get("consecutive_failures", 0))
        state[branch] = entry

    def _load_maintain_state(self) -> dict[str, Any]:
        import json as _json

        path = config.maintain_state_path(self.vault)
        try:
            state = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save_maintain_state(self, state: dict[str, Any]) -> None:
        """Atomic write (tmp + replace) so a crash mid-write never corrupts
        the file the session-start hook and ``brain status`` both read."""
        import json as _json
        import os as _os

        path = config.maintain_state_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(_json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        _os.replace(tmp, path)

    def _note_graphify_build_outcome(
        self, *, status: str, attempted_at: str, detail: str = "",
    ) -> None:
        """Best-effort, ALWAYS-updated record of the graphify build's last
        outcome, persisted into ``.brain/maintain-state.json`` under the
        ``graphify`` entry's ``last_build_*`` keys (finding 4, 2026-07-20
        dedup batch) — deliberately SEPARATE from ``maintain()``'s own
        ``_mark("graphify", ...)`` due/streak bookkeeping (`last_run`,
        `consecutive_failures`, ...), so a raw/manual `brain graphify`
        invocation (never wrapped by `maintain`) still leaves a trace
        `brain status`/the retro fold can see, without ever perturbing
        `maintain`'s own monthly-floor streak logic. Never raises — this is
        observability, not a load-bearing write."""
        try:
            state = self._load_maintain_state()
            prev = state.get("graphify") if isinstance(state.get("graphify"), dict) else {}
            entry = dict(prev)
            entry["last_build_attempt"] = attempted_at
            entry["last_build_status"] = status
            if detail:
                entry["last_build_detail"] = detail
            else:
                entry.pop("last_build_detail", None)
            state["graphify"] = entry
            self._save_maintain_state(state)
        except Exception:  # noqa: BLE001 — observability only, never blocking
            pass

    def _maintain_heartbeat_summary(self, today: Any = None) -> dict[str, Any]:
        """``brain status`` surfacing (HARDENED:premortem) — a heartbeat older
        than 48h on the ``daily`` branch, or any branch with >=2 consecutive
        failures, is flagged. Reads the SAME file `maintain` writes and the
        session-start hook already reads (one file, two consumers)."""
        import datetime as _dt

        state = self._load_maintain_state()
        if not state:
            return {"status": "no-record", "note": "no maintain-state.json yet — brain maintain has not run"}
        today = today or _dt.date.today()
        branches: dict[str, Any] = {}
        stale, repeated_failures = [], []
        for branch, entry in state.items():
            if str(branch).startswith("_"):
                continue  # [S04 fix 7] marker, not a branch (mirrors maintain()'s own filter)
            if not isinstance(entry, dict):
                continue
            last_run = entry.get("last_run")
            age_hours: float | None = None
            if last_run:
                try:
                    age_hours = (today - _dt.date.fromisoformat(last_run)).days * 24
                except ValueError:
                    age_hours = None
            branches[branch] = {
                "last_run": last_run,
                "last_attempt": entry.get("last_attempt"),
                "status": entry.get("status"),
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "age_hours": age_hours,
            }
            if branch == "daily" and (entry.get("failed") or (age_hours is not None and age_hours > 48)):
                stale.append(branch)
            if int(entry.get("consecutive_failures", 0)) >= 2:
                repeated_failures.append(branch)
        overall = "stale" if stale else ("repeated_failures" if repeated_failures else "ok")
        return {
            "status": overall, "stale_branches": stale,
            "repeated_failure_branches": repeated_failures, "branches": branches,
        }

    def _hot_md_path(self) -> Path:
        return config.memory_dir(self.vault) / "hot.md"

    def _append_hot_once(self, key: str, entry_md: str) -> bool:
        """Append ``entry_md`` to ``hot.md`` guarded by an idempotency-key
        HTML comment; a no-op (returns ``False``) if the key is already
        present — the per-branch/per-run-date idempotency guard for every
        scheduled fold that queues a hot-queue entry (HARDENED:codex).

        Two shared gates live HERE so every fold renderer inherits them:
        (1) vault-absolute paths are rewritten vault-relative (retro
        signature ``absolute-paths`` — an absolute path in fold output goes
        stale on a vault move); (2) the idempotency check also scans rotated
        ``archive/hot-*.md`` segments, so an entry rotated out by
        ``_rotate_hot_md`` is never re-appended under the same key."""
        import os as _os
        path = self._hot_md_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for root in {str(self.vault), str(Path(self.vault).resolve())}:
            entry_md = entry_md.replace(root.rstrip(_os.sep) + _os.sep, "")
        marker = f"<!-- idempotency-key: {key} -->"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if marker in existing:
            return False
        # ponytail: linear scan of rotated segments; index them if they ever
        # number in the hundreds.
        for seg in sorted((path.parent / "archive").glob("hot-*.md")):
            try:
                if marker in seg.read_text(encoding="utf-8"):
                    return False
            except OSError:
                continue
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(marker + "\n" + entry_md.rstrip("\n") + "\n\n")
        return True

    def _rotate_hot_md(self, today: Any) -> bool:
        """Rotate aged, resolved hot.md entries into ``archive/hot-<date>.md``
        once the live file exceeds the soft cap (retro signature
        ``hot-md-bloat``) — same auto-rotate posture handoff.md already has.
        Pure judgment lives in ``maintenance.rotate_hot_md``; this method is
        the file I/O. Returns True when a rotation happened."""
        from . import maintenance as maint
        path = self._hot_md_path()
        if not path.exists():
            return False
        kept, rotated = maint.rotate_hot_md(
            path.read_text(encoding="utf-8"), today)
        if not rotated:
            return False
        archive_dir = path.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        seg = archive_dir / f"hot-{today.isoformat()}.md"
        with seg.open("a", encoding="utf-8") as fh:
            fh.write(rotated)
        path.write_text(kept, encoding="utf-8")
        return True

    # -- Tier-2 owner question queue (PUSH redesign, 2026-07-13) --------------
    def _inbox_path(self) -> Path:
        return config.memory_dir(self.vault) / "inbox.jsonl"

    def _read_inbox(self) -> list[dict[str, Any]]:
        from . import inbox as ibx
        p = self._inbox_path()
        return ibx.parse_inbox(p.read_text(encoding="utf-8")) if p.exists() else []

    def _write_inbox(self, entries: list[dict[str, Any]]) -> None:
        from . import inbox as ibx
        p = self._inbox_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ibx.render_inbox(entries), encoding="utf-8")

    def enqueue_question(self, question: dict[str, Any], *,
                         source: str = "", today: Any = None) -> bool:
        """Queue ONE owner-only decision (validated options+default). Idempotent
        on (source, question). Host-only: the queue is host session state under
        ``.brain/`` — the headless synthesis session enqueues, an interactive
        ``/brain-inbox`` answers. Returns True if newly appended."""
        from . import inbox as ibx
        import datetime as _dt
        self._require_host("enqueue an owner question")
        d = today or _dt.date.today()
        entries, appended = ibx.enqueue(
            self._read_inbox(), question, created=d.isoformat(), source=source)
        if appended:
            self._write_inbox(entries)
        return appended

    def answer_question(self, key: str, answer: str, *, today: Any = None,
                        answered_at: str | None = None) -> bool:
        """Record the owner's answer to queued question ``key``. Host-only. The
        answer is CONSUMED (executed through the audited write path) by the next
        fold — recording it here is plain host queue state, not an index write."""
        from . import inbox as ibx
        import datetime as _dt
        self._require_host("answer an owner question")
        d = today or _dt.date.today()
        entries, matched = ibx.record_answer(
            self._read_inbox(), key, answer, answered=d.isoformat(),
            answered_at=answered_at)
        if matched:
            self._write_inbox(entries)
        return matched

    def open_questions(self) -> list[dict[str, Any]]:
        """The open owner-decision queue (host-only read)."""
        from . import inbox as ibx
        self._require_host("read the owner question queue")
        return ibx.open_questions(self._read_inbox())

    # -- COS host-engine capabilities (CUT-01E) ---------------------------
    def cos_propose(self, content: str, *, ident: str | None = None) -> dict[str, Any]:
        """VM-ALLOWED unsigned proposal ingress — writes to the proposal-drop
        dir that ``sync`` NEVER reads. Available on both legs (like
        ``draft_capture``); the broker/owner gate is what makes it safe."""
        from . import cos as cos_mod

        return cos_mod.propose(self.vault, content, ident=ident)

    def cos_propose_correction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """VM-ALLOWED correction drop (verdict-drop/) — see docs/cos-ops.md."""
        from . import cos as cos_mod

        return cos_mod.propose_correction(self.vault, payload)

    def cos_run_begin(self, *, run_id: str | None = None,
                      lane: str | None = None,
                      skill_path: str | None = None,
                      attended: bool = False) -> dict[str, Any]:
        """HOST-ONLY (STA-01): freeze the run manifest at run LAUNCH.

        The manifest — run id, resolved SKILL.md path + content digest, both
        producer versions, the artifacts the run owes — is what later stamps
        that run's candidates. `claim_drops` fires hourly and the deployed
        bundle changes between runs, so reading the version at CLAIM time would
        stamp a proposal with a bundle that did not produce it.

        An EXPLICIT ``run_id`` dated other than today is refused here, at the
        operator entry point rather than in ``write_run_manifest`` (fixtures
        and reconstruction legitimately build fixed-date manifests). A run
        executes under the CALENDAR date, so a manifest dated otherwise splits
        the run's identity: measured 2026-08-06/07, run 74 was begun a day
        early, wrote its corpus under the manifest id and its ledgers under the
        calendar id, and left 50 verified marks permanently unappendable
        because ``host_stamps`` found no manifest at the id the ledgers used.
        """
        from . import cos as cos_mod

        self._require_host("begin a COS run (write the run manifest)")
        if run_id:
            today = cos_mod.utcnow().strftime("%Y-%m-%d")
            if str(run_id)[:10] != today:
                raise ValueError(
                    f"refusing to begin {run_id}: its date is not today "
                    f"({today}). A run executes under the calendar date, so a "
                    f"manifest dated otherwise splits the run's identity — the "
                    f"corpus lands under the manifest id, the ledgers under the "
                    f"calendar id, and the metrics row can then be appended for "
                    f"neither. Begin the run on the day it runs.")
        return cos_mod.write_run_manifest(self.vault, run_id=run_id, lane=lane,
                                          skill_path=skill_path,
                                          attended=attended)

    def cos_corpus_check(self, run_id: str) -> dict[str, Any]:
        """HOST-ONLY (WIR-02): may this run's Phase 1.6 judgment start?

        The judge's input is the message body. This reports how many of the
        run's captured threads actually carry one, and REFUSES
        (:class:`brain.cos_corpus.NoBodiesToJudge`) when none does — the run-65
        shape, where a body pass that never executed still produced 58
        ``no-substance`` verdicts. Judging cannot honestly begin from there.

        A corpus with SOME bodyless rows is normal (an unread thread is never
        opened, and opens are capped) and passes; the count comes back so the
        run states its denominator rather than implying one."""
        from . import cos_corpus as corpus_mod

        self._require_host("check the COS capture corpus before judging")
        rows = corpus_mod.read_corpus(self.vault, run_id)
        _, report = corpus_mod.judgeable(rows, source=f"run {run_id}")
        return report

    def cos_corpus_append(self, run_id: str, rows: list[dict[str, Any]]
                          ) -> dict[str, Any]:
        """HOST-ONLY (WIR-01): save the text the run just READ.

        The nightly extracts a message body, judges it, and throws the text
        away — so re-judging anything costs a 90-minute live run, and a run
        that never read at all is indistinguishable from one that read and
        found nothing. This is the call that makes the reading leave evidence.

        Takes a LIST because the two shapes are different work: the row for an
        opened body is written one at a time, in the same breath as the open,
        while the rows for threads that were enumerated and never opened carry
        no text and so have nothing to lose by going in one batch.
        """
        from . import cos_corpus as corpus_mod

        self._require_host("append to the COS capture corpus")
        written: list[dict[str, Any]] = []
        for row in rows:
            try:
                written.append(corpus_mod.append_thread(self.vault, run_id, **row))
            except corpus_mod.CorpusRefused as exc:
                # NAME WHERE IT STOPPED. The corpus is append-only, so a caller
                # told only "refused" re-sends the whole batch and duplicates
                # every row that already landed.
                raise type(exc)(
                    f"{exc} — {len(written)} of {len(rows)} row(s) were "
                    f"appended before this one; re-send only the rest"
                ) from None
        return {"run": run_id, "appended": len(written),
                "conversation_ids": [w["conversation_id"] for w in written],
                "chars": sum(w["chars"] for w in written)}

    def cos_corpus_close(self, run_id: str) -> dict[str, Any]:
        """HOST-ONLY (WIR-01): close this run's corpus.

        Retention only deletes CLOSED corpora, so an unclosed one is unfiltered
        mail held at rest forever; and a closed corpus carrying ``rows: 0`` is
        how a genuinely quiet night stays distinguishable from a capture stage
        that died."""
        from . import cos_corpus as corpus_mod

        self._require_host("close the COS capture corpus")
        return corpus_mod.close_run(self.vault, run_id)

    def cos_corpus_reopen(self, run_id: str) -> dict[str, Any]:
        """HOST-ONLY: retract a close that certified ZERO rows.

        Run 68 closed its corpus with ``rows: 0`` after a transient browser
        failure, then recovered and opened three real bodies that could never
        be captured. A zero-row close certifies nothing — no denominator, no
        replay scope, no ledger row — so it alone is retractable. A close
        carrying rows is refused here and always will be."""
        from . import cos_corpus as corpus_mod

        self._require_host("reopen the COS capture corpus")
        return corpus_mod.reopen_run(self.vault, run_id)

    def cos_broker_fold(self, *, today: Any = None) -> dict[str, Any]:
        """HOST broker step (wired into ``maintain``): claim + validate drops,
        expire/requeue, consume owner answers (stage ONLY accepted candidates
        into the host-only approved queue), release due holds, enqueue one new
        signed batch, GC. Each stage is independent — one failure never
        aborts the rest."""
        from . import cos as cos_mod
        from . import cos_runverify
        import datetime as _dt

        self._require_host("run the COS broker")
        now = (_dt.datetime.combine(today, _dt.time(3, 0), tzinfo=_dt.timezone.utc)
               if isinstance(today, _dt.date) and not isinstance(today, _dt.datetime)
               else (today or _dt.datetime.now(_dt.timezone.utc)))
        report: dict[str, Any] = {"ritual": "cos-broker", "errors": []}
        cos_mod.ensure_layout(self.vault)
        def _expire_batches() -> list[str]:
            expired = cos_mod.expire_batches(self.vault, now)
            cos_mod.close_expired_batch_questions(self, expired)
            return expired

        for stage, fn in (
            # INS-01 runs FIRST, deliberately: `claim_drops` gates on the run
            # verdict, so scoring in the same pass means a run that just
            # finished has its candidates claimed (or quarantined) one hour
            # after it ends — never "eventually", and never on the run's own
            # say-so. It writes only its own verdict files, so it takes no
            # writer lock; `claim_drops` below still takes it as before.
            ("run_validity", lambda: cos_runverify.verify_pending_runs(
                self.vault, now=now)),
            ("claimed", lambda: cos_mod.claim_drops(self.vault, now)),
            ("batch_expired", _expire_batches),
            ("proposals_expired", lambda: cos_mod.expire_proposals(self.vault, now)),
            ("consumed", lambda: cos_mod.consume_answers(self, now)),
            ("holds_released", lambda: cos_mod.hold_release_due(self.vault, now)),
            ("corrections_asked", lambda: cos_mod.enqueue_correction_questions(self, now)),
            # ING-04: route auto-capture-eligible pending proposals into the
            # hold store BEFORE building the owner batch, so only
            # non-qualifying candidates ever reach the owner.
            ("auto_captured", lambda: cos_mod.auto_capture_fold(self.vault, now)),
            # VER-01/VER-02: deduce version links from email context and stage
            # them as candidates BEFORE the batch is built, so a supersede
            # proposal rides the SAME nightly owner question as ingestion —
            # never a second queue, never a second ritual. Wired here rather
            # than as its own date-gated `maintain` branch because this is the
            # smaller integration: the broker fold already owns the proposal
            # directories, the stage-isolated error handling and the ordering
            # (generate -> batch -> consume) this needs.
            ("version_links", lambda: cos_mod.version_link_fold(self, now)),
            ("batch", lambda: cos_mod.enqueue_batch(self, now)),
            ("gc", lambda: cos_mod.gc_compact(self.vault, now)),
            # SP-01/SP-02: refresh the VM-readable spine-summary.md projection
            # every fold so the brief's LATE+RADAR section is never stale.
            ("spine_rendered", lambda: self.cos_spine_render(now=now)),
            # BAK-01: same lane, same reason — a stale Internal-safe pointer
            # set is worse than none, so it refreshes on every fold too.
            ("grounding_pack", lambda: self.cos_grounding_pack(now=now)),
        ):
            try:
                report[stage] = fn()
            except Exception as exc:  # noqa: BLE001 — stage-isolated, surfaced
                report["errors"].append(f"{stage}: {type(exc).__name__}: {exc}")
        return report

    def cos_ingest_sweep(self, *, downloads_dir: str | Path | None = None,
                         dry_run: bool = False) -> dict[str, Any]:
        """HOST sweeper (v2.1 field bug b): claim VM ingest-manifest lines and
        move exact-filename matches from an explicitly configured dedicated
        host staging dir into ``<vault>/inbox/``. Never defaults to the user's
        shared ``~/Downloads`` directory."""
        from . import cos as cos_mod

        self._require_host("sweep host downloads into the ingest inbox")
        return cos_mod.ingest_sweep(self.vault, downloads_dir=downloads_dir,
                                    dry_run=dry_run)

    def cos_correct(self, round_: int, msg_key: str, bucket: str, tier: str,
                    *, actor: str = "host-cli") -> dict[str, Any]:
        """HOST-only correction of record (append-only correction_events)."""
        from . import cos as cos_mod

        self._require_host("record a COS correction")
        return cos_mod.record_correction(self.vault, round_, msg_key, bucket,
                                         tier, actor=actor)

    def cos_evidence_sign(self, **kwargs: Any) -> dict[str, Any]:
        from . import cos as cos_mod

        self._require_host("sign COS trust-gate evidence")
        return cos_mod.sign_evidence(self.vault, **kwargs)

    def cos_evidence_verify(self, bundle_dir: str | Path) -> dict[str, Any]:
        from . import cos as cos_mod

        self._require_host("verify COS evidence (resolves the signing key)")
        return cos_mod.verify_evidence(bundle_dir)

    def cos_priority_map(self, *, max_tier: str | None = None) -> dict[str, Any]:
        from . import cos as cos_mod

        self._require_host("generate the COS priority map")
        return cos_mod.generate_priority_map(self, max_tier=max_tier)

    def cos_report(self) -> dict[str, Any]:
        """HOST-only shadow-mode calibration report (verdicts × corrections)."""
        from . import cos as cos_mod

        self._require_host("read the COS calibration report")
        return cos_mod.calibration_report(self.vault)

    def cos_hold_add(self, content: str, *, not_before: str,
                     ident: str | None = None) -> dict[str, Any]:
        from . import cos as cos_mod

        self._require_host("add an auto-capture hold")
        return cos_mod.hold_add(self.vault, content, not_before=not_before,
                                ident=ident)

    def cos_hold_list(self) -> list[dict[str, Any]]:
        from . import cos as cos_mod

        self._require_host("list auto-capture holds")
        return cos_mod.hold_list(self.vault)

    def cos_hold_cancel(self, ident: str) -> bool:
        from . import cos as cos_mod

        self._require_host("cancel an auto-capture hold")
        return bool(cos_mod.hold_undo(self.vault, ident, core=self)["undone"])

    def cos_hold_undo(self, ident: str) -> dict[str, Any]:
        """The full undo state machine (held → releasing → capture-pending →
        signed), with the audited-retirement branch available because this
        call has the host core. Demotes the item's category in every branch."""
        from . import cos as cos_mod

        self._require_host("undo an auto-captured item")
        return cos_mod.hold_undo(self.vault, ident, core=self)

    def cos_hold_release_due(self) -> list[str]:
        from . import cos as cos_mod

        self._require_host("release due auto-capture holds")
        return cos_mod.hold_release_due(self.vault)

    def cos_spine_record(self, *, event: str, direction: str | None = None,
                         counterparty: str | None = None, text: str | None = None,
                         topic: str | None = None, due: str | None = None,
                         source_ref: str | None = None, note: str | None = None,
                         commitment_id: str | None = None) -> dict[str, Any]:
        """HOST-only manual/host-sourced spine event (e.g. a calendar
        follow-up or drafts-ledger item spotted outside the ingestion
        pipeline). Ingestion-candidate commitments are recorded automatically
        by ``cos_broker_fold`` on owner acceptance — this verb is for the
        other two named sources (calendar follow-ups, the drafts ledger)."""
        from . import spine as spine_mod

        self._require_host("record a commitment-spine event")
        return spine_mod.record_event(
            self.vault, event=event, direction=direction, counterparty=counterparty,
            text=text, topic=topic, due=due, source_ref=source_ref, note=note,
            commitment_id=commitment_id)

    def cos_spine_radar(self) -> dict[str, Any]:
        from . import spine as spine_mod

        self._require_host("read the commitment-spine radar")
        return spine_mod.radar(self.vault)

    def cos_spine_render(self, *, now: Any = None) -> dict[str, Any]:
        from . import spine as spine_mod

        self._require_host("render the commitment-spine summary")
        return spine_mod.render_spine_summary(self.vault, now)

    def cos_grounding_pack(self, *, now: Any = None) -> dict[str, Any]:
        """BAK-01: render the VM-readable grounding pack — Internal-safe
        POINTERS to documents the VM leg's egress ceiling now hides from it
        (owner ruling 2026-08-10). Same host-writes/VM-reads `shared/` lane as
        `cos_spine_render` above; see `spine.render_grounding_pack`."""
        from . import spine as spine_mod

        self._require_host("render the grounding pack (reads at full tier)")
        return spine_mod.render_grounding_pack(self, now)

    def _engine_feedback_dir(self) -> Path:
        return config.brain_runtime_dir(self.vault) / "engine-feedback"

    def retro(self, *, today: Any = None) -> dict[str, Any]:
        """Retro fold: scan this vault's own maintenance output for engine
        FAILURE SIGNATURES (future dates, absolute-path leakage, duplicate
        findings, future artifacts, hot.md bloat) and write a ready-to-run
        engine-repo prompt into ``.brain/engine-feedback/`` for each. Host-only,
        idempotent (one file per signature per day). Returns the signature ->
        evidence map plus the feedback files written."""
        from . import retro as rmod
        import datetime as _dt
        import re
        self._require_host("run the retro fold")
        d = today or _dt.date.today()

        hot_path = self._hot_md_path()
        hot_text = hot_path.read_text(encoding="utf-8") if hot_path.exists() else ""
        hot_bytes = hot_path.stat().st_size if hot_path.exists() else 0
        findings = rmod.scan(hot_text, config.brief_dir(self.vault), d, hot_md_bytes=hot_bytes)

        written: list[str] = []
        fb_dir = self._engine_feedback_dir()
        # De-dup on the EVIDENCE FINGERPRINT across both the open queue and
        # resolved/ — not on the dated filename. hot.md is append-only, so the
        # entries proving a signature never disappear; keying the file on the
        # run date re-filed an already-fixed, already-resolved defect under a
        # fresh name every run (measured 2026-07-27). A fingerprint already
        # sitting in resolved/ is a defect the operator has closed: never
        # re-open it. Genuinely new evidence yields a new fingerprint and files.
        seen = {
            m.group(1)
            for p in list(fb_dir.glob("*.md")) + list((fb_dir / "resolved").glob("*.md"))
            for m in [re.search(r"-([0-9a-f]{12})\.md$", p.name)] if m
        }
        for signature, evidence in findings.items():
            fp = rmod.evidence_fingerprint(signature, evidence)
            if fp in seen:
                continue
            slug, md = rmod.render_engine_feedback(signature, evidence, d)
            fpath = fb_dir / f"{slug}.md"
            if not fpath.exists():
                fb_dir.mkdir(parents=True, exist_ok=True)
                fpath.write_text(md, encoding="utf-8")
                written.append(fpath.name)
                seen.add(fp)
        return {"date": d.isoformat(), "findings": findings, "feedback_written": written}

    def pending_engine_feedback(self) -> list[str]:
        """Filenames of engine-feedback prompts waiting to be fired at the repo
        (surfaced by the SessionStart hook)."""
        fb_dir = self._engine_feedback_dir()
        return sorted(f.name for f in fb_dir.glob("*.md")) if fb_dir.is_dir() else []

    def _daily_note_fold(self, today: Any, brief_result: Any = None) -> dict[str, Any]:
        """Daily fold: create today's ``type: daily`` note exactly once per day.

        Second-brain parity with an old daily-note habit, done the
        native way — a dated note with the standard template sections, seeded
        from the morning brief already built this run. Idempotent via
        ``self.get`` (never a second copy). Host verb: ``capture`` signs +
        indexes. Defaults to Confidential (a personal work log carries deal
        detail, matching the Daily-zone migration floor)."""
        note_id = f"daily-{today.isoformat()}"
        if self.get(note_id) is not None:
            return {"created": False, "id": note_id}
        lines = [f"# {today.isoformat()} ({today.strftime('%A')})", "", "## Session Summary"]
        try:
            for n in (brief_result or {}).get("recent_notes") or []:
                title = (n.get("title") or n.get("id") or "").strip() if isinstance(n, dict) else str(n).strip()
                if title:
                    lines.append(f"- {title}")
        except Exception:
            pass  # seeding is best-effort; the empty note is still valid
        lines += ["", "## Work Done", "", "## Open Threads", "", "## Next Session", ""]
        # LNK-02: dailies are born CHAINED — link yesterday's note when it
        # exists, so machine-created dailies stop accruing one wikilink-orphan
        # per day (field lesson 2026-07-21: 13 of the vault's 20 knowledge-layer
        # orphans were exactly these). Only a resolvable link: the dangling
        # target metric is an absolute-zero convention.
        import datetime as _dt
        prev_id = f"daily-{(today - _dt.timedelta(days=1)).isoformat()}"
        if self.get(prev_id) is not None:
            lines += ["## Related", f"- [[{prev_id}]]", ""]
        body = "\n".join(lines).rstrip() + "\n"
        self.capture(body, note_id=note_id, note_type="daily",
                     classification="Confidential",
                     reason="brain-nightly daily-note fold")
        return {"created": True, "id": note_id}

    def _recommendations_aging_fold(self, today: Any) -> dict[str, Any]:
        """MEM-03 unconditional daily fold: surface any open recommendation
        older than the aging threshold into ``hot.md``, exactly once per
        recommendation (idempotent both at the JSONL-status level and via the
        hot.md idempotency key)."""
        from . import maintenance as maint

        open_path = config.recommendations_open_path(self.vault)
        if not open_path.exists():
            return {"scanned": 0, "surfaced": 0, "appended_to_hot": 0}

        entries = maint.parse_recommendation_lines(open_path.read_text(encoding="utf-8"))
        updated, newly = maint.recommendations_aging_scan(entries, today)
        appended = 0
        for entry in newly:
            key = f"rec:{entry.get('id')}"
            entry_md = maint.render_recommendation_hot_entry(entry, today)
            if self._append_hot_once(key, entry_md):
                appended += 1
        if newly:
            open_path.write_text(maint.render_recommendation_lines(updated), encoding="utf-8")
        return {"scanned": len(entries), "surfaced": len(newly), "appended_to_hot": appended}

    def _maybe_auto_update(self, today: Any) -> dict[str, Any]:
        """Hourly auto-apply of a newer engine version (owner decision
        2026-07-25). Safety rails: attempt-once-per-version (a failed version
        is never auto-retried until a manual `brain update` or a NEWER version
        appears), writer-lock aware (defers cleanly if a hand-run rebuild holds
        the single-writer lock), and post-update VERIFY as the gate
        (``run_update`` returns ok only when after-doctor is all-current AND a
        real query embed passes). Writes ``~/.brainiac/update-state.json`` for
        the session-start banner. Never raises past its own try in
        ``maintain`` — the caller wraps it too."""
        import os as _os
        from pathlib import Path as _Path

        from . import update as brain_update

        # Auto-apply is a SCHEDULED-TASK behavior only: the hourly brain-nightly
        # runner (scripts/brain-brief.sh) sets BRAIN_AUTO_UPDATE=1. A manual
        # `brain maintain`, an interactive session, and the test suite must NEVER
        # trigger an unattended pip upgrade / plugin reinstall of the machine —
        # so default OFF and require the explicit opt-in the scheduler provides.
        # KILL SWITCH, checked first (2026-08-07). Unattended auto-apply is an
        # ACCEPTED RISK, not an oversight -- see docs/adr/0005-update-versioning-ux.md
        # "Risk acceptance" for what is being accepted and why. But an acceptance
        # you cannot revoke is not an acceptance: `scripts/brain-brief.sh` sets
        # BRAIN_AUTO_UPDATE=1 INLINE, so an inherited 0 cannot switch it off, and
        # that runner is a shipped file the next update overwrites. This is the
        # one way to turn auto-apply off without editing shipped files or
        # enabling full BRAIN_MANAGED lockdown -- set it in the environment the
        # scheduled task runs in (launchd plist / cron). Default off: setting
        # nothing changes nothing.
        if _os.environ.get("BRAIN_NO_AUTO_UPDATE", "").strip().lower() in (
                "1", "true", "yes", "on"):
            return {"auto_update": "disabled", "reason": "BRAIN_NO_AUTO_UPDATE set"}
        if _os.environ.get("BRAIN_AUTO_UPDATE") != "1":
            return {"auto_update": "disabled", "reason": "BRAIN_AUTO_UPDATE!=1 (scheduled-task only)"}

        brainiac_home = _Path(_os.environ.get("BRAINIAC_HOME", _Path.home() / ".brainiac"))
        if config.is_managed():
            return {"auto_update": "skipped", "reason": "managed endpoint"}

        info = brain_update.detect_and_check_update(brainiac_home)
        prev = brain_update.read_update_state(brainiac_home)
        latest = info.get("latest")
        installed = info.get("installed")

        # A FAILED record for a version this machine has SINCE REACHED is moot,
        # and nothing else cleared it — the 0.20.4 `claude`-not-on-PATH failure
        # of 2026-08-07 nagged in every session for six days after 0.20.6 was
        # installed and the cause fixed. Cleared before the branches below, so
        # it dies on the same run whatever the availability check says.
        if (prev and prev.get("status") == "failed"
                and brain_update.failure_is_moot(prev, installed)):
            try:
                brain_update.update_state_path(brainiac_home).unlink()
            except FileNotFoundError:
                pass
            prev = None

        if not info.get("available"):
            # A previously-DEFERRED "available" nag that is no longer available
            # (applied manually, or superseded) gets cleared so the banner stops.
            if prev and prev.get("status") == "available":
                try:
                    brain_update.update_state_path(brainiac_home).unlink()
                except FileNotFoundError:
                    pass
            return {"auto_update": "none", "installed": installed, "latest": latest}

        # attempt-once-per-version: never auto-retry a version that already failed.
        if prev and prev.get("status") == "failed" and prev.get("latest") == latest:
            return {"auto_update": "skipped", "reason": "version already failed", "latest": latest}

        # writer-lock aware: if a hand-run rebuild/sync holds the single-writer
        # lock, defer (mark 'available') and let next hour retry — same posture
        # as the daily branch's skipped-writer-busy.
        from .lock import WriterLockBusy, vault_writer_lock
        try:
            with vault_writer_lock(self.vault, verb="update-probe", timeout=0.1):
                pass
        except WriterLockBusy as exc:
            brain_update.write_update_state(
                brainiac_home, status="available", installed=installed, latest=latest,
                source=info.get("source"), at=today.isoformat(),
                detail=f"deferred — writer busy (pid={exc.holder.get('pid')})")
            return {"auto_update": "deferred", "reason": "writer busy", "latest": latest}

        report = brain_update.run_update(brainiac_home=brainiac_home)
        if report.get("ok"):
            brain_update.write_update_state(
                brainiac_home, status="applied", installed=installed, latest=latest,
                source=info.get("source"), at=today.isoformat(),
                detail="auto-applied by brain-nightly")
            return {"auto_update": "applied", "latest": latest}
        # post-update verify is the gate: run_update already ran after-doctor +
        # a real query embed; a non-ok result means a step failed. Record it
        # loudly (banner) and DON'T auto-retry this version.
        brain_update.write_update_state(
            brainiac_home, status="failed", installed=installed, latest=latest,
            source=info.get("source"), at=today.isoformat(),
            detail=(report.get("notes") or "update pipeline reported not-ok")[:200])
        return {"auto_update": "failed", "latest": latest, "notes": report.get("notes")}

    def maintain(
        self, *, dry_run: bool = False, today: Any = None,
        min_score: float = 0.95, near_dup_k: int = 5,
        graphify_runner: Any = None, golden_runner: Any = None,
    ) -> dict[str, Any]:
        """The umbrella — THE single sanctioned host task (``brain-nightly``,
        persistence-budget.md THE LOCK). Runs ``sync --publish`` + ``brief`` +
        the recommendations-aging fold (skipped under ``dry_run`` — no
        mutation, no signing), then the date-gated branches: Mon->health,
        Tue->integrity, Sun->digest (+curate's stale-link/revisit scan
        +promote-scan, AUT-02), 1st-of-month->graphify (ADR-0003 Ruling 6/(a):
        a REAL, bounded graph build — drift-gated, embedding-reuse, wall-clock
        budgeted; this SUPERSEDES the earlier "documented only" disposition).
        ``health``/``integrity`` are READ-ONLY by construction, so they run
        for REAL even under ``--dry-run`` — only the mutating/signing half is
        skipped.

        ADR-0003 Ruling d/HARDENED:codex: a single-runner lock skips (never
        blocks) a concurrent run; branch due-ness reads
        ``.brain/maintain-state.json`` (due-since-last-run catch-up, not
        calendar-day-only); each branch runs in its own try/except so one
        crash never aborts the rest of the run, and a branch's marker only
        advances to ``today`` on SUCCESS — a crash leaves it due next time,
        safely, because every branch (and every hot-queue write) is
        idempotent. HOST-broker.

        FRESH-01 (2026-07-11): the 1st-of-month graphify date-gate is a
        FLOOR, not a gate — the daily fold ALSO measures corpus drift since
        the last build (``maintenance.graphify_drift``) and fires the same
        bounded build early once drift crosses ``BRAIN_GRAPHIFY_DRIFT_PCT``
        (default 15%) and its own attempt-keyed cooldown
        (``BRAIN_GRAPHIFY_COOLDOWN_DAYS``, default 2 days, capped
        exponential backoff on overruns) has elapsed. Both the monthly floor
        and the drift trigger execute through ONE attempt-bounded path
        (``_run_bounded_graphify``) that runs ``self.graphify()`` IN-PROCESS
        against this BrainCore's own index (owner decision 2026-07-11,
        superseding the earlier subprocess wrapper — see
        ``_run_bounded_graphify``). ``graphify_runner`` is test-only dependency
        injection: a ``(force, dry_run, today) -> result dict`` builder that
        stands in for ``self.graphify`` — production leaves it ``None``.

        WD-03 (2026-07-12): Sun->golden — cross-family EXECUTION (never
        "verification") of the WD-02 golden-probe scorer via ``codex exec``
        (read-only, validated, self-run-fallback on any failure — see
        ``_run_golden_probe``), gated by its own ``_golden_attempt``
        next-retry marker so a transient failure backs off instead of
        re-invoking codex every hourly run. ``golden_runner`` is test-only
        dependency injection: a ``(probes_path) -> result dict`` callable
        standing in for ``self._run_golden_probe`` — production leaves it
        ``None``.

        ENF-01 round 3 (2026-08-10): a pending supersede crash journal is
        checked and recovered ONCE, here, before any branch runs. It used to be
        reached only from inside ``core.supersede`` — i.e. only if some fold
        actually got as far as calling it — and ``auto_dedup_tier1`` refuses a
        sub-floor pair BEFORE that call. So an interrupted repair of the exact
        failed-OCR family ENF-01 is about reported an ordinary
        ``skipped_short_body`` and left the half-chain and its journal on disk
        indefinitely (reproduced: ``{"retired":[], "skipped_short_body":1,
        "journal_still_exists":true}``). A blocked journal is a vault-wide
        refusal; it belongs at the top of the run, not behind a filter."""
        from . import maintenance as maint
        import datetime as _dt
        import os as _os

        self._require_host("run the maintain umbrella")
        d = today or _dt.date.today()

        lock_path = config.maintain_lock_path(self.vault)
        lock_info = self._acquire_maintain_lock(lock_path)
        if lock_info is None:
            held = self._read_maintain_lock(lock_path)
            return {
                "ritual": "maintain", "dry_run": dry_run, "date": d.isoformat(),
                "skipped": "locked",
                "note": f"another maintain run holds the lock (pid={held.get('pid')}, "
                        f"started={held.get('started')}) — skipping this run",
                "outcomes": maint.build_outcomes(),
            }

        try:
            state = self._load_maintain_state()
            last_runs = {
                k: (v.get("last_run") if isinstance(v, dict) else v)
                for k, v in state.items() if not str(k).startswith("_")
            }
            branches = maint.maintain_branches(d, last_runs=last_runs)

            results: dict[str, Any] = {}
            auto_fixed: list[dict[str, Any]] = []
            action_required: list[dict[str, Any]] = []
            blocked: list[dict[str, Any]] = []

            def _mark(branch: str, ok: bool, error: str | None = None) -> None:
                if dry_run:
                    return
                prev = state.get(branch) if isinstance(state.get(branch), dict) else {}
                entry = dict(prev)
                entry["last_attempt"] = d.isoformat()
                if ok:
                    entry["last_run"] = d.isoformat()
                    entry["status"] = "ok"
                    entry["failed"] = False
                    entry["consecutive_failures"] = 0
                    entry.pop("error", None)
                    # A real completed run clears any writer-busy skip streak.
                    entry["consecutive_skips"] = 0
                    entry.pop("writer_busy_since", None)
                    entry.pop("writer_busy_holder", None)
                else:
                    entry["status"] = "failed"
                    entry["failed"] = True
                    entry["consecutive_failures"] = int(prev.get("consecutive_failures", 0)) + 1
                    entry["error"] = error
                state[branch] = entry

            # -- pending supersede journal, FIRST (ENF-01 round 3) ----------
            # Before candidate scanning and before every floor/filter branch:
            # an unfinished supersede blocks all of them, and the fold that
            # would otherwise have surfaced it filters the very family this
            # guard exists for. Read-only under --dry-run.
            try:
                jres = self.recover_pending_supersede(dry_run=dry_run)
                if jres:
                    results["supersede_journal"] = jres
                    if jres.get("restored"):
                        auto_fixed.append(maint.auto_fixed_item(
                            "supersede-journal", jres.get("journal", ""),
                            f"rolled back an interrupted {jres.get('op')} "
                            f"({', '.join(jres['restored'])} side(s) restored)"))
                    elif jres.get("pending"):
                        action_required.append(maint.action_required_item(
                            "a supersede crash journal is pending",
                            "--dry-run never writes, so it was reported not recovered",
                            "re-run `brain maintain` without --dry-run",
                            jres.get("journal", "")))
            except SupersedeJournalUnreadable as exc:
                blocked.append(maint.blocked_item(
                    f"supersede crash journal unreadable: {exc}",
                    "the two notes it names + the audit log",
                    "a human repairing the pair and deleting the journal"))
            except Exception as exc:  # noqa: BLE001 — never abort the run
                blocked.append(maint.blocked_item(
                    f"supersede journal preflight failed: {exc}",
                    "index/write path", "next maintain run"))

            # -- unconditional daily work (sync/drain/publish/brief + recs) --
            if dry_run:
                results["status"] = self.status()
            else:
                # Field bug 1 self-heal: reap any future-dated brief/digest
                # HTML (a `--date <future>` leak shadows the real day's
                # artifact). Cheap, id-free, safe — derived files only.
                reaped = maint.reap_future_dated_artifacts(config.brief_dir(self.vault), d)
                if reaped:
                    auto_fixed.append(maint.auto_fixed_item(
                        "reap-future-artifacts", "brief/",
                        f"removed {len(reaped)} future-dated brief/digest file(s): "
                        f"{', '.join(reaped)}"))

                # WSP-01 workspace sweep — BEFORE sync, so the ingest drain
                # inside sync picks up what the sweep just staged (settled
                # workspace files gain their lifecycle in the same nightly:
                # sweep -> inbox -> ingest -> raw/ -> index+embed -> snapshot).
                # No-op unless $BRAIN_WORKSPACE_SWEEP_DIRS is configured.
                sweep_dirs, sweep_age = maint.workspace_sweep_config()
                if sweep_dirs:
                    try:
                        sweep_res = maint.sweep_workspace(
                            sweep_dirs, self.vault / "inbox", sweep_age)
                        results["workspace_sweep"] = sweep_res
                        if sweep_res["swept"]:
                            auto_fixed.append(maint.auto_fixed_item(
                                "workspace-sweep", str(self.vault / "inbox"),
                                f"swept {len(sweep_res['swept'])} settled "
                                f"workspace file(s) into inbox/ "
                                f"(age>{sweep_age}d)"))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"workspace sweep failed: {exc}",
                            "filesystem", "next maintain run"))
                # DOC-01: the sweep runs BEFORE the broker fold so a swept
                # attachment reaches its host-private quarantine in time to
                # join THIS run's owner batch — documents and email text are
                # decided in the same question, not an hour apart.
                # PRV-10: drain pending new-vault provision requests written
                # by a Cowork session (VM-request → host-drain, owner ruling
                # 2026-08-16: automatic, loudly reported). Rides the hourly
                # daily branch instead of a new scheduled task (AGENTS.md §6).
                # Cheap no-op scan when no request is pending.
                try:
                    from . import provision as _provision
                    _provision.maintain_fold(
                        results, auto_fixed, action_required)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        f"provision drain failed: {exc}",
                        "workspace registry / filesystem", "next maintain run"))

                try:
                    sweep_res = self.cos_ingest_sweep()
                    results["cos_ingest_sweep"] = sweep_res
                    if sweep_res.get("moved"):
                        auto_fixed.append(maint.auto_fixed_item(
                            "cos-ingest-sweep", str(self.vault),
                            f"quarantined {len(sweep_res['moved'])} manifest-named "
                            f"download(s) for an owner verdict"))
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        f"COS ingest sweep failed: {exc}",
                        "downloads dir / cos ops dir", "next maintain run"))
                # CUT-01E: the COS broker step runs BEFORE the first sync so
                # broker-accepted candidates and released holds land in the
                # approved queue in time for THIS run's drain to sign them —
                # a VM cos-propose drop becomes a queued owner-inbox batch
                # within one nightly interval, never "eventually".
                try:
                    cos_res = self.cos_broker_fold(today=d)
                    results["cos_broker"] = cos_res
                    if cos_res.get("claimed", {}).get("claimed"):
                        auto_fixed.append(maint.auto_fixed_item(
                            "cos-broker", str(self.vault),
                            f"claimed {len(cos_res['claimed']['claimed'])} COS "
                            f"proposal drop(s) for owner review"))
                    if cos_res.get("batch", {}).get("enqueued"):
                        auto_fixed.append(maint.auto_fixed_item(
                            "cos-broker", "owner inbox",
                            f"queued COS ingestion batch "
                            f"{cos_res['batch']['batch_id']} "
                            f"({len(cos_res['batch']['candidates'])} candidate(s))"))
                    # INS-01: a run the host validator could not certify is a
                    # hot.md LOG line (§9) as well as a `brain status` / brief
                    # warning — keyed on the run ids + verdicts so a standing
                    # failure is not re-reported every hour, and a NEW one is.
                    from . import cos as _cos, cos_runverify as _crv
                    not_ok = [s for s in (cos_res.get("run_validity") or {})
                              .get("scored", [])
                              if s.get("verdict") not in _cos.CLAIMABLE_VERDICTS]
                    if not_ok:
                        self._append_hot_once(
                            "maintain:cos-run-invalid:" + ",".join(
                                f"{s['run_id']}={s['verdict']}"
                                for s in sorted(not_ok, key=lambda x: x["run_id"])),
                            _crv.hot_entry(not_ok, d.isoformat()),
                        )
                    waiting = (cos_res.get("batch", {}) or {}).get("waiting") or []
                    if waiting:
                        # Backpressure (ing-02) is correct, but SILENT
                        # backpressure is not — proposals queued behind an
                        # unanswered batch were invisible to the owner for two
                        # days (measured 2026-07-27). hot.md is the LOG (§9):
                        # keyed on the DISTINCT waiting-id set, so an unchanged
                        # queue isn't re-reported every hour.
                        self._append_hot_once(
                            "maintain:cos-broker-waiting:"
                            + maint.promote_scan_finding_key(
                                [{"id": i} for i in waiting]),
                            maint.render_cos_waiting_hot_entry(waiting, d),
                        )
                    consumed = cos_res.get("consumed", {}) or {}
                    applied_links = consumed.get("supersedes_applied") or []
                    # A version link is accepted but never signed into
                    # the approved queue — it retires a note in place — so it is
                    # counted separately rather than mis-reported as a capture.
                    n_captured = len(consumed.get("accepted") or []) - len(applied_links)
                    if n_captured > 0:
                        from . import cos as _cos_q
                        auto_fixed.append(maint.auto_fixed_item(
                            "cos-broker", str(_cos_q.approved_queue_dir(self.vault)),
                            f"moved {n_captured} owner-accepted candidate(s) into "
                            f"the host-only approved queue for signing"))
                    if applied_links:
                        auto_fixed.append(maint.auto_fixed_item(
                            "version-link", str(self.vault),
                            f"applied {len(applied_links)} owner-accepted "
                            f"supersede proposal(s) deduced from email context"))
                    # CUR-01: the currency layer's own coverage number, from
                    # the fold that just ran. Persisted so `health-report` can
                    # show it and so a run that stopped producing it is
                    # visible as a STALE number rather than as nothing.
                    cov = (cos_res.get("version_links") or {}).get("coverage")
                    if isinstance(cov, dict):
                        prev_daily = state.get("daily") if isinstance(
                            state.get("daily"), dict) else {}
                        state["daily"] = {**prev_daily,
                                          "curated_coverage": dict(cov)}
                    if cos_res.get("holds_released"):
                        from . import cos as _cos_q
                        auto_fixed.append(maint.auto_fixed_item(
                            "cos-broker", str(_cos_q.approved_queue_dir(self.vault)),
                            f"released {len(cos_res['holds_released'])} due "
                            f"auto-capture hold(s)"))
                    for err in cos_res.get("errors", []):
                        blocked.append(maint.blocked_item(
                            f"COS broker stage failed: {err}",
                            "cos ops dir / owner inbox / signing key",
                            "next maintain run"))
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        f"COS broker fold failed: {exc}",
                        "cos ops dir", "next maintain run"))
                try:
                    # First pass WITHOUT publish: the self-organization folds
                    # below mutate metadata/paths, and the snapshot must carry
                    # their result — publish happens in the second pass.
                    sync_res = self.sync(drain=True, publish=False)
                    results["sync"] = sync_res
                    added = sync_res.get("added", 0)
                    updated = sync_res.get("updated", 0)
                    deleted = sync_res.get("deleted", 0)
                    rebased = sync_res.get("rebased", 0)
                    if added or updated or deleted or rebased:
                        reb = f" ={rebased} path-rebased (move, no re-embed)" if rebased else ""
                        auto_fixed.append(maint.auto_fixed_item(
                            "sync", str(self.vault),
                            f"index reconciled +{added} ~{updated} -{deleted}{reb}"))
                    drain = sync_res.get("drain", {}) or {}
                    if drain.get("promoted"):
                        auto_fixed.append(maint.auto_fixed_item(
                            "drain", str(self.capture_inbox_dir()),
                            f"drained {drain['promoted']} pending capture(s)"))

                    # A quarantined drop is a document the owner MEANT to
                    # ingest and did not get — reported the run it happens
                    # (see `ingest_quarantine_findings` for why neither the
                    # trend metric nor the monthly triage can catch it).
                    action_required.extend(maint.ingest_quarantine_findings(
                        sync_res.get("ingest", {}), Path(self.vault)))

                    # -- self-organization folds (owner decision 2026-07-11:
                    # metadata, versioning, PARA and navigation are automatic,
                    # never user-gated). Each fold is independent — one
                    # failure never aborts the others or the publish.
                    try:
                        vres = maint.auto_version_chains(self)
                        results["version_chains"] = vres
                        if vres["chained"]:
                            auto_fixed.append(maint.auto_fixed_item(
                                "version-chain", str(self.vault),
                                f"stamped {len(vres['chained'])} supersession "
                                f"link(s) across explicit version families"))
                        for fam in vres["skipped_conflict"]:
                            action_required.append(maint.action_required_item(
                                f"version family '{fam}' has a manual chain that "
                                f"disagrees with the computed order",
                                "auto-chaining never overrides a human supersede",
                                "inspect the family and fix the chain with "
                                "`brain supersede` if the manual link is wrong",
                                fam))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"auto version-chain fold failed: {exc}",
                            "index/write path", "next maintain run"))
                    try:
                        ddp_res = maint.auto_dedup_tier1(self)
                        results["autodedup"] = ddp_res
                        prev_daily = state.get("daily") if isinstance(
                            state.get("daily"), dict) else {}
                        state["daily"] = {
                            **prev_daily,
                            "autodedup_retired": len(ddp_res["retired"]),
                            "autodedup_skipped_short_body": len(
                                ddp_res.get("skipped_short_body", [])),
                            "autodedup_skipped_classification": len(ddp_res["skipped_classification"]),
                            "autodedup_skipped_recurring": len(ddp_res["skipped_recurring"]),
                            "autodedup_skipped_trust": len(ddp_res["skipped_trust"]),
                        }
                        if ddp_res["retired"]:
                            auto_fixed.append(maint.auto_fixed_item(
                                "auto-dedup", str(self.vault),
                                f"auto-superseded {len(ddp_res['retired'])} "
                                f"sha256-identical duplicate pair(s) (DDP-01)"))
                        if ddp_res["skipped_classification"]:
                            action_required.append(maint.action_required_item(
                                f"{len(ddp_res['skipped_classification'])} sha256-identical "
                                f"pair(s) span different classifications",
                                "classification decisions are never automated",
                                "review the pair and `brain supersede` by hand if the "
                                "duplicate really is retired content",
                                "auto-dedup"))
                        if ddp_res["skipped_trust"]:
                            action_required.append(maint.action_required_item(
                                f"{len(ddp_res['skipped_trust'])} sha256-identical "
                                f"pair(s) span different trust levels (one side is a "
                                f"draft/untrusted-provenance note)",
                                "an untrusted draft must never automatically retire a "
                                "trusted note (codex 2026-07-22)",
                                "review the pair and `brain supersede` by hand if the "
                                "duplicate really is retired content",
                                "auto-dedup"))
                        if not dry_run and (ddp_res["retired"] or ddp_res["truncated"]):
                            try:
                                self._append_hot_once(
                                    f"maintain:autodedup:{d.isoformat()}:"
                                    f"{len(ddp_res['retired'])}:{ddp_res['truncated']}",
                                    maint.render_autodedup_hot_entry(ddp_res, d),
                                )
                            except Exception as hot_exc:  # noqa: BLE001
                                action_required.append(maint.action_required_item(
                                    "auto-dedup hot-queue entry could not be written",
                                    f"{type(hot_exc).__name__}: {hot_exc}",
                                    "check .brain/memory/hot.md writability; the "
                                    "dedup pass itself completed fine",
                                    "auto-dedup"))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"auto-dedup fold failed: {exc}",
                            "index/write path", "next maintain run"))
                    try:
                        pres = maint.auto_para(Path(self.vault))
                        results["auto_para"] = pres
                        if pres["moved"]:
                            auto_fixed.append(maint.auto_fixed_item(
                                "auto-para", str(Path(self.vault) / "brain"),
                                f"filed {len(pres['moved'])} note(s) into their "
                                f"PARA zone by metadata"))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"auto-PARA fold failed: {exc}",
                            "filesystem", "next maintain run"))
                    try:
                        nres = maint.refresh_navigation(Path(self.vault))
                        results["navigation"] = nres
                        auto_fixed.append(maint.auto_fixed_item(
                            "navigation", str(Path(self.vault) / "brain"),
                            f"regenerated backlinks ({nres['backlink_targets']} "
                            f"targets) + {len(nres['catalog_counts'])} zone catalogs"))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"navigation refresh failed: {exc}",
                            "filesystem", "next maintain run"))

                    # LNK-03b: cheap DAILY knowledge-layer orphan counter
                    # (not Wednesday-gated like the full `graph_hygiene`
                    # branch below — measured ~11ms on a 2.5k-note synthetic
                    # index, so the full metrics call runs every day rather
                    # than needing a lighter carve-out). Persists `kl_orphans`
                    # into state["daily"] + this run's health-history row
                    # (maintenance.collect_health_metrics reads
                    # results["kl_orphans"]); a SUSTAINED multi-day climb
                    # (>= $BRAIN_ORPHAN_SUSTAINED_GROWTH over the trailing 7
                    # RECORDED days) logs one hot.md line, idempotency-keyed
                    # per ISO week so it fires at most once even if the
                    # condition holds every day of that week. Never on the
                    # first runs (no 7-day baseline yet). The weekly
                    # `graph_hygiene` branch's own single-prior-run growth
                    # alarm stays fully independent.
                    try:
                        from . import graph as graph_mod

                        kl_metrics = graph_mod.graph_hygiene_metrics(self.index.conn)
                        kl_count = kl_metrics.get("orphan_count", 0)
                        results["kl_orphans"] = {"orphan_count": kl_count}
                        prev_daily = state.get("daily") if isinstance(
                            state.get("daily"), dict) else {}
                        state["daily"] = {**prev_daily, "kl_orphans": kl_count}
                        if not dry_run:
                            history_before = maint.read_health_history(Path(self.vault))
                            growth = maint.kl_orphan_sustained_growth(history_before, kl_count)
                            if maint.should_alert_kl_orphan_sustained_growth(growth):
                                iso = d.isocalendar()
                                self._append_hot_once(
                                    f"maintain:kl-orphans-sustained:{iso[0]}-W{iso[1]:02d}",
                                    maint.render_kl_orphan_sustained_growth_hot_entry(
                                        kl_count, growth, d),
                                )
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"kl_orphans daily counter failed: {exc}",
                            "index read", "next maintain run"))

                    # WAT-01: the corpus-invariants watchdog — four cheap
                    # read-only counts (unlinked raw sources, cross-tier
                    # name-twins, sub-floor supersession families, and the
                    # READ-not-recomputed unreachable-gold count), measured
                    # ~1.7s on the 2,581-note reference vault. Thresholds
                    # RATCHET off the best value ever recorded rather than
                    # trending a percentage, so the same rule alerts from a
                    # 2,132 baseline and from a zero one (see
                    # `brain.invariants`). It writes its own state row
                    # (`corpus_invariants`) through `_mark`, which is what
                    # gives the dead-man's switch — a stale or missing row —
                    # something to key on from doctor/health-report and the
                    # notification lane.
                    try:
                        from . import invariants as inv_mod

                        inv_metrics = inv_mod.corpus_invariants(
                            self.index.conn, Path(self.vault))
                        inv_values = inv_mod.metric_values(inv_metrics)
                        prev_inv = state.get(inv_mod.STATE_KEY) if isinstance(
                            state.get(inv_mod.STATE_KEY), dict) else {}
                        prev_floors = prev_inv.get("floors") if isinstance(
                            prev_inv.get("floors"), dict) else {}
                        inv_regressions = inv_mod.invariant_regressions(
                            prev_floors, inv_values)
                        results["corpus_invariants"] = inv_metrics
                        # Stash BEFORE `_mark` so its `dict(prev)` copy carries
                        # metrics/floors forward (same shape as graph_hygiene).
                        state[inv_mod.STATE_KEY] = {
                            **prev_inv,
                            "metrics": inv_metrics,
                            "floors": inv_mod.update_floors(prev_floors, inv_values),
                            "regressions": inv_regressions,
                        }
                        if not dry_run and inv_regressions:
                            self._append_hot_once(
                                f"maintain:corpus-invariants:{d.isoformat()}",
                                inv_mod.render_invariants_hot_entry(
                                    inv_regressions, inv_metrics, d),
                            )
                        # BAK-04: the same derivation, sliced worst-first and
                        # dropped where the weekly synthesis session reads it.
                        # No new scheduled task — the lane rides this fold's
                        # output and the existing Sunday session (AGENTS.md §6).
                        if not dry_run:
                            results["link_lane"] = inv_mod.write_link_lane(
                                self.index.conn, Path(self.vault))
                        _mark(inv_mod.STATE_KEY, True)
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"corpus-invariants watchdog failed: {exc}",
                            "index read", "next maintain run"))
                        _mark("corpus_invariants", False, f"{type(exc).__name__}: {exc}")

                    # CUT-02: duplicate-retention prune — safe only because
                    # every candidate is re-verified through the full
                    # provenance chain (manifest -> raw note -> archived
                    # original) right before deletion; see
                    # `maint.retention_fold`'s docstring. Gated to run at most
                    # ONCE PER DAY via the `_`-prefixed `_retention` marker
                    # (review finding [4]: the maintain umbrella fires hourly,
                    # and re-hashing the permanently-unverifiable residue every
                    # hour is ~24x wasted whole-file I/O over ~1k parked files).
                    _ret_marker = state.get("_retention")
                    _ret_marker = _ret_marker if isinstance(_ret_marker, dict) else {}
                    if _ret_marker.get("last_run") != d.isoformat():
                        try:
                            ret_res = maint.retention_fold(Path(self.vault), d)
                            results["retention"] = ret_res
                            if not dry_run:
                                state["_retention"] = {"last_run": d.isoformat()}
                            if ret_res["pruned"]:
                                auto_fixed.append(maint.auto_fixed_item(
                                    "duplicate-retention", str(Path(self.vault) / "inbox" / "_duplicate"),
                                    f"pruned {len(ret_res['pruned'])} duplicate(s) older "
                                    f"than {ret_res['retention_days']}d "
                                    f"(provenance-verified)"))
                            if ret_res["skipped"]:
                                # Distinguish "kept, chain unverifiable" (the
                                # designed conservative outcome) from a real
                                # delete/stat failure (review finding [2]): the
                                # message must not tell the owner a file failed
                                # provenance when it was actually a delete error.
                                prov = [s for s in ret_res["skipped"]
                                        if s.get("kind") == "provenance"]
                                err = [s for s in ret_res["skipped"]
                                       if s.get("kind") in ("delete", "stat")]
                                if prov:
                                    action_required.append(maint.action_required_item(
                                        f"{len(prov)} aged duplicate(s) kept — their "
                                        "provenance chain does not verify",
                                        "an unverifiable duplicate is never auto-deleted "
                                        "(its archived original may be missing/changed)",
                                        "inspect inbox/_duplicate and the referenced "
                                        "manifest/raw/originals entries",
                                        str(Path(self.vault) / "inbox" / "_duplicate")))
                                if err:
                                    action_required.append(maint.action_required_item(
                                        f"{len(err)} aged duplicate(s) could not be "
                                        "stat'd/deleted (filesystem error)",
                                        "a real I/O/permission error, NOT a provenance "
                                        "failure — the file was not removed",
                                        "check inbox/_duplicate permissions/mount",
                                        str(Path(self.vault) / "inbox" / "_duplicate")))
                        except Exception as exc:
                            blocked.append(maint.blocked_item(
                                f"duplicate-retention fold failed: {exc}",
                                "filesystem/manifest read", "next maintain run"))

                        # ADR-0008 S04: query capture logs contain raw host
                        # questions, so their retention is a separate,
                        # containment-aware fold.  It unlinks only whole
                        # expired YYYY-MM files — never compacts/truncates the
                        # live month while appenders may hold it open.
                        try:
                            from . import querylog

                            qret = querylog.prune_expired_months(
                                self.vault, role=self.role, today=d)
                            results["query_capture_retention"] = qret
                            if qret.get("pruned"):
                                auto_fixed.append(maint.auto_fixed_item(
                                    "query-log-retention", "host query ledger",
                                    f"pruned {len(qret['pruned'])} expired whole month file(s)"))
                        except Exception as exc:
                            blocked.append(maint.blocked_item(
                                f"query-log retention fold failed: {exc}",
                                "host query ledger", "next maintain run"))

                        # CAP-02: the capture corpus holds UNFILTERED MNPI mail
                        # bodies, so "for how long" has to be enforced by the
                        # schedule — a retention function nothing calls keeps
                        # them forever. Same shape as the fold above: whole
                        # expired run files only, never rows inside one.
                        try:
                            from . import cos_corpus as _corpus

                            # THE CUTOFF IS THE REAL UTC CLOCK, NEVER `d`.
                            # `--date` exists to exercise WHETHER a date-gated
                            # fold runs; passing it into a DESTRUCTIVE window
                            # made `--date <future>` delete real, unexpired
                            # mail bodies — irrecoverable, and this plan's own
                            # sessions use that flag.
                            cres = _corpus.prune(self.vault)
                            results["cos_corpus_retention"] = cres
                            # The marker is what `brain status` reports as
                            # `pruned_by_a_scheduled_fold`. Stamp it only when
                            # the scan actually completed: a permission error
                            # leaves expired MNPI bodies on disk, and marking
                            # that "pruned" is the instrument-lies failure this
                            # whole module exists to avoid.
                            if not dry_run and not cres["errors"]:
                                # …and it records the REAL UTC date, for the
                                # same reason the cutoff comes from the real
                                # clock: `--date <future>` would otherwise make
                                # status report a prune that happened today as
                                # having happened then.
                                state[_corpus.PRUNE_MARKER] = {
                                    "last_run": _corpus.cos.utcnow().date().isoformat()}
                            if cres["errors"]:
                                blocked.append(maint.blocked_item(
                                    f"COS capture-corpus retention did not complete: "
                                    f"{'; '.join(cres['errors'][:3])} — expired mail "
                                    f"bodies are still on disk and status will keep "
                                    f"reporting retention as not run here",
                                    "COS capture corpus", "next maintain run"))
                            if cres["pruned"]:
                                auto_fixed.append(maint.auto_fixed_item(
                                    "cos-corpus-retention", "COS capture corpus",
                                    f"pruned {len(cres['pruned'])} corpus file(s) "
                                    f"older than {cres['retention_days']}d"))
                            if cres["held"]:
                                # An expired corpus that never closed is kept on
                                # purpose (a live writer's inode) — but silence
                                # here IS the "forever" this fold exists to end.
                                action_required.append(maint.action_required_item(
                                    f"{len(cres['held'])} expired capture corpus/corpora "
                                    f"are past retention but never closed",
                                    "an unclosed corpus is never auto-deleted — its "
                                    "capture stage may still hold the file open",
                                    "confirm no run is writing them, then "
                                    "`brain cos-corpus-close <run>` so they age out",
                                    "COS capture corpus"))
                        except Exception as exc:
                            blocked.append(maint.blocked_item(
                                f"COS capture-corpus retention fold failed: {exc}",
                                "COS capture corpus", "next maintain run"))

                    # CUT-02: monthly quarantine triage summary — NEVER
                    # deletes; queues a hot.md summary at most once per ISO
                    # month (idempotency key), gated by the `_`-prefixed
                    # `_quarantine_summary` marker (mirrors `_graphify_drift`).
                    try:
                        q_marker = state.get("_quarantine_summary")
                        q_marker = q_marker if isinstance(q_marker, dict) else None
                        if maint.quarantine_summary_due(q_marker, d):
                            q_summary = maint.quarantine_triage_summary(Path(self.vault), d)
                            results["quarantine_summary"] = q_summary
                            if q_summary["total"]:
                                self._append_hot_once(
                                    f"quarantine-summary:{d.strftime('%Y-%m')}",
                                    maint.render_quarantine_summary_hot_entry(q_summary, d),
                                )
                                # Burn the month's only slot only when the
                                # summary REPORTED something — an empty 00:07
                                # run used to blind the rest of the month.
                                state["_quarantine_summary"] = {"last_month": d.strftime("%Y-%m")}
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"quarantine triage summary failed: {exc}",
                            "filesystem read", "next maintain run"))

                    # DEC-01 decision-capture nudge — after the sync so
                    # freshly ingested notes are already indexed. Queues each
                    # candidate to hot.md ONCE (idempotency key = note id);
                    # capturing the decision note stays a human/synthesis gate.
                    try:
                        dcands = maint.decision_capture_scan(self.index.conn, d)
                        results["decision_capture"] = {"candidates": len(dcands)}
                        for c in dcands:
                            if self._append_hot_once(
                                f"decision-capture:{c['id']}",
                                maint.render_decision_capture_hot_entry(c, d),
                            ):
                                action_required.append(maint.action_required_item(
                                    f"possible uncaptured decision in `{c['id']}` "
                                    f"(“{c['phrase']}”)",
                                    "recording a decision note is a human gate — "
                                    "the fold only nudges",
                                    "review the hot.md entry; if real, capture a "
                                    "type: decision note (+ supersede what it reverses)",
                                    c["id"]))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"decision-capture scan failed: {exc}",
                            "index read", "next maintain run"))

                    # WATCHDOG-01: the hourly umbrella watches the weekly
                    # synthesis task's heartbeat (the reverse watch lives in
                    # the synthesis prompt: doctor-first). Queued to hot.md
                    # at most once per ISO week.
                    try:
                        wd = maint.synthesis_heartbeat_finding(Path(self.vault), d)
                        if wd is not None:
                            action_required.append(wd)
                            week = d.isocalendar()
                            self._append_hot_once(
                                f"synthesis-watchdog:{week[0]}-W{week[1]}",
                                f"## {d.isoformat()} — synthesis watchdog\n"
                                f"- **Finding:** {wd['finding']}\n"
                                f"- **Owner input needed:** {wd['proposed_action']}\n")
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            f"synthesis watchdog failed: {exc}",
                            "state file read", "next maintain run"))

                    # Second pass: reconcile the folds' mutations + publish.
                    sync2 = self.sync(drain=False, publish=True)
                    results["sync_publish"] = {
                        k: sync2.get(k) for k in ("added", "updated", "deleted")}
                    snap = sync2.get("snapshot")
                    if snap:
                        auto_fixed.append(maint.auto_fixed_item(
                            "snapshot", str(snap.get("snapshot_db", "")),
                            f"published snapshot gen {snap.get('generation')}"))
                    try:
                        results["brief"] = self.brief(drain=False)
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            "could not generate the morning brief",
                            f"{type(exc).__name__}: {exc}",
                            "re-run after the underlying error is fixed"))
                    try:
                        results["brief_html"] = self.brief_html(drain=False, today=d)
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            "could not write the HTML morning brief",
                            f"{type(exc).__name__}: {exc}",
                            "re-run after the underlying error is fixed"))
                    rec_res = self._recommendations_aging_fold(d)
                    results["recommendations_aging"] = rec_res
                    if rec_res.get("surfaced"):
                        auto_fixed.append(maint.auto_fixed_item(
                            "recommendations-aging", str(config.recommendations_open_path(self.vault)),
                            f"surfaced {rec_res['surfaced']} aged recommendation(s) into hot.md"))
                    try:
                        # Opt-in (default off): folding note-creation into the
                        # maintain loop must not change maintain's note-count
                        # invariant for vaults that don't want a daily journal.
                        import os as _os
                        dn = (self._daily_note_fold(d, results.get("brief"))
                              if _os.environ.get("BRAIN_DAILY_NOTE") else {"created": False, "skipped": "BRAIN_DAILY_NOTE unset"})
                        results["daily_note"] = dn
                        if dn.get("created"):
                            auto_fixed.append(maint.auto_fixed_item(
                                "daily-note", str(self.vault),
                                f"created daily note {dn['id']}"))
                    except Exception as exc:
                        blocked.append(maint.blocked_item(
                            "could not create today's daily note",
                            f"{type(exc).__name__}: {exc}",
                            "create it manually with tools/brain_daily.py"))
                    _mark("daily", True)
                except WriterLockBusy as exc:
                    # CC-02/[HARDENED:adv-r1-consensus] contract: a long
                    # rebuild can legitimately hold the writer lock for 90
                    # minutes, so the hourly launchd job that can't acquire
                    # it MUST exit cleanly, not error -- one long rebuild
                    # would otherwise manufacture ~90 minutes of "failures"
                    # and fire a spurious notification. This is deliberately
                    # a DIFFERENT bucket than the generic-Exception branch
                    # below: it must NEVER touch consecutive_failures,
                    # last_run, or last_successful_index_run (those mean
                    # WORK COMPLETED and are what liveness keys on) -- a
                    # leaked/wedged lock would otherwise make every hourly
                    # run "refresh last_attempt and skip cleanly" forever,
                    # reporting HEALTHY while the system does zero work.
                    self._mark_writer_busy(state, "daily", d, exc)
                    if not dry_run:
                        self._save_maintain_state(state)
                    return {
                        "ritual": "maintain", "dry_run": dry_run,
                        "date": d.isoformat(), "status": "skipped-writer-busy",
                        "note": f"writer busy (held by pid={exc.holder.get('pid')}, "
                                f"verb={exc.holder.get('verb')}) — skipping this run",
                        "held_by": exc.holder,
                        "consecutive_skips": state.get("daily", {}).get("consecutive_skips"),
                        "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
                    }
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "daily branch (sync/brief/recommendations-aging) raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("daily", False, f"{type(exc).__name__}: {exc}")

            # FRESH-01 — drift-triggered graphify check (2026-07-11). Runs
            # every maintain, REGARDLESS of dry_run and independent of the
            # daily branch's own try/except (a corpus-drift READ never needs
            # the daily fold's sync to have succeeded — ``self.index.conn``
            # already reflects whatever the index currently holds): the
            # monthly date-gate below remains the FLOOR trigger; this is the
            # inverse — a vault that drifts past the threshold rebuilds
            # early instead of waiting out the calendar. Computed once here
            # so the unified graphify block below never double-builds on a
            # day that is BOTH drift-triggered and the monthly floor.
            graphify_drift_triggered = False
            try:
                import json as _json

                try:
                    old_manifest = _json.loads(
                        config.graph_manifest_path(self.vault).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    old_manifest = None
                drift_ratio = maint.graphify_drift(old_manifest, self.index.conn)
                drift_marker = state.get("_graphify_drift")
                drift_marker = drift_marker if isinstance(drift_marker, dict) else None
                # A manifest that has NEVER existed has no baseline to have
                # drifted FROM (the pure ratio function still reports 1.0 for
                # it — a defined, degenerate "unknown" signal) — that
                # first-ever-build case is already the monthly floor's job
                # (a "graphify" branch absent from maintain-state is due
                # immediately, ADR-0003 Ruling d/``maintain_branches``). The
                # drift trigger only fires once there IS an established
                # baseline to measure real drift against.
                graphify_drift_triggered = bool(old_manifest) and maint.should_trigger_drift_graphify(
                    drift_ratio, drift_marker, d)
                results["graphify_drift"] = {
                    "ratio": round(drift_ratio, 4), "triggered": graphify_drift_triggered,
                    "has_baseline": bool(old_manifest)}
            except Exception as exc:
                blocked.append(maint.blocked_item(
                    f"graphify drift check failed: {exc}",
                    "index read", "next maintain run"))

            if "health" in branches:
                try:
                    h = self.health()
                    fsync_finding = self._framework_sync_finding()
                    if fsync_finding is not None:
                        h["outcomes"]["action_required"].append(fsync_finding)
                    results["health"] = h
                    action_required += h["outcomes"]["action_required"]
                    blocked += h["outcomes"]["blocked"]
                    _mark("health", True)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "health branch raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("health", False, f"{type(exc).__name__}: {exc}")

            if "integrity" in branches:
                try:
                    i = self.integrity(min_score=min_score, k=near_dup_k)
                    results["integrity"] = i
                    blocked += i.get("blocked", [])
                    if i.get("audit_issue"):
                        action_required.append(i["audit_issue"])
                    if i.get("near_dup_pairs"):
                        # near_dup_pairs are UNFILTERED here; `maintain` reports only
                        # the raw count (egress applies at the standalone `integrity`
                        # verb, which is where a caller actually inspects pair content).
                        action_required.append(maint.action_required_item(
                            f"{len(i['near_dup_pairs'])} near-duplicate pair(s) found "
                            f">= {min_score}",
                            "de-dup is a human merge/keep judgment, never auto-merged",
                            "run `brain integrity --json` for the gated pair list and review",
                            "near-dup scan"))
                    _mark("integrity", True)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "integrity branch raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("integrity", False, f"{type(exc).__name__}: {exc}")

            if "graph_hygiene" in branches:
                try:
                    from . import graph as graph_mod

                    metrics = graph_mod.graph_hygiene_metrics(self.index.conn)
                    prev_entry = state.get("graph_hygiene") if isinstance(
                        state.get("graph_hygiene"), dict) else {}
                    prev_metrics = prev_entry.get("metrics") if isinstance(
                        prev_entry.get("metrics"), dict) else None
                    growth = maint.graph_hygiene_orphan_growth(prev_metrics, metrics)
                    results["graph_hygiene"] = metrics
                    # Stash the metrics INTO state before `_mark` so its
                    # `dict(prev)` copy carries them forward (mirrors no other
                    # branch needing extra state — this is the first one that
                    # persists more than status bookkeeping).
                    state["graph_hygiene"] = {**prev_entry, "metrics": metrics}
                    if not dry_run and maint.should_alert_graph_hygiene_growth(growth):
                        try:
                            self._append_hot_once(
                                f"maintain:graph_hygiene:{d.isoformat()}:{metrics.get('orphan_count')}",
                                maint.render_graph_hygiene_hot_entry(metrics, growth, d),
                            )
                        except Exception as hot_exc:  # noqa: BLE001
                            action_required.append(maint.action_required_item(
                                "graph-hygiene hot-queue entry could not be written",
                                f"{type(hot_exc).__name__}: {hot_exc}",
                                "check .brain/memory/hot.md writability; the "
                                "metrics themselves were computed fine",
                                "graph hygiene"))
                    if not dry_run:
                        # Best-effort regen — the weekly fold just recomputed
                        # hygiene metrics; the rendered explorer page should
                        # reflect them too. A render failure never fails the
                        # branch itself (mirrors the graphify success-path wiring).
                        try:
                            self.graph_report(today=d)
                        except Exception:  # noqa: BLE001
                            pass
                    _mark("graph_hygiene", True)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "graph_hygiene branch raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("graph_hygiene", False, f"{type(exc).__name__}: {exc}")

            if "digest" in branches:
                try:
                    results["digest"] = self.digest(days=7)
                    curate_res = self.curate(dry_run=dry_run, today=d)
                    promote_res = self.promote_scan()
                    results["curate"] = curate_res
                    results["promote_scan"] = promote_res
                    if not dry_run:
                        if curate_res.get("stale_links") or curate_res.get("revisit_sample"):
                            # Idempotency key = hash of the DISTINCT stale-target
                            # set, NOT the run date, so an unchanged dangling set
                            # isn't re-reported every week under a fresh key
                            # (field bug 2).
                            self._append_hot_once(
                                "maintain:curate:"
                                + maint.curation_finding_key(curate_res["stale_links"]),
                                maint.render_curation_hot_entry(
                                    curate_res["stale_links"], curate_res["revisit_sample"], d),
                            )
                        if promote_res.get("candidates"):
                            # Idempotency key = hash of the DISTINCT candidate-id
                            # set, NOT the run date — an unchanged candidate set
                            # isn't re-reported every run under a fresh key
                            # (retro signature: duplicate-findings).
                            self._append_hot_once(
                                "maintain:promote-scan:"
                                + maint.promote_scan_finding_key(promote_res["candidates"]),
                                maint.render_promote_scan_hot_entry(promote_res["candidates"], d),
                            )
                        results["digest_html"] = self.digest_html(days=7, today=d)
                        # Retro fold (PUSH redesign): deterministic scan of this
                        # vault's own maintenance output for engine failure
                        # signatures -> ready-to-run prompts in engine-feedback/.
                        # Fires weekly here so it stands even if the model-backed
                        # synthesis session doesn't run; the synthesis prompt
                        # ALSO calls `brain retro` to act on what it finds.
                        retro_res = self.retro(today=d)
                        results["retro"] = retro_res
                        if retro_res["feedback_written"]:
                            auto_fixed.append(maint.auto_fixed_item(
                                "retro", "engine-feedback/",
                                f"filed {len(retro_res['feedback_written'])} engine "
                                f"bug prompt(s): {', '.join(retro_res['feedback_written'])}"))
                    _mark("digest", True)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "digest branch (digest/curate/promote-scan) raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("digest", False, f"{type(exc).__name__}: {exc}")

            if "golden" in branches:
                try:
                    probes_path = maint.golden_probes_path(Path(self.vault))
                    if not probes_path.is_file():
                        # Absent probes file — skip LOUDLY (never a silent
                        # pass, never an error): mark done so this doesn't
                        # re-check every hour, but next Sunday re-checks.
                        results["golden"] = {
                            "score": None, "runner": None, "degraded": False,
                            "skipped": "no probes file",
                        }
                        action_required.append(maint.action_required_item(
                            f"golden-probe branch skipped: no probes file at {probes_path}",
                            "WD-03 cross-family execution needs a per-vault "
                            "eval/golden-probes.json (WD-02) to score",
                            "author a probes file (see `brain-golden-probe --help` "
                            "/ docs/operations/s06-evidence.md)",
                            str(probes_path)))
                        _mark("golden", True)
                    else:
                        marker = state.get("_golden_attempt")
                        marker = marker if isinstance(marker, dict) else None
                        now_dt = _dt.datetime.now(_dt.timezone.utc)
                        if not maint.golden_attempt_due(marker, now_dt):
                            results["golden"] = {
                                "score": None, "runner": None, "degraded": False,
                                "skipped": "cooldown",
                                "next_retry_at": (marker or {}).get("next_retry_at"),
                            }
                            # Deliberately no `_mark` call: the branch stays
                            # due (still Sunday, or a missed catch-up), but
                            # NO codex/self invocation happens this hour —
                            # the whole point of the next_retry_at gate.
                        elif dry_run:
                            # Fix [3]: a --dry-run PREVIEW must NOT spawn the
                            # golden runner (real codex/self subprocess, up to
                            # ~600s+600s) or burn codex quota — report what
                            # WOULD run and persist NO marker.
                            results["golden"] = {
                                "score": None, "runner": None, "degraded": False,
                                "dry_run": True, "would_run": True,
                                "probes_path": str(probes_path),
                            }
                            _mark("golden", True)
                        else:
                            # Persist a PROVISIONAL next_retry_at (short
                            # backoff) BEFORE the shell-out (fix [1], mirrors
                            # `_run_bounded_graphify`'s attempt-persisted-
                            # before-build ordering). `golden_attempt_due`
                            # keys the cooldown on `next_retry_at` alone, so a
                            # run KILLED mid-`codex exec` (reboot/OOM/launchd
                            # timeout) — leaving no clean return to write the
                            # outcome-based value — still backs off next hour
                            # instead of re-storming codex. Overwritten with
                            # the outcome-based value on a clean return below.
                            base_min = int(_os.environ.get(
                                maint.GOLDEN_RETRY_BASE_MINUTES_ENV,
                                maint.DEFAULT_GOLDEN_RETRY_BASE_MINUTES))
                            # ESCALATING provisional (re-review): optimistically
                            # count THIS attempt as a failure and back off on the
                            # incremented count, so a REPEATEDLY killed run backs
                            # off progressively (6h → 12h → …) instead of a flat
                            # base every time. A clean return below recomputes the
                            # authoritative marker from the PRE-attempt count
                            # `orig_n` (so a real transient isn't double-counted,
                            # and a success resets to 0).
                            orig_n = int((marker or {}).get("consecutive_transient_failures", 0))
                            prov_n = orig_n + 1
                            pre_marker = dict(marker or {})
                            pre_marker["last_attempt"] = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                            pre_marker["consecutive_transient_failures"] = prov_n
                            pre_marker["next_retry_at"] = (
                                now_dt + _dt.timedelta(
                                    minutes=maint.golden_retry_backoff_minutes(base_min, prov_n))
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                            state["_golden_attempt"] = pre_marker
                            self._save_maintain_state(state)
                            runner = golden_runner or self._run_golden_probe
                            g = runner(probes_path=probes_path)
                            results["golden"] = g
                            exit_code = g.get("exit_code")
                            transient = exit_code not in (
                                maint.GOLDEN_EXIT_OK, maint.GOLDEN_EXIT_REGRESSION,
                                maint.GOLDEN_EXIT_ACTION_REQUIRED)
                            state["_golden_attempt"] = maint.update_golden_attempt_marker(
                                {**pre_marker, "consecutive_transient_failures": orig_n},
                                now_dt, transient=transient)
                            self._save_maintain_state(state)
                            if transient:
                                blocked.append(maint.blocked_item(
                                    f"golden-probe run was transient (runner="
                                    f"{g.get('runner')}): {g.get('error') or g.get('codex_error') or 'no deterministic result'}",
                                    "the brain CLI itself failed/emitted non-JSON, or "
                                    "codex could not be validated and the self-run "
                                    "fallback also failed",
                                    "bounded backoff will retry automatically once "
                                    "the cooldown elapses"))
                                _mark("golden", False, "transient")
                            else:
                                if exit_code == maint.GOLDEN_EXIT_ACTION_REQUIRED:
                                    action_required.append(maint.action_required_item(
                                        f"golden-probe run is config-invalid (score="
                                        f"{g.get('score')})",
                                        "a deterministic problem in the probes file/vault "
                                        "anchors — never retried before next Sunday",
                                        "fix the probes file, then re-run "
                                        "`brain-golden-probe` manually to confirm",
                                        str(probes_path)))
                                elif exit_code == maint.GOLDEN_EXIT_REGRESSION:
                                    action_required.append(maint.action_required_item(
                                        f"golden-probe regression: score {g.get('score')}",
                                        "retrieval quality regressed below the "
                                        "probes-file threshold",
                                        "run the autoresearch skill or review recent "
                                        "promotions/curation findings",
                                        str(probes_path)))
                                # A persistent degraded/self-run state means
                                # cross-family EXECUTION is not actually
                                # happening (codex unavailable/unvalidated) —
                                # surfaced every degraded run, not just once,
                                # so it naturally reads as "persistent" in
                                # hot.md/action_required if it keeps recurring
                                # (ponytail: no extra streak-counter state).
                                if g.get("degraded"):
                                    action_required.append(maint.action_required_item(
                                        "golden-probe ran in DEGRADED (self) mode — "
                                        f"codex execution unavailable/unvalidated: "
                                        f"{g.get('codex_error')}",
                                        "cross-family EXECUTION requires codex to "
                                        "actually run the scorer; a persistent "
                                        "degraded state means that isn't happening",
                                        "check codex CLI availability/auth on this host",
                                        str(probes_path)))
                                _mark("golden", True)
                except Exception as exc:
                    # Fix [1]: a raise mid-branch (before the pre-shell-out
                    # save, or in the runner/marker-update path) must still
                    # leave a next_retry_at so the next hourly maintain backs
                    # off instead of re-invoking codex every hour. The
                    # pre-shell-out save usually already wrote one; this is the
                    # belt for a raise BEFORE it lands.
                    if not dry_run:
                        try:
                            cur = state.get("_golden_attempt")
                            cur = dict(cur) if isinstance(cur, dict) else {}
                            now_dt = _dt.datetime.now(_dt.timezone.utc)
                            # Refresh the backoff when it is ABSENT *or already
                            # ELAPSED*: an elapsed next_retry_at is exactly why
                            # this branch was due, so leaving it in place keeps
                            # `golden_attempt_due` True and re-storms codex every
                            # hour. Only a still-FUTURE value (the provisional
                            # save already landed) is left alone. Parsing mirrors
                            # `golden_attempt_due` so "present" means the same
                            # thing on both sides of the gate.
                            existing = cur.get("next_retry_at")
                            existing_dt = None
                            if existing:
                                try:
                                    existing_dt = _dt.datetime.fromisoformat(
                                        str(existing).replace("Z", "+00:00"))
                                    if existing_dt.tzinfo is None:
                                        existing_dt = existing_dt.replace(
                                            tzinfo=_dt.timezone.utc)
                                except ValueError:
                                    existing_dt = None
                            if existing_dt is None or existing_dt <= now_dt:
                                try:
                                    base_min = int(_os.environ.get(
                                        maint.GOLDEN_RETRY_BASE_MINUTES_ENV,
                                        maint.DEFAULT_GOLDEN_RETRY_BASE_MINUTES))
                                except ValueError:
                                    base_min = maint.DEFAULT_GOLDEN_RETRY_BASE_MINUTES
                                cur["last_attempt"] = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                                cur["next_retry_at"] = (
                                    now_dt + _dt.timedelta(minutes=base_min)
                                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                                state["_golden_attempt"] = cur
                                self._save_maintain_state(state)
                        except Exception:  # noqa: BLE001 — backoff persist is best-effort
                            pass
                    blocked.append(maint.blocked_item(
                        "golden branch raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("golden", False, f"{type(exc).__name__}: {exc}")

            # FRESH-01: the monthly date-gate (floor) and the drift trigger
            # (early rebuild) are ORed into ONE build — both routed through the
            # SAME attempt-bounded in-process path (`_run_bounded_graphify`;
            # owner decision 2026-07-12). A day that is both the monthly floor
            # AND drift-triggered still builds exactly once.
            monthly_due = "graphify" in branches
            # The ATTEMPT-keyed cooldown (with capped exponential backoff)
            # gates the MONTHLY floor too (review finding [0]): a failing
            # 1st-of-month build leaves its branch due, and without this gate
            # it would re-fire a full rebuild every hourly maintain, unbounded.
            # The floor OBLIGATION survives (the branch stays due until a
            # publish advances it) — only the RETRY CADENCE backs off.
            import os as _os

            _drift_marker = state.get("_graphify_drift")
            _drift_marker = _drift_marker if isinstance(_drift_marker, dict) else None
            attempt_allowed = maint.graphify_drift_marker_due(
                _drift_marker, d, int(_os.environ.get(
                    maint.GRAPHIFY_COOLDOWN_DAYS_ENV, maint.DEFAULT_GRAPHIFY_COOLDOWN_DAYS)))
            if (monthly_due or graphify_drift_triggered) and not attempt_allowed:
                # Deferred by the attempt-keyed (backed-off) cooldown — leave a
                # breadcrumb so a skipped build is explainable, never silent.
                results["graphify"] = {
                    "ritual": "graphify", "invoked": False, "published": False,
                    "status": "cooldown_deferred",
                    "note": "a recent failed attempt is backing off; the build "
                            "retries once the (exponential) cooldown elapses",
                }
            if (monthly_due or graphify_drift_triggered) and attempt_allowed:
                reason = "drift" if graphify_drift_triggered else "monthly-floor"
                try:
                    g = self._run_bounded_graphify(
                        force=False, dry_run=dry_run, today=d, state=state,
                        reason=reason, builder=graphify_runner)
                    results["graphify"] = g
                    if not dry_run and g.get("published") and g.get("candidates"):
                        # Best-effort (review finding [4]): a hot.md write
                        # failure must not skip the `_mark` below — the build
                        # PUBLISHED, and leaving the branch due would trigger a
                        # redundant rebuild next run. Surface it instead.
                        try:
                            self._append_hot_once(
                                f"maintain:graphify:{d.isoformat()}",
                                maint.render_graphify_hot_entry(g["candidates"], d),
                            )
                        except Exception as hot_exc:  # noqa: BLE001
                            action_required.append(maint.action_required_item(
                                "graphify hot-queue entry could not be written",
                                f"{type(hot_exc).__name__}: {hot_exc}",
                                "check .brain/memory/hot.md writability; the "
                                "graph itself published fine",
                                "graphify hot-queue"))
                    build_info = g.get("build") or {}
                    published = bool(g.get("published"))
                    skipped = bool(g.get("skipped"))
                    # Key the "benign, never alarm" suppression on the RESULT's
                    # own dry_run flag: a genuine dry-run PREVIEW sets
                    # ``g["dry_run"] = True`` (published False by design) and must
                    # not alarm as a FAILURE; but a REAL failure under `brain
                    # maintain --dry-run` returns a failure result with NO
                    # dry_run flag, and the preview MUST still surface it.
                    result_is_preview = bool(g.get("dry_run"))
                    dur = build_info.get("duration_seconds")
                    dur_suffix = f" ({dur}s)" if dur is not None else ""
                    if result_is_preview:
                        # Never a failure alarm — but a soft-budget breach in the
                        # preview is exactly the pre-flight signal `--dry-run`
                        # exists to show (review finding [5]): keep it.
                        if build_info.get("action_required"):
                            dur_txt = f"{dur}s" if dur is not None else "an unknown duration"
                            action_required.append(maint.action_required_item(
                                f"graphify dry-run build took {dur_txt} "
                                f"(> {build_info.get('action_required_seconds')}s soft budget)",
                                "the PREVIEW build exceeded the soft wall-clock "
                                "budget — the real scheduled build likely will too",
                                "investigate corpus scale / vector backend before "
                                "the next scheduled build",
                                "graphify build"))
                    elif not published and not skipped:
                        # A failed in-process build MUST append a `blocked` item
                        # too — OBS-02's alarm keys off `blocked` count, so a
                        # failed graph build must never read as a clean run just
                        # because it also has an action_required.
                        blocked.append(maint.blocked_item(
                            f"graphify build ({reason}, status={g.get('status', 'unknown')}) "
                            f"failed to complete{dur_suffix}",
                            "the in-process graph build raised, returned a bad "
                            "result, or failed to build/validate "
                            "(build_failed/invalid_artifact)",
                            "capped exponential backoff will retry automatically "
                            "once the cooldown elapses"))
                        action_required.append(maint.action_required_item(
                            f"graphify build ({reason}, status={g.get('status', 'unknown')}) "
                            f"failed to complete{dur_suffix}",
                            "the in-process graph build raised, returned a bad "
                            "result, or failed to build/validate",
                            "inspect the result's error/status detail and "
                            ".brain/graph/BUILD_FAILED.json; capped exponential "
                            "backoff will retry automatically once the cooldown "
                            "elapses",
                            "graphify build"))
                    elif published and build_info.get("action_required"):
                        # published, but slower than the soft budget —
                        # informational only: this build already succeeded, so
                        # never claim a retry that will not happen.
                        dur_txt = f"{dur}s" if dur is not None else "an unknown duration"
                        action_required.append(maint.action_required_item(
                            f"graphify build ({reason}) published but took {dur_txt} "
                            f"(> {build_info.get('action_required_seconds')}s soft budget)",
                            "the graph build exceeded its soft wall-clock budget "
                            "but completed and published successfully",
                            "investigate corpus scale / vector backend before the "
                            "next scheduled build — no retry is needed, this "
                            "build already succeeded",
                            "graphify build"))
                    if result_is_preview:
                        pass  # a genuine dry-run preview marks nothing
                    elif published or skipped:
                        _mark("graphify", True)
                    elif monthly_due:
                        # the monthly FLOOR was due and the build failed — leave
                        # it due (never silently drop the floor obligation).
                        _mark("graphify", False, g.get("status", "build_failed"))
                except Exception as exc:  # noqa: BLE001 — backstop for the maintain-
                    # SIDE handling (e.g. the hot.md append, or a disk error in
                    # `_run_bounded_graphify`'s own state write). The build's OWN
                    # outcome + backoff are fully owned by `_run_bounded_graphify`
                    # (it catches build errors internally and records/persists the
                    # `_graphify_drift` marker), so this handler MUST NOT touch the
                    # backoff marker: doing so would double-count a build failure,
                    # or worse, penalize a build that actually PUBLISHED when the
                    # post-publish hot.md write is what raised. It only surfaces
                    # the failure so it is never silent.
                    blocked.append(maint.blocked_item(
                        "graphify branch raised (maintain-side handling)",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))

            # -- OBS-01/02/04: ONE final health-history append per run
            # (HARDENED correction 2 — never appended right after the second
            # sync, so this record carries health/integrity/digest/graphify
            # outcomes too; ``results`` is the structured hook a later
            # golden-eval branch folds into via ``results["golden"]``).
            # HOST-broker, and skipped (read-only collection only) under
            # ``dry_run`` — no append, no notification, no state mutation.
            pre_outcomes = maint.build_outcomes(auto_fixed, action_required, blocked)
            health_record: dict[str, Any] | None = None
            trend_findings: list[dict[str, Any]] = []
            notifications: list[str] = []
            try:
                health_record = maint.collect_health_metrics(
                    self, outcomes=pre_outcomes, results=results,
                    run_id=maint.new_health_run_id())
                if not dry_run:
                    maint.append_health_record(Path(self.vault), health_record)
            except Exception as exc:
                blocked.append(maint.blocked_item(
                    f"health-history/trend/notify fold failed: {exc}",
                    "metrics collection or file I/O", "next maintain run"))

            # Fix [2]: compute trend + fire notifications from the POST-fold
            # outcomes (built fresh here, AFTER the except above may have
            # just appended its own blocked_item) — never from the frozen
            # ``pre_outcomes`` snapshot. Otherwise a health-fold failure
            # (e.g. `.brain` full/read-only) reports blocked>0 in the run's
            # own outcomes yet never raises the alarm OBS-02 exists for,
            # because the notify call used to sit INSIDE the same try block
            # that just failed and was skipped entirely.
            if not dry_run:
                post_outcomes = maint.build_outcomes(auto_fixed, action_required, blocked)
                try:
                    history = maint.read_health_history(Path(self.vault))
                    sparse_history = maint.read_sparse_history(Path(self.vault))
                    trend_findings = maint.health_trend(
                        history, d, sparse_history=sparse_history)
                except Exception:  # noqa: BLE001 — trend is best-effort; the
                    pass            # blocked count alone still drives the alarm below.
                try:
                    candidates = maint.pending_notifications(
                        Path(self.vault), post_outcomes, trend_findings, d,
                        maintain_state=state)
                    notifications = maint.fire_and_mark_notifications(
                        Path(self.vault), candidates, d)
                except Exception:  # noqa: BLE001 — a notify-path failure is cosmetic,
                    pass            # never allowed to fail the maintain run itself.
            results["health_history"] = health_record
            results["health_trend"] = trend_findings
            results["notifications"] = notifications

            if not dry_run:
                # Auto-apply a newer engine version (owner decision 2026-07-25).
                # Broad try/except — maintain itself NEVER dies on the update
                # machinery (and _maybe_auto_update wraps its own internals too).
                try:
                    au = self._maybe_auto_update(d)
                    results["auto_update"] = au
                    if au.get("auto_update") == "applied":
                        auto_fixed.append(maint.auto_fixed_item(
                            "auto-update", "brain update",
                            f"auto-updated engine to {au.get('latest')}"))
                    elif au.get("auto_update") == "failed":
                        blocked.append(maint.blocked_item(
                            f"auto-update to {au.get('latest')} FAILED: {au.get('notes')}",
                            "update pipeline", "run `brain update` manually"))
                except Exception as exc:  # noqa: BLE001
                    blocked.append(maint.blocked_item(
                        f"auto-update check/apply raised: {type(exc).__name__}: {exc}",
                        "update machinery", "next maintain run"))

                # hot-md-bloat: rotate aged/resolved entries to archive/ once
                # the live file exceeds the soft cap. Best-effort hygiene —
                # never allowed to fail the maintain run.
                try:
                    self._rotate_hot_md(d)
                except Exception:  # noqa: BLE001
                    pass
                # In-process graphify (owner decision 2026-07-11): the
                # `_graphify_drift` marker is written by `_run_bounded_graphify`
                # within THIS process, into THIS `state` dict, so `state` already
                # holds the authoritative marker — no cross-process re-merge is
                # needed (the subprocess-era re-read that could revert an
                # in-memory backoff bump is gone with the subprocess). Two
                # overlapping maintain PROCESSES under a broken 2h stale-lock
                # last-writer-win their branch stamps, as they always have — a
                # pre-existing maintain limitation, not something graphify adds.
                self._save_maintain_state(state)

                # Health-report regen: reads the state we just persisted
                # above, so it reflects THIS run. Best-effort — a render
                # failure must never fail the maintain umbrella itself.
                try:
                    self.health_report(today=d)
                except Exception:  # noqa: BLE001
                    pass

            return {
                "ritual": "maintain", "dry_run": dry_run, "date": d.isoformat(),
                "weekday": d.strftime("%A"), "branches_due": branches,
                "results": results,
                "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
            }
        finally:
            self._release_maintain_lock(lock_path)
