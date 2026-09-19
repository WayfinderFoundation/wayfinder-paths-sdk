"""On-chain spot venue: tokens bought and sold through the swap router.

Symbols are token ids (``ethereum-robinhood``, ``usd-coin-polygon``,
``robinhood_0x…``) — the same ids ``ctx.token_value`` reads. A buy opens a
long-only inventory position priced in USD, a sell closes it; there are no
shorts, brackets or resting orders. Paper fills go through the shared paper
broker at the token's USD price with the venue's fee and slippage
assumptions. Live fills quote and then swap through the on-chain tools on
the job's wallet, USDC in and USDC out on the token's own chain (gas is
sponsored on the shells, so nothing here checks or bridges gas).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.jobs.execution.hyperliquid import _paper_broker
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    TradeCapacity,
)
from wayfinder_paths.jobs.execution.token_bars import (
    TokenBarReader,
    bar_window,
    close_labelled_rows,
    is_token_symbol,
)
from wayfinder_paths.jobs.execution.venues import (
    Broker,
    MarketDataFeed,
    MarketEvent,
    VenueAdapter,
    VenueCapabilities,
    VenueState,
    register_venue,
)

ONCHAIN_CAPABILITIES = VenueCapabilities(
    market_kind="spot",
    supports_brackets=False,
    supports_shorts=False,
    supports_notional_sizing=True,
    supports_limit_orders=False,
    position_model="netting",
    settlement="continuous",
)
QUOTE_ASSET = "usd-coin"
DEFAULT_SLIPPAGE_BPS = 50


def chain_code_for(symbol: str, details: dict[str, Any] | None) -> str:
    """The chain a token id lives on: from the resolved details, else from
    the id itself (``<coingecko>-<chain>`` or ``<chain>_<address>``)."""
    chain = (details or {}).get("chain") or {}
    if chain.get("code"):
        return str(chain["code"])
    if "_" in symbol:
        return symbol.split("_", 1)[0]
    if "-" in symbol:
        return symbol.rsplit("-", 1)[1]
    raise ValueError(f"cannot tell which chain {symbol!r} is on")


def quote_token_for(symbol: str, details: dict[str, Any] | None) -> str:
    return f"{QUOTE_ASSET}-{chain_code_for(symbol, details)}"


def decimal_string(value: float | Decimal | str) -> str:
    """The on-chain tools want a human-units amount with a decimal point."""
    try:
        text = format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation as exc:
        raise ValueError(f"amount {value!r} is not a number") from exc
    return text if "." in text else f"{text}.0"


def human_amount(value: Any, decimals: int) -> float | None:
    """A quote amount as tokens. Routers report either human units or raw
    integer units; an integer-looking string at or above the raw scale is
    treated as raw."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if (
        "." not in text
        and "e" not in text.lower()
        and amount >= Decimal(10) ** max(decimals - 2, 0)
    ):
        amount = amount / (Decimal(10) ** decimals)
    return float(amount)


