"""Frozen BTC-relative positioning features and learned-analogue policy.

Pure research/live shared code: no providers, model-training dependency, or
orders. The backend fits the 64-tree model once per day and passes leaf IDs.
Returns refer to a 24-hour asset/BTC hedge, not an outright asset-price target
or the symmetric-bracket Pattern Match strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from wayfinder_paths.quant.pattern_match_universe import HISTORY_LIMIT, INTERVAL

POSITION_COLUMNS = [
    "position_count_imbalance",
    "position_concentration",
    "position_leverage",
    "position_growth",
]
PRICE_COLUMNS = [f"shape_{i}" for i in range(24)] + ["log_range", "log_variation"]
POSITIONING_COLUMNS = PRICE_COLUMNS + POSITION_COLUMNS
POSITIONING_HORIZON_BARS = 96
POSITIONING_COST = 0.0009
# The frozen development study's asset universe; BTC is the hedge, not a query.
POSITIONING_UNIVERSE = frozenset(
    {
        "AAVE",
        "ADA",
        "ASTER",
        "BNB",
        "CRV",
        "DOGE",
        "ENA",
        "ETH",
        "FARTCOIN",
        "HYPE",
        "LINK",
        "LIT",
        "MON",
        "NEAR",
        "PENGU",
        "PUMP",
        "SOL",
        "SPX",
        "SUI",
        "TAO",
        "TRUMP",
        "UNI",
        "XPL",
        "XRP",
        "ZEC",
        "ZRO",
    }
)
TREE_PARAMETERS = {
    "iterations": 64,
    "depth": 4,
    "loss_function": "MultiRMSE",
    "learning_rate": 0.05,
    "l2_leaf_reg": 10,
    "bootstrap_type": "No",
    "random_strength": 0,
    "random_seed": 20260921,
    "thread_count": 1,
    "allow_writing_files": False,
    "verbose": False,
}


def aggregate_positions(
    positions: pd.DataFrame, observed_at: pd.Timestamp
) -> pd.DataFrame:
    """Per-market aggregates only; never retain wallet identities."""
    values = positions[["size", "notional", "leverage"]]
    if not (
        positions.market.notna().all()
        and positions.market.str.strip().ne("").all()
        and np.isfinite(values).all().all()
        and positions["size"].ne(0).all()
        and positions[["notional", "leverage"]].gt(0).all().all()
    ):
        raise ValueError("Invalid open-position snapshot")
    frame = positions.assign(
        side=np.where(positions["size"] > 0, "long", "short"),
        units=positions["size"].abs(),
        squared_notional=positions.notional.pow(2),
        leveraged_notional=positions.notional * positions.leverage,
    )
    grouped = frame.groupby(["market", "side"]).agg(
        count=("size", "size"),
        notional=("notional", "sum"),
        units=("units", "sum"),
        squared_notional=("squared_notional", "sum"),
        leveraged_notional=("leveraged_notional", "sum"),
    )
    sides = grouped.unstack("side").reindex(
        columns=pd.MultiIndex.from_product([grouped.columns, ["long", "short"]])
    )
    count, notional = sides["count"], sides["notional"]
    concentration = sides["squared_notional"] / notional.pow(2)
    leverage = sides["leveraged_notional"] / notional
    return pd.DataFrame(
        {
            "coin": sides.index,
            "observed_at": observed_at,
            "position_count_imbalance": (
                (count.long - count.short) / (count.long + count.short)
            ).to_numpy(),
            "position_concentration": (
                concentration.long - concentration.short
            ).to_numpy(),
            "position_leverage": np.tanh(
                np.log(leverage.long / leverage.short)
            ).to_numpy(),
            "gross_units": (sides["units"].long + sides["units"].short).to_numpy(),
        }
    )


def position_state(snapshots: pd.DataFrame) -> pd.DataFrame:
    """A 24-hour minimum lag, with conservative source availability when supplied.

    Legacy research inputs omit source_modified_at and retain their original
    assumed clock. Production supplies it; growth also waits for publication
    of the preceding observation used in its denominator. Modification time
    may reflect a rewrite, so it is not proof of an object's first publication.
    """
    snapshots = snapshots.copy()
    snapshots["observed_at"] = pd.to_datetime(snapshots.observed_at, utc=True)
    snapshots = snapshots.sort_values(["coin", "observed_at"])
    if snapshots.duplicated(["coin", "observed_at"]).any():
        raise ValueError("Duplicate market snapshot")
    grouped = snapshots.groupby("coin")
    gap = grouped.observed_at.diff()
    growth = np.tanh(np.log(snapshots.gross_units / grouped.gross_units.shift(1)))
    snapshots["position_growth"] = growth.where(
        gap.between(pd.Timedelta(hours=20), pd.Timedelta(hours=28))
    )
    snapshots["position_available_at"] = snapshots.observed_at + pd.Timedelta(days=1)
    if "source_modified_at" in snapshots:
        publication = pd.to_datetime(snapshots.source_modified_at, utc=True)
        if publication.isna().any() or (publication < snapshots.observed_at).any():
            raise ValueError("Invalid position snapshot publication time")
        snapshots["source_modified_at"] = publication
        previous_publication = snapshots.groupby("coin").source_modified_at.shift(1)
        snapshots["position_available_at"] = pd.concat(
            [snapshots.position_available_at, publication, previous_publication], axis=1
        ).max(axis=1)
    return snapshots.sort_values(["position_available_at", "coin"])


def join_positions(frame: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    """Latest observed state actually available, never rejuvenated by late upload."""
    state = position_state(snapshots)
    maximum_age = pd.Timedelta(hours=54)  # 24-hour minimum lag + 30-hour tolerance
    state = state[
        state.position_available_at <= state.observed_at + maximum_age
    ].sort_values(["position_available_at", "observed_at", "coin"])
    # A late older file cannot replace a newer observation already published.
    state = state[state.observed_at.eq(state.groupby("coin").observed_at.cummax())]
    columns = ["observed_at", "position_available_at", *POSITION_COLUMNS]
    result = pd.merge_asof(
        frame.sort_values(["query_time", "coin"]),
        state[["coin", *columns]],
        by="coin",
        left_on="query_time",
        right_on="position_available_at",
        direction="backward",
        tolerance=pd.Timedelta(hours=30),
    )
    # The source-age limit is measured from observation, not late publication.
    result[columns] = result[columns].where(
        result.query_time - result.observed_at <= maximum_age
    )
    return result


def partitions(
    frame: pd.DataFrame, as_of: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily, disjoint structure/estimation sets with fully observed labels."""
    clock = frame.query_time.dt
    past = frame[
        (frame.query_time >= as_of - HISTORY_LIMIT * INTERVAL)
        & (frame.query_time < as_of - POSITIONING_HORIZON_BARS * INTERVAL)
        & clock.hour.eq(0)
        & clock.minute.eq(15)
        & (frame.long_exit <= as_of)
        & (frame.short_exit <= as_of)
        & np.isfinite(frame.long_return)
        & np.isfinite(frame.short_return)
    ].sort_values(["query_time", "coin"])
    epoch_days = (
        past.query_time.dt.floor("D") - pd.Timestamp("1970-01-01T00:00Z")
    ).dt.days
    return past[epoch_days % 2 == 0], past[epoch_days % 2 == 1]


