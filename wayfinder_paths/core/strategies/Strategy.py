from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from loguru import logger

from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.core.clients.TokenClient import TokenDetails
from wayfinder_paths.core.strategies.descriptors import StratDescriptor


class StatusDict(TypedDict):
    portfolio_value: float
    net_deposit: float
    strategy_status: Any
    gas_available: float
    gassed_up: bool


StatusTuple = tuple[bool, str]


class WalletConfig(TypedDict, total=False):
    address: str
    private_key: str | None
    private_key_hex: str | None
    wallet_type: str | None


class StrategyConfig(TypedDict, total=False):
    main_wallet: WalletConfig | None
    strategy_wallet: WalletConfig | None
    wallet_type: str | None


class LiquidationResult(TypedDict):
    usd_value: float
    token: TokenDetails
    amt: int


class QuoteResult(TypedDict, total=False):
    expected_apy: float  # Required - decimal (0.10 = 10%)
    apy_type: str  # Required - "blended" | "net" | "gross" | "combined"
    confidence: str | None  # "high" | "medium" | "low"
    methodology: str | None  # Brief description
    components: dict[str, float] | None  # Breakdown by source
    deposit_amount: float | None
    as_of: str | None  # ISO timestamp
    summary: str  # Required - human-readable


class Strategy(ABC):
    name: str | None = None
    INFO: StratDescriptor | None = None

    def __init__(
        self,
        config: StrategyConfig | dict[str, Any] | None = None,
        *,
        main_wallet_signing_callback: Callable[[dict], Awaitable[str]] | None = None,
        strategy_wallet_signing_callback: Callable[[dict], Awaitable[str]]
        | None = None,
        strategy_sign_typed_data: Callable[[dict], Awaitable[str]] | None = None,
        **kwargs: Any,
    ):
        self.ledger_adapter = LedgerAdapter()
        self.logger = logger.bind(strategy=self.__class__.__name__)
        self.config: StrategyConfig | dict[str, Any] = config or {}
        self.main_wallet_signing_callback = main_wallet_signing_callback
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback
        self.strategy_sign_typed_data = strategy_sign_typed_data

    async def setup(self) -> None:
        pass

    def _get_strategy_wallet_address(self) -> str:
        strategy_wallet = self.config.get("strategy_wallet")
        if not strategy_wallet or not isinstance(strategy_wallet, dict):
            raise ValueError("strategy_wallet not configured in strategy config")
        address = strategy_wallet.get("address")
        if not address:
            raise ValueError("strategy_wallet address not found in config")
        return str(address)

    def _get_main_wallet_address(self) -> str:
        main_wallet = self.config.get("main_wallet")
        if not main_wallet or not isinstance(main_wallet, dict):
            raise ValueError("main_wallet not configured in strategy config")
        address = main_wallet.get("address")
        if not address:
            raise ValueError("main_wallet address not found in config")
        return str(address)

    @abstractmethod
    async def deposit(self, **kwargs) -> StatusTuple:
        pass

    async def withdraw(self, **kwargs) -> StatusTuple:
        return (True, "Withdrawal complete")

    @abstractmethod
    async def update(self) -> StatusTuple:
        pass

    @abstractmethod
    async def exit(self, **kwargs) -> StatusTuple:
        pass

    @staticmethod
    async def policies() -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def _status(self) -> StatusDict:
        pass

    async def status(self) -> StatusDict:
        status = await self._status()
        await self.ledger_adapter.record_strategy_snapshot(
            wallet_address=self._get_strategy_wallet_address(),
            strategy_status=status,
        )

        return status

    async def partial_liquidate(self, usd_value: float) -> StatusTuple:
        if usd_value <= 0:
            raise ValueError(f"usd_value must be positive, got {usd_value}")
        return (False, "Partial liquidation not implemented for this strategy")
