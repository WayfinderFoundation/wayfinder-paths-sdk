from __future__ import annotations

import asyncio
import json
import re
from difflib import SequenceMatcher
from typing import Any, Literal

import httpx
from eth_account import Account
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from py_clob_client.client import ClobClient  # type: ignore[import-untyped]
from py_clob_client.clob_types import (  # type: ignore[import-untyped]
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
)
from py_clob_client.config import (  # type: ignore[import-untyped]
    get_contract_config,
)

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.constants.polymarket import (
    CONDITIONAL_TOKENS_ABI,
    MAX_UINT256,
    POLYGON_CHAIN_ID,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
    POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
    POLYMARKET_BRIDGE_BASE_URL,
    POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS,
    POLYMARKET_CLOB_BASE_URL,
    POLYMARKET_DATA_BASE_URL,
    POLYMARKET_GAMMA_BASE_URL,
    TOKEN_UNWRAP_ABI,
    ZERO32_STR,
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


class PolymarketAdapter(BaseAdapter):
    adapter_type = "POLYMARKET"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        strategy_wallet_signing_callback=None,
        private_key_hex: str | None = None,
        funder: str | None = None,
        signature_type: int | None = None,
        gamma_base_url: str = POLYMARKET_GAMMA_BASE_URL,
        clob_base_url: str = POLYMARKET_CLOB_BASE_URL,
        data_base_url: str = POLYMARKET_DATA_BASE_URL,
        bridge_base_url: str = POLYMARKET_BRIDGE_BASE_URL,
        http_timeout_s: float = 30.0,
    ) -> None:
        super().__init__("polymarket_adapter", config)

        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback
        self._private_key_hex = private_key_hex
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
        rerank: bool = True,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, data = await self.public_search(
            q=query,
            limit_per_type=max(limit, 1),
            page=page,
            keep_closed_markets=keep_closed_markets,
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

    async def place_prediction(
        self,
        *,
        market_slug: str,
        outcome: str | int = "YES",
        amount_usdc: float = 1.0,
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
            amount=amount_usdc,
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
        to_token_address: str = POLYGON_USDC_E_ADDRESS,
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
        """Convert USDC → USDC.e.

        Preferred path (fast, on-chain): BRAP swap on Polygon, when possible.
        Fallback (async, bridge service): Polymarket Bridge deposit address transfer.
        """
        from_address, sign_cb = self._resolve_wallet_signer()
        base_units = to_erc20_raw(amount, token_decimals)

        rcpt = to_checksum_address(recipient_address)
        if (
            from_chain_id == POLYGON_CHAIN_ID
            and to_checksum_address(from_token_address)
            == to_checksum_address(POLYGON_USDC_ADDRESS)
            and rcpt == to_checksum_address(from_address)
        ):
            brap = await _try_brap_swap_polygon(
                from_token_address=from_token_address,
                to_token_address=POLYGON_USDC_E_ADDRESS,
                from_address=from_address,
                amount_base_unit=base_units,
                signing_callback=sign_cb,
            )
            if brap:
                return True, {**brap, "recipient_address": rcpt}

        ok_addr, addr_data = await self.bridge_deposit_addresses(
            address=recipient_address
        )
        if not ok_addr:
            return False, addr_data

        deposit_evm = (addr_data.get("address") or {}).get("evm")
        if not deposit_evm:
            return False, "Bridge did not return an EVM deposit address"

        tx = await build_send_transaction(
            from_address=from_address,
            to_address=str(deposit_evm),
            token_address=from_token_address,
            chain_id=from_chain_id,
            amount=base_units,
        )
        tx_hash = await send_transaction(tx, sign_cb)

        return True, {
            "method": "polymarket_bridge",
            "tx_hash": tx_hash,
            "from_chain_id": from_chain_id,
            "from_token_address": from_token_address,
            "deposit_address": str(deposit_evm),
            "amount_base_unit": str(base_units),
            "recipient_address": to_checksum_address(recipient_address),
        }

    async def bridge_withdraw(
        self,
        *,
        amount_usdce: str | float,
        to_chain_id: int | str,
        to_token_address: str,
        recipient_addr: str,
        token_decimals: int = 6,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Convert USDC.e → destination token.

        Preferred path (fast, on-chain): BRAP swap USDC.e -> USDC on Polygon, when possible.
        Fallback (async, bridge service): Polymarket Bridge withdraw address transfer.
        """
        from_address, sign_cb = self._resolve_wallet_signer()
        base_units = to_erc20_raw(amount_usdce, token_decimals)

        rcpt = to_checksum_address(recipient_addr)
        if (
            int(to_chain_id) == POLYGON_CHAIN_ID
            and to_checksum_address(to_token_address)
            == to_checksum_address(POLYGON_USDC_ADDRESS)
            and rcpt == to_checksum_address(from_address)
        ):
            brap = await _try_brap_swap_polygon(
                from_token_address=POLYGON_USDC_E_ADDRESS,
                to_token_address=to_token_address,
                from_address=from_address,
                amount_base_unit=base_units,
                signing_callback=sign_cb,
            )
            if brap:
                return True, {**brap, "recipient_addr": rcpt}

        ok_addr, addr_data = await self.bridge_withdraw_addresses(
            address=from_address,
            to_chain_id=to_chain_id,
            to_token_address=to_token_address,
            recipient_addr=recipient_addr,
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
            "from_token_address": POLYGON_USDC_E_ADDRESS,
            "withdraw_address": str(withdraw_evm),
            "amount_base_unit": str(base_units),
            "to_chain_id": str(to_chain_id),
            "to_token_address": to_token_address,
            "recipient_addr": to_checksum_address(recipient_addr),
        }

    def _resolve_wallet(self) -> dict[str, Any]:
        cfg = self.config or {}
        for key in ("strategy_wallet", "main_wallet"):
            w = cfg.get(key)
            if isinstance(w, dict) and w.get("address"):
                return w

        wallets = cfg.get("wallets")
        if isinstance(wallets, list) and wallets:
            for w in wallets:
                if isinstance(w, dict) and str(w.get("label", "")).lower() == "main":
                    return w
            for w in wallets:
                if isinstance(w, dict) and w.get("address"):
                    return w

        raise ValueError(
            "No wallet configured. Provide config.strategy_wallet or pass wallet via get_adapter(..., wallet_label=...)."
        )

    def _resolve_private_key(self) -> str:
        if self._private_key_hex:
            return self._private_key_hex.removeprefix("0x")
        wallet = self._resolve_wallet()
        pk = wallet.get("private_key_hex") or wallet.get("private_key")
        if not pk:
            raise ValueError(
                "Wallet is missing private_key_hex (required for CLOB trading)."
            )
        return str(pk).removeprefix("0x")

    def _resolve_funder(self) -> str:
        if self._funder_override:
            return to_checksum_address(self._funder_override)
        wallet = self._resolve_wallet()
        addr = wallet.get("address")
        if not addr:
            raise ValueError("Wallet missing address")
        return to_checksum_address(str(addr))

    def _resolve_wallet_signer(self) -> tuple[str, Any]:
        wallet = self._resolve_wallet()
        from_address = to_checksum_address(str(wallet.get("address")))

        sign_cb = self.strategy_wallet_signing_callback
        if sign_cb is None:
            pk = self._resolve_private_key()
            account = Account.from_key(pk)

            async def _sign_cb(tx: dict) -> bytes:
                signed = account.sign_transaction(tx)
                return signed.raw_transaction

            sign_cb = _sign_cb

        return from_address, sign_cb

    def _contract_addrs(self, *, neg_risk: bool = False) -> dict[str, str]:
        cfg = get_contract_config(POLYGON_CHAIN_ID, neg_risk=neg_risk)
        return {
            "exchange": str(cfg.exchange),
            "collateral": str(cfg.collateral),
            "conditional_tokens": str(cfg.conditional_tokens),
        }

    @property
    def clob_client(self) -> ClobClient:  # type: ignore[valid-type]
        if self._clob_client is None:
            pk = self._resolve_private_key()
            funder = self._resolve_funder()
            self._clob_client = ClobClient(  # type: ignore[misc]
                str(self._clob_http.base_url),
                chain_id=POLYGON_CHAIN_ID,
                key=pk,
                signature_type=self._signature_type,
                funder=funder,
            )
        return self._clob_client  # type: ignore[return-value]

    async def ensure_api_creds(self) -> tuple[bool, dict[str, Any] | str]:
        try:
            if self._api_creds_set:
                return True, {"ok": True}

            creds = await asyncio.to_thread(self.clob_client.create_or_derive_api_creds)
            self.clob_client.set_api_creds(creds)
            self._api_creds_set = True
            return True, {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def ensure_onchain_approvals(
        self,
        *,
        also_approve_conditional_tokens_spender: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        from_address, sign_cb = self._resolve_wallet_signer()

        cfg = self._contract_addrs(neg_risk=False)
        cfg_nr = self._contract_addrs(neg_risk=True)

        exchanges: set[str] = {cfg["exchange"], cfg_nr["exchange"], POLYMARKET_RISK_ADAPTER_EXCHANGE_ADDRESS}
        collateral = cfg["collateral"]
        conditional_tokens = cfg["conditional_tokens"]

        spenders = set(exchanges)
        if also_approve_conditional_tokens_spender:
            spenders.add(conditional_tokens)

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
        ensure_approvals: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        if ensure_approvals:
            ok_appr, appr = await self.ensure_onchain_approvals()
            if not ok_appr:
                return False, appr
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )  # type: ignore[misc]
            order = await asyncio.to_thread(self.clob_client.create_order, order_args)
            resp = await asyncio.to_thread(
                self.clob_client.post_order, order, "GTC", post_only
            )
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
        ensure_approvals: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        # BUY amount = collateral ($) to spend, SELL amount = shares to sell
        if ensure_approvals:
            ok_appr, appr = await self.ensure_onchain_approvals()
            if not ok_appr:
                return False, appr
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg

        try:
            order_args = MarketOrderArgs(  # type: ignore[misc]
                token_id=token_id,
                side=side,
                amount=amount,
                price=price or 0.0,
            )
            order = await asyncio.to_thread(
                self.clob_client.create_market_order, order_args
            )
            resp = await asyncio.to_thread(
                self.clob_client.post_order, order, order_args.order_type, False
            )
            return True, resp if isinstance(resp, dict) else {"result": resp}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def cancel_order(self, *, order_id: str) -> tuple[bool, dict[str, Any] | str]:
        ok, msg = await self.ensure_api_creds()
        if not ok:
            return False, msg
        try:
            resp = await asyncio.to_thread(self.clob_client.cancel, order_id)
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
            data = await asyncio.to_thread(self.clob_client.get_orders, params)
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
            bal_usdce, bal_usdc = await asyncio.gather(
                get_token_balance(POLYGON_USDC_E_ADDRESS, POLYGON_CHAIN_ID, addr),
                get_token_balance(POLYGON_USDC_ADDRESS, POLYGON_CHAIN_ID, addr),
            )
            return True, {
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
            signer_addr = self._resolve_funder()
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
            POLYGON_USDC_ADDRESS,
            POLYGON_USDC_E_ADDRESS,
        ]

        index_sets = await self._outcome_index_sets(condition_id=condition_id)

        async def _try_parent(parent: bytes) -> tuple[bool, dict[str, Any] | str]:
            for collateral in collaterals:
                pos_ids = await asyncio.gather(
                    *[
                        self._compute_position_id(
                            collateral=collateral,
                            parent_collection_id=parent,
                            condition_id=cond_b32,
                            index_set=i,
                        )
                        for i in index_sets
                    ]
                )
                bals = await asyncio.gather(
                    *[
                        self._balance_of_position(holder=holder, position_id=pid)
                        for pid in pos_ids
                    ]
                )
                redeemable = [
                    i for i, b in zip(index_sets, bals, strict=False) if int(b) > 0
                ]
                if redeemable:
                    return True, {
                        "collateral": to_checksum_address(collateral),
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
        holder_addr, sign_cb = self._resolve_wallet_signer()
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
