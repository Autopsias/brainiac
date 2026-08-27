"""One definition of "this note is retired", shared by search and bases-query.

Two rules, and the second is the one that was missing.

1. A note a supersede chain retired says so itself (``is_latest_version:
   false``).
2. A **deliverable marker** is a POINTER, not a document. The drop lane writes
   ``brain/resources/<payload-id>-deliverable.md`` beside each archived payload
   (``ingest/deliverables.py``), carrying ``source: [[raw/<payload-id>]]`` and
   NO version state of its own. So the payload's chain is the marker's chain:
   a marker whose payload is retired is retired.

Without rule 2 every historical marker stays permanently "current", and each
one's title is a near-exact phrase match for any query about that family.
Measured 2026-08-25 on the reference vault (2 956 notes): one deliverable
family carried a complete v1->v58 chain on its raw payloads and 41 unversioned
markers; searching that family's plain-language name returned 6 markers for
retired v42-v55 in the top 10 and the current v58 nowhere. Retiring the
payloads had made the ranking WORSE, because the markers flooded the slots the
filter had just vacated.

Derived here rather than stamped into the marker's frontmatter on purpose. A
stamped copy of a payload's state is correct only until the payload is
superseded again, and this vault already carries notes whose indexed
``is_latest_version`` contradicts the file on disk — a second copy of derived
state is a second thing that can drift.

A marker that carries an EXPLICIT value keeps it. Only an unversioned marker
inherits, so this can never override a stamp somebody meant.
"""

from __future__ import annotations

#: The drop lane's anchor-id suffix (``ingest.deliverables._anchor_id``).
MARKER_SUFFIX = "-deliverable"

#: SQL truth-test for "retired". Callers MUST alias the notes table ``n``.
#: Interpolates only module constants — no caller input reaches the SQL text.
RETIRED_PREDICATE = f"""(
    LOWER(COALESCE(n.is_latest_version, '')) = 'false'
    OR (
        COALESCE(n.is_latest_version, '') = ''
        AND n.id LIKE '%{MARKER_SUFFIX}'
        AND EXISTS (
            SELECT 1 FROM notes AS payload
            WHERE payload.id = substr(n.id, 1, length(n.id) - {len(MARKER_SUFFIX)})
              AND LOWER(COALESCE(payload.is_latest_version, '')) = 'false'
        )
    )
)"""


def retired_ids(conn, ids) -> set[str]:
    """The subset of ``ids`` this engine calls retired.

    Same rule as the ranker, for callers that hold note ids rather than
    rowids — ``dossier`` over-fetches with ``include_retired=True`` so it can
    report how much version noise it absorbed, and then has to apply the rule
    itself. Reading its own ``is_latest_version`` is what let every unversioned
    deliverable marker through.
    """
    ids = list(ids)
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    return {
        row[0]
        for row in conn.execute(
            f"SELECT n.id FROM notes AS n WHERE n.id IN ({placeholders}) "  # nosec B608 — placeholders + module constant only
            f"AND {RETIRED_PREDICATE}",
            tuple(ids),
        )
    }
