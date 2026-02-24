"""
Data fetching utilities for backtesting.

Provides simple interfaces to fetch price, funding rate, and borrow rate data
in backtest-ready DataFrame format.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from wayfinder_paths.core.clients import DELTA_LAB_CLIENT
from wayfinder_paths.core.clients.HyperliquidDataClient import HyperliquidDataClient


async def fetch_prices(
    symbols: list[str],
    start_date: str,
    end_date: str,
    interval: str = "1h",
    source: str = "auto",
) -> pd.DataFrame:
    """
    Fetch price data in backtest-ready format.

    Args:
        symbols: List of symbols (e.g., ["BTC", "ETH"])
        start_date: Start date (ISO format: "2025-01-01")
        end_date: End date (ISO format: "2025-02-01")
        interval: Time interval ("1m", "5m", "15m", "1h", "4h", "1d")
        source: Data source ("auto", "delta_lab", "hyperliquid")

    Returns:
        DataFrame with index=timestamps, columns=symbols, values=prices

    Example:
        >>> prices = await fetch_prices(["BTC", "ETH"], "2025-01-01", "2025-02-01")
        >>> print(prices.head())
    """
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    lookback_days = (end - start).days

    if source == "auto":
        source = "delta_lab"

    if source == "delta_lab":
        return await _fetch_prices_delta_lab(symbols, lookback_days, end)
    elif source == "hyperliquid":
        return await _fetch_prices_hyperliquid(symbols, start, end, interval)
    else:
        raise ValueError(f"Unknown source: {source}")


async def _fetch_prices_delta_lab(
    symbols: list[str], lookback_days: int, as_of: datetime
) -> pd.DataFrame:
    """Fetch prices from Delta Lab timeseries."""
    all_prices = []

    for symbol in symbols:
        data = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=symbol,
            lookback_days=lookback_days,
            limit=10000,
            as_of=as_of,
            series="price",
        )

        if "price" in data:
            price_df = data["price"]
            if not price_df.empty and "price_usd" in price_df.columns:
                price_series = price_df["price_usd"].rename(symbol)
                all_prices.append(price_series)

    if not all_prices:
        raise ValueError("No price data found")

    result = pd.concat(all_prices, axis=1)
    result.index = pd.to_datetime(result.index)
    return result.sort_index()


async def _fetch_prices_hyperliquid(
    symbols: list[str], start: datetime, end: datetime, interval: str
) -> pd.DataFrame:
    """Fetch prices from Hyperliquid candles."""
    client = HyperliquidDataClient()
    all_prices = []

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    for symbol in symbols:
        candles = await client.get_candles(symbol, start_ms, end_ms, interval)

        if candles:
            df = pd.DataFrame(candles)
            df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
            df = df.set_index("timestamp")
            price_series = df["c"].astype(float).rename(symbol)
            all_prices.append(price_series)

    if not all_prices:
        raise ValueError("No price data found")

    result = pd.concat(all_prices, axis=1)
    return result.sort_index()


async def fetch_funding_rates(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    Fetch funding rates for perpetual futures.

    Args:
        symbols: List of perp symbols (e.g., ["BTC", "ETH"])
        start_date: Start date (ISO format: "2025-01-01")
        end_date: End date (ISO format: "2025-02-01")

    Returns:
        DataFrame with index=timestamps, columns=symbols, values=funding_rates

    Example:
        >>> funding = await fetch_funding_rates(["BTC", "ETH"], "2025-01-01", "2025-02-01")
        >>> print(funding.head())
    """
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    lookback_days = (end - start).days

    all_funding = []

    for symbol in symbols:
        data = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=symbol,
            lookback_days=lookback_days,
            limit=10000,
            as_of=end,
            series="funding",
        )

        if "funding" in data:
            funding_df = data["funding"]
            if not funding_df.empty and "funding_rate" in funding_df.columns:
                funding_series = funding_df["funding_rate"].rename(symbol)
                all_funding.append(funding_series)

    if not all_funding:
        raise ValueError("No funding rate data found")

    result = pd.concat(all_funding, axis=1)
    result.index = pd.to_datetime(result.index)
    return result.sort_index()


async def fetch_borrow_rates(
    symbols: list[str],
    start_date: str,
    end_date: str,
    protocol: str | None = None,
) -> pd.DataFrame:
    """
    Fetch lending protocol borrow rates.

    Args:
        symbols: List of asset symbols (e.g., ["USDC", "ETH"])
        start_date: Start date (ISO format: "2025-01-01")
        end_date: End date (ISO format: "2025-02-01")
        protocol: Protocol filter ("aave", "morpho", "moonwell", or None for all)

    Returns:
        DataFrame with index=timestamps, columns=symbols, values=borrow_rates

    Example:
        >>> rates = await fetch_borrow_rates(["USDC", "ETH"], "2025-01-01", "2025-02-01")
        >>> print(rates.head())
    """
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    lookback_days = (end - start).days

    all_rates = []

    for symbol in symbols:
        data = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=symbol,
            lookback_days=lookback_days,
            limit=10000,
            as_of=end,
            series="lending",
        )

        if "lending" in data:
            lending_df = data["lending"]

            if not lending_df.empty:
                if protocol:
                    lending_df = lending_df[lending_df["venue"] == protocol]

                if "borrow_apr" in lending_df.columns:
                    grouped = lending_df.groupby(lending_df.index)["borrow_apr"].mean()
                    rate_series = grouped.rename(symbol)
                    all_rates.append(rate_series)

    if not all_rates:
        raise ValueError("No borrow rate data found")

    result = pd.concat(all_rates, axis=1)
    result.index = pd.to_datetime(result.index)
    return result.sort_index()


async def align_dataframes(
    *dfs: pd.DataFrame, method: str = "ffill"
) -> tuple[pd.DataFrame, ...]:
    """
    Align multiple DataFrames to common timestamps.

    Args:
        *dfs: DataFrames to align
        method: Fill method ("ffill", "bfill", "interpolate", "drop")

    Returns:
        Tuple of aligned DataFrames

    Example:
        >>> prices, funding = await align_dataframes(prices_df, funding_df)
    """
    if not dfs:
        return ()

    combined_index = dfs[0].index
    for df in dfs[1:]:
        combined_index = combined_index.union(df.index)

    combined_index = combined_index.sort_values()

    aligned = []
    for df in dfs:
        reindexed = df.reindex(combined_index)

        if method == "ffill":
            reindexed = reindexed.ffill()
        elif method == "bfill":
            reindexed = reindexed.bfill()
        elif method == "interpolate":
            reindexed = reindexed.interpolate()
        elif method == "drop":
            reindexed = reindexed.dropna()
        else:
            raise ValueError(f"Unknown method: {method}")

        aligned.append(reindexed)

    return tuple(aligned)
