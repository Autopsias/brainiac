"""Index record projection methods."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _RecordMixin:
    """Index record projection methods."""

    def _note_row(self, rowid: int) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT id,title,classification,zone,path,body,is_latest_version,type"
            " FROM notes WHERE rowid=?",
            (rowid,),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "title": r[1], "classification": r[2],
            "zone": r[3], "path": r[4], "body": r[5], "is_latest_version": r[6] or "",
            "type": r[7] or "",
        }

    @staticmethod
    def _snippet(body: str, n: int = 160) -> str:
        s = " ".join(body.split())
        return s[:n] + ("…" if len(s) > n else "")

