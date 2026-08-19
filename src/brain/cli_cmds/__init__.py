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
from . import cos_capture, cos_capture_parser
from . import cos_operations, cos_operations_parser
from . import index_storage, index_storage_parser
from . import ingest_storage, ingest_storage_parser
from . import presentation, presentation_parser
from . import maintenance, maintenance_parser


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
    cos_capture_parser,
    cos_operations_parser,
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
    cos_capture,
    cos_operations,
    index_storage,
    ingest_storage,
    presentation,
    maintenance,
)

COMMAND_GROUPS = {
    command: group for group in RUNTIME_GROUPS for command in group.COMMANDS
}
CORELESS_COMMANDS = frozenset(lifecycle.COMMANDS + connect.COMMANDS + update.COMMANDS)
