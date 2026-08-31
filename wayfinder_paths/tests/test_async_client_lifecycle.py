from __future__ import annotations

import httpx
import pytest

from wayfinder_paths.core.clients.MerklClient import MerklClient
from wayfinder_paths.core.clients.MorphoClient import MorphoClient
from wayfinder_paths.core.clients.MorphoRewardsClient import MorphoRewardsClient
from wayfinder_paths.core.clients.WayfinderClient import (
    WayfinderClient,
    close_async_clients,
    with_async_client_cleanup,
)


def test_async_clients_are_created_lazily() -> None:
    assert WayfinderClient()._client is None
    assert MerklClient()._client is None
    assert MorphoClient()._client is None
    assert MorphoRewardsClient()._client is None


@pytest.mark.asyncio
async def test_wayfinder_client_aclose_releases_and_recreates_client() -> None:
    client = WayfinderClient()
    first = client.client

    await client.aclose()

    assert first.is_closed
    assert client._client is None
    second = client.client
    assert second is not first
    await client.aclose()


@pytest.mark.asyncio
async def test_wayfinder_client_async_context_manager_closes_client() -> None:
    async with WayfinderClient() as client:
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        client.client = httpx.AsyncClient(transport=transport)
        response = await client.client.get("https://example.test")
        owned_client = client.client

    assert response.status_code == 200
    assert owned_client.is_closed
    assert client._client is None


@pytest.mark.asyncio
async def test_registered_clients_close_at_process_boundary() -> None:
    client = WayfinderClient()
    owned_client = client.client

    await close_async_clients()

    assert owned_client.is_closed
    assert client._client is None


@pytest.mark.asyncio
async def test_client_cleanup_runs_when_operation_fails() -> None:
    client = WayfinderClient()
    owned_client = client.client

    async def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await with_async_client_cleanup(fail())

    assert owned_client.is_closed
    assert client._client is None
