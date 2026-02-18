from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.strategies import run_strategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_CONFIG = {
    "main_wallet": {"address": "0xMain"},
    "strategy_wallet": {"address": "0xStrat"},
}


def _make_strategy_class(
    *,
    has_analyze: bool = True,
    has_quote: bool = True,
    has_snapshot: bool = True,
    has_exit: bool = True,
    has_setup: bool = True,
):
    """Build a fake strategy class whose instances are AsyncMock-powered."""

    class FakeStrategy:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.status = AsyncMock(return_value={"portfolio": "ok"})
            self.deposit = AsyncMock(return_value=(True, "deposited"))
            self.update = AsyncMock(return_value=(True, "updated"))
            self.withdraw = AsyncMock(return_value=(True, "withdrawn"))

            if has_analyze:
                self.analyze = AsyncMock(return_value={"apy": 5.0})
            if has_quote:
                self.quote = AsyncMock(return_value={"expected_apy": 4.5})
            if has_snapshot:
                self.build_batch_snapshot = AsyncMock(return_value={"score": 99})
            if has_exit:
                self.exit = AsyncMock(return_value=(True, "exited"))
            if has_setup:
                self.setup = AsyncMock()

    return FakeStrategy


def _patch_load(strategy_class=None, status="active"):
    cls = strategy_class or _make_strategy_class()
    return patch(
        "wayfinder_paths.mcp.tools.strategies._load_strategy_class",
        return_value=(cls, status),
    )


def _patch_config():
    return patch(
        "wayfinder_paths.mcp.tools.strategies._get_strategy_config",
        return_value=dict(FAKE_CONFIG),
    )


def _patch_signing():
    return patch(
        "wayfinder_paths.mcp.tools.strategies._create_signing_callback",
        return_value=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Validation & loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_strategy_name():
    out = await run_strategy(strategy="", action="status")
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_whitespace_strategy_name():
    out = await run_strategy(strategy="   ", action="status")
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_strategy_not_found():
    with patch(
        "wayfinder_paths.mcp.tools.strategies._load_strategy_class",
        side_effect=FileNotFoundError("Missing manifest.yaml for strategy: nope"),
    ):
        out = await run_strategy(strategy="nope", action="status")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_found"
    assert "nope" in out["error"]["message"]


@pytest.mark.asyncio
async def test_wip_strategy_adds_warning():
    with _patch_load(status="wip"), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True
    assert "warning" in out
    assert "work-in-progress" in out["warning"]


# ---------------------------------------------------------------------------
# Policy action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_no_callable():
    cls = type("Bare", (), {})
    with _patch_load(strategy_class=cls, status="active"):
        out = await run_strategy(strategy="my_strat", action="policy")
    assert out["ok"] is True
    assert out["result"]["output"] == []


@pytest.mark.asyncio
async def test_policy_sync():
    cls = type("WithPolicy", (), {"policies": classmethod(lambda cls: [{"rule": "a"}])})
    with _patch_load(strategy_class=cls, status="active"):
        out = await run_strategy(strategy="my_strat", action="policy")
    assert out["ok"] is True
    assert out["result"]["output"] == [{"rule": "a"}]


@pytest.mark.asyncio
async def test_policy_async():
    async def async_policies():
        return [{"rule": "async"}]

    cls = type("WithAsyncPolicy", (), {"policies": staticmethod(async_policies)})
    with _patch_load(strategy_class=cls, status="active"):
        out = await run_strategy(strategy="my_strat", action="policy")
    assert out["ok"] is True
    assert out["result"]["output"] == [{"rule": "async"}]


@pytest.mark.asyncio
async def test_policy_raises():
    def bad_policies():
        raise RuntimeError("policy boom")

    cls = type("BadPolicy", (), {"policies": staticmethod(bad_policies)})
    with _patch_load(strategy_class=cls, status="active"):
        out = await run_strategy(strategy="my_strat", action="policy")
    assert out["ok"] is False
    assert out["error"]["code"] == "strategy_error"
    assert "boom" in out["error"]["message"]


# ---------------------------------------------------------------------------
# Read-only actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True
    assert out["result"]["action"] == "status"
    assert out["result"]["output"] == {"portfolio": "ok"}


@pytest.mark.asyncio
async def test_analyze_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="analyze", amount_usdc=500)
    assert out["ok"] is True
    assert out["result"]["action"] == "analyze"
    assert out["result"]["output"] == {"apy": 5.0}


@pytest.mark.asyncio
async def test_analyze_not_supported():
    cls = _make_strategy_class(has_analyze=False)
    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="analyze")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_supported"


@pytest.mark.asyncio
async def test_snapshot_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(
            strategy="my_strat", action="snapshot", amount_usdc=1000
        )
    assert out["ok"] is True
    assert out["result"]["action"] == "snapshot"
    assert out["result"]["output"] == {"score": 99}


@pytest.mark.asyncio
async def test_snapshot_not_supported():
    cls = _make_strategy_class(has_snapshot=False)
    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="snapshot")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_supported"


@pytest.mark.asyncio
async def test_quote_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="quote", amount_usdc=2000)
    assert out["ok"] is True
    assert out["result"]["action"] == "quote"
    assert out["result"]["output"] == {"expected_apy": 4.5}


