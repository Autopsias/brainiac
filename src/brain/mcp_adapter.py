"""Optional MCP facade for the read-only Chat tab transport.

The adapter keeps the public verb table and trust gate stable while the
individual read bodies live in :mod:`brain.mcp_verbs`.

TWO SEAMS, TWO DIFFERENT GUARANTEES (closed-stacks S02, DESK-01)
---------------------------------------------------------------
Do not read either one as doing the other's job.

1. **The registration seam** — :mod:`brain.mcp_tools`. Each module exposes
   ``register(server, *, core)``; :func:`brain.mcp_tools.load` imports them and
   calls each in turn. It exists so s03/s04/s05 each ADD A FILE instead of all
   editing ``serve()``. On its own a seam guarantees nothing: any code calling
   ``@server.tool()`` directly would escape it. So :class:`BrokerServer`
   overrides ``add_tool`` — which is where ``FastMCP.tool()`` and
   ``FastMCP.add_tool()`` both land — and REFUSES a registration made outside
   ``load()``. It also refuses the ``tools=`` constructor argument, which is a
   SECOND door: ``FastMCP.__init__`` passes that list straight to
   ``ToolManager`` without calling ``add_tool`` (mcp 1.28.1, ``server.py:158``
   -> ``:214``). ``add_resource``, ``resource()`` and ``add_prompt`` are
   refused the same way: ``resources/read`` and ``prompts/get`` are separate
   protocol handlers that never reach ``call_tool``, so a resource returning a
   note body would be a content surface with no tally and no SEC-06 row.
   ``completion()`` is refused for exactly that reason: it installs a THIRD
   such handler, ``request_handlers[CompleteRequest]``, which is content-
   capable and equally unmediated. ``custom_route()`` is refused for a weaker
   one — it appends a Starlette route, which only binds on ``sse_app`` /
   ``streamable_http_app`` while ``serve()`` runs stdio, so it is a door that
   is unreachable today rather than an open one; the guard is three lines.

   ``resource()``, ``completion()`` and ``custom_route()`` register nothing
   when CALLED — each returns a decorator that registers when it is APPLIED,
   so refusing in the method body alone guarded only the moment the decorator
   was obtained. Round 3 of the 2026-08-28 review reproduced a decorator
   retained across the seal and applied after it, which registered.
   :func:`_guard_at_application` now re-runs the refusal inside the decorator
   those three return.

   Which public methods those are is NOT decided by this paragraph. It is
   pinned by ``test_every_public_fastmcp_method_is_classified``, which walks
   ``dir(FastMCP)`` and fails when the SDK grows an attribute nobody has
   CLASSIFIED — because prose claiming an enumeration is complete has been
   wrong here twice. It forces the reading; it does not do it, so a name
   filed in the wrong bucket still passes. Beyond the public surface sit the
   private handles
   (``server._tool_manager`` / ``server._resource_manager`` by hand,
   ``server._mcp_server.call_tool``, which rebinds the ``tools/call``
   handler); each needs code already running inside the broker process, and a
   remote MCP client has none of them.

2. **The mediation boundary** — :meth:`BrokerServer.call_tool`, wrapping
   :func:`brain.mcp_mediation.mediate` (that module holds the tally reset, the
   fail-closed record and the four outcomes; every public name there is
   re-exported here, so ``mcp_adapter.mediate`` and
   ``mcp_adapter.BODYLESS_TOOLS`` are unchanged addresses — a BINDING, not an
   alias: ``dispatch`` reads the name in THIS module, so patching
   ``mcp_mediation.BODYLESS_TOOLS`` alone would not change what it sees).
   Measured on the installed package (``mcp`` 1.28.1, the official Python SDK)
   on 2026-08-27:
   ``FastMCP._setup_handlers`` binds ``self.call_tool`` as THE ``tools/call``
   handler, and it is the single method every tool invocation routes through
   regardless of how or when the tool was registered. The bundled FastMCP has
   no tool-call middleware — the ``Middleware`` importable from
   ``mcp.server.fastmcp.server`` is ``starlette.middleware.Middleware`` (HTTP),
   and ``Middleware.on_call_tool`` belongs to the standalone ``fastmcp``
   package, which is not installed and would be a new dependency. So the
   override is the mechanism: it resets the per-call egress tally and flushes
   the SEC-06 read record FAIL-CLOSED around every call.

**What the mediation boundary does NOT do: it does not filter.** Filtering
lives in the CLI-backed handler bodies (:mod:`brain.mcp_verbs`, which route
through ``egress.apply_gate``). Interception proves the RECORD, not the FILTER.
Two different holes follow from that, and only one of them is closed here. A
body that never reaches the gate at all is caught: its tally is empty, so
``mediate`` raises :class:`UngatedToolError` — no document, no record — unless
the tool is DECLARED in :data:`BODYLESS_TOOLS`, which is what declaring it
means. A body that DOES gate but gates the WRONG content is not caught, and
cannot be: the tally counts gate CALLS, not what went through them, so such a
call is indistinguishable from a correct one here. It is then recorded or not
by exactly the four outcomes that govern every other call — which is why "it is
recorded" is not something this layer may promise about it. So the honest
statement is: every registered tool came through the seam, and no call returns
having skipped the gate unless it was declared bodyless. Whether a seam
module's body gates the RIGHT content is covered by its own test and by S06's
whole-surface property tests. What a returning call leaves BEHIND is a separate
question with four answers, in :func:`brain.mcp_mediation.mediate`.
"""
from __future__ import annotations

