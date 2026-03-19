"""
Data fetching utilities for backtesting.

Provides simple interfaces to fetch price, funding rate, and borrow rate data
in backtest-ready DataFrame format.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from wayfinder_paths.core.clients import DELTA_LAB_CLIENT
from wayfinder_paths.core.clients.HyperliquidDataClient import HyperliquidDataClient


def get_available_date_range() -> tuple[datetime, datetime]:
    """
    Get the available data retention window.

    Returns:
        (oldest_date, newest_date) tuple

    Note:
        Both Delta Lab and Hyperliquid retain approximately 7 months of historical
        data. A 200-day safe window is used to avoid boundary rejections.
    """
    # 211 days matches confirmed Delta Lab + Hyperliquid retention.
    # Snap to midnight so that a start_date like "2025-08-11" (midnight) is never rejected
    # because datetime.now() has a time component that makes midnight appear to fall
    # before "2025-08-11 14:30:00 - 211 days".
    retention_days = 211
    newest = datetime.now()
    oldest = (newest - timedelta(days=retention_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return oldest, newest


def validate_date_range(start_date: str, end_date: str) -> tuple[bool, str | None]:
    """
    Validate that requested dates are within data retention window.

    Args:
        start_date: Start date in ISO format ("2025-01-01")
        end_date: End date in ISO format ("2025-02-01")

    Returns:
        (is_valid, error_message) tuple. error_message is None if valid.

    Example:
        >>> valid, error = validate_date_range("2025-01-01", "2025-02-01")
        >>> if not valid:
        ...     raise ValueError(error)
    """
    oldest, newest = get_available_date_range()

    try:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
    except ValueError as e:
        return False, f"Invalid date format: {e}"

    if start < oldest:
        return False, (
            f"Start date {start_date} is outside the safe retention window. "
            f"Use {oldest.date().isoformat()} or later. "
            f"(Retention is 211 days; window snapped to midnight to avoid time-of-day rejections.)"
        )

    if end > newest + timedelta(
        days=1
    ):  # Allow small future buffer for timezone issues
        return False, f"End date {end_date} is in the future"

    if start >= end:
        return False, "Start date must be before end date"

    return True, None


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

    Raises:
        ValueError: If date range is invalid or outside retention window

    Example:
        >>> prices = await fetch_prices(["BTC", "ETH"], "2025-01-01", "2025-02-01")
        >>> print(prices.head())
    """
    # Validate date range
    valid, error = validate_date_range(start_date, end_date)
    if not valid:
        raise ValueError(error)

    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    lookback_days = (end - start).days

    _SUB_HOURLY = {"1m", "5m", "15m"}
    _INTERVAL_TO_FREQ = {"1h": "1h", "4h": "4h", "1d": "1D"}

    if source == "auto":
        source = "hyperliquid" if interval in _SUB_HOURLY else "delta_lab"

    if source == "delta_lab":
        if interval in _SUB_HOURLY:
            raise ValueError(
                f"Delta Lab only provides hourly data; sub-hourly interval '{interval}' "
                f"is not supported. Use source='hyperliquid' for sub-hourly data."
            )
        result = await _fetch_prices_delta_lab(symbols, lookback_days, end)
        if interval != "1h":
            freq = _INTERVAL_TO_FREQ.get(interval)
            if freq:
                result = result.resample(freq).last().dropna(how="all")
        return result
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
    venue: str = "hyperliquid",
) -> pd.DataFrame:
    """
    Fetch funding rates for perpetual futures.

    **CRITICAL: Funding Rate Sign Convention**
        - **Positive funding (+)**: Longs PAY shorts → Good for shorts (receive funding)
        - **Negative funding (-)**: Shorts PAY longs → Bad for shorts (pay funding)

        This is backwards from intuition for many traders!

        Example:
            funding_rate = 0.08  # +8% annually
            # Longs pay shorts → collect funding by shorting

            funding_rate = -0.08  # -8% annually
            # Shorts pay longs → you PAY funding if short (bad!)

    Args:
        symbols: List of perp symbols (e.g., ["BTC", "ETH"])
        start_date: Start date (ISO format: "2025-01-01")
        end_date: End date (ISO format: "2025-02-01")
        venue: Venue to use for funding rates (default: "hyperliquid"). The funding
               timeseries may contain multiple venues per timestamp; this filters to
               a single venue so the result has one row per timestamp per symbol.

    Returns:
        DataFrame with index=timestamps, columns=symbols, values=funding_rates

    Raises:
        ValueError: If date range is invalid or outside retention window

    Example:
        >>> funding = await fetch_funding_rates(["BTC", "ETH"], "2025-01-01", "2025-02-01")
        >>> print(funding.head())
    """
    # Validate date range
    valid, error = validate_date_range(start_date, end_date)
    if not valid:
        raise ValueError(error)

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
                if "venue" in funding_df.columns:
                    funding_df = funding_df[funding_df["venue"] == venue]
                if not funding_df.empty:
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
        protocol: Venue name to filter to (e.g. "aave-v3-base", "moonwell-base",
            "morpho-base"). Names include the chain suffix — use fetch_lending_rates()
            with no venues filter to discover available names. When None, rates are
            averaged across all venues per timestamp.

    Returns:
        DataFrame with index=timestamps, columns=symbols, values=borrow_rates

    Raises:
        ValueError: If date range is invalid or outside retention window

    Example:
        >>> rates = await fetch_borrow_rates(["USDC", "ETH"], "2025-01-01", "2025-02-01")
        >>> print(rates.head())
    """
    # Validate date range
    valid, error = validate_date_range(start_date, end_date)
    if not valid:
        raise ValueError(error)

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

    # Warn if DataFrames have very different frequencies — the result will be upsampled
    # to the finest frequency, which silently breaks periods_per_year assumptions.
    def _median_seconds(df: pd.DataFrame) -> float | None:
        if len(df) < 2:
            return None
        diffs = pd.Series(df.index).diff().dropna()
        return float(diffs.median().total_seconds())

    freqs = [_median_seconds(df) for df in dfs]
    valid_freqs = [(i, f) for i, f in enumerate(freqs) if f is not None]
    if len(valid_freqs) >= 2:
        min_secs = min(f for _, f in valid_freqs)
        max_secs = max(f for _, f in valid_freqs)
        if max_secs / min_secs > 5:

            def _fmt(s: float) -> str:
                if s >= 3600:
                    return f"{s / 3600:.0f}h"
                return f"{s / 60:.0f}m"

            freq_desc = ", ".join(f"df[{i}]≈{_fmt(s)}" for i, s in valid_freqs)
            print(
                f"⚠️  align_dataframes: inputs have different frequencies ({freq_desc}, "
                f"ratio {max_secs / min_secs:.0f}x). All series forward-filled to finest "
                f"frequency. Update periods_per_year to match (e.g. 365→8760 if daily→hourly)."
            )

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


async def fetch_supply_rates(
    symbols: list[str],
    start_date: str,
    end_date: str,
    protocol: str | None = None,
) -> pd.DataFrame:
    """
    Fetch lending protocol supply (deposit) rates.

    Parallel to fetch_borrow_rates() — returns APR averaged across venues per symbol.
    For per-venue breakdown (needed for rotation/carry strategies), use fetch_lending_rates().

    Args:
        symbols: List of asset symbols (e.g., ["USDC", "ETH"])
        start_date: Start date (ISO format: "2025-08-01")
        end_date: End date (ISO format: "2026-01-01")
        protocol: Venue name to filter to (e.g. "aave-v3-base", "moonwell-base",
            "morpho-base"). Names include the chain suffix — use fetch_lending_rates()
            with no venues filter to discover available names. When None, rates are
            averaged across all venues per timestamp.

    Returns:
        DataFrame with index=timestamps, columns=symbols, values=supply_apr (decimal APR)

    Example:
        >>> rates = await fetch_supply_rates(["USDC"], "2025-08-01", "2026-01-01")
        >>> prices = (1 + rates / 365).cumprod()  # Build synthetic yield index
    """
    valid, error = validate_date_range(start_date, end_date)
    if not valid:
        raise ValueError(error)

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
                if "supply_apr" in lending_df.columns:
                    grouped = lending_df.groupby(lending_df.index)["supply_apr"].mean()
                    all_rates.append(grouped.rename(symbol))

    if not all_rates:
        raise ValueError("No supply rate data found")

    result = pd.concat(all_rates, axis=1)
    result.index = pd.to_datetime(result.index)
    return result.sort_index()


async def fetch_lending_rates(
    symbol: str,
    start_date: str,
    end_date: str,
    venues: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch supply and borrow rates broken down by venue for a single asset.

    Use this for multi-venue strategies (rotation, carry trade) where you need
    to compare rates across protocols. For a simple averaged rate, use
    fetch_supply_rates() / fetch_borrow_rates() instead.

    Args:
        symbol: Asset symbol (e.g., "USDC", "ETH")
        start_date: Start date (ISO format: "2025-08-01")
        end_date: End date (ISO format: "2026-01-01")
        venues: Venue filter (e.g., ["aave", "moonwell", "morpho"]), or None for all

    Returns:
        Dict with keys "supply" and "borrow", each a DataFrame:
        - index: timestamps
        - columns: venue names
        - values: APR as decimal (0.05 = 5% annually)

    Example:
        >>> rates = await fetch_lending_rates("USDC", "2025-08-01", "2026-01-01",
        ...                                   venues=["aave", "moonwell", "morpho"])
        >>> supply = rates["supply"]  # DataFrame[timestamp × venue]
        >>> borrow = rates["borrow"]  # DataFrame[timestamp × venue]
        >>> spread = supply.max(axis=1) - borrow.min(axis=1)  # Best carry spread
    """
    valid, error = validate_date_range(start_date, end_date)
    if not valid:
        raise ValueError(error)

    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    lookback_days = (end - start).days

    data = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol=symbol,
        lookback_days=lookback_days,
        limit=10000,
        as_of=end,
        series="lending",
    )

    if "lending" not in data or data["lending"].empty:
        raise ValueError(f"No lending data found for {symbol}")

    lending_df = data["lending"]
    if venues:
        available = (
            sorted(lending_df["venue"].unique().tolist())
            if "venue" in lending_df.columns
            else []
        )
        unknown = [v for v in venues if v not in available]
        if unknown:
            raise ValueError(
                f"Unknown venue(s) {unknown} for {symbol}. Available: {available}"
            )
        lending_df = lending_df[lending_df["venue"].isin(venues)]

    supply = lending_df.pivot_table(
        index=lending_df.index, columns="venue", values="supply_apr", aggfunc="mean"
    )
    borrow = lending_df.pivot_table(
        index=lending_df.index, columns="venue", values="borrow_apr", aggfunc="mean"
    )

    supply.index = pd.to_datetime(supply.index)
    borrow.index = pd.to_datetime(borrow.index)

    # Resample to hourly and ffill with a 24-hour limit.
    # Raw snapshots are expected hourly but can have gaps. Without a limit, a single
    # stale snapshot propagates indefinitely through the series and corrupts backtests.
    supply = supply.sort_index().resample("1h").last().ffill(limit=24)
    borrow = borrow.sort_index().resample("1h").last().ffill(limit=24)

    # Strip column axis name (pivot_table sets columns.name="venue") for cleaner access
    supply.columns.name = None
    borrow.columns.name = None

    return {"supply": supply, "borrow": borrow}


