from __future__ import annotations

import asyncio
import math
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.uniswap_v3_math import (
    ceil_tick_to_spacing,
    round_tick_to_spacing,
)


def fmt_amount(amount_raw: int, decimals: int) -> str:
    return f"{amount_raw / (10**decimals):,.6f}"


async def erc20_balance(chain_id: int, token: str, wallet: str) -> int:
    return await get_token_balance(
        token_address=to_checksum_address(token),
        chain_id=chain_id,
        wallet_address=to_checksum_address(wallet),
    )


async def swap_via_brap(
    *,
    brap: BRAPAdapter,
    from_token: str,
    to_token: str,
    chain_id: int,
    from_address: str,
    amount_raw: int,
    slippage_bps: int,
) -> dict[str, Any]:
    from_meta, to_meta = await asyncio.gather(
        TOKEN_CLIENT.get_token_details(from_token, chain_id=chain_id),
        TOKEN_CLIENT.get_token_details(to_token, chain_id=chain_id),
    )
    if not from_meta or not to_meta:
        raise SystemExit("Unable to resolve token metadata for BRAP swap")

    ok, quote = await brap.best_quote(
        from_token_address=from_token,
        to_token_address=to_token,
        from_chain_id=chain_id,
        to_chain_id=chain_id,
        from_address=from_address,
        amount=str(amount_raw),
        slippage=slippage_bps / 10_000,
    )
    if not ok:
        raise SystemExit(quote)

    ok, result = await brap.swap_from_quote(
        from_token=from_meta,
        to_token=to_meta,
        from_address=from_address,
        quote=quote,
    )
    if not ok:
        raise SystemExit(result)
    return result


def ticks_for_percent_range(
    current_tick: int,
    tick_spacing: int,
    range_pct: float,
) -> tuple[int, int]:
    pct = range_pct / 100.0
    if pct <= 0 or pct >= 1.0:
        raise ValueError("range_pct must be in (0, 100)")
    tick_lower = current_tick + math.floor(math.log(1.0 - pct) / math.log(1.0001))
    tick_upper = current_tick + math.ceil(math.log(1.0 + pct) / math.log(1.0001))
    return (
        round_tick_to_spacing(tick_lower, tick_spacing),
        ceil_tick_to_spacing(tick_upper, tick_spacing),
    )
