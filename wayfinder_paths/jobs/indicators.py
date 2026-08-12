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


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    prev_close = frame["close"].astype(float).shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def _spec_error(spec: str, reason: str) -> ValueError:
    return ValueError(
        f"bad indicator spec {spec!r}: {reason}. Known specs: sma:N, ema:N, "
        "rsi:N, atr:N, bb:N:K (bollinger %B and bandwidth), macd:F:S:SIG, "
        "don:N (donchian position), vwap, vr:N (variance ratio), "
        "volpct:N (ATR percentile), clv (close location in bar range), "
        "wickratio:N (upper/lower wick share, N-bar mean), volz:N (volume "
        "z-score), vwapdist (bps from session VWAP), daylevel (bps to prior "
        "UTC-day high/low), rvratio:N:M (short-vs-long realized vol), "
        "sigmabars:K (bars since last K-sigma move), fundclock (bars "
        "since/until the 8h funding settlement)"
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


def classify_regimes(frame: pd.DataFrame) -> pd.Series:
    """Causal per-bar regime label: trend (close vs SMA50) x vol split
    (ATR14/close vs its EXPANDING median — no future data). 2x2 keeps each
    cell's sample size workable for conditional stats; the same labels gate
    live legs via an `enabled_regimes` param, so what the scan conditioned on
    is exactly what the strategy trades."""
    close = frame["close"].astype(float)
    trend_up = close > close.rolling(50).mean()
    vol = atr(frame, 14) / close
    vol_high = vol > vol.expanding(min_periods=50).median()
    labels = pd.Series("down_lowvol", index=frame.index, dtype=object)
    labels[trend_up & vol_high] = "up_highvol"
    labels[trend_up & ~vol_high] = "up_lowvol"
    labels[~trend_up & vol_high] = "down_highvol"
    warmup = (
        close.rolling(50).mean().isna() | vol.expanding(min_periods=50).median().isna()
    )
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
