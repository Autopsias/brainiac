"""Shared sampling cap for every corpus-invariant metric row."""
from __future__ import annotations

SAMPLE_CAP = 10


def _supersession_families(rows: list, info: dict) -> dict[str, list[str]]:
    """Connected components over the indexed supersession links, by root id."""
    parent: dict[str, str] = {nid: nid for nid in info}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked: set[str] = set()
    for nid, _body, sup_by, prev in rows:
        for other in (sup_by, prev):
            o = str(other or "")
            if not o or o not in info or o == nid:
                continue
            linked.add(str(nid))
            linked.add(o)
            ra, rb = find(str(nid)), find(o)
            if ra != rb:
                parent[ra] = rb

    families: dict[str, list[str]] = {}
    for nid in linked:
        families.setdefault(find(nid), []).append(nid)
    return families
