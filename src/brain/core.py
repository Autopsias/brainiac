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

from pathlib import Path
from typing import Any

from . import classification
from . import config
from . import frontmatter
from .audit import AuditChain, KeyUnavailable
from .index import BrainIndex, Hit
from .notes import load_note, safe_slug, sha256_text


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


class RoleError(RuntimeError):
    """A host-broker operation was attempted from the read+draft-only VM leg.

    The VM leg (``role=vm``) may never write notes, mutate/WAL the index, publish
    a snapshot, or resolve a signing key. These ops fail with RoleError BEFORE
    any signing-key resolution or index write is attempted (S06 hard guarantee).
    """


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
        rrf_k: int = 60,
    ) -> list[Hit]:
        """Fused RRF(k) BM25 + dense retrieval (RET-01), optional skippable
        reranker (RET-02). UNFILTERED — the CLI applies the egress gate."""
        return self.index.hybrid_search(
            query, k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k,
        )

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
        rerank_fused: bool = False, fused_pool: int = 20,
    ) -> list[Hit]:
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
        """
        from dataclasses import replace

        variants = [q for q in (queries or []) if q and q.strip()]
        if not variants:
            return []
        if len(variants) == 1:
            return self.hybrid_search(
                variants[0], k=k, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k
            )
        # Per-query depth is deliberately SHALLOW (≈ k, not a wide over-fetch).
        # RRF over wide per-query lists lets a noise doc present in BOTH lists at
        # low rank (e.g. PT@50 + EN@60) out-accumulate a gold present in only ONE
        # list at high rank (e.g. EN@5) — measured: per_query_k 20→80 drops
        # monolingual_pt fan-out recall 0.736→0.625. Keep each variant's
        # contribution to its genuine top hits. Tunable via per_query_k.
        pk = per_query_k or max(k, 20)
        fused: dict[str, list] = {}  # id -> [fused_score, Hit]
        for q in variants:
            hits = self.hybrid_search(
                q, k=pk, rerank=rerank, rerank_top=rerank_top, rrf_k=rrf_k
            )
            for rank, h in enumerate(hits, start=1):
                contrib = 1.0 / (rrf_k + rank)
                cur = fused.get(h.id)
                if cur is None:
                    fused[h.id] = [contrib, h]
                else:
                    cur[0] += contrib
        ranked = sorted(fused.values(), key=lambda t: -t[0])
        # Stamp the fused score so any downstream re-sort preserves fan-out order.
        fused_hits = [replace(h, score=s) for s, h in ranked]

        # POST-FUSION RERANK (RET-05b) — fan-out maximises deep RECALL (golds the
        # single query missed surface at ranks 11-20), but answer generation reads
        # only the TOP few, where wide recall + RRF + a zone prior inject noise.
        # The cross-encoder reorders the wide fused POOL against the ORIGINAL query
        # (variants[0]) so brain's recall@20 advantage is converted into top-k
        # PRECISION. Without this, fan-out wins recall@20 but loses precision@5 to
        # SC's whole-note embeddings (measured: answer-grounded eval, S10). The
        # rerank is SKIPPABLE (offline/no model -> identity, never an error).
        if rerank_fused and fused_hits:
            fused_hits = self.index._apply_rerank(
                variants[0], fused_hits, None, fused_pool
            )
            # _apply_rerank REORDERS but keeps each hit's (fused) score, so a
            # downstream re-sort by score would undo the rerank. Re-stamp a strictly
            # descending score that encodes the post-rerank RANK, so the cross-encoder
            # order survives any {path: score} round-trip (e.g. the eval harness).
            n = len(fused_hits)
            fused_hits = [replace(h, score=float(n - i)) for i, h in enumerate(fused_hits)]
        return fused_hits[:k]

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
        for d in decisions:
            d_date = d.get("date") or ""
            d["tensions"] = [
                {"id": s["id"], "date": s.get("date", ""), "type": s.get("type", "")}
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
    def rebuild(self) -> dict[str, Any]:
        self._require_host("rebuild the index")
        return self.index.rebuild(self.vault)

    def embedder_pending(self) -> bool:
        """True when the index's stored dense vectors were built with a
        DIFFERENT embedder than the one the live runtime would use now (S02/
        CS-01) — e.g. a cold-start install built the index with the offline
        ``hash`` placeholder to avoid a network model download. Read-only,
        cheap (no download): :meth:`BrainIndex.model_matches` only compares
        recorded meta strings against the constructed (not yet loaded)
        embedder's ``model_id``/``dim``."""
        return not self.index.model_matches()

    def warmup(self) -> dict[str, Any]:
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

        embedder = get_embedder(os.environ.get("BRAIN_EMBEDDER", "auto"))
        was_cached = model_cache_ready(embedder)
        t0 = time.monotonic()
        embedder.embed("warmup")  # triggers the real load/download if needed
        elapsed = time.monotonic() - t0
        return {
            "model_id": embedder.model_id,
            "already_cached": bool(was_cached),
            "elapsed_s": round(elapsed, 2),
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

    def drain_drafts(self) -> dict[str, Any]:
        """drain-on-invoke (HOST only): promote pending capture drafts.

        The incremental indexer IS the capture drain. The host picks up each
        draft in ``.brain/drafts/`` AND ``capture-inbox/`` (the VM-facing drop),
        signs + writes it into ``raw/`` (if a source) or ``brain/resources/`` (if
        a note) via the audited host-broker ``write_note``, then removes the
        draft. Idempotent and cheap: empty drop dirs are a no-op. Fails CLOSED —
        if no signing key resolves, drafts are LEFT in place (never promoted
        unsigned) and reported as skipped.

        This is NOT a dedicated scheduled task and NOT a daemon: it runs as the
        first step of any host ``sync`` invocation. There is no capture daemon
        and no dedicated drain task — the ONE sanctioned scheduled task is the
        ux-02 brief/digest, which doubles as the guaranteed daily drain floor.
        """
        self._require_host("drain capture drafts (sign + index)")
        promoted: list[str] = []
        skipped: list[dict[str, str]] = []
        any_dir = False
        for ddir in self._draft_sources():
            if not ddir.is_dir():
                continue
            any_dir = True
            for draft in sorted(ddir.glob("*.md")):
                note = load_note(draft, self.vault)
                if note is None:
                    skipped.append({"draft": draft.name, "reason": "no-frontmatter"})
                    continue
                # C-2 trust boundary: note.id comes from attacker-controlled
                # draft frontmatter — refuse non-slug ids (fail closed, draft
                # left in place, never signed).
                try:
                    nid = safe_slug(note.id)
                except ValueError as exc:
                    skipped.append({"draft": draft.name, "reason": f"unsafe-id (fail-closed): {exc}"})
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
                    skipped.append({"draft": draft.name,
                                     "reason": f"duplicate-id: {nid!r} already exists"})
                    continue
                content = draft.read_text(encoding="utf-8")
                try:
                    self.write_note(rel, content,
                                    reason=f"drain-on-invoke promote {draft.name}",
                                    subtree=subtree)
                except KeyUnavailable:
                    skipped.append({"draft": draft.name, "reason": "no-signing-key (fail-closed)"})
                    continue
                except ValueError as exc:
                    skipped.append({"draft": draft.name, "reason": f"unsafe-path (fail-closed): {exc}"})
                    continue
                draft.unlink()
                promoted.append(rel)
        if not any_dir:
            return {"promoted": 0, "skipped": 0, "details": [], "reason": "no-drafts-dir"}
        return {
            "promoted": len(promoted),
            "skipped": len(skipped),
            "details": {"promoted": promoted, "skipped": skipped},
        }

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

    def sync(self, *, drain: bool = True, publish: bool = False) -> dict[str, Any]:
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
        idx_res = self.index.sync(self.vault)
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
        import datetime as _dt
        import shutil as _sh
        import sqlite3 as _sq

        self._require_host("restore the index from a snapshot")
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
        # metadata still says e5-small. Surface the model_id of the embedder
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
        # ADR-0003 Ruling 5/d + HARDENED:premortem — surface `brain maintain`'s
        # own heartbeat (a stale `daily` branch or a repeatedly-failing branch)
        # so a broken nightly is visible here too, not only via the
        # session-start hook's stale-nightly line.
        out["maintain_heartbeat"] = self._maintain_heartbeat_summary(today=today)
        out["graph"] = self._graph_status()
        return out

    def _count_pending_drafts(self) -> int:
        n = 0
        for ddir in self._draft_sources():
            if ddir.is_dir():
                n += len(list(ddir.glob("*.md")))
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
            target.write_text(content, encoding="utf-8")
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
    _SUPERSEDE_JOURNAL = "supersede-pending.json"

    def _supersede_journal_path(self) -> Path:
        return config.brain_runtime_dir(self.vault) / self._SUPERSEDE_JOURNAL

    def _recover_pending_supersede(self) -> dict[str, Any] | None:
        """HOST-only. If a prior ``supersede`` was interrupted between its two
        signed writes, roll the completed side back to its pre-transaction
        content (itself a fresh signed write) and clear the journal — so a crash
        mid-transaction can never leave a signed half-chain. Runs at the top of
        every ``supersede`` call before any new write is attempted."""
        path = self._supersede_journal_path()
        if not path.exists():
            return None
        import json

        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return {"recovered": False, "reason": "unreadable journal, discarded"}
        result: dict[str, Any] = {"recovered": True, "stage": journal.get("stage")}
        if journal.get("stage") == "old_written":
            self.write_note(
                journal["old_rel"], journal["old_before"],
                reason=f"supersede-rollback: {journal['old_id']} -> "
                       f"{journal['new_id']} (interrupted mid-transaction)",
            )
            result["action"] = "rolled_back_old"
        path.unlink(missing_ok=True)
        return result

    def supersede(self, old_id: str, new_id: str, *, reason: str = "") -> dict[str, Any]:
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

        Atomicity: a pending-operation journal at
        ``.brain/supersede-pending.json`` is written before either note write and
        cleared after both succeed. A crash between the two signed writes leaves
        a journal that the NEXT ``supersede`` call rolls back (restores the old
        note, then proceeds) before doing anything else — never a signed
        half-chain (HARDENED:codex).
        """
        self._require_host("supersede notes (writes both sides of a version chain)")
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

        journal_path = self._supersede_journal_path()
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        journal = {
            "stage": "starting", "old_id": old_id, "new_id": new_id,
            "old_rel": old_rel, "new_rel": new_rel,
            "old_before": old_before, "new_before": new_before,
        }
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

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
        journal["stage"] = "old_written"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        new_write = self.write_note(
            new_rel, new_after,
            reason=reason or f"supersede: {old_id} -> {new_id} (new head {new_id})",
        )
        journal_path.unlink(missing_ok=True)

        sync_res = self.sync(drain=False)
        return {
            "old_id": old_id, "new_id": new_id,
            "old_write": old_write, "new_write": new_write,
            "reindexed": {"added": sync_res.get("added", 0), "updated": sync_res.get("updated", 0)},
        }

    def verify_audit(self, *, check_content: bool = False) -> dict[str, Any]:
        # HOST-broker only: verify() derives the public key via the resolved
        # signing key — the VM leg must never resolve a key.
        self._require_host("verify the audit chain (resolves the signing key)")
        res = self.audit.verify()
        if check_content:
            drift = self.audit.content_drift(self.vault)
            res["content_drift"] = drift
            if drift and res["status"] == "ok":
                # signatures fine, but a signed note's bytes changed on disk
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

        result = brief_mod.build_brief(
            index_stats=stats,
            recent_notes=surfaced,
            pending_before_drain=pending_before,
            drain_result=drain_res,
            snapshot_age_hours=age_hours,
            max_recent=max_recent,
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
            index_stats=stats, recent_notes=surfaced, days=days
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
                    f"audit chain status={audit_res.get('status')} "
                    f"({len(audit_res.get('errors', []))} error(s))",
                    "chain tamper/break needs human judgment, never auto-repaired",
                    "inspect the chain errors; re-link from the last-good entry",
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
                "(onnxruntime/tokenizers missing or the e5-small model absent)",
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
                f"audit chain status={audit_res.get('status')} "
                f"({len(audit_res.get('errors', []))} error(s))",
                "chain tamper/break needs human judgment, never auto-repaired",
                "inspect the chain errors; re-link from the last-good entry",
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
        from . import maintenance as maint
        from .graph import build_graph

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
        try:
            link_graph = build_graph(conn)
            built = gmod.build_graph_artifact(conn, self.index.backend, link_graph, today=d)
        except Exception as exc:
            marker_path.write_text(_json.dumps({
                "status": "build_failed", "error": f"{type(exc).__name__}: {exc}",
                "attempted_at": d.isoformat(),
            }, indent=2), encoding="utf-8")
            return {
                "ritual": "graphify", "status": "build_failed", "published": False,
                "error": f"{type(exc).__name__}: {exc}", "marker": str(marker_path),
            }
        duration = _time.monotonic() - t0

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
        scheduled fold that queues a hot-queue entry (HARDENED:codex)."""
        path = self._hot_md_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = f"<!-- idempotency-key: {key} -->"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if marker in existing:
            return False
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(marker + "\n" + entry_md.rstrip("\n") + "\n\n")
        return True

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
        ``None``."""
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
                else:
                    entry["status"] = "failed"
                    entry["failed"] = True
                    entry["consecutive_failures"] = int(prev.get("consecutive_failures", 0)) + 1
                    entry["error"] = error
                state[branch] = entry

            # -- unconditional daily work (sync/drain/publish/brief + recs) --
            if dry_run:
                results["status"] = self.status()
            else:
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
                try:
                    # First pass WITHOUT publish: the self-organization folds
                    # below mutate metadata/paths, and the snapshot must carry
                    # their result — publish happens in the second pass.
                    sync_res = self.sync(drain=True, publish=False)
                    results["sync"] = sync_res
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

            if "digest" in branches:
                try:
                    results["digest"] = self.digest(days=7)
                    curate_res = self.curate(dry_run=dry_run, today=d)
                    promote_res = self.promote_scan()
                    results["curate"] = curate_res
                    results["promote_scan"] = promote_res
                    if not dry_run:
                        if curate_res.get("stale_links") or curate_res.get("revisit_sample"):
                            self._append_hot_once(
                                f"maintain:curate:{d.isoformat()}",
                                maint.render_curation_hot_entry(
                                    curate_res["stale_links"], curate_res["revisit_sample"], d),
                            )
                        if promote_res.get("candidates"):
                            self._append_hot_once(
                                f"maintain:promote-scan:{d.isoformat()}",
                                maint.render_promote_scan_hot_entry(promote_res["candidates"], d),
                            )
                        results["digest_html"] = self.digest_html(days=7, today=d)
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
                        Path(self.vault), post_outcomes, trend_findings, d)
                    notifications = maint.fire_and_mark_notifications(
                        Path(self.vault), candidates, d)
                except Exception:  # noqa: BLE001 — a notify-path failure is cosmetic,
                    pass            # never allowed to fail the maintain run itself.
            results["health_history"] = health_record
            results["health_trend"] = trend_findings
            results["notifications"] = notifications

            if not dry_run:
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

            return {
                "ritual": "maintain", "dry_run": dry_run, "date": d.isoformat(),
                "weekday": d.strftime("%A"), "branches_due": branches,
                "results": results,
                "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
            }
        finally:
            self._release_maintain_lock(lock_path)
