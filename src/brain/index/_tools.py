"""Index tool-query methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _ToolMixin:
    """Index tool-query methods."""

    def grep(
        self, pattern: str, *, k: int = 20, ignore_case: bool = True, regex: bool = False
    ) -> list[dict[str, Any]]:
        """Lexical-first exact/regex scan over note bodies — NO embedding.

        The agent's lexical-first entry point: it never embeds the query, so it
        is the cheap first probe before escalating to :meth:`hybrid_search`.
        Returns note-shaped dicts (filterable by the CLI egress gate) with the
        first matching line as the snippet and a match count.

        Bounded against ReDoS / resource exhaustion (RET-04 hardening):
        ``pattern`` is length-capped (:data:`MAX_GREP_PATTERN_LEN`) before
        compilation, and every match is wall-clock-bounded
        (:data:`GREP_REGEX_TIMEOUT_S`) when the optional `regex` engine is
        installed — see :func:`_grep_bounded_search`.
        """
        if len(pattern) > MAX_GREP_PATTERN_LEN:
            raise GrepPatternError(
                f"grep pattern too long ({len(pattern)} chars; max "
                f"{MAX_GREP_PATTERN_LEN}) — refusing to compile "
                "(ReDoS / resource-exhaustion guard)"
            )
        # M-1: without the `regex` engine, a user-supplied --regex pattern has
        # NO wall-clock bound (stdlib `re` can hang on catastrophic backtracking
        # even under MAX_GREP_PATTERN_LEN, e.g. `(a+)+$`). Refuse explicit regex
        # mode outright on the minimal build rather than silently degrading to
        # an unbounded engine (VM-reachable surface).
        if regex and not self._grep_has_timeout():
            raise GrepPatternError(
                "grep --regex requires the 'regex' engine (pip install "
                "'brainiac-cli[index]') for a bounded match timeout; "
                "the minimal build has no ReDoS-safe regex path"
            )
        _re = self._grep_engine()  # the timeout-capable `regex` engine when available, else stdlib re

        flags = _re.IGNORECASE if ignore_case else 0
        # Multi-word NATURAL-LANGUAGE handling: a literal full-question pattern
        # never matches a line verbatim, so a non-regex multi-token query is
        # treated as OR-of-terms (significant tokens only) and ranked by how many
        # DISTINCT terms a note hits, then total matches. Single-token and
        # explicit --regex patterns keep exact literal/regex behaviour (so the
        # tool's precise-pattern contract — and its tests — are unchanged).
        _STOP = {"the", "a", "an", "of", "to", "is", "are", "was", "were", "what",
                 "which", "who", "and", "or", "for", "on", "in", "about", "que",
                 "qual", "quais", "foi", "sobre", "ele", "ela", "com", "para",
                 "uma", "dos", "das", "no", "na", "em", "se", "de", "do", "da"}
        terms: list[str] = []
        if not regex:
            terms = [t for t in _re.split(r"\W+", pattern, flags=_re.UNICODE)
                     if len(t) > 2 and t.lower() not in _STOP]
        multi = (not regex) and len(terms) > 1
        if multi:
            rxs = [_re.compile(_re.escape(t), flags) for t in terms]
        else:
            try:
                rx = _re.compile(pattern if regex else _re.escape(pattern), flags)
            except _re.error:
                rx = _re.compile(_re.escape(pattern), flags)
            rxs = [rx]
        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path,body FROM notes"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            body = r[5] or ""
            matches = [ln for ln in body.splitlines()
                       if any(self._grep_bounded_search(x, ln) for x in rxs)]
            if not matches:
                continue
            distinct = (
                sum(1 for x in rxs if any(self._grep_bounded_search(x, ln) for ln in matches))
                if multi else 1
            )
            out.append({
                "id": r[0], "title": r[1], "classification": r[2],
                "zone": r[3], "path": r[4],
                "match_count": len(matches),
                "terms_matched": distinct,
                "snippet": self._snippet(matches[0]),
                "source": "grep",
            })
        out.sort(key=lambda d: (-d.get("terms_matched", 1), -d["match_count"], d["id"]))
        return out[:k]

    def bases_query(
        self,
        filters: dict[str, str] | None = None,
        *,
        k: int = 50,
        order_by: str = "updated",
        latest_only: bool = False,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Structured frontmatter query (an Obsidian-Bases-style view) over the
        indexed columns — NO embedding. Filters are exact-match on
        id/title/type/classification/zone/path; unknown keys are ignored. Returns
        note-shaped dicts for the CLI egress gate.

        TMP-02 temporal views (ADR-0003 Ruling 2/8 — the Latest Only / As Of
        Bases):

        - ``latest_only``: excludes any note explicitly retired
          (``is_latest_version: false``). A note that never entered a
          supersession chain has no opinion here and is included (it IS the
          current — only — version of itself).
        - ``as_of``: an ISO date; returns notes valid AT that date under
          valid-time semantics — ``effective_date`` if present, else
          ``document_date``, else ``created`` (fallback chain, per the ADR) —
          excluding anything not yet effective by that date or already
          superseded by then. Composable with ``latest_only`` (as-of naturally
          admits a since-superseded note back in, latest_only would then
          exclude it again — apply them together only if that's the intent;
          the CLI keeps them as independent flags).
        """
        cols = {"id", "title", "type", "classification", "zone", "path",
                "created", "updated"}
        filters = filters or {}
        where, params = [], []
        # FALSE POSITIVE (scanner: string-built SQL / hardcoded_sql_expressions):
        # `key` / `order_col` are only ever interpolated after an explicit
        # `in cols` allowlist check against the fixed column set above -- an
        # unrecognised key/order_by is dropped/defaulted, never reaches the SQL
        # text. Every VALUE (`val`, `k`) is a bound param, never interpolated.
        # See docs/SECURITY_NOTES.md.
        for key, val in filters.items():
            if key in cols:
                where.append(f"{key} = ?")  # nosec B608 - key is allowlisted above
                params.append(val)
        if latest_only:
            where.append("is_latest_version IS NOT 'false'")
        if as_of:
            where.append(
                "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) <= ?"
            )
            params.append(as_of)
            where.append("(superseded_date IS NULL OR superseded_date = '' OR superseded_date > ?)")
            params.append(as_of)
        order_col = order_by if order_by in cols else "updated"
        sql = (
            "SELECT id,title,classification,zone,path,type,updated,is_latest_version FROM notes"
            + (" WHERE " + " AND ".join(where) if where else "")
            + f" ORDER BY {order_col} DESC, id ASC LIMIT ?"  # nosec B608 - order_col is allowlisted above
        )
        params.append(k)
        rows = self.conn.execute(sql, params).fetchall()
        keys = ["id", "title", "classification", "zone", "path", "type", "updated",
                "is_latest_version"]
        return [dict(zip(keys, r)) for r in rows]

    def graph_expand(
        self, seeds: list[str], *, depth: int = 2, k: int = 10, use_ppr: bool = True,
        extra_edges: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """On-demand wikilink-BFS + PPR expansion (RET-03). DISCOVERY-ONLY — the
        derived graph is never authoritative; results carry that flag.
        ``extra_edges`` (GRF-01, optional) folds graphify's INFERRED edges in."""
        from ..graph import graph_expand as _expand

        return _expand(self.conn, seeds, depth=depth, k=k, use_ppr=use_ppr,
                        extra_edges=extra_edges)

    def get(self, note_id: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT id,title,type,classification,zone,path,created,updated,sha256,body,"
            "is_latest_version,superseded_by,previous_version,superseded_date"
            " FROM notes WHERE id=?",
            (note_id,),
        ).fetchone()
        if not r:
            return None
        keys = ["id", "title", "type", "classification", "zone", "path",
                "created", "updated", "sha256", "body",
                "is_latest_version", "superseded_by", "previous_version", "superseded_date"]
        return dict(zip(keys, r))

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path,updated FROM notes "
            "ORDER BY updated DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ["id", "title", "classification", "zone", "path", "updated"]
        return [dict(zip(keys, r)) for r in rows]

    def near_dup(self, *, min_score: float = 0.95, k: int = 5) -> list[dict[str, Any]]:
        """Detect backend-independent near-duplicate note pairs."""
        from ..index_stages.near_duplicates import near_duplicates

        return near_duplicates(
            self,
            min_score=min_score,
            neighbours=k,
            patterns=_boilerplate_patterns(),
            match_pattern=_matches_boilerplate_pattern,
        )

    def stale_wikilink_targets(self) -> list[dict[str, Any]]:
        """Wikilinks whose target vanished or moved to ``archive/`` (AUT-02,
        curation Sunday fold). Reuses the ``graph`` module's derived graph —
        DISCOVERY-ONLY, UNFILTERED (the CLI egress-gates before surfacing)."""
        from ..graph import stale_wikilink_targets as _stale

        return _stale(self.conn)

    def revisit_sample(self, *, today: Any = None, k: int = 10) -> list[dict[str, Any]]:
        """Staleness revisit sample ranked by age x whole-corpus PageRank
        centrality (AUT-02, curation Sunday fold). UNFILTERED — the CLI
        egress-gates before surfacing."""
        import datetime as _dt

        from ..graph import revisit_sample as _revisit

        return _revisit(self.conn, today or _dt.date.today(), k=k)

    def unclassified_notes(self, *, k: int = 100) -> list[dict[str, Any]]:
        """Notes whose ``classification`` is missing/empty or not a recognised
        tier — the curation-lint default-deny finding (no wikilink-graph orphan
        detection here; that stays vault-overlay tooling, see G4 / task-disposition
        row 4). UNFILTERED — note-shaped for the CLI egress gate."""
        from .. import classification as cls

        rows = self.conn.execute(
            "SELECT id,title,classification,zone,path FROM notes ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            if cls.is_default_denied(r[2]):
                out.append({"id": r[0], "title": r[1], "classification": r[2],
                            "zone": r[3], "path": r[4]})
        return out[:k]

    def language_census(self, *, refresh: bool = False, detail: bool = False) -> dict[str, Any]:
        """Which languages this vault holds — DERIVED, never declared.

        Read path (``refresh=False``, ``detail=False``): returns the block
        cached in ``meta`` by the last sync/rebuild, stamped ``stale`` when the
        index content or the profile set has moved since. It never computes on
        a read — ``stats()`` feeds ``brain status``, the morning brief and the
        weekly digest, and none of them should pay a whole-corpus scan.

        Compute path: classify every indexed body through
        :mod:`brain.language` and cache the result. ``detail=True`` adds
        ``by_note`` and is never cached (a triage/translation pass wants it;
        status does not).
        """
        from .. import language as lang_mod

        profiles = lang_mod.load_profiles()
        fingerprint = self.get_meta("vault_fingerprint") or ""
        profiles_fp = lang_mod.profiles_fingerprint(profiles)
        if not refresh and not detail:
            raw = self.get_meta("language_census")
            block = None
            if raw:
                try:
                    block = json.loads(raw)
                except ValueError:
                    block = None
            if isinstance(block, dict):
                block["stale"] = (
                    block.get("vault_fingerprint") != fingerprint
                    or block.get("profiles_fingerprint") != profiles_fp
                )
                return block
            return {
                "status": "not-computed",
                "hint": "the census is derived at index time — run `brain sync` or `brain rebuild`",
            }
        rows = self.conn.execute("SELECT id, body FROM notes").fetchall()
        block = lang_mod.census(
            ((r[0], r[1] or "") for r in rows), profiles=profiles, detail=detail)
        block["vault_fingerprint"] = fingerprint
        block["stale"] = False
        if not detail:
            try:
                self._set_meta("language_census", json.dumps(block, ensure_ascii=False))
                self.conn.commit()
            except sqlite3.Error:
                pass  # read-only snapshot: the value is still returned, just not cached
        return block

    def _refresh_language_census(self) -> dict[str, Any]:
        """Recompute + cache the census after an index write. Never fatal — a
        census failure must not fail a sync or a rebuild."""
        try:
            return self.language_census(refresh=True)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    def stats(self) -> dict[str, Any]:
        c = self.conn
        notes = int(c.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        chunks = int(c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        return {
            "notes": notes,
            "chunks": chunks,
            "languages": self.language_census(),
            "schema_version": self.get_meta("schema_version"),
            "vector_backend": self.get_meta("vector_backend"),
            "embed_model": self.get_meta("embed_model"),
            "embed_dim": self.get_meta("embed_dim"),
            "vault_fingerprint": self.get_meta("vault_fingerprint"),
            "db": str(self.db_path),
        }

