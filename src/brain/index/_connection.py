"""Index connection lifecycle methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _ConnectionMixin:
    """Index connection lifecycle methods."""

    def __init__(
        self,
        db_path: Path | None = None,
        backend: VectorBackend | None = None,
        embedder: Embedder | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path else config.index_path()
        self.backend: VectorBackend = backend or get_backend("auto")
        # $BRAIN_EMBEDDER overrides embedder selection (auto|hash|arctic|catalog);
        # default auto. CI + air-gapped validation force "hash" (offline, no model
        # download), per get_embedder's contract — same one-line swap tests use.
        # None ⇒ resolved LAZILY on first use (the `embedder` property below):
        # constructing the index must never die on a missing embedder, or
        # DV-03's fail-closed ($BRAIN_REQUIRE_REAL_EMBEDDER, defaulted on the
        # VM leg) bites verbs that never embed — capture/draft-capture, grep,
        # bases-query — instead of only the semantic path as documented.
        self._embedder: Embedder | None = embedder
        # Cache the reranker on the index instance so the ONNX session is loaded
        # ONCE, not on every _apply_rerank call. Without this, qwen3-embed's
        # TextCrossEncoder reloads the 573MB ONNX model per query (S11 finding),
        # making rerank-bound eval pathologically slow. The cache is keyed on the
        # resolved model id so a mid-session BRAIN_RERANKER_MODEL change is honoured.
        self._reranker_cache: tuple[str, Any] | None = None
        # Logged once (not per query) when a RESOLVED reranker's .rerank() call
        # itself raises -- distinct from "no model available" (that path never
        # reaches rerank(), since get_reranker() already returned a NoopReranker
        # and applied is already False before any try/except). A real crash
        # here was previously indistinguishable from "reranking made no
        # difference": the skippable contract (RET-02) must still degrade to
        # identity, but doing so silently hid a real bug (BL-02) as a null
        # delta. See the except block in _rerank_impl.
        self._rerank_failure_logged = False
        # BR-03 circuit breaker: a single persistent worker thread that every
        # rerank call is submitted to, so a call can be TIMED OUT from the
        # caller's side (see _rerank_impl). Lazily created on first rerank —
        # constructing the index must never spin up a thread pool no one asked
        # for. Not a process pool (nothing here needs one): a slow ONNX call
        # cannot be killed, only abandoned, and a single background thread is
        # enough to abandon it in.
        self._rerank_executor: "concurrent.futures.ThreadPoolExecutor | None" = None
        # Multi-hop retrieval (RET-06) caches: the wikilink graph and the entity
        # lexicon are both derived from the immutable ``notes`` table, so build
        # them once per index lifetime (not per query). None until first use.
        self._link_graph: Any | None = None
        self._entity_lex: Any | None = None
        # ADR-0008's title-phrase tier verifies candidate titles itself instead
        # of trusting FTS token-OR semantics. Cache both the small immutable
        # projection and a token -> title-record prefilter per index generation:
        # the prefilter only narrows candidates; the contiguous phrase check is
        # still the authority. Rebuild/sync invalidate it before any mutation.
        self._title_phrase_records_cache: tuple[
            list[dict[str, Any]], dict[str, list[dict[str, Any]]]
        ] | None = None
        # Identity and title-phrase *membership* are immutable for one index
        # generation.  Cache them separately from exact-leg ordering: the
        # latter deliberately remains live to zone/env configuration and the
        # source-zone mtime checks in ``_exact_tiebreak``.  This keeps repeated
        # searches cheap without making a configuration change appear stale.
        self._identity_owner_cache: dict[
            str, tuple[frozenset[int], frozenset[int], frozenset[int]]
        ] = {}
        self._title_phrase_match_cache: dict[str, dict[int, dict[str, Any]]] = {}
        # Keyword-exact evidence needs literal checks against title and body.
        # Notes are immutable for an index generation, so cache their normalized
        # search text by rowid and clear it before rebuild/sync mutations.
        self._literal_text_cache: dict[int, tuple[str, str]] = {}
        # ``date.today()`` is a syscall on the supported macOS runtime.  The
        # recency prior needs day-level, not sub-minute, precision, so retain a
        # bounded cache for ordinary wall-clock searches.  ``BRAIN_NOW`` stays
        # uncached to preserve deterministic tests and explicit operator
        # overrides immediately.
        self._search_today_cache: tuple[float, _dt.date] | None = None
        # read_only is the VM-leg posture (S06): the connection is opened
        # ``mode=ro`` so the engine CANNOT open WAL or mutate the index. Any
        # write raises ``sqlite3.OperationalError`` (attempt to write a readonly
        # database) and no ``-wal``/``-shm`` sidecar is ever created.
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None

    @property
    def embedder(self) -> Embedder:
        """The query/index embedder, resolved on FIRST USE, not construction.

        Only the paths that actually embed (search/hybrid-search, rebuild/sync,
        near-dup scoring) ever touch this — so under DV-03's fail-closed policy
        an EmbedderUnavailable raises exactly where the semantic contract is
        exercised, and lexical/draft verbs keep working on a machine with no
        real embedder (the documented DV-03 scope)."""
        if self._embedder is None:
            self._embedder = self._resolve_embedder(os.environ.get("BRAIN_EMBEDDER", "auto"))
        return self._embedder

    @property
    def conn(self) -> sqlite3.Connection:
        from ..index_stages.connection import open_connection

        return open_connection(self)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._rerank_executor is not None:
            # wait=False: a still-running rerank call is abandoned, not
            # awaited — see the comment on _rerank_executor's creation.
            self._rerank_executor.shutdown(wait=False, cancel_futures=True)
            self._rerank_executor = None

