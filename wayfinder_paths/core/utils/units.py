from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation


def _to_decimal(value: str | int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return Decimal(str(value).strip())


def to_wei_eth(amount_eth: str | int | float | Decimal) -> int:
    try:
        amt = _to_decimal(amount_eth)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid ETH amount: {amount_eth}") from exc
    if amt < 0:
        raise ValueError("Amount must be non-negative")
    return int((amt * Decimal(10**18)).to_integral_value(rounding=ROUND_DOWN))


def to_erc20_raw(amount_tokens: str | int | float | Decimal, decimals: int) -> int:
    try:
        amt = _to_decimal(amount_tokens)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid token amount: {amount_tokens}") from exc
    if amt < 0:
        raise ValueError("Amount must be non-negative")
    scale = Decimal(10) ** int(decimals)
    return int((amt * scale).to_integral_value(rounding=ROUND_DOWN))
