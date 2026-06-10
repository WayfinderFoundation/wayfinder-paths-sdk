from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from wayfinder_paths.core.clients.MorphoClient import MorphoClient


def _http_400(body: dict) -> MagicMock:
    """A mocked httpx response that raises HTTPStatusError(400) on raise_for_status."""
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = body
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("400", request=MagicMock(), response=resp)
    )
    return resp


def _ok(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": data}
    return resp


@pytest.mark.asyncio
async def test_market_id_lookup_requires_chain_id():
    client = MorphoClient(graphql_url="https://example.com/graphql")

    with pytest.raises(ValueError, match="chain_id is required"):
        await client.get_market_by_unique_key(unique_key="0xabc")

    with pytest.raises(ValueError, match="chain_id is required"):
        await client.get_market_history(unique_key="0xabc")

    with pytest.raises(ValueError, match="chain_id is required"):
        await client.get_vault_v2_by_address(address="0xabc")


@pytest.mark.asyncio
async def test_post_retries_retryable_graphql_errors():
    client = MorphoClient(graphql_url="https://example.com/graphql")
    client._ensure_client = AsyncMock()
    client._reset_client = AsyncMock()

    error_response = MagicMock()
    error_response.raise_for_status = MagicMock()
    error_response.json.return_value = {"errors": [{"status": "INTERNAL_SERVER_ERROR"}]}

    success_response = MagicMock()
    success_response.raise_for_status = MagicMock()
    success_response.json.return_value = {"data": {"markets": {"items": []}}}

    client.client = MagicMock(
        post=AsyncMock(side_effect=[error_response, success_response])
    )

    with patch(
        "wayfinder_paths.core.clients.MorphoClient.asyncio.sleep",
        new=AsyncMock(),
    ):
        payload = await client._post(
            query="query Markets { markets { items { marketId } } }"
        )

    assert payload == {"markets": {"items": []}}
    assert client.client.post.await_count == 2
    client._reset_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_retries_transient_400():
    """Morpho returns 400 under rate/load; a non-validation 400 should be retried."""
    client = MorphoClient(graphql_url="https://example.com/graphql")
    client._ensure_client = AsyncMock()
    client._reset_client = AsyncMock()

    transient = _http_400({"errors": [{"message": "rate limited, try again"}]})
    success = _ok({"markets": {"items": []}})
    client.client = MagicMock(post=AsyncMock(side_effect=[transient, success]))

    with patch(
        "wayfinder_paths.core.clients.MorphoClient.asyncio.sleep", new=AsyncMock()
    ):
        payload = await client._post(query="query { markets { items { marketId } } }")

    assert payload == {"markets": {"items": []}}
    assert client.client.post.await_count == 2


@pytest.mark.asyncio
async def test_post_fails_fast_on_validation_400():
    """A 400 caused by an invalid query must NOT be retried -- it would never succeed."""
    client = MorphoClient(graphql_url="https://example.com/graphql")
    client._ensure_client = AsyncMock()
    client._reset_client = AsyncMock()

    validation = _http_400(
        {
            "errors": [
                {
                    "message": 'Cannot query field "uniqueKey" on type "Market".',
                    "status": "GRAPHQL_VALIDATION_FAILED",
                }
            ]
        }
    )
    client.client = MagicMock(post=AsyncMock(side_effect=[validation]))

    with patch(
        "wayfinder_paths.core.clients.MorphoClient.asyncio.sleep", new=AsyncMock()
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await client._post(query="query { markets { items { uniqueKey } } }")

    assert client.client.post.await_count == 1
