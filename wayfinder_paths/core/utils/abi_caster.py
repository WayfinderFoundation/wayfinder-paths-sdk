"""Type-safe casting of values to Solidity ABI types.

Handles ``address``, ``bool``, ``uint*``/``int*``, ``bytes*``, ``string``,
arrays, and nested tuples/structs.  Used to prepare constructor arguments
for contract deployment and encoded function calls.
"""

from __future__ import annotations

from typing import Any

from web3 import Web3


def cast_single(arg: Any, abi_type: str) -> Any:
    """Cast a single Python value to its Solidity ABI type."""
    t = abi_type.strip()

    if t == "bool":
        if isinstance(arg, bool):
            return arg
        if isinstance(arg, str):
            return arg.lower() in ("true", "1", "yes")
        return bool(arg)

    if t.startswith("uint") or t.startswith("int"):
        if isinstance(arg, str) and arg.startswith("0x"):
            return int(arg, 16)
        return int(arg)

    if t == "address":
        return Web3.to_checksum_address(str(arg))

    if t == "string":
        return str(arg)

    if t.startswith("bytes"):
        if isinstance(arg, bytes):
            return arg
        s = str(arg)
        if s.startswith("0x"):
            return bytes.fromhex(s[2:])
        return s.encode("utf-8")

    return arg


def cast_args(args: list[Any], abi_inputs: list[dict[str, Any]]) -> list[Any]:
    """Recursively cast a list of arguments to match ABI input definitions.

    Each entry in *abi_inputs* must have at least ``"type"`` and optionally
    ``"components"`` for tuple/struct types.
    """
    if not args and not abi_inputs:
        return []

    if len(args) != len(abi_inputs):
        raise ValueError(
            f"Argument count mismatch: got {len(args)}, expected {len(abi_inputs)}"
        )

    result: list[Any] = []
    for arg, inp in zip(args, abi_inputs, strict=True):
        result.append(_cast_value(arg, inp))
    return result


def _cast_value(arg: Any, inp: dict[str, Any]) -> Any:
    t = inp.get("type", "").strip()
    components = inp.get("components")

    # Array types: e.g. "uint256[]", "address[3]", "tuple[]"
    if t.endswith("]"):
        bracket = t.rindex("[")
        element_type = t[:bracket]
        if not isinstance(arg, (list, tuple)):
            raise TypeError(f"Expected list for {t}, got {type(arg).__name__}")
        element_inp = {"type": element_type}
        if components:
            element_inp["components"] = components
        return [_cast_value(item, element_inp) for item in arg]

    # Tuple/struct types
    if t == "tuple" and components:
        if isinstance(arg, dict):
            ordered = [
                arg.get(c["name"], arg.get(str(i))) for i, c in enumerate(components)
            ]
            return tuple(cast_args(ordered, components))
        if isinstance(arg, (list, tuple)):
            return tuple(cast_args(list(arg), components))
        raise TypeError(
            f"Expected dict/list/tuple for tuple type, got {type(arg).__name__}"
        )

    return cast_single(arg, t)


def get_constructor_inputs(abi: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract constructor inputs from an ABI."""
    for entry in abi:
        if entry.get("type") == "constructor":
            return entry.get("inputs", [])
    return []
