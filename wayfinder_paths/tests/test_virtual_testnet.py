import pytest

from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.utils import web3 as web3_utils


def gorlami_configured() -> bool:
    return bool(get_api_key())


pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami proxy)",
)


class TestGorlamiTestnetClient:
    @pytest.fixture
    async def client(self):
        client = GorlamiTestnetClient()
        yield client
        await client.close()

    @pytest.mark.asyncio
    async def test_create_and_delete_fork(self, client):
        fork_info = await client.create_fork(chain_id=8453)

        assert "fork_id" in fork_info
        assert "rpc_url" in fork_info
        assert fork_info["chain_id"] == 8453

        result = await client.delete_fork(fork_info["fork_id"])
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_fork_not_found(self, client):
        result = await client.delete_fork("nonexistent-fork-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_native_balance(self, client):
        fork_info = await client.create_fork(chain_id=8453)
        try:
            result = await client.set_native_balance(
                fork_id=fork_info["fork_id"],
                wallet="0x1234567890123456789012345678901234567890",
                amount=10**18,
            )
            assert result is True
        finally:
            await client.delete_fork(fork_info["fork_id"])

    @pytest.mark.asyncio
    async def test_set_erc20_balance(self, client):
        fork_info = await client.create_fork(chain_id=8453)
        try:
            result = await client.set_erc20_balance(
                fork_id=fork_info["fork_id"],
                token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
                wallet="0x1234567890123456789012345678901234567890",
                amount=1000 * 10**6,
            )
            assert result is True
        finally:
            await client.delete_fork(fork_info["fork_id"])


class TestGorlamiProxyAuth:
    """Verify Gorlami operations work via the strategies.wayfinder.ai proxy with X-API-KEY auth."""

    @pytest.fixture
    async def client(self):
        client = GorlamiTestnetClient()
        yield client
        await client.close()

    @pytest.mark.asyncio
    async def test_proxy_round_trip(self, client):
        # Uses X-API-KEY header (not the old Authorization: <gorlami_key>)
        assert "X-API-KEY" in client.client.headers

        fork_info = await client.create_fork(chain_id=8453)
        fork_id = fork_info["fork_id"]
        try:
            assert fork_info["rpc_url"].startswith(client.base_url)

            wallet = "0x1234567890123456789012345678901234567890"
            await client.set_native_balance(fork_id, wallet, 10**18)
            await client.set_erc20_balance(
                fork_id,
                "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                wallet,
                500 * 10**6,
            )

            block = await client.send_rpc(fork_id, "eth_blockNumber", [])
            assert int(block, 16) > 0

            balance_hex = await client.send_rpc(
                fork_id, "eth_getBalance", [wallet, "latest"]
            )
            assert int(balance_hex, 16) == 10**18
        finally:
            assert await client.delete_fork(fork_id) is True


class TestGorlamiFixture:
    @pytest.mark.asyncio
    async def test_set_and_get_balance(self, gorlami):
        test_wallet = "0x1234567890123456789012345678901234567890"
        test_amount = 5 * 10**18

        async with web3_utils.web3_from_chain_id(8453) as web3:
            block_num = await web3.eth.block_number
            assert block_num >= 0

            chain_id = await web3.eth.chain_id
            assert chain_id == 8453

            fork_info = gorlami.forks.get("8453")
            assert fork_info is not None

            await gorlami.set_native_balance(
                fork_info["fork_id"], test_wallet, test_amount
            )

            new_balance = await web3.eth.get_balance(test_wallet)
            assert new_balance == test_amount
