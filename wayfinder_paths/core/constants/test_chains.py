import os

import pytest

from wayfinder_paths.core.constants.chains import (
    CHAIN_CODE_TO_ID,
    CHAIN_ID_KATANA,
    CHAIN_ID_MEGAETH,
    CHAIN_ID_MONAD,
    CHAIN_ID_TO_CODE,
    SUPPORTED_CHAINS,
)

# --- Unit tests (always run) ---


def test_new_chain_id_values():
    assert CHAIN_ID_KATANA == 747474
    assert CHAIN_ID_MONAD == 143
    assert CHAIN_ID_MEGAETH == 4326


def test_chain_code_to_id_lookup():
    assert CHAIN_CODE_TO_ID["katana"] == 747474
    assert CHAIN_CODE_TO_ID["monad"] == 143
    assert CHAIN_CODE_TO_ID["megaeth"] == 4326


def test_chain_id_to_code_lookup():
    assert CHAIN_ID_TO_CODE[747474] == "katana"
    assert CHAIN_ID_TO_CODE[143] == "monad"
    assert CHAIN_ID_TO_CODE[4326] == "megaeth"


def test_new_chains_in_supported_chains():
    for cid in (CHAIN_ID_KATANA, CHAIN_ID_MONAD, CHAIN_ID_MEGAETH):
        assert cid in SUPPORTED_CHAINS


def test_no_duplicate_chain_ids_in_supported_chains():
    assert len(SUPPORTED_CHAINS) == len(set(SUPPORTED_CHAINS))


def test_chain_code_to_id_and_id_to_code_are_consistent():
    # Every code in CHAIN_CODE_TO_ID (except known aliases) should round-trip
    aliases = {"arbitrum-one", "mainnet"}
    for code, cid in CHAIN_CODE_TO_ID.items():
        if code not in aliases:
            assert CHAIN_ID_TO_CODE[cid] == code, (
                f"Round-trip failed: {code} -> {cid} -> {CHAIN_ID_TO_CODE.get(cid)}"
            )


# --- Live connectivity tests (opt-in) ---

if os.getenv("RUN_NEW_CHAIN_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "New chain live tests are disabled (set RUN_NEW_CHAIN_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )

from wayfinder_paths.core.utils.web3 import web3_from_chain_id  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain_id,expected_chain_id",
    [
        (CHAIN_ID_KATANA, 747474),
        (CHAIN_ID_MONAD, 143),
        (CHAIN_ID_MEGAETH, 4326),
    ],
)
async def test_rpc_returns_correct_chain_id(chain_id: int, expected_chain_id: int):
    async with web3_from_chain_id(chain_id) as w3:
        reported = await w3.eth.chain_id
    assert reported == expected_chain_id, (
        f"RPC for chain {chain_id} reported chain_id={reported}, expected {expected_chain_id}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain_id",
    [CHAIN_ID_KATANA, CHAIN_ID_MONAD, CHAIN_ID_MEGAETH],
)
async def test_rpc_returns_nonzero_block_number(chain_id: int):
    async with web3_from_chain_id(chain_id) as w3:
        block = await w3.eth.block_number
    assert isinstance(block, int)
    assert block > 0, f"Chain {chain_id} returned block_number=0, RPC may be unhealthy"
