import asyncio
import math
import time
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal

import numpy as np
import pandas as pd
from loguru import logger
from web3 import Web3

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.adapters.hyperlend_adapter.adapter import HyperlendAdapter
from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.constants.base import DEFAULT_SLIPPAGE
from wayfinder_paths.core.constants.contracts import HYPEREVM_WHYPE
from wayfinder_paths.core.strategies.descriptors import (
    Complexity,
    Directionality,
    Frequency,
    StratDescriptor,
    TokenExposure,
    Volatility,
)
from wayfinder_paths.core.strategies.Strategy import StatusDict, StatusTuple, Strategy
from wayfinder_paths.policies.enso import ENSO_ROUTER, enso_swap
from wayfinder_paths.policies.erc20 import erc20_spender_for_any_token
from wayfinder_paths.policies.hyper_evm import (
    hypecore_sentinel_deposit,
    whype_deposit_and_withdraw,
)
from wayfinder_paths.policies.hyperlend import (
    HYPERLEND_POOL,
    hyperlend_supply_and_withdraw,
)
from wayfinder_paths.policies.hyperliquid import (
    any_hyperliquid_l1_payload,
    any_hyperliquid_user_payload,
)
from wayfinder_paths.policies.lifi import LIFI_ROUTERS, lifi_swap
from wayfinder_paths.policies.prjx import PRJX_ROUTER, prjx_swap

SYMBOL_TRANSLATION_TABLE = str.maketrans(
    {
        "₮": "T",
        "₿": "B",
        "Ξ": "X",
    }
)
WRAPPED_HYPE_ADDRESS = HYPEREVM_WHYPE


