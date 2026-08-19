"""Index authority-weight methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _ZoneMixin:
    """Index authority-weight methods."""

    def _zone_config_warning(self, var: str, problems: list[str]) -> None:
        """Say so, once per process, on stderr — the same shape the reranker
        fallback uses. A misconfigured opt-in that behaves exactly like an
        unset one is the failure mode this exists to prevent."""
        seen = getattr(self, "_zone_config_warned", None)
        if seen is None:
            seen = set()
            self._zone_config_warned = seen
        if var in seen:
            return
        seen.add(var)
        print(f"brain: WARNING — {var} is misconfigured and was not fully "
              f"applied: {'; '.join(problems)}.", file=sys.stderr)

    def _zone_weight(self, zone: str) -> float:
        """Authority multiplier for a note's zone (see hybrid_search). Curated
        typed zones get a modest boost over voluminous transcript/source zones;
        unknown zones default to 1.0. Override via BRAIN_ZONE_WEIGHTS (JSON).

        Invalid input is REPORTED, not swallowed: unparseable JSON, a non-object
        document, a non-numeric value, or a factor outside
        [`_ZONE_WEIGHT_MIN`, `_ZONE_WEIGHT_MAX`] is dropped with one stderr
        warning naming what was wrong. Confirm what actually applied per hit
        with ``search --explain --json`` (``zone.applied`` / ``zone.factor``).
        """
        weights = getattr(self, "_zone_weights", None)
        if weights is None:
            import json as _json
            import os as _os
            weights = dict(self._DEFAULT_ZONE_WEIGHTS)
            raw = (_os.environ.get("BRAIN_ZONE_WEIGHTS") or "").strip()
            if raw:
                problems: list[str] = []
                configured: object = None
                try:
                    configured = _json.loads(raw)
                except Exception as exc:
                    problems.append(f"not valid JSON ({type(exc).__name__})")
                if configured is not None and not isinstance(configured, dict):
                    problems.append('must be a JSON object like {"brain": 2.5, "raw": 1.0}')
                    configured = None
                for key, value in (configured or {}).items():
                    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                        problems.append(f"{key!r}: {value!r} is not a number")
                        continue
                    try:
                        factor = float(value)
                    except (TypeError, ValueError):
                        problems.append(f"{key!r}: {value!r} is not a number")
                        continue
                    if not math.isfinite(factor) or not (
                            self._ZONE_WEIGHT_MIN <= factor <= self._ZONE_WEIGHT_MAX):
                        problems.append(
                            f"{key!r}: {value!r} is outside the supported range "
                            f"[{self._ZONE_WEIGHT_MIN}, {self._ZONE_WEIGHT_MAX}]")
                        continue
                    weights[str(key)] = factor
                if problems:
                    self._zone_config_warning("BRAIN_ZONE_WEIGHTS", problems)
            self._zone_weights = weights
        try:
            factor = float(weights.get(zone, 1.0))
        except (TypeError, ValueError):
            return 1.0
        return factor if math.isfinite(factor) and factor > 0 else 1.0

    def _zone_scope(self) -> str:
        """`all` | `semantic_only` (default). An UNRECOGNISED value fails safe
        to `semantic_only`, never to `all`: `all` also multiplies lexical and
        exact-identity candidates, which demotes identifier hits and changes
        what the reranker is handed. A typo must not silently opt into the
        wider, riskier behaviour."""
        raw = (os.environ.get("BRAIN_ZONE_SCOPE") or "").strip().lower()
        if not raw:
            return "semantic_only"
        if raw not in self._ZONE_SCOPES:
            self._zone_config_warning(
                "BRAIN_ZONE_SCOPE",
                [f"{raw!r} is not one of {' | '.join(self._ZONE_SCOPES)}; "
                 "falling back to semantic_only"])
            return "semantic_only"
        return raw

    def _resolve_zone(self, zone_col: str, path: str) -> str:
        """Anti-burial authority KEY for a note (PT-02, s05).

        The live migrated index flattens every Johnny-Decimal zone to
        ``brain``/``raw`` in the ``notes.zone`` column (curated typed pages
        all land in ``brain/areas/``; meeting transcripts land in ``raw/``).
        ``_zone_weight`` is keyed on the ORIGINAL (pre-flattening) zone name a
        note carries in its ``source_zone`` frontmatter, so on the flattened
        column it was a no-op — see `docs/eval-bench/pt-diagnosis.md` root
        cause 1.

        This is a RETRIEVAL-TIME-ONLY fix (H23/H11 reversibility gate): the
        migration tool (`tools/apply_live_migration.py`) already writes the
        original zone into each migrated note's frontmatter as
        ``source_zone:`` alongside ``source_path:``. Rather than re-indexing
        to carry that field into the SQLite schema, we read it straight off
        the note's file (identified by the already-indexed ``path`` column)
        at query time — no index/schema/vector change, fully reversible by
        deleting this method and the call site. Brain-native notes created
        after the migration have no ``source_zone`` (they were never
        Johnny-Decimal); those fall back to the flattened ``zone`` column
        unchanged, so this fix only ever *adds* signal, never removes it.

        Only the frontmatter block (first ~2 KB) is read, not the full note
        body — cheap even for large meeting transcripts. Results are cached
        per ``(path, mtime)`` for the life of the index object so a query
        that touches the same candidate note twice (or a session with many
        queries) does not re-read the file repeatedly; a changed mtime
        invalidates the cache entry automatically.

        Kill switch: ``BRAIN_ZONE_SOURCE_MODE=column`` disables this and
        restores the pre-fix behaviour (flattened column only) for rollback
        without a code change.

        MEASURED DEAD ON THE REFERENCE VAULT (2026-08-04, s05 Gate 0): 0 of
        its 2,570 INDEXED notes carry ``source_zone:`` (the scan covers exactly
        the indexed zones — ``brain/`` + ``raw/``, excluding
        ``raw/originals/`` — so it is the same population the ranking sees; an
        earlier "3,589" here counted a different, unnamed set and is wrong).
        The field is absent because the vault was migrated again
        after that field was written, so every lookup here takes the fallback
        and this method contributes no signal there. It is kept, not deleted,
        because the fallback is correct and the field still exists on vaults
        that were migrated once: removing it would silently change ranking for
        them. The consequence for anyone TUNING the prior is the part that
        matters — on a vault with no ``source_zone``, ``BRAIN_ZONE_WEIGHTS``
        must be keyed on the flattened zone names (``brain`` / ``raw``), not
        on Johnny-Decimal zone names, or it is a no-op.
        """
        if os.environ.get("BRAIN_ZONE_SOURCE_MODE", "auto").strip().lower() == "column":
            return zone_col
        cache = getattr(self, "_source_zone_cache", None)
        if cache is None:
            cache = {}
            self._source_zone_cache = cache
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            return zone_col
        key = (path, mtime_ns)
        if key in cache:
            return cache[key] or zone_col
        source_zone: str | None = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(2048)
            if head.startswith("---"):
                end = head.find("---", 3)
                block = head[3:end] if end != -1 else head[3:]
                meta = frontmatter.parse(block)
                sz = meta.get("source_zone")
                if isinstance(sz, str) and sz.strip():
                    source_zone = sz.strip()
        except OSError:
            source_zone = None
        cache[key] = source_zone
        return source_zone or zone_col

    def _today_for_search(self) -> _dt.date:
        """Return the recency-prior date without a syscall per query.

        The cache is rechecked at most once a minute, bounding a midnight
        rollover delay while avoiding a measurable ``date.today`` cost in the
        hot retrieval loop.  A supplied ``BRAIN_NOW`` intentionally bypasses
        it: callers and tests that change the override expect that change to
        apply to the very next search.
        """
        if os.environ.get("BRAIN_NOW", "").strip():
            return _today()
        monotonic_now = time.monotonic()
        cached = self._search_today_cache
        if cached is None or monotonic_now - cached[0] >= 60.0:
            cached = (monotonic_now, _dt.date.today())
            self._search_today_cache = cached
        return cached[1]