def convert_to_spot(prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Convert price data to spot representation with zero funding rates.

    Spot assets trade without funding rates (unlike perpetual futures which have
    periodic funding payments). This function is useful for creating the spot leg
    of delta-neutral strategies where you need matching price data but no funding.

    In reality, spot prices and perp mark prices converge due to arbitrage and
    funding mechanisms. For backtesting purposes, using the same price series for
    both spot and perp is a reasonable approximation - the key difference is that
    only perp positions receive/pay funding.

    Args:
        prices: Price DataFrame with index=timestamps, columns=symbols

    Returns:
        Tuple of (prices_df, funding_rates_df):
        - prices_df: Same as input (spot prices ≈ perp prices in practice)
        - funding_rates_df: Zero funding rates with same shape as prices

    Example:
        >>> perp_prices = await fetch_prices(["BTC", "ETH"], "2025-01-01", "2025-02-01")
        >>> perp_funding = await fetch_funding_rates(["BTC", "ETH"], "2025-01-01", "2025-02-01")
        >>>
        >>> # Create spot leg (no funding)
        >>> spot_prices, spot_funding = convert_to_spot(perp_prices)
        >>>
        >>> # For delta-neutral: combine both legs
        >>> # Perp: short to collect funding, Spot: long to hedge
        >>> all_prices = pd.concat([
        ...     perp_prices.add_suffix("_PERP"),
        ...     spot_prices.add_suffix("_SPOT")
        ... ], axis=1)
        >>> all_funding = pd.concat([
        ...     perp_funding.add_suffix("_PERP"),
        ...     spot_funding.add_suffix("_SPOT")
        ... ], axis=1)
    """
    # Spot prices are the same as input (spot ≈ perp prices converge in reality)
    spot_prices = prices.copy()

    # Spot assets have zero funding (no periodic payments)
    zero_funding = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)

    return spot_prices, zero_funding
