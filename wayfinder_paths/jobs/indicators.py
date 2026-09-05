"""On-demand indicator engine for the agent's chart lens.

Parses compact indicator specs ("ema:9", "rsi:14", "bb:20:2", "macd:12:26:9")
and computes the columns on a single-symbol OHLCV frame. Conventions match the
strategies and the signal library (Wilder RSI via ewm(alpha=1/n), right-closed
resamples upstream) so what the agent sees is what the stats test.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

MAX_INDICATORS = 8
# A Wilder/EMA recursion truncated to ``multiple`` periods matches the
# open-ended one to within (1 - 1/period) ** ((multiple - 1) * period), about
# 7e-4 at the default; the strategy's declared window can then be exact.
BOUNDED_EWM_MULTIPLE = 8


def bounded_span(period: int, *, multiple: int = BOUNDED_EWM_MULTIPLE) -> int:
    """Trailing bars a bounded Wilder/EMA needs; declare at least this many."""
    if period <= 0:
        raise ValueError("indicator period must be positive")
    return int(period) * int(multiple)


def bounded_recursive_mean(
    values: pd.Series, alpha: float, *, seed: int, window: int
) -> pd.Series:
    """Wilder/EMA recursion over a fixed trailing ``window`` of bars.

    Seeded with the simple mean of the oldest ``seed`` bars in the window,
    then recursed over the rest — the textbook Wilder start, applied at every
    bar.  The value at bar t depends only on bars t-window+1..t, so a strategy
    that declares ``warmup_bars >= window`` is window-invariant by
    construction instead of by a long-history approximation.
    """
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    if seed <= 0 or window <= seed:
        raise ValueError("window must exceed the seed length")
    raw = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(raw)
    if not finite.any():
        return pd.Series(np.nan, index=values.index)
    lead = int(np.argmax(finite))
    filled = np.where(finite, raw, 0.0)
    tail = window - seed
    weights = np.empty(window)
    weights[:tail] = alpha * (1 - alpha) ** np.arange(tail)
    weights[tail:] = (1 - alpha) ** tail / seed
    out = np.convolve(filled, weights, mode="full")[: len(filled)]
    out[: min(len(out), lead + window - 1)] = np.nan
    return pd.Series(out, index=values.index)


def wilder_rsi(
    close: pd.Series, period: int = 14, *, window: int | None = None
) -> pd.Series:
    """Wilder RSI; ``window`` bounds its memory to that many trailing bars."""
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    if window is None:
        gain = gains.ewm(alpha=1 / period, adjust=False).mean()
        loss = losses.ewm(alpha=1 / period, adjust=False).mean()
    else:
        gain = bounded_recursive_mean(gains, 1 / period, seed=period, window=window)
        loss = bounded_recursive_mean(losses, 1 / period, seed=period, window=window)
    rs = gain / loss.replace(0.0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(
    frame: pd.DataFrame, period: int = 14, *, window: int | None = None
) -> pd.Series:
    """Wilder ATR; ``window`` bounds its memory to that many trailing bars."""
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    prev_close = frame["close"].astype(float).shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    if window is None:
        return true_range.ewm(alpha=1 / period, adjust=False).mean()
    return bounded_recursive_mean(true_range, 1 / period, seed=period, window=window)


def trailing_return(close: pd.Series, period: int) -> pd.Series:
    """Simple trailing return in decimal units (``0.10`` means 10%)."""
    if period <= 0:
        raise ValueError("trailing return period must be positive")
    return pd.to_numeric(close, errors="coerce").pct_change(period)


def realized_volatility(close: pd.Series, period: int) -> pd.Series:
    """Rolling stdev of simple per-bar returns; intentionally not annualized."""
    if period <= 0:
        raise ValueError("realized volatility period must be positive")
    returns = pd.to_numeric(close, errors="coerce").pct_change()
    return returns.rolling(period, min_periods=period).std()


def panel_breadth(
    panel: pd.DataFrame, threshold: float, *, min_assets: int
) -> pd.Series:
    """Fraction of finite synchronized assets at or above ``threshold``.

    Rows with fewer than ``min_assets`` finite observations are unknown rather
    than bearish. Callers can therefore require their configured universe
    before using breadth as a trading gate.
    """
    if min_assets <= 0:
        raise ValueError("panel breadth min_assets must be positive")
    numeric = panel.apply(pd.to_numeric, errors="coerce")
    finite = numeric.notna() & np.isfinite(numeric)
    eligible = finite.sum(axis=1)
    denominator = eligible.where(eligible.gt(0))
    breadth = (
        numeric.ge(float(threshold)).where(finite, False).sum(axis=1) / denominator
    )
    return breadth.where(eligible >= min_assets)


# Feature-feed columns the positioning indicators read. A job that declares
# the feed in its data contract sees the column on every bar (as-of merged);
# without it the indicators return NaN / a zero signal rather than raising.
FEED_FUNDING = "funding"
FEED_OPEN_INTEREST = "open_interest"
OI_CONFIRMATION_MODES = frozenset({"off", "building", "unwinding"})
FLUSH_SIDES = frozenset({"both", "long", "short"})


def funding_zscore(
    funding: pd.Series, window: int, *, min_periods: int | None = None
) -> pd.Series:
    """Rolling z-score of a (forward-filled) funding series over ``window`` bars.

    Hourly Hyperliquid funding on 15m bars is the calibrated use: 2,880 bars
    is 30 days. The default ``min_periods`` is a quarter of the window.
    """
    if window <= 1:
        raise ValueError("funding z-score window must exceed one bar")
    periods = max(10, window // 4) if min_periods is None else int(min_periods)
    values = pd.to_numeric(funding, errors="coerce").ffill()
    mean = values.rolling(window, min_periods=periods).mean()
    std = values.rolling(window, min_periods=periods).std()
    return (values - mean) / std.replace(0.0, np.nan)


def trailing_change(values: pd.Series, period: int) -> pd.Series:
    """Forward-filled level change over ``period`` bars (``0.10`` means +10%);
    the open-interest transform behind the positioning indicators."""
    if period <= 0:
        raise ValueError("trailing change period must be positive")
    filled = pd.to_numeric(values, errors="coerce").ffill()
    return filled / filled.shift(period) - 1.0


def _feed_column(frame: pd.DataFrame, column: str) -> pd.Series | None:
    return frame[column] if column in frame.columns else None


def funding_divergence_signal(
    frame: pd.DataFrame,
    *,
    z_window: int = 2880,
    z_entry: float = 2.0,
    confirm_bars: int = 96,
    confirm_max: float = 0.0,
    oi_bars: int = 96,
    oi_mode: str = "building",
) -> dict[str, pd.Series]:
    """Crowded positioning that price is not rewarding.

    ``signal`` is -1 where the funding z-score is above ``z_entry`` while the
    trailing ``confirm_bars`` return is at most ``confirm_max`` (longs pay up,
    price does not follow: fade them), +1 for the mirror, 0 otherwise.
    ``oi_mode`` adds the open-interest read over ``oi_bars``: ``"building"``
    requires the crowd to still be adding, ``"unwinding"`` requires it to be
    shrinking, ``"off"`` ignores it. Missing feeds leave the signal at 0.
    Returns ``funding_z``, ``confirm_return``, ``oi_change`` and ``signal``.
    """
    if oi_mode not in OI_CONFIRMATION_MODES:
        raise ValueError(
            "oi_mode must be one of " + ", ".join(sorted(OI_CONFIRMATION_MODES))
        )
    index = frame.index
    close = pd.to_numeric(frame["close"], errors="coerce")
    funding = _feed_column(frame, FEED_FUNDING)
    funding_z = (
        funding_zscore(funding, z_window)
        if funding is not None
        else pd.Series(np.nan, index=index)
    )
    confirm_return = close.pct_change(confirm_bars, fill_method=None)
    open_interest = _feed_column(frame, FEED_OPEN_INTEREST)
    oi_change = (
        trailing_change(open_interest, oi_bars)
        if open_interest is not None
        else pd.Series(np.nan, index=index)
    )
    if oi_mode == "building":
        oi_confirms = oi_change > 0
    elif oi_mode == "unwinding":
        oi_confirms = oi_change < 0
    else:
        oi_confirms = pd.Series(True, index=index)
    short = (funding_z > z_entry) & (confirm_return <= confirm_max) & oi_confirms
    long = (funding_z < -z_entry) & (confirm_return >= -confirm_max) & oi_confirms
    signal = pd.Series(0.0, index=index)
    signal[short.fillna(False)] = -1.0
    signal[long.fillna(False)] = 1.0
    return {
        "funding_z": funding_z,
        "confirm_return": confirm_return,
        "oi_change": oi_change,
        "signal": signal,
    }


def liquidation_flush_signal(
    frame: pd.DataFrame,
    *,
    return_bars: int = 96,
    return_min: float = 0.08,
    oi_bars: int = 96,
    oi_drop_min: float = 0.10,
    sides: str = "both",
) -> dict[str, pd.Series]:
    """A move that open interest did not survive.

    ``signal`` is +1 where the trailing ``return_bars`` return is at most
    ``-return_min`` while open interest fell by at least ``oi_drop_min`` over
    ``oi_bars`` (longs were liquidated into the drop: buy the flush), -1 for
    the mirror (a short squeeze), 0 otherwise; ``sides`` keeps ``"long"``,
    ``"short"`` or ``"both"``. Missing open interest leaves the signal at 0.
    Returns ``flush_return``, ``oi_change`` and ``signal``.
    """
    if sides not in FLUSH_SIDES:
        raise ValueError("sides must be one of " + ", ".join(sorted(FLUSH_SIDES)))
    if return_min <= 0 or oi_drop_min <= 0:
        raise ValueError("flush thresholds must be positive")
    index = frame.index
    close = pd.to_numeric(frame["close"], errors="coerce")
    flush_return = close.pct_change(return_bars, fill_method=None)
    open_interest = _feed_column(frame, FEED_OPEN_INTEREST)
    oi_change = (
        trailing_change(open_interest, oi_bars)
        if open_interest is not None
        else pd.Series(np.nan, index=index)
    )
    flushed = oi_change <= -oi_drop_min
    long = flushed & (flush_return <= -return_min)
    short = flushed & (flush_return >= return_min)
    if sides == "long":
        short = pd.Series(False, index=index)
    elif sides == "short":
        long = pd.Series(False, index=index)
    signal = pd.Series(0.0, index=index)
    signal[short.fillna(False)] = -1.0
    signal[long.fillna(False)] = 1.0
    return {"flush_return": flush_return, "oi_change": oi_change, "signal": signal}


def bars_since_signal(signal: pd.Series) -> pd.Series:
    """Bars since the last nonzero signal (0 on a signal bar, NaN before the
    first): a pure function of the bar history, so restarts cannot lose it."""
    active = pd.to_numeric(signal, errors="coerce").fillna(0.0) != 0
    positions = np.arange(len(signal))
    last_active = pd.Series(
        np.where(active, positions, np.nan), index=signal.index
    ).ffill()
    return pd.Series(positions, index=signal.index) - last_active


def _spec_error(spec: str, reason: str) -> ValueError:
    return ValueError(
        f"bad indicator spec {spec!r}: {reason}. Known specs: sma:N, ema:N, "
        "rsi:N, atr:N, ret:N, rv:N, bb:N:K (bollinger %B and bandwidth), "
        "macd:F:S:SIG, "
        "don:N (donchian position), vwap, vr:N (variance ratio), "
        "volpct:N (ATR percentile), clv (close location in bar range), "
        "wickratio:N (upper/lower wick share, N-bar mean), volz:N (volume "
        "z-score), vwapdist (bps from session VWAP), daylevel (bps to prior "
        "UTC-day high/low), rvratio:N:M (short-vs-long realized vol), "
        "sigmabars:K (bars since last K-sigma move), fundclock (bars "
        "since/until the 8h funding settlement), fundz:N (funding-rate z-score "
        "over N bars; needs the funding feed), oichg:N (open-interest change "
        "over N bars; needs the open_interest feed), funddiv:Z:R:W "
        "(funding/open-interest divergence side, +1 long / -1 short: z-score "
        "over W bars beyond Z with the R-bar return not confirming and open "
        "interest building; W defaults to 2880), "
        "flush:R:O (liquidation flush side: R% move against an O% "
        "open-interest drop over 96 bars)"
    )


def compute_indicator(frame: pd.DataFrame, spec: str) -> dict[str, pd.Series]:
    """One spec -> one or more named columns (e.g. macd yields line+signal)."""
    parts = [p for p in spec.strip().lower().split(":") if p]
    if not parts:
        raise _spec_error(spec, "empty")
    name, args = parts[0], parts[1:]
    try:
        nums = [float(a) for a in args]
    except ValueError as exc:
        raise _spec_error(spec, "non-numeric parameter") from exc
    close = frame["close"].astype(float)

    def _one(default: float) -> int:
        if len(nums) > 1:
            raise _spec_error(spec, "expected at most one parameter")
        return int(nums[0]) if nums else int(default)

    match name:
        case "sma":
            n = _one(20)
            return {f"sma{n}": close.rolling(n).mean()}
        case "ema":
            n = _one(20)
            return {f"ema{n}": close.ewm(span=n, adjust=False).mean()}
        case "rsi":
            n = _one(14)
            return {f"rsi{n}": wilder_rsi(close, n)}
        case "atr":
            n = _one(14)
            return {f"atr{n}": atr(frame, n)}
        case "ret":
            n = _one(24)
            return {f"ret{n}": trailing_return(close, n)}
        case "rv":
            n = _one(24)
            return {f"rv{n}": realized_volatility(close, n)}
        case "vwap":
            if nums:
                raise _spec_error(spec, "vwap takes no parameters")
            typical = (
                frame["high"].astype(float) + frame["low"].astype(float) + close
            ) / 3
            volume = frame["volume"].astype(float)
            return {"vwap": (typical * volume).cumsum() / volume.cumsum()}
        case "bb":
            if len(nums) > 2:
                raise _spec_error(spec, "expected bb:N:K")
            n = int(nums[0]) if nums else 20
            k = float(nums[1]) if len(nums) > 1 else 2.0
            mid = close.rolling(n).mean()
            std = close.rolling(n).std()
            upper, lower = mid + k * std, mid - k * std
            width = upper - lower
            return {
                f"bb{n}_pctb": (close - lower) / width.replace(0.0, np.nan),
                f"bb{n}_bw": width / mid,
            }
        case "macd":
            if nums and len(nums) != 3:
                raise _spec_error(spec, "expected macd:FAST:SLOW:SIGNAL")
            fast, slow, signal = (int(x) for x in (nums or [12, 26, 9]))
            line = (
                close.ewm(span=fast, adjust=False).mean()
                - close.ewm(span=slow, adjust=False).mean()
            )
            return {
                f"macd{fast}_{slow}": line,
                f"macds{signal}": line.ewm(span=signal, adjust=False).mean(),
            }
        case "don":
            n = _one(20)
            hi = frame["high"].astype(float).rolling(n).max()
            lo = frame["low"].astype(float).rolling(n).min()
            return {f"don{n}_pos": (close - lo) / (hi - lo).replace(0.0, np.nan)}
        case "vr":
            n = _one(24)
            r1 = close.pct_change()
            rn = close.pct_change(n)
            return {f"vr{n}": rn.rolling(200).var() / (n * r1.rolling(200).var())}
        case "volpct":
            n = _one(14)
            series = atr(frame, n) / close
            return {f"volpct{n}": series.expanding(min_periods=n).rank(pct=True) * 100}
        case "clv":
            if nums:
                raise _spec_error(spec, "clv takes no parameters")
            high = frame["high"].astype(float)
            low = frame["low"].astype(float)
            bar_range = (high - low).replace(0.0, np.nan)
            # 0 = closed on the low, 1 = closed on the high: rejection measure.
            return {"clv": (close - low) / bar_range}
        case "wickratio":
            n = _one(1)
            high = frame["high"].astype(float)
            low = frame["low"].astype(float)
            open_ = frame["open"].astype(float)
            bar_range = (high - low).replace(0.0, np.nan)
            upper = (high - pd.concat([open_, close], axis=1).max(axis=1)) / bar_range
            lower = (pd.concat([open_, close], axis=1).min(axis=1) - low) / bar_range
            if n > 1:
                upper, lower = upper.rolling(n).mean(), lower.rolling(n).mean()
            suffix = "" if n == 1 else str(n)
            return {f"uwick{suffix}": upper, f"lwick{suffix}": lower}
        case "volz":
            n = _one(20)
            volume = frame["volume"].astype(float)
            mean = volume.rolling(n).mean()
            std = volume.rolling(n).std().replace(0.0, np.nan)
            return {f"volz{n}": (volume - mean) / std}
        case "vwapdist":
            if nums:
                raise _spec_error(spec, "vwapdist takes no parameters")
            stamps = pd.to_datetime(frame["timestamp"], utc=True)
            day = stamps.dt.floor("D")
            typical = (
                frame["high"].astype(float) + frame["low"].astype(float) + close
            ) / 3
            volume = frame["volume"].astype(float)
            pv = (typical * volume).groupby(day).cumsum()
            cum_volume = volume.groupby(day).cumsum().replace(0.0, np.nan)
            session_vwap = pv / cum_volume
            return {"vwapdist_bps": (close / session_vwap - 1) * 1e4}
        case "daylevel":
            if nums:
                raise _spec_error(spec, "daylevel takes no parameters")
            stamps = pd.to_datetime(frame["timestamp"], utc=True)
            day = stamps.dt.floor("D")
            prev_high = frame["high"].astype(float).groupby(day).max().shift(1)
            prev_low = frame["low"].astype(float).groupby(day).min().shift(1)
            high_map = day.map(prev_high)
            low_map = day.map(prev_low)
            return {
                "pdh_dist_bps": (close / high_map - 1) * 1e4,
                "pdl_dist_bps": (close / low_map - 1) * 1e4,
            }
        case "rvratio":
            if nums and len(nums) != 2:
                raise _spec_error(spec, "expected rvratio:SHORT:LONG")
            short_n, long_n = (int(x) for x in (nums or [12, 288]))
            returns = close.pct_change()
            short_vol = returns.rolling(short_n).std()
            long_vol = returns.rolling(long_n).std().replace(0.0, np.nan)
            return {f"rvratio{short_n}_{long_n}": short_vol / long_vol}
        case "sigmabars":
            k = _one(2)
            returns = close.pct_change()
            sigma = returns.rolling(100).std()
            shock = (returns.abs() > k * sigma).astype(int)
            groups = shock.cumsum()
            return {f"sigmabars{k}": shock.groupby(groups).cumcount()}
        case "fundclock":
            if nums:
                raise _spec_error(spec, "fundclock takes no parameters")
            stamps = pd.to_datetime(frame["timestamp"], utc=True)
            seconds_into = (
                (stamps.dt.hour % 8) * 3600 + stamps.dt.minute * 60 + stamps.dt.second
            )
            bar_seconds = (
                stamps.diff().median().total_seconds() if len(stamps) > 1 else 300
            )
            # Bars since the last 00/08/16 UTC settlement (0 = the bar that
            # closed ON the boundary).
            return {"fundclock": (seconds_into / bar_seconds).round()}
        case "fundz":
            n = _one(2880)
            funding = _feed_column(frame, FEED_FUNDING)
            return {
                f"fundz{n}": funding_zscore(funding, n)
                if funding is not None
                else pd.Series(np.nan, index=frame.index)
            }
        case "oichg":
            n = _one(96)
            open_interest = _feed_column(frame, FEED_OPEN_INTEREST)
            return {
                f"oichg{n}": trailing_change(open_interest, n)
                if open_interest is not None
                else pd.Series(np.nan, index=frame.index)
            }
        case "funddiv":
            if len(nums) > 3:
                raise _spec_error(spec, "expected funddiv:Z:R:W")
            z_entry = float(nums[0]) if nums else 2.0
            bars = int(nums[1]) if len(nums) > 1 else 96
            z_window = int(nums[2]) if len(nums) > 2 else 2880
            divergence = funding_divergence_signal(
                frame,
                z_window=z_window,
                z_entry=z_entry,
                confirm_bars=bars,
                oi_bars=bars,
            )
            return {"funddiv": divergence["signal"]}
        case "flush":
            if len(nums) > 2:
                raise _spec_error(spec, "expected flush:R:O in percent")
            move_pct = float(nums[0]) if nums else 8.0
            drop_pct = float(nums[1]) if len(nums) > 1 else 10.0
            flush = liquidation_flush_signal(
                frame, return_min=move_pct / 100.0, oi_drop_min=drop_pct / 100.0
            )
            return {"flush": flush["signal"]}
        case _:
            raise _spec_error(spec, "unknown indicator")


def compute_indicators(
    frame: pd.DataFrame, specs: Sequence[str]
) -> dict[str, pd.Series]:
    if len(specs) > MAX_INDICATORS:
        raise ValueError(
            f"too many indicators ({len(specs)} > {MAX_INDICATORS}) — pick the "
            "lenses that test THIS hypothesis"
        )
    columns: dict[str, pd.Series] = {}
    for spec in specs:
        for key, series in compute_indicator(frame, spec).items():
            columns[key] = series
    return columns


REGIME_LABELS = ("up_lowvol", "up_highvol", "down_lowvol", "down_highvol")


def classify_regimes(
    frame: pd.DataFrame, *, vol_baseline_bars: int | None = None
) -> pd.Series:
    """Causal per-bar regime label: trend (close vs SMA50) x vol split
    (ATR14/close vs its EXPANDING median — no future data). 2x2 keeps each
    cell's sample size workable for conditional stats. Evolution uses the
    same label vocabulary for its engine-owned `target_regimes` attribution."""
    close = frame["close"].astype(float)
    trend_up = close > close.rolling(50).mean()
    vol = atr(frame, 14) / close
    if vol_baseline_bars is None:
        vol_threshold = vol.expanding(min_periods=50).median()
    else:
        if vol_baseline_bars < 50:
            raise ValueError("regime volatility baseline must be at least 50 bars")
        vol_threshold = (
            vol.rolling(vol_baseline_bars, min_periods=vol_baseline_bars)
            .median()
            .shift(1)
        )
    vol_high = vol > vol_threshold
    labels = pd.Series("down_lowvol", index=frame.index, dtype=object)
    labels[trend_up & vol_high] = "up_highvol"
    labels[trend_up & ~vol_high] = "up_lowvol"
    labels[~trend_up & vol_high] = "down_highvol"
    warmup = close.rolling(50).mean().isna() | vol_threshold.isna()
    labels[warmup] = None
    return labels


def current_regime(frame: pd.DataFrame) -> str | None:
    labels = classify_regimes(frame)
    tail = labels.dropna()
    return str(tail.iloc[-1]) if len(tail) else None


def regime_snapshot(frame: pd.DataFrame, at: pd.Timestamp) -> dict[str, object]:
    """Compact market-state tags at a timestamp: the context a human absorbs
    from the chart without thinking. Computed causally (data through `at`)."""
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    history = frame.loc[stamps <= at]
    if len(history) < 60:
        return {"insufficient_history": True}
    close = history["close"].astype(float)
    sma50 = close.rolling(50).mean().iloc[-1]
    atr14 = atr(history, 14) / close
    vol_pctile = float((atr14.iloc[-1] > atr14.dropna()).mean() * 100)
    hour = pd.Timestamp(at).tz_convert("UTC").hour
    session = (
        "asia" if hour < 7 else "europe" if hour < 13 else "us" if hour < 21 else "late"
    )
    return {
        "trend": "up" if close.iloc[-1] > sma50 else "down",
        "vol_pctile": round(vol_pctile, 1),
        "session": session,
    }
