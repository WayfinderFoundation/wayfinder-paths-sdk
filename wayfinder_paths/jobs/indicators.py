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
        "volpct:N (ATR percentile)"
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
