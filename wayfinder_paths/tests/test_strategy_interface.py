"""Verify all strategies conform to expected interface."""

import inspect
from importlib import import_module
from pathlib import Path

import pytest

from wayfinder_paths.core.strategies import Strategy


def get_all_strategy_classes():
    """Discover all Strategy subclasses in wayfinder_paths/strategies/."""
    strategies_dir = Path(__file__).parent.parent / "strategies"
    strategy_classes = []

    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir() or strategy_dir.name.startswith("_"):
            continue
        strategy_file = strategy_dir / "strategy.py"
        if not strategy_file.exists():
            continue

        module_path = f"wayfinder_paths.strategies.{strategy_dir.name}.strategy"
        try:
            module = import_module(module_path)
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Strategy) and obj is not Strategy:
                    strategy_classes.append((strategy_dir.name, obj))
        except ImportError:
            continue

    return strategy_classes


@pytest.mark.parametrize("strategy_name,strategy_class", get_all_strategy_classes())
def test_deposit_accepts_run_strategy_kwargs(strategy_name, strategy_class):
    """
    Verify deposit() accepts main_token_amount and gas_token_amount kwargs.

    Both run_strategy.py and the MCP tool runner pass these kwargs.
    """
    sig = inspect.signature(strategy_class.deposit)
    params = sig.parameters

    # Must accept **kwargs OR explicitly include the canonical params.
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if has_var_keyword:
        return

    assert "main_token_amount" in params, (
        f"{strategy_class.__name__}.deposit() must accept main_token_amount "
        "or **kwargs."
    )
    assert "gas_token_amount" in params, (
        f"{strategy_class.__name__}.deposit() must accept gas_token_amount or **kwargs."
    )


@pytest.mark.parametrize("strategy_name,strategy_class", get_all_strategy_classes())
def test_withdraw_accepts_run_strategy_kwargs(strategy_name, strategy_class):
    """
    Verify withdraw() accepts max_wait_s and poll_interval_s kwargs.

    run_strategy.py always passes these to withdraw(), so all strategies
    must accept **kwargs to avoid TypeError.
    """
    sig = inspect.signature(strategy_class.withdraw)
    params = sig.parameters

    # Must accept **kwargs (VAR_KEYWORD)
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )

    assert has_var_keyword, (
        f"{strategy_class.__name__}.withdraw() must accept **kwargs. "
        f"run_strategy.py passes max_wait_s and poll_interval_s to all strategies."
    )


@pytest.mark.parametrize("strategy_name,strategy_class", get_all_strategy_classes())
def test_quote_accepts_deposit_amount(strategy_name, strategy_class):
    """
    Verify quote() accepts deposit_amount kwarg when implemented.

    run_strategy.py and the MCP tool runner call quote(deposit_amount=...).
    """
    quote_fn = getattr(strategy_class, "quote", None)
    if not callable(quote_fn):
        return

    sig = inspect.signature(quote_fn)
    params = sig.parameters

    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if has_var_keyword:
        return

    assert "deposit_amount" in params, (
        f"{strategy_class.__name__}.quote() must accept deposit_amount or **kwargs."
    )


@pytest.mark.parametrize("strategy_name,strategy_class", get_all_strategy_classes())
def test_analyze_accepts_cli_kwargs(strategy_name, strategy_class):
    """
    Verify analyze() accepts deposit_usdc and verbose kwargs when implemented.

    run_strategy.py calls analyze(deposit_usdc=..., verbose=...).
    """
    analyze_fn = getattr(strategy_class, "analyze", None)
    if not callable(analyze_fn):
        return

    sig = inspect.signature(analyze_fn)
    params = sig.parameters

    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if has_var_keyword:
        return

    assert "deposit_usdc" in params, (
        f"{strategy_class.__name__}.analyze() must accept deposit_usdc or **kwargs."
    )
    assert "verbose" in params, (
        f"{strategy_class.__name__}.analyze() must accept verbose or **kwargs."
    )


@pytest.mark.parametrize("strategy_name,strategy_class", get_all_strategy_classes())
def test_build_batch_snapshot_accepts_score_deposit_usdc(strategy_name, strategy_class):
    """
    Verify build_batch_snapshot() accepts score_deposit_usdc kwarg when implemented.

    The MCP tool runner calls build_batch_snapshot(score_deposit_usdc=...).
    """
    snapshot_fn = getattr(strategy_class, "build_batch_snapshot", None)
    if not callable(snapshot_fn):
        return

    sig = inspect.signature(snapshot_fn)
    params = sig.parameters

    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if has_var_keyword:
        return

    assert "score_deposit_usdc" in params, (
        f"{strategy_class.__name__}.build_batch_snapshot() must accept "
        "score_deposit_usdc or **kwargs."
    )
