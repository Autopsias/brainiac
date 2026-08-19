"""Validate supersession invariants."""
from __future__ import annotations

import sys
from pathlib import Path

from tools import validate as _source
from tools.validate import (
    CLASSIFICATIONS,
    PARA_ZONES,
    WIKILINK,
    build_zone_catalog,
    check_alias_collisions,
    check_note,
    check_section_staleness,
    check_type_lint,
    err,
    errors,
    iter_md,
    link_id,
    parse_bool,
    warn,
    warnings,
)

__doc__ = _source.__doc__


def _add_edge(
    edges: set[tuple[str, str]],
    by_id: dict[str, dict],
    old: str | None,
    new: str | None,
    rel: str,
    field: str,
) -> None:
    if old is None or new is None:
        return
    if old not in by_id:
        err(f"{rel}: {field} [[{old}]] does not resolve to a note")
        return
    if new not in by_id:
        err(f"{rel}: {field} [[{new}]] does not resolve to a note")
        return
    edges.add((old, new))


def _collect_edges(notes: list[dict], by_id: dict[str, dict]) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for note in notes:
        meta, rel, nid = note["meta"], note["rel"], note["meta"].get("id")
        successor = link_id(meta.get("superseded_by"))
        _add_edge(edges, by_id, nid, successor, rel, "superseded_by")
        for field in ("previous_version", "replaces"):
            predecessor = link_id(meta.get(field))
            _add_edge(edges, by_id, predecessor, nid, rel, field)
    return edges


def _check_reciprocal_links(edges: set[tuple[str, str]], by_id: dict[str, dict]) -> None:
    for old, new in edges:
        old_meta, new_meta = by_id[old]["meta"], by_id[new]["meta"]
        new_declares_back = (link_id(new_meta.get("previous_version")) == old or
                             link_id(new_meta.get("replaces")) == old)
        old_declares_fwd = link_id(old_meta.get("superseded_by")) == new
        if old_declares_fwd and not new_declares_back:
            warn(f"{by_id[old]['rel']}: superseded_by [[{new}]] has no reciprocal "
                 f"previous_version/replaces on the successor")


def _check_no_self_supersession(
    edges: set[tuple[str, str]], by_id: dict[str, dict]
) -> None:
    for old, new in edges:
        if old != new:
            continue
        message = f"{by_id[old]['rel']}: a note may not supersede itself (superseded_by == id)"
        if message not in errors:
            err(message)


def _check_classification_links(
    edges: set[tuple[str, str]], by_id: dict[str, dict]
) -> None:
    for old, new in edges:
        for side, side_id in (("predecessor", old), ("successor", new)):
            classification = by_id[side_id]["meta"].get("classification")
            if classification not in CLASSIFICATIONS:
                err(f"{by_id[side_id]['rel']}: {side_id} is part of a supersession "
                    f"chain ({old} -> {new}) but has no explicit classification")


def _check_forks(edges: set[tuple[str, str]], by_id: dict[str, dict]) -> None:
    successors_of: dict[str, set[str]] = {}
    for old, new in edges:
        successors_of.setdefault(old, set()).add(new)
    for old, successors in successors_of.items():
        if len(successors) > 1:
            err(f"{by_id[old]['rel']}: {old} is superseded by more than one note "
                f"({sorted(successors)}) — fork / re-supersession of an already-"
                f"superseded note is not allowed")


def _visit_cycle(
    node: str,
    adjacency: dict[str, set[str]],
    colors: dict[str, int],
    stack: list[str],
) -> bool:
    white, gray, black = 0, 1, 2
    colors[node] = gray
    stack.append(node)
    for successor in adjacency.get(node, ()):
        if colors.get(successor) == gray:
            cycle = " -> ".join(stack[stack.index(successor):] + [successor])
            err(f"supersession cycle detected: {cycle}")
            return True
        if colors.get(successor) == white and _visit_cycle(
            successor, adjacency, colors, stack
        ):
            return True
    stack.pop()
    colors[node] = black
    return False


def _check_cycles(edges: set[tuple[str, str]], by_id: dict[str, dict]) -> None:
    del by_id
    white = 0
    colors = {node: white for edge in edges for node in edge}
    adjacency: dict[str, set[str]] = {}
    for old, new in edges:
        adjacency.setdefault(old, set()).add(new)
    for node in list(colors):
        if colors[node] == white:
            _visit_cycle(node, adjacency, colors, [])


def _connected_component(
    start: str, undirected: dict[str, set[str]], seen: set[str]
) -> set[str]:
    component: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in component:
            continue
        component.add(node)
        stack.extend(undirected.get(node, ()) - component)
    seen.update(component)
    return component


def _check_one_latest_per_chain(
    edges: set[tuple[str, str]], by_id: dict[str, dict]
) -> None:
    undirected: dict[str, set[str]] = {}
    for old, new in edges:
        undirected.setdefault(old, set()).add(new)
        undirected.setdefault(new, set()).add(old)
    seen: set[str] = set()
    for start in undirected:
        if start in seen:
            continue
        component = _connected_component(start, undirected, seen)
        latest = [node for node in component
                  if parse_bool(by_id[node]["meta"].get("is_latest_version")) is True]
        if len(latest) > 1:
            err(f"supersession chain {sorted(component)} has more than one "
                f"is_latest_version: true note ({sorted(latest)})")


