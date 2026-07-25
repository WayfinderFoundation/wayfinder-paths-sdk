from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.utils.token_refs import (
    looks_like_evm_address,
    looks_like_solana_address,
    parse_token_id_to_chain_and_address,
)
from wayfinder_paths.core.utils.token_resolver import TokenResolver

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def test_looks_like_solana_address():
    assert looks_like_solana_address(USDC_MINT)
    assert looks_like_solana_address("So11111111111111111111111111111111111111112")
    # EVM addresses and slugs are not solana
    assert not looks_like_solana_address(EVM_USDC)
    assert not looks_like_solana_address("usdc")
    assert not looks_like_solana_address(None)
    assert not looks_like_solana_address("")
    # solana detection must not swallow EVM
    assert looks_like_evm_address(EVM_USDC)
    assert not looks_like_evm_address(USDC_MINT)


def test_parse_solana_token_id():
    # both orderings, mint case preserved
    assert parse_token_id_to_chain_and_address(f"solana_{USDC_MINT}") == (
        900,
        USDC_MINT,
    )
    assert parse_token_id_to_chain_and_address(f"{USDC_MINT}_solana") == (
        900,
        USDC_MINT,
    )
    assert parse_token_id_to_chain_and_address(f"900_{USDC_MINT}") == (900, USDC_MINT)


@pytest.mark.asyncio
async def test_resolve_bare_mint_preserves_case():
    chain_id, addr = await TokenResolver.resolve_token(USDC_MINT, chain_id=900)
    assert chain_id == 900
    assert addr == USDC_MINT  # base58, never lowercased/checksummed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        f"solana_{USDC_MINT}",
        f"{USDC_MINT}_solana",
        f"900_{USDC_MINT}",
        USDC_MINT,
    ],
)
async def test_resolve_solana_token_meta_uses_spl_decimals(query: str):
    with (
        patch(
            "wayfinder_paths.core.utils.token_resolver.get_spl_mint_decimals",
            new=AsyncMock(return_value=6),
        ) as get_spl_decimals,
        patch(
            "wayfinder_paths.core.utils.token_resolver.get_token_decimals",
            new=AsyncMock(),
        ) as get_evm_decimals,
    ):
        out = await TokenResolver.resolve_token_meta(
            query,
            chain_id=900 if query == USDC_MINT else None,
        )

    assert out["chain_id"] == 900
    assert out["address"] == USDC_MINT
    assert out["decimals"] == 6
    get_spl_decimals.assert_awaited_once_with(USDC_MINT, 900)
    get_evm_decimals.assert_not_awaited()