import functools as _functools
from typing import Any

from . import classification as cls
from . import config
from .core import BrainCore
from .mcp_mediation import (  # re-exported: the mediation boundary lives there
    BODYLESS_TOOLS,
    ReadRecordError,
    UngatedToolError,
    in_mediated_call,
    mediate,
    read_log_regime,
)
from .mcp_capture_verbs import CaptureRecordError, dispatch_capture
from .mcp_housekeeping_verbs import dispatch_alerts, dispatch_exceptions
from .mcp_verbs import (
    DEFAULT_EGRESS_CEILING_TIER,
    EGRESS_CEILING_ENV_VAR,
    VM_READ_ALIASES,
    VM_WRITE_ALIASES,
    _capture_rerank_metadata,
    _clamp_max_tier,
    _egress_ceiling_tier,
    _filtered,
    _variant_queries,
    dispatch_bases_query,
    dispatch_diagnose,
    dispatch_dossier,
    dispatch_graph_expand,
    dispatch_grep,
    dispatch_note,
    dispatch_recent,
    dispatch_search,
    dispatch_vault_languages,
)

__all__ = [
    "READ_TOOLS", "WRITE_TOOLS", "TOOLS", "dispatch", "serve", "build_server",
    # "BrokerServer" is served by __getattr__ below, not defined at module
    # level: it subclasses FastMCP from the optional `mcp` extra, and defining
    # it eagerly would make this module — and so `dispatch()` — unimportable on
    # a minimal install. Naming it in __all__ would be an F822 undefined-name.
    "broker_server_class", "ReadRecordError", "UnmediatedToolError", "mediate",
    "UngatedToolError", "BODYLESS_TOOLS", "read_log_regime", "in_mediated_call",
    "CaptureRecordError",
    "DEFAULT_EGRESS_CEILING_TIER", "EGRESS_CEILING_ENV_VAR",
    "_capture_rerank_metadata", "_clamp_max_tier", "_egress_ceiling_tier",
    "_filtered", "_variant_queries",
]

#: MCP tool names a client may call for a READ. A name here is a name a client
#: can type; ``brain bases-query`` is served as ``bases_query``.
#:
#: ``alerts``/``exceptions``/``inbox`` (S03, DESK-03) are the three housekeeping
#: verbs — status, never note content, so they take no ``max_tier`` — that let
#: a Cowork session still answer "what needs me?" once it can no longer read
#: `.brain/` files itself.
READ_TOOLS = (
    "search", "hybrid_search", "get", "read", "recent", "bases_query",
    "dossier", "vault_languages", "grep", "graph_expand", "diagnose",
    "alerts", "exceptions", "inbox",
)

