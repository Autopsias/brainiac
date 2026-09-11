"""The five detectors behind the owner-interview lane (INT-01, `interview`).

Every detector is a signal the vault ALREADY computes for another purpose —
the dossier's tensions, the decision-capture scan, the commitment radar, the
linking lane's worklist, curation's revisit sample — turned into one
decidable question each. A detector yields rows lazily, so an expensive one
(only `dossier` needs the embedder) runs only as far as the day's budget asks.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from .interview import (EXPIRE_DAYS, OPTIONS, SKIP, OPEN, blocked,
                        question_key)

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
#: COS mail-thread records: the bulk of `brain/resources/` and never the
#: note an orphan "belongs with".
_MAIL_RECORD_PREFIX = "cosbridge-"


def rel_path(vault: Any, path: str) -> str:
    """A hit's path as the vault-relative form `write_note` takes; an absolute
    path outside the vault comes back unchanged (and later refused there)."""
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(vault).resolve()).as_posix()
        except ValueError:
            return str(path)
    return str(path)


def _row(shape: str, *, evidence: list[dict[str, str]], target: dict[str, str],
         question: str, change: str, today: _dt.date,
         options: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    opts = options if options is not None else OPTIONS[shape]
    return {
        "key": question_key(shape, [e["id"] for e in evidence]),
        "shape": shape, "asked_on": today.isoformat(),
        "expires_on": (today + _dt.timedelta(days=EXPIRE_DAYS)).isoformat(),
        "question": question, "evidence": evidence,
        "options": [{"action": a, "label": lbl} for a, lbl in opts],
        "default": SKIP, "target": target, "change": change,
        "answer": "", "note": "", "status": OPEN,
    }


def note_excerpt(vault: Any, path: str, n: int = 600) -> str:
    """The body of a note, frontmatter stripped, whitespace collapsed."""
    if not path:
        return ""
    try:
        text = (Path(vault) / path).read_text(encoding="utf-8")
    except OSError:
        return ""
    text = _FRONTMATTER_RE.sub("", text, count=1)
    return re.sub(r"\s+", " ", text).strip()[:n]


def note_path(core: Any, note_id: str) -> str:
    try:
        hits = core.bases_query({"id": note_id}, k=1)
    except Exception:  # noqa: BLE001 — a detector never kills the lane
        return ""
    return rel_path(core.vault, str(hits[0].get("path") or "")) if hits else ""


def _tension_candidates(core: Any, state: dict[str, Any], today: _dt.date,
                        limit: int = 2):
    """Latest decisions with NEWER sources against them (``dossier`` tensions).
    The one detector that needs the embedder, so it sweeps at most ``limit``
    decisions a night, most recently updated first."""
    decisions = core.bases_query({"type": "decision"}, k=60, latest_only=True)
    decisions.sort(key=lambda d: str(d.get("updated") or ""), reverse=True)
    swept = 0
    for d in decisions:
        if swept >= limit:
            return
        did = str(d.get("id") or "")
        if not did or blocked(state, "", did, today):
            continue
        swept += 1
        sweep = core.dossier(str(d.get("title") or did), k=12)
        titles = {str(s.get("id")): str(s.get("title") or "")
                  for s in sweep.get("sources") or []}
        mine = next((x for x in sweep.get("decisions") or []
                     if str(x.get("id")) == did), None)
        tensions = list((mine or {}).get("tensions") or [])
        if not tensions:
            continue
        tensions.sort(key=lambda t: str(t.get("date") or ""), reverse=True)
        evidence = [{"id": str(t["id"]), "date": str(t.get("date") or ""),
                     "title": titles.get(str(t["id"]), "")} for t in tensions[:4]]
        newest = evidence[0]
        when = str((mine or {}).get("date") or d.get("updated") or "")[:10]
        yield _row("tension", evidence=evidence, today=today,
                   target={"id": did, "path": rel_path(core.vault, str(d.get("path") or "")),
                           "title": str(d.get("title") or did)},
                   question=(f"You decided [[{did}]] on {when}: "
                             f"{d.get('title') or did}. {len(tensions)} newer "
                             f"source(s) touch it, the latest [[{newest['id']}]] "
                             f"({newest['date']}). Does the decision still stand?"),
                   change=f"[[{did}]] gets an owner-review line, or an owner-update "
                          "section naming what changed.")


def _decision_candidates(core: Any, state: dict[str, Any], today: _dt.date):
    """Fresh sources carrying decision language and no decision note (the
    ``decision_capture_scan`` the daily fold already runs)."""
    from .maintenance_folds_4 import decision_capture_scan  # noqa: PLC0415
    for c in decision_capture_scan(core.index.conn, today):
        sid = str(c.get("id") or "")
        if not sid or blocked(state, question_key("decision", [sid]), sid, today):
            continue
        phrase = str(c.get("phrase") or "").strip()
        yield _row("decision", today=today,
                   evidence=[{"id": sid, "date": str(c.get("date") or ""),
                              "title": sid}],
                   target={"id": sid, "path": note_path(core, sid), "title": sid},
                   question=(f"[[{sid}]] ({c.get('date')}) says \"{phrase}\" and "
                             "no decision note records it. Is this a decision to "
                             "record?"),
                   change="A new decision note is written from the source and "
                          "your words, anchored to it.")


def _late_candidates(core: Any, state: dict[str, Any], today: _dt.date):
    """Commitments past their date (the commitment-spine radar)."""
    from . import spine  # noqa: PLC0415
    for c in spine.radar(core.vault).get("late") or []:
        cid = str(c.get("id") or "")
        if not cid or blocked(state, question_key("late", [cid]), cid, today):
            continue
        who = str(c.get("counterparty") or "someone")
        owed = (f"you owe {who}" if c.get("direction") == "owed_by_me"
                else f"{who} owes you")
        age = int(float(c.get("age_days") or 0))
        text = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()[:140]
        yield _row("late", today=today,
                   evidence=[{"id": cid, "date": str(c.get("due") or "")[:10],
                              "title": text}],
                   target={"id": cid, "path": "", "title": text},
                   question=(f"A commitment {owed} was due {str(c.get('due'))[:10]} "
                             f"({age} days ago) and is still open: \"{text}\". "
                             "What happened?"),
                   change="The commitment is closed, cancelled or moved to the "
                          "date you give.")


def _link_targets(core: Any, query: str, sid: str,
                  limit: int = 3) -> list[tuple[str, str]]:
    """The notes an orphan could belong with: brain-zone notes near it, a
    project or area note first, never a source and never a mail record."""
    ranked: list[tuple[int, str, str]] = []
    for i, h in enumerate(core.hybrid_search(query, k=20)):
        hd = h.to_dict() if hasattr(h, "to_dict") else dict(h)
        hid = str(hd.get("id") or "")
        path = rel_path(core.vault, str(hd.get("path") or ""))
        if (not hid or hid == sid or hd.get("type") == "source"
                or not path.startswith("brain/")
                or hid.startswith(_MAIL_RECORD_PREFIX)):
            continue
        tier = 0 if path.startswith(("brain/projects/", "brain/areas/")) else 1
        ranked.append((tier * 100 + i, hid, str(hd.get("title") or hid)))
    ranked.sort()
    return [(f"link:{hid}", f"Belongs with: {title[:70]}")
            for _, hid, title in ranked[:limit]]


def _orphan_candidates(core: Any, state: dict[str, Any], today: _dt.date,
                       per_night: int = 3):
    """Sources nothing cites, from the linking lane's own worklist, with the
    nearest notes offered as the places they could belong."""
    from .invariant_coverage import LINK_LANE_RELPATH  # noqa: PLC0415
    try:
        lane = json.loads((Path(core.vault) / LINK_LANE_RELPATH)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    tried = 0
    for c in lane.get("candidates") or []:
        if tried >= per_night:
            return
        sid = str(c.get("id") or "")
        if not sid or blocked(state, question_key("orphan", [sid]), sid, today):
            continue
        tried += 1
        title = str(c.get("title") or sid)
        query = (title + " " + note_excerpt(core.vault, str(c.get("path") or ""),
                                            200)).strip()
        links = _link_targets(core, query, sid)
        if not links:
            continue
        yield _row("orphan", today=today, options=links + OPTIONS["orphan"],
                   evidence=[{"id": sid, "date": str(c.get("created") or ""),
                              "title": title}],
                   target={"id": sid, "path": rel_path(core.vault, str(c.get("path") or "")),
                           "title": title},
                   question=(f"[[{sid}]] ({c.get('created')}) landed and nothing "
                             "cites it. Where does it belong?"),
                   change="The note you pick gets a Sources line citing it; "
                          "noise is never asked again.")


def _stale_candidates(core: Any, state: dict[str, Any], today: _dt.date,
                      per_night: int = 2):
    """Central notes gone stale (curation's revisit sample, PageRank-weighted)."""
    taken = 0
    for r in core.index.revisit_sample(today=today, k=10):
        if taken >= per_night:
            return
        nid = str(r.get("id") or "")
        if (not nid or r.get("updated_unparseable")
                or blocked(state, question_key("stale", [nid]), nid, today)):
            continue
        taken += 1
        age = int(float(r.get("age_days") or 0))
        yield _row("stale", today=today,
                   evidence=[{"id": nid, "date": str(r.get("updated") or "")[:10],
                              "title": str(r.get("title") or nid)}],
                   target={"id": nid, "path": rel_path(core.vault, str(r.get("path") or "")),
                           "title": str(r.get("title") or nid)},
                   question=(f"[[{nid}]] is a central note last updated "
                             f"{str(r.get('updated'))[:10]} ({age} days ago): "
                             f"{r.get('title') or nid}. Is it still current?"),
                   change="It gets an owner-review line, or an owner-update "
                          "section with what changed.")


DETECTORS = {
    "tension": _tension_candidates, "decision": _decision_candidates,
    "late": _late_candidates, "orphan": _orphan_candidates,
    "stale": _stale_candidates,
}


__all__ = ["DETECTORS", "note_excerpt", "note_path", "rel_path"]
