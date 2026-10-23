"""One bounded public-data read for all proposed allocations; no wallet access."""

import asyncio
import time
from collections import OrderedDict
from copy import deepcopy
from math import isfinite
from typing import Annotated, Any

import httpx
from hyperliquid.utils.error import ClientError, ServerError
from pydantic import Field
from requests import RequestException

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.clients.HyperliquidDataClient import HYPERLIQUID_DATA_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.theses.models import Construction, Position, Variant
from wayfinder_paths.core.theses.quantification import DAY_MS, quantify_variants
from wayfinder_paths.core.theses.sizing import size_variant
from wayfinder_paths.mcp.polymarket_summary import (
    compact_market_candidate,
    compact_order_book,
)
from wayfinder_paths.mcp.utils import catch_errors, ok

# Public observations only. Bounded, memory-only; execution always needs fresh quotes.
_market_cache: OrderedDict[
    tuple[str, str, str, int, int], tuple[float, dict[str, Any]]
] = OrderedDict()


async def _read_market(position: Position, start: int, end: int) -> dict[str, Any]:
    instrument = position.instrument_id
    result: dict[str, Any] = {}
    if position.kind == "prediction":
        adapter = PolymarketAdapter()
        try:
            success, market = await adapter.get_market_by_token_id(token_id=instrument)
            if not success or not isinstance(market, dict):
                raise ValueError("Prediction market lookup unavailable")
            outcome = adapter.resolve_outcome_from_token_id(
                market=market, token_id=instrument
            )
            candidate = compact_market_candidate(market)
            if (
                not outcome
                or outcome.lower() != position.direction
                or not candidate["tradable"]
            ):
                raise ValueError("Prediction outcome mismatch or market not tradable")
            (history_ok, history), (book_ok, book) = await asyncio.gather(
                adapter.get_prices_history(
                    token_id=instrument,
                    interval=None,
                    start_ts=start // 1000,
                    end_ts=end // 1000,
                    fidelity=60,
                ),
                adapter.get_order_book(token_id=instrument),
            )
            result = {
                "source": "Polymarket outcome observations, last actual point per UTC day",
                "market": candidate,
                "outcome": outcome,
                "book": compact_order_book(book)
                if book_ok and isinstance(book, dict)
                else None,
                "prices": {},
            }
            if not history_ok or not isinstance(history, dict):
                result["history_error"] = (
                    "Prediction history unavailable; entry payoff is independent of history"
                )
            else:
                for row in sorted(
                    history.get("history", []), key=lambda r: int(r["t"])
                ):
                    stamp, value = int(row["t"]) * 1000, float(row["p"])
                    if start <= stamp < end and isfinite(value) and 0 <= value <= 1:
                        result["prices"][stamp // DAY_MS * DAY_MS] = value
            return result
        finally:
            await adapter.close()

    if position.kind == "token" and "/" not in instrument:
        token = await TOKEN_CLIENT.get_token_details(instrument)
        if token.get("identity", {}).get("suspicious"):
            raise ValueError("Resolved token has a conflicting identity")
        result.update(
            source="Wayfinder pinned-pool daily OHLC",
            resolved_token_id=token["token_id"],
            resolved_token={
                **{
                    key: token.get(key)
                    for key in (
                        "token_id",
                        "symbol",
                        "name",
                        "address",
                        "chain",
                        "identity",
                    )
                },
                "lookup_id": instrument,
            },
        )
        try:
            rows = await TOKEN_CLIENT.get_candles(
                token["token_id"],
                "1d",
                chain_id=token["chain"]["id"],
                start_ms=start,
                end_ms=end,
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            # Missing history does not undo a successful identity lookup.
            return {
                **result,
                "prices": {},
                "history_error": str(exc)[:200] or "Spot history timed out",
            }
    else:
        if position.kind == "token" and not instrument.endswith("/USDC"):
            raise ValueError("Spot USD diagnostics require a USDC-quoted pair")
        response = await HYPERLIQUID_DATA_CLIENT.get_candles_response(
            instrument, start, end, "1d"
        )
        rows = response.get("rows", [])
        result["source"] = response.get("source", "Hyperliquid daily OHLC")
        if position.kind in {"perp", "hip3"}:
            funding_start = end - 7 * DAY_MS
            try:
                async with asyncio.timeout(15):
                    funding_rows = await HYPERLIQUID_DATA_CLIENT.get_funding_history(
                        instrument, funding_start, end
                    )
                rates = {
                    int(r["time"]) // 3_600_000: float(r["fundingRate"])
                    for r in funding_rows
                    if funding_start <= int(r["time"]) < end
                }
                if not all(isfinite(r) for r in rates.values()):
                    raise ValueError("Non-finite funding rate")
                result["funding"] = {
                    "sum_rates": sum(rates.values()) if rates else None,
                    "observed_hours": len(rates),
                    "expected_hours": 168,
                    "start_ms": funding_start,
                    "end_ms": end,
                }
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                TimeoutError,
            ) as exc:
                # Price diagnostics remain useful when a separate provider read fails.
                result["funding"] = {"sum_rates": None, "error": str(exc)[:200]}
    prices = {}
    for row in rows:
        stamp, close_time = int(row["t"]), int(row["T"])
        value = float(row["c"])
        if start <= stamp and close_time <= end and isfinite(value) and value > 0:
            if stamp % DAY_MS or close_time - stamp not in {DAY_MS - 1, DAY_MS}:
                raise ValueError("Source did not return completed UTC daily candles")
            prices[stamp] = value
    return {**result, "prices": prices}


@catch_errors
async def research_quantify_portfolio(
    variants: Annotated[list[Variant], Field(min_length=1, max_length=4)],
    lookback_days: Annotated[int, Field(ge=14, le=90)] = 90,
    construction: Construction | None = None,
    alternatives: Annotated[list[Position], Field(max_length=12)] | None = None,
    compare_implementations: bool = False,
    counterfactuals: Annotated[list[Variant], Field(max_length=2)] | None = None,
) -> dict:
    """Measure draft portfolios before final sizing; reuse the returned metrics in review.

    Supply the frozen construction to size matched-relative dollar exposure in
    code before measuring it. Reuse the returned sized_variants exactly in draft
    checkpoints and update prose for changed weights. No holdings or leverage are
    added/removed; missing hedge or infeasible minimums require parent correction.
    Submit the proposal's variants (one per distinct allocation suffices). Resolves
    lookup IDs internally and shares price/funding reads across all budgets.
    Returns historical returns, volatility, drawdowns, aligned correlations,
    exposure/cash, observed funding and binary-outcome entry payoffs. Prices are
    fetched by trusted code, never supplied by the model. Missing/short history
    stays unavailable; it does not mean zero risk or an unsuitable investment.
    All return/risk numbers are fractions, not percentages. No forecasts or trades.
    counterfactuals: At most two complete alternative allocations against ONE proposed
        variant at the same budget. Reduce a weak holding/reallocate to an approved
        exposure, or substitute the closest credible challenger. The returned
        allocation_comparison uses identical observed timestamps for every mix;
        short history is explicitly limited, not zero risk. Selection stays with
        the agent: compare thesis fit, growth, risk and carry, not trailing Sharpe.
        Reuse a measured allocation exactly in the final draft. All mixes share
        the same bounded market reads; alternatives alone are asset diagnostics,
        not a whole-portfolio substitution comparison.
    alternatives: Hypothetical positions for closest competing implementations,
        using the SAME position schema. These are measured, never added to portfolios.
    compare_implementations: Include public depth/pool observations only for
        supplied holdings and alternatives. Does NOT discover alternative routes.
        Discover spot/perp/wrapper IDs with existing lookup tools and pass them as
        alternatives before rejecting an exposure for one route's funding or depth.
        A spot short is incompatible. Use per-budget notional, not volume as capacity.
        Read at most 12 distinct instruments including alternatives. Reuses public
        histories for five minutes; depth is refreshed on each comparison call.
    """
    # Direct Python callers need the same validation as MCP's generated schema.
    variants = [Variant.model_validate(v) for v in variants]
    counterfactuals = [Variant.model_validate(v) for v in counterfactuals or []]
    if counterfactuals and (
        len(variants) != 1
        or len(counterfactuals) > 2
        or any(v.budget_usd != variants[0].budget_usd for v in counterfactuals)
    ):
        raise ValueError(
            "Compare one proposed variant with at most two counterfactuals at the same budget"
        )
    alternatives = [Position.model_validate(p) for p in alternatives or []]
    if construction is not None:
        construction = Construction.model_validate(construction)
        variants = [size_variant(v, construction) for v in variants]
    if not 1 <= len(variants) <= 4 or not 14 <= lookback_days <= 90:
        raise ValueError("Use 1–4 variants and 14–90 days")
    if construction is not None:
        counterfactuals = [size_variant(v, construction) for v in counterfactuals]
    variants = [*variants, *counterfactuals]
    positions = {
        p.instrument_id: p
        for p in [*(p for v in variants for p in v.positions), *alternatives]
    }
    if len(positions) > 12:
        raise ValueError("Quantify at most 12 distinct finalist instruments at once")
    for group in [*(v.positions for v in variants), alternatives]:
        for position in group:
            other = positions[position.instrument_id]
            if position.kind != other.kind or (
                position.kind == "prediction" and position.direction != other.direction
            ):
                raise ValueError(
                    "An instrument cannot have conflicting kinds or prediction outcomes"
                )
    as_of = int(time.time() * 1000)
    end = as_of // DAY_MS * DAY_MS
    start = end - lookback_days * DAY_MS
    semaphore = asyncio.Semaphore(4)
    # One deadline for the whole batch, including queued finalists, below MCP's
    # 90-second request timeout. Completed markets survive a slow provider.
    deadline = asyncio.get_running_loop().time() + 60

    async def read(position: Position) -> tuple[str, dict]:
        async with semaphore:
            try:
                async with asyncio.timeout_at(deadline):
                    key = (
                        position.kind,
                        position.instrument_id,
                        position.direction,
                        start,
                        end,
                    )
                    cached = _market_cache.get(key)
                    if cached and time.monotonic() - cached[0] < 300:
                        result = deepcopy(cached[1])
                        _market_cache.move_to_end(key)
                    else:
                        result = await _read_market(position, start, end)
                        result["retrieved_at_ms"] = as_of
                        if (
                            result.get("prices")
                            and position.kind != "prediction"
                            and not result.get("history_error")
                            and not result.get("funding", {}).get("error")
                        ):
                            _market_cache[key] = (time.monotonic(), deepcopy(result))
                            while len(_market_cache) > 64:
                                _market_cache.popitem(last=False)
            except (
                httpx.HTTPError,
                RequestException,
                ClientError,
                ServerError,
                ValueError,
                KeyError,
                TypeError,
                TimeoutError,
            ) as exc:
                # Isolate provider/shape failures per finalist, never invent observations.
                result = {
                    "prices": {},
                    "error": str(exc)[:200] or "Market read timed out",
                }
            result["coverage_fraction"] = len(result["prices"]) / lookback_days
            result["last_observation_age_days"] = (
                (end - max(result["prices"]) - DAY_MS) / DAY_MS
                if result["prices"]
                else None
            )
            return position.instrument_id, result

    markets = dict(await asyncio.gather(*(read(p) for p in positions.values())))
    comparisons: dict[str, Any] = {}
    if compare_implementations:
        # Reuse the existing public market tools and their normalization/evidence.
        from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_mid_prices
        from wayfinder_paths.mcp.tools.tokens import onchain_list_tokens

        async def liquidity(position: Position) -> None:
            instrument = position.instrument_id
            async with semaphore:
                try:
                    async with asyncio.timeout_at(
                        min(deadline + 20, asyncio.get_running_loop().time() + 20)
                    ):
                        if position.kind == "prediction":
                            data = {
                                "book": markets[instrument].get("book"),
                                "market": markets[instrument].get("market"),
                                "summaryMode": True,
                            }
                        elif position.kind == "token" and "/" not in instrument:
                            token = markets[instrument].get("resolved_token")
                            if not token:
                                raise ValueError(
                                    "Identity unresolved; lookup before comparing pools"
                                )
                            response = await onchain_list_tokens(
                                chain_code=token["chain"]["code"],
                                token_id=instrument,
                                limit=5,
                            )
                            if not response.get("ok"):
                                raise ValueError(str(response.get("error")))
                            data = response["result"]
                        else:
                            response = await hyperliquid_search_mid_prices(
                                asset_names=[instrument], include_depth=True
                            )
                            if not response.get("ok"):
                                raise ValueError(str(response.get("error")))
                            data = response["result"]
                        comparisons[instrument] = {
                            "observations": data,
                            "retrieved_at_ms": int(time.time() * 1000),
                        }
                except (
                    httpx.HTTPError,
                    RequestException,
                    ClientError,
                    ServerError,
                    ValueError,
                    KeyError,
                    TypeError,
                    TimeoutError,
                ) as exc:
                    comparisons[instrument] = {
                        "unavailable": str(exc)[:200] or "Liquidity read timed out"
                    }

        await asyncio.gather(*(liquidity(p) for p in positions.values()))
        for instrument, position in positions.items():
            comparisons[instrument]["notional_by_budget"] = {
                str(v.budget_usd): v.budget_usd * p.capital_bps / 10000 * p.leverage
                for v in variants
                for p in [
                    next(
                        (p for p in v.positions if p.instrument_id == instrument),
                        position,
                    )
                ]
            }
            comparisons[instrument]["capacity_note"] = (
                "Observed book/pool is a screening snapshot, not total asset capacity or a sized execution quote. Never infer capacity from daily volume."
            )
    return ok(
        {
            "implementation_comparison_scope": {
                "instruments": "supplied_only",
                "alternative_discovery_performed": False,
                "note": "Discover and supply alternative routes separately. An absent route here was not checked; it is not evidence of unavailability.",
            },
            "implementation_comparisons": comparisons,
            **(
                {
                    "allocation_comparison": {
                        "basis": "identical observed UTC timestamps across proposed and counterfactual portfolios; no filled prices or expected returns",
                        "portfolios": quantify_variants(
                            variants, markets, align_portfolios=True
                        )["portfolios"],
                    }
                }
                if counterfactuals
                else {}
            ),
            **(
                {"sized_variants": [v.model_dump(mode="json") for v in variants]}
                if construction is not None
                else {}
            ),
            "portfolio_quantification": {
                "as_of_ms": as_of,
                "start_ms": start,
                "end_ms": end,
                **quantify_variants(variants, markets),
            },
        }
    )
