from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from wayfinder_paths.jobs.store import JobStore

DERIVED_SERIES_KINDS = {
    "spread",
    "ratio",
    "z_score",
    "zscore",
    "indicator",
    "signal",
    "basket",
}
PERFORMANCE_SERIES_KINDS = {
    "equity_curve",
    "drawdown_curve",
    "realized_pnl",
    "unrealized_pnl",
    "pnl",
}
VIEW_KINDS = {
    "legs": {"market_price"},
    "spread": DERIVED_SERIES_KINDS,
    "equity": {"equity_curve"},
    "drawdown": {"drawdown_curve"},
    "performance": PERFORMANCE_SERIES_KINDS,
}


def summarize_backtest_artifacts(
    job_id: str, *, store: JobStore | None = None, proposal_id: str | None = None
) -> dict[str, Any]:
    store = store or JobStore()
    prefix = (
        f"applications/{proposal_id}/candidate/results/backtest"
        if proposal_id
        else "results/backtest"
    )
    visualization = store.read_json(job_id, f"{prefix}/visualization.json")
    latest = store.read_json(job_id, f"{prefix}/latest.json", default={}) or {}
    if not visualization:
        return {"available": False}

    viz_path = store.job_dir(job_id) / "results" / "backtest" / "visualization.json"
    marker_counts = Counter(marker["kind"] for marker in visualization["markers"])
    return {
        "available": True,
        "run_id": latest["run_id"] if latest else None,
        "updated_at": (
            datetime.fromtimestamp(viz_path.stat().st_mtime).astimezone().isoformat()
            if viz_path.exists()
            else None
        ),
        "stats": latest["stats"] if latest else {},
        "symbols": visualization["symbols"],
        "series": [
            {
                "name": series["name"],
                "kind": series["kind"],
                "symbol": series.get("symbol"),
                "point_count": len(series["points"]),
            }
            for series in visualization["series"]
        ],
        "marker_counts": dict(marker_counts),
        "marker_count": sum(marker_counts.values()),
        "validation": visualization["validation"]
        or (latest["validation"] if latest else {}),
    }


