from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.llama.fi"
STABLECOINS_BASE_URL = "https://stablecoins.llama.fi"
YIELDS_BASE_URL = "https://yields.llama.fi"
TIMEOUT_SECONDS = 20
ATTRIBUTION = "Data from DeFiLlama free API"
DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 100
MAX_RESPONSE_CHARACTERS = 250_000
OVERVIEW_PARAMS = {
    "excludeTotalDataChart": "true",
    "excludeTotalDataChartBreakdown": "true",
}


def _path_part(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if any(character in normalized for character in ("?", "#", "\n", "\r")):
        raise ValueError(f"{field_name} contains invalid characters")
    return quote(normalized, safe=":-_,")


class DefiLlamaFreeClient:
    """Direct DeFiLlama free API client.

    This intentionally does not call the Wayfinder backend.
    """

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        base_url: str = BASE_URL,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(f"{base_url}{path}", params=params or {})
            response.raise_for_status()
            body = response.json()

        return {
            "provider": "defillama_free",
            "url": str(response.url),
            "result": body,
            "evidence": [
                {
                    "provider": "defillama_free",
                    "sourceType": "api",
                    "url": str(response.url),
                    "clientDirect": True,
                    "attributionRequired": True,
                    "attribution": ATTRIBUTION,
                }
            ],
        }

    async def protocols(self) -> dict[str, Any]:
        return await self._get("/protocols")

    async def protocols_page(
        self, *, limit: int = DEFAULT_PAGE_LIMIT, cursor: str = "_"
    ) -> dict[str, Any]:
        response = await self.protocols()
        protocols = response.get("result")
        if not isinstance(protocols, list):
            protocols = []
        items = [
            _compact_protocol(protocol)
            for protocol in protocols
            if isinstance(protocol, dict)
        ]
        response["result"] = _paged_result(
            dataset="protocols",
            source_url=response["url"],
            items=items,
            limit=limit,
            cursor=cursor,
            totals={"protocolCount": len(items)},
        )
        return _enforce_response_budget(response)

    async def protocol_search(self, query: str, limit: int = 10) -> dict[str, Any]:
        response = await self.protocols()
        normalized = str(query).strip().lower()
        if not normalized:
            raise ValueError("query is required")
        protocols = response.get("result")
        if not isinstance(protocols, list):
            protocols = []

        matches = []
        for protocol in protocols:
            if not isinstance(protocol, dict):
                continue
            haystack = " ".join(
                str(protocol.get(key) or "")
                for key in ("name", "slug", "symbol", "category", "description")
            ).lower()
            if normalized not in haystack:
                continue
            matches.append(
                {
                    "name": protocol.get("name"),
                    "slug": protocol.get("slug"),
                    "symbol": protocol.get("symbol"),
                    "category": protocol.get("category"),
                    "chains": protocol.get("chains"),
                    "tvl": protocol.get("tvl"),
                    "change_1d": protocol.get("change_1d"),
                    "change_7d": protocol.get("change_7d"),
                    "url": protocol.get("url"),
                }
            )
            if len(matches) >= max(1, min(int(limit), 25)):
                break

        return {
            **response,
            "result": {
                "query": query,
                "matches": matches,
                "count": len(matches),
            },
        }

    async def protocol(self, protocol_slug: str) -> dict[str, Any]:
        return await self._get(f"/protocol/{_path_part(protocol_slug, 'protocolSlug')}")

    async def tvl(self, protocol_slug: str) -> dict[str, Any]:
        return await self._get(f"/tvl/{_path_part(protocol_slug, 'protocolSlug')}")

    async def protocol_fees(
        self,
        protocol_slug: str,
        *,
        data_type: str = "dailyFees",
        days: int = 30,
    ) -> dict[str, Any]:
        normalized_type = str(data_type).strip()
        if normalized_type not in {"dailyFees", "dailyRevenue"}:
            raise ValueError("data_type must be dailyFees or dailyRevenue")
        response = await self._get(
            f"/summary/fees/{_path_part(protocol_slug, 'protocolSlug')}",
            params={"dataType": normalized_type},
        )
        result = (
            response.get("result") if isinstance(response.get("result"), dict) else {}
        )
        rows = _last_daily_rows(result.get("totalDataChart"), days=days)
        chain_rows = _last_daily_breakdown_rows(
            result.get("totalDataChartBreakdown"), days=days
        )
        response["result"] = {
            "protocolSlug": protocol_slug,
            "dataType": normalized_type,
            "days": days,
            "dailyRows": rows,
            "weeklyRollups": _weekly_sum_rollups(rows),
            "chainDailyRows": chain_rows,
            "latestDaily": rows[-1] if rows else None,
        }
        return response

    async def protocol_tvl_history(
        self,
        protocol_slug: str,
        *,
        days: int = 30,
    ) -> dict[str, Any]:
        response = await self.protocol(protocol_slug)
        result = (
            response.get("result") if isinstance(response.get("result"), dict) else {}
        )
        rows = _last_tvl_rows(result.get("tvl"), days=days)
        chain_summary = _chain_tvl_summary(result.get("chainTvls"), days=days)
        response["result"] = {
            "protocolSlug": protocol_slug,
            "days": days,
            "dailyRows": rows,
            "latestDaily": rows[-1] if rows else None,
            "chainSummary": chain_summary,
        }
        return response

    async def chains(
        self, *, limit: int = DEFAULT_PAGE_LIMIT, cursor: str = "_"
    ) -> dict[str, Any]:
        response = await self._get("/v2/chains")
        chains = response.get("result")
        if not isinstance(chains, list):
            chains = []
        items = [_compact_chain(chain) for chain in chains if isinstance(chain, dict)]
        items.sort(key=lambda item: _number(item.get("tvl")), reverse=True)
        response["result"] = _paged_result(
            dataset="chains",
            source_url=response["url"],
            items=items,
            limit=limit,
            cursor=cursor,
            totals={"chainCount": len(items)},
        )
        return _enforce_response_budget(response)

    async def stablecoins(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str = "_",
    ) -> dict[str, Any]:
        response = await self._get("/stablecoins", base_url=STABLECOINS_BASE_URL)
        result = (
            response.get("result") if isinstance(response.get("result"), dict) else {}
        )
        assets = result.get("peggedAssets")
        if not isinstance(assets, list):
            assets = []
        items = [
            _compact_stablecoin(asset) for asset in assets if isinstance(asset, dict)
        ]
        items.sort(key=lambda item: _number(item.get("circulatingUsd")), reverse=True)
        response["result"] = _paged_result(
            dataset="stablecoins",
            source_url=response["url"],
            items=items,
            limit=limit,
            cursor=cursor,
            totals={
                "peggedAssetCount": len(items),
                "chainCount": len(result.get("chains") or []),
            },
        )
        return _enforce_response_budget(response)

    async def yields_pools(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str = "_",
    ) -> dict[str, Any]:
        response = await self._get("/pools", base_url=YIELDS_BASE_URL)
        result = (
            response.get("result") if isinstance(response.get("result"), dict) else {}
        )
        pools = result.get("data")
        if not isinstance(pools, list):
            pools = []
        items = [_compact_yield_pool(pool) for pool in pools if isinstance(pool, dict)]
        items.sort(key=lambda item: _number(item.get("tvlUsd")), reverse=True)
        response["result"] = _paged_result(
            dataset="yields_pools",
            source_url=response["url"],
            items=items,
            limit=limit,
            cursor=cursor,
            totals={"poolCount": len(items)},
        )
        return _enforce_response_budget(response)

    async def current_prices(self, coins: str) -> dict[str, Any]:
        return await self._get(f"/prices/current/{_path_part(coins, 'coins')}")

    async def dex_overview(
        self,
        chain: str | None = None,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str = "_",
    ) -> dict[str, Any]:
        if chain:
            response = await self._get(
                f"/overview/dexs/{_path_part(chain, 'chain')}",
                params=OVERVIEW_PARAMS,
            )
        else:
            response = await self._get("/overview/dexs", params=OVERVIEW_PARAMS)
        return _compact_overview_response(
            response,
            dataset="dex_overview",
            limit=limit,
            cursor=cursor,
        )

    async def fees_overview(
        self,
        chain: str | None = None,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str = "_",
    ) -> dict[str, Any]:
        if chain:
            response = await self._get(
                f"/overview/fees/{_path_part(chain, 'chain')}",
                params=OVERVIEW_PARAMS,
            )
        else:
            response = await self._get("/overview/fees", params=OVERVIEW_PARAMS)
        return _compact_overview_response(
            response,
            dataset="fees_overview",
            limit=limit,
            cursor=cursor,
        )

    async def open_interest_overview(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str = "_",
    ) -> dict[str, Any]:
        response = await self._get("/overview/open-interest", params=OVERVIEW_PARAMS)
        return _compact_overview_response(
            response,
            dataset="open_interest_overview",
            limit=limit,
            cursor=cursor,
        )


DEFILLAMA_FREE_CLIENT = DefiLlamaFreeClient()


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, min(int(days), 365)))