#: THE READ-ONLY INVARIANT, DECIDED ONCE (S02/DESK-01). Both guards below were
#: keyed on "is this a read", with the read set doing double duty as the
#: allow-list. s04's ``capture`` is a QUASI-WRITE (AGENTS.md §5), so it cannot
#: join :data:`READ_TOOLS` without silently deleting the distinction the whole
#: trust split rests on — and it must not be refused by a guard whose error
#: string says "non-read tool" either.
#:
#: The shape: a SECOND set, and a role gate that BRANCHES on it rather than
#: folding it into one allow-list — ``dispatch`` tests ``VM_WRITE_ALIASES``
#: first and takes the write path with its own provenance/staging rules, then
#: tests ``VM_READ_ALIASES``. s04 (DESK-04) is the first member: ``capture``,
#: which stages an unsigned draft host-side (see :mod:`brain.mcp_capture_verbs`)
#: rather than folding into the read allow-list, which is exactly the quiet
#: drop this note exists to prevent.
WRITE_TOOLS: tuple[str, ...] = ("capture",)

#: What the broker exposes at all. Never the stop list: the 28 verbs the s01
#: survey scores ``host_only_never`` (``write``, ``rebuild``, ``maintain``,
#: ``ingest``, ``verify-audit``, ``sync``, ``snapshot``, ``anchor``, …) stay
#: host-broker privileges and must never be added here to close a "gap".
TOOLS = READ_TOOLS + WRITE_TOOLS


class UnmediatedToolError(RuntimeError):
    """A tool was registered outside :func:`brain.mcp_tools.load`."""


#: The CLI verb an MCP tool name stands for. Only ``dispatch_search`` reads its
#: ``tool`` argument, and it passes it straight into the query log as ``mode``,
#: which ``querylog_status._validate_record_header`` accepts only as one of
#: ``{"search", "hybrid-search", "dossier"}`` — so an underscored MCP tool name
#: reaching it would break ``brain eval replay`` on the first line it wrote.
_CLI_VERB = {
    "hybrid_search": "hybrid-search",
    "graph_expand": "graph-expand",
    "bases_query": "bases-query",
    "vault_languages": "vault-languages",
}

#: The MCP tool name a CLI-spelled alias stands for — ``_CLI_VERB`` inverted.
#: :data:`BODYLESS_TOOLS` is MCP-named, so an alias has to be normalised back
#: before it is looked up there.
_MCP_NAME = {cli: mcp for mcp, cli in _CLI_VERB.items()}

_HANDLERS = {
    "search": dispatch_search,
    "hybrid-search": dispatch_search,
    "hybrid_search": dispatch_search,
    "get": dispatch_note,
    "read": dispatch_note,
    "recent": dispatch_recent,
    "dossier": dispatch_dossier,
    "vault_languages": dispatch_vault_languages,
    "vault-languages": dispatch_vault_languages,
    "bases-query": dispatch_bases_query,
    "bases_query": dispatch_bases_query,
    "grep": dispatch_grep,
    "graph-expand": dispatch_graph_expand,
    "graph_expand": dispatch_graph_expand,
    "diagnose": dispatch_diagnose,
    "alerts": dispatch_alerts,
    "exceptions": dispatch_exceptions,
    "inbox": dispatch_exceptions,  # alias — see dispatch_exceptions' docstring
    "capture": dispatch_capture,
}


