from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class BacktestParams:
    symbol: str
    lookback_days: int
    limit: int
    notional_usd: float
    rebalance_every_days: int
    min_tvl_usd: float
    min_days_to_maturity: int
    max_days_to_maturity: int


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_float(v: object) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except Exception:
        return float("nan")
    return f if math.isfinite(f) else float("nan")


def _score_row(row: dict[str, Any]) -> float:
    implied = _to_float(row.get("implied_apy"))
    reward = _to_float(row.get("reward_apr"))
    if not math.isfinite(implied) and not math.isfinite(reward):
        return float("-inf")
    return (implied if math.isfinite(implied) else 0.0) + (
        reward if math.isfinite(reward) else 0.0
    )


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    ts: datetime,
    min_tvl_usd: float,
    min_days_to_maturity: int,
    max_days_to_maturity: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        tvl = _to_float(row.get("tvl_usd"))
        if math.isfinite(tvl) and tvl < min_tvl_usd:
            continue

        maturity_ts = row.get("maturity_ts")
        if not isinstance(maturity_ts, str) or not maturity_ts.strip():
            continue
        try:
            maturity = _parse_iso(maturity_ts)
        except ValueError:
            continue

        days_to_mat = int((maturity - ts).total_seconds() // (24 * 3600))
        if days_to_mat < min_days_to_maturity or days_to_mat > max_days_to_maturity:
            continue

        price = _to_float(row.get("pt_price"))
        if not math.isfinite(price) or price <= 0:
            continue

        out.append(row)
    return out


def _group_by_ts(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ts = row.get("ts")
        if not isinstance(ts, str) or not ts.strip():
            continue
        grouped.setdefault(ts, []).append(row)
    return grouped


def _run_backtest(
    *,
    symbol: str,
    pendle_rows: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
    params: BacktestParams,
) -> dict[str, Any]:
    pendle_by_ts = _group_by_ts(pendle_rows)
    price_by_ts: dict[str, float] = {}
    for row in price_rows:
        ts = row.get("ts")
        if not isinstance(ts, str) or not ts.strip():
            continue
        price_by_ts[ts] = _to_float(row.get("price_usd"))

    ts_list = sorted(pendle_by_ts.keys())
    if len(ts_list) < 2:
        raise RuntimeError("Not enough pendle rows to backtest")

    nav = float(params.notional_usd)
    shares = 0.0
    current_symbol: str | None = None

    hold_symbol: str | None = None
    hold_shares = 0.0

    points: list[dict[str, Any]] = []
    rolls: list[dict[str, Any]] = []

    for i, ts_raw in enumerate(ts_list):
        ts = _parse_iso(ts_raw)
        all_rows = pendle_by_ts.get(ts_raw, [])
        filtered = _filter_rows(
            all_rows,
            ts=ts,
            min_tvl_usd=params.min_tvl_usd,
            min_days_to_maturity=params.min_days_to_maturity,
            max_days_to_maturity=params.max_days_to_maturity,
        )
        ranked = sorted(filtered, key=_score_row, reverse=True)

        decision_day = i == 0 or (
            params.rebalance_every_days > 0 and i % params.rebalance_every_days == 0
        )
        if decision_day and ranked:
            chosen = ranked[0]
            chosen_symbol = str(chosen.get("pt_symbol") or "").strip() or None
            if chosen_symbol and chosen_symbol != current_symbol:
                if current_symbol is not None:
                    rolls.append(
                        {
                            "ts": ts_raw,
                            "from": current_symbol,
                            "to": chosen_symbol,
                            "from_score": None,
                            "to_score": _score_row(chosen),
                        }
                    )
                current_symbol = chosen_symbol
                shares = nav / float(chosen["pt_price"])

            if hold_symbol is None and chosen_symbol:
                hold_symbol = chosen_symbol
                hold_shares = float(params.notional_usd) / float(chosen["pt_price"])

        # Mark NAV using the current PT's price at this timestamp.
        mark_row = None
        if current_symbol:
            for row in all_rows:
                if str(row.get("pt_symbol") or "") == current_symbol:
                    mark_row = row
                    break
        if mark_row is None and ranked:
            mark_row = ranked[0]
            current_symbol = str(mark_row.get("pt_symbol") or "") or current_symbol
            shares = nav / float(mark_row["pt_price"])

        if mark_row is not None:
            nav = shares * float(mark_row["pt_price"])

        hold_nav = None
        if hold_symbol:
            for row in all_rows:
                if str(row.get("pt_symbol") or "") == hold_symbol:
                    hold_nav = hold_shares * float(row["pt_price"])
                    break

        points.append(
            {
                "ts": ts_raw,
                "underlying_price_usd": price_by_ts.get(ts_raw),
                "selected_pt": current_symbol,
                "selected_pt_price": float(mark_row["pt_price"]) if mark_row else None,
                "selected_score": _score_row(mark_row) if mark_row else None,
                "nav_usd": nav,
                "hold_nav_usd": hold_nav,
                "top_candidates": [
                    {
                        "pt_symbol": str(r.get("pt_symbol") or ""),
                        "pt_price": _to_float(r.get("pt_price")),
                        "tvl_usd": _to_float(r.get("tvl_usd")),
                        "implied_apy": _to_float(r.get("implied_apy")),
                        "reward_apr": _to_float(r.get("reward_apr")),
                        "underlying_apy": _to_float(r.get("underlying_apy")),
                        "maturity_ts": r.get("maturity_ts"),
                        "score": _score_row(r),
                    }
                    for r in ranked[:8]
                ],
            }
        )

    start_nav = float(params.notional_usd)
    end_nav = float(points[-1]["nav_usd"])
    ret = (end_nav / start_nav - 1.0) if start_nav else 0.0

    return {
        "schemaVersion": "0.1",
        "source": "delta-lab" if params.lookback_days else "demo",
        "symbol": symbol,
        "notionalUsdDefault": params.notional_usd,
        "params": {
            "rebalance_every_days": params.rebalance_every_days,
            "min_tvl_usd": params.min_tvl_usd,
            "min_days_to_maturity": params.min_days_to_maturity,
            "max_days_to_maturity": params.max_days_to_maturity,
        },
        "summary": {
            "startNavUsd": round(start_nav, 2),
            "endNavUsd": round(end_nav, 2),
            "returnPct": round(ret * 100.0, 3),
            "rolls": len(rolls),
            "generatedAt": _iso(datetime.now(UTC)),
        },
        "rolls": rolls,
        "points": points,
    }


async def _load_delta_lab_series(
    *, symbol: str, lookback_days: int, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        from wayfinder_paths.core.clients.DeltaLabClient import DeltaLabClient
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing DeltaLabClient (install wayfinder-paths-sdk)"
        ) from exc

    client = DeltaLabClient()
    series = await client.get_asset_timeseries(
        symbol=symbol,
        lookback_days=lookback_days,
        limit=limit,
        series="pendle,price",
    )
    pendle_df = series.get("pendle")
    price_df = series.get("price")
    if pendle_df is None or pendle_df.empty:
        raise RuntimeError("Delta Lab response missing pendle series")

    pendle_rows: list[dict[str, Any]] = []
    pendle_reset = pendle_df.reset_index()
    for _, row in pendle_reset.iterrows():
        ts = row.get("ts")
        if ts is None:
            continue
        pendle_rows.append(
            {
                **row.to_dict(),
                "ts": ts.to_pydatetime()
                .replace(tzinfo=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )

    price_rows: list[dict[str, Any]] = []
    if price_df is not None and not price_df.empty:
        price_reset = price_df.reset_index()
        for _, row in price_reset.iterrows():
            ts = row.get("ts")
            if ts is None:
                continue
            price_rows.append(
                {
                    **row.to_dict(),
                    "ts": ts.to_pydatetime()
                    .replace(tzinfo=UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )

    return pendle_rows, price_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest a simple Pendle PT carry roller strategy."
    )
    parser.add_argument("--symbol", default="ETH")
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--notional-usd", type=float, default=100_000)
    parser.add_argument("--rebalance-every-days", type=int, default=7)
    parser.add_argument("--min-tvl-usd", type=float, default=250_000)
    parser.add_argument("--min-days-to-maturity", type=int, default=14)
    parser.add_argument("--max-days-to-maturity", type=int, default=365)
    parser.add_argument(
        "--out",
        default="",
        help="Optional output JSON path (prints to stdout when omitted).",
    )
    args = parser.parse_args()

    params = BacktestParams(
        symbol=str(args.symbol).upper().strip(),
        lookback_days=int(args.lookback_days),
        limit=int(args.limit),
        notional_usd=float(args.notional_usd),
        rebalance_every_days=max(1, int(args.rebalance_every_days)),
        min_tvl_usd=max(0.0, float(args.min_tvl_usd)),
        min_days_to_maturity=max(0, int(args.min_days_to_maturity)),
        max_days_to_maturity=max(1, int(args.max_days_to_maturity)),
    )

    pendle_rows, price_rows = asyncio.run(
        _load_delta_lab_series(
            symbol=params.symbol,
            lookback_days=params.lookback_days,
            limit=params.limit,
        )
    )
    result = _run_backtest(
        symbol=params.symbol,
        pendle_rows=pendle_rows,
        price_rows=price_rows,
        params=params,
    )

    if args.out:
        out_path = str(args.out)
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(
            json.dumps(
                {"ok": True, "out": out_path, "summary": result.get("summary")},
                indent=2,
            )
        )
    else:
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
