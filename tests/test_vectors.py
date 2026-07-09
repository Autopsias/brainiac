"""DV-04 (2026-07-09): the sqlite-vec ``vec0`` KNN query must specify k as an
``AND k = ?`` MATCH constraint, not a bound ``LIMIT ?``.

Newer sqlite pushes a parameterised LIMIT into vec0's planner, so the LIMIT
form happens to work on a modern host (which is why this stayed hidden). Older
sqlite builds — the Cowork device VM's bundled interpreter — do not, and vec0
raises "A LIMIT or 'k = ?' constraint is required on vec0 knn queries". This
only surfaces once a *real* embedder is present and the vec0 native lib loads
(on the host, vec0 often doesn't load and the brute-force backend is used).
"""
import inspect
import sqlite3

import pytest

from brain.vectors import SqliteVecBackend


def test_search_uses_k_constraint_not_bound_limit():
    # Guards against reverting to the bound-LIMIT form that breaks on old sqlite.
    # Strip comment lines first — the explanatory comment names the old form on
    # purpose, so we check the actual code, not the prose.
    code = "\n".join(
        line for line in inspect.getsource(SqliteVecBackend.search).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "AND k = ?" in code
    assert "ORDER BY distance LIMIT ?" not in code


def test_vec0_knn_returns_k_nearest_when_backend_loads():
    be = SqliteVecBackend()
    if not SqliteVecBackend.available():
        pytest.skip("vec0 native extension not loadable here — brute-force fallback")
    con = sqlite3.connect(":memory:")
    be.setup(con, dim=4)
    be.upsert(con, 0, [1.0, 0.0, 0.0, 0.0])
    be.upsert(con, 1, [0.0, 1.0, 0.0, 0.0])
    be.upsert(con, 2, [0.9, 0.1, 0.0, 0.0])
    res = be.search(con, [1.0, 0.0, 0.0, 0.0], k=2)
    assert len(res) == 2               # k results, not "all rows" or an error
    assert res[0][0] == 0              # the exact match is nearest
    assert res[0][1] >= res[1][1]      # sorted closest-first (similarity desc)