def dispatch(
    tool: str,
    args: dict[str, Any],
    *,
    core: BrainCore | None = None,
    vault: str | None = None,
) -> dict[str, Any]:
    """Dispatch one verb after enforcing the VM trust boundary."""
    role = getattr(core, "role", config.role()) if core is not None else config.role()
    if role == config.ROLE_VM:
        # BRANCHED, not keyed on the union. Adversarial review 2026-08-28: a
        # single ``tool not in (READ | WRITE)`` test would give an s04
        # quasi-write byte-identical treatment to ``get`` at the one place the
        # host/VM trust split is actually enforced (AGENTS.md §5). The write
        # branch below is now DEFINED (s04, DESK-04): a name in
        # ``VM_WRITE_ALIASES`` falls through to the same handler dispatch as
        # a read, but arrived via a SEPARATE membership test, so a future
        # write-adjacent verb still cannot join the read allow-list quietly —
        # it has to earn its own line here, same as this one did.
        if tool in VM_WRITE_ALIASES:
            pass
        elif tool not in VM_READ_ALIASES:
            raise ValueError(
                f"role=vm may not dispatch host-only tool {tool!r}; "
                f"the broker exposes reads {READ_TOOLS} and writes {WRITE_TOOLS}"
            )
    core = core or BrainCore(vault=vault)
    max_tier = _clamp_max_tier(str(args.get("max_tier", cls.DEFAULT_MAX_TIER)))
    handler = _HANDLERS.get(tool)
    if handler is None:
        raise ValueError(
            f"unknown tool {tool!r}; the broker exposes reads {READ_TOOLS} "
            f"and writes {WRITE_TOOLS}"
        )
    verb = _CLI_VERB.get(tool, tool)
    if in_mediated_call():
        # Already inside BrokerServer.call_tool: it owns the tally and the
        # record for this call. Resetting here would zero counts it is
        # accumulating and would write a second row for one call.
        return handler(verb, args, core=core, max_tier=max_tier)
    # SEC-06: the bridged surface logs like the CLI does. This is the leg a
    # Cowork session reaches through Claude Desktop, so leaving it unlogged
    # would leave exactly the caller the pentest was about with no record.
    # Direct callers (tests, the s01 probes, any in-process use) get the same
    # guarantee the broker gives.
    with mediate(
        vault=getattr(core, "vault", None), role=role, cmd=f"mcp:{tool}",
        max_tier=max_tier,
        expects_gate=_MCP_NAME.get(tool, tool) not in BODYLESS_TOOLS,
    ):
        return handler(verb, args, core=core, max_tier=max_tier)


def build_server(core: BrainCore, *, name: str = "brain") -> Any:
    """Construct the broker, load every seam module, then SEAL registration."""
    import sys as _sys

    from . import mcp_tools

    role = getattr(core, "role", None) or config.role()
    vault = getattr(core, "vault", None)
    server = broker_server_class()(name, brain_vault=vault, brain_role=role)
    mcp_tools.load(server, core=core)

    # SEALED. Until 2026-08-28 the only guard was the ``registering()``
    # ContextVar, an AMBIENT FLAG any in-process module can set — the seam's own
    # test flips it. After load there is no legitimate registration left, so all
    # six overridden doors — ``add_tool``, ``add_resource``, ``resource()``,
    # ``add_prompt``, ``completion()`` and ``custom_route()`` — refuse from here
    # on, at APPLICATION time for the three that return a decorator, and forging
    # the flag no longer helps.
    #
    # WHAT THE SEAL BUYS, exactly: it catches LATE registration by in-repo code
    # — s03/s04/s05 adding a surface outside their seam module, by hand or by
    # accident. It is NOT a boundary against code already executing inside the
    # broker process: ``server._tool_manager.add_tool`` reaches the served list
    # without ever calling ``BrokerServer.add_tool``, and
    # ``server._mcp_server.call_tool`` rebinds the ``tools/call`` handler past
    # ``BrokerServer.call_tool``. Both are past the public API and neither is
    # reachable by a remote MCP client (adversarial review 2026-08-28).
    server._brain_registration_sealed = True

    registered = {t.name for t in server._tool_manager.list_tools()}
    if registered != set(TOOLS):
        raise UnmediatedToolError(
            f"broker registered {sorted(registered)}, expected {sorted(TOOLS)}"
        )
    print(f"brain-mcp: {read_log_regime(vault, role)}", file=_sys.stderr)
    return server


