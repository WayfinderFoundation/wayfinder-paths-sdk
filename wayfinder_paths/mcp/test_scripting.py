"""Tests for wayfinder_paths.mcp.scripting module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.core.utils.signing import SigningCallbacks
from wayfinder_paths.core.utils.wallets import get_local_sign_callback
from wayfinder_paths.mcp.scripting import get_adapter

TEST_PK = "0x" + "ab" * 32
TEST_PK_2 = "0x" + "cd" * 32
MAIN_WALLET = {
    "label": "main",
    "address": "0x" + "11" * 20,
    "private_key_hex": TEST_PK,
}
STRATEGY_WALLET = {
    "label": "strat",
    "address": "0x" + "22" * 20,
    "private_key_hex": TEST_PK_2,
}


def _mock_find(wallets: dict[str, dict]):
    async def find(label):
        return wallets.get(label)

    return patch(
        "wayfinder_paths.core.utils.signing.find_wallet_by_label",
        side_effect=find,
    )


def _mock_config(config=None):
    return patch("wayfinder_paths.mcp.scripting.CONFIG", config or {})


class TestMakeSignCallback:
    @pytest.mark.asyncio
    async def test_creates_working_callback(self):
        callback = get_local_sign_callback(TEST_PK)
        tx = {
            "to": "0x" + "00" * 20,
            "value": 0,
            "gas": 21000,
            "gasPrice": 1000000000,
            "nonce": 0,
            "chainId": 1,
        }
        result = await callback(tx)
        assert isinstance(result, bytes)
        assert len(result) > 0


class TestGetAdapter:
    @pytest.mark.asyncio
    async def test_raises_when_wallet_not_found(self):
        class MockAdapter:
            def __init__(self, config=None):
                pass

        with _mock_find({}):
            with pytest.raises(ValueError, match="not found"):
                await get_adapter(MockAdapter, "nonexistent")

    @pytest.mark.asyncio
    async def test_raises_when_wallet_missing_private_key(self):
        class MockAdapter:
            def __init__(self, config=None, *, signing: SigningCallbacks | None = None):
                pass

        wallet = {"label": "test", "address": "0x" + "11" * 20}
        with _mock_find({"test": wallet}):
            with pytest.raises(ValueError, match="missing private_key"):
                await get_adapter(MockAdapter, "test")

    @pytest.mark.asyncio
    async def test_works_without_wallet_for_readonly(self):
        class MockAdapter:
            def __init__(self, config=None):
                self.config = config

        with _mock_config({"foo": "bar"}):
            adapter = await get_adapter(MockAdapter)
            assert adapter.config == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_wires_signing_and_wallet(self):
        class MockAdapter:
            def __init__(self, config=None, signing=None, wallet_address=None):
                self.config = config
                self.signing = signing
                self.wallet_address = wallet_address

        with _mock_find({"main": MAIN_WALLET}), _mock_config():
            adapter = await get_adapter(MockAdapter, "main")
            assert adapter.signing is not None
            assert adapter.signing.sign is not None
            assert adapter.wallet_address == MAIN_WALLET["address"]

    @pytest.mark.asyncio
    async def test_applies_config_overrides(self):
        class MockAdapter:
            def __init__(self, config=None):
                self.config = config

        with _mock_config({"base": "value"}):
            adapter = await get_adapter(
                MockAdapter, config_overrides={"override": "yes"}
            )
            assert adapter.config["base"] == "value"
            assert adapter.config["override"] == "yes"

    @pytest.mark.asyncio
    async def test_passes_kwargs_to_adapter(self):
        class MockAdapter:
            def __init__(self, config=None, custom_arg=None):
                self.config = config
                self.custom_arg = custom_arg

        with _mock_config():
            adapter = await get_adapter(MockAdapter, custom_arg="my_value")
            assert adapter.custom_arg == "my_value"

    @pytest.mark.asyncio
    async def test_caller_kwargs_override_auto_wired_signing(self):
        class MockAdapter:
            def __init__(self, config=None, signing=None):
                self.signing = signing

        custom = MagicMock()
        with _mock_find({"main": MAIN_WALLET}), _mock_config():
            adapter = await get_adapter(MockAdapter, "main", signing=custom)
            assert adapter.signing is custom

    @pytest.mark.asyncio
    async def test_raises_when_adapter_has_no_signing_param(self):
        class MockAdapter:
            def __init__(self, config=None):
                pass

        with _mock_find({"main": MAIN_WALLET}), _mock_config():
            with pytest.raises(ValueError, match="does not accept signing callbacks"):
                await get_adapter(MockAdapter, "main")

    @pytest.mark.asyncio
    async def test_integration_with_real_adapter_mocked_wallet(self):
        with _mock_find({"main": MAIN_WALLET}), _mock_config():
            adapter = await get_adapter(MoonwellAdapter, "main")
            assert isinstance(adapter, MoonwellAdapter)
            assert adapter.signing is not None
            assert adapter.wallet_address == MAIN_WALLET["address"]

    @pytest.mark.asyncio
    async def test_dual_wallet_wiring(self):
        class DualAdapter:
            def __init__(
                self,
                config=None,
                *,
                main_signing=None,
                strategy_signing=None,
                main_wallet_address=None,
                strategy_wallet_address=None,
            ):
                self.main_signing = main_signing
                self.strategy_signing = strategy_signing
                self.main_addr = main_wallet_address
                self.strategy_addr = strategy_wallet_address

        wallets = {"main": MAIN_WALLET, "strat": STRATEGY_WALLET}
        with _mock_find(wallets), _mock_config():
            adapter = await get_adapter(DualAdapter, "main", "strat")
            assert adapter.main_signing is not None
            assert adapter.strategy_signing is not None
            assert adapter.main_addr == MAIN_WALLET["address"]
            assert adapter.strategy_addr == STRATEGY_WALLET["address"]
            assert adapter.main_signing is not adapter.strategy_signing

    @pytest.mark.asyncio
    async def test_dual_wallet_raises_without_strategy_label(self):
        class DualAdapter:
            def __init__(
                self,
                config=None,
                *,
                main_signing=None,
                strategy_signing=None,
            ):
                pass

        with _mock_find({"main": MAIN_WALLET}), _mock_config():
            with pytest.raises(ValueError, match="requires a strategy wallet"):
                await get_adapter(DualAdapter, "main")

    @pytest.mark.asyncio
    async def test_dual_wallet_kwargs_bypass_strategy_requirement(self):
        class DualAdapter:
            def __init__(
                self,
                config=None,
                *,
                main_signing=None,
                strategy_signing=None,
            ):
                self.strategy_signing = strategy_signing

        custom = MagicMock()
        with _mock_find({"main": MAIN_WALLET}), _mock_config():
            adapter = await get_adapter(DualAdapter, "main", strategy_signing=custom)
            assert adapter.strategy_signing is custom

    @pytest.mark.asyncio
    async def test_integration_balance_adapter(self):
        wallets = {"main": MAIN_WALLET, "strat": STRATEGY_WALLET}
        with _mock_find(wallets), _mock_config():
            adapter = await get_adapter(BalanceAdapter, "main", "strat")
            assert isinstance(adapter, BalanceAdapter)
            assert adapter.main_signing is not None
            assert adapter.strategy_signing is not None
            assert adapter.main_wallet_address == MAIN_WALLET["address"]
            assert adapter.strategy_wallet_address == STRATEGY_WALLET["address"]
