"""Creation-site scanning."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Pattern, TypeVar


CreationSiteType = TypeVar("CreationSiteType")


def _command_name_for_line(
    lines: list[str],
    line_number: int,
    definition_pattern: Pattern[str],
    command_pattern: Pattern[str],
) -> str | None:
    """Find the nearest CLI command decorator for a creation call."""
    for previous_line in reversed(lines[:line_number - 1]):
        command = command_pattern.search(previous_line)
        if command:
            return command.group(1)
        if definition_pattern.match(previous_line):
            break
    return None


def _scan_creation_site_file(
    src_root: Path,
    file: Path,
    *,
    module_name: Callable[[Path, Path], str],
    definition_pattern: Pattern[str],
    call_pattern: Pattern[str],
    command_pattern: Pattern[str],
    site_factory: Callable[..., CreationSiteType],
) -> list[CreationSiteType]:
    """Scan one Python source file for signed-write creation calls."""
    lines = file.read_text(encoding="utf-8").splitlines()
    qualified_module_name = module_name(src_root, file)
    sites: list[CreationSiteType] = []
    stack: list[tuple[int, str]] = []
    paren_depth = 0
    for line_number, line in enumerate(lines, start=1):
        is_continuation = paren_depth > 0
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            indent = len(line) - len(line.lstrip())
            if not is_continuation:
                definition = definition_pattern.match(line)
                if definition:
                    while stack and indent <= stack[-1][0]:
                        stack.pop()
                    stack.append((indent, definition.group("name")))
                else:
                    while stack and indent <= stack[-1][0]:
                        stack.pop()
        paren_depth += sum(line.count(character) for character in "([{")
        paren_depth -= sum(line.count(character) for character in ")]}")
        paren_depth = max(paren_depth, 0)
        if not is_continuation and definition_pattern.match(line):
            continue
        if not call_pattern.search(line):
            continue
        enclosing = stack[-1][1] if stack else "<module>"
        if enclosing == "write_note":
            continue
        site_id = f"{qualified_module_name}.{enclosing}"
        if qualified_module_name == "cli":
            command_name = _command_name_for_line(
                lines,
                line_number,
                definition_pattern,
                command_pattern,
            )
            if command_name:
                site_id = f"cli.{command_name}"
        sites.append(site_factory(
            file=str(file.relative_to(src_root)),
            function=enclosing,
            line=line_number,
            site_id=site_id,
        ))
    return sites


def discover_creation_sites(
    src_root: Path,
    *,
    module_name: Callable[[Path, Path], str],
    definition_pattern: Pattern[str],
    call_pattern: Pattern[str],
    command_pattern: Pattern[str],
    site_factory: Callable[..., CreationSiteType],
) -> list[CreationSiteType]:
    """Scan Python sources for calls to the signed-write choke point."""
    sites: list[CreationSiteType] = []
    for file in sorted(src_root.rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        if file.name == "conventions.py":
            continue
        sites.extend(_scan_creation_site_file(
            src_root,
            file,
            module_name=module_name,
            definition_pattern=definition_pattern,
            call_pattern=call_pattern,
            command_pattern=command_pattern,
            site_factory=site_factory,
        ))
    return sites
