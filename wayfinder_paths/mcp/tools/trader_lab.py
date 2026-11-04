"""Trader Lab MCP tools: compact snapshots of what consistently profitable
Hyperliquid traders do (daily release, T+1).

MCP for snapshots, Python for bulk: these return at most 50 rows. For whole
datasets, local DuckDB and backtests use TRADER_LAB_CLIENT and
wayfinder_paths.core.trader_lab (load the using-trader-lab skill).
"""

from __future__ import annotations

import json
import math
from typing import Any

from wayfinder_paths.core.clients.trader_lab_types import BUCKETS, SORTS
from wayfinder_paths.core.clients.TraderLabClient import TRADER_LAB_CLIENT
from wayfinder_paths.mcp.arg_validation import (
    MCPArgumentError,
    normalize_enum,
    normalize_int,
)
from wayfinder_paths.mcp.utils import catch_errors, ok

MAX_ROWS = 50
COHORT_VIEWS = ("persistence", "copyability", "entry_states", "flows")
LEADERBOARD_FIELDS = (
    "address",
    "bucket",
    "sharpe",
    "net_mtm",
    "pos_weeks",
    "active_days",
    "oos_sharpe",
    "copy_net_bp",
    "top_coins",
)


def _blank(value: str) -> bool:
    return value is None or str(value).strip() in ("", "_")


def _round(value: Any) -> Any:
    """4 significant digits for floats, recursively: snapshots stay small."""
    if isinstance(value, float):
        return float(f"{value:.4g}") if math.isfinite(value) else None
    if isinstance(value, list):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def _release(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "release": payload.get("release"),
        "as_of": payload.get("as_of"),
        "rules_version": payload.get("rules_version"),
    }


def _bool(value: Any, field_name: str) -> bool:
    return (
        normalize_enum(
            str(value), field_name=field_name, allowed_values=("true", "false")
        )
        == "true"
    )


@catch_errors
async def research_trader_lab_meta() -> dict[str, Any]:
    """Trader Lab release info: coverage, archetype buckets, datasets, and the
    persistence headline (how often past top wallets stayed profitable out of
    sample). Check this FIRST: it tells you how much to trust any wallet."""
    payload = await TRADER_LAB_CLIENT.meta()
    data = payload["data"]
    return ok(
        {
            **_release(payload),
            "coverage": data.get("coverage"),
            "buckets": data.get("buckets"),
            "default_buckets": data.get("default_buckets"),
            "datasets": data.get("datasets"),
            "persistence": _round(data.get("persistence", [])[:MAX_ROWS]),
            "query_enabled": data.get("query_enabled"),
        }
    )


@catch_errors
async def research_trader_lab_leaderboard(
    bucket: str = "_",
    sort: str = "sharpe",
    include_hft: str = "false",
    coin: str = "_",
    min_days: str | int = "0",
    limit: str | int = "20",
) -> dict[str, Any]:
    """Screened Hyperliquid wallets (mark-to-market Sharpe, net P&L, up-week
    share, out-of-sample Sharpe, copy result at 5 min / 11 bp).

    Args:
        bucket: dip_buyer | momentum_swing | swing_other | scalper | mixed |
            hft_mm | unprofiled | all. "_" = every style except HFT/unprofiled.
        sort: sharpe | net_mtm | oos_sharpe | copy_net_bp | pos_weeks |
            active_days (descending; prefix "-" for ascending).
        include_hft: "true" to include HFT / market-maker wallets.
        coin: only wallets with this coin in their top coins. "_" = any.
        min_days: minimum active trading days.
        limit: rows to return (max 50).
    """
    kwargs: dict[str, Any] = {
        "include_hft": _bool(include_hft, "include_hft"),
        "limit": normalize_int(
            limit, field_name="limit", min_value=1, max_value=MAX_ROWS
        ),
    }
    if not _blank(bucket):
        kwargs["bucket"] = normalize_enum(
            bucket, field_name="bucket", allowed_values=(*BUCKETS, "all")
        )
    descending = not str(sort).strip().startswith("-")
    sort_field = normalize_enum(
        str(sort).strip().lstrip("-"), field_name="sort", allowed_values=SORTS
    )
    kwargs["sort"] = sort_field if descending else f"-{sort_field}"
    if not _blank(coin):
        kwargs["coin"] = str(coin).strip()
    days = normalize_int(min_days, field_name="min_days", min_value=0)
    if days:
        kwargs["min_days"] = days
    payload = await TRADER_LAB_CLIENT.leaderboard(**kwargs)
    rows = [{k: row.get(k) for k in LEADERBOARD_FIELDS} for row in payload["data"]]
    return ok(
        {
            **_release(payload),
            "rows": _round(rows),
            "next_cursor": (payload.get("page") or {}).get("next"),
        }
    )