def serve(vault: str | None = None) -> None:  # pragma: no cover - transport glue
    """Run the optional stdio MCP server for the Chat tab."""
    build_server(BrainCore(vault=vault)).run()


def _refuse_unseamed(server: Any, kind: str, obj: Any) -> None:
    """Refuse a registration that is late (sealed) or made outside the seam."""
    from . import mcp_tools

    name = getattr(obj, "name", None) or getattr(obj, "__name__", obj)
    what = f"{kind} {name!r}"
    if getattr(server, "_brain_registration_sealed", False):
        raise UnmediatedToolError(
            f"{what} was registered after build_server() sealed this broker; "
            "the served surface is fixed once the seam has loaded"
        )
    if not mcp_tools.registering():
        raise UnmediatedToolError(
            f"{what} was registered outside brain.mcp_tools.load(); add it to a "
            "seam module's register() instead — a surface registered by hand is "
            "one nobody reviewed for the egress gate"
        )


def _guard_at_application(server: Any, kind: str, obj: Any, decorator: Any) -> Any:
    """Re-run :func:`_refuse_unseamed` when ``decorator`` is APPLIED.

    ``resource()``, ``completion()`` and ``custom_route()`` register nothing
    themselves — each RETURNS a decorator, and the registration happens when
    that decorator is applied to a function. Checking only in the method body
    therefore guards the moment the decorator is OBTAINED. Reproduced
    2026-08-28 (adversarial review, round 3): a decorator obtained inside
    ``load()``, retained, and applied after ``build_server`` set
    ``_brain_registration_sealed`` put a template in
    ``_resource_manager._templates``, a ``CompleteRequest`` handler in
    ``_mcp_server.request_handlers`` and a route in
    ``_custom_starlette_routes`` — all on a sealed broker.

    ``tool()`` and ``prompt()`` need no equivalent: their decorators call
    ``self.add_tool`` / ``self.add_prompt`` at application time, so the
    overrides already run then.

    The wrapper forwards ``*args, **kwargs`` and carries ``functools.wraps``
    because a guard is not allowed to change the contract it guards. The SDK's
    inner decorators do not agree on a parameter name — ``FastMCP.resource``
    calls it ``fn``, lowlevel ``completion()`` and ``FastMCP.custom_route``
    call it ``func`` — so an earlier ``def guarded(fn)`` raised ``TypeError``
    on the legal keyword application ``deco(func=handler)`` for two of the
    three doors, and replaced ``__name__``/``__wrapped__``/the signature for
    all three. Reproduced 2026-08-28 (adversarial review, round 4); every test
    then in the suite applied positionally, which is why none of them saw it.
    """
    @_functools.wraps(decorator)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        _refuse_unseamed(server, kind, obj)
        return decorator(*args, **kwargs)

    return guarded


