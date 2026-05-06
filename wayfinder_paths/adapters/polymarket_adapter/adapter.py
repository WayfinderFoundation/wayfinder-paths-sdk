from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Literal

import httpx
from eth_utils import to_checksum_address
from hexbytes import HexBytes
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

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.config import get_polygon_builder_code
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.polymarket import (
    CONDITIONAL_TOKENS_ABI,
    MAX_UINT256,
    POLYGON_CHAIN_ID,
    POLYGON_P_USDC_PROXY_ADDRESS,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
    POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
    POLYMARKET_BRIDGE_BASE_URL,
    POLYMARKET_CLOB_BASE_URL,
    POLYMARKET_COLLATERAL_OFFRAMP_ADDRESS,
    POLYMARKET_COLLATERAL_ONRAMP_ADDRESS,
    POLYMARKET_COLLATERAL_RAMP_ABI,
    POLYMARKET_DATA_BASE_URL,
    POLYMARKET_GAMMA_BASE_URL,
    POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS,
    TOKEN_UNWRAP_ABI,
    ZERO32_STR,
)
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import (
    build_send_transaction,
    ensure_allowance,
    ensure_erc1155_approval,
    get_token_balance,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
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


async def _try_brap_swap_polygon(
    *,
    from_token_address: str,
    to_token_address: str,
    from_address: str,
    amount_base_unit: int,
    signing_callback,
) -> dict[str, Any] | None:
    """Try to swap on Polygon via BRAP and return a result payload, or None.

    Notes:
    - Only used for USDC ⇄ USDC.e conversions on Polygon.
    - Any failure returns None so callers can fall back to the Polymarket bridge flow.
    """
    try:
        quote = await BRAP_CLIENT.get_quote(
            from_token=from_token_address,
            to_token=to_token_address,
            from_chain=POLYGON_CHAIN_ID,
            to_chain=POLYGON_CHAIN_ID,
            from_wallet=to_checksum_address(from_address),
            from_amount=str(amount_base_unit),
        )
        best = quote["best_quote"]
        calldata = best["calldata"]
        if not calldata.get("data") or not calldata.get("to"):
            return None

        tx: dict[str, Any] = {
            **calldata,
            "chainId": POLYGON_CHAIN_ID,
            "from": to_checksum_address(from_address),
        }
        if "value" in tx:
            tx["value"] = int(tx["value"])

        approve_tx_hash: str | None = None
        spender = tx.get("to")
        if spender:
            ok_appr, appr = await ensure_allowance(
                token_address=from_token_address,
                owner=to_checksum_address(from_address),
                spender=str(spender),
                amount=int(best["input_amount"]),
                chain_id=POLYGON_CHAIN_ID,
                signing_callback=signing_callback,
            )
            if not ok_appr:
                return None
            if isinstance(appr, str) and appr.startswith("0x"):
                approve_tx_hash = appr

        swap_tx_hash = await send_transaction(tx, signing_callback)
        return {
            "method": "brap",
            "tx_hash": swap_tx_hash,
            "approve_tx_hash": approve_tx_hash,
            "from_chain_id": POLYGON_CHAIN_ID,
            "from_token_address": from_token_address,
            "to_chain_id": POLYGON_CHAIN_ID,
            "to_token_address": to_token_address,
            "amount_base_unit": str(amount_base_unit),
            "provider": best.get("provider"),
            "input_amount": best.get("input_amount"),
            "output_amount": best.get("output_amount"),
            "fee_estimate": best.get("fee_estimate"),
        }
    except Exception:
        return None


async def _wrap_usdce_to_pusd(
    *,
    owner_address: str,
    recipient_address: str,
    amount_base_unit: int,
    signing_callback,
) -> tuple[bool, dict[str, Any] | str]:
    """Wrap Polygon USDC.e into pUSD via the Polymarket CollateralOnramp."""
    try:
        recipient = to_checksum_address(recipient_address)
        approve_tx_hash: str | None = None
        ok_appr, appr = await ensure_allowance(
            token_address=POLYGON_USDC_E_ADDRESS,
            owner=owner_address,
            spender=POLYMARKET_COLLATERAL_ONRAMP_ADDRESS,
            amount=amount_base_unit,
            chain_id=POLYGON_CHAIN_ID,
            signing_callback=signing_callback,
        )
        if not ok_appr:
            return False, str(appr)
        if isinstance(appr, str) and appr.startswith("0x"):
            approve_tx_hash = appr

        tx = await encode_call(
            target=POLYMARKET_COLLATERAL_ONRAMP_ADDRESS,
            abi=POLYMARKET_COLLATERAL_RAMP_ABI,
            fn_name="wrap",
            args=[
                POLYGON_USDC_E_ADDRESS,
                recipient,
                amount_base_unit,
            ],
            from_address=owner_address,
            chain_id=POLYGON_CHAIN_ID,
        )
        tx_hash = await send_transaction(tx, signing_callback)
        return True, {
            "method": "pusd_wrap",
            "tx_hash": tx_hash,
            "approve_tx_hash": approve_tx_hash,
            "from_chain_id": POLYGON_CHAIN_ID,
            "from_token_address": POLYGON_USDC_E_ADDRESS,
            "to_chain_id": POLYGON_CHAIN_ID,
            "to_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
            "amount_base_unit": str(amount_base_unit),
            "recipient_address": recipient,
        }
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def _unwrap_pusd_to_usdce(
    *,
    owner_address: str,
    recipient_address: str,
    amount_base_unit: int,
    signing_callback,
) -> tuple[bool, dict[str, Any] | str]:
    """Unwrap Polygon pUSD into USDC.e via the Polymarket CollateralOfframp."""
    try:
        recipient = to_checksum_address(recipient_address)
        approve_tx_hash: str | None = None
        ok_appr, appr = await ensure_allowance(
            token_address=POLYGON_P_USDC_PROXY_ADDRESS,
            owner=owner_address,
            spender=POLYMARKET_COLLATERAL_OFFRAMP_ADDRESS,
            amount=amount_base_unit,
            chain_id=POLYGON_CHAIN_ID,
            signing_callback=signing_callback,
        )
        if not ok_appr:
            return False, str(appr)
        if isinstance(appr, str) and appr.startswith("0x"):
            approve_tx_hash = appr

        tx = await encode_call(
            target=POLYMARKET_COLLATERAL_OFFRAMP_ADDRESS,
            abi=POLYMARKET_COLLATERAL_RAMP_ABI,
            fn_name="unwrap",
            args=[
                POLYGON_USDC_E_ADDRESS,
                recipient,
                amount_base_unit,
            ],
            from_address=owner_address,
            chain_id=POLYGON_CHAIN_ID,
        )
        tx_hash = await send_transaction(tx, signing_callback)
        return True, {
            "method": "pusd_unwrap",
            "tx_hash": tx_hash,
            "approve_tx_hash": approve_tx_hash,
            "from_chain_id": POLYGON_CHAIN_ID,
            "from_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
            "to_chain_id": POLYGON_CHAIN_ID,
            "to_token_address": POLYGON_USDC_E_ADDRESS,
            "amount_base_unit": str(amount_base_unit),
            "recipient_address": recipient,
        }
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


class PolymarketAdapter(BaseAdapter):
    adapter_type = "POLYMARKET"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback=None,
        sign_hash_callback=None,
        wallet_address: str | None = None,
        funder: str | None = None,
        signature_type: int | None = None,
        gamma_base_url: str = POLYMARKET_GAMMA_BASE_URL,
        clob_base_url: str = POLYMARKET_CLOB_BASE_URL,
        data_base_url: str = POLYMARKET_DATA_BASE_URL,
        bridge_base_url: str = POLYMARKET_BRIDGE_BASE_URL,
        http_timeout_s: float = 30.0,
    ) -> None:
        super().__init__("polymarket_adapter", config)

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self.sign_callback = sign_callback
        self.sign_hash_callback = sign_hash_callback
        self._funder_override = funder
        self._signature_type = signature_type

        timeout = httpx.Timeout(http_timeout_s)
        self._gamma_http = httpx.AsyncClient(base_url=gamma_base_url, timeout=timeout)
        self._clob_http = httpx.AsyncClient(base_url=clob_base_url, timeout=timeout)
        self._data_http = httpx.AsyncClient(base_url=data_base_url, timeout=timeout)
        self._bridge_http = httpx.AsyncClient(base_url=bridge_base_url, timeout=timeout)

        self._clob_client: ClobClient | None = None  # type: ignore[valid-type]
        self._api_creds_set = False

    async def close(self) -> None:
        await asyncio.gather(
            self._gamma_http.aclose(),
            self._clob_http.aclose(),
            self._data_http.aclose(),
            self._bridge_http.aclose(),
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
        )

    async def cash_out_prediction(
        self,
        *,
        market_slug: str,
        outcome: str | int = "YES",
        shares: float = 1.0,
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

    async def bridge_supported_assets(self) -> tuple[bool, dict[str, Any] | str]:
        try:
            res = await self._bridge_http.get("/supported-assets")
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                return (
                    False,
                    f"Unexpected /supported-assets response: {type(data).__name__}",
                )
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

        Direct fast paths implemented here:
        - USDC.e -> pUSD via the Polymarket CollateralOnramp
        - Polygon native USDC -> USDC.e via BRAP, then wrap to pUSD

        Other supported source tokens / chains are not normalized here via an
        arbitrary BRAP route. They currently fall back to the async Polymarket
        Bridge deposit-address flow from `from_chain_id`, which lands as
        pUSD on Polygon.
        """
        from_address, sign_cb = self._require_signer()
        from_token = to_checksum_address(from_token_address)
        base_units = to_erc20_raw(amount, token_decimals)

        rcpt = to_checksum_address(recipient_address)
        usdce_balance_before_swap: int | None = None
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
                    acct = from_address
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
                            Call(pusd, "balanceOf", args=(acct,), postprocess=int),
                            Call(usdce, "balanceOf", args=(acct,), postprocess=int),
                            Call(usdc, "balanceOf", args=(acct,), postprocess=int),
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

            if from_chain_id == POLYGON_CHAIN_ID and from_token == POLYGON_USDC_ADDRESS:
                usdce_balance_before_swap = await get_token_balance(
                    POLYGON_USDC_E_ADDRESS,
                    POLYGON_CHAIN_ID,
                    from_address,
                    web3=web3,
                    block_identifier="pending",
                )

        if from_chain_id == POLYGON_CHAIN_ID and from_token == POLYGON_USDC_E_ADDRESS:
            return await _wrap_usdce_to_pusd(
                owner_address=from_address,
                recipient_address=rcpt,
                amount_base_unit=base_units,
                signing_callback=sign_cb,
            )

        if from_chain_id == POLYGON_CHAIN_ID and from_token == POLYGON_USDC_ADDRESS:
            brap = await _try_brap_swap_polygon(
                from_token_address=from_token,
                to_token_address=POLYGON_USDC_E_ADDRESS,
                from_address=from_address,
                amount_base_unit=base_units,
                signing_callback=sign_cb,
            )
            if brap:
                usdce_balance_after_swap = await get_token_balance(
                    POLYGON_USDC_E_ADDRESS,
                    POLYGON_CHAIN_ID,
                    from_address,
                    block_identifier="pending",
                )
                wrapped_amount = max(
                    0,
                    usdce_balance_after_swap - (usdce_balance_before_swap or 0),
                )
                if wrapped_amount <= 0:
                    return False, (
                        "BRAP swap completed, but no new USDC.e balance was detected "
                        "for wrapping into pUSD."
                    )

                ok_wrap, wrap = await _wrap_usdce_to_pusd(
                    owner_address=from_address,
                    recipient_address=rcpt,
                    amount_base_unit=wrapped_amount,
                    signing_callback=sign_cb,
                )
                if not ok_wrap:
                    return False, wrap
                return True, {
                    "method": "brap_then_wrap",
                    "tx_hash": wrap.get("tx_hash"),
                    "approve_tx_hash": brap.get("approve_tx_hash"),
                    "from_chain_id": POLYGON_CHAIN_ID,
                    "from_token_address": from_token,
                    "to_chain_id": POLYGON_CHAIN_ID,
                    "to_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
                    "amount_base_unit": str(base_units),
                    "recipient_address": rcpt,
                    "provider": brap.get("provider"),
                    "input_amount": brap.get("input_amount"),
                    "output_amount": brap.get("output_amount"),
                    "fee_estimate": brap.get("fee_estimate"),
                    "swap_tx_hash": brap.get("tx_hash"),
                    "swap_approve_tx_hash": brap.get("approve_tx_hash"),
                    "wrap_tx_hash": wrap.get("tx_hash"),
                    "wrap_approve_tx_hash": wrap.get("approve_tx_hash"),
                    "swap": brap,
                    "wrap": wrap,
                }

        ok_addr, addr_data = await self.bridge_deposit_addresses(address=rcpt)
        if not ok_addr:
            return False, addr_data

        deposit_evm = (addr_data.get("address") or {}).get("evm")
        if not deposit_evm:
            return False, "Bridge did not return an EVM deposit address"

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

        Direct fast paths implemented here:
        - pUSD -> USDC.e via the Polymarket CollateralOfframp
        - pUSD -> USDC.e -> Polygon native USDC via BRAP

        For other destination assets / chains, the adapter unwraps to USDC.e
        and then falls back to the async Polymarket bridge withdraw-address
        flow.
        """
        from_address, sign_cb = self._require_signer()
        base_units = to_erc20_raw(amount_pusd, token_decimals)

        rcpt = to_checksum_address(recipient_addr)
        ok_unwrap, unwrap = await _unwrap_pusd_to_usdce(
            owner_address=from_address,
            recipient_address=from_address,
            amount_base_unit=base_units,
            signing_callback=sign_cb,
        )
        if not ok_unwrap:
            return False, unwrap

        if (
            to_chain_id == POLYGON_CHAIN_ID
            and to_checksum_address(to_token_address) == POLYGON_USDC_E_ADDRESS
            and rcpt == from_address
        ):
            return True, {**unwrap, "recipient_addr": rcpt}

        if (
            to_chain_id == POLYGON_CHAIN_ID
            and to_checksum_address(to_token_address) == POLYGON_USDC_ADDRESS
            and rcpt == from_address
        ):
            brap = await _try_brap_swap_polygon(
                from_token_address=POLYGON_USDC_E_ADDRESS,
                to_token_address=to_token_address,
                from_address=from_address,
                amount_base_unit=base_units,
                signing_callback=sign_cb,
            )
            if brap:
                return True, {
                    "method": "unwrap_then_brap",
                    "tx_hash": brap.get("tx_hash"),
                    "approve_tx_hash": brap.get("approve_tx_hash"),
                    "from_chain_id": POLYGON_CHAIN_ID,
                    "from_token_address": POLYGON_P_USDC_PROXY_ADDRESS,
                    "to_chain_id": POLYGON_CHAIN_ID,
                    "to_token_address": to_token_address,
                    "amount_base_unit": str(base_units),
                    "recipient_addr": rcpt,
                    "provider": brap.get("provider"),
                    "input_amount": brap.get("input_amount"),
                    "output_amount": brap.get("output_amount"),
                    "fee_estimate": brap.get("fee_estimate"),
                    "unwrap_tx_hash": unwrap.get("tx_hash"),
                    "swap_tx_hash": brap.get("tx_hash"),
                    "swap_approve_tx_hash": brap.get("approve_tx_hash"),
                    "unwrap": unwrap,
                    "swap": brap,
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
            "unwrap": unwrap,
        }

    def _require_wallet_address(self) -> str:
        if not self.wallet_address:
            raise ValueError(
                "wallet_address is required. Use get_adapter(PolymarketAdapter, wallet_label)."
            )
        return self.wallet_address

    def _require_funder(self) -> str:
        if self._funder_override:
            return to_checksum_address(self._funder_override)
        return self._require_wallet_address()

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

    def _builder_code(self) -> str | None:
        system = self.config.get("system", {})
        if isinstance(system, dict):
            builder_code = system.get("polymarket_builder_code")
            if isinstance(builder_code, str):
                builder_code = builder_code.strip()
                if builder_code:
                    return builder_code
        return get_polygon_builder_code()

    @property
    def clob_client(self) -> ClobClient:  # type: ignore[valid-type]
        if self._clob_client is None:
            addr = self._require_wallet_address()
            funder = self._require_funder()
            self._clob_client = ClobClient(  # type: ignore[misc]
                str(self._clob_http.base_url),
                chain_id=POLYGON_CHAIN_ID,
                key="0x" + "00" * 32,
                signature_type=self._signature_type,
                funder=funder,
                address_override=addr,
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

    async def ensure_onchain_approvals(self) -> tuple[bool, dict[str, Any] | str]:
        from_address, sign_cb = self._require_signer()

        cfg = self._contract_addrs(neg_risk=False)
        cfg_nr = self._contract_addrs(neg_risk=True)

        exchanges: set[str] = {
            cfg["exchange"],
            cfg_nr["exchange"],
            POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS,
        }
        collateral = cfg["collateral"]
        conditional_tokens = cfg["conditional_tokens"]

        spenders = set(exchanges) | {conditional_tokens}

        txs: list[str] = []
        for spender in sorted(spenders):
            ok, res = await ensure_allowance(
                token_address=collateral,
                owner=from_address,
                spender=spender,
                amount=MAX_UINT256 // 2,
                chain_id=POLYGON_CHAIN_ID,
                signing_callback=sign_cb,
                approval_amount=MAX_UINT256,
            )
            if not ok:
                return False, res
            if isinstance(res, str) and res.startswith("0x"):
                txs.append(res)

        for operator in sorted(exchanges):
            ok, res = await ensure_erc1155_approval(
                token_address=conditional_tokens,
                owner=from_address,
                operator=operator,
                approved=True,
                chain_id=POLYGON_CHAIN_ID,
                signing_callback=sign_cb,
            )
            if not ok:
                return False, res
            if isinstance(res, str) and res.startswith("0x"):
                txs.append(res)

        return True, {
            "tx_hashes": txs,
            "collateral": collateral,
            "ctf": conditional_tokens,
            "exchanges": sorted(exchanges),
        }

    async def place_limit_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        price: float,
        size: float,
        post_only: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok_appr, appr = await self.ensure_onchain_approvals()
        if not ok_appr:
            return False, appr
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg
        try:
            builder_code = self._builder_code()
            order_kwargs: dict[str, Any] = {
                "token_id": token_id,
                "price": price,
                "size": size,
                "side": side,
            }
            if builder_code:
                order_kwargs["builder_code"] = builder_code

            order_args = OrderArgsV2(
                **order_kwargs,
            )  # type: ignore[misc]
            order = await self.clob_client.create_order(order_args)
            resp = self.clob_client.post_order(order, "GTC", post_only)
            return True, resp if isinstance(resp, dict) else {"result": resp}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def place_market_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        amount: float,
        price: float | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        # BUY amount = collateral ($) to spend, SELL amount = shares to sell
        ok_appr, appr = await self.ensure_onchain_approvals()
        if not ok_appr:
            return False, appr
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg

        try:
            builder_code = self._builder_code()
            order_kwargs: dict[str, Any] = {
                "token_id": token_id,
                "side": side,
                "amount": amount,
                "price": price or 0.0,
            }
            if builder_code:
                order_kwargs["builder_code"] = builder_code

            order_args = MarketOrderArgs(**order_kwargs)  # type: ignore[misc]
            order = await self.clob_client.create_market_order(order_args)
            resp = self.clob_client.post_order(order, order_args.order_type, False)
            return True, resp if isinstance(resp, dict) else {"result": resp}
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

    async def get_builder_trades(
        self,
        *,
        builder_code: str | None = None,
        trade_id: str | None = None,
        maker_address: str | None = None,
        market: str | None = None,
        asset_id: str | None = None,
        before: str | None = None,
        after: str | None = None,
        next_cursor: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        effective_builder_code = builder_code or self._builder_code()
        if not effective_builder_code:
            return False, "builder_code is required"

        try:
            params: dict[str, str] = {"builder_code": effective_builder_code}
            if trade_id:
                params["id"] = trade_id
            if maker_address:
                params["maker_address"] = maker_address
            if market:
                params["market"] = market
            if asset_id:
                params["asset_id"] = asset_id
            if before:
                params["before"] = before
            if after:
                params["after"] = after
            if next_cursor:
                params["next_cursor"] = next_cursor
            res = await self._clob_http.get("/builder/trades", params=params)
            res.raise_for_status()
            data = res.json()
            return True, {
                "trades": list(data.get("data", [])),
                "next_cursor": data.get("next_cursor"),
                "limit": data.get("limit"),
                "count": data.get("count"),
            }
        except Exception as exc:
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
            # CLOB requires Level-2 auth; only works for the configured signing wallet.
            signer_addr = self._require_funder()
            if to_checksum_address(signer_addr) != addr:
                return (
                    False,
                    "Open orders can only be fetched for the configured signing wallet (account mismatch).",
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
        self,
        *,
        condition_id: str,
        holder: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        holder_addr, sign_cb = self._require_signer()
        if holder and to_checksum_address(holder) != holder_addr:
            return False, "holder must match the configured signing wallet"

        ok, path = await self.preflight_redeem(
            condition_id=condition_id, holder=holder_addr
        )
        if not ok:
            return False, path

        collateral = path["collateral"]
        parent = path["parentCollectionId"]
        cond = path["conditionId"]
        index_sets = path["indexSets"]

        tx = await encode_call(
            target=self._contract_addrs(neg_risk=False)["conditional_tokens"],
            abi=CONDITIONAL_TOKENS_ABI,
            fn_name="redeemPositions",
            args=[collateral, parent, cond, index_sets],
            from_address=holder_addr,
            chain_id=POLYGON_CHAIN_ID,
        )
        tx_hash = await send_transaction(tx, sign_cb)

        if to_checksum_address(collateral) == to_checksum_address(
            POLYMARKET_ADAPTER_COLLATERAL_ADDRESS
        ):
            shares = await get_token_balance(
                POLYMARKET_ADAPTER_COLLATERAL_ADDRESS, POLYGON_CHAIN_ID, holder_addr
            )
            if shares > 0:
                unwrap_tx = await encode_call(
                    target=POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
                    abi=TOKEN_UNWRAP_ABI,
                    fn_name="unwrap",
                    args=[holder_addr, shares],
                    from_address=holder_addr,
                    chain_id=POLYGON_CHAIN_ID,
                )
                await send_transaction(unwrap_tx, sign_cb)

        return True, {"tx_hash": tx_hash, "path": path}
