"""Index language-census methods (split out of ``_tools`` for the size limit)."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _LanguageCensusMixin:
    """Derived language census: cached read path, compute path, post-write refresh."""

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
        """Cache the census after an index write (reused while fingerprints match). Never fatal."""
        try:
            c = self.language_census()
            fresh = c.get("stale") is False and "counts" in c and c.get("status") != "not-computed"
            return c if fresh else self.language_census(refresh=True)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
