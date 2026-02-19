"""Tests for wayfinder_paths.mcp.scripting module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from wayfinder_paths.adapters.boros_adapter import BorosAdapter
from wayfinder_paths.adapters.hyperlend_adapter import HyperlendAdapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter
from wayfinder_paths.mcp.scripting import (
    _detect_callback_params,
    _make_sign_callback,
    get_adapter,
)


class TestDetectCallbackParams:
    """Tests for _detect_callback_params function."""

    def test_detects_strategy_wallet_signing_callback(self):
        """Should detect strategy_wallet_signing_callback parameter."""

        class MockAdapter:
            def __init__(self, config=None, strategy_wallet_signing_callback=None):
                pass

        result = _detect_callback_params(MockAdapter)
        assert "strategy_wallet_signing_callback" in result

    def test_detects_sign_callback(self):
        """Should detect sign_callback parameter."""

        class MockAdapter:
            def __init__(self, config=None, *, sign_callback=None):
                pass

        result = _detect_callback_params(MockAdapter)
        assert "sign_callback" in result

    def test_detects_custom_signing_callback_suffix(self):
        """Should detect params ending with _signing_callback."""

        class MockAdapter:
            def __init__(self, config=None, custom_signing_callback=None):
                pass

        result = _detect_callback_params(MockAdapter)
        assert "custom_signing_callback" in result

    def test_returns_empty_for_no_callback_params(self):
        """Should return empty set when no callback params found."""

        class MockAdapter:
            def __init__(self, config=None, timeout=30):
                pass

        result = _detect_callback_params(MockAdapter)
        assert result == set()

    def test_detects_multiple_callback_params(self):
        """Should detect all matching callback params."""

        class MockAdapter:
            def __init__(
                self,
                config=None,
                strategy_wallet_signing_callback=None,
                sign_callback=None,
            ):
                pass

        result = _detect_callback_params(MockAdapter)
        assert "strategy_wallet_signing_callback" in result
        assert "sign_callback" in result

    def test_real_moonwell_adapter(self):
        """Should detect callback param from MoonwellAdapter."""
        result = _detect_callback_params(MoonwellAdapter)
        assert "strategy_wallet_signing_callback" in result

    def test_real_boros_adapter(self):
        """Should detect callback param from BorosAdapter."""
        result = _detect_callback_params(BorosAdapter)
        assert "sign_callback" in result

    def test_real_hyperlend_adapter(self):
        """Should detect callback param from HyperlendAdapter."""
        result = _detect_callback_params(HyperlendAdapter)
        assert "strategy_wallet_signing_callback" in result

    def test_real_pendle_adapter(self):
        """Should detect callback param from PendleAdapter."""
        result = _detect_callback_params(PendleAdapter)
        assert "strategy_wallet_signing_callback" in result


class TestMakeSignCallback:
    """Tests for _make_sign_callback function."""

    @pytest.mark.asyncio
    async def test_creates_working_callback(self):
        """Should create a callback that signs transactions."""
        # Test private key (DO NOT USE IN PRODUCTION)
        test_pk = "0x" + "ab" * 32

        callback = _make_sign_callback(test_pk)

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
    """Tests for get_adapter function."""

    def test_raises_when_wallet_not_found(self):
        """Should raise ValueError when wallet label not found."""

        class MockAdapter:
            def __init__(self, config=None):
                pass

        with patch(
            "wayfinder_paths.mcp.scripting.find_wallet_by_label", return_value=None
        ):
            with pytest.raises(ValueError, match="not found"):
                get_adapter(MockAdapter, "nonexistent")

    def test_raises_when_wallet_missing_private_key(self):
        """Should raise ValueError when wallet has no private key."""

        class MockAdapter:
            def __init__(self, config=None):
                pass

        wallet = {"label": "test", "address": "0x" + "11" * 20}

        with patch(
            "wayfinder_paths.mcp.scripting.find_wallet_by_label", return_value=wallet
        ):
            with pytest.raises(ValueError, match="missing private_key"):
                get_adapter(MockAdapter, "test")

    def test_works_without_wallet_for_readonly(self):
        """Should work without wallet for read-only adapters."""

        class MockAdapter:
            def __init__(self, config=None):
                self.config = config

        with patch("wayfinder_paths.mcp.scripting.CONFIG", {"foo": "bar"}):
            adapter = get_adapter(MockAdapter)
            assert adapter.config == {"foo": "bar"}

    def test_wires_strategy_wallet_into_config(self):
        """Should wire wallet into config['strategy_wallet']."""

        class MockAdapter:
            def __init__(self, config=None, strategy_wallet_signing_callback=None):
                self.config = config
                self.callback = strategy_wallet_signing_callback

        wallet = {
            "label": "main",
            "address": "0x" + "11" * 20,
            "private_key_hex": "0x" + "ab" * 32,
        }

        with patch(
            "wayfinder_paths.mcp.scripting.find_wallet_by_label", return_value=wallet
        ):
            with patch("wayfinder_paths.mcp.scripting.CONFIG", {}):
                adapter = get_adapter(MockAdapter, "main")
                assert adapter.config["strategy_wallet"] == wallet
                assert adapter.callback is not None

    def test_applies_config_overrides(self):
        """Should merge config_overrides into loaded config."""

        class MockAdapter:
            def __init__(self, config=None):
                self.config = config

        with patch("wayfinder_paths.mcp.scripting.CONFIG", {"base": "value"}):
            adapter = get_adapter(MockAdapter, config_overrides={"override": "yes"})
            assert adapter.config["base"] == "value"
            assert adapter.config["override"] == "yes"

    def test_passes_kwargs_to_adapter(self):
        """Should pass additional kwargs to adapter constructor."""

        class MockAdapter:
            def __init__(self, config=None, custom_arg=None):
                self.config = config
                self.custom_arg = custom_arg

        with patch("wayfinder_paths.mcp.scripting.CONFIG", {}):
            adapter = get_adapter(MockAdapter, custom_arg="my_value")
            assert adapter.custom_arg == "my_value"

    def test_caller_kwargs_override_auto_wired_callback(self):
        """Should allow caller to override auto-wired signing callback."""

        class MockAdapter:
            def __init__(self, config=None, strategy_wallet_signing_callback=None):
                self.callback = strategy_wallet_signing_callback

        wallet = {
            "label": "main",
            "address": "0x" + "11" * 20,
            "private_key_hex": "0x" + "ab" * 32,
        }

        custom_callback = MagicMock()

        with patch(
            "wayfinder_paths.mcp.scripting.find_wallet_by_label", return_value=wallet
        ):
            with patch("wayfinder_paths.mcp.scripting.CONFIG", {}):
                adapter = get_adapter(
                    MockAdapter,
                    "main",
                    strategy_wallet_signing_callback=custom_callback,
                )
                assert adapter.callback is custom_callback

    def test_integration_with_real_adapter_mocked_wallet(self):
        """Integration test with real adapter class and mocked wallet."""
        wallet = {
            "label": "main",
            "address": "0x" + "11" * 20,
            "private_key_hex": "0x" + "ab" * 32,
        }

        with patch(
            "wayfinder_paths.mcp.scripting.find_wallet_by_label", return_value=wallet
        ):
            with patch("wayfinder_paths.mcp.scripting.CONFIG", {}):
                adapter = get_adapter(MoonwellAdapter, "main")
                assert isinstance(adapter, MoonwellAdapter)
                assert adapter.strategy_wallet_signing_callback is not None
                assert adapter.strategy_wallet_address == wallet["address"]
