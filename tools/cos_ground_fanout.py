"""The per-thread fan-out stage of `cos_ground.fetch`'s grounding run (D1/D5).

`fetch` owns the run's payload, its refusal labels and its end-of-run verdicts;
this module owns the middle stage — one `ground_one` call per required
conversation id, inside the worker pool, behind the deadline — writing each
result's block entry and class count into the payload as it lands, and keeping
the three tallies (`covered`, `covered_with_content`, `lookup_failed`) the
payload's lists are built from. Import direction is one-way: this module never
imports :mod:`cos_ground`; `ground_one`, the `Brain` caller and the
`TrackedMatters` index all arrive as parameters so a test that patches them on
the parent is still honoured.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def fan_out(ground_one: Callable[..., dict[str, Any]], brain: Any,
            ctx_by_id: dict[str, dict[str, Any]], required: list[str],
            payload: dict[str, Any], *, tenant_domains: list[str], tracked: Any,
            workers: int, deadline: float, started: float
            ) -> tuple[list[str], list[str], list[str], bool]:
    """Ground every required id in parallel; return the three outcome tallies.

    ``(covered, covered_with_content, lookup_failed, exhausted)`` — the fourth
    is True when at least one thread was picked up past the deadline, which the
    caller turns into the `budget-exhausted` ungrounded label. Each result's
    block entry and class counter are written into ``payload`` here, in arrival
    order, exactly as the run's own map records them.
    """
    covered: list[str] = []
    with_content: list[str] = []
    failed: list[str] = []
    exhausted = False

    # THE DEADLINE IS CHECKED WHEN A THREAD IS PICKED UP, not mid-flight: a
    # `brain` call cannot be interrupted, so the real ceiling is the deadline
    # plus one thread's worst case (64s at the internal class). Stated rather
    # than papered over — 6 minutes is an allocation out of the OWA bearer's
    # life, and an allocation that pretends to be exact is the worse lie.
    def work(cid: str) -> dict[str, Any] | None:
        if time.monotonic() - started > deadline:
            return None
        return ground_one(brain, cid, ctx_by_id.get(cid) or {},
                          tenant_domains=tenant_domains, tracked=tracked)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for res in pool.map(work, sorted(required)):
            if res is None:
                exhausted = True
                continue
            payload["blocks"][res["cid"]] = res["entry"]
            payload["classes"][res["class"]] += 1
            if res["entry"]["status"] == "lookup-failed":
                failed.append(res["cid"])
            else:
                covered.append(res["cid"])
                if res["entry"]["status"] == "ok":
                    with_content.append(res["cid"])

    return covered, with_content, failed, exhausted


def failure_reasons(payload: dict[str, Any],
                    failed: list[str]) -> dict[str, int]:
    """`{reason: count}` over the failed ids — reason WORDS only, never content.

    A COUNT WITHOUT ITS REASON IS NOT A FINDING. Run 249 reported "24
    lookup-failed" and stopped there; the reason lived one level down in each
    block and took a separate probe the next day to read (all 24 were
    `timeout`, which is what named the fix). It is counted HERE, beside the
    tallies, rather than read by the nightly's log line: SINK 14 forbids that
    script from opening a grounding block at all, and the bluntness of that
    rule is the point — a block also carries vault text.
    """
    blocks = payload.get("blocks") or {}
    out: dict[str, int] = {}
    for cid in failed:
        word = str((blocks.get(cid) or {}).get("reason") or "unrecorded")
        out[word] = out.get(word, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
