"""TriggerContext + SignalFrame: the inputs handed to decide()."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from wayfinder_paths.core.perps.handlers.protocol import MarketHandler
from wayfinder_paths.core.perps.state import StateStore


@dataclass
class SignalFrame:
    """Output of compute_signal — one column per symbol, one row per bar.

    Cell value is a target weight or target size. Convention is fixed by the strategy
    (the default `decide` interprets values as target sizes in base units).
    """

    targets: pd.DataFrame  # index=timestamps, columns=symbols
    extras: dict[str, pd.DataFrame] = field(default_factory=dict)

    def at(self, t: datetime) -> pd.Series:
        """Lookup the target row at-or-before `t`.

        Backtest: `t` aligns to a bar in `targets.index` exactly — fast path.
        Live: `t` is `handler.now()` (wall-clock); we floor to the latest bar
        whose timestamp is ≤ t (`get_indexer(method="ffill")`). If `t` is
        before any bar, falls back to the first row.
        """
        idx = self.targets.index
        ts = pd.Timestamp(t)
        if ts.tzinfo is not None and idx.tz is None:
            ts = ts.tz_localize(None)
        elif ts.tzinfo is None and idx.tz is not None:
            ts = ts.tz_localize(idx.tz)
        try:
            return self.targets.loc[ts]
        except KeyError:
            pass
        pos = idx.get_indexer([ts], method="ffill")[0]
        if pos < 0:
            return self.targets.iloc[0]
        return self.targets.iloc[pos]


def normalize_signal(out: Any, fallback_index: pd.DatetimeIndex | None = None,
                     fallback_columns: list[str] | None = None) -> SignalFrame:
    """Wrap a `compute_signal` return value into a `SignalFrame`.

    Accepts `SignalFrame` (returned as-is), `pd.DataFrame`, or `pd.Series`.
    Used in both backtest driver and live `_run_trigger` so strategies can return
    a plain DataFrame without worrying about wrapping.
    """
    if isinstance(out, SignalFrame):
        return out
    if isinstance(out, pd.DataFrame):
        df = out
        if fallback_index is not None:
            df = df.reindex(index=fallback_index)
        if fallback_columns is not None:
            df = df.reindex(columns=fallback_columns)
        return SignalFrame(targets=df.fillna(0.0))
    if isinstance(out, pd.Series):
        return SignalFrame(targets=out.to_frame().T)
    raise TypeError(f"signal_fn must return SignalFrame, DataFrame, or Series — got {type(out).__name__}")


@dataclass
class TriggerContext:
    perp: MarketHandler
    hip3: dict[str, MarketHandler]
    params: dict[str, Any]
    state: StateStore
    signal: SignalFrame
    t: datetime

    def signal_at_now(self) -> pd.Series:
        return self.signal.at(self.t)

    def venue(self, key: str) -> MarketHandler:
        """Look up a handler by 'perp' or 'hip3:<dex>'."""
        if key == "perp":
            return self.perp
        if key.startswith("hip3:"):
            return self.hip3[key.removeprefix("hip3:")]
        raise KeyError(f"Unknown venue key: {key!r}")
