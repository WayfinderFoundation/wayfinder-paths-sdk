from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DemoConfig:
    symbol: str
    days: int
    notional_usd: float


def _build_demo_points(cfg: DemoConfig) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    start = now - timedelta(days=cfg.days)

    base_price = 2800.0
    market_id = 101
    chain_id = 8453
    venue = "hyperliquid"
    market_external_id = cfg.symbol

    points: list[dict[str, Any]] = []
    for i in range(cfg.days + 1):
        ts = start + timedelta(days=i)

        wobble = 220.0 * math.sin(i / 9.0) + 110.0 * math.sin(i / 3.7)
        price = max(250.0, base_price + wobble + 15.0 * math.sin(i / 1.9))

        fixed = 0.12 + 0.03 * math.sin(i / 11.0)  # 12% ± 3%
        floating = (
            fixed + 0.03 + 0.015 * math.sin(i / 7.3 + 0.85)
        )  # avg +3%, swings ±1.5%
        funding = 0.04 + 0.02 * math.sin(i / 8.5 + 1.3)  # 4% ± 2% (stylized)

        points.append(
            {
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "price_usd": round(price, 2),
                "fixed_rate_mark": round(fixed, 6),
                "floating_rate_oracle": round(floating, 6),
                "funding_rate": round(funding, 6),
                "pv": None,
                "market_id": market_id,
                "chain_id": chain_id,
                "venue": venue,
                "market_external_id": market_external_id,
            }
        )

    return points


async def _build_delta_lab_points(
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
        series="price,boros,funding",
    )
    price_df = series.get("price")
    boros_df = series.get("boros")
    funding_df = series.get("funding")
    if price_df is None or boros_df is None:
        raise RuntimeError(
            "Delta Lab response missing required series: price and boros"
        )

    joined = price_df.join(boros_df, how="inner")
    if joined.empty:
        raise RuntimeError("Delta Lab returned no overlapping price+boros points")

    if funding_df is None or funding_df.empty:
        joined["funding_rate"] = None
    else:
        funding_reset = funding_df.reset_index()
        if "oi_usd" in funding_reset.columns:
            oi = funding_reset["oi_usd"].fillna(0.0)
            best_rows = funding_reset.loc[oi.groupby(funding_reset["ts"]).idxmax()]
        else:
            best_rows = funding_reset.drop_duplicates(subset=["ts"], keep="first")
        best_rows = best_rows.set_index("ts")
        if "funding_rate" in best_rows.columns:
            joined = joined.join(best_rows[["funding_rate"]], how="left")
        else:
            joined["funding_rate"] = None

    points: list[dict[str, Any]] = []
    for ts, row in joined.iterrows():
        points.append(
            {
                "ts": ts.to_pydatetime()
                .replace(tzinfo=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "price_usd": float(row.get("price_usd")),
                "fixed_rate_mark": float(row.get("fixed_rate_mark"))
                if row.get("fixed_rate_mark") is not None
                else None,
                "floating_rate_oracle": float(row.get("floating_rate_oracle"))
                if row.get("floating_rate_oracle") is not None
                else None,
                "funding_rate": float(row.get("funding_rate"))
                if row.get("funding_rate") is not None
                else None,
                "pv": float(row.get("pv")) if row.get("pv") is not None else None,
                "market_id": int(row.get("market_id"))
                if row.get("market_id") is not None
                else 0,
                "chain_id": int(row.get("chain_id"))
                if row.get("chain_id") is not None
                else None,
                "venue": str(row.get("venue"))
                if row.get("venue") is not None
                else None,
                "market_external_id": str(row.get("market_external_id"))
                if row.get("market_external_id") is not None
                else None,
            }
        )

    points.sort(key=lambda p: p["ts"])
    return points


def _compute_summary(
    points: list[dict[str, Any]], *, notional_usd: float
) -> dict[str, Any]:
    pnl_long = 0.0  # pay fixed, receive floating
    pnl_short = 0.0  # receive fixed, pay floating

    for prev, cur in zip(points, points[1:], strict=False):
        t0 = datetime.fromisoformat(str(prev["ts"]).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(cur["ts"]).replace("Z", "+00:00"))
        dt_years = (t1 - t0).total_seconds() / (365.0 * 24.0 * 3600.0)

        fixed = cur.get("fixed_rate_mark")
        floating = cur.get("floating_rate_oracle")
        if fixed is None or floating is None:
            continue

        spread = float(floating) - float(fixed)
        pnl_long += notional_usd * spread * dt_years
        pnl_short += notional_usd * (-spread) * dt_years

    days = max(0, len(points) - 1)
    return {
        "days": days,
        "notionalUsd": notional_usd,
        "pnlLongUsd": round(pnl_long, 2),
        "pnlShortUsd": round(pnl_short, 2),
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Boros carry dataset for the demo applet."
    )
    parser.add_argument("--mode", choices=["demo", "delta-lab"], default="demo")
    parser.add_argument("--symbol", default="ETH")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--notional-usd", type=float, default=100_000)
    parser.add_argument(
        "--out",
        default=str(Path("applet/dist/data/boros_demo.json")),
        help="Output path for the applet data JSON.",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.mode == "delta-lab":
        points = asyncio.run(
            _build_delta_lab_points(
                symbol=str(args.symbol).upper(),
                lookback_days=int(args.lookback_days),
                limit=int(args.limit),
            )
        )
    else:
        points = _build_demo_points(
            DemoConfig(
                symbol=str(args.symbol).upper(),
                days=int(args.lookback_days),
                notional_usd=float(args.notional_usd),
            )
        )

    summary = _compute_summary(points, notional_usd=float(args.notional_usd))
    payload: dict[str, Any] = {
        "schemaVersion": "0.1",
        "source": args.mode,
        "asset": {"symbol": str(args.symbol).upper()},
        "notionalUsdDefault": float(args.notional_usd),
        "points": points,
        "summary": summary,
        "notes": [
            "Carry PnL uses simple notional*(floating-fixed)*dt (no fees, no MTM PV).",
            "For Boros mental model: LONG YU = pay fixed, receive floating; SHORT YU = receive fixed, pay floating.",
        ],
    }

    out_path.write_text(json.dumps(payload, indent=2))
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out_path),
                "points": len(points),
                "summary": summary,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
