"""The broker's tool-registration seam (closed-stacks S02, DESK-01).

One module per family of verbs, each exposing ``register(server, *, core)``.
:func:`load` imports them in order and calls each. s03 adds
``housekeeping.py``, s04 adds ``capture.py``, s05 adds its own — each session
ADDS A FILE and appends one name to :data:`MODULES`, instead of five sessions
editing one ``serve()`` and colliding.

**What this seam guarantees, exactly.** It is not a convention:
:meth:`brain.mcp_adapter.BrokerServer.add_tool` REFUSES any registration made
while :func:`registering` is false, and ``FastMCP.tool()`` delegates to
``add_tool``, so ``@server.tool()`` outside ``load()`` raises rather than
quietly creating a tool nobody reviewed. The SDK has a second public door —
``FastMCP(tools=[...])`` reaches ``ToolManager`` without calling ``add_tool``
(mcp 1.28.1, ``server.py:158`` -> ``:214``) — and ``BrokerServer.__init__``
refuses that too.

**Tools are not the only content surface.** ``FastMCP`` also serves resources
and prompts, bound by ``_setup_handlers`` as SEPARATE ``resources/read`` and
``prompts/get`` handlers that never reach ``call_tool``: a
``@server.resource("brain://note/{id}")`` returning a note body would pass a
tools-only seal, produce no egress tally and leave no SEC-06 row. So
``BrokerServer`` refuses ``add_resource``, ``add_prompt`` and the
``resource()`` decorator (whose TEMPLATED branch calls
``_resource_manager.add_template`` directly, past ``add_resource`` — mcp
1.28.1, ``server.py:613``) on the same terms as ``add_tool``. ``completion()``
is a THIRD such handler and is refused too: it installs
``request_handlers[CompleteRequest]`` (mcp 1.28.1,
``lowlevel/server.py:616``), which serves values without a tally and without
checking that the incoming ``ref`` names anything registered. ``custom_route()``
is refused on weaker grounds — it appends a Starlette route, which binds only on
``sse_app``/``streamable_http_app`` while ``serve()`` runs stdio, so it is a door
that is unreachable today rather than an open one.

**And the flag is not the boundary — the SEAL is.** :func:`registering` is an
ambient ContextVar, so any in-process module can set it (this package's own
tests do). ``build_server`` therefore SEALS the broker once ``load`` returns:
after that, each public door named above refuses regardless of the flag, and
the registered TOOL set is checked once against ``mcp_adapter.TOOLS``. Adversarial review
2026-08-28 found both reviewers making the same point independently — before
the seal, "registration is closed" was a convention wearing a mechanism's
clothes.

Three of those doors — ``resource()``, ``completion()``, ``custom_route()`` —
register nothing when called; each RETURNS a decorator that registers when it
is APPLIED. Round 3 of the same review reproduced the consequence: a decorator
obtained inside ``load()``, retained, and applied after the seal registered
successfully. Those three now re-run the refusal inside the decorator they
return (``mcp_adapter._guard_at_application``), so the check follows the
registration rather than the call; ``tool()`` and ``prompt()`` already did,
because their decorators call ``add_tool``/``add_prompt`` at application time.

**What it does NOT guarantee.** Nothing here inspects what a registered tool
RETURNS. The egress filter lives in the handler bodies
(:mod:`brain.mcp_verbs`); the read record is enforced one layer out, in
``BrokerServer.call_tool``. A tool whose body never reached
``egress.apply_gate`` passes this seam and is caught there instead — an empty
tally makes ``mediate`` raise ``UngatedToolError``, so that call returns NEITHER
the document NOR a record, unless the tool is DECLARED in
``mcp_adapter.BODYLESS_TOOLS``, which is the whole point of declaring it. A
body that DOES call the gate but gates the WRONG content is invisible here:
the tally counts gate CALLS, not what went through them, so such a call is
indistinguishable from a correct one at this layer. What it leaves behind, and
whether it is released at all, is then the same four-outcome question as for
any other call (``mcp_mediation.mediate``) — which is why nothing here may
promise it is recorded. Interception proves the RECORD, not the FILTER.
Adding a verb here means routing it through ``mcp_adapter.dispatch`` and
shipping its own gating test.

**Nor is it a boundary against code already running in the broker process.**
The residuals below are PRIVATE handles, not public methods. That no public
method is missing from the CLASSIFICATION is what
``test_every_public_fastmcp_method_is_classified`` asserts: it walks
``dir(FastMCP)`` and reds until somebody files each name in one of five
buckets, and it checks the refused-by-override bucket against the overrides
``BrokerServer`` actually defines. WHICH bucket a name belongs in is a human
judgement recorded in that test's ``_FASTMCP_PUBLIC_SURFACE`` table — a name
filed under ``read_only`` that can in fact serve content is a residual no test
here catches. Reading the SDK is still the work; the test only makes skipping
it loud. None of the residuals below is reachable by a remote MCP client:

* ``server._tool_manager.add_tool`` and ``server._resource_manager`` reach the
  served surface without calling ``BrokerServer.add_tool``/``add_resource``, and
  the ``registered == TOOLS`` check runs once, in ``build_server``, so a later
  hand registration is never rechecked.
* ``server._mcp_server.call_tool(...)`` rebinds the ``tools/call`` request
  handler, bypassing ``BrokerServer.call_tool`` and therefore ``mediate``.
* When a mediated handler RAISES, ``mediate`` flushes the record inside
  ``contextlib.suppress(Exception)`` and discards the outcome, so a gated read
  that then raises can leave no SEC-06 row and no signal. Deliberate: no content
  reaches the client on that path, and a failed read must not be reported to the
  caller as a logging failure (``mcp_mediation.mediate``).
"""
from __future__ import annotations

import contextvars as _contextvars
import importlib
from typing import Any

#: Load order. Append, never insert: a client's tool list is ordered by
#: registration and the Cowork skills read it top-down.
MODULES: tuple[str, ...] = ("core_read", "retrieval", "housekeeping", "capture")

_REGISTERING: _contextvars.ContextVar[bool] = _contextvars.ContextVar(
    "brain_mcp_seam_registering", default=False,
)


def registering() -> bool:
    """True only inside :func:`load`. The gate ``BrokerServer.add_tool`` reads."""
    return _REGISTERING.get()


def load(server: Any, *, core: Any) -> list[str]:
    """Register every seam module's tools onto ``server``. Returns the names loaded."""
    token = _REGISTERING.set(True)
    try:
        loaded: list[str] = []
        for name in MODULES:
            module = importlib.import_module(f"{__name__}.{name}")
            module.register(server, core=core)
            loaded.append(name)
        return loaded
    finally:
        _REGISTERING.reset(token)