def _row_date(timestamp: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(timestamp), tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _last_daily_rows(chart: Any, *, days: int) -> list[dict[str, Any]]:
    cutoff = _cutoff(days)
    rows = []
    if not isinstance(chart, list):
        return rows
    for item in chart:
        if not isinstance(item, list) or len(item) < 2:
            continue
        try:
            ts = int(item[0])
            value = float(item[1])
        except (TypeError, ValueError):
            continue
        dt = datetime.fromtimestamp(ts, tz=UTC)
        if dt < cutoff:
            continue
        rows.append({"date": dt.date().isoformat(), "value": value})
    return rows


def _last_daily_breakdown_rows(chart: Any, *, days: int) -> list[dict[str, Any]]:
    cutoff = _cutoff(days)
    rows = []
    if not isinstance(chart, list):
        return rows
    for item in chart:
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[1], dict):
            continue
        try:
            ts = int(item[0])
        except (TypeError, ValueError):
            continue
        dt = datetime.fromtimestamp(ts, tz=UTC)
        if dt < cutoff:
            continue
        rows.append({"date": dt.date().isoformat(), "breakdown": item[1]})
    return rows


def _weekly_sum_rollups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rollups = []
    for index in range(0, len(rows), 7):
        chunk = rows[index : index + 7]
        if not chunk:
            continue
        rollups.append(
            {
                "startDate": chunk[0]["date"],
                "endDate": chunk[-1]["date"],
                "sum": sum(float(row.get("value") or 0) for row in chunk),
                "days": len(chunk),
            }
        )
    return rollups


