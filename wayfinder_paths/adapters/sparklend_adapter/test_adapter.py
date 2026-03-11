import pytest

from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter


class TestSparkLendAdapter:
    @pytest.fixture
    def adapter(self):
        return SparkLendAdapter(
            config={},
            wallet_address="0x1234567890123456789012345678901234567890",
        )

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "SPARKLEND"

    def test_wallet_optional(self):
        a = SparkLendAdapter(config={})
        assert a.wallet_address is None

    @pytest.mark.asyncio
    async def test_borrow_rate_mode_validation(self, adapter):
        ok, msg = await adapter.borrow(
            chain_id=1,
            asset="0x0000000000000000000000000000000000000001",
            amount=1,
            rate_mode=3,
        )
        assert ok is False
        assert isinstance(msg, str) and "rate_mode" in msg.lower()

