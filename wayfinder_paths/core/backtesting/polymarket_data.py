"""Data utilities for Polymarket backtesting.

fetch_wallet_trades is a stub (requires Delta Lab integration).
fetch_market_prices and fetch_market_metadata use the Polymarket adapter
(CLOB prices-history + Gamma markets API).
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter


def regularize_to_grid(
    raw: dict[str, list[dict]],
    start: str,
    end: str,
    interval: str = "1h",
    max_gap_hours: int | None = None,
) -> pd.DataFrame:
    """Convert sparse {token_id: [{t: unix_ts, p: price}]} history to a regular grid.

    Convention: row at grid time ``t`` contains the last observation STRICTLY
    BEFORE ``t``.  This guarantees no lookahead — at time ``t`` you only see
    prices that were known before ``t``.

    For each grid point we binary-search the sorted observation array for the
    rightmost observation with ``obs_time < t`` (``searchsorted(t, side='left') - 1``).
    """
    grid = pd.date_range(start, end, freq=interval, tz="UTC")

    frames: dict[str, pd.Series] = {}
    for token_id, ticks in raw.items():
        if not ticks:
            frames[token_id] = pd.Series(np.nan, index=grid, dtype=float)
            continue

        sorted_ticks = sorted(ticks, key=lambda x: x["t"])
        obs_ts = pd.to_datetime(
            [t["t"] for t in sorted_ticks],
            unit="s",
            utc=True,
        )
        obs_prices = np.array(
            [t["p"] for t in sorted_ticks],
            dtype=float,
        )

        values = np.full(len(grid), np.nan)
        for i, t in enumerate(grid):
            # Last observation strictly before t
            idx = obs_ts.searchsorted(t, side="left") - 1
            if idx < 0:
                continue  # no observation before this grid point
            values[i] = obs_prices[idx]
            if max_gap_hours is not None:
                gap_h = (t - obs_ts[idx]).total_seconds() / 3600.0
                if gap_h > max_gap_hours:
                    values[i] = np.nan

        frames[token_id] = pd.Series(values, index=grid, dtype=float)

    return pd.DataFrame(frames)


def warn_if_large(n_markets: int, n_trades: int) -> None:
    """Emit UserWarnings if inputs are large enough to slow the backtest."""
    if n_markets > 50:
        warnings.warn(
            f"Backtesting {n_markets} markets — this may be slow.",
            stacklevel=2,
        )
    if n_trades > 5000:
        warnings.warn(
            f"Processing {n_trades} trades — this may be slow.",
            stacklevel=2,
        )


def detect_resolutions(
    prices_df: pd.DataFrame,
    threshold: float = 0.99,
) -> dict[str, float]:
    """Return {token_id: resolution_price} for tokens whose price hit 0 or 1.

    A token resolves YES if its max price >= threshold.
    A token resolves NO  if its min price <= (1 - threshold).
    Ambiguous tokens are excluded.
    """
    result: dict[str, float] = {}
    for col in prices_df.columns:
        series = prices_df[col].dropna()
        if series.empty:
            continue
        if series.max() >= threshold:
            result[col] = 1.0
        elif series.min() <= (1.0 - threshold):
            result[col] = 0.0
    return result


async def _fetch_wallet_trades_delta_lab(
    wallet_address: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame | None:
    """Try Delta Lab first. Returns None if not available yet."""
    # TODO: wire up POLYMARKET_CLIENT.get_wallet_trades() when Delta Lab ships
    return None


def _activity_records_to_trades_df(
    wallet_address: str,
    records: list[dict[str, Any]],
) -> pd.DataFrame:
    """Convert raw Data API activity records into the backtester trades_df format."""
    from wayfinder_paths.core.backtesting.polymarket_parser import (
        parse_activity,
    )

    rows: list[dict[str, Any]] = []
    for record in records:
        signal = parse_activity(wallet_address, record)
        if signal is None:
            continue

        ts_raw = record.get("timestamp", "")
        if isinstance(ts_raw, (int, float)) or str(ts_raw).isdigit():
            ts = pd.Timestamp(int(ts_raw), unit="s", tz="UTC")
        else:
            ts = pd.Timestamp(str(ts_raw), tz="UTC")

        rows.append(
            {
                "timestamp": ts,
                "woi_address": signal.woi_address,
                "condition_id": signal.condition_id,
                "token_id": signal.token_id,
                "outcome": signal.outcome,
                "side": signal.side,
                "usdc_amount": signal.usdc_amount,
                "share_count": signal.share_count,
                "avg_price": signal.avg_price,
                "market_slug": signal.market_slug,
                "tx_hash": signal.dedupe_key,
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "woi_address",
                "condition_id",
                "token_id",
                "outcome",
                "side",
                "usdc_amount",
                "share_count",
                "avg_price",
                "market_slug",
                "tx_hash",
            ],
            index=pd.DatetimeIndex([], name="timestamp", tz="UTC"),
        )

    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return df


async def fetch_wallet_trades(
    wallet_address: str,
    start_date: str,
    end_date: str,
    adapter: PolymarketAdapter | None = None,
) -> pd.DataFrame:
    """Fetch historical trades for a wallet.

    Tries Delta Lab first, then Polymarket Data API /activity, then
    Goldsky subgraph (OrderFilledEvent) for full history on resolved markets.

    Returns a DataFrame indexed by UTC timestamp with columns:
    woi_address, condition_id, token_id, outcome, side,
    usdc_amount, share_count, avg_price, market_slug, tx_hash.
    """
    # 1) Try Delta Lab
    dl_result = await _fetch_wallet_trades_delta_lab(
        wallet_address,
        start_date,
        end_date,
    )
    if dl_result is not None and not dl_result.empty:
        return dl_result

    start_ts = int(pd.Timestamp(start_date, tz="UTC").timestamp())
    end_ts = int(pd.Timestamp(end_date, tz="UTC").timestamp())

    close_adapter = adapter is None
    if adapter is None:
        adapter = PolymarketAdapter(config={})

    try:
        # 2) Data API (recent trades, limited to ~3500)
        ok, records = await adapter.get_wallet_trade_history(
            user=wallet_address,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        if not ok:
            records = []

        data_api_df = _activity_records_to_trades_df(wallet_address, records)

        # 3) Goldsky fallback for deeper history
        # If Data API returned trades, check if they cover the full date range.
        # If the earliest trade is much later than start_date, fetch the gap from Goldsky.
        goldsky_end_ts = end_ts
        if not data_api_df.empty:
            earliest_data_api = int(data_api_df.index.min().timestamp())
            # Only query Goldsky if Data API is missing >1 day of the requested range
            if earliest_data_api - start_ts > 86400:
                goldsky_end_ts = earliest_data_api
            else:
                # Data API covers the range, no Goldsky needed
                return data_api_df

        ok_gs, gs_records = await adapter.get_goldsky_wallet_trades(
            user=wallet_address,
            start_ts=start_ts,
            end_ts=goldsky_end_ts,
        )
        goldsky_df = _activity_records_to_trades_df(
            wallet_address,
            gs_records if ok_gs else [],
        )

        # Merge, dedupe by tx_hash, sort
        if goldsky_df.empty:
            return data_api_df
        if data_api_df.empty:
            return goldsky_df

        combined = pd.concat([goldsky_df, data_api_df])
        combined = combined[~combined["tx_hash"].duplicated(keep="last")]
        return combined.sort_index()
    finally:
        if close_adapter:
            await adapter.close()


async def fetch_market_prices(
    token_ids: list[str],
    start_date: str,
    end_date: str,
    fidelity: int = 60,
    max_gap_hours: int | None = None,
    adapter: PolymarketAdapter | None = None,
) -> pd.DataFrame:
    """Fetch hourly price history for Polymarket token IDs via CLOB API.

    Returns a DataFrame indexed by UTC DatetimeIndex (hourly grid),
    columns = token_ids, values = mid-market prices (0-1).
    """
    start_ts = int(pd.Timestamp(start_date, tz="UTC").timestamp())
    end_ts = int(pd.Timestamp(end_date, tz="UTC").timestamp())

    close_adapter = adapter is None
    if adapter is None:
        adapter = PolymarketAdapter(config={})

    try:
        ok, raw = await adapter.get_batch_prices_history(
            token_ids=token_ids,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=fidelity,
        )
        if not ok:
            raise RuntimeError(f"Failed to fetch prices: {raw}")

        return regularize_to_grid(
            raw,
            start=start_date,
            end=end_date,
            interval="1h",
            max_gap_hours=max_gap_hours,
        )
    finally:
        if close_adapter:
            await adapter.close()


async def fetch_market_metadata(
    condition_ids: list[str],
    adapter: PolymarketAdapter | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch market metadata for condition IDs via Gamma API.

    Returns {condition_id: {question, market_slug, end_date, resolved,
    outcomes, clob_token_ids, volume, ...}} keyed by condition ID.
    """
    close_adapter = adapter is None
    if adapter is None:
        adapter = PolymarketAdapter(config={})

    try:
        ok, data = await adapter.get_markets_by_condition_ids(
            condition_ids=condition_ids,
        )
        if not ok:
            raise RuntimeError(f"Failed to fetch market metadata: {data}")

        result: dict[str, dict[str, Any]] = {}
        for cid, market in data.items():
            tokens = market.get("clobTokenIds") or []
            outcomes_raw = market.get("outcomes") or []
            result[cid] = {
                "condition_id": cid,
                "market_slug": market.get("slug", ""),
                "question": market.get("question", ""),
                "end_date_iso": market.get("endDate", ""),
                "resolved": bool(market.get("closed", False)),
                "volume_usdc": float(market.get("volumeNum", 0)),
                "outcomes": outcomes_raw,
                "tokens": [
                    {
                        "token_id": tid,
                        "outcome": outcomes_raw[i]
                        if i < len(outcomes_raw)
                        else f"outcome_{i}",
                    }
                    for i, tid in enumerate(tokens)
                ],
            }
        return result
    finally:
        if close_adapter:
            await adapter.close()
