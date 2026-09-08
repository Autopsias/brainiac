"""What a PROMPT may carry from the feedback record (FB-01, the render half).

Split out of `feedback.py` at the 500-LOC file-size bound. The seam is real,
not arbitrary: `feedback.py` owns the durable RECORD — where it lives, what
shape a row has, how one is appended and read back. This module owns the
different question of **how much of it a single model message may carry**, and
the answer is never "all of it".

Storing every ruling and rendering every ruling are different things. Rendering
361 thread rulings recreates the measured ~250-row failure (258 verdicts, 24
dropping compliance) that s06's batch cap exists to prevent. So every function
here returns what it KEPT alongside what it EXCLUDED, by count and by key — a
ruling above the ceiling is stored, counted and named, never silently dropped.

Imports flow one way: this module reads `feedback`, never the reverse.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import _parse_ts, _utcnow
from ._learning_config import _env_int
from .feedback import (DEFAULT_RULE_CAP, DEFAULT_THREAD_RENDER_CEILING,
                       DO_NOT_TOUCH_VERDICT, RULE_CAP_ENV, THREAD_CEILING_ENV,
                       WANTED_MORE_VERDICT, read_record, rule_rows, thread_rows)


def _latest_by(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Fold to one row per `key`, newest `ts` winning. Stable for equal stamps:
    the later LINE wins, because an append-only ledger's own order is the only
    tie-break that is always available."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        k = str(row.get(key) or "")
        held = latest.get(k)
        if held is None or str(row.get("ts")) >= str(held.get("ts")):
            latest[k] = row
    return list(latest.values())


def live_rules(rows: list[dict[str, Any]], *,
               now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    """Every standing rule still in force: one row per `rule_key`, latest wins,
    revoked dropped, expired dropped, empty rule text dropped.

    The empty-text clause is the belt under the second row kind: a sheet
    `wrong` mark carries no rule, so its rule row must never reach the judge as
    a blank instruction. It reaches the judge as a THREAD ruling instead.
    """
    at = now or _utcnow()
    keep = []
    for row in _latest_by([r for r in rule_rows(rows) if r.get("rule_key")],
                          "rule_key"):
        if row.get("revoked"):
            continue
        expires = _parse_ts(str(row.get("expires_at") or ""))
        if expires is None or expires <= at:
            continue
        keep.append(row)
    return keep


def ranked_rules(rows: list[dict[str, Any]], *, now: _dt.datetime | None = None,
                 cap: int | None = None) -> dict[str, Any]:
    """The rules block: CONFIRMATIONS then recency, capped, excess NAMED.

    Ranking by recency alone would let one noisy sheet evict a twice-confirmed
    rule, which is precisely backwards — a rule the evidence has agreed with
    twice is the one worth keeping. `confirmations` has no other consumer, so
    without this ordering the field would not work.

    The cap is read HERE, in the pass that renders, not at import: the batch
    size it has to share a message with is decided per run.
    """
    limit = _env_int(RULE_CAP_ENV, DEFAULT_RULE_CAP) if cap is None else cap
    live = sorted(live_rules(rows, now=now),
                  key=lambda r: (int(r.get("confirmations") or 0),
                                 str(r.get("ts"))), reverse=True)
    return {"rendered": live[:limit], "excluded": len(live[limit:]),
            "excluded_keys": [str(r.get("rule_key")) for r in live[limit:]],
            "cap": limit, "live": len(live)}


def latest_thread_rulings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per conversation, latest `ts` winning — the fold BOTH consumers
    share. A thread ruling is a STANDING ruling, so what the owner said last is
    what stands: a later `right` releases a thread an earlier `wrong` held."""
    return _latest_by(thread_rows(rows), "conversation_id")


def _by_verdict(rows: list[dict[str, Any]], verdict: str) -> list[dict[str, Any]]:
    return [r for r in latest_thread_rulings(rows)
            if r.get("verdict") == verdict]


def held_threads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """THE ONE PRODUCER of "which conversations are do-not-touch right now".

    The host guard (`cos_mutate_plan.screen_owner_rulings`) and the prompt block
    (`cos_judge_grounding.rulings_block`) both read this, and that is the point:
    the prompt told the judge the host would refuse a `missed` thread when the
    guard only ever refused `wrong`, so the model was told to leave alone the
    exact thread the owner had asked for MORE action on. Two readers, one fold,
    one verdict test — a divergence test pins it.
    """
    return _by_verdict(rows, DO_NOT_TOUCH_VERDICT)


def wanted_more_threads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `missed` side: the owner said the porter did nothing and should have.

    NOT a do-not-touch and NOT guarded by anything — it asks for more, not less.
    It reaches the judge as its own list so the asymmetry is visible in the
    prompt rather than implied by a heading that covers both.
    """
    return _by_verdict(rows, WANTED_MORE_VERDICT)


def projected_thread_rulings(rows: list[dict[str, Any]], *,
                             ceiling: int | None = None) -> dict[str, Any]:
    """Thread rulings PROJECTED for a prompt: newest first, one per
    conversation, SPLIT BY VERDICT, under a hard rendered-row ceiling.

    The ledger keeps all of them, uncapped and unexpired. This function does
    not delete anything — it decides how many a single model message may carry,
    because rendering 361 of them recreates the ~250-row failure the batch cap
    exists to prevent. Everything above the ceiling is COUNTED and its
    conversation digests returned, so the run report can name what it left out.

    The ceiling bounds the TOTAL rendered thread rows, and the do-not-touch
    list is served first: it is the one with a host guard behind it, so the
    rows most worth spending the budget on are the ones the porter will
    actually refuse. `wanted_more` takes what is left.
    """
    limit = (_env_int(THREAD_CEILING_ENV, DEFAULT_THREAD_RENDER_CEILING)
             if ceiling is None else ceiling)
    def newest(rulings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rulings, key=lambda r: str(r.get("ts")), reverse=True)

    held = newest(held_threads(rows))
    more = newest(wanted_more_threads(rows))
    room = max(limit - len(held[:limit]), 0)
    return {"rendered": held[:limit], "excluded": len(held[limit:]),
            "excluded_digests": [str(r.get("conversation_id_digest"))
                                 for r in held[limit:]],
            "wanted_more": more[:room],
            "wanted_more_excluded": len(more[room:]),
            "wanted_more_digests": [str(r.get("conversation_id_digest"))
                                    for r in more[room:]],
            "ceiling": limit, "stored": len(thread_rows(rows)),
            "active": len(held), "wanted_more_active": len(more)}


def render_budget(vault: Any = None, *, now: _dt.datetime | None = None,
                  rule_cap: int | None = None,
                  thread_ceiling: int | None = None) -> dict[str, Any]:
    """One call, both blocks, plus every number the run report must print.

    The single entry point s05's prompt assembly and s07's sheet both use, so
    "what is live" has ONE producer. `unreadable` rides along: a ledger the
    reader could not fully parse is a finding, not a quiet zero.
    """
    record = read_record(vault)
    rows = record["rows"]
    return {
        "path": record["path"], "unreadable": record["unreadable"],
        "rules": ranked_rules(rows, now=now, cap=rule_cap),
        "thread_rulings": projected_thread_rulings(rows,
                                                   ceiling=thread_ceiling),
    }


__all__ = ['_latest_by', 'live_rules', 'ranked_rules', 'latest_thread_rulings',
           'held_threads', 'wanted_more_threads', 'projected_thread_rulings',
           'render_budget']
