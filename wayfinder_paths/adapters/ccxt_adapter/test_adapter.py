from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.ccxt_adapter.adapter import CCXTAdapter


def _make_mock_exchange(exchange_id: str) -> MagicMock:
    mock = MagicMock()
    mock.id = exchange_id
    mock.close = AsyncMock()
    return mock


def _mock_ccxt_module(exchange_ids: list[str]) -> MagicMock:
    module = MagicMock()
    factories = {}
    for eid in exchange_ids:
        instance = _make_mock_exchange(eid)
        factory = MagicMock(return_value=instance)
        factories[eid] = (factory, instance)
        setattr(module, eid, factory)

    # unknown attributes return None via getattr default in adapter
    module.configure_mock(
        **{"__getattr__": lambda self, name: factories.get(name, (None,))[0]}
    )
    return module, factories


class _FakeCCXTModule:
    """Stands in for ccxt.async_support. Only registered exchanges exist as attributes."""

    def __init__(self) -> None:
        self._instances: dict[str, MagicMock] = {}

    def _register(self, exchange_id: str) -> MagicMock:
        instance = _make_mock_exchange(exchange_id)
        factory = MagicMock(return_value=instance)
        object.__setattr__(self, exchange_id, factory)
        self._instances[exchange_id] = instance
        return instance


@pytest.fixture
def mock_ccxt():
    module = _FakeCCXTModule()

    with patch("wayfinder_paths.adapters.ccxt_adapter.adapter.ccxt_async", module):
        yield module


class TestCCXTAdapterInit:
    def test_multi_exchange_instantiation(self, mock_ccxt):
        mock_ccxt._register("binance")
        mock_ccxt._register("dydx")

        adapter = CCXTAdapter(
            exchanges={
                "binance": {"apiKey": "k1", "secret": "s1"},
                "dydx": {"apiKey": "k2", "secret": "s2"},
            }
        )

        assert hasattr(adapter, "binance")
        assert hasattr(adapter, "dydx")
        assert adapter.binance == mock_ccxt._instances["binance"]
        assert adapter.dydx == mock_ccxt._instances["dydx"]

    def test_config_fallback(self, mock_ccxt):
        mock_ccxt._register("bybit")

        config = {"ccxt": {"bybit": {"apiKey": "k"}}}
        adapter = CCXTAdapter(config=config)

        assert hasattr(adapter, "bybit")
        assert adapter.bybit == mock_ccxt._instances["bybit"]

    def test_explicit_exchanges_overrides_config(self, mock_ccxt):
        mock_ccxt._register("binance")

        config = {"ccxt": {"bybit": {"apiKey": "config_key"}}}
        adapter = CCXTAdapter(
            config=config,
            exchanges={"binance": {"apiKey": "explicit_key"}},
        )

        assert hasattr(adapter, "binance")
        assert not hasattr(adapter, "bybit") or adapter._exchanges.get("bybit") is None

    def test_unknown_exchange_raises(self, mock_ccxt):
        with pytest.raises(ValueError, match="Unknown exchange 'fakexchange'"):
            CCXTAdapter(exchanges={"fakexchange": {}})

    def test_no_exchanges_is_noop(self, mock_ccxt):
        adapter = CCXTAdapter(config={})
        assert adapter._exchanges == {}

    def test_options_passed_through(self, mock_ccxt):
        mock_ccxt._register("binance")
        factory = mock_ccxt.binance

        CCXTAdapter(
            exchanges={
                "binance": {"options": {"defaultType": "spot"}},
            }
        )

        call_args = factory.call_args[0][0]
        assert call_args["options"] == {"defaultType": "spot"}

    def test_credentials_passed(self, mock_ccxt):
        mock_ccxt._register("binance")
        factory = mock_ccxt.binance

        CCXTAdapter(
            exchanges={
                "binance": {
                    "apiKey": "mykey",
                    "secret": "mysecret",
                    "password": "mypass",
                },
            }
        )

        call_args = factory.call_args[0][0]
        assert call_args["apiKey"] == "mykey"
        assert call_args["secret"] == "mysecret"
        assert call_args["password"] == "mypass"

    def test_wallet_credentials_passed(self, mock_ccxt):
        mock_ccxt._register("hyperliquid")
        factory = mock_ccxt.hyperliquid

        CCXTAdapter(
            exchanges={
                "hyperliquid": {
                    "walletAddress": "0xabc",
                    "privateKey": "0xdef",
                },
            }
        )

        call_args = factory.call_args[0][0]
        assert call_args["walletAddress"] == "0xabc"
        assert call_args["privateKey"] == "0xdef"


class TestCCXTAdapterClose:
    async def test_close_all_exchanges(self, mock_ccxt):
        binance = mock_ccxt._register("binance")
        dydx = mock_ccxt._register("dydx")

        adapter = CCXTAdapter(exchanges={"binance": {}, "dydx": {}})
        await adapter.close()

        binance.close.assert_awaited_once()
        dydx.close.assert_awaited_once()

    async def test_close_empty(self, mock_ccxt):
        mock_ccxt._register("binance")
        adapter = CCXTAdapter(exchanges={"binance": {}})
        adapter._exchanges.clear()
        await adapter.close()


class TestCCXTAdapterProperties:
    def test_adapter_type(self, mock_ccxt):
        mock_ccxt._register("binance")
        adapter = CCXTAdapter(exchanges={"binance": {}})
        assert adapter.adapter_type == "CCXT"

    def test_adapter_name(self, mock_ccxt):
        mock_ccxt._register("binance")
        adapter = CCXTAdapter(exchanges={"binance": {}})
        assert adapter.name == "ccxt_adapter"