class OnchainMarketFeed:
    """Completed OHLCV bars per token id at the job's interval, read by window
    from the on-chain data source's store. Symbols that are not token ids (a
    perp coin, a spot pair) belong to another venue's feed and are skipped,
    so a mixed book never sees a symbol twice."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        resolver: type[TokenResolver] = TokenResolver,
        token_resolution: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.client = client or TOKEN_CLIENT
        self.reader = TokenBarReader(client=self.client, resolver=resolver)
        self.token_resolution = {
            str(symbol): dict(pinned)
            for symbol, pinned in (token_resolution or {}).items()
        }
        self._provenance: dict[str, dict[str, Any]] = {}

    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: datetime | None = None,
    ) -> CompletedBarsView:
        end = as_of or datetime.now(UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        start_ms, end_ms = bar_window(
            interval, lookback_bars=lookback_bars, end_ms=int(end.timestamp() * 1000)
        )
        rows: list[Mapping[str, Any]] = []
        for symbol in symbols:
            if not is_token_symbol(symbol):
                continue
            pinned = self.token_resolution.get(str(symbol)) or {}
            window = await self.reader.window(
                str(symbol),
                interval,
                start_ms=start_ms,
                end_ms=end_ms,
                chain_id=pinned.get("chain_id"),
                address=pinned.get("address"),
            )
            rows.extend(close_labelled_rows(str(symbol), window.bars))
            self._provenance[str(symbol)] = {
                "earliest_available": _iso_ms(window.history_start_ms),
                "requested_start": _iso_ms(start_ms),
                "chain_id": window.chain_id,
                "address": window.address,
            }
        return CompletedBarsView.from_rows(rows)

    def history_provenance(self) -> dict[str, dict[str, Any]]:
        return {symbol: dict(entry) for symbol, entry in self._provenance.items()}

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]:
        return []


def _iso_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


class OnchainSwapBroker:
    """Live broker: quote, then swap, on the job's wallet. A buy spends
    ``notional`` USDC on the token's chain; a sell swaps ``size`` tokens back
    to USDC. Fills carry tokens as ``filled_size`` and USD per token as
    ``avg_price`` so the ledger is in the same units as paper."""

    capabilities = ONCHAIN_CAPABILITIES

    def __init__(
        self,
        *,
        wallet_label: str,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        quote_token: str | None = None,
        quote: Callable[..., Any] | None = None,
        swap: Callable[..., Any] | None = None,
        token_details: Callable[..., Any] | None = None,
    ) -> None:
        if not wallet_label:
            raise ValueError("live onchain trading needs the job's wallet_label")
        self.wallet_label = wallet_label
        self.slippage_bps = int(slippage_bps)
        self.quote_token = quote_token
        self._quote = quote
        self._swap = swap
        self._token_details = token_details

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        if intent.limit_price is not None:
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error="on-chain swaps fill at market; limit orders are not supported",
            )
        if intent.action == "OPEN" and intent.side != "long":
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error="onchain is spot: it holds tokens and cannot short",
            )
        try:
            details = await self._details(intent.symbol)
        except Exception as exc:  # noqa: BLE001 — resolution failures are fills, never raises
            return self._fill(
                intent, "rejected", timestamp, error=f"token resolution: {exc}"
            )
        decimals = int((details or {}).get("decimals") or 18)
        quote_token = self.quote_token or quote_token_for(intent.symbol, details)
        buying = intent.action == "OPEN"
        if buying:
            notional = intent.notional
            if notional is None and intent.size is not None and price:
                notional = float(intent.size) * float(price)
            if not notional or float(notional) <= 0:
                return self._fill(
                    intent, "rejected", timestamp, error="a buy needs a USD notional"
                )
            from_token, to_token, amount = quote_token, intent.symbol, float(notional)
        else:
            if intent.size is None or float(intent.size) <= 0:
                return self._fill(
                    intent,
                    "rejected",
                    timestamp,
                    error="a sell needs the token size to sell",
                )
            from_token, to_token, amount = (
                intent.symbol,
                quote_token,
                float(intent.size),
            )
        try:
            quoted = await self._quote_fn()(
                wallet_label=self.wallet_label,
                from_token=from_token,
                to_token=to_token,
                amount=decimal_string(amount),
                slippage_bps=self.slippage_bps,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fill(
                intent, "rejected", timestamp, error=f"quote failed: {exc}"
            )
        if not quoted.get("ok"):
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error=_tool_error(quoted),
                raw={"quote": quoted},
            )
        quote_result = quoted.get("result") or {}
        best = (quote_result.get("quote") or {}).get("best_quote") or {}
        request = dict(quote_result.get("suggested_swap_request") or {})
        if not request:
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error="quote returned no swap request",
                raw={"quote": best},
            )
        try:
            swapped = await self._swap_fn()(**request)
        except Exception as exc:  # noqa: BLE001 — a broadcast we cannot confirm is ambiguous, not failed
            return self._fill(
                intent,
                "ambiguous",
                timestamp,
                error=f"swap failed: {exc}",
                raw={"quote": best},
            )
        if not swapped.get("ok"):
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error=_tool_error(swapped),
                raw={"quote": best, "swap": swapped},
            )
        result = swapped.get("result") or {}
        status = str(result.get("status") or "")
        if status == "failed":
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error=str(result.get("error") or "swap failed on chain"),
                raw={"quote": best, "swap": result},
            )
        fill_status = "filled" if status == "confirmed" else "ambiguous"
        tokens: float | None
        usd: float | None
        if buying:
            tokens = human_amount(best.get("output_amount"), decimals)
            if not tokens and price:
                tokens = float(amount) / float(price)
            usd = _float(best.get("input_amount_usd")) or float(amount)
        else:
            tokens = float(amount)
            usd = _float(best.get("output_amount_usd"))
        avg_price = (
            (usd / tokens) if (usd and tokens) else (float(price) if price else None)
        )
        swap_effect = (result.get("effects") or {}).get("swap") or {}
        return FillEvent(
            status=fill_status,  # type: ignore[arg-type]
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            filled_size=float(tokens or 0.0),
            avg_price=avg_price,
            fee=_float(best.get("fee_estimate")) or 0.0,
            order_id=str(swap_effect.get("txn_hash") or result.get("txn_hash") or "")
            or None,
            client_order_id=intent.client_order_id,
            reduce_only=intent.reduce_only,
            error=None
            if fill_status == "filled"
            else f"swap {status or 'unconfirmed'}",
            raw={"quote": best, "swap": result, "quote_token": quote_token},
            timestamp=timestamp,
        )

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        # Inventory lives in the job ledger; the wallet is reconciled by the
        # forensics, not on every tick.
        return VenueState(source="onchain")

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="onchain")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue="onchain",
            symbol="",
            side="",
            error="on-chain swaps do not rest; nothing to cancel",
            client_order_id=client_order_id,
        )

    # ---- internals -------------------------------------------------------
    async def _details(self, symbol: str) -> dict[str, Any]:
        fn = self._token_details or (
            lambda s: TOKEN_CLIENT.get_token_details(s, market_data=False)
        )
        return dict(await fn(symbol) or {})

    def _quote_fn(self) -> Callable[..., Any]:
        if self._quote is not None:
            return self._quote
        # lazy: keeps execution/ decoupled from the MCP tool stack and patchable in tests
        from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap

        return onchain_quote_swap

    def _swap_fn(self) -> Callable[..., Any]:
        if self._swap is not None:
            return self._swap
        from wayfinder_paths.mcp.tools.execute import onchain_swap

        return onchain_swap

    def _fill(
        self,
        intent: OrderIntent,
        status: str,
        timestamp: str,
        *,
        error: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> FillEvent:
        return FillEvent(
            status=status,  # type: ignore[arg-type]
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            reduce_only=intent.reduce_only,
            error=error,
            raw=raw or {},
            timestamp=timestamp,
        )


def _tool_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(error or payload)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class OnchainVenueAdapter:
    name = "onchain"
    capabilities = ONCHAIN_CAPABILITIES
    feed: MarketDataFeed
    broker: Broker

    def __init__(
        self, *, mode: str, params: dict[str, Any] | None = None, spec: Any = None
    ) -> None:
        params = params or {}
        self.feed = OnchainMarketFeed(token_resolution=_token_resolution(spec))
        if mode == "live":
            self.broker = OnchainSwapBroker(
                wallet_label=str(params.get("wallet_label") or ""),
                slippage_bps=int(params.get("slippage_bps") or DEFAULT_SLIPPAGE_BPS),
                quote_token=params.get("quote_token"),
            )
        else:
            self.broker = _paper_broker(ONCHAIN_CAPABILITIES, params, venue="onchain")


def _token_resolution(spec: Any) -> dict[str, Any]:
    """`data_contract.token_resolution` pins chain and address per symbol at
    create time; a spec without it (or no spec) resolves on first read."""
    contract = (
        getattr(spec, "data_contract", None)
        if spec is not None and not isinstance(spec, Mapping)
        else (spec or {}).get("data_contract")
    )
    return dict((contract or {}).get("token_resolution") or {})


def build_onchain_adapter(
    *, mode: str, spec: Any = None, params: dict[str, Any] | None = None
) -> VenueAdapter:
    return OnchainVenueAdapter(mode=mode, params=params, spec=spec)


register_venue("onchain", build_onchain_adapter, capabilities=ONCHAIN_CAPABILITIES)
