from __future__ import annotations

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