def _last_tvl_rows(chart: Any, *, days: int) -> list[dict[str, Any]]:
    cutoff = _cutoff(days)
    rows = []
    if not isinstance(chart, list):
        return rows
    for item in chart:
        if not isinstance(item, dict):
            continue
        date_value = item.get("date")
        date_text = _row_date(date_value)
        if date_text is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(date_value), tz=UTC)
            value = float(item.get("totalLiquidityUSD"))
        except (TypeError, ValueError, OSError):
            continue
        if dt < cutoff:
            continue
        rows.append({"date": date_text, "tvlUsd": value})
    return rows


def _chain_tvl_summary(chain_tvls: Any, *, days: int) -> list[dict[str, Any]]:
    if not isinstance(chain_tvls, dict):
        return []
    summary = []
    for chain, payload in chain_tvls.items():
        if not isinstance(payload, dict):
            continue
        rows = _last_tvl_rows(payload.get("tvl"), days=days)
        if not rows:
            continue
        first = rows[0]["tvlUsd"]
        latest = rows[-1]["tvlUsd"]
        summary.append(
            {
                "chain": chain,
                "latestTvlUsd": latest,
                "startTvlUsd": first,
                "changeUsd": latest - first,
                "changePct": ((latest - first) / first) if first else None,
                "days": len(rows),
            }
        )
    return sorted(
        summary, key=lambda row: abs(float(row["latestTvlUsd"])), reverse=True
    )


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_PAGE_LIMIT))


