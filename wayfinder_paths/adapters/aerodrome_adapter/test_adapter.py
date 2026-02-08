import pytest

from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter


class TestAerodromeAdapter:
    @pytest.fixture
    def adapter(self):
        return AerodromeAdapter(
            config={"strategy_wallet": {"address": "0x" + "11" * 20}}
        )

    def test_init(self, adapter: AerodromeAdapter):
        assert adapter.adapter_type == "AERODROME"
        assert adapter.name == "aerodrome_adapter"
