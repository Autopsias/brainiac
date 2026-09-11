"""Index record projection methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ..provenance import header_row_fields


class _RecordMixin:
    """Index record projection methods."""

    def _note_row(self, rowid: int) -> dict[str, Any] | None:
        # FALSE POSITIVE (scanner: string-built SQL): the only interpolation is
        # `_concealment_sql()`, which returns one of two module literals — the
        # column name or `''`. No caller input reaches the SQL text.
        r = self.conn.execute(
            "SELECT id,title,classification,zone,path,body,is_latest_version,type,"
            f"{self._concealment_sql()},{self._frontmatter_sql()}"  # nosec B608
            " FROM notes WHERE rowid=?",
            (rowid,),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "title": r[1], "classification": r[2],
            "zone": r[3], "path": r[4], "body": r[5], "is_latest_version": r[6] or "",
            "type": r[7] or "",
            # M-3b: every Hit is built from this row, so the reader-facing
            # concealment verdict travels with it. `unknown` (never `clean`) is
            # what a note indexed before the column existed says about itself.
            "concealment": stored_verdict(r[8]),
            # PV-02: sender/sent/subject, absent when the note has none.
            "header": header_row_fields(r[9]),
        }

    @staticmethod
    def _snippet(body: str, n: int = 160) -> str:
        s = " ".join(body.split())
        return s[:n] + ("…" if len(s) > n else "")

