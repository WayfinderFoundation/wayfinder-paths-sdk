"""Signal computation for APEX/GMX Pair Velocity Strategy.

Velocity-filtered z-score on log(APEX / GMX). At each bar:
  - z = (log(APEX/GMX) - rolling_mean) / rolling_std over `lookback_bars`
  - dz = z[t] - z[t-velocity_bars]
  - LONG APEX / SHORT GMX when z < -entry_z AND dz > 0 (extreme + reverting up)
  - SHORT APEX / LONG GMX when z > +entry_z AND dz < 0 (extreme + reverting down)
  - Exit when z crosses zero

Output: SignalFrame with target weights per symbol per timestamp. Sum |w|
equals `target_leverage` when an entry is active, 0 when flat. Each leg
gets ±target_leverage/2.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from wayfinder_paths.core.perps.context import SignalFrame


def compute_signal(
    prices: pd.DataFrame,
    funding: pd.DataFrame | None,
    params: dict,
) -> SignalFrame:
    """Compute APEX/GMX pair velocity signal.

    Args:
        prices: DataFrame columns include APEX and GMX, hourly bars.
        funding: ignored.
        params: must contain `lookback_bars`, `entry_z`, `velocity_bars`,
            `target_leverage`, and `symbols` (the universe — must include
            both APEX and GMX).

    Returns:
        SignalFrame with one column per symbol in `prices.columns`.
        Non-pair symbols always carry weight 0.
    """
    lb = int(params["lookback_bars"])
    ez = float(params["entry_z"])
    vb = int(params.get("velocity_bars", 6))
    target_leverage = float(params["target_leverage"])
    a, b = "APEX", "GMX"

    leg_weight = target_leverage / 2.0  # half each side
    syms = list(prices.columns)
    n = len(prices)
    pos = np.zeros((n, len(syms)))
    if a not in syms or b not in syms:
        return SignalFrame(
            targets=pd.DataFrame(pos, index=prices.index, columns=syms).fillna(0.0)
        )

    si = {s: i for i, s in enumerate(syms)}
    lr = np.log(prices[a].values / prices[b].values)
    s = pd.Series(lr)
    rm = s.rolling(lb).mean().values
    rs = s.rolling(lb).std().values
    z = np.where(rs > 0, (lr - rm) / rs, 0.0)

    state = 0  # 0 flat, +1 long-A/short-B, -1 short-A/long-B
    for i in range(lb, n):
        zi = 0.0 if np.isnan(z[i]) else float(z[i])
        zprev = z[i - vb] if i >= vb and not np.isnan(z[i - vb]) else zi
        dz = zi - zprev
        # Exit on z-cross-zero
        if state == 1 and zi >= 0:
            state = 0
        elif state == -1 and zi <= 0:
            state = 0
        # Enter only with velocity confirmation
        if state == 0:
            if zi < -ez and dz > 0:
                state = 1
            elif zi > ez and dz < 0:
                state = -1
        if state == 1:
            pos[i, si[a]] = leg_weight
            pos[i, si[b]] = -leg_weight
        elif state == -1:
            pos[i, si[a]] = -leg_weight
            pos[i, si[b]] = leg_weight

    targets = pd.DataFrame(pos, index=prices.index, columns=syms).fillna(0.0)
    return SignalFrame(targets=targets)
