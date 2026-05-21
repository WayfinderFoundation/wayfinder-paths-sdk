from __future__ import annotations

import pytest

from wayfinder_paths.adapters.derive_adapter.adapter import DeriveAdapter


@pytest.mark.asyncio
async def test_deterministic_smoke_covers_non_gorlami_surface() -> None:
    """Derive lifecycle/order flows are API/CLOB actions, not local EVM fork calls.

    Gorlami fork tests in this repo exercise EVM contract transactions. Derive's
    option discovery, account reads, lifecycle writes, and matching endpoints are
    REST/WebSocket API flows. Signed collateral/order actions settle through
    Derive's matching/protocol pipeline. This smoke check keeps deterministic
    coverage near the adapter surface while README/skill docs record the
    fork-simulation limitation.
    """
    assert DeriveAdapter.orderbook_channel("ETH-20260522-2500-C") == (
        "orderbook.ETH-20260522-2500-C.1.10"
    )
    assert DeriveAdapter.expiry_date(1779408000) == "20260522"
