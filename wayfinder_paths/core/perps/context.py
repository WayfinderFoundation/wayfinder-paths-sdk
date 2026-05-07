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
        # Use loc with .iloc fallback for label exactness.
        return self.targets.loc[t]


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
