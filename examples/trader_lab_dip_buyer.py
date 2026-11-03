"""Trader Lab walkthrough: learn from the dip-buyer cohort, then test YOUR rule.

    poetry run python examples/trader_lab_dip_buyer.py

Costs a few thousand API credits (meta, persistence, copyability, one
query). It never trades. Steps follow .claude/skills/using-trader-lab/rules/workflow.md.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd

from wayfinder_paths.core.backtesting import BacktestConfig, fetch_prices, run_backtest
from wayfinder_paths.core.clients.TraderLabClient import TRADER_LAB_CLIENT as tl

COIN = "ETH"
HOLD_HOURS = 6


async def main() -> None:
    # 1. How much do past winners keep winning?
    meta = await tl.meta()
    print(f"release {meta['release']} (data through {meta['as_of']})")
    for row in meta["data"]["persistence"]:
        if row["bucket"] == "all" and row["horizon_months"] == 3:
            print(
                f"  vintage {row['as_of']} {row['selector']:>13}: "
                f"{row['pct_profitable']:.0%} still profitable after 3 months (n={row['n']})"
            )

    # 2-4. Where do dip buyers' long entries sit, and how do they turn out?
    by_z = await tl.query(
        sql=f"""
        SELECT round(e.z60 * 2) / 2 AS z60, count(*) AS n,
               avg((e.ret_bp > 0)::INT) AS win_rate, median(e.ret_bp) AS med_ret_bp
        FROM entry_states e JOIN universe u USING (address)
        WHERE u.bucket = 'dip_buyer' AND e.side = 1 AND e.coin = '{COIN}'
        GROUP BY 1 HAVING count(*) >= 30 ORDER BY 1
        """
    )
    print("\ndip-buyer longs on", COIN, "by 1h z-score at entry:")
    print(
        pd.DataFrame(
            by_z["data"]["rows"], columns=[c["name"] for c in by_z["data"]["columns"]]
        )
    )
    print("query cost:", by_z["usage"]["credits"], "credits")

    # 5. The user's own rule, inspired by (not copied from) the cohort:
    #    long when the last hour's move is <= -1.5 sigma; exit after HOLD_HOURS.
    #    z here is an hourly approximation of the release's z60 feature.
    end = datetime.now(UTC).date()
    start = end - timedelta(days=180)
    prices = await fetch_prices([COIN], str(start), str(end), interval="1h")
    ret = prices[COIN].pct_change()
    z = ret / ret.rolling(24 * 7).std()
    entries = (z <= -1.5).astype(float)
    target = entries.rolling(HOLD_HOURS, min_periods=1).max().to_frame(COIN)

    # 6. Backtest at ~11 bp round trip (5.5 bp per side: fee + slippage).
    result = run_backtest(
        prices,
        target,
        BacktestConfig(fee_rate=0.00035, slippage_rate=0.0002, periods_per_year=8760),
    )
    stats = result.stats
    print(
        f"\nyour rule on {COIN}, {start}..{end}: return {stats['total_return']:.1%}, "
        f"Sharpe {stats['sharpe']:.2f}, trades {stats['trade_count']}"
    )

    # 7. Would simply copying the cohort have done better?
    copy = await tl.copyability(bucket="dip_buyer")
    for row in copy["data"].get("dip_buyer", []):
        if row["delay"] == 5 and row["cost_bp"] == 11:
            print(
                f"copying dip buyers 5 min late at 11 bp: median {row['median_net_bp']:.1f} bp/trip, "
                f"{row['share_positive_oos'] or 0:.0%} of wallets positive out of sample"
            )


if __name__ == "__main__":
    asyncio.run(main())