def check_bitemporal_global(notes: list[dict]) -> None:
    """Check every cross-note supersession invariant."""
    by_id = {n["meta"].get("id"): n for n in notes if n["meta"].get("id")}
    edges = _collect_edges(notes, by_id)
    _check_reciprocal_links(edges, by_id)
    _check_no_self_supersession(edges, by_id)
    _check_classification_links(edges, by_id)
    _check_forks(edges, by_id)
    _check_cycles(edges, by_id)
    _check_one_latest_per_chain(edges, by_id)


def _collect_notes(
    raw_dir: Path, brain_dir: Path, vault: Path, okf: bool
) -> list[dict]:
    notes: list[dict] = []
    for path in iter_md(raw_dir, vault) if raw_dir.is_dir() else []:
        note = check_note(path, "raw", okf)
        if note:
            notes.append(note)
    for path in iter_md(brain_dir, vault) if brain_dir.is_dir() else []:
        if path.name in ("backlinks.md", "catalog.md"):
            continue
        note = check_note(path, "brain", okf)
        if note:
            notes.append(note)
    return notes


def _resolve_links(notes: list[dict]) -> dict[str, set[str]]:
    ids = {n["meta"].get("id") for n in notes if n["meta"].get("id")}
    backlinks: dict[str, set[str]] = {}
    for note in notes:
        source = note["meta"].get("id")
        for match in WIKILINK.finditer(note["body"]):
            target = match.group(1).strip()
            if target not in ids:
                warn(f"{note['rel']}: wikilink [[{target}]] does not resolve")
            else:
                backlinks.setdefault(target, set()).add(source)
    return backlinks


def _write_backlinks(
    brain_dir: Path, notes: list[dict], backlinks: dict[str, set[str]]
) -> None:
    lines = [
        "---", "id: backlinks", "title: \"Backlinks (generated)\"",
        "type: index", "classification: Internal", "---", "",
        "# Backlinks (generated — do not hand-edit)", "",
    ]
    title_by_id = {n["meta"].get("id"): n["meta"].get("title", n["meta"].get("id"))
                   for n in notes}
    for target in sorted(backlinks):
        lines.append(f"## [[{target}]]")
        for source in sorted(x for x in backlinks[target] if x):
            lines.append(f"- [[{source}|{title_by_id.get(source, source)}]]")
        lines.append("")
    (brain_dir / "backlinks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {brain_dir.as_posix()}/backlinks.md ({len(backlinks)} targets)")


def _write_catalogs(brain_dir: Path, notes: list[dict]) -> None:
    by_zone: dict[str, list[dict]] = {zone: [] for zone in PARA_ZONES}
    for note in notes:
        if note["zone"] != "brain":
            continue
        rel_parts = note["path"].relative_to(brain_dir).parts
        if len(rel_parts) > 1 and rel_parts[0] in by_zone:
            by_zone[rel_parts[0]].append(note)
    for zone in PARA_ZONES:
        zone_dir = brain_dir / zone
        zone_dir.mkdir(parents=True, exist_ok=True)
        (zone_dir / "catalog.md").write_text(
            build_zone_catalog(zone, by_zone[zone]), encoding="utf-8"
        )
        print(f"wrote {zone_dir.as_posix()}/catalog.md ({len(by_zone[zone])} notes)")


def _print_summary(notes: list[dict]) -> int:
    print(f"\nchecked {len(notes)} notes "
          f"({sum(1 for n in notes if n['zone']=='raw')} raw, "
          f"{sum(1 for n in notes if n['zone']=='brain')} brain)")
    for warning in warnings:
        print(f"  WARN  {warning}")
    for error in errors:
        print(f"  ERROR {error}")
    deny = sum(1 for n in notes
               if n["meta"].get("classification") not in CLASSIFICATIONS)
    print(f"\nsummary: {len(errors)} errors, {len(warnings)} warnings, "
          f"{deny} default-denied (unlabelled) notes")
    return 1 if errors else 0


def main() -> int:
    # ponytail: module-level accumulators reset per call so tests (and any
    # in-process re-run) don't leak errors/warnings across invocations.
    errors.clear()
    warnings.clear()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    vault = Path(args[0]).resolve()
    do_backlinks = "--backlinks" in args
    do_catalogs = "--catalogs" in args
    okf = "--okf" in args
    if not vault.is_dir():
        print(f"error: {vault} is not a directory")
        return 2

    raw_dir, brain_dir = vault / "raw", vault / "brain"
    if not raw_dir.is_dir():
        err("vault/raw/ missing")
    if not brain_dir.is_dir():
        err("vault/brain/ missing")
    if not (brain_dir / "index.md").is_file():
        err("vault/brain/index.md missing")

    notes = _collect_notes(raw_dir, brain_dir, vault, okf)
    check_bitemporal_global(notes)
    check_alias_collisions(notes)
    check_type_lint(notes)
    check_section_staleness(notes)
    backlinks = _resolve_links(notes)
    if do_backlinks:
        _write_backlinks(brain_dir, notes, backlinks)
    if do_catalogs and brain_dir.is_dir():
        _write_catalogs(brain_dir, notes)
    return _print_summary(notes)
