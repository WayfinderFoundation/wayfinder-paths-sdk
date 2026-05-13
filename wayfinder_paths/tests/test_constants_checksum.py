"""Enforce two source-of-truth invariants for
`wayfinder_paths.core.constants`:

1. Every address literal in the source is wrapped in
   `to_checksum_address(...)`. Even ones that already look checksummed —
   we always pay the wrap cost so a developer pasting a lowercase address
   can't slip through.

2. Every address value exported at runtime is EIP-55 checksummed. This is
   the contract downstream code depends on.

(1) catches the bare literal at PR time; (2) catches the bare runtime
value in case (1) ever gets bypassed (e.g. address built by string
concatenation).
"""

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import pytest
from eth_utils import is_checksum_address

import wayfinder_paths.core.constants as constants_pkg

ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONSTANTS_DIR = Path(constants_pkg.__path__[0])


def _walk(value: object, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(value, str):
        if ADDRESS_PATTERN.match(value):
            out.append((path, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk(k, f"{path}[key]", out)
            _walk(v, f"{path}[{k!r}]", out)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for i, item in enumerate(value):
            _walk(item, f"{path}[{i}]", out)


def _runtime_addresses() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for info in pkgutil.iter_modules(constants_pkg.__path__):
        if info.name.endswith("_abi") or info.name.startswith("test_"):
            continue
        module = importlib.import_module(f"wayfinder_paths.core.constants.{info.name}")
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            _walk(value, f"{info.name}.{name}", found)
    return found


def _is_checksum_wrap(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "to_checksum_address"
    )


def _bare_address_literals() -> list[tuple[str, int, str]]:
    """Find address literals NOT wrapped in to_checksum_address(...)."""
    findings: list[tuple[str, int, str]] = []
    for path in sorted(CONSTANTS_DIR.glob("*.py")):
        if path.name.endswith("_abi.py") or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        wrapped: set[int] = set()
        for node in ast.walk(tree):
            if _is_checksum_wrap(node) and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    wrapped.add(id(arg))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ADDRESS_PATTERN.match(node.value)
                and id(node) not in wrapped
            ):
                findings.append((path.name, node.lineno, node.value))
    return findings


@pytest.mark.parametrize("path,address", _runtime_addresses())
def test_constant_address_is_checksummed_at_runtime(path: str, address: str) -> None:
    assert is_checksum_address(address), (
        f"{path} = {address!r} is not EIP-55 checksummed. "
        "Constants must be checksummed at definition; downstream code "
        "relies on this invariant to avoid re-normalization at every "
        "comparison."
    )


def test_every_address_literal_is_wrapped_in_to_checksum_address() -> None:
    bare = _bare_address_literals()
    assert not bare, (
        "Bare EVM address literals found in constants — wrap each one in "
        "to_checksum_address(...) so the import always emits the canonical "
        "form, even if the literal already looks checksummed:\n  "
        + "\n  ".join(f"{f}:{ln}  {addr!r}" for f, ln, addr in bare)
    )
