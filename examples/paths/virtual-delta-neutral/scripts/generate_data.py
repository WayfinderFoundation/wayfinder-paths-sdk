"""Fetch and cache VIRTUAL delta-neutral data from Delta Lab API."""

import argparse
import json
from pathlib import Path

from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient


def fetch_data(lookback_days: int = 30) -> list[dict]:
    """Fetch VIRTUAL and USDC data from Delta Lab and merge into strategy dataset."""
    client = DeltaLabClient()

    # Fetch VIRTUAL timeseries (price + funding + lending)
    virtual_ts = client.get_asset_timeseries(
        symbol="VIRTUAL",
        series=["price", "funding", "lending"],
        lookback_days=lookback_days,
        limit=2000,
    )

    # Fetch USDC lending timeseries
    usdc_ts = client.get_asset_timeseries(
        symbol="USDC",
        series=["lending"],
        lookback_days=lookback_days,
        limit=2000,
    )

    # Build lookup maps
    price_map = {}
    for p in virtual_ts.get("price", []):
        ts = _normalize_ts(p["ts"])
        price_map[ts] = p["price_usd"]

    funding_map = {}
    for f in virtual_ts.get("funding", []):
        ts = _normalize_ts(f["ts"])
        existing = funding_map.get(ts)
        if not existing or (f.get("oi_usd", 0) > existing.get("oi_usd", 0)):
            funding_map[ts] = f

    virtual_lending_map = {}
    for lending_point in virtual_ts.get("lending", []):
        ts = _normalize_ts(lending_point["ts"])
        venue = (lending_point.get("venue") or "").lower()
        if "moonwell" in venue:
            virtual_lending_map[ts] = (
                lending_point.get("supply_apr")
                or lending_point.get("net_supply_apr")
                or 0
            )

    usdc_lending_map = {}
    for lending_point in usdc_ts.get("lending", []):
        ts = _normalize_ts(lending_point["ts"])
        venue = (lending_point.get("venue") or "").lower()
        if "moonwell" in venue:
            usdc_lending_map[ts] = (
                lending_point.get("supply_apr")
                or lending_point.get("net_supply_apr")
                or 0
            )

    # Merge on timestamps that have price data
    all_ts = sorted(price_map.keys())
    points = []
    for ts in all_ts:
        f = funding_map.get(ts)
        points.append(
            {
                "ts": ts,
                "price": price_map[ts],
                "funding_rate": f["funding_rate"] if f else 0,
                "virtual_supply_apr": virtual_lending_map.get(ts, 0),
                "usdc_supply_apr": usdc_lending_map.get(ts, 0),
            }
        )

    return points


def _normalize_ts(ts: str) -> str:
    return ts.replace("Z", "").split(".")[0][:13] + ":00:00"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback", type=int, default=30)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()

    data = fetch_data(args.lookback)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2))
        print(f"Wrote {len(data)} points to {out}")
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