def _cursor_offset(cursor: str) -> int:
    normalized = str(cursor or "_").strip()
    if normalized in {"", "_", "none", "null"}:
        return 0
    try:
        offset = int(normalized)
    except ValueError as exc:
        raise ValueError("cursor must be '_' or a numeric offset") from exc
    if offset < 0:
        raise ValueError("cursor must be zero or greater")
    return offset


def _paged_result(
    *,
    dataset: str,
    source_url: str,
    items: list[dict[str, Any]],
    limit: int,
    cursor: str,
    totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit)
    offset = _cursor_offset(cursor)
    page_items = items[offset : offset + bounded_limit]
    next_offset = offset + len(page_items)
    has_more = next_offset < len(items)
    return {
        "dataset": dataset,
        "sourceUrl": source_url,
        "items": page_items,
        "page": {
            "limit": bounded_limit,
            "cursor": str(offset),
            "nextCursor": str(next_offset) if has_more else None,
            "hasMore": has_more,
            "returned": len(page_items),
            "totalAvailable": len(items),
        },
        "totals": totals or {},
        "rawPayloadOmitted": True,
        "attribution": ATTRIBUTION,
    }


def _compact_overview_response(
    response: dict[str, Any],
    *,
    dataset: str,
    limit: int,
    cursor: str,
) -> dict[str, Any]:
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    protocols = result.get("protocols")
    if not isinstance(protocols, list):
        protocols = []
    items = [
        _compact_overview_protocol(protocol)
        for protocol in protocols
        if isinstance(protocol, dict)
    ]
    items.sort(key=lambda item: _number(item.get("total24h")), reverse=True)
    response["result"] = _paged_result(
        dataset=dataset,
        source_url=response["url"],
        items=items,
        limit=limit,
        cursor=cursor,
        totals=_overview_totals(result),
    )
    response["result"]["omittedFields"] = [
        "totalDataChart",
        "totalDataChartBreakdown",
        "protocols[].breakdown24h",
        "protocols[].breakdown30d",
    ]
    return _enforce_response_budget(response)


def _overview_totals(result: dict[str, Any]) -> dict[str, Any]:
    total_keys = [
        "chain",
        "allChains",
        "total24h",
        "total48hto24h",
        "total7d",
        "total14dto7d",
        "total30d",
        "total60dto30d",
        "total1y",
        "totalAllTime",
        "change_1d",
        "change_7d",
        "change_1m",
        "change_7dover7d",
        "change_30dover30d",
        "total7DaysAgo",
        "total30DaysAgo",
    ]
    totals = {key: result.get(key) for key in total_keys if key in result}
    protocols = result.get("protocols")
    if isinstance(protocols, list):
        totals["protocolCount"] = len(protocols)
    return totals


def _compact_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": protocol.get("name"),
        "slug": protocol.get("slug"),
        "symbol": protocol.get("symbol"),
        "category": protocol.get("category"),
        "chains": protocol.get("chains"),
        "tvl": protocol.get("tvl"),
        "change_1d": protocol.get("change_1d"),
        "change_7d": protocol.get("change_7d"),
        "url": protocol.get("url"),
    }


def _compact_chain(chain: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": chain.get("name"),
        "tvl": chain.get("tvl"),
        "tokenSymbol": chain.get("tokenSymbol"),
        "chainId": chain.get("chainId"),
        "cmcId": chain.get("cmcId"),
        "change_1d": chain.get("change_1d"),
        "change_7d": chain.get("change_7d"),
    }


def _compact_stablecoin(asset: dict[str, Any]) -> dict[str, Any]:
    circulating_usd = _number(_nested_get(asset.get("circulating"), "peggedUSD"))
    return {
        "id": asset.get("id"),
        "name": asset.get("name"),
        "symbol": asset.get("symbol"),
        "geckoId": asset.get("gecko_id"),
        "pegType": asset.get("pegType"),
        "pegMechanism": asset.get("pegMechanism"),
        "priceSource": asset.get("priceSource"),
        "circulatingUsd": circulating_usd,
        "circulatingUsdPrevDay": _number(
            _nested_get(asset.get("circulatingPrevDay"), "peggedUSD")
        ),
        "circulatingUsdPrevWeek": _number(
            _nested_get(asset.get("circulatingPrevWeek"), "peggedUSD")
        ),
        "circulatingUsdPrevMonth": _number(
            _nested_get(asset.get("circulatingPrevMonth"), "peggedUSD")
        ),
        "topChains": _top_chain_circulating(asset.get("chainCirculating"), limit=5),
    }


