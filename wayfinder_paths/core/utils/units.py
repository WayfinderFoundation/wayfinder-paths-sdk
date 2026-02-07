from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, ROUND_DOWN


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


def parse_native_funds(specs: Iterable[str]) -> dict[str, int]:
    balances: dict[str, int] = {}
    for spec in specs:
        parts = [p.strip() for p in str(spec).split(":", 1)]
        if len(parts) != 2:
            raise ValueError(f"Invalid native funds spec: {spec}")
        addr, eth_amount = parts
        balances[addr] = to_wei_eth(eth_amount)
    return balances


def parse_erc20_funds(specs: Iterable[str]) -> list[tuple[str, str, int]]:
    balances: list[tuple[str, str, int]] = []
    for spec in specs:
        parts = [p.strip() for p in str(spec).split(":")]
        if len(parts) != 4:
            raise ValueError(f"Invalid ERC20 funds spec: {spec}")
        token, wallet, amount, decimals = parts
        balances.append((token, wallet, to_erc20_raw(amount, int(decimals))))
    return balances