@pytest.mark.asyncio
async def test_quote_not_supported():
    cls = _make_strategy_class(has_quote=False)
    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="quote")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_supported"


# ---------------------------------------------------------------------------
# Fund-moving actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deposit_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(
            strategy="my_strat",
            action="deposit",
            main_token_amount=100.0,
            gas_token_amount=0.01,
        )
    assert out["ok"] is True
    assert out["result"]["action"] == "deposit"
    assert out["result"]["success"] is True
    assert out["result"]["message"] == "deposited"


@pytest.mark.asyncio
async def test_deposit_missing_amount():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="deposit")
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"
    assert "main_token_amount" in out["error"]["message"]


@pytest.mark.asyncio
async def test_deposit_backcompat_amount():
    """The `amount` parameter is accepted as a fallback for main_token_amount."""
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="deposit", amount=50.0)
    assert out["ok"] is True
    assert out["result"]["success"] is True


@pytest.mark.asyncio
async def test_update_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="update")
    assert out["ok"] is True
    assert out["result"]["action"] == "update"
    assert out["result"]["success"] is True
    assert out["result"]["message"] == "updated"


@pytest.mark.asyncio
async def test_withdraw_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="withdraw")
    assert out["ok"] is True
    assert out["result"]["action"] == "withdraw"
    assert out["result"]["success"] is True
    assert out["result"]["message"] == "withdrawn"


@pytest.mark.asyncio
async def test_withdraw_rejects_partial():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="withdraw", amount=50.0)
    assert out["ok"] is False
    assert out["error"]["code"] == "not_supported"
    assert "partial" in out["error"]["message"]


@pytest.mark.asyncio
async def test_exit_success():
    with _patch_load(), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="exit")
    assert out["ok"] is True
    assert out["result"]["action"] == "exit"
    assert out["result"]["success"] is True
    assert out["result"]["message"] == "exited"


@pytest.mark.asyncio
async def test_exit_not_supported():
    cls = _make_strategy_class(has_exit=False)
    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="exit")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_supported"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_exception_returns_strategy_error():
    cls = _make_strategy_class()
    # Override status to raise
    original_init = cls.__init__

    def patched_init(self, *_args: Any, **_kwargs: Any):
        original_init(self, *_args, **_kwargs)
        self.status = AsyncMock(side_effect=RuntimeError("kaboom"))

    cls.__init__ = patched_init

    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is False
    assert out["error"]["code"] == "strategy_error"
    assert "kaboom" in out["error"]["message"]


# ---------------------------------------------------------------------------
# Constructor fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constructor_fallback_config_only():
    """If strategy class rejects signing callbacks, fall back to (config=config)."""
    call_log = []

    class ConfigOnlyStrategy:
        def __init__(self, config: Any = None, **kwargs: Any):
            if "main_wallet_signing_callback" in kwargs:
                raise TypeError("unexpected keyword argument")
            call_log.append("config_only")
            self.status = AsyncMock(return_value={"mode": "config_only"})
            self.setup = AsyncMock()

    with (
        _patch_load(strategy_class=ConfigOnlyStrategy),
        _patch_config(),
        _patch_signing(),
    ):
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True
    assert out["result"]["output"]["mode"] == "config_only"
    assert "config_only" in call_log


@pytest.mark.asyncio
async def test_constructor_fallback_no_args():
    """If strategy class rejects config kwarg too, fall back to no args."""
    call_log = []

    class NoArgStrategy:
        def __init__(self):
            call_log.append("no_args")
            self.status = AsyncMock(return_value={"mode": "no_args"})
            self.setup = AsyncMock()

    with _patch_load(strategy_class=NoArgStrategy), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True
    assert out["result"]["output"]["mode"] == "no_args"
    assert "no_args" in call_log


@pytest.mark.asyncio
async def test_setup_called_before_action():
    """strategy.setup() is called before any action if the method exists."""
    setup_mock = AsyncMock()
    cls = _make_strategy_class()
    original_init = cls.__init__

    def patched_init(self, *_args: Any, **_kwargs: Any):
        original_init(self, *_args, **_kwargs)
        self.setup = setup_mock

    cls.__init__ = patched_init

    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        await run_strategy(strategy="my_strat", action="status")
    setup_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_setup_method_ok():
    """Strategies without setup() still work fine."""
    cls = _make_strategy_class(has_setup=False)
    with _patch_load(strategy_class=cls), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_wip_warning_not_present_for_active():
    """Active strategies don't get the WIP warning."""
    with _patch_load(status="active"), _patch_config(), _patch_signing():
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True
    assert "warning" not in out


@pytest.mark.asyncio
async def test_signing_cb_returns_none_when_no_address():
    """When config wallets lack addresses, signing_cb returns None (no error)."""
    empty_config: dict = {}
    with (
        _patch_load(),
        patch(
            "wayfinder_paths.mcp.tools.strategies._get_strategy_config",
            return_value=empty_config,
        ),
        _patch_signing(),
    ):
        out = await run_strategy(strategy="my_strat", action="status")
    assert out["ok"] is True
