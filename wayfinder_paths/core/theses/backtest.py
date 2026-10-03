"""Entry once, then hold: funded spot/shares and isolated perp collateral.

Historical depth is not available. Fees, routing cost and slippage are explicit
assumptions, not claimed historical fills. History is validated, never filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.core.theses.models import Position, Variant
from wayfinder_paths.quant.market_metrics import max_drawdown


@dataclass(frozen=True)
class MarketHistory:
    # Open-labelled, completed hourly OHLC; prediction markets use observed marks.
    prices: pd.DataFrame
    source: str
    fee_rate: float
    slippage_rate: float
    routing_cost_usd: float
    min_notional_usd: float
    max_notional_usd: float
    funding: pd.Series | None = None
    maintenance_margin_rate: float | None = None
    settlement_at: pd.Timestamp | None = None
    settlement_price: float | None = None

    def validate(self, position: Position, end: pd.Timestamp) -> pd.DataFrame:
        amounts = [
            self.fee_rate,
            self.slippage_rate,
            self.routing_cost_usd,
            self.min_notional_usd,
            self.max_notional_usd,
        ]
        if not all(np.isfinite(v) and v >= 0 for v in amounts):
            raise ValueError("Invalid cost or capacity assumptions")
        if self.fee_rate >= 1 or self.slippage_rate >= 1 or not self.source:
            raise ValueError("Invalid costs or missing provenance")
        frame = self.prices.copy()
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise ValueError("History timestamps must be timezone-aware")
        frame.index = frame.index.tz_convert("UTC")
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError("History must be sorted without duplicates")
        frame = frame[frame.index + pd.Timedelta(hours=1) <= end]
        columns = (
            ["close"]
            if position.kind == "prediction"
            else ["open", "high", "low", "close"]
        )
        if not set(columns).issubset(frame.columns) or frame.empty:
            raise ValueError("Required price history is unavailable")
        values = frame[columns].to_numpy()
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError("Missing, non-finite or non-positive prices")
        if position.kind == "prediction" and (values > 1).any():
            raise ValueError("Outcome share prices must be in (0, 1]")
        if position.kind != "prediction" and (
            (frame.low > frame[["open", "close"]].min(axis=1)).any()
            or (frame.high < frame[["open", "close"]].max(axis=1)).any()
        ):
            raise ValueError("Invalid OHLC ordering")
        if any(t != t.floor("h") for t in frame.index):
            raise ValueError("Prices must be aligned to UTC hours")
        if position.kind in {"perp", "hip3"}:
            if self.funding is None or self.maintenance_margin_rate is None:
                raise ValueError(
                    "Perps require funding and maintenance margin history assumptions"
                )
            if not 0 < self.maintenance_margin_rate < 1 / position.leverage:
                raise ValueError("Invalid maintenance margin rate")
        if (self.settlement_at is None) != (self.settlement_price is None):
            raise ValueError("Settlement requires both a verified timestamp and payout")
        if self.settlement_at is not None:
            if position.kind != "prediction" or self.settlement_at.tz is None:
                raise ValueError(
                    "Only outcome shares can settle; timestamp must have timezone"
                )
            if self.settlement_price not in (0, 1):
                raise ValueError("Only verified binary settlements are supported")
        return frame


def backtest_variant(
    variant: Variant,
    markets: dict[str, MarketHistory],
    *,
    end: pd.Timestamp,
) -> dict[str, Any]:
    """Compose isolated sleeves using the jobs-v1 execution simulator.

    There is no shared collateral across sleeves. Never feed this result to a
    cross-margin executor. Prediction samples retain their midpoint provenance.
    """
    from wayfinder_paths.core.backtesting.execution.primitives import ExecutionSpec
    from wayfinder_paths.core.backtesting.execution.simulator import (
        PreparedExecutionDataset,
        simulate_execution,
    )
    from wayfinder_paths.core.backtesting.execution.venues import MarketEvent
    from wayfinder_paths.core.theses.entry_hold import EntryAndHold

    if end.tz is None:
        raise ValueError("Cutoff must be timezone-aware")
    end = end.tz_convert("UTC").floor("h")
    requested_start = end - pd.DateOffset(months=3)
    if not variant.positions:
        raise ValueError("Cash-only proposal: no investment backtest required")
    frames: dict[str, pd.DataFrame] = {}
    start, last = requested_start, end - pd.Timedelta(hours=1)
    for position in variant.positions:
        market = markets[position.instrument_id]
        frame = market.validate(position, end)
        frames[position.id] = frame
        start = max(start, frame.index[0])
        if market.settlement_at is None or market.settlement_at > end:
            last = min(last, frame.index[-1])
    index = pd.date_range(start, last, freq="h")
    if len(index) < 3:
        raise ValueError(
            "Insufficient common price history (at least three completed hours required)"
        )
    budget = variant.budget_usd
    # The upstream engine uses close-labelled bars.
    close_index = index + pd.Timedelta(hours=1)
    equity = pd.Series(
        budget * variant.cash_bps / 10000, index=close_index, dtype=float
    )
    fees, funding_paid, routing_cost = [], [], 0.0
    positions: list[dict[str, Any]] = []
    for position in variant.positions:
        market, frame = markets[position.instrument_id], frames[position.id]
        required = (
            index
            if market.settlement_at is None
            else index[index + pd.Timedelta(hours=1) <= market.settlement_at]
        )
        if len(required) < 2 or not required.isin(frame.index).all():
            raise ValueError(f"Missing price observations for {position.symbol}")
        perp = position.kind in {"perp", "hip3"}
        events = []
        if perp:
            funding = market.funding
            if (
                funding is None
                or not funding.index.is_unique
                or funding.index.tz is None
            ):
                raise ValueError(f"Invalid funding observations for {position.symbol}")
            if (
                not index.isin(funding.index).all()
                or not np.isfinite(funding.reindex(index)).all()
            ):
                raise ValueError(f"Missing funding observations for {position.symbol}")
            # Exclude the entry boundary; the position did not exist before it.
            events = [
                MarketEvent(
                    kind="funding",
                    symbol=position.instrument_id,
                    timestamp=(t + pd.Timedelta(hours=1)).isoformat(),
                    payload={
                        "rate": float(funding.loc[t]),
                        "mark_price": float(frame.loc[t, "open"]),
                    },
                )
                for t in index[2:]
            ]
        allocation = budget * position.capital_bps / 10000
        sleeve_capital = allocation - market.routing_cost_usd
        notional = sleeve_capital / (
            1 / position.leverage
            + market.slippage_rate
            + market.fee_rate * (1 + market.slippage_rate)
        )
        if (
            sleeve_capital <= 0
            or not market.min_notional_usd <= notional <= market.max_notional_usd
        ):
            raise ValueError(
                f"Budget cannot support executable size for {position.symbol}"
            )
        venue = {
            "token": "onchain",
            "perp": "hyperliquid",
            "hip3": "hyperliquid",
            "prediction": "polymarket",
        }[position.kind]
        source = frame.loc[required].copy()
        if position.kind == "prediction":
            if float(source.iloc[1].close) * (1 + market.slippage_rate) > 1:
                raise ValueError("Outcome fill price exceeds payout")
            # Engine container only: these are observed marks, never OHLC evidence.
            for column in ("open", "high", "low"):
                source[column] = source.close
        rows = [
            {
                "timestamp": (t + pd.Timedelta(hours=1)).isoformat(),
                "symbol": position.instrument_id,
                **{c: float(row[c]) for c in ("open", "high", "low", "close")},
            }
            for t, row in source.iterrows()
        ]
        if market.settlement_at is not None and market.settlement_at <= end:
            # Explicit resolution event, not synthetic post-resolution trading history.
            settled_at = market.settlement_at.ceil("h")
            events.append(
                MarketEvent(
                    kind="resolution",
                    symbol=position.instrument_id,
                    timestamp=settled_at.isoformat(),
                    payload={"value": market.settlement_price, "venue": venue},
                )
            )
            if settled_at > pd.Timestamp(rows[-1]["timestamp"]):
                rows.append({**rows[-1], "timestamp": settled_at.isoformat()})
        spec = ExecutionSpec(
            market_kind="perp"
            if perp
            else ("spot" if position.kind == "token" else "prediction"),
            venues=[venue],
            data_contract={"bar_interval": "1h"},
            validation={"mode": "strict"},
        )
        bracket = {
            key: value
            for key, value in {
                "stop_loss_pct": position.stop_loss_pct,
                "take_profit_pct": position.take_profit_pct,
            }.items()
            if value is not None
        }
        result = simulate_execution(
            EntryAndHold,
            PreparedExecutionDataset.from_rows(
                rows,
                {"label_convention": "close_time", "source": market.source},
                events,
            ),
            spec,
            {
                "initial_capital": sleeve_capital,
                "notional": notional,
                "venue": venue,
                "symbol": position.instrument_id,
                "side": "sell" if position.direction == "short" else "buy",
                "bracket": bracket or None,
                "fee_bps": market.fee_rate * 10000,
                "slippage_bps": market.slippage_rate * 10000,
                "stop_market_slippage_bps": market.slippage_rate * 10000,
                "enable_liquidation": perp,
                "liquidation_intrabar": True,
                "maintenance_margin_by_symbol": {
                    position.instrument_id: market.maintenance_margin_rate or 0
                },
                "warmup_bars": 1,
            },
        )
        if not result.validation["execution_valid"]:
            raise ValueError(f"Execution validation failed for {position.symbol}")
        if not result.trades:
            raise ValueError(f"No initial fill for {position.symbol}")
        curve = pd.Series(
            {
                pd.Timestamp(p["timestamp"]): float(p["equity"])
                for p in result.equity_curve
            }
        )
        liquidated = bool(result.stats["liquidation_count"])
        settled = any(e.kind == "resolution" for e in events)
        if not close_index.isin(curve.index).all():
            if not liquidated and not settled:
                raise ValueError(f"Simulation ended early for {position.symbol}")
            # The instrument has become cash; extending that cash is not filling prices.
            curve = curve.reindex(close_index).ffill()
        curve.iloc[0] = allocation
        equity += curve.reindex(close_index)
        fees.append(result.stats["total_fees"])
        # jobs-v1 uses received funding; public contract uses paid funding.
        funding_paid.append(-result.stats["total_funding"])
        routing_cost += market.routing_cost_usd
        initial = result.trades[0]
        reason = "liquidation" if liquidated else ("settlement" if settled else None)
        if reason is None and len(result.trades) > 1:
            reason = "protective_exit"
        positions.append(
            {
                "position_id": position.id,
                "entry_price": initial["avg_price"],
                "quantity": initial["filled_size"],
                "capital_usd": allocation,
                "notional_usd": notional,
                "final_value_usd": float(curve.iloc[-1]),
                "close_reason": reason,
                "source": market.source,
                "fee_rate": market.fee_rate,
                "slippage_rate": market.slippage_rate,
                "routing_cost_usd": market.routing_cost_usd,
            }
        )
    warnings = [
        "Designed with today's information and market universe; not an out-of-sample test.",
        "Fees, routing costs and slippage are assumptions; historical order-book depth is unavailable.",
        "Positions are marked to market at the end; future exit costs are not included.",
    ]
    if any(p.kind in {"perp", "hip3"} for p in variant.positions):
        warnings.append(
            "Each perp has isolated collateral. Adverse intrabar liquidation takes precedence over protective exits when ordering is unknown."
        )
    if any(p.kind == "prediction" for p in variant.positions):
        warnings.append(
            "Prediction entries use the next observed hourly midpoint plus slippage, not OHLC fills."
        )
    return {
        "budget_usd": budget,
        "coverage": {
            "requested_start": requested_start.isoformat(),
            "requested_end": end.isoformat(),
            "start": close_index[0].isoformat(),
            "end": close_index[-1].isoformat(),
            "partial": start > requested_start or last < end - pd.Timedelta(hours=1),
        },
        "equity": [
            {"time": t.isoformat(), "value": float(v)} for t, v in equity.items()
        ],
        "total_return": float(equity.iloc[-1] / budget - 1),
        "max_drawdown": max_drawdown(equity.tolist()),
        "fees_usd": sum(fees),
        "funding_paid_usd": sum(funding_paid),
        "routing_cost_usd": routing_cost,
        "positions": positions,
        "warnings": warnings,
        "fill_model": "next_bar_open_with_prediction_mark_proxy",
        "target_semantics": "decision_after_completed_bar",
        "targets_pre_shifted": False,
        "liquidated": any(p["close_reason"] == "liquidation" for p in positions),
    }
