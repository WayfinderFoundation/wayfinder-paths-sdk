import pytest

from wayfinder_paths.adapters.euler_v2_adapter.adapter import EulerV2Adapter


class TestEulerV2Adapter:
    def test_adapter_type(self):
        assert EulerV2Adapter.adapter_type == "EULER_V2"

    def test_strategy_address_optional(self):
        adapter = EulerV2Adapter(config={})
        assert adapter.strategy_wallet_address is None

    @pytest.mark.asyncio
    async def test_unsupported_chain_returns_error(self):
        adapter = EulerV2Adapter(config={})
        ok, err = await adapter.get_verified_vaults(chain_id=0)
        assert ok is False
        assert isinstance(err, str) and err
