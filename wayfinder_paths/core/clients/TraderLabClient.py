"""Client for Trader Lab (/api/v1/trader-lab/): what consistently profitable
Hyperliquid traders do, from a daily (T+1) release.

Every read returns the server envelope ``{release, as_of, rules_version,
data, page?, usage?}`` so callers always know which release they used.
Calls are billed in API credits (cheap reads, CPU-metered queries,
per-byte downloads); see the ``using-trader-lab`` skill for costs.

Bulk work (whole datasets, local DuckDB, features, backtests) lives in
``wayfinder_paths.core.trader_lab`` (optional ``trader-lab`` extra).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.core.clients.trader_lab_types import (
    BUCKETS,
    SORTS,
    TraderLabAPIError,
)
from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

# Async queries finish within ~5 minutes server-side.
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 420.0


def _extract_error(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("code", "http_error")), str(err.get("message", ""))
        if "detail" in body:
            return str(body.get("code") or response.status_code), str(body["detail"])
        if body:
            # DRF field errors: {"field": ["msg"]}
            first = next(iter(body.items()))
            return "invalid_request", f"{first[0]}: {first[1]}"
    return "http_error", response.text[:200] or response.reason_phrase or "unknown"


class TraderLabClient(WayfinderClient):
    def _url(self, path: str) -> str:
        base = get_api_base_url().rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{base}/trader-lab{suffix}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        url = self._url(path)
        clean = {k: v for k, v in (params or {}).items() if v is not None} or None
        try:
            response = await self._authed_request(method, url, params=clean, json=json)
        except httpx.HTTPStatusError as exc:
            code, message = _extract_error(exc.response)
            raise TraderLabAPIError(
                code, message, status=exc.response.status_code, url=url
            ) from exc
        return response.json()

    # ------------------------------------------------------------ reads

    async def meta(self) -> dict[str, Any]:
        """Current release, coverage, buckets, datasets and the persistence
        headline (did past winners keep winning?)."""
        return await self._request("GET", "/meta/")

    async def leaderboard(
        self,
        *,
        bucket: str | None = None,
        sort: str = "sharpe",
        include_hft: bool = False,
        min_days: int | None = None,
        coin: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Screened wallets. `bucket` None = every style except HFT/unprofiled;
        "all" = every bucket. `sort` is descending; prefix "-" for ascending."""
        if bucket is not None and bucket not in (*BUCKETS, "all"):
            raise ValueError(f"bucket must be one of {BUCKETS} or 'all'")
        if sort.lstrip("-") not in SORTS:
            raise ValueError(f"sort must be one of {SORTS} (prefix '-' for ascending)")
        return await self._request(
            "GET",
            "/leaderboard/",
            params={
                "bucket": bucket,
                "sort": sort,
                "include_hft": "true" if include_hft else None,
                "min_days": min_days,
                "coin": coin,
                "limit": limit,
                "cursor": cursor,
            },
        )

    async def iter_leaderboard(
        self, *, max_rows: int = 500, **filters: Any
    ) -> list[dict]:
        """All leaderboard rows (following cursors) up to `max_rows`."""
        rows: list[dict] = []
        cursor = None
        while len(rows) < max_rows:
            page = await self.leaderboard(
                cursor=cursor, limit=min(100, max_rows - len(rows)), **filters
            )
            rows.extend(page["data"])
            cursor = (page.get("page") or {}).get("next")
            if not cursor:
                break
        return rows

    async def wallet(self, address: str) -> dict[str, Any]:
        """Leaderboard row plus profile card (style, hold-time quantiles,
        entry hours, markouts, coin mix, copyability, 12-month equity)."""
        return await self._request("GET", f"/wallets/{address.lower()}/")

    async def trips(
        self,
        address: str,
        *,
        coin: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Round trips (flat to flat), newest first, last 180 days."""
        return await self._request(
            "GET",
            f"/wallets/{address.lower()}/trips/",
            params={"coin": coin, "limit": limit, "cursor": cursor},
        )

    async def positions(
        self,
        address: str,
        *,
        coin: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        """Hourly positions (end the day before the release)."""
        return await self._request(
            "GET",
            f"/wallets/{address.lower()}/positions/",
            params={"coin": coin, "since": since, "until": until, "limit": limit},
        )

    async def markouts(
        self, address: str, *, coin: str | None = None
    ) -> dict[str, Any]:
        """Notional-weighted fill markouts (bp) by maker/taker."""
        return await self._request(
            "GET", f"/wallets/{address.lower()}/markouts/", params={"coin": coin}
        )

    async def flows(
        self, *, coin: str | None = None, bucket: str | None = None, days: int = 90
    ) -> dict[str, Any]:
        """Daily net flow (USD, buys positive) and long/short counts by bucket."""
        return await self._request(
            "GET",
            "/cohort/flows/",
            params={"coin": coin, "bucket": bucket, "days": days},
        )

    async def entry_states(self, *, bucket: str | None = None) -> dict[str, Any]:
        """Entry-feature histograms per bucket/side vs a baseline of all times."""
        return await self._request(
            "GET", "/cohort/entry-states/", params={"bucket": bucket}
        )

    async def copyability(self, *, bucket: str | None = None) -> dict[str, Any]:
        """Copy-delay results by bucket: delay x cost -> median net bp, share
        of wallets positive out of sample."""
        return await self._request(
            "GET", "/cohort/copyability/", params={"bucket": bucket}
        )

    async def persistence(self, *, bucket: str | None = None) -> dict[str, Any]:
        """Per vintage, horizon and selector: how many picks stayed profitable."""
        return await self._request(
            "GET", "/cohort/persistence/", params={"bucket": bucket}
        )

    # ------------------------------------------------------------ datasets

    async def datasets(self) -> dict[str, Any]:
        """Release datasets with files, sizes and sha256, plus `views_sql`
        and `features_sql` (the same SQL the server sandbox uses)."""
        return await self._request("GET", "/datasets/")

    async def download_dataset_file(
        self, name: str, file: str, dest: Path, *, sha256: str | None = None
    ) -> Path:
        """Stream one release file to `dest` (billed per byte). Verifies the
        sha256 when given; the partial file is removed on mismatch."""
        url = self._url(f"/datasets/{name}/files/{file}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        digest = hashlib.sha256()
        self._ensure_api_key_header()
        async with self.client.stream(
            "GET", url, headers=dict(self.headers), timeout=httpx.Timeout(600.0)
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                code, message = _extract_error(response)
                raise TraderLabAPIError(
                    code, message, status=response.status_code, url=url
                )
            with tmp.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    digest.update(chunk)
                    fh.write(chunk)
        if sha256 and digest.hexdigest() != sha256:
            tmp.unlink(missing_ok=True)
            raise TraderLabAPIError(
                "checksum_mismatch", f"{name}/{file} failed sha256", status=0, url=url
            )
        tmp.replace(dest)
        return dest

    # ------------------------------------------------------------ queries

    async def recipes(self) -> dict[str, Any]:
        """Parameterised server-side queries (the cheap, safe default)."""
        return await self._request("GET", "/recipes/")

    async def schema(self) -> dict[str, Any]:
        """Views and columns available to `query`."""
        return await self._request("GET", "/schema/")

    async def query(
        self,
        *,
        sql: str | None = None,
        recipe: str | None = None,
        params: dict[str, Any] | None = None,
        mode: str = "sync",
    ) -> dict[str, Any]:
        """Run read-only DuckDB SQL (one SELECT over the release views) or a
        recipe on the server. sync: <=20 s, <=10k rows. async: <=300 s, up
        to 1 GB of Parquet; returns a job to poll (`query_status`)."""
        if (sql is None) == (recipe is None):
            raise ValueError("give exactly one of sql or recipe")
        if mode not in ("sync", "async"):
            raise ValueError("mode must be 'sync' or 'async'")
        body: dict[str, Any] = {"mode": mode}
        if sql is not None:
            body["sql"] = sql
        else:
            body["recipe"], body["params"] = recipe, params or {}
        return await self._request("POST", "/query/", json=body)

    async def query_status(self, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/query/{int(job_id)}/")

    async def wait_for_query(
        self, job_id: int, *, timeout: float = POLL_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Poll an async query until it is done or failed."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            status = await self.query_status(job_id)
            if status["data"]["status"] in ("done", "failed"):
                return status
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"Trader Lab query {job_id} still running")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def download_query_result(self, job_id: int, dest: Path) -> Path:
        """Stream a finished async query's Parquet result (kept for a day)."""
        url = self._url(f"/query/{int(job_id)}/result/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_api_key_header()
        async with self.client.stream(
            "GET", url, headers=dict(self.headers), timeout=httpx.Timeout(600.0)
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                code, message = _extract_error(response)
                raise TraderLabAPIError(
                    code, message, status=response.status_code, url=url
                )
            with dest.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    fh.write(chunk)
        return dest


TRADER_LAB_CLIENT = TraderLabClient()
