"""Enforce that every ABI literal lives in `wayfinder_paths/core/constants/`.

An ABI literal is a list whose first element is a dict containing the
`"type"` key set to one of `function | event | constructor | error |
fallback | receive`. Adapters, strategies, mcp tools, and core utils
should import from `core/constants/*_abi.py` rather than redefining the
shape inline.

Test fixtures are exempt — purpose-built minimal ABIs in `test_*.py`
exercise overload / proxy / fallback edge cases and shouldn't be coupled
to production constants.
"""

import ast
from pathlib import Path

import pytest

import wayfinder_paths

SDK_ROOT = Path(wayfinder_paths.__path__[0])
CONSTANTS_DIR = SDK_ROOT / "core" / "constants"
ABI_FRAGMENT_TYPES = {
    "function",
    "event",
    "constructor",
    "error",
    "fallback",
    "receive",
}


def _is_abi_fragment(node: ast.AST) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    for key, value in zip(node.keys, node.values, strict=True):
        if (
            isinstance(key, ast.Constant)
            and key.value == "type"
            and isinstance(value, ast.Constant)
            and value.value in ABI_FRAGMENT_TYPES
        ):
            return True
    return False


def _is_abi_list(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.List)
        and len(node.elts) > 0
        and _is_abi_fragment(node.elts[0])
    )


def _find_inline_abis(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [node.lineno for node in ast.walk(tree) if _is_abi_list(node)]


def _python_files_outside_constants_and_tests() -> list[Path]:
    files: list[Path] = []
    for path in SDK_ROOT.rglob("*.py"):
        if CONSTANTS_DIR in path.parents:
            continue
        if path.name.startswith("test_"):
            continue
        files.append(path)
    return files


def test_no_inline_abis_in_production_code() -> None:
    offenders: list[str] = []
    for path in _python_files_outside_constants_and_tests():
        for lineno in _find_inline_abis(path):
            offenders.append(f"{path.relative_to(SDK_ROOT)}:{lineno}")
    assert not offenders, (
        "Inline ABI literals found outside `core/constants/` — move each "
        "to a `_abi.py` file and import from there:\n  " + "\n  ".join(offenders)
    )


def _exports_abi(value: object) -> bool:
    """A constants module export counts as an ABI if it's a list of ABI
    fragments OR a single ABI fragment (some files export a single event
    descriptor as a dict)."""
    if isinstance(value, dict) and value.get("type") in ABI_FRAGMENT_TYPES:
        return True
    if (
        isinstance(value, list)
        and value
        and isinstance(value[0], dict)
        and value[0].get("type") in ABI_FRAGMENT_TYPES
    ):
        return True
    return False


@pytest.mark.parametrize(
    "abi_module",
    [p.stem for p in sorted(CONSTANTS_DIR.glob("*_abi.py"))],
)
def test_abi_module_is_importable(abi_module: str) -> None:
    import importlib

    module = importlib.import_module(f"wayfinder_paths.core.constants.{abi_module}")
    abi_exports = [
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and _exports_abi(value)
    ]
    assert abi_exports, (
        f"{abi_module}.py exports no ABI lists — either drop the file or "
        "add a NAME_ABI constant."
    )
