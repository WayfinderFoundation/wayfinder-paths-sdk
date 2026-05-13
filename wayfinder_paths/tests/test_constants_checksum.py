"""Enforce that every EVM address literal exported by
`wayfinder_paths.core.constants` is already EIP-55 checksummed at definition.

This is the source-of-truth contract: callers can trust any address pulled
from the constants package without re-running `to_checksum_address` on it.
"""

import importlib
import pkgutil
import re

import pytest
from eth_utils import is_checksum_address

import wayfinder_paths.core.constants as constants_pkg

ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


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


def _collect_addresses() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for info in pkgutil.iter_modules(constants_pkg.__path__):
        # Skip ABI dumps (JSON ABIs, not addresses) and the live-test module
        # that raises pytest.skip at import time.
        if info.name.endswith("_abi") or info.name.startswith("test_"):
            continue
        module = importlib.import_module(f"wayfinder_paths.core.constants.{info.name}")
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            _walk(value, f"{info.name}.{name}", found)
    return found


@pytest.mark.parametrize("path,address", _collect_addresses())
def test_constant_address_is_checksummed(path: str, address: str) -> None:
    assert is_checksum_address(address), (
        f"{path} = {address!r} is not EIP-55 checksummed. "
        "Constants must be checksummed at definition; downstream code relies "
        "on this invariant to avoid re-normalization at every comparison."
    )