def _compact_yield_pool(pool: dict[str, Any]) -> dict[str, Any]:
    return {
        "chain": pool.get("chain"),
        "project": pool.get("project"),
        "symbol": pool.get("symbol"),
        "pool": pool.get("pool"),
        "tvlUsd": pool.get("tvlUsd"),
        "apy": pool.get("apy"),
        "apyBase": pool.get("apyBase"),
        "apyReward": pool.get("apyReward"),
        "apyPct1D": pool.get("apyPct1D"),
        "apyPct7D": pool.get("apyPct7D"),
        "apyPct30D": pool.get("apyPct30D"),
        "stablecoin": pool.get("stablecoin"),
        "ilRisk": pool.get("ilRisk"),
        "exposure": pool.get("exposure"),
        "underlyingTokens": pool.get("underlyingTokens"),
        "rewardTokens": pool.get("rewardTokens"),
    }


def _compact_overview_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": protocol.get("name"),
        "displayName": protocol.get("displayName"),
        "slug": protocol.get("slug"),
        "module": protocol.get("module"),
        "category": protocol.get("category"),
        "chains": protocol.get("chains"),
        "total24h": protocol.get("total24h"),
        "total7d": protocol.get("total7d"),
        "total30d": protocol.get("total30d"),
        "total1y": protocol.get("total1y"),
        "totalAllTime": protocol.get("totalAllTime"),
        "change_1d": protocol.get("change_1d"),
        "change_7d": protocol.get("change_7d"),
        "change_1m": protocol.get("change_1m"),
        "topBreakdown24h": _top_breakdown(protocol.get("breakdown24h"), limit=5),
        "topBreakdown30d": _top_breakdown(protocol.get("breakdown30d"), limit=5),
    }


def _top_breakdown(value: Any, *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, dict):
        return rows
    for chain, chain_value in value.items():
        if isinstance(chain_value, dict):
            for name, amount in chain_value.items():
                rows.append({"chain": chain, "name": name, "value": _number(amount)})
        else:
            rows.append({"chain": chain, "name": None, "value": _number(chain_value)})
    rows.sort(key=lambda row: row["value"], reverse=True)
    return rows[:limit]


def _top_chain_circulating(value: Any, *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, dict):
        return rows
    for chain, payload in value.items():
        rows.append(
            {
                "chain": chain,
                "circulatingUsd": _number(_nested_get(payload, "current.peggedUSD")),
            }
        )
    rows.sort(key=lambda row: row["circulatingUsd"], reverse=True)
    return rows[:limit]


def _nested_get(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _enforce_response_budget(response: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(response, default=str, separators=(",", ":"))
    if len(rendered) <= MAX_RESPONSE_CHARACTERS:
        return response

    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        response["result"] = {
            "truncated": True,
            "reason": "response_exceeds_budget",
            "maxResponseCharacters": MAX_RESPONSE_CHARACTERS,
            "actualCharacters": len(rendered),
            "rawPayloadOmitted": True,
            "attribution": ATTRIBUTION,
        }
        return response

    while result["items"] and len(rendered) > MAX_RESPONSE_CHARACTERS:
        result["items"] = result["items"][: max(1, len(result["items"]) // 2)]
        page = result.get("page")
        if isinstance(page, dict):
            offset = _cursor_offset(str(page.get("cursor") or "0"))
            page["returned"] = len(result["items"])
            page["nextCursor"] = str(offset + len(result["items"]))
            page["hasMore"] = True
        rendered = json.dumps(response, default=str, separators=(",", ":"))

    if len(rendered) > MAX_RESPONSE_CHARACTERS:
        result["items"] = []
        result["truncated"] = True
        result["reason"] = "response_exceeds_budget"
        result["maxResponseCharacters"] = MAX_RESPONSE_CHARACTERS
    return response
