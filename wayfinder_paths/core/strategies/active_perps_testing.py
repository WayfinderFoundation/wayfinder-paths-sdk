"""Smoke-test helpers for ActivePerpsStrategy subclasses.

`assert_active_perps_backtest_runs` exercises a strategy's signal + decide
through the same trigger-pattern driver used in `_run_trigger` (the live path),
and checks for the specific bugs that cause live↔backtest divergence:

- `decide()` reading framework-owned state via side channels (e.g.
  `await ctx.perp.get_margin_balance()` — returns 0 in backtest)
- `decide()` writing framework-owned keys (e.g. `"nav"`) into `ctx.state`
- impurity violations (the driver's `purity_sandbox` raises `PurityViolation`
  on `time.*` / `random.random()`)

The helper is reusable: every `ActivePerpsStrategy` subclass's smoke test
should call it, ideally with the same fixture data the audit used.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from wayfinder_paths.core.backtesting.perps import (
    backtest_perps_trigger,
    default_decide,
)
from wayfinder_paths.core.backtesting.ref import load_ref
from wayfinder_paths.core.backtesting.types import BacktestResult
from wayfinder_paths.core.perps.context import TriggerContext

if TYPE_CHECKING:
    from wayfinder_paths.core.strategies.active_perps import ActivePerpsStrategy

# Keys reserved by the framework — decide() must never write these (they are
# computed by the driver and exposed via ctx.nav / ctx.signal / etc.).
FRAMEWORK_STATE_KEYS: frozenset[str] = frozenset({"nav"})


def _import_dotted(spec: str) -> Any:
    if ":" in spec:
        module, attr = spec.split(":", 1)
    else:
        module, _, attr = spec.rpartition(".")
    return getattr(importlib.import_module(module), attr)


async def assert_active_perps_backtest_runs(
    strategy_cls: type[ActivePerpsStrategy],
    prices: pd.DataFrame,
    funding: pd.DataFrame | None = None,
    *,
    interval: str = "1h",
    expect_trades: bool = True,
    min_total_return: float | None = None,
    params: dict[str, Any] | None = None,
    slippage_bps: float | None = None,
    fee_bps: float | None = None,
    min_order_usd: float | None = None,
    sz_decimals: dict[str, int] | None = None,
) -> BacktestResult:
    """Drive `strategy_cls`'s signal + decide through `backtest_perps_trigger`.

    Asserts:
      - backtest ran all bars without raising
      - decide() did not write to any `FRAMEWORK_STATE_KEYS`
      - if `expect_trades`, at least one trade was placed (a zero-trade run
        is the classic symptom of NAV being read from `get_margin_balance()`
        inside decide — BacktestHandler returns 0, so decide no-ops on
        every bar)
      - `total_return >= min_total_return`. When `min_total_return` is None,
        falls back to `strategy_cls.SMOKE_MIN_TOTAL_RETURN` (default 0.0 —
        break-even). Catches signal/decide regressions that still produce
        trades but lose money.

    Caller provides `prices` (and optionally `funding`) so the test can be
    deterministic. Use the audit window for parity with `backtest_ref.json`.
    """
    signal_fn = _import_dotted(strategy_cls.SIGNAL)
    decide_fn = (
        _import_dotted(strategy_cls.DECIDE) if strategy_cls.DECIDE else default_decide
    )

    state_writes: list[str] = []

    async def spying_decide(ctx: TriggerContext) -> None:
        store = ctx.state
        original_set = store.set
        original_update = store.update

        def tracked_set(key: str, value: Any) -> None:
            state_writes.append(key)
            original_set(key, value)

        def tracked_update(updates: dict[str, Any]) -> None:
            state_writes.extend(updates.keys())
            original_update(updates)

        store.set = tracked_set  # type: ignore[method-assign]
        store.update = tracked_update  # type: ignore[method-assign]
        try:
            await decide_fn(ctx)
        finally:
            store.set = original_set  # type: ignore[method-assign]
            store.update = original_update  # type: ignore[method-assign]

    effective_params = dict(
        params
        if params is not None
        else getattr(strategy_cls, "DEFAULT_PARAMS", {}) or {}
    )
    leverage = float(effective_params.get("target_leverage", 1.0))

    start_str = pd.Timestamp(prices.index[0]).strftime("%Y-%m-%d")
    end_str = pd.Timestamp(prices.index[-1]).strftime("%Y-%m-%d")
    extra: dict[str, Any] = {}
    if slippage_bps is not None:
        extra["slippage_bps"] = slippage_bps
    if fee_bps is not None:
        extra["fee_bps"] = fee_bps
    if min_order_usd is not None:
        extra["min_order_usd"] = min_order_usd
    if sz_decimals is not None:
        extra["sz_decimals"] = sz_decimals
    result = await backtest_perps_trigger(
        signal_fn=signal_fn,
        decide_fn=spying_decide,
        symbols=list(prices.columns),
        start=start_str,
        end=end_str,
        interval=interval,
        params=effective_params,
        prices=prices,
        funding=funding,
        include_funding=funding is not None,
        leverage=leverage,
        **extra,
    )

    forbidden = FRAMEWORK_STATE_KEYS.intersection(state_writes)
    assert not forbidden, (
        f"decide() wrote framework-owned state keys {sorted(forbidden)}. "
        f"These must come from the context (ctx.nav etc.), not be stored in "
        f"ctx.state — doing so causes live↔backtest divergence. See "
        f"TriggerContext docs."
    )

    assert len(result.equity_curve) == len(prices), (
        f"backtest produced {len(result.equity_curve)} bars but prices has "
        f"{len(prices)} — driver bailed early"
    )

    if expect_trades:
        assert len(result.trades) > 0, "backtest produced zero trades"

    floor = (
        min_total_return
        if min_total_return is not None
        else getattr(strategy_cls, "SMOKE_MIN_TOTAL_RETURN", 0.0)
    )
    total_return = float(result.stats.get("total_return", 0.0))
    assert total_return >= floor, (
        f"smoke backtest total_return {total_return:.4f} < floor {floor:.4f}. "
        f"Check signal/decide for regressions or widen the window if the floor "
        f"is too tight for the strategy's variance."
    )
    return result


# ---------- ref-reproduction (slow) ----------
#
# Default tolerances. Sharpe and return are checked by absolute gap because
# relative tolerance is degenerate near zero. Drawdown is signed (negative);
# assert |actual - expected| <= tol. Trade count is fractional.
REF_REPRO_SHARPE_TOL: float = 0.30
REF_REPRO_TOTAL_RETURN_TOL: float = 0.15  # absolute, in decimal (0.15 = 15pp)
REF_REPRO_MAX_DRAWDOWN_TOL: float = 0.10  # absolute, in decimal
REF_REPRO_TRADE_COUNT_TOL_PCT: float = 0.20  # ±20% on trade count


async def assert_active_perps_reproduces_ref(
    strategy_cls: type[ActivePerpsStrategy],
    prices: pd.DataFrame,
    funding: pd.DataFrame | None = None,
    *,
    sharpe_tol: float = REF_REPRO_SHARPE_TOL,
    total_return_tol: float = REF_REPRO_TOTAL_RETURN_TOL,
    max_drawdown_tol: float = REF_REPRO_MAX_DRAWDOWN_TOL,
    trade_count_tol_pct: float = REF_REPRO_TRADE_COUNT_TOL_PCT,
) -> BacktestResult:
    """Re-run the trigger backtest over `ref.data.window` exactly and assert
    the resulting stats match `ref.performance` within tolerance.

    This is the canonical "is the strategy still doing what we said it does"
    check. Slow: caller is responsible for fetching `prices` (and `funding`)
    for the full ref window — see `ref.data.window.start/end`. Use with
    `@pytest.mark.ref_reproduction` so it can be skipped from default runs.

    **Data source matters.** Use the framework's `fetch_prices` /
    `fetch_funding_rates` from `wayfinder_paths.core.backtesting.data` —
    these are what `emit_backtest_ref` bonds against. Other data feeds
    (direct HL candles, third-party APIs) may have different coverage or
    revised prices and will not reproduce the ref. The numbers won't budge
    a little — they'll differ by orders of magnitude on cumulative return.

    Tolerances default to:
      sharpe ±0.30, total_return ±0.15 (15pp), max_drawdown ±0.10 (10pp),
      trade_count ±20%. Widen with kwargs if the audit data has known
      revisions; tighten when bonding a new ref to lock in fidelity.
    """
    ref = load_ref(Path(strategy_cls.REF).parent)
    expected = dict(ref.performance)
    if not expected:
        raise AssertionError(
            f"{strategy_cls.__name__}: ref.performance is empty — nothing to "
            f"reproduce. Regenerate the ref with emit_backtest_ref(...)."
        )

    # Disable the smoke floor; we check exact stats below.
    # Crucially, pass through ref.execution_assumptions — using the default
    # 1 bps slippage vs the ref's (often 30 bps) is the single biggest source
    # of divergence between bonded numbers and a reproduction run.
    exe = ref.execution_assumptions
    result = await assert_active_perps_backtest_runs(
        strategy_cls,
        prices,
        funding,
        interval=ref.data.interval,
        expect_trades=True,
        min_total_return=float("-inf"),
        params=dict(ref.params),
        slippage_bps=exe.slippage_bps,
        fee_bps=exe.fee_bps,
        min_order_usd=exe.min_order_usd,
        sz_decimals=exe.sz_decimals,
    )

    failures: list[str] = []

    def _check_abs(key: str, tol: float) -> None:
        if key not in expected:
            return
        exp = float(expected[key])
        act = float(result.stats.get(key, 0.0))
        if abs(act - exp) > tol:
            failures.append(f"{key}: ref={exp:.4f} actual={act:.4f} (tol ±{tol:.2f})")

    _check_abs("sharpe", sharpe_tol)
    _check_abs("total_return", total_return_tol)
    _check_abs("max_drawdown", max_drawdown_tol)

    if "trade_count" in expected:
        exp_n = int(expected["trade_count"])
        act_n = int(result.stats.get("trade_count", 0))
        lo = int(round(exp_n * (1 - trade_count_tol_pct)))
        hi = int(round(exp_n * (1 + trade_count_tol_pct)))
        if not (lo <= act_n <= hi):
            failures.append(
                f"trade_count: ref={exp_n} actual={act_n} (tol ±{trade_count_tol_pct:.0%})"
            )

    assert not failures, (
        f"{strategy_cls.__name__} no longer reproduces backtest_ref.json:\n  "
        + "\n  ".join(failures)
        + "\n\nThis means signal/decide/data changed since the ref was bonded. "
        "Either revert the regression or re-bond the ref via emit_backtest_ref(...)."
    )

    return result