def _make_broker_server() -> type:
    """Define :class:`BrokerServer` against the optional ``mcp`` dependency.

    ``mcp`` is an extra, so the class cannot be defined at import time without
    making the whole module unimportable on a minimal install. Built once and
    cached in the module global below.
    """
    from mcp.server.fastmcp import FastMCP

    class BrokerServer(FastMCP):  # noqa: D401
        """The FastMCP the broker actually serves.

        Two kinds of override, explained once in this MODULE's docstring
        rather than restated here: the registration doors REFUSE anything not
        from the seam, and ``call_tool`` MEDIATES every call. WHICH names are
        doors is a human judgement, recorded in the
        ``_FASTMCP_PUBLIC_SURFACE`` table beside
        ``test_every_public_fastmcp_method_is_classified``: that test forces
        every public name into a bucket, so a name nobody looked at reds the
        suite — a name looked at and filed wrongly does not.
        """

        def __init__(
            self, *args: Any, brain_vault: Any = None, brain_role: str | None = None,
            **kwargs: Any,
        ) -> None:
            # THE SECOND DOOR, closed. ``FastMCP.__init__`` hands ``tools=``
            # STRAIGHT to ``ToolManager(tools=...)`` without ever calling
            # ``add_tool`` (mcp 1.28.1, ``server.py:158`` -> ``:214``), so the
            # override below never sees it. Refused, not routed through the
            # seam: a constructor list is not a seam module.
            if kwargs.get("tools"):
                raise UnmediatedToolError(
                    "FastMCP(tools=[...]) bypasses add_tool and therefore the "
                    "brain.mcp_tools seam; register through a seam module's "
                    "register() instead"
                )
            self._brain_vault = brain_vault
            self._brain_role = brain_role or "host"
            super().__init__(*args, **kwargs)

        def add_tool(self, fn: Any, *args: Any, **kwargs: Any) -> None:
            _refuse_unseamed(self, "tool", fn)
            super().add_tool(fn, *args, **kwargs)

        def add_resource(self, resource: Any) -> None:
            _refuse_unseamed(self, "resource", resource)
            super().add_resource(resource)

        def resource(self, uri: Any, **kwargs: Any) -> Any:
            # NOT covered by ``add_resource``: a ``{param}`` uri, or any
            # function with arguments, calls ``_resource_manager.add_template``
            # directly (mcp 1.28.1, ``server.py:613``) — the shape a per-note
            # ``brain://note/{id}`` resource would take.
            _refuse_unseamed(self, "resource", uri)
            return _guard_at_application(
                self, "resource", uri, super().resource(uri, **kwargs),
            )

        def add_prompt(self, prompt: Any) -> None:
            _refuse_unseamed(self, "prompt", prompt)
            super().add_prompt(prompt)

        def completion(self) -> Any:
            # A THIRD protocol handler, and a public one: it installs
            # ``request_handlers[CompleteRequest]`` (mcp 1.28.1,
            # ``lowlevel/server.py:616``), which never reaches ``call_tool``
            # and does not check that ``params.ref`` names anything
            # registered. Content-capable, no tally, no SEC-06 row.
            _refuse_unseamed(self, "completion handler", "completion/complete")
            return _guard_at_application(
                self, "completion handler", "completion/complete",
                super().completion(),
            )

        def custom_route(self, path: Any, *args: Any, **kwargs: Any) -> Any:
            # Binds only on the SSE / streamable-HTTP apps, and ``serve()`` runs
            # stdio — so this is a door that is currently unreachable rather
            # than an open one. Refused anyway: it is public, it is free, and
            # the alternative is a footnote nobody re-reads.
            _refuse_unseamed(self, "HTTP route", path)
            return _guard_at_application(
                self, "HTTP route", path,
                super().custom_route(path, *args, **kwargs),
            )

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            max_tier = _clamp_max_tier(
                str((arguments or {}).get("max_tier", cls.HOST_MCP_DEFAULT_MAX_TIER))
            )
            with mediate(
                vault=self._brain_vault, role=self._brain_role,
                cmd=f"mcp:{name}", max_tier=max_tier,
                expects_gate=name not in BODYLESS_TOOLS,
            ):
                return await super().call_tool(name, arguments)

    return BrokerServer


_BROKER_SERVER_CLASS: type | None = None


def broker_server_class() -> type:
    """The broker's FastMCP subclass, built on first use and cached.

    ``mcp`` is an optional extra (``pip install brainiac-cli[mcp]``), so the
    class cannot be defined at import time without making this module — and
    therefore ``dispatch()`` — unimportable on a minimal install.
    """
    global _BROKER_SERVER_CLASS
    if _BROKER_SERVER_CLASS is None:
        _BROKER_SERVER_CLASS = _make_broker_server()
    return _BROKER_SERVER_CLASS


def __getattr__(attr: str) -> Any:
    """Expose ``mcp_adapter.BrokerServer`` without importing ``mcp`` eagerly."""
    if attr == "BrokerServer":
        return broker_server_class()
    raise AttributeError(attr)


if __name__ == "__main__":  # pragma: no cover
    serve()
