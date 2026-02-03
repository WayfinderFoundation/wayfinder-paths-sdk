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
