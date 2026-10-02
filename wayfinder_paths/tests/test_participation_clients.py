from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wayfinder_paths.adapters.reward_participation_adapter.adapter import (
    RewardParticipationAdapter,
)
from wayfinder_paths.core.clients.ParticipationReadClient import ParticipationReadClient

ACCOUNT = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
EXAMPLES = (
    Path(__file__).parents[1] / "adapters/reward_participation_adapter/examples.json"
)


def config(protocol: str) -> dict:
    return json.loads(EXAMPLES.read_text())[protocol]


@pytest.mark.asyncio
async def test_risex_receipts_are_own_account_and_real_fees() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/v1/system/config":
            assert "authorization" not in request.headers
            data = {"chain": {"chain_id": "4153"}}
        else:
            assert request.headers["authorization"] == "Bearer private-test-token"
            if request.url.path.endswith("/history"):
                data = {"entries": [{"ledger": {"wallet_address": ACCOUNT}}]}
            elif request.url.path == "/v1/user/fees":
                data = {"maker_bps": -0.1, "taker_bps": 3}
            else:
                data = {"wallet_address": ACCOUNT, "total_points": "12.5"}
        return httpx.Response(200, json={"data": data})

    client = ParticipationReadClient(transport=httpx.MockTransport(handle))
    adapter = RewardParticipationAdapter(
        config("risex"), client=client, risex_token="private-test-token"
    )
    try:
        ok, observation = await adapter.observe()
        assert ok and isinstance(observation, dict)
        assert observation["rewards"][0]["amount"] == 12.5
        assert observation["rewards"][0]["unit"] == "RISE_POINTS"
        assert observation["metrics"]["maker_bps"] == -0.1
        assert "private-test-token" not in json.dumps(observation)
        assert adapter.supports_submit is False
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_read_client_rejects_wrong_network_and_other_users_points() -> None:
    client = ParticipationReadClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"chain": {"chain_id": "11155931"}, "wallet_address": OTHER}
            )
        )
    )
    try:
        with pytest.raises(ValueError, match="chain identity"):
            await client.risex_config()
        with pytest.raises(ValueError, match="account mismatch"):
            await client.risex_points(ACCOUNT, token="secret")
        with pytest.raises(ValueError, match="EVM account"):
            await client.risex_points("../admin", token="secret")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_imd_seat_and_earnings_remain_unvalued_read_only() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and "authorization" not in request.headers
        if request.url.path == "/version":
            data = {"commit": "deployed-version"}
        elif request.url.path.startswith("/seats/"):
            assert request.url.params["work"] == "0"
            data = {"owner": ACCOUNT, "attempts": 7, "accepted": 3, "rejected": 4}
        else:
            data = {
                "wallet": ACCOUNT,
                "count": 3,
                "earnings": [{"unknown_token_allocation": 1000}],
            }
        return httpx.Response(200, json=data)

    client = ParticipationReadClient(transport=httpx.MockTransport(handle))
    adapter = RewardParticipationAdapter(config("imd"), client=client, imd_seat_id=42)
    try:
        ok, observation = await adapter.observe()
        assert ok and isinstance(observation, dict)
        assert observation["metrics"]["accepted"] == 3
        assert observation["rewards"] == []
        assert observation["eligible"] is None
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["flop", "perptools"])
async def test_unverified_interfaces_do_not_invent_calls(protocol: str) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")

    adapter = RewardParticipationAdapter(
        config(protocol),
        client=ParticipationReadClient(transport=httpx.MockTransport(unexpected)),
    )
    try:
        ok, observation = await adapter.observe()
        assert ok and isinstance(observation, dict)
        assert observation["readiness"] == "blocked" and observation["rewards"] == []
    finally:
        await adapter.close()
