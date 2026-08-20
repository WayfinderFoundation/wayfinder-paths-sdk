from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.clients.WalletClient import WalletClient
from wayfinder_paths.core.utils import wallets as wallets_mod


@pytest.mark.asyncio
async def test_wallet_client_sends_instance_scoped_create(monkeypatch) -> None:
    client = WalletClient()
    request = AsyncMock(
        return_value=SimpleNamespace(json=lambda: {"evm": {}, "svm": {}})
    )
    monkeypatch.setattr(client, "_authed_request", request)
    monkeypatch.setattr(
        "wayfinder_paths.core.clients.WalletClient.get_api_base_url",
        lambda: "https://api.test/api/v1",
    )

    try:
        await client.create_wallet(
            policies=[{"name": "TTL"}],
            wallet_type="strategy",
            instance_id="oc-test",
        )
    finally:
        await client.client.aclose()

    request.assert_awaited_once_with(
        "POST",
        "https://api.test/api/v1/wallets/",
        json={
            "policies": [{"name": "TTL"}],
            "wallet_type": "strategy",
            "chain_type": "ethereum",
            "instance_id": "oc-test",
        },
    )


@pytest.mark.asyncio
async def test_remote_wallet_create_uses_atomic_backend_binding(monkeypatch) -> None:
    create = AsyncMock(return_value={"evm": {"wallet_address": "0xabc"}, "svm": {}})
    bind = AsyncMock()
    monkeypatch.setattr(wallets_mod.WALLET_CLIENT, "create_wallet", create)
    monkeypatch.setattr(wallets_mod.WALLET_CLIENT, "bind_to_instance", bind)
    monkeypatch.setattr(wallets_mod, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(wallets_mod, "get_opencode_instance_id", lambda: "oc-test")

    result = await wallets_mod.create_remote_wallet("strategy", "strategy")

    assert result["evm"]["wallet_address"] == "0xabc"
    assert create.await_args.kwargs["instance_id"] == "oc-test"
    assert create.await_args.kwargs["wallet_type"] == "strategy"
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_wallet_create_requires_instance_outside_shell(
    monkeypatch,
) -> None:
    monkeypatch.setattr(wallets_mod, "is_opencode_instance", lambda: False)

    with pytest.raises(ValueError, match="instance_id is required"):
        await wallets_mod.create_remote_wallet("strategy", "strategy")
