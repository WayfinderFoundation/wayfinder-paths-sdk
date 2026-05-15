from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.llama.fi"
YIELDS_BASE_URL = "https://yields.llama.fi"
TIMEOUT_SECONDS = 20
ATTRIBUTION = "Data from DeFiLlama free API"


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
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        rows = _last_daily_rows(result.get("totalDataChart"), days=days)
        chain_rows = _last_daily_breakdown_rows(result.get("totalDataChartBreakdown"), days=days)
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
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
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

    async def chains(self) -> dict[str, Any]:
        return await self._get("/v2/chains")

    async def stablecoins(self) -> dict[str, Any]:
        return await self._get("/stablecoins")

    async def yields_pools(self) -> dict[str, Any]:
        return await self._get("/pools", base_url=YIELDS_BASE_URL)

    async def current_prices(self, coins: str) -> dict[str, Any]:
        return await self._get(f"/prices/current/{_path_part(coins, 'coins')}")

    async def dex_overview(self, chain: str | None = None) -> dict[str, Any]:
        if chain:
            return await self._get(f"/overview/dexs/{_path_part(chain, 'chain')}")
        return await self._get("/overview/dexs")

    async def fees_overview(self, chain: str | None = None) -> dict[str, Any]:
        if chain:
            return await self._get(f"/overview/fees/{_path_part(chain, 'chain')}")
        return await self._get("/overview/fees")

    async def open_interest_overview(self) -> dict[str, Any]:
        return await self._get("/overview/open-interest")


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
    return sorted(summary, key=lambda row: abs(float(row["latestTvlUsd"])), reverse=True)
