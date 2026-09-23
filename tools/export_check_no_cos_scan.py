"""AST scan behind ``export_check_no_cos.py``: every core-to-COS link, guarded or not.

A LINK is a COS import (top-level or lazy) or a ``cos_`` / ``_cos_`` method
call on ``self`` or ``core`` in a file that is not itself COS. A module is COS
when the file it names matches ``COS_EXCLUDES`` in ``export_cleanroom.py`` —
this scanner names no COS path of its own.

A link is GUARDED in exactly two shapes (ADR 0013); anything else is a finding:

(a) it sits in the BODY (never the ``else``/``elif`` arm) of an ``if`` whose
    whole test is a bare call ``cos_available()`` or ``_optional.cos_available()``
    — no ``not``, ``and``, ``or`` or comparison;
(b) an earlier statement at the top level of the same function reads
    ``if not cos_available(): return ...`` and its body is only that return.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_COS_ATTR = re.compile(r"^_?cos_")
_RECEIVERS = {"self", "core"}


@dataclass(frozen=True)
class Link:
    path: str
    line: int
    kind: str      # "import" or "attr"
    target: str    # module name or attribute name
    guarded: bool

    def render(self) -> str:
        state = "guarded" if self.guarded else "UNGUARDED"
        return f"{self.path}:{self.line}: {self.kind} {self.target} [{state}]"


def _is_guard_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    f = node.func
    if isinstance(f, ast.Name):
        return f.id == "cos_available"
    return (isinstance(f, ast.Attribute) and f.attr == "cos_available"
            and isinstance(f.value, ast.Name) and f.value.id == "_optional")


def _is_early_return_guard(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.If) and not stmt.orelse
            and isinstance(stmt.test, ast.UnaryOp) and isinstance(stmt.test.op, ast.Not)
            and _is_guard_call(stmt.test.operand)
            and len(stmt.body) == 1 and isinstance(stmt.body[0], ast.Return))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _in_if_body(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Shape (a): some enclosing ``if cos_available():`` holds it in its body."""
    child, cur = node, parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.If) and _is_guard_call(cur.test) and child in cur.body:
            return True
        child, cur = cur, parents.get(cur)
    return False


def _after_early_return(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Shape (b): an earlier top-level ``if not cos_available(): return`` in the
    innermost enclosing function."""
    child, cur = node, parents.get(node)
    while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
        child, cur = cur, parents.get(cur)
    if cur is None or child not in cur.body:
        return False
    return any(_is_early_return_guard(s) for s in cur.body[:cur.body.index(child)])


def is_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    return _in_if_body(node, parents) or _after_early_return(node, parents)


def _package_of(rel: str) -> str | None:
    """Dotted package a file lives in, for resolving relative imports."""
    if not (rel.startswith("src/") and rel.endswith(".py")):
        return None
    mod = rel[len("src/"):-len(".py")].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod.rpartition(".")[0]


def _module_paths(module: str, rel: str) -> list[str]:
    """Repo paths a module name could load from: the ``src/`` layout, plus a
    sibling file for script-local imports (``tools/x.py`` doing ``import y``)."""
    stem = module.replace(".", "/")
    paths = [f"src/{stem}.py", f"src/{stem}/__init__.py", f"src/{stem}/"]
    if "." not in module:
        parent = rel.rpartition("/")[0]
        paths.append(f"{parent}/{module}.py" if parent else f"{module}.py")
    return paths


def _imported_modules(node: ast.AST, rel: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    assert isinstance(node, ast.ImportFrom)
    base = node.module or ""
    if node.level:
        pkg = _package_of(rel)
        if pkg is None:
            return []
        parts = pkg.split(".")
        if node.level > 1:
            parts = parts[: -(node.level - 1)]
        base = ".".join(parts + ([node.module] if node.module else []))
    return [base] + [f"{base}.{a.name}" for a in node.names if a.name != "*"]


def _cos_target(node: ast.AST, rel: str, is_cos_path) -> str | None:
    for mod in _imported_modules(node, rel):
        if mod and any(is_cos_path(p) for p in _module_paths(mod, rel)):
            return mod
    return None


def _cos_attr_call(node: ast.AST) -> str | None:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    f = node.func
    if not _COS_ATTR.match(f.attr):
        return None
    recv = f.value
    name = recv.id if isinstance(recv, ast.Name) else recv.attr if isinstance(recv, ast.Attribute) else None
    return f.attr if name in _RECEIVERS else None


def scan_source(text: str, rel: str, is_cos_path) -> list[Link]:
    """Every COS link in one file's source."""
    tree = ast.parse(text, filename=rel)
    parents = _parents(tree)
    links: list[Link] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            target, kind = _cos_target(node, rel, is_cos_path), "import"
        else:
            target, kind = _cos_attr_call(node), "attr"
        if target:
            links.append(Link(rel, node.lineno, kind, target, is_guarded(node, parents)))
    return sorted(links, key=lambda k: (k.path, k.line, k.target))


def scan_tree(root: Path, rels: list[str], is_cos_path) -> tuple[list[Link], list[str]]:
    """Scan every non-COS ``.py`` file in ``rels``. Returns (links, parse errors)."""
    links: list[Link] = []
    errors: list[str] = []
    for rel in sorted(rels):
        if not rel.endswith(".py") or is_cos_path(rel):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
            links += scan_source(text, rel, is_cos_path)
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            errors.append(f"{rel}: cannot scan ({type(exc).__name__}: {exc})")
    return links, errors
