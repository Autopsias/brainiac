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

from . import config
from . import frontmatter
from .audit import AuditChain, KeyUnavailable
from .index import BrainIndex, Hit
from .notes import load_note, sha256_text


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
            self.index = BrainIndex()
        if self.role == config.ROLE_VM:
            # No signing surface AT ALL on the VM: the audit chain (and thus
            # resolve_signing_key) is simply not constructed here.
            self.audit = None
        else:
            log = Path(audit_log) if audit_log else (config.index_dir() / "audit_chain.jsonl")
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
        self, filters: dict[str, str] | None = None, *, k: int = 50
    ) -> list[dict[str, Any]]:
        """Structured frontmatter view over indexed columns — no embedding (RET-04)."""
        return self.index.bases_query(filters, k=k)

    def graph_expand(
        self, seeds: list[str], *, depth: int = 2, k: int = 10, use_ppr: bool = True
    ) -> dict[str, Any]:
        """On-demand wikilink-BFS + PPR — DISCOVERY-ONLY (RET-03)."""
        return self.index.graph_expand(seeds, depth=depth, k=k, use_ppr=use_ppr)

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
        staged = _stamp_draft_frontmatter(content, note_id, is_source)
        inbox = self.capture_inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / f"{note_id}.md"
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
                # raw source -> raw/<id>.md ; otherwise a brain note -> resources/.
                if note.type == "source" or note.zone == "raw":
                    rel = f"raw/{note.id}.md"
                else:
                    rel = f"brain/resources/{note.id}.md"
                content = draft.read_text(encoding="utf-8")
                try:
                    self.write_note(rel, content, reason=f"drain-on-invoke promote {draft.name}")
                except KeyUnavailable:
                    skipped.append({"draft": draft.name, "reason": "no-signing-key (fail-closed)"})
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

    def sync(self, *, drain: bool = True, publish: bool = False) -> dict[str, Any]:
        """Incremental index reconcile (IDX-03), draining capture drafts first.

        HOST-broker only (it mutates the index). ``drain`` runs the host capture
        drain before reconciling; ``publish`` additionally republishes the
        read-only snapshot so a VM session's next read sees the just-committed
        note (closing the capture loop). Set ``drain=False`` only for a host
        read-only reconcile."""
        self._require_host("sync (mutate) the index")
        drain_res = self.drain_drafts() if drain else {"promoted": 0, "skipped": 0, "drain": "off"}
        idx_res = self.index.sync(self.vault)
        idx_res["drain"] = drain_res
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

    def status(self, snapshot_dest: str | Path | None = None) -> dict[str, Any]:
        """Report index stats + snapshot generation/age (available on BOTH legs —
        the VM uses it to tell whether its read-only view is fresh or stale, and
        how many drafts are pending)."""
        from .snapshot import snapshot_status

        dest_dir = Path(snapshot_dest) if snapshot_dest else config.snapshot_dir(self.vault)
        out: dict[str, Any] = {"vault": str(self.vault), "role": self.role}
        try:
            out["index"] = self.index.stats()
        except Exception as exc:  # index/snapshot not built yet
            out["index"] = {"error": f"{type(exc).__name__}: {exc}"}
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
            out["live_embedder"] = {
                "model_id": live_id,
                "is_hash_fallback": live_id == "hash-v1",
                "matches_index_metadata": (recorded is None or recorded == live_id),
            }
        except Exception as exc:
            out["live_embedder"] = {"error": f"{type(exc).__name__}: {exc}"}
        out["snapshot"] = snapshot_status(dest_dir)
        out["pending_drafts"] = self._count_pending_drafts()
        return out

    def _count_pending_drafts(self) -> int:
        n = 0
        for ddir in self._draft_sources():
            if ddir.is_dir():
                n += len(list(ddir.glob("*.md")))
        return n

    # -- write verb (HOST-BROKER ONLY; audited; fails closed) ------------
    def write_note(self, rel_path: str, content: str, reason: str = "") -> dict[str, Any]:
        """Write a note to the vault and append a signed audit-chain entry.

        Fails closed in BOTH directions:
        - if no signing key resolves (KeyUnavailable), nothing is written;
        - the chain records the write ATTEMPT first, then the OUTCOME. If the
          file write raises after signing (disk full, permission), a compensating
          ``write_failed`` entry is appended so the chain never claims a write
          that didn't land (F-06). The original exception is re-raised.

        HOST-broker only: refused on the VM leg BEFORE any signing-key
        resolution (the VM never holds the audit key).
        """
        self._require_host("write notes (sign + commit)")
        target = (self.vault / rel_path).resolve()
        if self.vault not in target.parents and target != self.vault:
            raise ValueError(f"write target escapes vault: {rel_path}")
        # Append the signed audit entry FIRST; if signing fails, nothing is written.
        try:
            entry = self.audit.append(
                verb="write", path=rel_path,
                reason=reason or f"write_note {rel_path} sha256={sha256_text(content)[:12]}",
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

    def verify_audit(self) -> dict[str, Any]:
        # HOST-broker only: verify() derives the public key via the resolved
        # signing key — the VM leg must never resolve a key.
        self._require_host("verify the audit chain (resolves the signing key)")
        return self.audit.verify()

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
            nid = str(meta.get("id", "capture"))
            ntype = str(meta.get("type", "note"))
            rel = f"raw/{nid}.md" if ntype == "source" else f"brain/resources/{nid}.md"
            write_res = self.write_note(rel, enforced, reason=reason or f"capture {nid}")
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

    def brief(self, *, max_recent: int = 5, drain: bool = True) -> dict[str, Any]:
        """Generate the morning brief (UX-02).

        Drains pending captures first (HOST only) — making this the guaranteed
        daily drain FLOOR when run as the scheduled task. Always reports the
        pending count BEFORE the drain attempt so a stalled drain is visible
        next morning via the tripwire line.

        VM leg: reports pending count + index stats (read-only view) but cannot
        drain (no signing key).
        """
        from . import brief as brief_mod
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

        snap = snapshot_status(config.snapshot_dir(self.vault))
        age_hours: float | None = None
        if snap.get("snapshot") == "present" and snap.get("age_seconds") is not None:
            age_hours = snap["age_seconds"] / 3600

        return brief_mod.build_brief(
            index_stats=stats,
            recent_notes=recent,
            pending_before_drain=pending_before,
            drain_result=drain_res,
            snapshot_age_hours=age_hours,
            max_recent=max_recent,
        )

    def digest(self, *, days: int = 7) -> dict[str, Any]:
        """Generate the weekly digest (UX-02).

        Shows notes from the past ``days`` days. Available on both host and VM
        legs (read-only; reads from the index/snapshot in use for this role).
        """
        from . import brief as brief_mod

        try:
            stats = self.index.stats()
        except Exception:
            stats = {"notes": 0, "chunks": 0}

        try:
            recent = self.recent(limit=500)
        except Exception:
            recent = []

        return brief_mod.build_digest(
            index_stats=stats, recent_notes=recent, days=days
        )

    # -- maintenance rituals (CUT-03) --------------------------------------
    # check / health / curate / integrity / promote-scan + the `maintain`
    # umbrella. Per docs/cutover/task-disposition.md these are WRITE rituals
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

    def curate(self, *, dry_run: bool = False, k: int = 50) -> dict[str, Any]:
        """curation OVERLAY-ONLY fold (task-disposition.md row 4): only the
        refresh-index sub-step folds to ``sync``; orphan/stale-link/
        contradiction/callout lint stay vault-overlay tooling with NO brain
        equivalent (G4 RETIRE). UNFILTERED unclassified-notes finding — the
        CLI egress-gates it before surfacing."""
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
        return {
            "ritual": "curate", "dry_run": dry_run, "sync": sync_res,
            "unclassified_notes": unclassified,  # UNFILTERED
            "overlay_only_skipped": {
                "orphans": "vault-structure overlay, no brain equivalent (RETIRE)",
                "stale_links": "vault-structure overlay, no brain equivalent (RETIRE)",
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

    def maintain(
        self, *, dry_run: bool = False, today: Any = None,
        min_score: float = 0.95, near_dup_k: int = 5,
    ) -> dict[str, Any]:
        """The umbrella — THE single sanctioned host task (``brain-nightly``,
        persistence-budget.md THE LOCK). Runs ``sync --publish`` + ``brief``
        (skipped under ``dry_run`` — no mutation, no signing), then the
        date-gated branches: Mon->health, Tue->integrity, Sun->digest,
        1st-of-month->graphify (documented only — graph build stays separate
        tooling, task-disposition.md row 7). ``health``/``integrity`` are
        READ-ONLY by construction, so they run for REAL even under
        ``--dry-run`` — only the mutating/signing half (sync/drain/publish) is
        skipped. HOST-broker."""
        from . import maintenance as maint
        import datetime as _dt

        self._require_host("run the maintain umbrella")
        d = today or _dt.date.today()
        branches = maint.maintain_branches(d)

        results: dict[str, Any] = {}
        auto_fixed: list[dict[str, Any]] = []
        action_required: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        if dry_run:
            results["status"] = self.status()
        else:
            sync_res = self.sync(drain=True, publish=True)
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
            snap = sync_res.get("snapshot")
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

        if "health" in branches:
            h = self.health()
            results["health"] = h
            action_required += h["outcomes"]["action_required"]
            blocked += h["outcomes"]["blocked"]

        if "integrity" in branches:
            i = self.integrity(min_score=min_score, k=near_dup_k)
            results["integrity"] = i
            blocked += i.get("blocked", [])
            if i.get("audit_issue"):
                action_required.append(i["audit_issue"])
            if i.get("near_dup_pairs"):
                # near_dup_pairs are UNFILTERED here; `maintain` reports only the
                # raw count (egress applies at the standalone `integrity` verb,
                # which is where a caller actually inspects pair content).
                action_required.append(maint.action_required_item(
                    f"{len(i['near_dup_pairs'])} near-duplicate pair(s) found "
                    f">= {min_score}",
                    "de-dup is a human merge/keep judgment, never auto-merged",
                    "run `brain integrity --json` for the gated pair list and review",
                    "near-dup scan"))

        if "digest" in branches:
            results["digest"] = self.digest(days=7)

        if "graphify" in branches:
            results["graphify"] = {
                "invoked": False,
                "reason": "graphify discovery stays separate tooling "
                          "(task-disposition.md row 7); maintain only documents the date-gate",
            }

        return {
            "ritual": "maintain", "dry_run": dry_run, "date": d.isoformat(),
            "weekday": d.strftime("%A"), "branches_due": branches,
            "results": results,
            "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
        }
