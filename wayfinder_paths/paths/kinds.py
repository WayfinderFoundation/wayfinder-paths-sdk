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

PATH_KINDS = ("strategy", "monitor", "script", "skill", "policy", "tool", "contract")

# SDK base classes a strategy may extend, and the abstract methods a subclass
# still owes. ActivePerpsStrategy implements all of Strategy's itself.
_SDK_STRATEGY_BASES: dict[str, frozenset[str]] = {
    "Strategy": frozenset({"deposit", "update", "exit", "_status"}),
    "ActivePerpsStrategy": frozenset(),
}

MANIFEST_FILE = "wfpath.yaml"


def primary_kind_error(raw: Any) -> str | None:
    kind = str(raw or "").strip()
    allowed = ", ".join(PATH_KINDS)
    if not kind:
        return f"primary_kind is required; use one of: {allowed}"
    if kind not in PATH_KINDS:
        return f"primary_kind '{kind}' is not supported; use one of: {allowed}"
    return None


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
            f"{node.name} does not implement {', '.join(sorted(missing))}"
        )
    if incomplete:
        return f"Strategy component '{path}': {'; '.join(incomplete)}."
    return f"Strategy component '{path}' defines no Strategy subclass."


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
                }
                index[node.name] = (bases, methods)
    return index


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
