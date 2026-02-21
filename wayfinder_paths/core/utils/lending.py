from __future__ import annotations

from typing import Any

from wayfinder_paths.core.utils.interest import RAY


def reserve_to_dict(reserve: Any, reserve_keys: list[str]) -> dict[str, Any]:
    """Convert an Aave-style reserve tuple or dict into a keyed dictionary."""
    if isinstance(reserve, dict):
        return dict(reserve)
    return dict(zip(reserve_keys, reserve, strict=False))


def compute_supply_cap_headroom(
    reserve: dict[str, Any], decimals: int
) -> tuple[int | None, int | None]:
    """Return (headroom_wei, supply_cap_tokens) for an Aave-fork reserve.

    Works for both Aave V3 and forks (HyperLend) that expose the same
    reserve fields: supplyCap, availableLiquidity, totalScaledVariableDebt,
    variableBorrowIndex.
    """
    supply_cap_tokens = int(reserve.get("supplyCap") or 0)
    if supply_cap_tokens <= 0:
        return (None, None)
    unit = 10 ** max(0, int(decimals))
    supply_cap_wei = supply_cap_tokens * unit

    available = int(reserve.get("availableLiquidity") or 0)
    scaled_variable_debt = int(reserve.get("totalScaledVariableDebt") or 0)
    variable_index = int(reserve.get("variableBorrowIndex") or 0)
    current_variable_debt = (scaled_variable_debt * variable_index) // RAY

    total_supplied = available + current_variable_debt
    headroom = max(0, supply_cap_wei - total_supplied)
    return (headroom, supply_cap_tokens)
