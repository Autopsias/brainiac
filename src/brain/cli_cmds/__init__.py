"""Route parsed CLI commands to responsibility-focused handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import eval, eval_parser
from . import lifecycle, lifecycle_parser
from . import connect, connect_parser
from . import update, update_parser
from . import retrieval, retrieval_parser
from . import versioning, versioning_parser
from . import navigation, navigation_parser
from . import owner_workflow, owner_workflow_parser
from . import index_storage, index_storage_parser
from . import ingest_storage, ingest_storage_parser
from . import presentation, presentation_parser
from . import maintenance, maintenance_parser
from .. import _optional, host_hooks

# The COS verbs register only when the private COS code is installed (ADR 0013).
# HostHookProvider CLI groups are NEVER loaded at module import time because
# `--role vm` is parsed only after `cli_cmds` is imported; callers that need
# hook-provided CLI groups invoke `load_cli_hook_groups(role=..., vault=...)`.
_COS_PARSERS: tuple[Any, ...] = ()
_COS_RUNTIME: tuple[Any, ...] = ()
if _optional.cos_available():
    from . import cos_capture, cos_capture_parser
    from . import cos_operations, cos_operations_parser

    _COS_PARSERS = (cos_capture_parser, cos_operations_parser)
    _COS_RUNTIME = (cos_capture, cos_operations)


def load_cli_hook_groups(
    *, role: str | None = None, vault: Any = None,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Return ``(parser_groups, runtime_groups)`` from approved host hooks."""
    providers = host_hooks.load_host_hooks(role=role, vault=vault)
    parsers: list[Any] = []
    runtime: list[Any] = []
    for provider in providers:
        groups = host_hooks.call_hook_method(provider, "cli_groups", default=None)
        if isinstance(groups, tuple) and len(groups) == 2:
            parsers.extend(groups[0])
            runtime.extend(groups[1])
    return tuple(parsers), tuple(runtime)


@dataclass(frozen=True)
class CommandContext:
    role: str
    config: Any
    core: Any | None


PARSER_GROUPS = (
    eval_parser,
    lifecycle_parser,
    connect_parser,
    update_parser,
    retrieval_parser,
    versioning_parser,
    navigation_parser,
    owner_workflow_parser,
    *_COS_PARSERS,
    index_storage_parser,
    ingest_storage_parser,
    presentation_parser,
    maintenance_parser,
)

RUNTIME_GROUPS = (
    eval,
    lifecycle,
    connect,
    update,
    retrieval,
    versioning,
    navigation,
    owner_workflow,
    *_COS_RUNTIME,
    index_storage,
    ingest_storage,
    presentation,
    maintenance,
)

COMMAND_GROUPS = {
    command: group for group in RUNTIME_GROUPS for command in group.COMMANDS
}
CORELESS_COMMANDS = frozenset(lifecycle.COMMANDS + connect.COMMANDS + update.COMMANDS)
