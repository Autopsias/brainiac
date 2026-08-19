"""Sub-steps of the run validator's CORPUS JOIN check (s16).

One function per E-check sub-step of ``check_corpus_join`` (g): the
no-corpus branch (INCONCLUSIVE / owed-FAIL / innocent-DEGRADED) and the
ledger↔corpus join recount. The check function itself stays in
:mod:`brain.cos_runverify` with an unchanged signature; this module never
imports it.
"""
from __future__ import annotations

from typing import Any

from . import config, cos_corpus
from .cos_runverify_checks import DEGRADED, FAIL, INCONCLUSIVE, _MARKER_DISPOSITION, _row

# -- g corpus join -------------------------------------------------------------

def _capture_was_live_by(vault, run_id: str) -> str | None:
    """The oldest corpus this host still holds, when it is no NEWER than
    ``run_id`` — or ``None``.

    One artifact, two exclusions. A corpus dated on or before this run's date
    proves capture was ALREADY RUNNING on this host by then (so the run neither
    predates capture nor ran a bundle that cannot write one), and it proves
    retention has not reached that date (whole files expire oldest-first by the
    date in the name, so the witness would have been deleted before this run's
    corpus). With no corpus on disk at all there is no witness and nothing is
    concluded — the host genuinely cannot tell, which is the degraded case.
    """
    runs = cos_corpus.list_runs(vault)                  # oldest first, [] on error
    oldest = runs[0] if runs else ""
    return oldest if oldest and oldest[:10] <= run_id[:10] else None


def _corpus_missing_row(vault, run_id: str,
                        rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The no-corpus branch: INCONCLUSIVE on an unusable location, FAIL when a
    corpus was owed, DEGRADED when none of the three innocent causes is
    excluded."""
    try:
        exists = cos_corpus.corpus_path(vault, run_id).exists()
    except cos_corpus.CorpusHostOnly as exc:
        return _row("corpus_join", INCONCLUSIVE, str(exc), reexecuted=False)
    except config.HostPathUnsafe as exc:
        return _row("corpus_join", INCONCLUSIVE,
                    f"the capture corpus location could not be proven safe, "
                    f"so the host cannot tell whether one exists for this "
                    f"run: {exc}", reexecuted=False)
    if exists:
        return None
    opened = sum(1 for r in rows if r.get("body_opened"))
    witness = _capture_was_live_by(vault, run_id) if opened else None
    if witness:
        return _row("corpus_join", FAIL,
                    f"{opened} ledger row(s) claim `body_opened: true` and "
                    f"this run wrote NO capture corpus at all. None of the "
                    f"three innocent causes applies: this host was already "
                    f"capturing by this run's date — it still holds the "
                    f"corpus for {witness} — so the run neither predates "
                    f"capture nor ran a bundle that does not write one, and "
                    f"retention has not reached this date either, or "
                    f"{witness} would have gone first. A body claimed open "
                    f"with nothing captured is a read asserted by the "
                    f"ledger and by nothing else — the run-64 shape",
                    reexecuted=True)
    return _row("corpus_join", DEGRADED,
                "not applicable — no capture corpus on disk for this run. "
                "It either predates corpus capture (s06, 2026-08-02), ran "
                "a bundle that does not write one, or is older than the "
                "capture-corpus retention window ($BRAIN_COS_CORPUS_DAYS, "
                "default 30 days) and the nightly deleted it. So the "
                "ledger-corpus join could not be re-executed; this is "
                "never scored as a failure on that account alone",
                reexecuted=False)


def _corpus_join_problems(rows: list[dict[str, Any]], vault, run_id: str
                          ) -> tuple[list[str], list[str], int, int, int]:
    """(uncaptured, opened_no_text, in_scope_n, bodied_n, corpus_n).

    Every in-scope ledger row must resolve to a thread the corpus captured,
    and every row claiming ``body_opened: true`` to a corpus row that carries
    text.
    """
    corpus_rows = cos_corpus.read_corpus(vault, run_id)
    captured: set[str] = set()
    bodied: set[str] = set()
    for r in corpus_rows:
        cid = str(r.get("conversation_id") or "").strip()
        if not cid:
            continue
        captured.add(cid)
        if str(r.get("text") or "").strip():
            bodied.add(cid)

    in_scope = [r for r in rows
                if str(r.get("disposition") or "") != _MARKER_DISPOSITION]
    missing = [str(r.get("conversation_id") or "").strip() or "<no conversation_id>"
               for r in in_scope
               if str(r.get("conversation_id") or "").strip() not in captured]
    opened_no_text = [str(r.get("conversation_id") or "").strip() for r in rows
                      if r.get("body_opened")
                      and str(r.get("conversation_id") or "").strip() not in bodied]
    return missing, opened_no_text, len(in_scope), len(bodied), len(corpus_rows)


def _corpus_missing_thread_row(missing: list[str], in_scope_n: int
                               ) -> dict[str, Any] | None:
    if not missing:
        return None
    return _row("corpus_join", FAIL,
                f"{len(missing)} of {in_scope_n} in-scope ledger row(s) "
                "carry a verdict for a thread this run's own capture "
                "corpus never recorded: "
                + ", ".join(m[:32] for m in missing[:5])
                + (" …" if len(missing) > 5 else "")
                + ". The ledger and the corpus are one record joined on "
                  "`conversation_id` (SKILL.md rule 8); a row on one side "
                  "with nothing on the other is not a coherent pair",
                reexecuted=True)


def _corpus_opened_no_text_row(opened_no_text: list[str]) -> dict[str, Any] | None:
    if not opened_no_text:
        return None
    return _row("corpus_join", FAIL,
                f"{len(opened_no_text)} ledger row(s) claim `body_opened: "
                "true` for a thread whose corpus row carries no text: "
                + ", ".join(m[:32] for m in opened_no_text[:5])
                + (" …" if len(opened_no_text) > 5 else "")
                + ". A body claimed open is a body the corpus should hold "
                  "the text of",
                reexecuted=True)
