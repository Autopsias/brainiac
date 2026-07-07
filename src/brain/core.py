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

    def status(self, snapshot_dest: str | Path | None = None) -> dict[str, Any]:
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
            out["live_embedder"] = {
                "model_id": live_id,
                "is_hash_fallback": live_id == "hash-v1",
                "matches_index_metadata": (recorded is None or recorded == live_id),
            }
        except Exception as exc:
            out["live_embedder"] = {"error": f"{type(exc).__name__}: {exc}"}
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
        out["maintain_heartbeat"] = self._maintain_heartbeat_summary()
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
        max_tier: str = classification.DEFAULT_MAX_TIER, today: Any = None,
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
        self, *, days: int = 7, max_tier: str = classification.DEFAULT_MAX_TIER,
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
        max_tier: str = classification.DEFAULT_MAX_TIER, candidate_limit: int = 20,
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

        self._require_host("build the graphify discovery graph")
        d = today or _dt.date.today()
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

    def _maintain_heartbeat_summary(self) -> dict[str, Any]:
        """``brain status`` surfacing (HARDENED:premortem) — a heartbeat older
        than 48h on the ``daily`` branch, or any branch with >=2 consecutive
        failures, is flagged. Reads the SAME file `maintain` writes and the
        session-start hook already reads (one file, two consumers)."""
        import datetime as _dt

        state = self._load_maintain_state()
        if not state:
            return {"status": "no-record", "note": "no maintain-state.json yet — brain maintain has not run"}
        today = _dt.date.today()
        branches: dict[str, Any] = {}
        stale, repeated_failures = [], []
        for branch, entry in state.items():
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
        idempotent. HOST-broker."""
        from . import maintenance as maint
        import datetime as _dt

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
                try:
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
                    _mark("daily", True)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "daily branch (sync/brief/recommendations-aging) raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("daily", False, f"{type(exc).__name__}: {exc}")

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

            if "graphify" in branches:
                try:
                    g = self.graphify(force=False, dry_run=dry_run, today=d)
                    g["invoked"] = True
                    results["graphify"] = g
                    if not dry_run and g.get("published") and g.get("candidates"):
                        self._append_hot_once(
                            f"maintain:graphify:{d.isoformat()}",
                            maint.render_graphify_hot_entry(g["candidates"], d),
                        )
                    if g.get("build", {}).get("action_required"):
                        action_required.append(maint.action_required_item(
                            f"graphify build took {g['build']['duration_seconds']}s "
                            f"(> {g['build']['action_required_seconds']}s soft budget)",
                            "the monthly graph build exceeded its 5-minute soft budget",
                            "investigate corpus scale / vector backend before next month's run",
                            "graphify build"))
                    _mark("graphify", True)
                except Exception as exc:
                    blocked.append(maint.blocked_item(
                        "graphify branch raised",
                        f"{type(exc).__name__}: {exc}",
                        "re-run maintain after the underlying error is fixed"))
                    _mark("graphify", False, f"{type(exc).__name__}: {exc}")

            if not dry_run:
                self._save_maintain_state(state)

            return {
                "ritual": "maintain", "dry_run": dry_run, "date": d.isoformat(),
                "weekday": d.strftime("%A"), "branches_due": branches,
                "results": results,
                "outcomes": maint.build_outcomes(auto_fixed, action_required, blocked),
            }
        finally:
            self._release_maintain_lock(lock_path)
