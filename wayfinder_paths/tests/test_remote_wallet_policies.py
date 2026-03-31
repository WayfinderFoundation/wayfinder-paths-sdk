from __future__ import annotations

import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

import wayfinder_paths.core.config as config
from wayfinder_paths.core.clients.WalletClient import WalletClient
from wayfinder_paths.core.utils.wallets import (
    create_remote_wallet,
    extend_remote_wallet_expiry,
)


@pytest.fixture
def restore_global_config() -> None:
    original = copy.deepcopy(config.CONFIG)
    yield
    config.set_config(original)


@pytest.mark.asyncio
async def test_create_remote_wallet_injects_default_ttl_policy(
    restore_global_config: None,
) -> None:
    config.set_config({"system": {"default_remote_wallet_ttl_seconds": 3600}})

    with patch(
        "wayfinder_paths.core.utils.wallets.WALLET_CLIENT.create_wallet",
        new=AsyncMock(return_value={"wallet_address": "0xabc"}),
    ) as mock_create:
        result = await create_remote_wallet(label="test-wallet")

    assert result["wallet_address"] == "0xabc"
    _, kwargs = mock_create.call_args
    assert kwargs["label"] == "test-wallet"
    assert kwargs["chain_type"] == "ethereum"
    policies = kwargs["policies"]
    assert len(policies) == 1
    assert policies[0]["name"] == "TTL"
    assert policies[0]["chain_type"] == "ethereum"


@pytest.mark.asyncio
async def test_create_remote_wallet_disables_default_ttl_when_nonpositive(
    restore_global_config: None,
) -> None:
    config.set_config({"system": {"default_remote_wallet_ttl_seconds": 0}})

    with patch(
        "wayfinder_paths.core.utils.wallets.WALLET_CLIENT.create_wallet",
        new=AsyncMock(return_value={"wallet_address": "0xabc"}),
    ) as mock_create:
        await create_remote_wallet(label="test-wallet")

    _, kwargs = mock_create.call_args
    assert kwargs["policies"] == []


@pytest.mark.asyncio
async def test_create_remote_wallet_prefers_explicit_policies(
    restore_global_config: None,
) -> None:
    config.set_config({"system": {"default_remote_wallet_ttl_seconds": 3600}})
    explicit_policies = [{"name": "custom-policy"}]

    with patch(
        "wayfinder_paths.core.utils.wallets.WALLET_CLIENT.create_wallet",
        new=AsyncMock(return_value={"wallet_address": "0xabc"}),
    ) as mock_create:
        await create_remote_wallet(label="test-wallet", policies=explicit_policies)

    _, kwargs = mock_create.call_args
    assert kwargs["policies"] == explicit_policies


def test_default_remote_wallet_ttl_requires_int(restore_global_config: None) -> None:
    config.set_config({"system": {"default_remote_wallet_ttl_seconds": "abc"}})

    with pytest.raises(
        ValueError,
        match="system.default_remote_wallet_ttl_seconds must be an int",
    ):
        config.get_default_remote_wallet_ttl_seconds()


@pytest.mark.asyncio
async def test_wallet_client_extend_wallet_expiry_uses_post() -> None:
    client = WalletClient()
    response = Mock()
    response.json.return_value = {"wallet_address": "0xabc"}

    try:
        with patch.object(
            client,
            "_authed_request",
            new=AsyncMock(return_value=response),
        ) as mock_request:
            result = await client.extend_wallet_expiry(
                "0xabc",
                expires_at="2030-01-01T00:00:00+00:00",
                privy_authorization_signature="0xsig",
            )
    finally:
        await client.client.aclose()

    assert result["wallet_address"] == "0xabc"
    mock_request.assert_awaited_once_with(
        "POST",
        f"{config.get_api_base_url()}/wallets/0xabc/extend-expiry/",
        json={
            "expires_at": "2030-01-01T00:00:00+00:00",
            "privy_authorization_signature": "0xsig",
        },
    )


@pytest.mark.asyncio
async def test_extend_remote_wallet_expiry_uses_default_ttl(
    restore_global_config: None,
) -> None:
    config.set_config({"system": {"default_remote_wallet_ttl_seconds": 3600}})

    with patch(
        "wayfinder_paths.core.utils.wallets.WALLET_CLIENT.extend_wallet_expiry",
        new=AsyncMock(return_value={"wallet_address": "0xabc"}),
    ) as mock_extend:
        before = datetime.now(UTC)
        result = await extend_remote_wallet_expiry(
            "0xabc",
            privy_authorization_signature="0xsig",
        )

    assert result["wallet_address"] == "0xabc"
    mock_extend.assert_awaited_once()
    args, kwargs = mock_extend.call_args
    assert args == ("0xabc",)
    assert kwargs["privy_authorization_signature"] == "0xsig"
    expires_at = datetime.fromisoformat(kwargs["expires_at"])
    assert before <= expires_at
    remaining_seconds = (expires_at - before).total_seconds()
    assert 3595 <= remaining_seconds <= 3605


@pytest.mark.asyncio
async def test_extend_remote_wallet_expiry_rejects_nonpositive_ttl() -> None:
    with pytest.raises(
        ValueError, match="ttl must be positive to extend wallet expiry"
    ):
        await extend_remote_wallet_expiry(
            "0xabc",
            ttl=0,
            privy_authorization_signature="0xsig",
        )


@pytest.mark.asyncio
async def test_extend_remote_wallet_expiry_requires_signature() -> None:
    with pytest.raises(
        ValueError,
        match="privy_authorization_signature is required to extend wallet expiry",
    ):
        await extend_remote_wallet_expiry("0xabc", ttl=3600)
