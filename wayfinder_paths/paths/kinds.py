"""Path kinds: the fixed list a manifest's ``primary_kind`` comes from, and the
structure each kind's path has to ship.

``wayfinder path doctor`` runs these checks so authors hit them before
publishing; the registry runs the same rules at review and rejects a version
that fails. ``strategy`` is the strict one: the path has to ship a
``Strategy`` subclass ``run_strategy`` can load, with every abstract method
implemented. The check is static (``ast``), nothing is imported or executed.

Loading a manifest stays permissive so installed copies of paths published
under retired kinds ("bundle", "dashboard") keep working.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

# Each kind's one-line meaning. Rejections quote these so the author (or their
# agent) can pick the right kind from the error alone.
KIND_HINTS = {
    "strategy": "runs trades through a Strategy subclass run_strategy can load",
    "monitor": "watches markets or state and emits signals, scores or alerts",
    "script": "a runnable one-off task or workflow",
    "skill": "instructions that extend an agent's abilities",
    "policy": "constrains or guides agent behavior",
    "tool": "a function the agent calls on demand",
    "contract": "ships a smart contract with its Solidity source",
}
PATH_KINDS = tuple(KIND_HINTS)

# SDK base classes a strategy may extend, and the abstract methods a subclass
# still owes. ActivePerpsStrategy implements all of Strategy's itself.
_SDK_STRATEGY_BASES: dict[str, frozenset[str]] = {
    "Strategy": frozenset({"deposit", "update", "exit", "_status"}),
    "ActivePerpsStrategy": frozenset(),
}

MANIFEST_FILE = "wfpath.yaml"


def primary_kind_error(raw: Any) -> str | None:
    kind = str(raw or "").strip()
    if kind in PATH_KINDS:
        return None
    choices = "; ".join(f"{name} ({hint})" for name, hint in KIND_HINTS.items())
    problem = (
        "primary_kind is required"
        if not kind
        else f"primary_kind '{kind}' is not supported"
    )
    return f"{problem}. Pick the kind that matches what the path ships: {choices}."


def kind_problems(
    *, manifest: Mapping[str, Any], files: Mapping[str, str], has_skill: bool
) -> list[tuple[str, str]]:
    """(file, message) for each way the path falls short of its declared kind.

    ``files`` maps path-relative file names to their text; ``has_skill`` is
    whether the path ships skill/SKILL.md.
    """
    error = primary_kind_error(manifest.get("primary_kind"))
    if error:
        return [(MANIFEST_FILE, error)]
    kind = str(manifest.get("primary_kind")).strip()
    if kind == "strategy":
        return _strategy_problems(manifest, files)
    if kind == "contract" and not any(path.endswith(".sol") for path in files):
        return [
            (MANIFEST_FILE, "A contract path must ship its Solidity source (.sol).")
        ]
    skill = manifest.get("skill")
    generated_skill = isinstance(skill, dict) and bool(skill.get("enabled"))
    if kind == "skill" and not (has_skill or generated_skill):
        return [
            (
                MANIFEST_FILE,
                "A skill path must ship skill/SKILL.md or enable skill in wfpath.yaml.",
            )
        ]
    return []


def _strategy_problems(
    manifest: Mapping[str, Any], files: Mapping[str, str]
) -> list[tuple[str, str]]:
    raw_components = manifest.get("components")
    components = [
        item
        for item in (raw_components if isinstance(raw_components, list) else [])
        if isinstance(item, dict) and str(item.get("kind") or "").strip() == "strategy"
    ]
    if not components:
        return [
            (
                MANIFEST_FILE,
                "A strategy path must declare a component with kind: strategy.",
            )
        ]

    classes = _class_index(files)
    problems: list[tuple[str, str]] = []
    for component in components:
        path = str(component.get("path") or "").strip()
        problem = _strategy_component_problem(path, files, classes)
        if problem is None:
            return []
        problems.append((path or MANIFEST_FILE, problem))
    return problems


def _strategy_component_problem(
    path: str, files: Mapping[str, str], classes: dict[str, tuple[list[str], set[str]]]
) -> str | None:
    if not path.endswith(".py"):
        return f"Strategy component '{path}' must be a Python file."
    if path not in files:
        return f"Strategy component '{path}' is not in the path."
    try:
        tree = ast.parse(files[path])
    except SyntaxError as exc:
        return f"Strategy component '{path}' does not parse: {exc.msg} (line {exc.lineno})."

    # The component's own classes win over same-named classes elsewhere.
    classes = {**classes, **_class_index({path: files[path]})}
    incomplete: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name in _SDK_STRATEGY_BASES:
            continue
        missing = _missing_methods(node.name, classes, seen=set())
        if missing is None:
            continue
        if not missing:
            return None
        incomplete.append(
            f"{node.name} leaves {', '.join(sorted(missing))} unimplemented"
        )
    if incomplete:
        return (
            f"Strategy component '{path}': {'; '.join(incomplete)} "
            "(missing, or only a stub such as pass or a constant return)."
        )
    return (
        f"Strategy component '{path}' defines no Strategy subclass. A path that only "
        "signals, scores or researches is a monitor or tool, not a strategy."
    )


def _missing_methods(
    name: str, classes: dict[str, tuple[list[str], set[str]]], *, seen: set[str]
) -> frozenset[str] | None:
    """Abstract methods ``name`` still owes, or None when it is not a Strategy.

    A class defined in the path shadows the SDK name, so a path cannot pass
    by declaring its own ``class Strategy``.
    """
    if name in seen:
        return None
    seen.add(name)
    bases, methods = classes[name]
    for base in bases:
        if base in classes:
            owed = _missing_methods(base, classes, seen=seen)
        else:
            owed = _SDK_STRATEGY_BASES.get(base)
        if owed is not None:
            return owed - methods
    return None


def _class_index(files: Mapping[str, str]) -> dict[str, tuple[list[str], set[str]]]:
    """Every top-level class in the path's Python files: name -> (bases, methods)."""
    index: dict[str, tuple[list[str], set[str]]] = {}
    for path, text in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name not in index:
                bases = [name for name in map(_base_name, node.bases) if name]
                methods = {
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not _is_stub(item)
                }
                index[node.name] = (bases, methods)
    return index


def _is_stub(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A body that does nothing: pass, ..., a raise, or a constant return (the
    scaffold's ``return False, "TODO"``). It does not count as implemented."""
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]  # docstring
    if not body:
        return True
    if len(body) > 1:
        return False
    stmt = body[0]
    if isinstance(stmt, (ast.Pass, ast.Raise)):
        return True
    if isinstance(stmt, ast.Expr):
        return isinstance(stmt.value, ast.Constant)
    if isinstance(stmt, ast.Return):
        return stmt.value is None or _is_constant(stmt.value)
    return False


def _is_constant(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_is_constant(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_constant(key) and _is_constant(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    return False


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
