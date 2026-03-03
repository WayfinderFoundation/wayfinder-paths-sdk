from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    lookback_days: int
    limit: int
    notional_usd: float
    funding_threshold_bps: float
    momentum_days: int
    out: str


def _to_float(value: object) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except Exception:
        return float("nan")
    return num if math.isfinite(num) else float("nan")


async def _load_points(
    *, symbol: str, lookback_days: int, limit: int
) -> list[dict[str, Any]]:
    try:
        from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing wayfinder_paths DeltaLabClient (install wayfinder-paths-sdk)"
        ) from exc

    client = DeltaLabClient()
    series = await client.get_asset_timeseries(
        symbol=symbol,
        lookback_days=lookback_days,
        limit=limit,
        series="price,funding",
    )

    price_df = series.get("price")
    funding_df = series.get("funding")
    if price_df is None or price_df.empty:
        raise RuntimeError("Delta Lab response missing required series: price")

    price_reset = price_df.reset_index()
    prices: dict[str, float] = {}
    for _, row in price_reset.iterrows():
        ts = row.get("ts")
        if ts is None:
            continue
        ts_iso = (
            ts.to_pydatetime().replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        )
        prices[ts_iso] = _to_float(row.get("price_usd"))

    # Funding can have multiple markets per ts; pick the row with max oi_usd when present.
    best_funding: dict[str, float] = {}
    if funding_df is not None and not funding_df.empty:
        funding_reset = funding_df.reset_index()
        if "oi_usd" in funding_reset.columns:
            oi = funding_reset["oi_usd"].fillna(0.0)
            best_rows = funding_reset.loc[oi.groupby(funding_reset["ts"]).idxmax()]
        else:
            best_rows = funding_reset.drop_duplicates(subset=["ts"], keep="first")
        for _, row in best_rows.iterrows():
            ts = row.get("ts")
            if ts is None:
                continue
            ts_iso = (
                ts.to_pydatetime()
                .replace(tzinfo=UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            best_funding[ts_iso] = _to_float(row.get("funding_rate"))

    points: list[dict[str, Any]] = []
    for ts_iso, price_usd in prices.items():
        points.append(
            {
                "ts": ts_iso,
                "price_usd": price_usd,
                "funding_rate": best_funding.get(ts_iso),
            }
        )

    points.sort(key=lambda p: p["ts"])
    return points


def _compute_pnl_series(
    points: list[dict[str, Any]],
    *,
    notional_usd: float,
    funding_threshold_bps: float,
    momentum_days: int,
) -> dict[str, list[float]]:
    if len(points) < 2:
        raise RuntimeError("Need at least 2 points")

    thr = float(funding_threshold_bps) / 10000.0
    mom_days = max(1, int(momentum_days))

    long_pnl = [0.0]
    short_pnl = [0.0]
    carry_pnl = [0.0]
    carry_filtered_pnl = [0.0]

    for i in range(1, len(points)):
        prev = points[i - 1]
        cur = points[i]

        t0 = datetime.fromisoformat(str(prev["ts"]).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(cur["ts"]).replace("Z", "+00:00"))
        dt_years = (t1 - t0).total_seconds() / (365.0 * 24.0 * 3600.0)
        if dt_years <= 0:
            long_pnl.append(long_pnl[-1])
            short_pnl.append(short_pnl[-1])
            carry_pnl.append(carry_pnl[-1])
            carry_filtered_pnl.append(carry_filtered_pnl[-1])
            continue

        p0 = _to_float(prev.get("price_usd"))
        p1 = _to_float(cur.get("price_usd"))
        if not math.isfinite(p0) or not math.isfinite(p1) or p0 <= 0:
            long_pnl.append(long_pnl[-1])
            short_pnl.append(short_pnl[-1])
            carry_pnl.append(carry_pnl[-1])
            carry_filtered_pnl.append(carry_filtered_pnl[-1])
            continue

        ret = p1 / p0 - 1.0
        funding = _to_float(prev.get("funding_rate"))
        funding = funding if math.isfinite(funding) else 0.0

        # Always-long / always-short perps (USD notional), including funding.
        long_step = notional_usd * ret - funding * dt_years * notional_usd
        short_step = -notional_usd * ret + funding * dt_years * notional_usd
        long_pnl.append(long_pnl[-1] + long_step)
        short_pnl.append(short_pnl[-1] + short_step)

        # Funding carry: take the side that receives funding when above threshold.
        side = 0.0
        if funding > thr:
            side = -1.0
        elif funding < -thr:
            side = 1.0
        carry_step = (
            side * notional_usd * ret - side * funding * dt_years * notional_usd
        )
        carry_pnl.append(carry_pnl[-1] + carry_step)

        # Funding-vs-underlying: only take carry when the recent underlying trend is not against it.
        side_f = 0.0
        mom = 0.0
        if i - mom_days >= 0:
            p_m = _to_float(points[i - mom_days].get("price_usd"))
            if math.isfinite(p_m) and p_m > 0:
                mom = p0 / p_m - 1.0
        if funding > thr and mom <= 0:
            side_f = -1.0
        elif funding < -thr and mom >= 0:
            side_f = 1.0
        carry_filtered_step = (
            side_f * notional_usd * ret - side_f * funding * dt_years * notional_usd
        )
        carry_filtered_pnl.append(carry_filtered_pnl[-1] + carry_filtered_step)

    return {
        "alwaysLongPerp": long_pnl,
        "alwaysShortPerp": short_pnl,
        "fundingCarry": carry_pnl,
        "fundingCarryFiltered": carry_filtered_pnl,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest a simple long/short perp funding strategy (funding vs underlying trend)."
    )
    parser.add_argument("--symbol", default="ETH")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--notional-usd", type=float, default=100_000)
    parser.add_argument("--funding-threshold-bps", type=float, default=0.0)
    parser.add_argument("--momentum-days", type=int, default=7)
    parser.add_argument(
        "--out",
        default="",
        help="Optional output JSON path (prints to stdout when omitted).",
    )
    args = parser.parse_args()

    cfg = BacktestConfig(
        symbol=str(args.symbol).upper(),
        lookback_days=int(args.lookback_days),
        limit=int(args.limit),
        notional_usd=float(args.notional_usd),
        funding_threshold_bps=float(args.funding_threshold_bps),
        momentum_days=int(args.momentum_days),
        out=str(args.out),
    )

    points = asyncio.run(
        _load_points(
            symbol=cfg.symbol, lookback_days=cfg.lookback_days, limit=cfg.limit
        )
    )
    series = _compute_pnl_series(
        points,
        notional_usd=cfg.notional_usd,
        funding_threshold_bps=cfg.funding_threshold_bps,
        momentum_days=cfg.momentum_days,
    )

    payload = {
        "schemaVersion": "0.1",
        "source": "delta-lab",
        "asset": {"symbol": cfg.symbol},
        "notionalUsdDefault": cfg.notional_usd,
        "params": {
            "funding_threshold_bps": cfg.funding_threshold_bps,
            "momentum_days": cfg.momentum_days,
        },
        "points": points,
        "series": series,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    if cfg.out:
        with open(cfg.out, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print(
            json.dumps(
                {
                    "ok": True,
                    "out": cfg.out,
                    "final_pnl": {k: v[-1] for k, v in series.items()},
                },
                indent=2,
            )
        )
    else:
        print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