class HyperlendStableYieldStrategy(Strategy):
    name = "HyperLend Stable Optimizer"

    # Strategy parameters
    APY_SHORT_CIRCUIT_THRESHOLD = None
    MIN_USDT0_DEPOSIT_AMOUNT = 1
    HORIZON_HOURS = 6
    BLOCK_LEN = 6
    TRIALS = 4000
    HALFLIFE_DAYS = 7
    SEED = 7
    HYSTERESIS_DWELL_HOURS = 168
    HYSTERESIS_Z = 1.15
    GAS_MAXIMUM = 0.1
    ROTATION_POLICY = "hysteresis"
    ROTATION_TX_COST = 0.002
    SUPPLY_CAP_BUFFER_BPS = 50
    SUPPLY_CAP_MIN_BUFFER_TOKENS = 0.5
    ASSETS_SNAPSHOT_TTL_SECONDS = 20.0
    DEFAULT_LOOKBACK_HOURS = 24 * 7
    APY_REBALANCE_THRESHOLD = 0.0035
    TOURNAMENT_MODE = "joint"
    ROTATION_COOLDOWN = timedelta(hours=168)
    P_BEST_ROTATION_THRESHOLD = 0.4
    MAX_CANDIDATES = 5
    MIN_STABLE_SWAP_TOKENS = 1e-3
    MAX_GAS = 0.1

    INFO = StratDescriptor(
        description=f"""Multi-strategy allocator that converts USDT0 into the most consistently rewarding HyperLend stablecoin and continuously checks if a rotation is justified.
            **What it does:** Pulls USDT0 from the main wallet, ensures a small HYPE safety buffer for gas, swaps the remaining stable balance into candidate markets, and supplies
            liquidity to HyperLend. Hourly rate histories are aggregated into a 7-day panel and routed through a block-bootstrap tournament (horizon {HORIZON_HOURS}h, block length {BLOCK_LEN}, {TRIALS:,}
            trials, {HALFLIFE_DAYS}-day half-life weighting) to estimate which asset has the highest probability of outperforming peers. USDT0 is the LayerZero bridgable stablecoin for USDT.
            **Exposure type:** Market-neutral stablecoin lending on HyperEVM with automated rotation into whichever pool offers the strongest risk-adjusted lending yield.
            **Chains:** HyperEVM only (HyperLend pool suite).
            **Deposit/Withdrawal:** Deposits move USDT0 from the main wallet into the strategy wallet, top up a minimal HYPE gas buffer, rotate into the selected stable, and lend it via HyperLend.
            Withdrawals unwind the lend position, convert balances back to USDT, and return funds (plus residual HYPE) to the main wallet.
            **Gas**: Requires HYPE on HypeEVM. Arbitrary amount of funding gas is accepted via strategy wallet transfers.
            """,
        summary=(
            "Recency-weighted HyperLend stablecoin optimizer that bootstraps 7-day rate history "
            f"(horizon {HORIZON_HOURS}h, {BLOCK_LEN}-hour blocks, {TRIALS:,} simulations) to pick the top "
            "performer, funds with USDT0, tops up a small HYPE gas buffer, and defaults to a hysteresis "
            f"rotation band (dwell={HYSTERESIS_DWELL_HOURS}h, z={HYSTERESIS_Z:.2f}) to avoid churn while still "
            "short-circuiting when yield gaps are extreme."
        ),
        risk_description="Protocol risk is always present when engaging with DeFi strategies, this includes underlying DeFi protocols and Wayfinder itself. Additional risk includes rate volatility between sampling windows, swap slippage on HYPE ⇄ stable legs, HyperLend protocol risk, and rotation gas costs eroding yield if APY edges are thin. Strategy requires a small HYPE balance for gas on HyperEVM.",
        gas_token_symbol="HYPE",
        gas_token_id="hyperliquid-hyperevm",
        deposit_token_id="usdt0-hyperevm",
        minimum_net_deposit=10,
        gas_maximum=MAX_GAS,
        gas_threshold=MAX_GAS / 3,
        # risk indicators
        volatility=Volatility.LOW,
        volatility_description=(
            "Pure HyperLend stablecoin lending keeps NAV steady aside from rate drift."
        ),
        directionality=Directionality.MARKET_NEUTRAL,
        directionality_description=(
            "Rotates capital between USD stables so exposure stays market neutral."
        ),
        complexity=Complexity.LOW,
        complexity_description="Agent handles optimal pool finding, swaps, and lend transactions automatically.",
        token_exposure=TokenExposure.STABLECOINS,
        token_exposure_description=(
            "Only HyperEVM USD stables (USDT0 and peers), no volatile tokens."
        ),
        frequency=Frequency.LOW,
        frequency_description=(
            "Updates every 2 hours; rotations infrequent (weekly cooldowns)."
        ),
        return_drivers=["lend APY", "pool yield"],
        config={
            "deposit": {
                "description": "Move USDT0 into the strategy, ensure a small HYPE gas buffer, and supply the best HyperLend stable.",
                "parameters": {
                    "main_token_amount": {
                        "type": "float",
                        "unit": "USDT0 tokens",
                        "description": "Amount of USDT0 to allocate to HyperLend.",
                        "minimum": 1.0,  # TODO: 10
                        "examples": ["100.0", "250.5"],
                    },
                    "gas_token_amount": {
                        "type": "float",
                        "unit": "HYPE tokens",
                        "description": "Amount of HYPE to top up into the strategy wallet to cover gas costs.",
                        "minimum": 0.0,
                        "maximum": GAS_MAXIMUM,
                        "recommended": GAS_MAXIMUM,
                    },
                },
                "result": "USDT0 converted into the top-performing HyperLend stablecoin and supplied on-chain.",
            },
            "withdraw": {
                "description": "Unwinds the position, converts balances to USDT0, and returns funds (plus HYPE buffer) to the main wallet.",
                "parameters": {},
                "result": "Principal and accrued gains returned in USDT0; residual HYPE buffer swept home.",
            },
            "update": {
                "description": (
                    "Evaluates tournament projections and rotates when the hysteresis band is breached "
                    f"(dwell={HYSTERESIS_DWELL_HOURS}h, z={HYSTERESIS_Z:.2f}) or when a short-circuit gap is hit "
                    "(set HYPERLEND_ROTATION_POLICY=cooldown to restore the legacy threshold/cooldown rule)."
                ),
                "parameters": {},
            },
            "status": {
                "description": "Summarises current lend position, APY, and chosen asset.",
                "provides": [
                    "lent_asset",
                    "lent_balance",
                    "current_apy",
                    "best_candidate",
                    "best_candidate_apy",
                ],
            },
            "points": {
                "description": "Fetch the HyperLend points account snapshot for this strategy wallet using a signed login.",
                "parameters": {},
                "result": "Returns the HyperLend points API payload for the strategy wallet.",
            },
            "technical_details": {
                "rotation_policy": ROTATION_POLICY.lower(),
                "hysteresis_dwell_hours": HYSTERESIS_DWELL_HOURS,
                "hysteresis_z": HYSTERESIS_Z,
                "rotation_tx_cost": ROTATION_TX_COST,
            },
        },
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        main_wallet: dict[str, Any] | None = None,
        strategy_wallet: dict[str, Any] | None = None,
        main_wallet_signing_callback: Callable[[dict], Awaitable[str]] | None = None,
        strategy_wallet_signing_callback: Callable[[dict], Awaitable[str]]
        | None = None,
        strategy_sign_typed_data: Callable[[dict], Awaitable[str]] | None = None,
    ):
        super().__init__(
            main_wallet_signing_callback=main_wallet_signing_callback,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
            strategy_sign_typed_data=strategy_sign_typed_data,
        )
        merged_config: dict[str, Any] = dict(config or {})
        if main_wallet is not None:
            merged_config["main_wallet"] = main_wallet
        if strategy_wallet is not None:
            merged_config["strategy_wallet"] = strategy_wallet

        self.config: dict[str, Any] = merged_config

        try:
            main_wallet_cfg = self.config.get("main_wallet")
            strategy_wallet_cfg = self.config.get("strategy_wallet")

            if not strategy_wallet_cfg or not strategy_wallet_cfg.get("address"):
                raise ValueError(
                    "strategy_wallet not configured. Provide strategy_wallet address in config or ensure wallet is properly configured for your wallet provider"
                )

            adapter_config = {
                "main_wallet": main_wallet_cfg or None,
                "strategy_wallet": strategy_wallet_cfg or None,
                "strategy": self.config,
            }

            self.balance_adapter = BalanceAdapter(
                adapter_config,
                main_wallet_signing_callback=self.main_wallet_signing_callback,
                strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
            )
            self.token_adapter = TokenAdapter()
            self.ledger_adapter = LedgerAdapter()
            self.brap_adapter = BRAPAdapter(
                adapter_config,
                strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
            )
            self.hyperlend_adapter = HyperlendAdapter(
                adapter_config,
                strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
            )

            self._assets_snapshot = None
            self._assets_snapshot_at = None
            self._assets_snapshot_lock = asyncio.Lock()
            self.symbol_display_map: dict[str, str] = {}
        except Exception as e:
            logger.error(f"Failed to initialize strategy adapters: {e}")
            raise

    async def setup(self):
        if self.token_adapter is None:
            raise RuntimeError(
                "Token adapter not initialized. Strategy initialization may have failed."
            )
        try:
            success, self.usdt_token_info = await self.token_adapter.get_token(
                "usdt0-hyperevm"
            )
            if not success:
                self.usdt_token_info = {}

            success, self.hype_token_info = await self.token_adapter.get_token(
                "hype-hyperevm"
            )
            if not success:
                self.hype_token_info = {}
        except Exception:
            self.usdt_token_info = {}
            self.hype_token_info = {}

        self.current_token = None
        self.current_symbol = None
        self.current_avg_apy = 0.0
        self.kept_hype_tokens = 0.0

        self.last_summary: pd.DataFrame | None = None
        self.last_dominance: pd.DataFrame | None = None
        self.last_samples: np.ndarray | None = None

        self.rotation_policy = self.ROTATION_POLICY
        if self.rotation_policy not in {"hysteresis", "cooldown"}:
            self.rotation_policy = "hysteresis"
        self.hys_dwell_hours: int = max(1, self.HYSTERESIS_DWELL_HOURS)
        self.hys_z: float = self.HYSTERESIS_Z
        self.rotation_tx_cost: float = self.ROTATION_TX_COST

    async def deposit(
        self, main_token_amount: float = 0.0, gas_token_amount: float = 0.0
    ) -> StatusTuple:
        if main_token_amount == 0.0 and gas_token_amount == 0.0:
            return (
                False,
                "Either main_token_amount or gas_token_amount must be provided",
            )

        if main_token_amount > 0:
            if main_token_amount < self.MIN_USDT0_DEPOSIT_AMOUNT:
                return (
                    False,
                    f"Main token amount {main_token_amount} is below minimum {self.MIN_USDT0_DEPOSIT_AMOUNT}",
                )

        if gas_token_amount and gas_token_amount > self.GAS_MAXIMUM:
            return (
                False,
                f"Gas token amount exceeds maximum configured gas buffer: {self.GAS_MAXIMUM}",
            )

        if self.balance_adapter is None:
            return (
                False,
                "Balance adapter not initialized. Strategy initialization may have failed.",
            )

        (
            success,
            main_usdt0_balance,
        ) = await self.balance_adapter.get_balance(
            token_id=self.usdt_token_info.get("token_id"),
            wallet_address=self._get_main_wallet_address(),
        )
        if not success:
            return (
                False,
                f"Failed to get main wallet USDT0 balance: {main_usdt0_balance}",
            )

        (
            success,
            main_hype_balance,
        ) = await self.balance_adapter.get_balance(
            token_id=self.hype_token_info.get("token_id"),
            wallet_address=self._get_main_wallet_address(),
        )
        if not success:
            return (
                False,
                f"Failed to get main wallet HYPE balance: {main_hype_balance}",
            )

        main_usdt0_native = main_usdt0_balance / (
            10 ** self.usdt_token_info.get("decimals")
        )
        main_hype_native = main_hype_balance / (
            10 ** self.hype_token_info.get("decimals")
        )

        if main_token_amount > 0:
            if main_usdt0_native < main_token_amount:
                return (
                    False,
                    f"Main wallet USDT0 balance is less than the deposit amount: {main_usdt0_native} < {main_token_amount}",
                )

        if gas_token_amount > 0:
            if main_hype_native < gas_token_amount:
                return (
                    False,
                    f"Main wallet HYPE balance is less than the deposit amount: {main_hype_native} < {gas_token_amount}",
                )

        if gas_token_amount > 0:
            (
                success,
                msg,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                self.hype_token_info.get("token_id"),
                gas_token_amount,
                strategy_name=self.name,
            )
            if not success:
                return (False, f"HYPE transfer to strategy failed: {msg}")

        if main_token_amount > 0:
            (
                success,
                msg,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                self.usdt_token_info.get("token_id"),
                main_token_amount,
                strategy_name=self.name,
            )
            if not success:
                return (False, f"USDT0 transfer to strategy failed: {msg}")

        self._invalidate_assets_snapshot()
        await self._hydrate_position_from_chain()

        return (success, msg)

    async def _estimate_redeploy_tokens(self) -> float:
        positions = await self._get_lent_positions()
        total_tokens = 0.0

        for entry in positions.values():
            token = entry.get("token")
            amount_wei = entry.get("amount_wei", 0)
            if not token or amount_wei <= 0:
                continue
            try:
                total_tokens += float(amount_wei) / 10 ** token.get("decimals")
            except Exception:
                continue

        return total_tokens

    def _amount_to_wei(self, token: dict[str, Any], amount: Decimal) -> int:
        if amount <= 0:
            return 0

        try:
            return int(amount * (10 ** token.get("decimals")))
        except Exception:
            try:
                decimals = int(getattr(token, "decimals", 18))
            except (TypeError, ValueError):
                decimals = 18
            scale = Decimal(10) ** decimals
            return int((amount * scale).to_integral_value(rounding=ROUND_UP))

    def _display_symbol(self, symbol: str | None) -> str:
        if not symbol:
            return ""
        display = self.symbol_display_map.get(symbol)
        if display:
            return str(display)
        return str(symbol).upper()

    async def _hydrate_position_from_chain(self) -> None:
        snapshot = await self._get_assets_snapshot()
        asset_map = (
            snapshot.get("_by_underlying", {}) if isinstance(snapshot, dict) else {}
        )

        if self.current_token:
            checksum = self._token_checksum(self.current_token)
            asset = asset_map.get(checksum) if checksum else None
            supply = float(asset.get("supply", 0.0)) if asset else 0.0
            if supply > 0.0:
                symbol = self.current_token.get("symbol", None)
                display = asset.get("symbol_display") if asset else symbol
                if symbol and display:
                    self.symbol_display_map.setdefault(str(symbol), display)
                self.current_avg_apy = float(asset.get("supply_apy") or 0.0) if asset else 0.0
                return True
            self.current_token = None
            self.current_symbol = None
            self.current_avg_apy = 0.0

        positions = await self._get_lent_positions(snapshot)
        if not positions:
            return False

        top_entry = max(positions.values(), key=lambda entry: entry["amount_wei"])
        if top_entry.get("amount_wei") <= 0:
            return False

        token = top_entry.get("token")
        if token and not token.get("address"):
            asset_entry = top_entry.get("asset")
            token["address"] = asset_entry.get("underlying") if asset_entry else None
        self.current_token = token
        symbol = token.get("symbol", None) if token else None
        checksum = self._token_checksum(token)
        asset = asset_map.get(checksum) if checksum else None
        if not symbol and asset:
            symbol = asset.get("symbol") or asset.get("symbol_display")
        self.current_symbol = symbol
        display_symbol = asset.get("symbol_display") if asset else None
        if symbol:
            self.symbol_display_map.setdefault(
                str(symbol), display_symbol or symbol.upper()
            )
        self.current_avg_apy = float(asset.get("supply_apy") or 0.0) if asset else 0.0
        return True

    async def _get_assets_snapshot(self, force_refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        if (
            not force_refresh
            and self._assets_snapshot is not None
            and self._assets_snapshot_at is not None
            and now - self._assets_snapshot_at <= self.ASSETS_SNAPSHOT_TTL_SECONDS
        ):
            return self._assets_snapshot

        async with self._assets_snapshot_lock:
            now = time.time()
            if (
                not force_refresh
                and self._assets_snapshot is not None
                and self._assets_snapshot_at is not None
                and now - self._assets_snapshot_at <= self.ASSETS_SNAPSHOT_TTL_SECONDS
            ):
                return self._assets_snapshot

            _, snapshot = await self.hyperlend_adapter.get_assets_view(
                user_address=self._get_strategy_wallet_address(),
            )
            if isinstance(snapshot, str):
                return False

            assets = snapshot.get("assets", [])
            asset_map = {}

            for asset in assets:
                underlying = asset.get("underlying")
                try:
                    checksum = Web3.to_checksum_address(underlying)
                except Exception:
                    continue

                asset["underlying_checksum"] = checksum
                symbol_raw = asset.get("symbol")
                canonical = asset.get("symbol_canonical")
                if not canonical:
                    canonical = (
                        self._normalize_symbol(symbol_raw)
                        if symbol_raw
                        else self._normalize_symbol(checksum)
                    )
                    asset["symbol_canonical"] = canonical
                display_symbol = asset.get("symbol_display")
                if not display_symbol:
                    display_symbol = symbol_raw or (
                        canonical.upper() if canonical else checksum
                    )
                    asset["symbol_display"] = display_symbol
                key = symbol_raw or canonical
                if key:
                    self.symbol_display_map.setdefault(str(key), display_symbol)
                if canonical:
                    self.symbol_display_map.setdefault(canonical, display_symbol)
                asset_map[checksum] = asset

            snapshot["_by_underlying"] = asset_map
            self._assets_snapshot = snapshot
            self._assets_snapshot_at = time.time()

            return snapshot

    async def _has_supply_cap_headroom(
        self, token: dict[str, Any], required_tokens: float
    ) -> bool:
        checksum = self._token_checksum(token)
        if not checksum:
            return False

        try:
            _, data = await self.hyperlend_adapter.get_stable_markets(
                required_underlying_tokens=required_tokens,
                buffer_bps=self.SUPPLY_CAP_BUFFER_BPS,
                min_buffer_tokens=self.SUPPLY_CAP_MIN_BUFFER_TOKENS,
            )
            markets = data.get("markets", {}) if isinstance(data, dict) else {}
        except Exception:
            return True

        try:
            target_lower = Web3.to_checksum_address(checksum).lower()
        except Exception:
            target_lower = str(checksum).lower()

        for addr in markets.keys():
            try:
                if Web3.to_checksum_address(addr).lower() == target_lower:
                    return True
            except Exception:
                if str(addr).lower() == target_lower:
                    return True
        return False

    async def _get_lent_positions(self, snapshot=None) -> dict[str, dict[str, Any]]:
        if not snapshot:
            snapshot = await self._get_assets_snapshot()
        assets = snapshot.get("assets", None)

        if not assets:
            return {}

        positions = {}
        for asset in assets:
            try:
                checksum = asset.get("underlying_checksum") or Web3.to_checksum_address(
                    asset.get("underlying")
                )
            except Exception:
                logger.info(f"Error getting checksum for asset: {asset}")
                continue

            supply = float(asset.get("supply", 0.0) or 0.0)
            if supply <= 0.0:
                logger.info(f"Supply is 0 for asset: {asset}")
                continue

            try:
                chain_id = None
                try:
                    chain_id = int((self.hype_token_info.get("chain") or {}).get("id"))
                except Exception:
                    chain_id = None
                success, token = await self.token_adapter.get_token(
                    checksum, chain_id=chain_id
                )
                if not success or not isinstance(token, dict):
                    logger.info(f"Error getting token for asset: {asset}")
                    continue
            except Exception:
                logger.info(f"Error getting token for asset: {asset}")
                continue

            amount_wei = supply * (10 ** token.get("decimals", 0))
            if amount_wei <= 0:
                logger.info(f"Amount wei is 0 for asset: {asset}")
                continue

            positions[checksum] = {
                "token": token,
                "amount_wei": amount_wei,
                "asset": asset,
            }
        return positions

    def _normalize_symbol(self, symbol: str) -> str:
        if symbol is None:
            return ""

        normalized = unicodedata.normalize("NFKD", str(symbol)).translate(
            SYMBOL_TRANSLATION_TABLE
        )
        ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
        filtered = "".join(ch for ch in ascii_only if ch.isalnum())
        if filtered:
            return filtered.lower()
        return str(symbol).lower()

    def _is_stable_symbol(self, symbol: str) -> bool:
        if not symbol:
            return False
        symbol_upper = symbol.upper()
        stable_keywords = ["USD", "USDC", "USDT", "USDP", "USDD", "USDS", "DAI", "USKB"]
        return any(keyword in symbol_upper for keyword in stable_keywords)

    def _invalidate_assets_snapshot(self) -> None:
        self._assets_snapshot = None
        self._assets_snapshot_at = None

    async def _execute_swap(
        self,
        from_token_info: dict[str, Any],
        to_token_info: dict[str, Any],
        amount_wei: int,
        *,
        slippage: float = DEFAULT_SLIPPAGE,
    ) -> str | None:
        if amount_wei <= 0:
            return None

        from_token_id = (
            from_token_info.get("token_id")
            or f"{from_token_info.get('asset_id')}-{self.hype_token_info.get('chain').get('code')}"
        )
        to_token_id = (
            to_token_info.get("token_id")
            or f"{to_token_info.get('asset_id')}-{self.hype_token_info.get('chain').get('code')}"
        )
        if not from_token_id or not to_token_id:
            return None

        from_address = self._get_token_address(from_token_info, chain_code="hyperevm")
        to_address = self._get_token_address(to_token_info, chain_code="hyperevm")
        if not from_address or not to_address:
            return None

        from_symbol = from_token_info.get("symbol")
        to_symbol = to_token_info.get("symbol")

        strategy_address = self._get_strategy_wallet_address()

        retries = 7
        while retries > 0:
            try:
                from_decimals = from_token_info.get("decimals") or 18
                amount_wei_str = str(amount_wei)
                # TODO: await favourable fees
                (
                    result,
                    tx_data,
                ) = await self.brap_adapter.swap_from_token_ids(
                    from_token_id=from_token_id,
                    to_token_id=to_token_id,
                    from_address=strategy_address,
                    amount=amount_wei_str,
                    slippage=slippage,
                    strategy_name=self.name,
                )

                if not result:
                    error_msg = str(tx_data) if isinstance(tx_data, str) else ""

                    if (
                        "Transaction did not land" in error_msg
                        or "Broadcast fail" in error_msg.lower()
                        or "broadcast" in error_msg.lower()
                        and "fail" in error_msg.lower()
                    ):
                        retries -= 1
                        await asyncio.sleep(3.0)
                        continue
                    else:
                        return None

                self._invalidate_assets_snapshot()
                human = float(amount_wei) / (10**from_decimals)
                return f"Swapped {human:.4f} {from_symbol} → {to_symbol}"

            except Exception:
                retries -= 1
                if retries > 0:
                    await asyncio.sleep(3.0)

        return None

    def _get_token_address(
        self, token: dict[str, Any] | None, chain_code: str = "hyperevm"
    ) -> str | None:
        if not token:
            return None

        address = token.get("address")
        if address:
            return str(address)

        addresses = token.get("addresses")
        if isinstance(addresses, dict):
            address = addresses.get(chain_code)
            if address:
                return str(address)
            if addresses:
                first_address = next(iter(addresses.values()), None)
                if first_address:
                    return str(first_address)

        chain_addresses = token.get("chain_addresses")
        if isinstance(chain_addresses, dict):
            chain_info = chain_addresses.get(chain_code)
            if isinstance(chain_info, dict):
                address = chain_info.get("address")
                if address:
                    return str(address)
            if chain_addresses:
                first_chain_info = next(iter(chain_addresses.values()), None)
                if isinstance(first_chain_info, dict):
                    address = first_chain_info.get("address")
                    if address:
                        return str(address)

        return None

    def _token_checksum(self, token: dict[str, Any] | None) -> str | None:
        address = self._get_token_address(token)
        if not address:
            return None
        try:
            return Web3.to_checksum_address(address)
        except Exception:
            return None

    async def withdraw(self, amount: float | None = None, **kwargs) -> StatusTuple:
        messages = []

        active_token = self.current_token
        if not active_token:
            await self._hydrate_position_from_chain()
            active_token = self.current_token

        amount_wei = 0
        snapshot = await self._get_assets_snapshot(force_refresh=True)
        asset_map = (
            snapshot.get("_by_underlying", {}) if isinstance(snapshot, dict) else {}
        )
        if active_token:
            checksum = self._token_checksum(active_token)
            asset = asset_map.get(checksum) if checksum else None
            lent_balance = float(asset.get("supply", 0.0)) if asset else 0.0
            amount_to_withdraw = amount
            if lent_balance > 0:
                if amount is None:
                    amount_to_withdraw = lent_balance
                elif amount > float(lent_balance):
                    self.logger.warning(
                        "Amount entered is greater than lent_balance. Withdrawing full lent amount instead"
                    )
                    amount_to_withdraw = lent_balance

                amount_wei = float(amount_to_withdraw) * (
                    10 ** active_token.get("decimals")
                )
                chain_code = self.hype_token_info.get("chain", {}).get(
                    "code", "hyperevm"
                )
                underlying_token_address = self._get_token_address(
                    active_token, chain_code
                )
                if not underlying_token_address:
                    messages.append(
                        f"Failed to resolve token address for {active_token.get('symbol', 'unknown')} on {chain_code}; skipping unlend"
                    )
                else:
                    # TODO: await favourable fees
                    status, message = await self.hyperlend_adapter.unlend(
                        underlying_token=underlying_token_address,
                        qty=int(amount_wei),
                        chain_id=int(self.hype_token_info.get("chain").get("id")),
                        native=False,
                    )
                    self._invalidate_assets_snapshot()
                self._invalidate_assets_snapshot()
            else:
                messages.append(
                    "No active HyperLend position found; sweeping idle balances."
                )
        else:
            messages.append("No HyperLend position detected; sweeping idle balances.")

        sweep_actions = await self._swap_residual_balances_to_token(
            self.usdt_token_info
        )

        # Get final balances in strategy wallet (don't transfer to main)
        total_usdt = 0.0
        try:
            _, total_usdt_wei = await self.balance_adapter.get_balance(
                token_id=self.usdt_token_info.get("token_id"),
                wallet_address=self._get_strategy_wallet_address(),
            )
            if total_usdt_wei and total_usdt_wei > 0:
                total_usdt = float(total_usdt_wei) / (
                    10 ** self.usdt_token_info.get("decimals", 18)
                )
        except Exception:
            pass

        total_hype = 0.0
        try:
            _, total_hype_wei = await self.balance_adapter.get_balance(
                token_id=self.hype_token_info.get("token_id"),
                wallet_address=self._get_strategy_wallet_address(),
            )
            if total_hype_wei and total_hype_wei > 0:
                total_hype = float(total_hype_wei) / (
                    10 ** self.hype_token_info.get("decimals", 18)
                )
        except Exception:
            pass

        if sweep_actions:
            messages.append(f"Residual sweeps: {'; '.join(sweep_actions)}.")

        balance_parts = []
        if total_usdt > 0:
            balance_parts.append(
                f"{total_usdt:.2f} {self.usdt_token_info.get('symbol')}"
            )
        if total_hype > 0:
            balance_parts.append(
                f"{total_hype:.4f} {self.hype_token_info.get('symbol')}"
            )

        if balance_parts:
            messages.append(f"Strategy wallet balance: {', '.join(balance_parts)}")

        self.current_token = None
        self.current_symbol = None
        self.current_avg_apy = 0.0
        self.kept_hype_tokens = 0.0

        strategy_address = self._get_strategy_wallet_address()
        messages.append(
            f"Call exit() to transfer funds from strategy wallet ({strategy_address}) to main wallet"
        )

        return (True, ". ".join(messages))

    async def exit(self, **kwargs) -> StatusTuple:
        self.logger.info("EXIT: Transferring remaining funds to main wallet")

        strategy_address = self._get_strategy_wallet_address()
        main_address = self._get_main_wallet_address()

        if strategy_address.lower() == main_address.lower():
            return (True, "Main wallet is strategy wallet, no transfer needed")

        transferred_items = []

        usdt_ok, usdt_raw = await self.balance_adapter.get_balance(
            token_id="usdt0-hyperevm",
            wallet_address=strategy_address,
        )
        if usdt_ok and usdt_raw:
            usdt_balance = float(usdt_raw) / 1e6  # USDT has 6 decimals
            if usdt_balance > 1.0:
                self.logger.info(
                    f"Transferring {usdt_balance:.2f} USDT0 to main wallet"
                )
                (
                    success,
                    msg,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    token_id="usdt0-hyperevm",
                    amount=usdt_balance,
                    strategy_name=self.name,
                    skip_ledger=False,
                )
                if success:
                    transferred_items.append(f"{usdt_balance:.2f} USDT0")
                else:
                    self.logger.warning(f"USDT0 transfer failed: {msg}")

        hype_ok, hype_raw = await self.balance_adapter.get_balance(
            token_id="hyperliquid-hyperevm",
            wallet_address=strategy_address,
        )
        if hype_ok and hype_raw:
            hype_balance = float(hype_raw) / 1e18  # HYPE has 18 decimals
            tx_fee_reserve = 0.1
            transferable_hype = hype_balance - tx_fee_reserve
            if transferable_hype > 0.01:
                self.logger.info(
                    f"Transferring {transferable_hype:.4f} HYPE to main wallet"
                )
                (
                    success,
                    msg,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    token_id="hyperliquid-hyperevm",
                    amount=transferable_hype,
                    strategy_name=self.name,
                    skip_ledger=False,
                )
                if success:
                    transferred_items.append(f"{transferable_hype:.4f} HYPE")
                else:
                    self.logger.warning(f"HYPE transfer failed: {msg}")

        if not transferred_items:
            return (True, "No funds to transfer to main wallet")

        return (True, f"Transferred to main wallet: {', '.join(transferred_items)}")

    async def _swap_residual_balances_to_token(
        self, token_info: dict[str, Any], include_native: bool = False
    ) -> list[str]:
        snapshot = await self._get_assets_snapshot(force_refresh=True)
        balances = await self._wallet_balances_from_snapshot(snapshot)
        if not balances:
            return []
        actions = []
        target_checksum = self._token_checksum(token_info)
        for checksum, entry in balances.items():
            if checksum == "native":
                if not include_native:
                    continue
            else:
                if checksum == target_checksum:
                    continue
                asset = entry.get("asset")
                if not asset or not asset.get("is_stablecoin"):
                    continue
            balance_wei = int(entry.get("wei") or 0)
            if balance_wei <= 0:
                continue
            token = entry.get("token")
            if not token:
                continue
            token["address"] = checksum
            min_amount = self._amount_to_wei(
                token, Decimal(str(self.MIN_STABLE_SWAP_TOKENS))
            )
            if balance_wei <= min_amount:
                continue

            try:
                swap_action = await self._execute_swap(
                    from_token_info=token,
                    to_token_info=token_info,
                    amount_wei=balance_wei,
                )
                if swap_action:
                    actions.append(swap_action)
                    continue
            except Exception:
                continue

            # TODO: untested past this point
            try:
                try:
                    decimals = int(token.get("decimals", 18))
                except (TypeError, ValueError):
                    decimals = 18
                amount_tokens = float(balance_wei) / (10**decimals)
                (
                    success,
                    message,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    token_id=token_info.get("token_id"),
                    amount=amount_tokens,
                    strategy_name=self.name,
                )
            except Exception:
                continue

            if success:
                actions.append(
                    f"Transferred {amount_tokens:.4f} {token.symbol} to main wallet"
                )
        if actions:
            self._invalidate_assets_snapshot()

        return actions

    async def update(self) -> StatusTuple:
        await self._hydrate_position_from_chain()

        redeploy_tokens = await self._estimate_redeploy_tokens()
        idle_tokens = await self._get_idle_tokens()

        if idle_tokens > 0.1:
            total_required = (redeploy_tokens or 0.0) + idle_tokens
            best_candidate = await self._select_best_stable_asset(
                required_underlying_tokens=total_required,
                operation="deposit",
                allow_rotation_without_current=False,
            )
            if best_candidate is None:
                token_symbol = (
                    self._display_symbol(getattr(self, "current_symbol", None))
                    if self.current_token
                    else "idle"
                )
                return (
                    True,
                    f"Idle balance ({idle_tokens:.4f} {token_symbol}) remains; no HyperLend market has sufficient capacity.",
                    False,
                )
            best_token, best_symbol, best_hourly = best_candidate
            target_apy = self._hourly_to_apy(best_hourly)
            reserve_wei = self.GAS_MAXIMUM * (
                10 ** self.hype_token_info.get("decimals")
            )
            display_symbol = self._display_symbol(best_symbol)

            (
                actions,
                total_target,
                _,
                kept_hype,
            ) = await self._allocate_to_target(
                best_token,
                target_symbol=display_symbol,
                target_apy=target_apy,
                hype_reserve_wei=reserve_wei,
                lend_operation="deposit",
            )
            message = (
                f"Redeployed idle funds into {display_symbol} (~{target_apy:.2%} APY). "
                f"Current lent balance {total_target:.4f} {display_symbol}."
            )
            if actions:
                message = f"{message} Actions: {'; '.join(actions)}."
            message = f"{message} HYPE buffer at {self.kept_hype_tokens:.2f} tokens."
            return (True, message, True)

        required_tokens = (
            redeploy_tokens if redeploy_tokens and redeploy_tokens > 0 else None
        )

        best_candidate = await self._select_best_stable_asset(
            required_underlying_tokens=required_tokens,
            operation="update",
            allow_rotation_without_current=True,
        )
        if best_candidate is None:
            return (True, "No optimal HyperLend market identified.", False)

        best_token, best_symbol, best_hourly = best_candidate
        target_apy = self._hourly_to_apy(best_hourly)
        current_checksum = self._token_checksum(self.current_token)
        best_checksum = self._token_checksum(best_token)
        display_symbol = self._display_symbol(best_symbol)
        self.symbol_display_map.setdefault(best_symbol, display_symbol)

        if current_checksum and best_checksum and current_checksum == best_checksum:
            message = (
                f"Maintained allocation in {display_symbol} (~{target_apy:.2%} APY)."
            )
            if redeploy_tokens:
                message = (
                    f"{message} Existing position remains optimal versus alternatives."
                )
            return (True, message, False)

        reserve_wei = self.GAS_MAXIMUM * (10 ** self.hype_token_info.get("decimals"))
        previous_apy = float(self.current_avg_apy or 0.0)
        delta_apy = target_apy - previous_apy if previous_apy else target_apy

        policy_mode = (
            self.rotation_policy
            if self.rotation_policy
            else self.ROTATION_POLICY.lower()
        )
        summary_df = (
            self.last_summary
            if isinstance(self.last_summary, pd.DataFrame)
            else pd.DataFrame()
        )
        hys_hours = max(
            1,
            int(
                self.hys_dwell_hours
                if self.hys_dwell_hours
                else self.HYSTERESIS_DWELL_HOURS
            ),
        )
        hys_z = float(self.hys_z if self.hys_z else self.HYSTERESIS_Z)
        rotation_tx_cost = float(
            self.rotation_tx_cost if self.rotation_tx_cost else self.ROTATION_TX_COST
        )

        short_circuit_triggered = (
            self.APY_SHORT_CIRCUIT_THRESHOLD is not None
            and delta_apy > self.APY_SHORT_CIRCUIT_THRESHOLD
        )
        deny_reasons = []
        rotation_reason = None
        should_rotate = False

        if short_circuit_triggered:
            should_rotate = True
            rotation_reason = f"Short-circuit triggered by {delta_apy:.2%} APY edge."
        elif policy_mode == "hysteresis":
            if summary_df.empty:
                deny_reasons.append(
                    "Hysteresis check skipped: no tournament summary available."
                )
            else:
                best_row_df = summary_df.loc[summary_df["asset"] == best_symbol]
                cur_row_df = (
                    summary_df.loc[summary_df["asset"] == self.current_symbol]
                    if self.current_symbol
                    else pd.DataFrame()
                )
                if best_row_df.empty:
                    deny_reasons.append(
                        f"Unable to locate {display_symbol} in tournament summary."
                    )
                else:
                    best_row = best_row_df.iloc[0]
                    best_E = float(best_row.get("mean", 0.0))
                    best_SD = float(best_row.get("std", 0.0))

                    if not cur_row_df.empty:
                        cur_row = cur_row_df.iloc[0]
                        cur_E = float(cur_row.get("mean", 0.0))
                        cur_SD = float(cur_row.get("std", 0.0))
                    else:
                        cur_hourly = float(
                            self._apy_to_hourly(previous_apy)
                            if previous_apy
                            else best_hourly
                        )
                        cur_E = math.log1p(cur_hourly) * self.HORIZON_HOURS
                        cur_SD = 0.0

                    edge_cum_log = best_E - cur_E
                    sigma_delta = math.sqrt(
                        max(0.0, best_SD * best_SD + cur_SD * cur_SD)
                    )
                    cost_log_mag = abs(math.log1p(-rotation_tx_cost))
                    amortized_cost = cost_log_mag * (
                        self.HORIZON_HOURS / max(1.0, float(hys_hours))
                    )
                    hurdle = amortized_cost + hys_z * sigma_delta

                    if edge_cum_log > hurdle:
                        should_rotate = True
                        rotation_reason = (
                            f"Hysteresis edge {edge_cum_log:.4f} > hurdle {hurdle:.4f}."
                        )
                    else:
                        deny_reasons.append(
                            f"Hysteresis band holds: edge {edge_cum_log:.4f} ≤ hurdle {hurdle:.4f}."
                        )
        else:
            rotation_allowed = True
            if previous_apy and delta_apy <= self.APY_REBALANCE_THRESHOLD:
                rotation_allowed = False
                deny_reasons.append(
                    f"APY improvement ({delta_apy:.2%}) below {self.APY_REBALANCE_THRESHOLD:.2%} threshold."
                )

            last_rotation = await self._get_last_rotation_time(
                wallet_address=self._get_strategy_wallet_address(),
            )
            cooldown_notice = None
            if rotation_allowed and last_rotation is not None:
                elapsed = timezone.now() - last_rotation
                if elapsed < self.ROTATION_COOLDOWN:
                    rotation_allowed = False
                    remaining_hours = max(
                        0, (self.ROTATION_COOLDOWN - elapsed).total_seconds() / 3600
                    )
                    cooldown_notice = (
                        f"Rotation cooldown active; ~{remaining_hours:.1f}h remaining."
                    )

            if rotation_allowed:
                should_rotate = True
                if previous_apy:
                    rotation_reason = (
                        f"APY edge {delta_apy:.2%} cleared threshold and cooldown."
                    )
                else:
                    rotation_reason = "Initial deployment into best-performing asset."
            else:
                if cooldown_notice:
                    deny_reasons.append(cooldown_notice)

        if not should_rotate:
            current_display = (
                self._display_symbol(self.current_symbol)
                if self.current_symbol
                else display_symbol
            )
            baseline_apy = previous_apy if previous_apy else target_apy
            message_parts = []
            if deny_reasons:
                message_parts.append("NO UPDATE was performed.")
                message_parts.append(" ".join(deny_reasons))

            if current_checksum:
                message_parts.append(
                    f"Maintained allocation in {current_display} (~{baseline_apy:.2%} APY)."
                )

            if best_checksum:
                message_parts.append(
                    f"The best symbol would be: {display_symbol} at ~{target_apy:.2%} APY."
                )
            if policy_mode == "hysteresis":
                message_parts.append(
                    f"Hysteresis parameters: dwell={hys_hours}h, z={hys_z:.2f}."
                )
            return (
                True,
                " ".join(part for part in message_parts if part).strip(),
                False,
            )

        actions, total_target, _, kept_hype = await self._allocate_to_target(
            best_token,
            target_symbol=display_symbol,
            target_apy=target_apy,
            hype_reserve_wei=reserve_wei,
            lend_operation="update",
        )
        self.kept_hype_tokens = kept_hype

        base_message = (
            f"Aligned supplies into {display_symbol} (~{target_apy:.2%} APY). "
            f"Current lent balance {total_target:.4f} {display_symbol}."
        )
        if rotation_reason:
            base_message = f"{base_message} {rotation_reason}"
        elif policy_mode == "hysteresis":
            base_message = f"{base_message} Hysteresis rotation with dwell={hys_hours}h, z={hys_z:.2f}."

        should_notify_user = False
        if actions:
            base_message = f"{base_message} Actions: {'; '.join(actions)}."
            should_notify_user = True
        else:
            base_message = f"{base_message} No rebalancing required."

        base_message = (
            f"{base_message} HYPE buffer at {self.kept_hype_tokens:.2f} tokens."
        )

        return (True, base_message, should_notify_user)

    async def _allocate_to_target(
        self,
        target_token: dict[str, Any],
        *,
        target_symbol: str,
        target_apy: float,
        hype_reserve_wei: int,
        lend_operation: Literal["deposit", "update"] = "update",
    ) -> tuple[list[dict[str, Any]], float, float, float]:
        actions = []
        actions.extend(await self._unwind_other_lends(target_token))

        align_actions, kept_hype = await self._align_wallet_balances(
            target_token, hype_reserve_wei=hype_reserve_wei
        )
        actions.extend(align_actions)

        lent_tokens = await self._lend_available_balance(
            target_token, operation=lend_operation
        )
        if lent_tokens > 0:
            actions.append(f"Lent {lent_tokens:.4f} {target_symbol}")

        try:
            target_checksum = Web3.to_checksum_address(
                self._get_token_address(target_token)
            )
        except Exception:
            target_checksum = None

        total_target = 0.0
        if target_checksum:
            new_positions = await self._get_lent_positions()
            total_target_wei = sum(
                entry["amount_wei"]
                for entry in new_positions.values()
                if Web3.to_checksum_address(entry["asset"]["underlying"])
                == target_checksum
            )
            total_target = float(total_target_wei) / (
                10 ** target_token.get("decimals", 18)
            )

        self.current_token = target_token
        self.current_symbol = target_token.get("symbol")
        self.current_avg_apy = target_apy
        self.kept_hype_tokens = kept_hype

        await self._hydrate_position_from_chain()

        return actions, total_target, lent_tokens, kept_hype

    async def _lend_available_balance(
        self,
        target_token: dict[str, Any],
        *,
        operation: Literal["deposit", "update"] = "update",
    ) -> float:
        if not self._get_token_address(target_token):
            return 0.0

        snapshot = await self._get_assets_snapshot(force_refresh=True)
        balances = await self._wallet_balances_from_snapshot(snapshot)

        target_checksum = self._token_checksum(target_token)
        if not target_checksum:
            return 0.0

        entry = balances.get(target_checksum)
        original_amount_wei = int(entry.get("wei") or 0) if entry else 0
        if original_amount_wei <= 0:
            return 0.0

        amount_wei = int(original_amount_wei)
        if operation == "deposit":
            amount_tokens = float(amount_wei) / (10 ** target_token.get("decimals"))
            has_headroom = await self._has_supply_cap_headroom(
                target_token, amount_tokens
            )
            if not has_headroom:
                return 0.0

        # TODO: await favourable fees
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                result, message = await self.hyperlend_adapter.lend(
                    underlying_token=target_token.get("address"),
                    chain_id=int(self.hype_token_info.get("chain").get("id")),
                    qty=amount_wei,
                    native=False,
                )
                if result:
                    self._invalidate_assets_snapshot()
                    amount_lent = amount_wei
                    break
            except Exception as e:
                message = str(e)
                if (
                    "panic code 0x11" in message
                    and amount_wei > 0
                    and attempt < max_attempts - 1
                ):
                    reduction = max(amount_wei // 10, 1)
                    amount_wei -= reduction
                    continue

                return 0.0

        if amount_lent <= 0:
            return 0.0

        return float(amount_lent) / (10 ** target_token.get("decimals"))

    async def _align_wallet_balances(
        self, target_token: dict[str, Any], *, hype_reserve_wei: int
    ) -> tuple[list[dict[str, Any]], float]:
        snapshot = await self._get_assets_snapshot(force_refresh=True)
        balances = await self._wallet_balances_from_snapshot(snapshot)
        if not balances:
            return [], 0.0

        actions = []
        target_checksum = self._token_checksum(target_token)
        hype_checksum = self._token_checksum(self.hype_token_info)

        for checksum, entry in balances.items():
            if checksum == "native":
                continue
            if checksum == target_checksum or checksum == hype_checksum:
                continue
            asset = entry.get("asset")
            if not asset or not asset.get("is_stablecoin"):
                continue
            token = entry.get("token")
            if not token or not isinstance(token, dict):
                continue
            token["address"] = checksum

            entry_decimals = entry.get("decimals")
            if entry_decimals is not None:
                token["decimals"] = entry_decimals
            balance_wei = int(entry.get("wei") or 0)
            if balance_wei <= 0:
                continue

            min_token_swap_wei = self._amount_to_wei(
                token, Decimal(str(self.MIN_STABLE_SWAP_TOKENS))
            )
            if balance_wei <= min_token_swap_wei:
                continue

            swap_action = await self._execute_swap(
                from_token_info=token,
                to_token_info=target_token,
                amount_wei=balance_wei,
                slippage=DEFAULT_SLIPPAGE,
            )
            if swap_action:
                actions.append(swap_action)
                continue

            balance_tokens = float(entry.get("tokens") or 0)
            if balance_tokens > 0:
                logger.warning(
                    f"Failed to swap {balance_tokens:.4f} {token.get('symbol', 'unknown')} "
                    f"to {target_token.get('symbol', 'target')} during alignment. "
                    f"Leaving in strategy wallet for next update cycle."
                )

        kept_tokens = float(balances.get("native", {}).get("tokens") or 0)

        return actions, kept_tokens

    async def _unwind_other_lends(
        self, target_token: dict[str, Any]
    ) -> list[dict[str, Any]]:
        positions = await self._get_lent_positions()
        if not positions:
            return []

        actions = []
        try:
            target_checksum = self._token_checksum(target_token)
        except Exception:
            return actions

        for address, entry in positions.items():
            token = entry.get("token")
            amount_wei = entry.get("amount_wei", 0)
            if not token or amount_wei <= 0:
                continue

            try:
                checksum = Web3.to_checksum_address(address)
            except Exception:
                continue
            if checksum == target_checksum:
                continue
            try:
                chain_code = self.hype_token_info.get("chain", {}).get(
                    "code", "hyperevm"
                )
                underlying_token_address = self._get_token_address(token, chain_code)
                if not underlying_token_address:
                    continue
                # TODO: await favourable fees
                await self.hyperlend_adapter.unlend(
                    underlying_token=underlying_token_address,
                    qty=int(amount_wei),
                    chain_id=int(self.hype_token_info.get("chain").get("id")),
                    native=False,
                )
                self._invalidate_assets_snapshot()
                human = float(amount_wei) / (10 ** token.get("decimals"))
                actions.append(
                    f"Unwound {human:.4f} {token.get('symbol')} from HyperLend"
                )
            except Exception:
                continue

        return actions

    async def _get_last_rotation_time(self, wallet_address: str) -> datetime | None:
        success, data = await self.ledger_adapter.get_strategy_latest_transactions(
            wallet_address=self._get_strategy_wallet_address(),
        )
        if success is False:
            return None
        for transaction in data.get("transactions", []):
            op_data = transaction.get("op_data", {})
            if op_data.get("type") in {"LEND", "SWAP"}:
                created_str = transaction.get("created")
                if not created_str:
                    continue
                try:
                    dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    return dt
                except (ValueError, AttributeError):
                    continue
        return None

    async def _select_best_stable_asset(
        self,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        *,
        required_underlying_tokens=None,
        operation: Literal["deposit", "update", "quote"] = "update",
        exclude_addresses=None,
        allow_rotation_without_current=False,
    ) -> dict[str, Any] | None:
        excluded = (
            {addr.lower() for addr in exclude_addresses}
            if exclude_addresses
            else set[Any]()
        )
        current_token = self.current_token
        current_symbol = self.current_symbol

        current_checksum_value = (
            self._token_checksum(current_token) if current_token else None
        )
        current_checksum_lower = (
            current_checksum_value.lower() if current_checksum_value else None
        )
        current_excluded = current_checksum_lower and current_checksum_lower in excluded
        allow_current_fallback = (
            operation != "deposit"
            and current_token is not None
            and not current_excluded
        )

        _, stable_markets = await self.hyperlend_adapter.get_stable_markets(
            required_underlying_tokens=required_underlying_tokens,
            buffer_bps=self.SUPPLY_CAP_BUFFER_BPS,
            min_buffer_tokens=self.SUPPLY_CAP_MIN_BUFFER_TOKENS,
        )
        if isinstance(stable_markets, str):
            stable_markets = {"notes": [], "markets": {}}
        filtered_notes = stable_markets.get("notes", [])
        filtered_map = stable_markets.get("markets", {})

        if excluded:
            pruned = {}
            for addr, entry in filtered_map.items():
                try:
                    checksum = Web3.to_checksum_address(addr)
                except Exception:
                    checksum = addr
                if str(checksum).lower() in excluded:
                    continue
                pruned[addr] = entry
            filtered_map = pruned

        if (
            allow_rotation_without_current
            and current_token
            and current_checksum_lower
            and not current_excluded
        ):
            existing_addresses = set[str]()
            for addr in filtered_map.keys():
                try:
                    existing_addresses.add(Web3.to_checksum_address(addr).lower())
                except Exception:
                    existing_addresses.add(str(addr).lower())
            if current_checksum_lower not in existing_addresses:
                try:
                    _, current_entry = await self.hyperlend_adapter.get_market_entry(
                        token=current_checksum_value,
                    )
                except Exception:
                    current_entry = None
                if current_entry:
                    filtered_map[current_token.get("address")] = current_entry
                    filtered_notes.append(
                        f"Included capped market {current_symbol or current_token.get('symbol') or 'unknown'} for rotation comparison."
                    )

        if not filtered_map:
            if filtered_notes:
                truncated = "; ".join(filtered_notes[:3])
                if len(filtered_notes) > 3:
                    truncated += f"{truncated} ..."
            if allow_current_fallback and current_token:
                fallback_symbol = current_symbol or current_token.get("symbol")
                fallback_hourly = self._apy_to_hourly(
                    float(self.current_avg_apy or 0.0)
                )
                if not current_token.get("address"):
                    if not current_checksum_value:
                        return None
                    current_token["address"] = current_checksum_value
                return (current_token, fallback_symbol, fallback_hourly)
            return None

        self.symbol_display_map = {}
        filtered = []
        for addr, entry in filtered_map.items():
            symbol_canonical = entry.get("symbol_canonical")
            if not symbol_canonical:
                raw_symbol = entry.get("symbol") or entry.get("display_symbol")
                symbol_canonical = (
                    self._normalize_symbol(raw_symbol) if raw_symbol else None
                )
            if not symbol_canonical:
                continue
            display_symbol = (
                entry.get("display_symbol") or entry.get("symbol") or symbol_canonical
            )
            self.symbol_display_map[symbol_canonical] = str(display_symbol)
            filtered.append((addr, symbol_canonical))

        histories = await asyncio.gather(
            *[
                self.hyperlend_adapter.get_lend_rate_history(
                    token=addr,
                    lookback_hours=lookback_hours,
                )
                for addr, _ in filtered
            ],
            return_exceptions=True,
        )

        records = []
        symbol_map = {}
        for (addr, symbol), history in zip(filtered, histories, strict=False):
            label = symbol or addr
            symbol_map[label] = addr
            if isinstance(history, Exception):
                self.logger.warning(
                    f"Exception fetching rate history for {label} ({addr}): {history}"
                )
                continue
            history_status = history[0]
            if not history_status:
                continue
            history_data = history[1]
            if isinstance(history_data, str):
                continue
            for row in history_data.get("history", []):
                ts_ms = row.get("timestamp_ms")
                if ts_ms is None:
                    continue
                apr = row.get("supply_apr")
                apy = row.get("supply_apy")
                rate_hourly = None
                if isinstance(apr, (int, float)) and not math.isnan(apr):
                    rate_hourly = np.expm1(np.log1p(apr) / (365 * 24))
                elif isinstance(apy, (int, float)) and not math.isnan(apy):
                    rate_hourly = (1.0 + apy) ** (1 / (365 * 24)) - 1.0
                records.append(
                    {
                        "timestamp": pd.to_datetime(ts_ms, unit="ms", utc=True),
                        "asset": label,
                        "supplyAPR": apr,
                        "rate_hourly": rate_hourly,
                    }
                )
        if not records:
            self.last_summary = None
            self.last_dominance = None
            self.last_samples = None
            return None

        rates_df = pd.DataFrame(records)
        try:
            wide = self._prep_rates(rates_df)
        except Exception as e:
            self.logger.error(f"Error preparing rates: {e}")
            self.last_summary = None
            self.last_dominance = None
            self.last_samples = None
            return None

        if wide.empty or wide.shape[1] == 0:
            self.last_summary = None
            self.last_dominance = None
            self.last_samples = None
            return None

        if self.TOURNAMENT_MODE == "joint":
            summary, dominance, samples = self._tournament(
                wide,
                horizon_h=self.HORIZON_HOURS,
                block_len=self.BLOCK_LEN,
                trials=self.TRIALS,
                halflife_days=self.HALFLIFE_DAYS,
                seed=self.SEED,
            )
        else:
            summary, dominance, samples = self._tournament_independent(
                wide,
                horizon_h=self.HORIZON_HOURS,
                block_len=self.BLOCK_LEN,
                trials=self.TRIALS,
                halflife_days=self.HALFLIFE_DAYS,
                seed=self.SEED,
            )

        self.last_summary = summary
        self.last_dominance = dominance
        self.last_samples = samples

        if summary.empty:
            if allow_current_fallback and current_token:
                fallback_hourly = self._apy_to_hourly(
                    float(self.current_avg_apy or 0.0)
                )
                fallback_symbol = current_symbol or current_token.get("symbol")
                fallback_address = (
                    current_token.get("address") or current_checksum_value
                )
                if not fallback_address:
                    return None
                if not current_token.get("address"):
                    current_token["address"] = fallback_address
                return (current_token, fallback_symbol, fallback_hourly)
            return None

        max_candidates = min(self.MAX_CANDIDATES, len(summary))
        for i in range(max_candidates):
            top_row = summary.iloc[i]
            top_symbol = top_row["asset"]

            current_candidate = None
            if current_symbol:
                current_row_df = summary.loc[summary["asset"] == current_symbol]
                if not current_row_df.empty:
                    current_candidate = await self._make_candidate(
                        current_symbol,
                        current_row_df.iloc[0],
                        symbol_map,
                        current_token,
                        current_symbol,
                    )
                elif allow_current_fallback and current_token:
                    hourly = self._apy_to_hourly(float(self.current_avg_apy or 0.0))
                    if not current_token.get("address"):
                        if not current_checksum_value:
                            current_candidate = None
                        else:
                            current_token["address"] = current_checksum_value
                            current_candidate = (current_token, current_symbol, hourly)
                    else:
                        current_candidate = (current_token, current_symbol, hourly)

            can_rotate = True
            if (
                required_underlying_tokens is not None
                and required_underlying_tokens > 0
                and current_symbol
                and current_candidate is not None
            ):
                current_row_df = summary.loc[summary["asset"] == current_symbol]
                if current_row_df.empty:
                    if allow_current_fallback and (
                        current_excluded or allow_rotation_without_current
                    ):
                        current_p = 0.0
                    else:
                        can_rotate = False
                        current_p = 0.0
                else:
                    current_p = float(current_row_df.iloc[0].get("p_best", 0.0) or 0.0)
                if can_rotate:
                    top_p = float(top_row.get("p_best", 0.0) or 0.0)
                    if current_p > 0:
                        can_rotate = top_p > max(
                            current_p, self.P_BEST_ROTATION_THRESHOLD
                        )
                    else:
                        can_rotate = top_p > self.P_BEST_ROTATION_THRESHOLD
            if not can_rotate:
                return current_candidate

            candidate = await self._make_candidate(
                top_symbol, top_row, symbol_map, current_token, current_symbol
            )

            if candidate:
                return candidate
        return current_candidate

    async def _make_candidate(
        self,
        symbol: str,
        row: pd.Series,
        symbol_map: dict[str, str],
        current_token: dict[str, Any] | None = None,
        current_symbol: str | None = None,
    ):
        address = symbol_map.get(symbol)
        token = None
        if address:
            try:
                chain_id = None
                try:
                    chain_id = int((self.hype_token_info.get("chain") or {}).get("id"))
                except Exception:
                    chain_id = None
                success, token = await self.token_adapter.get_token(
                    address.lower(), chain_id=chain_id
                )
            except Exception:
                token = None
            if not success or isinstance(token, str):
                token = None
        if token is None and current_token is None and symbol == current_symbol:
            token = current_token
        if not token or isinstance(token, str):
            return None
        if not address:
            address = token.get("address") if token else None
        if not address:
            return None
        token["address"] = address
        hourly_rate = self._log_yield_to_hourly(float(row.get("mean", 0.0) or 0.0))
        return (token, symbol, hourly_rate)

    def _prep_rates(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._coerce_rates_df(df).copy()
        df["ts"] = pd.to_datetime(df["timestamp"]).dt.floor("h")
        wide = (
            df.pivot_table(
                index="ts", columns="asset", values="rate_hourly", aggfunc="mean"
            )
            .sort_index()
            .dropna(axis=0, how="any")
        )
        return wide

    def _coerce_rates_df(self, df: pd.DataFrame) -> pd.DataFrame:
        g = df.copy()
        if "timestamp" not in g.columns:
            if "ts" in g.columns:
                g = g.rename(columns={"ts": "timestamp"})
            else:
                raise KeyError("Expected a 'timestamp' column.")
        if "asset" not in g.columns:
            if "symbol" in g.columns:
                g = g.rename(columns={"symbol": "asset"})
            else:
                raise KeyError("Expected an 'asset' (or 'symbol') column.")
        if "rate_hourly" not in g.columns:
            if "supplyAPR" in g.columns:
                g["rate_hourly"] = np.expm1(np.log1p(g["supplyAPR"]) / (365 * 24))
            elif "supplyAPY" in g.columns:
                g["rate_hourly"] = (1.0 + g["supplyAPY"]) ** (1 / (365 * 24)) - 1.0
            else:
                raise KeyError(
                    "Need 'rate_hourly' or one of 'supplyAPR'/'supplyAPY' to derive it."
                )
        g["timestamp"] = pd.to_datetime(g["timestamp"], utc=True)
        return g

    def _tournament(
        self,
        wide: pd.DataFrame,
        horizon_h: int = None,
        block_len: int | None = None,
        trials: int | None = None,
        halflife_days: float | None = None,
        seed: int | None = None,
    ):
        if horizon_h is None:
            horizon_h = self.HORIZON_HOURS
        if block_len is None:
            block_len = self.BLOCK_LEN
        if trials is None:
            trials = self.TRIALS
        if halflife_days is None:
            halflife_days = self.HALFLIFE_DAYS
        if seed is None:
            seed = self.SEED

        wide = wide.copy()
        assets = wide.columns.to_list()
        arr = wide.values

        w = self.recency_weights(wide.index, halflife_days=halflife_days)
        rng = np.random.default_rng(seed=seed)
        A = arr.shape[1]

        wins = np.zeros(A, dtype=int)
        all_trial_log_returns = np.empty((trials, A))
        for t in range(trials):
            horizon_log_returns = self.sample_sequence_block_bootstrap(
                arr, horizon_h, block_len, start_weights=w, rng=rng
            )
            all_trial_log_returns[t] = horizon_log_returns
            wins[np.argmax(horizon_log_returns)] += 1

        p_best = wins / trials
        q05 = np.quantile(all_trial_log_returns, 0.05, axis=0)
        mean = all_trial_log_returns.mean(axis=0)
        std = all_trial_log_returns.std(axis=0)

        dom = np.zeros((A, A))
        for i in range(A):
            for j in range(A):
                if i == j:
                    continue
                dom[i, j] = np.mean(
                    all_trial_log_returns[:, i] > all_trial_log_returns[:, j]
                )

        summary = pd.DataFrame(
            {
                "asset": assets,
                "p_best": p_best,
                "q05": q05,
                "mean": mean,
                "std": std,
            }
        ).sort_values(
            ["p_best", "q05", "mean"],
            ascending=[False, False, False],
        )

        dominance = pd.DataFrame(dom, index=assets, columns=assets)
        return summary, dominance, all_trial_log_returns

    def _tournament_independent(
        self,
        wide: pd.DataFrame,
        horizon_h: int = None,
        block_len: int | None = None,
        trials: int | None = None,
        halflife_days: float | None = None,
        seed: int | None = None,
    ):
        if horizon_h is None:
            horizon_h = self.HORIZON_HOURS
        if block_len is None:
            block_len = self.BLOCK_LEN
        if trials is None:
            trials = self.TRIALS
        if halflife_days is None:
            halflife_days = self.HALFLIFE_DAYS
        if seed is None:
            seed = self.SEED

        wide = wide.copy()
        assets = wide.columns.to_list()
        arr = wide.values
        w = self.recency_weights(wide.index, halflife_days=halflife_days)
        rng = np.random.default_rng(seed=seed)
        A = arr.shape[1]

        wins = np.zeros(A, dtype=int)
        all_trial_log_returns = np.empty((trials, A), dtype=float)

        for t in range(trials):
            for i in range(A):
                all_trial_log_returns[t, i] = self.sample_seq_independent(
                    arr[:, i], horizon_h, block_len, start_weights=w, rng=rng
                )
            wins[np.argmax(all_trial_log_returns[t])] += 1

        p_best = wins / trials
        q05 = np.quantile(all_trial_log_returns, 0.05, axis=0)
        mean = all_trial_log_returns.mean(axis=0)
        std = all_trial_log_returns.std(axis=0)

        dom = np.zeros((A, A), dtype=float)
        for i in range(A):
            for j in range(A):
                if i == j:
                    continue
                dom[i, j] = float(
                    np.mean(all_trial_log_returns[:, i] > all_trial_log_returns[:, j])
                )

        summary = pd.DataFrame(
            {
                "asset": assets,
                "p_best": p_best,
                "q05": q05,
                "mean": mean,
                "std": std,
            }
        ).sort_values(
            ["p_best", "q05", "mean"],
            ascending=[False, False, False],
        )

        dominance = pd.DataFrame(dom, index=assets, columns=assets)
        return summary, dominance, all_trial_log_returns

    @staticmethod
    def sample_sequence_block_bootstrap(
        arr: np.ndarray,
        horizon_h: int = HORIZON_HOURS,
        block_len: int = BLOCK_LEN,
        start_weights: np.ndarray | None = None,
        rng: np.random.Generator | None = None,
    ) -> np.ndarray:
        if rng is None:
            rng = np.random.default_rng()

        T, _ = arr.shape
        max_start = T - block_len
        if max_start < 0:
            raise ValueError("Not enough rows for chosen block length.")

        starts = np.arange(max_start + 1)
        if start_weights is None:
            probs = np.ones_like(starts, dtype=float) / (max_start + 1)
        else:
            probs = start_weights[: max_start + 1].astype(float)
            probs /= probs.sum()

        picked = []
        need = horizon_h
        while need > 0:
            s = rng.choice(starts, p=probs)
            picked.append(arr[s : s + block_len])
            need -= block_len

        seq = np.vstack(picked)[:horizon_h]
        return np.sum(np.log1p(seq), axis=0)

    @staticmethod
    def sample_seq_independent(
        col: np.ndarray,
        horizon_h: int = HORIZON_HOURS,
        block_len: int = BLOCK_LEN,
        start_weights: np.ndarray | None = None,
        rng: np.random.Generator | None = None,
    ) -> np.ndarray:
        if rng is None:
            rng = np.random.default_rng()
        T = len(col)
        max_start = T - block_len
        if max_start < 0:
            raise ValueError("Not enough rows for chosen BLOCK_LEN.")

        starts = np.arange(max_start + 1)
        probs = start_weights[: max_start + 1].astype(float)
        probs /= probs.sum()

        picked: list[np.ndarray] = []
        need = horizon_h
        while need > 0:
            s = rng.choice(starts, p=probs)
            picked.append(col[s : s + block_len])
            need -= block_len
        seq = np.concatenate(picked)[:horizon_h]
        return float(np.sum(np.log1p(seq)))

    def recency_weights(self, index: pd.DatetimeIndex, halflife_days) -> np.ndarray:
        ages_h = ((index.max() - index) / pd.Timedelta(hours=1)).to_numpy()
        lam = math.log(2) / (halflife_days * 24.0)
        w = np.exp(-lam * ages_h)
        total = w.sum()

        if total == 0:
            return np.ones_like(w) / len(w)
        return w / total

    @staticmethod
    def _apy_to_hourly(apy: float) -> float:
        return (1.0 + apy) ** (1 / (365 * 24)) - 1 if apy is not None else 0.0

    @staticmethod
    def _hourly_to_apy(rate_hourly: float) -> float:
        return (1.0 + rate_hourly) ** (365 * 24) - 1.0

    @staticmethod
    def _log_yield_to_hourly(log_yield: float, horizon_h: int = 6) -> float:
        return float(np.expm1(log_yield / max(horizon_h, 1)))

    async def _get_idle_tokens(self) -> float:
        snapshot = await self._get_assets_snapshot()
        balances = await self._wallet_balances_from_snapshot(snapshot)
        if not balances:
            return 0.0

        idle_total = 0.0
        token = self.current_token
        current_checksum = None
        if token and token.get("address"):
            current_checksum = self._token_checksum(token)

        for checksum, entry in balances.items():
            if checksum == "native":
                continue
            include_balance = False
            if current_checksum and checksum == current_checksum:
                include_balance = True
            else:
                asset = entry.get("asset") or {}
                symbol = (
                    (asset or {}).get("symbol")
                    or (asset or {}).get("symbol_display")
                    or ""
                )
                if self._is_stable_symbol(symbol):
                    include_balance = True
            if not include_balance:
                continue
            idle_total += float(entry.get("tokens") or 0.0)
        return idle_total

    async def _wallet_balances_from_snapshot(
        self, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        balances = {}
        assets = snapshot.get("assets", [])
        if assets:
            for asset in assets:
                checksum = asset.get("underlying_checksum") or asset.get("underlying")
                if not checksum:
                    continue
                try:
                    checksum = Web3.to_checksum_address(checksum)
                except Exception:
                    continue

                try:
                    chain_id = None
                    try:
                        chain_id = int(
                            (self.hype_token_info.get("chain") or {}).get("id")
                        )
                    except Exception:
                        chain_id = None
                    success, token = await self.token_adapter.get_token(
                        checksum, chain_id=chain_id
                    )
                    if not success or not isinstance(token, dict):
                        continue
                except Exception:
                    continue

                raw_balance_wei = asset.get("underlying_wallet_balance_wei")
                try:
                    token_decimals = token.get("decimals") or token.get("decimal")
                    asset_decimals = asset.get("decimals") or asset.get("decimal")
                    if token_decimals is not None:
                        decimals = int(token_decimals)
                    elif asset_decimals is not None:
                        decimals = int(asset_decimals)
                    else:
                        decimals = 18
                except (TypeError, ValueError):
                    decimals = 18
                scale = 10**decimals
                if raw_balance_wei is not None:
                    try:
                        balance_wei = int(raw_balance_wei)
                    except (TypeError, ValueError):
                        balance_wei = None
                else:
                    balance_wei = None
                if balance_wei is None:
                    balance_decimal_input = Decimal(
                        str(asset.get("underlying_wallet_balance") or 0.0)
                    )
                    balance_wei = int(balance_decimal_input * scale).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                if balance_wei < 0:
                    balance_wei = 0
                balance_decimal = (
                    Decimal(balance_wei) / scale if balance_wei else Decimal(0.0)
                )
                balance_tokens = float(balance_decimal) if balance_decimal else 0.0
                if balance_tokens > 0.0:
                    float_decimal = Decimal.from_float(balance_tokens)
                    if float_decimal > balance_decimal:
                        balance_tokens = math.nextafter(balance_tokens, 0.0)
                        while (
                            balance_tokens > 0.0
                            and Decimal.from_float(balance_tokens) > balance_decimal
                        ):
                            balance_tokens = math.nextafter(balance_tokens, 0.0)
                price = float(asset.get("price_usd") or 0.0)
                balances[checksum] = {
                    "token": token,
                    "address": checksum,
                    "wei": int(balance_wei),
                    "tokens": balance_tokens,
                    "usd": balance_tokens * price,
                    "asset": asset,
                    "decimals": decimals,
                }
        return balances

    async def quote(self, deposit_amount: float | None = None) -> dict:
        # Try to get APY from current position first
        current_apy = self.current_avg_apy or 0.0
        current_asset = (
            self._display_symbol(self.current_symbol) if self.current_symbol else None
        )

        # If we have tournament results, include best candidate info
        best_apy = None
        best_asset = None
        if self.last_summary is not None and not self.last_summary.empty:
            top = self.last_summary.iloc[0]
            best_asset_raw = str(top.get("asset"))
            expected_hourly = self._log_yield_to_hourly(
                float(top.get("E_cum_log_yield", 0.0))
            )
            best_apy = self._hourly_to_apy(expected_hourly)
            best_asset = self._display_symbol(best_asset_raw)

        # Use best available APY for the quote
        expected_apy = best_apy if best_apy is not None else current_apy
        display_asset = best_asset if best_asset else current_asset

        components = {
            "current_apy": current_apy,
            "current_asset": current_asset,
        }
        if best_apy is not None:
            components["best_candidate_apy"] = best_apy
            components["best_candidate_asset"] = best_asset

        summary = f"Expected APY: {expected_apy * 100:.2f}%"
        if display_asset:
            summary += f" on {display_asset}"

        return {
            "expected_apy": float(expected_apy),
            "apy_type": "gross",
            "confidence": "medium" if best_apy is not None else "low",
            "methodology": "Bootstrap tournament analysis of HyperLend stablecoin markets over 6-hour horizon",
            "components": components,
            "deposit_amount": deposit_amount,
            "as_of": datetime.now(UTC).isoformat(),
            "summary": summary,
        }

    async def _status(self) -> StatusDict:
        if not self.current_token:
            await self._hydrate_position_from_chain()
        _, net_deposit = await self.ledger_adapter.get_strategy_net_deposit(
            wallet_address=self._get_strategy_wallet_address()
        )
        snapshot = await self._get_assets_snapshot()
        lent_positions = await self._get_lent_positions(snapshot)
        asset_map = (
            snapshot.get("_by_underlying", {}) if isinstance(snapshot, dict) else {}
        )
        wallet_balances = await self._wallet_balances_from_snapshot(snapshot)
        position_rows = []
        total_usd = 0.0
        for entry in lent_positions.values():
            token = entry.get("token")
            if not token:
                continue
            amount_wei = entry.get("amount_wei", 0)
            amount = float(amount_wei) / (10 ** token.get("decimals", 18))
            asset = entry.get("asset")
            if not asset:
                checksum = self._token_checksum(token)
                asset = asset_map.get(checksum) if checksum else None
            if asset and asset.get("price_usd") is not None:
                try:
                    price = float(asset.get("price_usd"))
                except (TypeError, ValueError):
                    price = 0.0
            apy = float(asset.get("supply_apy") or 0.0) if asset else 0.0
            display_symbol = asset.get("symbol_display") if asset else token.symbol
            if asset and asset.get("supply_usd") is not None:
                try:
                    balance_usd = float(asset.get("supply_usd"))
                except (TypeError, ValueError):
                    balance_usd = amount * price
            else:
                balance_usd = amount * price
            total_usd += balance_usd
            position_rows.append(
                {
                    "asset": display_symbol,
                    "balance": amount,
                    "apy": apy,
                    "balance_usd": balance_usd,
                }
            )

        (
            success,
            strategy_hype_balance_wei,
        ) = await self.balance_adapter.get_balance(
            token_id=self.hype_token_info.get("token_id"),
            wallet_address=self._get_strategy_wallet_address(),
        )
        hype_price = asset_map.get(WRAPPED_HYPE_ADDRESS, {}).get("price_usd") or 0.0
        hype_value = 0.0
        if hype_price and success:
            hype_value = (
                strategy_hype_balance_wei
                / (10 ** self.hype_token_info.get("decimals"))
                * hype_price
            )

        idle_value = 0.0
        idle_tokens = 0.0
        if self.current_token:
            current_checksum = self._token_checksum(self.current_token)
            entry = wallet_balances.get(current_checksum) if current_checksum else None
            if entry:
                idle_tokens = entry.get("tokens") or 0.0
                asset = asset_map.get(current_checksum) if current_checksum else None
                if asset and asset.get("price_usd") is not None:
                    try:
                        idle_price = float(asset.get("price_usd"))
                    except (TypeError, ValueError):
                        idle_price = 1.0
                    idle_value = idle_tokens * idle_price
        excludes = {
            self._token_checksum(entry["token"]) for entry in lent_positions.values()
        }
        if self.current_token:
            excludes.add(self._token_checksum(self.current_token))
        remaining_tokens = [
            (value["token"].get("asset_id"), value["usd"])
            for addr, value in wallet_balances.items()
            if addr not in excludes
        ]
        remaining_usd = sum([usd for _, usd in remaining_tokens])
        total_portfolio_value = total_usd + idle_value + remaining_usd

        status_payload: dict[str, Any] = {
            "lent_asset": self.current_symbol,
            "lent_balance": 0.0,
            "current_apy": float(self.current_avg_apy or 0.0),
            "positions": position_rows,
            "hype_buffer_tokens": strategy_hype_balance_wei
            / (10 ** self.hype_token_info.get("decimals")),
            "hype_buffer_usd": hype_value,
            "idle_tokens": idle_tokens,
            "idle_usd": idle_value,
            "other_tokens": remaining_tokens,
            "other_balance_usd": remaining_usd,
            "rebalance_threshold": self.APY_REBALANCE_THRESHOLD,
            "short_circuit_threshold": self.APY_SHORT_CIRCUIT_THRESHOLD,
            "rotation_cooldown_hours": self.ROTATION_COOLDOWN.total_seconds() / 3600,
        }

        if position_rows:
            current_row = next(
                (row for row in position_rows if row["asset"] == self.current_symbol),
                position_rows[0],
            )
            status_payload["lent_asset"] = self._display_symbol(
                self.current_symbol or current_row["asset"]
            )
            status_payload["lent_balance"] = current_row["balance"]
            status_payload["current_apy"] = current_row["apy"]

        if self.current_token:
            status_payload["current_asset_address"] = self.current_token.get("address")

        if self.last_summary is not None and not self.last_summary.empty:
            top = self.last_summary.iloc[0]
            best_asset = str(top.get("asset"))
            expected_hourly = self._log_yield_to_hourly(
                float(top.get("E_cum_log_yield", 0.0))
            )
            status_payload.update(
                {
                    "best_candidate": self._display_symbol(best_asset),
                    "best_candidate_expected_apy": self._hourly_to_apy(expected_hourly),
                }
            )

        return {
            "portfolio_value": total_portfolio_value,
            "net_deposit": net_deposit,
            "strategy_status": status_payload,
            "gas_available": strategy_hype_balance_wei
            / (10 ** self.hype_token_info.get("decimals")),
            "gassed_up": self.GAS_MAXIMUM / 3
            <= strategy_hype_balance_wei / (10 ** self.hype_token_info.get("decimals")),
        }

    @staticmethod
    async def policies() -> list[str]:
        return [
            any_hyperliquid_l1_payload(),
            any_hyperliquid_user_payload(),
            hypecore_sentinel_deposit(),
            await whype_deposit_and_withdraw(),
            erc20_spender_for_any_token(HYPERLEND_POOL),
            await hyperlend_supply_and_withdraw(),
            erc20_spender_for_any_token(ENSO_ROUTER),
            await enso_swap(),
            erc20_spender_for_any_token(PRJX_ROUTER),
            await prjx_swap(),
            erc20_spender_for_any_token(LIFI_ROUTERS[999]),
            await lifi_swap(999),
        ]