def order_series_for_display(
    series: list[dict[str, Any]], markers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Marker-bearing price series first, then equity curves, then the rest.

    The jobs UI renders only the first couple of series — with a multi-symbol
    data contract the traded symbol's chart (the one with entry/exit markers)
    must sort ahead of untraded siblings or the markers are never visible.
    Stable within ranks, so single-symbol payloads are unchanged.
    """
    marked = {
        str(marker["symbol"]) for marker in markers if marker.get("symbol") is not None
    }

    def rank(entry: dict[str, Any]) -> int:
        if entry.get("symbol") is not None and str(entry["symbol"]) in marked:
            return 0
        if entry.get("kind") == "equity_curve":
            return 1
        return 2

    return sorted(series, key=rank)


def load_backtest_view(
    job_id: str,
    *,
    store: JobStore | None = None,
    view: str = "all",
    series_names: list[str] | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    max_points: int = 1500,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    # Proposal-scoped view (contract C2): read the CANDIDATE run written by
    # candidate validation so the FE can overlay it against the active run.
    prefix = (
        f"applications/{proposal_id}/candidate/results/backtest"
        if proposal_id
        else "results/backtest"
    )
    visualization = store.read_json(job_id, f"{prefix}/visualization.json")
    latest = store.read_json(job_id, f"{prefix}/latest.json", default={}) or {}
    if not visualization:
        return {"available": False}

    requested = {item.strip() for item in series_names or [] if item.strip()}
    bounded_max = min(max(max_points, 100), 10_000)
    start = _parse_ts(from_ts)
    end = _parse_ts(to_ts)
    kinds = VIEW_KINDS.get(view)
    selected_series = []
    for series in visualization["series"]:
        if requested and series["name"] not in requested:
            continue
        if kinds is not None and series["kind"] not in kinds:
            continue
        points = [
            point
            for point in series["points"]
            if _in_range(_parse_ts(point["timestamp"]), start, end)
        ]
        if len(points) > bounded_max:
            # Even-stride downsample keeping first and last points;
            # bounded_max >= 100, so no degenerate two-point case.
            last_index = len(points) - 1
            points = [
                points[math.floor(index * last_index / (bounded_max - 1))]
                for index in range(bounded_max)
            ]
        selected_series.append({**series, "points": points})
    symbols = {
        str(series["symbol"])
        for series in selected_series
        if series.get("symbol") is not None
    }
    markers = [
        marker
        for marker in visualization["markers"]
        if _in_range(_parse_ts(marker["timestamp"]), start, end)
        and (view != "legs" or not symbols or str(marker["symbol"]) in symbols)
    ]
    return {
        "available": True,
        "view": view,
        "run_id": latest["run_id"] if latest else None,
        "summary": summarize_backtest_artifacts(
            job_id, store=store, proposal_id=proposal_id
        ),
        "visualization": {
            key: value
            for key, value in visualization.items()
            if key not in {"series", "markers"}
        }
        | {
            "series": order_series_for_display(selected_series, markers),
            "markers": markers,
        },
        "trades": latest["trades"] if latest else [],
    }


def diagnose_backtest(
    job_id: str,
    *,
    store: JobStore | None = None,
    proposal_id: str | None = None,
    top_trades: int = 5,
) -> dict[str, Any]:
    """Framework-computed breakdown of the latest backtest — win rate, PnL, and
    counts bucketed by exit reason, close hour (UTC), and side, plus the best/
    worst trades. The headline is taken VERBATIM from the run's `stats`, and the
    buckets come from the same `realized_pnl_delta` the engine recorded on each
    closing fill — so this never disagrees with `job backtest`. Read this to find
    a strategy's strong/weak spots instead of recomputing PnL by hand (ad-hoc
    recomputation is what drifts from the framework's numbers)."""
    store = store or JobStore()
    prefix = (
        f"applications/{proposal_id}/candidate/results/backtest"
        if proposal_id
        else "results/backtest"
    )
    latest = store.read_json(job_id, f"{prefix}/latest.json", default={}) or {}
    if not latest:
        return {"available": False}
    stats = latest.get("stats") or {}
    trades = latest.get("trades") or []
    # Completed trades = closing fills carrying realized PnL (same source as stats).
    closes = [t for t in trades if abs(float(t.get("realized_pnl_delta") or 0.0)) > 0]

    def _reason(trade: dict[str, Any]) -> str:
        raw = trade.get("raw") or {}
        meta = raw.get("metadata") or raw
        return str(
            meta.get("exit_reason") or ("close" if trade.get("reduce_only") else "open")
        )

    def _hour(trade: dict[str, Any]) -> int:
        ts = _parse_ts(trade.get("timestamp"))
        return ts.hour if ts else -1

    def _same_bar_close_fraction() -> float | None:
        # Fraction of closes that fill on the SAME timestamp as the position's
        # most recent opening fill — the signature of same-bar bracket fills,
        # which produce fantasy win rates the live engine cannot reproduce.
        opens: dict[str, str | None] = {}
        paired = 0
        same = 0
        for trade in trades:
            symbol = str(trade.get("symbol") or "?")
            if abs(float(trade.get("realized_pnl_delta") or 0.0)) > 0:
                open_ts = opens.get(symbol)
                if open_ts is not None:
                    paired += 1
                    same += 1 if trade.get("timestamp") == open_ts else 0
            else:
                opens[symbol] = trade.get("timestamp")
        return same / paired if paired else None

    def _bucket(key_fn: Any) -> dict[Any, dict[str, Any]]:
        agg: dict[Any, dict[str, float]] = {}
        for trade in closes:
            key = key_fn(trade)
            row = agg.setdefault(key, {"trades": 0.0, "pnl": 0.0, "wins": 0.0})
            pnl = float(trade.get("realized_pnl_delta") or 0.0)
            row["trades"] += 1
            row["pnl"] += pnl
            row["wins"] += 1 if pnl > 0 else 0
        return {
            key: {
                "trades": int(row["trades"]),
                "pnl": round(row["pnl"], 4),
                "win_rate": round(row["wins"] / row["trades"], 3)
                if row["trades"]
                else 0.0,
                "avg_pnl": round(row["pnl"] / row["trades"], 4)
                if row["trades"]
                else 0.0,
            }
            for key, row in agg.items()
        }

    def _trade_view(trade: dict[str, Any]) -> dict[str, Any]:
        return {
            "timestamp": trade.get("timestamp"),
            "side": trade.get("side"),
            "pnl": round(float(trade.get("realized_pnl_delta") or 0.0), 4),
            "reason": _reason(trade),
        }

    ranked = sorted(closes, key=lambda t: float(t.get("realized_pnl_delta") or 0.0))
    headline_keys = (
        "net_return",
        "trade_count",
        "win_rate",
        "profit_factor",
        "avg_trade_pnl",
        "sharpe",
        "sortino",
        "max_drawdown_pct",
        "best_trade_pnl",
        "worst_trade_pnl",
    )
    by_exit_reason = _bucket(_reason)
    by_close_hour = dict(sorted(_bucket(_hour).items()))
    by_side = _bucket(lambda t: t.get("side") or "?")
    by_symbol = _bucket(lambda t: str(t.get("symbol") or "?"))
    recommendations = _recommendations(
        stats,
        by_exit_reason,
        by_side,
        by_close_hour,
        by_symbol,
        len(closes),
        same_bar_close_fraction=_same_bar_close_fraction(),
    )
    return {
        "available": True,
        "run_id": latest.get("run_id"),
        "headline": {k: stats[k] for k in headline_keys if k in stats},
        "closed_trades_analyzed": len(closes),
        "how_to_use": (
            "Recommendations are hypotheses ranked by evidence, not edicts — "
            "verify any change on the full dataset AND out-of-sample "
            "(walk-forward) before keeping it. Never remove a risk control "
            "(stop / time-stop) to lift a backtest; retune it via a grid + "
            "walk-forward instead."
        ),
        # The single most important thing to do next — read this first.
        "next_step": recommendations[0]["suggest"] if recommendations else None,
        "recommendations": recommendations,
        "by_exit_reason": by_exit_reason,
        "by_close_hour_utc": by_close_hour,
        "by_side": by_side,
        "by_symbol": by_symbol,
        "worst_trades": [_trade_view(t) for t in ranked[:top_trades]],
        "best_trades": [_trade_view(t) for t in reversed(ranked[-top_trades:])],
    }


# Severity rank → sort order for recommendations (blocking first).
_REC_RANK = {"blocking": 0, "high": 1, "medium": 2, "low": 3, "validate": 4}


def _recommendations(
    stats: dict[str, Any],
    by_reason: dict[Any, dict[str, Any]],
    by_side: dict[Any, dict[str, Any]],
    by_hour: dict[Any, dict[str, Any]],
    by_symbol: dict[Any, dict[str, Any]],
    n_closes: int,
    *,
    same_bar_close_fraction: float | None = None,
) -> list[dict[str, Any]]:
    """Turn the run's own stats + trade buckets into ranked, evidence-backed
    next actions — so a bad backtest yields concrete things to try instead of a
    hand-rolled recompute. Every recommendation quotes the numbers that triggered
    it (never invented); the top one is echoed as `next_step`. This is what makes
    the diagnose→improve loop good: read `recommendations`, change ONE thing, and
    re-run `--quick` rather than thrashing across many full backtests.

    INVARIANT (risk-control preservation): no recommendation text may suggest
    REMOVING a risk control — stop, time-stop, trailing stop, liquidation
    buffer. Suggestions may retune (widen/tighten via grid + walk-forward),
    never delete. A string-level test in test_backtest_profile_and_summary.py
    enforces this across crafted payloads.

    Bucket-driven rules (exit-reason / side / hour / symbol concentration) only
    fire when the bucket has n >= 8 trades AND >= 25% of all closes — below
    that a bucket is a hypothesis, not evidence (n=12 bucket inferences drove a
    live exit-rule change that didn't survive validation)."""

    def gated(bucket_n: int) -> bool:
        return bucket_n >= 8 and bucket_n >= 0.25 * n_closes

    def pct(value: Any) -> str:
        return (
            f"{float(value) * 100:.1f}%" if isinstance(value, (int, float)) else "n/a"
        )

    def num(value: Any, places: int = 2) -> str:
        return (
            f"{float(value):.{places}f}" if isinstance(value, (int, float)) else "n/a"
        )

    net = stats.get("net_return")
    sharpe = stats.get("sharpe")
    pf = stats.get("profit_factor")
    wr = stats.get("win_rate")
    tc = int(stats.get("trade_count") or 0)
    avg = stats.get("avg_trade_pnl")
    fees = float(stats.get("total_fees") or 0.0)
    bh = stats.get("buy_hold_return")
    liq = int(stats.get("liquidation_count") or 0)
    recs: list[dict[str, Any]] = []

    def add(severity: str, issue: str, evidence: str, suggest: str) -> None:
        recs.append(
            {
                "severity": severity,
                "issue": issue,
                "evidence": evidence,
                "suggest": suggest,
            }
        )

    # Blocking: can't conclude anything from too few trades — don't tune on noise.
    if tc < 10:
        add(
            "blocking",
            "Too few trades to judge or tune",
            f"trade_count={tc}",
            "Loosen the entry condition (or fetch a longer dataset) so there are "
            "≥20 trades. Tuning params on this few is fitting to noise."
            if tc > 0
            else "Entry never triggered — check the symbol, the threshold, and that "
            "the frame has enough bars before `decide()` returns an intent.",
        )
    # Blocking: forced liquidation means the sizing/stop is wrong before anything else.
    if liq > 0:
        add(
            "blocking",
            "Position was liquidated",
            f"liquidation_count={liq}",
            "Reduce leverage and/or widen the stop — the position is force-closed "
            "before the thesis can play out. Fix this before tuning entries.",
        )
    # High: results too good to be real are usually simulation artifacts —
    # same-bar bracket fills, look-ahead in precompute, or fantasy fill prices.
    # This codifies what a live session had to catch by hand.
    same_bar_hot = (
        same_bar_close_fraction is not None and same_bar_close_fraction >= 0.5
    )
    if tc >= 10 and ((wr is not None and wr > 0.90) or same_bar_hot):
        evidence = f"win_rate={pct(wr)} over {tc} trades"
        if same_bar_close_fraction is not None:
            evidence += (
                f"; {same_bar_close_fraction:.0%} of closes fill on the entry bar"
            )
        add(
            "high",
            "Result looks like an execution artifact, not edge",
            evidence,
            "Win rates this high (or entries that close on their own bar) are "
            "usually a simulation artifact — same-bar bracket fills, look-ahead "
            "in precompute, or unrealistic fill prices. Check same_bar_policy / "
            "ohlc_rules in the execution spec and validate before believing any "
            "of these numbers.",
        )
    multi_symbol = len([k for k in by_symbol if str(k) != "?"]) >= 2
    # High: negative base — params rarely rescue a signal with no edge. For a
    # multi-symbol (pair/basket) strategy the first question is statistical:
    # correlated is not cointegrated, and no parameter rescues a spread that
    # does not mean-revert — so pair-check outranks generic re-thinking.
    if tc >= 10 and net is not None and net <= 0:
        if multi_symbol:
            add(
                "high",
                "Losing multi-symbol strategy — test the relationship first",
                f"net_return={pct(net)}, profit_factor={num(pf)}, "
                f"symbols={sorted(str(k) for k in by_symbol)}",
                "Run `job pair-check <id> --symbols A,B` before tuning anything: "
                "correlated is not cointegrated, and no parameter rescues a "
                "spread that does not mean-revert. If the gate REJECTs, change "
                "the pair or the idea (funding-spread pair, momentum, carry) — "
                "not the thresholds.",
            )
        else:
            add(
                "high",
                "No edge on this data (net loss)",
                f"net_return={pct(net)}, sharpe={num(sharpe)}, profit_factor={num(pf)}",
                "Test the SIGNAL before touching parameters: run `job "
                "signal-check <id> --column <entry_column>` — if no horizon "
                "beats the series' own drift (t >= 2, n >= 30), the entry has "
                "no predictive power and no exit/stop/sizing change will save "
                "it; change the idea or add a regime filter instead.",
            )
    # High: wins often but the losers are bigger — a payoff problem, not an entry one.
    if wr is not None and wr > 0.55 and pf is not None and pf < 1.1:
        add(
            "high",
            "Winners smaller than losers (poor payoff)",
            f"win_rate={pct(wr)}, profit_factor={num(pf)}",
            "Tighten the stop or add a take-profit / trailing exit — you win often "
            "but give it back on the losses. Do NOT loosen entry.",
        )
    # Medium: most of the P&L is just the market trend, not alpha.
    if (
        bh is not None
        and abs(float(bh)) > 0.20
        and (net is None or abs(float(net)) <= abs(float(bh)))
    ):
        add(
            "medium",
            "Result may be mostly market trend, not edge",
            f"buy_hold_return={pct(bh)} vs net_return={pct(net)}",
            "The underlying moved a lot over this window, so a large share of any "
            "P&L is beta. Re-test on a flat or opposite-regime slice before trusting it.",
        )
    # Medium: trading costs eat a big share of gross — cut trade count.
    gross = abs(float(avg) * tc) if isinstance(avg, (int, float)) and tc else 0.0
    if fees > 0 and gross > 0 and fees >= 0.3 * gross:
        add(
            "medium",
            "Trading costs eat a large share of P&L",
            f"total_fees={num(fees)} vs gross≈{num(gross)} over {tc} trades",
            "Cut trade count with an entry filter or cooldown; confirm fee_bps / "
            "slippage_bps are set so the net number is realistic.",
        )
    # Medium: losses concentrate in one exit path (often stop-outs). Only worth
    # flagging when losses are an actual drag — a clean winner's losses are all
    # stops by construction and don't need a nag.
    losing = {k: v for k, v in by_reason.items() if v.get("pnl", 0) < 0}
    losses_hurt = net is None or net <= 0 or (pf is not None and pf < 1.5)
    if losing and losses_hurt:
        gross_loss = sum(abs(v["pnl"]) for v in losing.values())
        worst_key = min(losing, key=lambda k: losing[k]["pnl"])
        worst = losing[worst_key]
        if (
            gross_loss > 0
            and abs(worst["pnl"]) >= 0.5 * gross_loss
            and gated(worst["trades"])
        ):
            is_stop = "stop" in str(worst_key).lower()
            add(
                "medium",
                f"Losses concentrate in one exit: {worst_key}",
                f"{worst_key}: n={worst['trades']} of {n_closes} closes, "
                f"pnl={num(worst['pnl'])} "
                f"({worst['pnl'] / -gross_loss * -1:.0%} of gross loss)",
                "You're getting stopped out — widen the stop (via grid + "
                "walk-forward) or add an entry filter so you're not stopped on "
                "entry noise. Keep the stop — never trade without one."
                if is_stop
                else f"Revisit the {worst_key} exit rule — that's where the "
                "losses are. Retune it through the validation ladder, don't "
                "delete it.",
            )
    # Medium: one leg of a multi-symbol strategy carries the losses — a sizing
    # problem (1:1 notional is almost never neutral), not an entry problem.
    if multi_symbol and losses_hurt:
        losing_symbols = {k: v for k, v in by_symbol.items() if v.get("pnl", 0) < 0}
        if losing_symbols:
            sym_gross_loss = sum(abs(v["pnl"]) for v in losing_symbols.values())
            worst_sym = min(losing_symbols, key=lambda k: losing_symbols[k]["pnl"])
            leg = losing_symbols[worst_sym]
            if (
                sym_gross_loss > 0
                and abs(leg["pnl"]) >= 0.8 * sym_gross_loss
                and gated(leg["trades"])
            ):
                add(
                    "medium",
                    f"One leg carries the losses: {worst_sym}",
                    f"{worst_sym}: n={leg['trades']} of {n_closes} closes, "
                    f"pnl={num(leg['pnl'])} "
                    f"({abs(leg['pnl']) / sym_gross_loss:.0%} of symbol gross loss)",
                    "Size the legs by the regression hedge ratio, not 1:1 — "
                    "`job pair-check` reports `suggested.hedge_ratio`. A 1:1 "
                    "notional pair carries directional exposure on the more "
                    "volatile leg.",
                )
    # Low: the edge is one-directional.
    losing_sides = {k: v for k, v in by_side.items() if v.get("pnl", 0) < 0}
    winning_sides = {k: v for k, v in by_side.items() if v.get("pnl", 0) > 0}
    if losing_sides and winning_sides:
        side_key = min(losing_sides, key=lambda k: losing_sides[k]["pnl"])
        if gated(losing_sides[side_key]["trades"]):
            add(
                "low",
                f"One direction loses money: {side_key}",
                f"{side_key}: n={losing_sides[side_key]['trades']} of "
                f"{n_closes} closes, pnl={num(losing_sides[side_key]['pnl'])}",
                f"The edge is one-directional — consider trading only the "
                f"profitable side or filtering {side_key} entries. Verify on the "
                f"full dataset first; a one-sided split can be a regime artifact.",
            )
    # Low: P&L concentrates in a few hours — a session filter may help.
    net_hour = sum(v.get("pnl", 0) for v in by_hour.values())
    if net_hour > 0 and len(by_hour) >= 6:
        hot_rows = sorted(
            by_hour.items(), key=lambda kv: kv[1].get("pnl", 0), reverse=True
        )[:3]
        top3_pnl = sum(v.get("pnl", 0) for _, v in hot_rows)
        top3_n = sum(int(v.get("trades", 0)) for _, v in hot_rows)
        if top3_pnl >= 0.7 * net_hour and gated(top3_n):
            hot = [str(k) for k, _ in hot_rows]
            add(
                "low",
                "P&L concentrates in a few hours (UTC)",
                f"hours {', '.join(hot)} hold ≥70% of net P&L "
                f"(n={top3_n} of {n_closes} closes)",
                "A session / time-of-day entry filter may cut the flat or negative "
                "hours and lift risk-adjusted return. Treat as a hypothesis — "
                "verify on the full dataset and out-of-sample before keeping it.",
            )
    # Validate: promising in-sample → the ONLY next step is out-of-sample proof.
    if (
        net is not None
        and net > 0
        and sharpe is not None
        and sharpe > 0.5
        and pf is not None
        and pf > 1.2
        and tc >= 20
    ):
        add(
            "validate",
            "In-sample looks promising — validate out-of-sample",
            f"net_return={pct(net)}, sharpe={num(sharpe)}, profit_factor={num(pf)}, "
            f"{tc} trades",
            "Prove it out-of-sample before trusting it: `job experiments <id> --grid "
            "grid.json --wf-test-bars N --wf-folds K`. Keep it only if decay_ratio "
            "stays near 1 and most folds are positive — then offer to deploy.",
        )

    if not recs:
        add(
            "low",
            "Result is inconclusive / mediocre",
            f"net_return={pct(net)}, sharpe={num(sharpe)}, {tc} trades",
            "Change ONE structural thing (an entry filter or an exit rule), re-run "
            "`backtest --quick`, and compare — or validate out-of-sample if you "
            "believe the edge is real.",
        )

    recs.sort(key=lambda r: _REC_RANK.get(r["severity"], 9))
    return recs[:4]


def _in_range(
    current: datetime | None, start: datetime | None, end: datetime | None
) -> bool:
    if current is None:
        return True
    if start and current < start:
        return False
    return not (end and current > end)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # from_ts/to_ts arrive as free-form CLI/agent input; a bad bound
        # disables filtering rather than failing the whole view.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
