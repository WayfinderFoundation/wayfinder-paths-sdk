from __future__ import annotations

from typing import Any, Protocol

from wayfinder_paths.core.clients.BRAPClient import BRAPQuoteResponse
from wayfinder_paths.core.clients.HyperlendClient import (
    AssetsView,
    LendRateHistory,
    MarketEntry,
    StableMarketsHeadroomResponse,
)
from wayfinder_paths.core.clients.HyperliquidDataClient import (
    CandleEntry,
    FundingHistoryEntry,
)
from wayfinder_paths.core.clients.LedgerClient import (
    StrategyTransactionList,
    TransactionRecord,
)
from wayfinder_paths.core.clients.PoolClient import (
    LlamaMatchesResponse,
    PoolList,
)
from wayfinder_paths.core.clients.TokenClient import (
    GasToken,
    TokenDetails,
)


class TokenClientProtocol(Protocol):
    async def get_token_details(
        self,
        query: str,
        market_data: bool = True,
        chain_id: int | None = None,
    ) -> TokenDetails: ...

    async def get_gas_token(self, chain_code: str) -> GasToken: ...


class HyperlendClientProtocol(Protocol):
    async def get_stable_markets(
        self,
        *,
        required_underlying_tokens: float | None = None,
        buffer_bps: int | None = None,
        min_buffer_tokens: float | None = None,
    ) -> StableMarketsHeadroomResponse: ...

    async def get_assets_view(
        self,
        *,
        user_address: str,
    ) -> AssetsView: ...

    async def get_market_entry(
        self,
        *,
        token: str,
    ) -> MarketEntry: ...

    async def get_lend_rate_history(
        self,
        *,
        token: str,
        lookback_hours: int,
        force_refresh: bool | None = None,
    ) -> LendRateHistory: ...


class HyperliquidDataClientProtocol(Protocol):
    async def get_funding_history(
        self, coin: str, start_ms: int, end_ms: int
    ) -> list[FundingHistoryEntry]: ...

    async def get_candles(
        self, coin: str, start_ms: int, end_ms: int, interval: str = "1h"
    ) -> list[CandleEntry]: ...


class LedgerClientProtocol(Protocol):
    async def get_strategy_transactions(
        self,
        *,
        wallet_address: str,
        limit: int = 50,
        offset: int = 0,
    ) -> StrategyTransactionList: ...

    async def get_strategy_net_deposit(self, *, wallet_address: str) -> float: ...

    async def get_strategy_latest_transactions(
        self, *, wallet_address: str
    ) -> StrategyTransactionList: ...

    async def add_strategy_deposit(
        self,
        *,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord: ...

    async def add_strategy_withdraw(
        self,
        *,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord: ...

    async def add_strategy_operation(
        self,
        *,
        wallet_address: str,
        operation_data: dict[str, Any],
        usd_value: str | float,
        strategy_name: str | None = None,
    ) -> TransactionRecord: ...


class PoolClientProtocol(Protocol):
    async def get_pools_by_ids(
        self,
        *,
        pool_ids: list[str] | str,
    ) -> PoolList: ...

    async def get_pools(
        self,
        *,
        chain_id: int | None = None,
        project: str | None = None,
    ) -> LlamaMatchesResponse: ...


class BRAPClientProtocol(Protocol):
    async def get_quote(
        self,
        *,
        from_token: str,
        to_token: str,
        from_chain: int,
        to_chain: int,
        from_wallet: str,
        from_amount: str,
    ) -> BRAPQuoteResponse: ...


class HyperliquidExecutorProtocol(Protocol):
    async def place_market_order(
        self,
        *,
        asset_id: int,
        is_buy: bool,
        slippage: float,
        size: float,
        address: str,
        reduce_only: bool = False,
        cloid: Any = None,
        builder: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_order(
        self,
        *,
        asset_id: int,
        order_id: int,
        address: str,
    ) -> dict[str, Any]: ...

    async def update_leverage(
        self,
        *,
        asset_id: int,
        leverage: int,
        is_cross: bool,
        address: str,
    ) -> dict[str, Any]: ...

    async def transfer_spot_to_perp(
        self,
        *,
        amount: float,
        address: str,
    ) -> dict[str, Any]: ...

    async def transfer_perp_to_spot(
        self,
        *,
        amount: float,
        address: str,
    ) -> dict[str, Any]: ...

    async def place_stop_loss(
        self,
        *,
        asset_id: int,
        is_buy: bool,
        trigger_price: float,
        size: float,
        address: str,
    ) -> dict[str, Any]: ...

    async def place_limit_order(
        self,
        *,
        asset_id: int,
        is_buy: bool,
        price: float,
        size: float,
        address: str,
        reduce_only: bool = False,
        builder: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def withdraw(
        self,
        *,
        amount: float,
        address: str,
    ) -> dict[str, Any]: ...

    async def approve_builder_fee(
        self,
        *,
        builder: str,
        max_fee_rate: str,
        address: str,
    ) -> dict[str, Any]: ...