@catch_errors
async def research_trader_lab_wallet(
    address: str, include_trips: str = "true"
) -> dict[str, Any]:
    """One wallet's profile: style stats (hold-time quantiles, win rate,
    payoff, long/maker share), entry hours, fill markouts, coin mix,
    copyability by delay/cost, and (optionally) its 20 most recent trips.

    Args:
        address: 0x wallet address.
        include_trips: "false" to skip recent trips.
    """
    card_payload = await TRADER_LAB_CLIENT.wallet(address)
    data = dict(card_payload["data"])
    card = dict(data.pop("card", None) or {})
    equity = card.pop("equity", None) or []
    out: dict[str, Any] = {
        **_release(card_payload),
        "wallet": _round(data),
        "card": _round(card),
        # The full curve is in the card via the Python client; MCP gets a summary.
        "equity_points": len(equity),
        "equity_last": _round(equity[-1]) if equity else None,
    }
    if _bool(include_trips, "include_trips"):
        trips = await TRADER_LAB_CLIENT.trips(address, limit=20)
        out["recent_trips"] = _round(trips["data"])
    return ok(out)


@catch_errors
async def research_trader_lab_cohort(
    view: str = "persistence",
    bucket: str = "_",
    coin: str = "_",
    days: str | int = "30",
) -> dict[str, Any]:
    """Cohort-level views.

    Args:
        view: persistence (did past winners keep winning, per vintage) |
            copyability (copying late at 5/11/20 bp, per bucket) |
            entry_states (entry-feature histograms vs baseline; needs bucket) |
            flows (daily net flow by bucket; needs coin).
        bucket: a bucket name, or "_" for all (persistence/copyability).
        coin: coin symbol for flows, e.g. "BTC".
        days: flow lookback in days (max 365).
    """
    view = normalize_enum(view, field_name="view", allowed_values=COHORT_VIEWS)
    b = (
        None
        if _blank(bucket)
        else normalize_enum(
            bucket, field_name="bucket", allowed_values=(*BUCKETS, "all")
        )
    )
    if view == "flows":
        if _blank(coin):
            raise MCPArgumentError(
                "coin is required for flows", field="coin", received=coin
            )
        payload = await TRADER_LAB_CLIENT.flows(
            coin=str(coin).strip(),
            bucket=b,
            days=normalize_int(days, field_name="days", min_value=1, max_value=365),
        )
        rows = payload["data"][-MAX_ROWS:]
        return ok({**_release(payload), "view": view, "rows": _round(rows)})
    if view == "entry_states" and b is None:
        raise MCPArgumentError(
            "bucket is required for entry_states", field="bucket", received=bucket
        )
    fetch = {
        "persistence": TRADER_LAB_CLIENT.persistence,
        "copyability": TRADER_LAB_CLIENT.copyability,
        "entry_states": TRADER_LAB_CLIENT.entry_states,
    }[view]
    payload = await fetch(bucket=b)
    data = {
        k: (v[:MAX_ROWS] if isinstance(v, list) else v)
        for k, v in payload["data"].items()
    }
    return ok({**_release(payload), "view": view, "data": _round(data)})


@catch_errors
async def research_trader_lab_query(
    sql: str = "_",
    recipe: str = "_",
    params_json: str = "{}",
    mode: str = "sync",
) -> dict[str, Any]:
    """Run read-only DuckDB SQL or a recipe over the release on the server
    (billed on CPU; sync <= 20 s). Prefer recipes; list them with
    TRADER_LAB_CLIENT.recipes(). Views: universe, cohort_membership,
    persistence, fills, round_trips, positions_hourly, markouts,
    entry_states, leaderboard, coin_flows, entry_state_hist, copyability,
    account_day, bars_1m, funding.

    Args:
        sql: one SELECT over the views ("_" when using a recipe).
        recipe: recipe name, e.g. "trips_with_features" ("_" when using sql).
        params_json: recipe parameters as a JSON object string.
        mode: "sync" (rows back, max 50 shown here) or "async" (returns a
            job id; poll with research_trader_lab_query_status, then
            download the Parquet with the Python client).
    """
    mode = normalize_enum(mode, field_name="mode", allowed_values=("sync", "async"))
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as exc:
        raise MCPArgumentError(
            "params_json must be a JSON object",
            field="params_json",
            received=params_json,
        ) from exc
    if not isinstance(params, dict):
        raise MCPArgumentError(
            "params_json must be a JSON object",
            field="params_json",
            received=params_json,
        )
    payload = await TRADER_LAB_CLIENT.query(
        sql=None if _blank(sql) else sql,
        recipe=None if _blank(recipe) else str(recipe).strip(),
        params=params or None,
        mode=mode,
    )
    data = payload["data"]
    if mode == "async":
        return ok({**_release(payload), "job": data, "usage": payload.get("usage")})
    rows = data.get("rows", [])
    return ok(
        {
            **_release(payload),
            "columns": [c["name"] for c in data.get("columns", [])],
            "rows": _round(rows[:MAX_ROWS]),
            "row_count": len(rows),
            "shown": min(len(rows), MAX_ROWS),
            "truncated": data.get("truncated", False) or len(rows) > MAX_ROWS,
            "usage": payload.get("usage"),
        }
    )


@catch_errors
async def research_trader_lab_query_status(job_id: str | int) -> dict[str, Any]:
    """Status and usage of an async Trader Lab query. When done, download
    the result with TRADER_LAB_CLIENT.download_query_result(job_id, path)."""
    payload = await TRADER_LAB_CLIENT.query_status(
        normalize_int(job_id, field_name="job_id", min_value=1)
    )
    return ok(payload)
