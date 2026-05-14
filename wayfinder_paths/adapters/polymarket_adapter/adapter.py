from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Literal

import httpx
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from py_clob_client_v2 import AssetType, BalanceAllowanceParams, SignatureTypeV2
from py_clob_client_v2.client import ClobClient  # type: ignore[import-untyped]
from py_clob_client_v2.clob_types import (  # type: ignore[import-untyped]
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgsV2,
    OrderPayload,
)
from py_clob_client_v2.config import (  # type: ignore[import-untyped]
    get_contract_config,
)

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.polymarket import (
    MAX_UINT256,
    POLYGON_CHAIN_ID,
    POLYGON_P_USDC_PROXY_ADDRESS,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
    POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
    POLYMARKET_APPROVAL_TARGETS,
    POLYMARKET_BRIDGE_BASE_URL,
    POLYMARKET_BUILDER_CODE,
    POLYMARKET_CLOB_BASE_URL,
    POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
    POLYMARKET_DATA_BASE_URL,
    POLYMARKET_DEPOSIT_WALLET_FACTORY,
    POLYMARKET_DEPOSIT_WALLET_IMPLEMENTATION,
    POLYMARKET_GAMMA_BASE_URL,
    POLYMARKET_RELAYER_BASE_URL,
    ZERO32_STR,
    derive_deposit_wallet,
    polymarket_deposit_wallet_id,
)
from wayfinder_paths.core.constants.polymarket_abi import (
    CONDITIONAL_TOKENS_ABI,
    POLYMARKET_DEPOSIT_WALLET_BATCH_TYPES,
    POLYMARKET_DEPOSIT_WALLET_FACTORY_ABI,
    TOKEN_UNWRAP_ABI,
)
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import (
    build_send_transaction,
    get_token_balance,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.units import to_erc20_raw
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


def _normalize_text(value: str) -> str:
    s = str(value or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _fuzzy_score(query: str, text: str) -> float:
    q = _normalize_text(query)
    t = _normalize_text(text)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    return SequenceMatcher(None, q, t).ratio()


class PolymarketAdapter(BaseAdapter):
    adapter_type = "POLYMARKET"

    DEFAULT_MAX_SLIPPAGE_PCT = (
        2.0  # Polymarket prices live in [0, 1], no native slippage param
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback=None,
        sign_hash_callback=None,
        sign_typed_data_callback=None,
        wallet_address: str | None = None,
        gamma_base_url: str = POLYMARKET_GAMMA_BASE_URL,
        clob_base_url: str = POLYMARKET_CLOB_BASE_URL,
        data_base_url: str = POLYMARKET_DATA_BASE_URL,
        bridge_base_url: str = POLYMARKET_BRIDGE_BASE_URL,
        relayer_base_url: str = POLYMARKET_RELAYER_BASE_URL,
        http_timeout_s: float = 30.0,
    ) -> None:
        super().__init__("polymarket_adapter", config)

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self.sign_callback = sign_callback
        self.sign_hash_callback = sign_hash_callback
        self.sign_typed_data_callback = sign_typed_data_callback

        timeout = httpx.Timeout(http_timeout_s)
        self._gamma_http = httpx.AsyncClient(base_url=gamma_base_url, timeout=timeout)
        self._clob_http = httpx.AsyncClient(base_url=clob_base_url, timeout=timeout)
        self._data_http = httpx.AsyncClient(base_url=data_base_url, timeout=timeout)
        self._bridge_http = httpx.AsyncClient(base_url=bridge_base_url, timeout=timeout)
        self._relayer_http = httpx.AsyncClient(
            base_url=relayer_base_url, timeout=timeout
        )

        self._clob_client: ClobClient | None = None  # type: ignore[valid-type]
        self._api_creds_set = False
        self._setup_complete = False
        self._builder_creds: dict[str, str] | None = None

    async def close(self) -> None:
        await asyncio.gather(
            self._gamma_http.aclose(),
            self._clob_http.aclose(),
            self._data_http.aclose(),
            self._bridge_http.aclose(),
            self._relayer_http.aclose(),
            return_exceptions=True,
        )

    @staticmethod
    def _normalize_market(market: dict[str, Any]) -> dict[str, Any]:
        out = dict(market)
        for key in ("outcomes", "outcomePrices", "clobTokenIds"):
            if key in out:
                out[key] = json.loads(out[key])
        return out

    async def list_markets(
        self,
        *,
        closed: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str | None = None,
        ascending: bool | None = None,
        **filters: Any,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = str(ascending).lower()
        params.update({k: v for k, v in filters.items() if v is not None})

        try:
            res = await self._gamma_http.get("/markets", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list):
                return False, f"Unexpected /markets response: {type(data).__name__}"
            normalized = [self._normalize_market(m) for m in data]
            return True, normalized
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def list_events(
        self,
        *,
        closed: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str | None = None,
        ascending: bool | None = None,
        **filters: Any,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = str(ascending).lower()
        params.update({k: v for k, v in filters.items() if v is not None})

        try:
            res = await self._gamma_http.get("/events", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list):
                return False, f"Unexpected /events response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_market_by_slug(self, slug: str) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._gamma_http.get(f"/markets/slug/{slug}")
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return (
                    False,
                    f"Unexpected /markets/slug response: {type(data).__name__}",
                )
            return True, self._normalize_market(data)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_event_by_slug(self, slug: str) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._gamma_http.get(f"/events/slug/{slug}")
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /events/slug response: {type(data).__name__}"
            if "markets" in data:
                data = dict(data)
                data["markets"] = [self._normalize_market(m) for m in data["markets"]]
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def public_search(
        self,
        *,
        q: str,
        limit_per_type: int = 10,
        page: int = 1,
        keep_closed_markets: bool = False,
        **kwargs: Any,
    ) -> tuple[bool, dict[str, Any] | str]:
        params: dict[str, Any] = {
            "q": q,
            "limit_per_type": limit_per_type,
            "page": page,
            "keep_closed_markets": "1" if keep_closed_markets else "0",
        }
        params.update({k: v for k, v in kwargs.items() if v is not None})

        try:
            res = await self._gamma_http.get("/public-search", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return (
                    False,
                    f"Unexpected /public-search response: {type(data).__name__}",
                )
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def search_markets_fuzzy(
        self,
        *,
        query: str,
        limit: int = 10,
        page: int = 1,
        keep_closed_markets: bool = False,
        events_status: str | None = None,
        end_date_min: str | None = None,
        rerank: bool = True,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, data = await self.public_search(
            q=query,
            limit_per_type=max(limit, 1),
            page=page,
            keep_closed_markets=keep_closed_markets,
            events_status=events_status,
        )
        if not ok:
            return False, str(data)

        markets: list[dict[str, Any]] = []
        for event in data.get("events") or []:
            for market in event.get("markets") or []:
                markets.append(
                    {
                        **self._normalize_market(market),
                        "_event": {
                            "id": event.get("id"),
                            "slug": event.get("slug"),
                            "title": event.get("title"),
                        },
                    }
                )

        if end_date_min:
            markets = [
                m
                for m in markets
                if (m.get("endDateIso") or m.get("endDate") or "") >= end_date_min
            ]

        if not rerank:
            return True, markets[:limit]

        def score(m: dict[str, Any]) -> float:
            return max(
                _fuzzy_score(query, str(m.get("question") or "")),
                _fuzzy_score(query, str(m.get("slug") or "")),
                _fuzzy_score(query, str((m.get("_event") or {}).get("title") or "")),
            )

        markets.sort(key=score, reverse=True)
        return True, markets[:limit]

    async def get_market_by_condition_id(
        self, *, condition_id: str
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._gamma_http.get(
                "/markets", params={"condition_ids": condition_id}
            )
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                return False, "Market not found"
            return True, self._normalize_market(data[0])
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def resolve_clob_token_id(
        self,
        *,
        market: dict[str, Any],
        outcome: str | int,
    ) -> tuple[bool, str]:
        outcomes: list[Any] = market.get("outcomes") or []
        token_ids: list[Any] = market.get("clobTokenIds") or []

        if not token_ids:
            return False, "Market missing clobTokenIds (not tradable on CLOB)"

        if isinstance(outcome, int):
            idx = outcome
        else:
            want = _normalize_text(outcome)
            idx = -1
            if outcomes:
                for i, o in enumerate(outcomes):
                    if _normalize_text(str(o)) == want:
                        idx = i
                        break
                if idx == -1 and want in {"yes", "no"} and len(outcomes) >= 2:
                    idx = 0 if want == "yes" else 1
                if idx == -1:
                    best = max(
                        enumerate(outcomes),
                        key=lambda t: _fuzzy_score(want, str(t[1])),
                        default=None,
                    )
                    if best and _fuzzy_score(want, str(best[1])) >= 0.5:
                        idx = best[0]
            else:
                if want in {"yes", "no"} and len(token_ids) >= 2:
                    idx = 0 if want == "yes" else 1

        if idx < 0 or idx >= len(token_ids):
            return False, f"Outcome index out of range: {outcome}"

        tok = token_ids[idx]
        return True, str(tok)

    @staticmethod
    def _decimal_or_none(value: Any) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite():
            return None
        return parsed

    @classmethod
    def _normalized_book_levels(
        cls,
        *,
        book: dict[str, Any],
        side: Literal["BUY", "SELL"],
    ) -> list[tuple[Decimal, Decimal]]:
        raw_levels = book.get("asks") if side == "BUY" else book.get("bids")
        if not isinstance(raw_levels, list):
            return []

        levels: list[tuple[Decimal, Decimal]] = []
        for level in raw_levels:
            if not isinstance(level, dict):
                continue
            price = cls._decimal_or_none(level.get("price"))
            size = cls._decimal_or_none(level.get("size"))
            if price is None or size is None or price <= 0 or size <= 0:
                continue
            levels.append((price, size))

        levels.sort(key=lambda item: item[0], reverse=(side == "SELL"))
        return levels

    @staticmethod
    def _decimal_to_float(value: Decimal | None) -> float | None:
        return float(value) if value is not None else None

    @staticmethod
    def _book_meta(book: dict[str, Any]) -> dict[str, Any]:
        return {
            key: book.get(key)
            for key in (
                "market",
                "asset_id",
                "timestamp",
                "hash",
                "tick_size",
                "min_order_size",
                "neg_risk",
                "last_trade_price",
            )
            if key in book
        }

    async def quote_prediction(
        self,
        *,
        market_slug: str,
        outcome: str | int = "YES",
        side: Literal["BUY", "SELL"],
        amount: float,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, market = await self.get_market_by_slug(market_slug)
        if not ok:
            return False, market

        ok_tid, token_id = self.resolve_clob_token_id(market=market, outcome=outcome)
        if not ok_tid:
            return False, token_id

        return await self.quote_market_order(
            token_id=token_id,
            side=side,
            amount=amount,
        )

    async def quote_market_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        amount: float,
    ) -> tuple[bool, dict[str, Any] | str]:
        requested_amount = self._decimal_or_none(amount)
        if requested_amount is None or requested_amount <= 0:
            return False, "amount must be positive"

        ok_book, book = await self.get_order_book(token_id=token_id)
        if not ok_book:
            return False, book
        if not isinstance(book, dict):
            return False, f"Unexpected order book response: {type(book).__name__}"

        levels = self._normalized_book_levels(book=book, side=side)
        remaining = requested_amount
        total_shares = Decimal("0")
        total_notional = Decimal("0")
        fills: list[dict[str, Any]] = []
        best_price: Decimal | None = None
        worst_price: Decimal | None = None

        for price, size in levels:
            if side == "BUY":
                available_notional = size * price
                notional = min(remaining, available_notional)
                shares = notional / price
                remaining -= notional
            else:
                shares = min(remaining, size)
                notional = shares * price
                remaining -= shares

            if shares <= 0 or notional <= 0:
                continue

            if best_price is None:
                best_price = price
            worst_price = price
            total_shares += shares
            total_notional += notional
            fills.append(
                {
                    "price": float(price),
                    "shares": float(shares),
                    "notional_usdc": float(notional),
                }
            )

            if remaining <= 0:
                remaining = Decimal("0")
                break

        average_price = (total_notional / total_shares) if total_shares > 0 else None
        price_impact_bps: Decimal | None = None
        if best_price is not None and average_price is not None and best_price > 0:
            if side == "BUY":
                price_impact_bps = (
                    (average_price - best_price) / best_price
                ) * Decimal("10000")
            else:
                price_impact_bps = (
                    (best_price - average_price) / best_price
                ) * Decimal("10000")

        filled_amount = total_notional if side == "BUY" else total_shares
        return True, {
            "token_id": str(token_id),
            "side": side,
            "amount_kind": "usdc" if side == "BUY" else "shares",
            "requested_amount": float(requested_amount),
            "filled_amount": float(filled_amount),
            "unfilled_amount": float(remaining),
            "fully_fillable": remaining == 0,
            "best_price": self._decimal_to_float(best_price),
            "worst_price": self._decimal_to_float(worst_price),
            "average_price": self._decimal_to_float(average_price),
            "price_impact_bps": self._decimal_to_float(price_impact_bps),
            "shares": float(total_shares),
            "notional_usdc": float(total_notional),
            "levels_consumed": len(fills),
            "fills": fills,
            "book_meta": self._book_meta(book),
        }

    async def place_prediction(
        self,
        *,
        market_slug: str,
        outcome: str | int = "YES",
        amount_collateral: float = 1.0,
        max_slippage_pct: float | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, market = await self.get_market_by_slug(market_slug)
        if not ok:
            return False, market

        ok_tid, token_id = self.resolve_clob_token_id(market=market, outcome=outcome)
        if not ok_tid:
            return False, token_id

        return await self.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount_collateral,
            max_slippage_pct=max_slippage_pct,
        )

    async def cash_out_prediction(
        self,
        *,
        market_slug: str,
        outcome: str | int = "YES",
        shares: float = 1.0,
        max_slippage_pct: float | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, market = await self.get_market_by_slug(market_slug)
        if not ok:
            return False, market

        ok_tid, token_id = self.resolve_clob_token_id(market=market, outcome=outcome)
        if not ok_tid:
            return False, token_id

        return await self.place_market_order(
            token_id=token_id,
            side="SELL",
            amount=shares,
            max_slippage_pct=max_slippage_pct,
        )

    async def get_market_prices_history(
        self,
        *,
        market_slug: str,
        outcome: str | int = "YES",
        interval: str | None = "1d",
        start_ts: int | None = None,
        end_ts: int | None = None,
        fidelity: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, market = await self.get_market_by_slug(market_slug)
        if not ok:
            return False, market

        ok_tid, token_id = self.resolve_clob_token_id(market=market, outcome=outcome)
        if not ok_tid:
            return False, token_id

        return await self.get_prices_history(
            token_id=token_id,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=fidelity,
        )

    async def get_price(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"] = "BUY",
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._clob_http.get(
                "/price",
                params={"token_id": token_id, "side": side},
            )
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /price response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_order_book(
        self, *, token_id: str
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._clob_http.get("/book", params={"token_id": token_id})
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /book response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_order_books(
        self, *, token_ids: list[str]
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            payload = [{"token_id": t} for t in token_ids]
            res = await self._clob_http.post("/books", json=payload)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list):
                return False, f"Unexpected /books response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_prices_history(
        self,
        *,
        token_id: str,
        interval: str | None = "1d",
        start_ts: int | None = None,
        end_ts: int | None = None,
        fidelity: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        params: dict[str, Any] = {"market": token_id}
        if interval:
            params["interval"] = interval
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        if fidelity is not None:
            params["fidelity"] = fidelity

        try:
            res = await self._clob_http.get("/prices-history", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return (
                    False,
                    f"Unexpected /prices-history response: {type(data).__name__}",
                )
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_positions(
        self,
        *,
        user: str,
        limit: int = 500,
        offset: int = 0,
        **filters: Any,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        params = {
            "user": to_checksum_address(user),
            "limit": limit,
            "offset": offset,
            **{k: v for k, v in filters.items() if v is not None},
        }
        try:
            res = await self._data_http.get("/positions", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list):
                return False, f"Unexpected /positions response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_activity(
        self,
        *,
        user: str,
        limit: int = 500,
        offset: int = 0,
        **filters: Any,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        params = {
            "user": to_checksum_address(user),
            "limit": limit,
            "offset": offset,
            **{k: v for k, v in filters.items() if v is not None},
        }
        try:
            res = await self._data_http.get("/activity", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list):
                return False, f"Unexpected /activity response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_trades(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        user: str | None = None,
        **filters: Any,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user:
            params["user"] = to_checksum_address(user)
        params.update({k: v for k, v in filters.items() if v is not None})

        try:
            res = await self._data_http.get("/trades", params=params)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, list):
                return False, f"Unexpected /trades response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def bridge_quote(
        self,
        *,
        from_amount_base_unit: str,
        from_chain_id: int | str,
        from_token_address: str,
        recipient_address: str,
        to_chain_id: int | str = POLYGON_CHAIN_ID,
        to_token_address: str = POLYGON_P_USDC_PROXY_ADDRESS,
    ) -> tuple[bool, dict[str, Any] | str]:
        body = {
            "fromAmountBaseUnit": from_amount_base_unit,
            "fromChainId": str(from_chain_id),
            "fromTokenAddress": from_token_address,
            "recipientAddress": to_checksum_address(recipient_address),
            "toChainId": str(to_chain_id),
            "toTokenAddress": to_token_address,
        }
        try:
            res = await self._bridge_http.post("/quote", json=body)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /quote response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def bridge_deposit_addresses(
        self, *, address: str
    ) -> tuple[bool, dict[str, Any] | str]:
        body = {"address": to_checksum_address(address)}
        try:
            res = await self._bridge_http.post("/deposit", json=body)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /deposit response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def bridge_withdraw_addresses(
        self,
        *,
        address: str,
        to_chain_id: int | str,
        to_token_address: str,
        recipient_addr: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        body = {
            "address": to_checksum_address(address),
            "toChainId": str(to_chain_id),
            "toTokenAddress": to_token_address,
            "recipientAddr": to_checksum_address(recipient_addr),
        }
        try:
            res = await self._bridge_http.post("/withdraw", json=body)
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /withdraw response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def bridge_status(self, *, address: str) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._bridge_http.get(f"/status/{to_checksum_address(address)}")
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return False, f"Unexpected /status response: {type(data).__name__}"
            return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # Stable-asset swaps (USDC ↔ USDC.e and wraps to/from pUSD) need a little
    # headroom because LI.FI routes occasionally revert at inclusion when a
    # pool shifts between quote and inclusion. 1% is generous for stables and
    # absorbs MEV/state-change races without users having to think about it.
    _BRAP_DEFAULT_SLIPPAGE = 0.01

    async def _brap_swap_polygon(
        self,
        *,
        from_token_address: str,
        to_token_address: str,
        amount_base_unit: int,
        recipient_address: str | None = None,
        slippage: float | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Swap `from_token → to_token` on Polygon via BRAP.

        Handles USDC.e ↔ pUSD (via the backend's polymarket_bridge solver) and
        USDC ↔ USDC.e (via the generic lifi/odos/enso solvers). The BRAP HTTP
        layer accepts `to_wallet` so the encoded recipient differs from sender
        when needed.
        """
        from_address = self._require_wallet_address()
        from_token = to_checksum_address(from_token_address)
        to_token = to_checksum_address(to_token_address)
        rcpt = (
            to_checksum_address(recipient_address)
            if recipient_address
            else from_address
        )
        try:
            quote_response = await BRAP_CLIENT.get_quote(
                from_token=from_token,
                to_token=to_token,
                from_chain=POLYGON_CHAIN_ID,
                to_chain=POLYGON_CHAIN_ID,
                from_wallet=from_address,
                from_amount=str(amount_base_unit),
                to_wallet=rcpt if rcpt != from_address else None,
                slippage=slippage
                if slippage is not None
                else self._BRAP_DEFAULT_SLIPPAGE,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"BRAP quote failed: {exc}"

        quote = quote_response.get("best_quote")
        if not quote:
            return False, "No BRAP quote available"

        brap = BRAPAdapter(
            sign_callback=self.sign_callback,
            wallet_address=from_address,
        )
        return await brap.swap_from_quote(
            from_token={"address": from_token, "chain": {"id": POLYGON_CHAIN_ID}},
            to_token={"address": to_token, "chain": {"id": POLYGON_CHAIN_ID}},
            from_address=from_address,
            quote=quote,
        )

    async def _wrap_deposit_wallet_usdce_to_pusd(self, *, amount_base_unit: int) -> str:
        """USDC.e → pUSD wrap signed by the deposit wallet via its batch entry.

        Routes through BRAP's polymarket_bridge solver, which wraps 1:1 — no
        slippage. We bundle the ERC20 approval + router call into a single
        batch so the deposit wallet never holds an open USDC.e allowance
        between txs.
        """
        deposit_wallet = self.deposit_wallet_address()
        quote_response = await BRAP_CLIENT.get_quote(
            from_token=POLYGON_USDC_E_ADDRESS,
            to_token=POLYGON_P_USDC_PROXY_ADDRESS,
            from_chain=POLYGON_CHAIN_ID,
            to_chain=POLYGON_CHAIN_ID,
            from_wallet=deposit_wallet,
            from_amount=str(amount_base_unit),
            slippage=0,
        )
        quote = quote_response["best_quote"]
        calldata = quote["calldata"]
        router = to_checksum_address(calldata["to"])

        async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
            usdce = web3.eth.contract(address=POLYGON_USDC_E_ADDRESS, abi=ERC20_ABI)
            approve_data = usdce.encode_abi("approve", [router, amount_base_unit])

        result = await self._submit_wallet_batch(
            calls=[
                {
                    "target": POLYGON_USDC_E_ADDRESS,
                    "value": 0,
                    "data": approve_data,
                },
                {
                    "target": router,
                    "value": int(calldata.get("value", 0)),
                    "data": calldata["data"],
                },
            ]
        )
        return result["tx_hash"]

    async def bridge_deposit(
        self,
        *,
        from_chain_id: int,
        from_token_address: str,
        amount: str | float,
        recipient_address: str,
        token_decimals: int = 6,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Prepare Polymarket collateral on Polygon.

        Polygon fast paths via BRAP (the backend's polymarket_bridge solver
        emits the wrap calldata; lifi/odos/enso handle USDC ↔ USDC.e):
        - USDC.e -> pUSD                 (single BRAP swap)
        - Polygon native USDC -> pUSD    (two BRAP swaps: USDC -> USDC.e -> pUSD)

        Other source chains fall back to the async Polymarket Bridge
        deposit-address flow, which lands as pUSD on Polygon.
        """
        from_address, _ = self._require_signer()
        from_token = to_checksum_address(from_token_address)
        base_units = to_erc20_raw(amount, token_decimals)

        rcpt = to_checksum_address(recipient_address)
        async with web3_from_chain_id(from_chain_id) as web3:
            bal = await get_token_balance(
                from_token,
                from_chain_id,
                from_address,
                web3=web3,
                block_identifier="pending",
            )
            if bal < base_units:
                msg = (
                    "Insufficient balance for bridge_deposit "
                    f"(token={from_token}, need_base_units={base_units}, balance_base_units={bal})."
                )
                if from_chain_id == POLYGON_CHAIN_ID:
                    pusd = web3.eth.contract(
                        address=POLYGON_P_USDC_PROXY_ADDRESS,
                        abi=ERC20_ABI,
                    )
                    usdce = web3.eth.contract(
                        address=POLYGON_USDC_E_ADDRESS,
                        abi=ERC20_ABI,
                    )
                    usdc = web3.eth.contract(
                        address=POLYGON_USDC_ADDRESS,
                        abi=ERC20_ABI,
                    )
                    (
                        pusd_bal,
                        usdce_bal,
                        usdc_bal,
                    ) = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=POLYGON_CHAIN_ID,
                        calls=[
                            Call(
                                pusd, "balanceOf", args=(from_address,), postprocess=int
                            ),
                            Call(
                                usdce,
                                "balanceOf",
                                args=(from_address,),
                                postprocess=int,
                            ),
                            Call(
                                usdc, "balanceOf", args=(from_address,), postprocess=int
                            ),
                        ],
                        block_identifier="pending",
                    )
                    msg += (
                        " Polygon balances: "
                        f"pusd_base_units={pusd_bal}, "
                        f"usdc_e_base_units={usdce_bal}, "
                        f"usdc_base_units={usdc_bal}."
                    )
                    msg += (
                        f" Note: Polymarket V2 collateral is pUSD ({POLYGON_P_USDC_PROXY_ADDRESS}); "
                        f"USDC.e ({POLYGON_USDC_E_ADDRESS}) can be wrapped into pUSD on Polygon."
                    )
                return False, msg

        if from_chain_id == POLYGON_CHAIN_ID and from_token == POLYGON_USDC_E_ADDRESS:
            ok_wrap, wrap = await self._brap_swap_polygon(
                from_token_address=POLYGON_USDC_E_ADDRESS,
                to_token_address=POLYGON_P_USDC_PROXY_ADDRESS,
                amount_base_unit=base_units,
                recipient_address=rcpt,
            )
            if not ok_wrap:
                return False, wrap
            return True, {
                "method": "pusd_wrap",
                "tx_hash": wrap["tx_hash"],
                "from_chain_id": POLYGON_CHAIN_ID,
                "from_token_address": POLYGON_USDC_E_ADDRESS,
                "to_chain_id": POLYGON_CHAIN_ID,
                "to_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
                "amount_base_unit": str(base_units),
                "recipient_address": rcpt,
                "from_amount": wrap.get("from_amount"),
                "to_amount": wrap.get("to_amount"),
            }

        if from_chain_id == POLYGON_CHAIN_ID and from_token == POLYGON_USDC_ADDRESS:
            # Read USDC.e balance before leg 1 so leg 2 can use the on-chain
            # delta (the BRAP quote's `output_amount` is the quoted upper bound;
            # actual fills are usually slightly lower after slippage and would
            # cause the wrap's transferFrom to revert during gas estimation).
            usdce_balance_before = await get_token_balance(
                POLYGON_USDC_E_ADDRESS,
                POLYGON_CHAIN_ID,
                from_address,
                block_identifier="latest",
            )

            ok_swap, swap = await self._brap_swap_polygon(
                from_token_address=POLYGON_USDC_ADDRESS,
                to_token_address=POLYGON_USDC_E_ADDRESS,
                amount_base_unit=base_units,
            )
            if not ok_swap:
                return False, swap

            usdce_balance_after = await get_token_balance(
                POLYGON_USDC_E_ADDRESS,
                POLYGON_CHAIN_ID,
                from_address,
                block_identifier="latest",
            )
            usdce_received = max(0, usdce_balance_after - usdce_balance_before)
            if usdce_received <= 0:
                return False, (
                    "BRAP swap completed, but no USDC.e balance delta detected "
                    "for wrapping into pUSD."
                )

            ok_wrap, wrap = await self._brap_swap_polygon(
                from_token_address=POLYGON_USDC_E_ADDRESS,
                to_token_address=POLYGON_P_USDC_PROXY_ADDRESS,
                amount_base_unit=usdce_received,
                recipient_address=rcpt,
            )
            if not ok_wrap:
                return False, wrap
            return True, {
                "method": "brap_then_wrap",
                "tx_hash": wrap["tx_hash"],
                "from_chain_id": POLYGON_CHAIN_ID,
                "from_token_address": from_token,
                "to_chain_id": POLYGON_CHAIN_ID,
                "to_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
                "amount_base_unit": str(base_units),
                "recipient_address": rcpt,
                "from_amount": swap.get("from_amount"),
                "to_amount": wrap.get("to_amount"),
                "swap_tx_hash": swap["tx_hash"],
                "wrap_tx_hash": wrap["tx_hash"],
                "swap": swap,
                "wrap": wrap,
            }

        ok_addr, addr_data = await self.bridge_deposit_addresses(address=rcpt)
        if not ok_addr:
            return False, addr_data

        deposit_evm = (addr_data.get("address") or {}).get("evm")
        if not deposit_evm:
            return False, "Bridge did not return an EVM deposit address"

        _, sign_cb = self._require_signer()
        tx = await build_send_transaction(
            from_address=from_address,
            to_address=str(deposit_evm),
            token_address=from_token,
            chain_id=from_chain_id,
            amount=base_units,
        )
        tx_hash = await send_transaction(tx, sign_cb)

        return True, {
            "method": "polymarket_bridge",
            "tx_hash": tx_hash,
            "from_chain_id": from_chain_id,
            "from_token_address": from_token,
            "deposit_address": str(deposit_evm),
            "amount_base_unit": str(base_units),
            "recipient_address": rcpt,
        }

    async def bridge_withdraw(
        self,
        *,
        amount_pusd: str | float,
        to_chain_id: int,
        to_token_address: str,
        recipient_addr: str,
        token_decimals: int = 6,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Withdraw Polymarket V2 collateral to a destination token.

        Polygon fast paths via BRAP:
        - pUSD -> USDC.e                 (single BRAP swap, polymarket_bridge solver)
        - pUSD -> Polygon native USDC    (two BRAP swaps: pUSD -> USDC.e -> USDC)

        For other destination chains, unwrap pUSD -> USDC.e on Polygon (still
        via BRAP), then fall back to the async Polymarket bridge withdraw-address
        flow.
        """
        from_address, sign_cb = self._require_signer()
        base_units = to_erc20_raw(amount_pusd, token_decimals)
        rcpt = to_checksum_address(recipient_addr)

        # Same pattern as bridge_deposit: read USDC.e balance before the
        # unwrap so the optional second leg uses the actual on-chain delta.
        usdce_balance_before = await get_token_balance(
            POLYGON_USDC_E_ADDRESS,
            POLYGON_CHAIN_ID,
            from_address,
            block_identifier="latest",
        )

        ok_unwrap, unwrap = await self._brap_swap_polygon(
            from_token_address=POLYGON_P_USDC_PROXY_ADDRESS,
            to_token_address=POLYGON_USDC_E_ADDRESS,
            amount_base_unit=base_units,
        )
        if not ok_unwrap:
            return False, unwrap

        unwrap_result = {
            "method": "pusd_unwrap",
            "tx_hash": unwrap["tx_hash"],
            "from_chain_id": POLYGON_CHAIN_ID,
            "from_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
            "to_chain_id": POLYGON_CHAIN_ID,
            "to_token_address": POLYGON_USDC_E_ADDRESS,
            "amount_base_unit": str(base_units),
            "recipient_address": from_address,
            "from_amount": unwrap.get("from_amount"),
            "to_amount": unwrap.get("to_amount"),
        }

        if (
            to_chain_id == POLYGON_CHAIN_ID
            and to_checksum_address(to_token_address) == POLYGON_USDC_E_ADDRESS
            and rcpt == from_address
        ):
            return True, {**unwrap_result, "recipient_addr": rcpt}

        if (
            to_chain_id == POLYGON_CHAIN_ID
            and to_checksum_address(to_token_address) == POLYGON_USDC_ADDRESS
            and rcpt == from_address
        ):
            usdce_balance_after = await get_token_balance(
                POLYGON_USDC_E_ADDRESS,
                POLYGON_CHAIN_ID,
                from_address,
                block_identifier="latest",
            )
            usdce_received = max(0, usdce_balance_after - usdce_balance_before)
            if usdce_received <= 0:
                return False, (
                    "BRAP unwrap completed, but no USDC.e balance delta "
                    "detected for swap into USDC."
                )
            ok_swap, swap = await self._brap_swap_polygon(
                from_token_address=POLYGON_USDC_E_ADDRESS,
                to_token_address=POLYGON_USDC_ADDRESS,
                amount_base_unit=usdce_received,
            )
            if not ok_swap:
                return False, swap
            return True, {
                "method": "unwrap_then_brap",
                "tx_hash": swap["tx_hash"],
                "from_chain_id": POLYGON_CHAIN_ID,
                "from_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
                "to_chain_id": POLYGON_CHAIN_ID,
                "to_token_address": POLYGON_USDC_ADDRESS,
                "amount_base_unit": str(base_units),
                "recipient_addr": rcpt,
                "from_amount": unwrap.get("from_amount"),
                "to_amount": swap.get("to_amount"),
                "unwrap_tx_hash": unwrap["tx_hash"],
                "swap_tx_hash": swap["tx_hash"],
                "unwrap": unwrap_result,
                "swap": swap,
            }

        ok_addr, addr_data = await self.bridge_withdraw_addresses(
            address=from_address,
            to_chain_id=to_chain_id,
            to_token_address=to_token_address,
            recipient_addr=rcpt,
        )
        if not ok_addr:
            return False, addr_data

        withdraw_evm = (addr_data.get("address") or {}).get("evm")
        if not withdraw_evm:
            return False, "Bridge did not return an EVM withdraw address"

        tx = await build_send_transaction(
            from_address=from_address,
            to_address=str(withdraw_evm),
            token_address=POLYGON_USDC_E_ADDRESS,
            chain_id=POLYGON_CHAIN_ID,
            amount=base_units,
        )
        tx_hash = await send_transaction(tx, sign_cb)

        return True, {
            "method": "polymarket_bridge",
            "tx_hash": tx_hash,
            "from_chain_id": POLYGON_CHAIN_ID,
            "from_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
            "withdraw_address": str(withdraw_evm),
            "amount_base_unit": str(base_units),
            "to_chain_id": to_chain_id,
            "to_token_address": to_token_address,
            "recipient_addr": rcpt,
            "unwrap": unwrap_result,
        }

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError(
                "wallet_address is required. Use get_adapter(PolymarketAdapter, wallet_label)."
            )
        return self.wallet_address

    def deposit_wallet_address(self) -> str:
        return derive_deposit_wallet(self._require_wallet_address())

    def _require_signer(self) -> tuple[str, Any]:
        addr = self._require_wallet_address()
        if not self.sign_callback:
            raise ValueError(
                "sign_callback is required. Use get_adapter(PolymarketAdapter, wallet_label)."
            )
        return addr, self.sign_callback

    def _contract_addrs(self, *, neg_risk: bool = False) -> dict[str, str]:
        cfg = get_contract_config(POLYGON_CHAIN_ID)
        return {
            "exchange": str(cfg.neg_risk_exchange_v2 if neg_risk else cfg.exchange_v2),
            "collateral": str(cfg.collateral),
            "conditional_tokens": str(cfg.conditional_tokens),
        }

    async def _ensure_builder_creds(self) -> dict[str, str]:
        if self._builder_creds:
            return self._builder_creds
        owner_client = ClobClient(  # type: ignore[misc]
            str(self._clob_http.base_url),
            chain_id=POLYGON_CHAIN_ID,
            address_override=self._require_wallet_address(),
            sign_callback_override=self.sign_hash_callback,
        )
        creds = await owner_client.create_or_derive_api_creds()
        owner_client.set_api_creds(creds)
        raw = owner_client.create_builder_api_key()
        self._builder_creds = {
            "key": str(raw["key"]),
            "secret": str(raw["secret"]),
            "passphrase": str(raw["passphrase"]),
        }
        return self._builder_creds

    async def _builder_headers(
        self, method: str, path: str, body: str
    ) -> dict[str, str]:
        creds = await self._ensure_builder_creds()
        ts = str(int(time.time()))
        # Relayer expects the canonical JSON body with single→double quote normalization
        message = f"{ts}{method}{path}" + body.replace("'", '"')
        sig = base64.urlsafe_b64encode(
            hmac.new(
                base64.urlsafe_b64decode(creds["secret"]),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {
            "Content-Type": "application/json",
            "POLY_BUILDER_API_KEY": creds["key"],
            "POLY_BUILDER_TIMESTAMP": ts,
            "POLY_BUILDER_PASSPHRASE": creds["passphrase"],
            "POLY_BUILDER_SIGNATURE": sig,
        }

    async def _poll_relayer_tx(self, transaction_id: str) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        for _ in range(720):
            res = await self._relayer_http.get(
                "/transaction", params={"id": transaction_id}
            )
            res.raise_for_status()
            rows = res.json()
            if rows:
                last = rows[0]
                state = last["state"]
                if state == "STATE_MINED":
                    return last
                if state in {"STATE_FAILED", "STATE_INVALID"}:
                    raise ValueError(f"Relayer transaction failed: {last}")
            await asyncio.sleep(0.25)
        raise TimeoutError(f"Timed out waiting for relayer tx {transaction_id}: {last}")

    async def _submit_wallet_batch(
        self, *, calls: list[dict[str, Any]]
    ) -> dict[str, Any]:
        owner = self._require_wallet_address()
        deposit_wallet = self.deposit_wallet_address()
        nonce_res = await self._relayer_http.get(
            "/nonce", params={"address": owner, "type": "WALLET"}
        )
        nonce_res.raise_for_status()
        nonce: int = nonce_res.json()["nonce"]
        deadline = int(time.time()) + 600
        signature: str = await self.sign_typed_data_callback(
            {
                "primaryType": "Batch",
                "types": POLYMARKET_DEPOSIT_WALLET_BATCH_TYPES,
                "domain": {
                    "name": "DepositWallet",
                    "version": "1",
                    "chainId": POLYGON_CHAIN_ID,
                    "verifyingContract": deposit_wallet,
                },
                "message": {
                    "wallet": deposit_wallet,
                    "nonce": nonce,
                    "deadline": deadline,
                    "calls": calls,
                },
            }
        )
        payload = {
            "type": "WALLET",
            "from": owner,
            "to": POLYMARKET_DEPOSIT_WALLET_FACTORY,
            "nonce": str(nonce),
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": deposit_wallet,
                "deadline": str(deadline),
                "calls": [
                    {
                        "target": c["target"],
                        "value": str(c["value"]),
                        "data": c["data"],
                    }
                    for c in calls
                ],
            },
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = await self._builder_headers("POST", "/submit", body)
        submit_res = await self._relayer_http.post(
            "/submit", content=body, headers=headers
        )
        submit_res.raise_for_status()
        submitted = submit_res.json()
        tx = await self._poll_relayer_tx(submitted["transactionID"])
        return {"deposit_wallet": deposit_wallet, "tx_hash": tx["transactionHash"]}

    async def fund_deposit_wallet(
        self, *, amount_raw: int
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            owner, sign_cb = self._require_signer()
            deposit_wallet = self.deposit_wallet_address()
            if amount_raw <= 0:
                return False, "amount must be positive"
            tx = await build_send_transaction(
                from_address=owner,
                to_address=deposit_wallet,
                token_address=POLYGON_P_USDC_PROXY_ADDRESS,
                chain_id=POLYGON_CHAIN_ID,
                amount=amount_raw,
            )
            tx_hash = await send_transaction(tx, sign_cb, confirmations=1)
            return True, {
                "deposit_wallet": deposit_wallet,
                "amount_raw": amount_raw,
                "tx_hash": tx_hash,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def withdraw_deposit_wallet(
        self, *, amount_raw: int | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            owner = self._require_wallet_address()
            deposit_wallet = self.deposit_wallet_address()
            balance: int = await get_token_balance(
                POLYGON_P_USDC_PROXY_ADDRESS, POLYGON_CHAIN_ID, deposit_wallet
            )
            if amount_raw is None:
                amount_raw = balance
            if amount_raw <= 0:
                return False, "nothing to withdraw"
            if amount_raw > balance:
                return (
                    False,
                    f"insufficient deposit wallet balance: have {balance}, requested {amount_raw}",
                )
            async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
                pusd = web3.eth.contract(
                    address=POLYGON_P_USDC_PROXY_ADDRESS, abi=ERC20_ABI
                )
                calls = [
                    {
                        "target": POLYGON_P_USDC_PROXY_ADDRESS,
                        "value": 0,
                        "data": pusd.encode_abi("transfer", [owner, amount_raw]),
                    }
                ]
            result = await self._submit_wallet_batch(calls=calls)
            return True, {**result, "amount_raw": amount_raw, "recipient": owner}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _setup_deposit_wallet(self) -> tuple[str | None, str | None]:
        owner = self._require_wallet_address()
        deposit_wallet = self.deposit_wallet_address()
        async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
            factory = web3.eth.contract(
                address=POLYMARKET_DEPOSIT_WALLET_FACTORY,
                abi=POLYMARKET_DEPOSIT_WALLET_FACTORY_ABI,
            )
            pusd = web3.eth.contract(
                address=POLYGON_P_USDC_PROXY_ADDRESS, abi=ERC20_ABI
            )
            ctf = web3.eth.contract(
                address=POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
                abi=CONDITIONAL_TOKENS_ABI,
            )
            predicted, code, allowances, approvals = await asyncio.gather(
                factory.functions.predictWalletAddress(
                    POLYMARKET_DEPOSIT_WALLET_IMPLEMENTATION,
                    polymarket_deposit_wallet_id(owner),
                ).call(block_identifier="latest"),
                web3.eth.get_code(deposit_wallet),
                asyncio.gather(
                    *[
                        pusd.functions.allowance(deposit_wallet, s).call(
                            block_identifier="latest"
                        )
                        for s in POLYMARKET_APPROVAL_TARGETS
                    ]
                ),
                asyncio.gather(
                    *[
                        ctf.functions.isApprovedForAll(deposit_wallet, o).call(
                            block_identifier="latest"
                        )
                        for o in POLYMARKET_APPROVAL_TARGETS
                    ]
                ),
            )
            if to_checksum_address(predicted) != deposit_wallet:
                raise ValueError(
                    "Deposit wallet derivation mismatch, this should never happen, please contact support."
                )

            deploy_tx_hash: str | None = None
            if not code:
                payload = {
                    "type": "WALLET-CREATE",
                    "from": owner,
                    "to": POLYMARKET_DEPOSIT_WALLET_FACTORY,
                }
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                headers = await self._builder_headers("POST", "/submit", body)
                res = await self._relayer_http.post(
                    "/submit", content=body, headers=headers
                )
                res.raise_for_status()
                deploy_tx = await self._poll_relayer_tx(res.json()["transactionID"])
                deploy_tx_hash = deploy_tx["transactionHash"]

            calls: list[dict[str, Any]] = []
            for spender, allowance in zip(
                POLYMARKET_APPROVAL_TARGETS, allowances, strict=True
            ):
                if allowance < MAX_UINT256 // 2:
                    calls.append(
                        {
                            "target": POLYGON_P_USDC_PROXY_ADDRESS,
                            "value": 0,
                            "data": pusd.encode_abi("approve", [spender, MAX_UINT256]),
                        }
                    )
            for operator, approved in zip(
                POLYMARKET_APPROVAL_TARGETS, approvals, strict=True
            ):
                if not approved:
                    calls.append(
                        {
                            "target": POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
                            "value": 0,
                            "data": ctf.encode_abi(
                                "setApprovalForAll", [operator, True]
                            ),
                        }
                    )

        approval_tx_hash: str | None = None
        if calls:
            approval = await self._submit_wallet_batch(calls=calls)
            approval_tx_hash = approval["tx_hash"]
        return deploy_tx_hash, approval_tx_hash

    async def ensure_trading_setup(
        self, *, token_id: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            if self._setup_complete:
                return True, {"deposit_wallet": self.deposit_wallet_address()}
            deploy_tx_hash, approval_tx_hash = await self._setup_deposit_wallet()
            ok_creds, msg = await self.ensure_api_creds()
            if not ok_creds:
                return False, msg
            self.clob_client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=SignatureTypeV2.POLY_1271,
                )
            )
            if token_id:
                self.clob_client.update_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                        signature_type=SignatureTypeV2.POLY_1271,
                    )
                )
            self._setup_complete = True
            return True, {
                "deposit_wallet": self.deposit_wallet_address(),
                "deploy_tx_hash": deploy_tx_hash,
                "approval_tx_hash": approval_tx_hash,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @property
    def clob_client(self) -> ClobClient:  # type: ignore[valid-type]
        if self._clob_client is None:
            self._clob_client = ClobClient(  # type: ignore[misc]
                str(self._clob_http.base_url),
                chain_id=POLYGON_CHAIN_ID,
                signature_type=SignatureTypeV2.POLY_1271,
                funder=self.deposit_wallet_address(),
                address_override=self._require_wallet_address(),
                sign_callback_override=self.sign_hash_callback,
            )
        return self._clob_client  # type: ignore[return-value]

    async def ensure_api_creds(self) -> tuple[bool, dict[str, Any] | str]:
        try:
            if self._api_creds_set:
                return True, {"ok": True}

            creds = await self.clob_client.create_or_derive_api_creds()
            self.clob_client.set_api_creds(creds)
            self._api_creds_set = True
            return True, {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def place_limit_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        price: float,
        size: float,
        post_only: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok_setup, setup = await self.ensure_trading_setup(token_id=token_id)
        if not ok_setup:
            return False, setup
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg
        try:
            order_args = OrderArgsV2(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
                builder_code=POLYMARKET_BUILDER_CODE,
            )  # type: ignore[misc]
            order = await self.clob_client.create_order(order_args)
            resp = self.clob_client.post_order(order, "GTC", post_only)
            out = resp if isinstance(resp, dict) else {"result": resp}
            out.setdefault("deposit_wallet", self.deposit_wallet_address())
            out.setdefault("setup", setup)
            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def place_market_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        amount: float,
        max_slippage_pct: float | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        # BUY amount = collateral ($) to spend, SELL amount = shares to sell.
        # Polymarket has no slippage param; canonical pattern (see py-clob-client-v2
        # MarketOrderArgsV2.price doc + create_market_order source) is to derive a
        # worst-acceptable price from the book and sign an FOK at that cap.
        ok_setup, setup = await self.ensure_trading_setup(token_id=token_id)
        if not ok_setup:
            return False, setup
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg

        ok_quote, quote = await self.quote_market_order(
            token_id=token_id, side=side, amount=amount
        )
        if not ok_quote:
            return False, quote
        if not quote["fully_fillable"]:
            return False, {"error": "insufficient book liquidity", "quote": quote}

        worst = Decimal(str(quote["worst_price"]))
        tick = Decimal(str(quote["book_meta"].get("tick_size") or "0.01"))
        pct = Decimal(
            str(
                self.DEFAULT_MAX_SLIPPAGE_PCT
                if max_slippage_pct is None
                else max_slippage_pct
            )
        )
        slip = pct / Decimal(100)
        if side == "BUY":
            raw = worst * (Decimal(1) + slip)
            cap = (raw / tick).quantize(Decimal(1), rounding=ROUND_CEILING) * tick
            cap = min(cap, Decimal(1) - tick)
        else:
            raw = worst * (Decimal(1) - slip)
            cap = (raw / tick).quantize(Decimal(1), rounding=ROUND_FLOOR) * tick
            cap = max(cap, tick)

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                side=side,
                amount=amount,
                price=float(cap),
                builder_code=POLYMARKET_BUILDER_CODE,
            )  # type: ignore[misc]
            order = await self.clob_client.create_market_order(order_args)
            resp = self.clob_client.post_order(order, order_args.order_type, False)
            out = resp if isinstance(resp, dict) else {"result": resp}
            out.setdefault("deposit_wallet", self.deposit_wallet_address())
            out.setdefault("setup", setup)
            out["quote"] = quote
            out["price_cap"] = float(cap)
            out["max_slippage_pct"] = float(pct)
            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def cancel_order(self, *, order_id: str) -> tuple[bool, dict[str, Any] | str]:
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg
        try:
            resp = self.clob_client.cancel_order(OrderPayload(orderID=order_id))
            return True, resp if isinstance(resp, dict) else {"result": resp}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def list_open_orders(
        self,
        *,
        token_id: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, str(msg)
        try:
            params = None
            if token_id:
                # CLOB uses `asset_id` for the outcome token id returned by Gamma `clobTokenIds`.
                params = OpenOrderParams(asset_id=token_id)  # type: ignore[misc]
            data = self.clob_client.get_open_orders(params)
            if isinstance(data, list):
                return True, data
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return True, data["data"]
            return True, []
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_orders: bool = True,
        include_activity: bool = False,
        activity_limit: int = 50,
        include_trades: bool = False,
        trades_limit: int = 50,
        positions_limit: int = 500,
        max_positions_pages: int = 10,
    ) -> tuple[bool, dict[str, Any]]:
        addr = to_checksum_address(account)
        out: dict[str, Any] = {
            "protocol": "polymarket",
            "chainId": POLYGON_CHAIN_ID,
            "account": addr,
            "positions": None,
            "positionsSummary": None,
            "pnl": None,
            "openOrders": None,
            "orders": None,
            "recentActivity": None,
            "recentTrades": None,
            "pusd_balance": None,
            "usdc_e_balance": None,
            "usdc_balance": None,
            "balances": None,
            "errors": {},
        }

        ok_any = False

        async def _fetch_all_positions() -> tuple[bool, list[dict[str, Any]] | str]:
            rows: list[dict[str, Any]] = []
            offset = 0
            for _ in range(max(1, max_positions_pages)):
                ok_page, page = await self.get_positions(
                    user=addr, limit=positions_limit, offset=offset
                )
                if not ok_page:
                    return False, page
                if not page:
                    break
                rows.extend(page)
                if len(page) < positions_limit:
                    break
                offset += positions_limit
            rows = [
                p
                for p in rows
                if not (p.get("redeemable") is True and p.get("curPrice") == 0)
            ]
            return True, rows

        async def _fetch_balances() -> tuple[bool, dict[str, Any] | str]:
            async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
                pusd = web3.eth.contract(
                    address=POLYGON_P_USDC_PROXY_ADDRESS,
                    abi=ERC20_ABI,
                )
                usdce = web3.eth.contract(
                    address=POLYGON_USDC_E_ADDRESS,
                    abi=ERC20_ABI,
                )
                usdc = web3.eth.contract(
                    address=POLYGON_USDC_ADDRESS,
                    abi=ERC20_ABI,
                )
                (
                    bal_pusd,
                    bal_usdce,
                    bal_usdc,
                ) = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=POLYGON_CHAIN_ID,
                    calls=[
                        Call(pusd, "balanceOf", args=(addr,), postprocess=int),
                        Call(usdce, "balanceOf", args=(addr,), postprocess=int),
                        Call(usdc, "balanceOf", args=(addr,), postprocess=int),
                    ],
                    block_identifier="pending",
                )
            return True, {
                "pusd": {
                    "address": POLYGON_P_USDC_PROXY_ADDRESS,
                    "decimals": 6,
                    "amount_base_units": bal_pusd,
                    "amount": bal_pusd / 1_000_000,
                },
                "usdc_e": {
                    "address": POLYGON_USDC_E_ADDRESS,
                    "decimals": 6,
                    "amount_base_units": bal_usdce,
                    "amount": bal_usdce / 1_000_000,
                },
                "usdc": {
                    "address": POLYGON_USDC_ADDRESS,
                    "decimals": 6,
                    "amount_base_units": bal_usdc,
                    "amount": bal_usdc / 1_000_000,
                },
            }

        async def _fetch_orders() -> tuple[bool, list[dict[str, Any]] | str]:
            # CLOB orders are tied to the deposit wallet, not the owner EOA.
            if self.deposit_wallet_address() != addr:
                return (
                    False,
                    "Open orders can only be fetched for the configured trading wallet (account mismatch).",
                )
            return await self.list_open_orders()

        coros: list[Any] = [_fetch_all_positions(), _fetch_balances()]
        if include_orders:
            coros.append(_fetch_orders())
        if include_activity:
            coros.append(self.get_activity(user=addr, limit=activity_limit, offset=0))
        if include_trades:
            coros.append(self.get_trades(user=addr, limit=trades_limit, offset=0))

        results = await asyncio.gather(*coros, return_exceptions=True)

        pos_result = results[0]
        if isinstance(pos_result, Exception):
            out["errors"]["positions"] = str(pos_result)
        else:
            pos_ok, positions = pos_result
            if pos_ok:
                ok_any = True
                out["positions"] = positions

                # Data API returns PnL values as strings or None
                def _pnl_float(x: Any) -> float:
                    return float(x) if x is not None else 0.0

                total_initial_value = sum(
                    _pnl_float(p.get("initialValue")) for p in positions
                )
                total_current_value = sum(
                    _pnl_float(p.get("currentValue")) for p in positions
                )
                total_cash_pnl = sum(_pnl_float(p.get("cashPnl")) for p in positions)
                total_realized_pnl = sum(
                    _pnl_float(p.get("realizedPnl")) for p in positions
                )

                total_percent_pnl: float | None = None
                if total_initial_value:
                    total_percent_pnl = (total_cash_pnl / total_initial_value) * 100.0

                redeemable_count = sum(
                    1 for p in positions if p.get("redeemable") is True
                )
                mergeable_count = sum(
                    1 for p in positions if p.get("mergeable") is True
                )
                negative_risk_count = sum(
                    1 for p in positions if p.get("negativeRisk") is True
                )

                out["positionsSummary"] = {
                    "count": len(positions),
                    "redeemableCount": redeemable_count,
                    "mergeableCount": mergeable_count,
                    "negativeRiskCount": negative_risk_count,
                }

                out["pnl"] = {
                    "totalInitialValue": total_initial_value,
                    "totalCurrentValue": total_current_value,
                    "totalCashPnl": total_cash_pnl,
                    "totalRealizedPnl": total_realized_pnl,
                    "totalUnrealizedPnl": total_cash_pnl - total_realized_pnl,
                    "totalPercentPnl": total_percent_pnl,
                }
            else:
                out["errors"]["positions"] = positions

        bal_result = results[1]
        if isinstance(bal_result, Exception):
            out["errors"]["balances"] = str(bal_result)
        else:
            bal_ok, bal_data = bal_result
            if bal_ok:
                ok_any = True
                out["balances"] = bal_data
                out["pusd_balance"] = bal_data["pusd"]["amount"]
                out["usdc_e_balance"] = bal_data["usdc_e"]["amount"]
                out["usdc_balance"] = bal_data["usdc"]["amount"]
            else:
                out["errors"]["balances"] = bal_data

        idx = 2
        if include_orders:
            ord_result = results[idx]
            idx += 1
            if isinstance(ord_result, Exception):
                out["errors"]["openOrders"] = str(ord_result)
            else:
                ord_ok, ord_data = ord_result
                if ord_ok:
                    ok_any = True
                    out["openOrders"] = ord_data
                    out["orders"] = ord_data
                else:
                    out["errors"]["openOrders"] = ord_data

        if include_activity:
            act_result = results[idx]
            idx += 1
            if isinstance(act_result, Exception):
                out["errors"]["recentActivity"] = str(act_result)
            else:
                act_ok, act_data = act_result
                if act_ok:
                    ok_any = True
                    out["recentActivity"] = act_data
                else:
                    out["errors"]["recentActivity"] = act_data

        if include_trades:
            tr_result = results[idx]
            if isinstance(tr_result, Exception):
                out["errors"]["recentTrades"] = str(tr_result)
            else:
                tr_ok, tr_data = tr_result
                if tr_ok:
                    ok_any = True
                    out["recentTrades"] = tr_data
                else:
                    out["errors"]["recentTrades"] = tr_data

        return ok_any, out

    @staticmethod
    def _b32(x: str | bytes | HexBytes) -> bytes:
        hb = HexBytes(x) if not isinstance(x, HexBytes) else x
        if len(hb) > 32:
            raise ValueError(f"bytes32 too long: {len(hb)}")
        return bytes(hb.rjust(32, b"\x00"))

    async def _compute_position_id(
        self,
        *,
        collateral: str,
        parent_collection_id: bytes,
        condition_id: bytes,
        index_set: int,
    ) -> int:
        ctf_addr = self._contract_addrs(neg_risk=False)["conditional_tokens"]
        async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
            ctf = web3.eth.contract(
                address=to_checksum_address(ctf_addr),
                abi=CONDITIONAL_TOKENS_ABI,
            )
            collection_id = await ctf.functions.getCollectionId(
                parent_collection_id,
                condition_id,
                index_set,
            ).call(block_identifier="pending")
            pos_id = await ctf.functions.getPositionId(
                to_checksum_address(collateral),
                collection_id,
            ).call(block_identifier="pending")
            return int(pos_id)

    async def _balance_of_position(self, *, holder: str, position_id: int) -> int:
        ctf_addr = self._contract_addrs(neg_risk=False)["conditional_tokens"]
        async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
            ctf = web3.eth.contract(
                address=to_checksum_address(ctf_addr),
                abi=CONDITIONAL_TOKENS_ABI,
            )
            bal = await ctf.functions.balanceOf(
                to_checksum_address(holder), position_id
            ).call(block_identifier="pending")
            return int(bal)

    async def _outcome_index_sets(self, *, condition_id: str) -> list[int]:
        try:
            res = await self._gamma_http.get(
                "/markets", params={"condition_ids": condition_id}
            )
            res.raise_for_status()
            data = res.json()
            if data:
                outcomes = json.loads(data[0].get("outcomes", "[]"))
                if len(outcomes) >= 2:
                    return [1 << i for i in range(len(outcomes))]
        except Exception:
            pass
        return [1, 2]

    def _is_rpc_log_limit_error(self, exc: Exception) -> bool:
        if isinstance(exc, ValueError) and exc.args:
            payload = exc.args[0]
            if isinstance(payload, dict) and int(payload.get("code", 0)) == -32005:
                return True
        return "query returned more than 10000 results" in str(exc).lower()

    async def _find_parent_collection_id(
        self,
        *,
        condition_id: bytes,
        stakeholder: str | None = None,
    ) -> bytes | None:
        ctf_addr = self._contract_addrs(neg_risk=False)["conditional_tokens"]
        async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
            latest = await web3.eth.block_number

            pos_split_sig = web3.keccak(
                text="PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)"
            )
            pos_merge_sig = web3.keccak(
                text="PositionsMerge(address,address,bytes32,bytes32,uint256[],uint256)"
            )
            cond_topic = HexBytes(condition_id).rjust(32, b"\x00")

            stakeholder_topic: HexBytes | None = None
            if stakeholder:
                try:
                    stakeholder_topic = HexBytes(stakeholder).rjust(32, b"\x00")
                except Exception:
                    stakeholder_topic = None

            end = int(latest)
            step = 10_000  # Polygon RPCs cap at 10k results per query
            min_step = 500
            max_back = 4_000_000
            scanned = 0

            while scanned <= max_back and end > 0:
                start = max(0, end - step)
                split_logs: list[dict[str, Any]] = []
                merge_logs: list[dict[str, Any]] = []

                too_many = False
                try:
                    split_logs = await web3.eth.get_logs(
                        {
                            "fromBlock": start,
                            "toBlock": end,
                            "address": to_checksum_address(ctf_addr),
                            "topics": [
                                pos_split_sig,
                                stakeholder_topic,
                                None,
                                cond_topic,
                            ],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    too_many = self._is_rpc_log_limit_error(exc)

                try:
                    merge_logs = await web3.eth.get_logs(
                        {
                            "fromBlock": start,
                            "toBlock": end,
                            "address": to_checksum_address(ctf_addr),
                            "topics": [
                                pos_merge_sig,
                                stakeholder_topic,
                                None,
                                cond_topic,
                            ],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    too_many = too_many or self._is_rpc_log_limit_error(exc)

                if too_many:
                    if step <= min_step:
                        return None
                    step = max(min_step, step // 2)
                    continue

                for logs in (split_logs, merge_logs):
                    if logs:
                        parent = HexBytes(logs[-1]["topics"][2]).rjust(32, b"\x00")
                        if parent.hex() != HexBytes(ZERO32_STR).hex():
                            return bytes(parent)

                scanned += end - start
                end = start

        return None

    async def preflight_redeem(
        self,
        *,
        condition_id: str,
        holder: str,
        candidate_collaterals: list[str] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        holder = to_checksum_address(holder)
        cond_b32 = self._b32(condition_id)

        collaterals = candidate_collaterals or [
            POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
            POLYGON_P_USDC_PROXY_ADDRESS,
            POLYGON_USDC_ADDRESS,
            POLYGON_USDC_E_ADDRESS,
        ]

        index_sets = await self._outcome_index_sets(condition_id=condition_id)

        async def _try_parent(parent: bytes) -> tuple[bool, dict[str, Any] | str]:
            ctf_addr = self._contract_addrs(neg_risk=False)["conditional_tokens"]
            async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
                ctf = web3.eth.contract(
                    address=to_checksum_address(ctf_addr),
                    abi=CONDITIONAL_TOKENS_ABI,
                )

                for collateral in collaterals:
                    collateral_cs = to_checksum_address(collateral)

                    # getCollectionId -> getPositionId -> balanceOf; all are pure/view reads,
                    # but Multicall3 may not be available on every RPC/network. The helper
                    # automatically falls back to `asyncio.gather` if multicall isn't supported.
                    collection_ids = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=POLYGON_CHAIN_ID,
                        calls=[
                            Call(
                                ctf,
                                "getCollectionId",
                                args=(parent, cond_b32, int(i)),
                            )
                            for i in index_sets
                        ],
                        block_identifier="pending",
                        chunk_size=32,
                    )

                    pos_ids = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=POLYGON_CHAIN_ID,
                        calls=[
                            Call(
                                ctf,
                                "getPositionId",
                                args=(collateral_cs, collection_id),
                                postprocess=int,
                            )
                            for collection_id in collection_ids
                        ],
                        block_identifier="pending",
                        chunk_size=32,
                    )

                    bals = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=POLYGON_CHAIN_ID,
                        calls=[
                            Call(
                                ctf,
                                "balanceOf",
                                args=(holder, int(pid)),
                                postprocess=int,
                            )
                            for pid in pos_ids
                        ],
                        block_identifier="pending",
                        chunk_size=64,
                    )

                    redeemable = [
                        i for i, b in zip(index_sets, bals, strict=False) if int(b) > 0
                    ]
                    if redeemable:
                        return True, {
                            "collateral": collateral_cs,
                            "parentCollectionId": "0x" + parent.hex(),
                            "conditionId": "0x" + cond_b32.hex(),
                            "indexSets": redeemable,
                        }
            return (
                False,
                "No redeemable balance detected for the provided condition_id.",
            )

        # Most markets redeem with parentCollectionId = 0x0. Avoid expensive log scans unless needed.
        ok, path = await _try_parent(self._b32(ZERO32_STR))
        if ok:
            return True, path

        try:
            parent_nz = await self._find_parent_collection_id(
                condition_id=cond_b32, stakeholder=holder
            )
        except Exception:  # noqa: BLE001
            parent_nz = None
        if parent_nz:
            ok, path = await _try_parent(parent_nz)
            if ok:
                return True, path

        return False, "No redeemable balance detected for the provided condition_id."

    async def redeem_positions(
        self, *, condition_id: str
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            deposit_wallet = self.deposit_wallet_address()
            ok, path = await self.preflight_redeem(
                condition_id=condition_id, holder=deposit_wallet
            )
            if not ok:
                return False, path

            collateral = path["collateral"]
            parent = path["parentCollectionId"]
            cond = path["conditionId"]
            index_sets = path["indexSets"]

            pusd_before, usdce_before = await asyncio.gather(
                get_token_balance(
                    POLYGON_P_USDC_PROXY_ADDRESS,
                    POLYGON_CHAIN_ID,
                    deposit_wallet,
                    block_identifier="latest",
                ),
                get_token_balance(
                    POLYGON_USDC_E_ADDRESS,
                    POLYGON_CHAIN_ID,
                    deposit_wallet,
                    block_identifier="latest",
                ),
            )

            async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
                ctf = web3.eth.contract(
                    address=POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
                    abi=CONDITIONAL_TOKENS_ABI,
                )
                redeem_data = ctf.encode_abi(
                    "redeemPositions", [collateral, parent, cond, index_sets]
                )
            redeem = await self._submit_wallet_batch(
                calls=[
                    {
                        "target": POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
                        "value": 0,
                        "data": redeem_data,
                    }
                ]
            )

            unwrap_tx_hash: str | None = None
            if to_checksum_address(collateral) == to_checksum_address(
                POLYMARKET_ADAPTER_COLLATERAL_ADDRESS
            ):
                shares = await get_token_balance(
                    POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
                    POLYGON_CHAIN_ID,
                    deposit_wallet,
                )
                if shares > 0:
                    async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
                        wrapper = web3.eth.contract(
                            address=POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
                            abi=TOKEN_UNWRAP_ABI,
                        )
                        unwrap_data = wrapper.encode_abi(
                            "unwrap", [deposit_wallet, shares]
                        )
                    unwrap = await self._submit_wallet_batch(
                        calls=[
                            {
                                "target": POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
                                "value": 0,
                                "data": unwrap_data,
                            }
                        ]
                    )
                    unwrap_tx_hash = unwrap["tx_hash"]

            # Poll for the redemption's payout to land on the public RPC —
            # the relayer reports MINED ahead of public-node propagation.
            # Bounded at 5s; whichever side increases tells us how the market
            # settled (pUSD direct → done, USDC.e → wrap the delta).
            wrap_tx_hash: str | None = None
            wrap_error: str | None = None
            usdce_to_wrap = 0
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                pusd_now, usdce_now = await asyncio.gather(
                    get_token_balance(
                        POLYGON_P_USDC_PROXY_ADDRESS,
                        POLYGON_CHAIN_ID,
                        deposit_wallet,
                        block_identifier="latest",
                    ),
                    get_token_balance(
                        POLYGON_USDC_E_ADDRESS,
                        POLYGON_CHAIN_ID,
                        deposit_wallet,
                        block_identifier="latest",
                    ),
                )
                if pusd_now > pusd_before or usdce_now > usdce_before:
                    usdce_to_wrap = usdce_now
                    break
                await asyncio.sleep(0.25)

            if usdce_to_wrap > 0:
                try:
                    wrap_tx_hash = await self._wrap_deposit_wallet_usdce_to_pusd(
                        amount_base_unit=usdce_to_wrap,
                    )
                except Exception as wrap_exc:  # noqa: BLE001
                    wrap_error = str(wrap_exc)

            return True, {
                "deposit_wallet": deposit_wallet,
                "tx_hash": redeem["tx_hash"],
                "unwrap_tx_hash": unwrap_tx_hash,
                "wrap_tx_hash": wrap_tx_hash,
                "wrap_error": wrap_error,
                "path": path,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
