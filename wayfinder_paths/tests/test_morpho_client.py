from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.core.clients.MorphoClient import MorphoClient


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
            query="query Markets { markets { items { uniqueKey } } }"
        )

    assert payload == {"markets": {"items": []}}
    assert client.client.post.await_count == 2
    client._reset_client.assert_awaited_once()