def sufficient_training_history(part: pd.DataFrame) -> bool:
    return (
        len(part) >= 500
        and part.query_time.dt.date.nunique() >= 21
        and part.coin.nunique() >= 10
    )


def day_weights(frame: pd.DataFrame) -> np.ndarray:
    days = frame.query_time.dt.floor("D")
    return 1.0 / days.map(days.value_counts()).to_numpy(dtype=float)


def normalized_payoffs(frame: pd.DataFrame) -> np.ndarray:
    """Clipping affects training/forecasts only, never realized results."""
    values = (
        frame[["long_return", "short_return"]].to_numpy()
        / frame.scale.to_numpy()[:, None]
    )
    return values.clip(-5, 5)


def analogue_weights(
    reference_leaves: np.ndarray,
    query_leaves: np.ndarray,
    base_weight: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Same-leaf matches, with equal contribution from each covered tree."""
    weights = np.zeros((len(query_leaves), len(reference_leaves)))
    coverage = np.zeros(len(query_leaves), dtype=int)
    for tree in range(reference_leaves.shape[1]):
        for leaf in np.unique(query_leaves[:, tree]):
            queries = np.flatnonzero(query_leaves[:, tree] == leaf)
            matches = np.flatnonzero(reference_leaves[:, tree] == leaf)
            if not len(matches):
                continue
            weights[np.ix_(queries, matches)] += (
                base_weight[matches] / base_weight[matches].sum()
            )
            coverage[queries] += 1
    weights /= np.maximum(coverage, 1)[:, None]
    return weights, coverage


def forecast(
    weights: np.ndarray, payoffs: np.ndarray, days: pd.Series
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Day-clustered uncertainty; many correlated coins are not many days."""
    # Group shuffled rows by day too: repeated days are not independent evidence.
    if not days.is_monotonic_increasing:
        order = np.argsort(days.to_numpy(), kind="stable")
        days, weights, payoffs = days.iloc[order], weights[:, order], payoffs[order]
    boundaries = np.r_[
        0, np.flatnonzero(days.to_numpy()[1:] != days.to_numpy()[:-1]) + 1
    ]
    daily_weight = np.add.reduceat(weights, boundaries, axis=1)
    squared_weight = np.square(daily_weight).sum(axis=1)
    effective_days = np.divide(
        1.0,
        squared_weight,
        out=np.zeros(len(weights)),
        where=squared_weight > 0,
    )
    mean = weights @ payoffs
    uncertainty = np.full_like(mean, np.inf)
    valid = effective_days > 1
    for side in range(payoffs.shape[1]):
        totals = np.add.reduceat(weights * payoffs[:, side], boundaries, axis=1)
        scores = totals - daily_weight * mean[:, side, None]
        uncertainty[valid, side] = np.sqrt(
            np.square(scores[valid]).sum(axis=1)
            * effective_days[valid]
            / (effective_days[valid] - 1)
        )
    return mean, uncertainty, effective_days


@dataclass(frozen=True)
class PositioningForecast:
    direction: int | None
    reason: str
    expected_net_return: float
    standard_error: float
    effective_days: float
    covered_trees: int
    analogue_weights: np.ndarray = field(repr=False, compare=False)


def score_positioning_analogues(
    estimation: pd.DataFrame,
    reference_leaves: np.ndarray,
    query_leaves: np.ndarray,
    scales: np.ndarray,
    *,
    as_of: pd.Timestamp,
) -> list[PositioningForecast]:
    """Forecast using the fitted model's estimation set, without query labels.

    Returned weights align with estimation rows and are the actual analogue
    distribution to use for the chart, not a separately selected nearest set.
    The caller owns daily model fitting and one-open-position-per-asset gating.
    """
    if (
        not sufficient_training_history(estimation)
        or reference_leaves.shape != (len(estimation), TREE_PARAMETERS["iterations"])
        or query_leaves.shape != (len(scales), TREE_PARAMETERS["iterations"])
        or scales.ndim != 1
        or not np.isfinite(scales).all()
        or not (scales > 0).all()
        or not np.isfinite(estimation[["long_return", "short_return", "scale"]])
        .all()
        .all()
        or not estimation.scale.gt(0).all()
        or not estimation[["long_exit", "short_exit"]].le(as_of).all().all()
    ):
        raise ValueError("Invalid or incomplete positioning analogue inputs")
    weights, coverage = analogue_weights(
        reference_leaves, query_leaves, day_weights(estimation)
    )
    mean, uncertainty, effective = forecast(
        weights, normalized_payoffs(estimation), estimation.query_time.dt.floor("D")
    )
    mean *= scales[:, None]
    uncertainty *= scales[:, None]
    results = []
    for i in range(len(scales)):
        side = int(np.argmax(mean[i] - POSITIONING_COST - uncertainty[i]))
        net = float(mean[i, side] - POSITIONING_COST)
        error = float(uncertainty[i, side])
        if coverage[i] < 48 or effective[i] < 20:
            reason = "insufficient_analogue_support"
        elif net <= error + 0.0005:
            reason = "insufficient_net_edge"
        else:
            reason = "high_confidence_candidate"
        results.append(
            PositioningForecast(
                direction=(1 if side == 0 else -1)
                if reason == "high_confidence_candidate"
                else None,
                reason=reason,
                expected_net_return=net,
                standard_error=error,
                effective_days=float(effective[i]),
                covered_trees=int(coverage[i]),
                analogue_weights=weights[i],
            )
        )
    return results
