from __future__ import annotations

from typing import Any

from wayfinder_paths.core.utils.interest import RAY


def reserve_to_dict(reserve: Any, reserve_keys: list[str]) -> dict[str, Any]:
    if isinstance(reserve, dict):
        return dict(reserve)
    return dict(zip(reserve_keys, reserve, strict=False))


def compute_supply_cap_headroom(
    reserve: dict[str, Any], decimals: int
) -> tuple[int | None, int | None]:
    supply_cap_tokens = int(reserve.get("supplyCap") or 0)
    if supply_cap_tokens <= 0:
        return (None, None)
    unit = 10 ** max(0, decimals)
    supply_cap_wei = supply_cap_tokens * unit

    available = int(reserve.get("availableLiquidity") or 0)
    scaled_variable_debt = int(reserve.get("totalScaledVariableDebt") or 0)
    variable_index = int(reserve.get("variableBorrowIndex") or 0)
    current_variable_debt = (scaled_variable_debt * variable_index) // RAY

    total_supplied = available + current_variable_debt
    headroom = supply_cap_wei - total_supplied
    if headroom < 0:
        headroom = 0
    return (headroom, supply_cap_tokens)


def base_currency_to_ref(base_currency: Any) -> tuple[int, float]:
    """Extract (ref_unit, ref_usd) from an Aave/Hyperlend-style base_currency tuple."""
    try:
        ref_unit = int(base_currency[0]) if base_currency else 1
    except (TypeError, ValueError):
        ref_unit = 1
    if not ref_unit:
        ref_unit = 1

    try:
        ref_usd_raw = int(base_currency[1]) if base_currency else 0
    except (TypeError, ValueError):
        ref_usd_raw = 0

    try:
        ref_usd_decimals = int(base_currency[3]) if base_currency else 0
    except (TypeError, ValueError):
        ref_usd_decimals = 0

    ref_usd = (
        ref_usd_raw / (10**ref_usd_decimals)
        if ref_usd_decimals and ref_usd_decimals > 0
        else float(ref_usd_raw)
    )
    return (ref_unit, float(ref_usd))
