from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from wayfinder_paths.adapters.avantis_adapter.adapter import AvantisAdapter
from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.boros_adapter import BorosAdapter, BorosVault
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.strategies.descriptors import (
    Complexity,
    Directionality,
    Frequency,
    StratDescriptor,
    TokenExposure,
    Volatility,
)
from wayfinder_paths.core.strategies.Strategy import (
    QuoteResult,
    StatusDict,
    StatusTuple,
    Strategy,
)
from wayfinder_paths.core.utils.units import from_erc20_raw, to_erc20_raw

USDC_ARB = "usd-coin-arbitrum"
USDC_BASE = "usd-coin-base"
USDT_ARB = "usdt0-arbitrum"
ETH_ARB = "ethereum-arbitrum"
ETH_BASE = "ethereum-base"

DEFAULT_HLP_VAULT_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
MIN_NET_DEPOSIT = 40.0
MIN_HLP_USD = 11.0
MIN_BOROS_USD = 11.0
EPS = 1e-6


@dataclass
class Inventory:
    usdc_arb_idle: float
    usdc_base_idle: float
    usdt_arb_idle: float
    eth_arb_idle: float
    eth_base_idle: float
    hlp_equity: float
    hlp_wait_ms: int | None
    hlp_in_cooldown: bool
    hlp_withdrawable_now: float
    hl_perp_idle: float
    avantis_value_usdc: float
    boros_vault_value_usd: float
    boros_vault_reported_value_usd: float
    boros_account_idle_usd: float
    boros_vaults: list[BorosVault]
    positions_value: float
    unallocated_total: float
    total_value: float


class MultiVaultSplitStrategy(Strategy):
    name = "Multi Vault Split Strategy"

    INFO = StratDescriptor(
        description=(
            "Splits Arbitrum USDC across three perp-dex aligned vault legs: "
            "Hyperliquid HLP, Boros AMM vaults, and Avantis avUSDC on Base. "
            "The strategy deploys fresh capital, rolls expired Boros vaults, and "
            "best-effort unwinds back to Arbitrum USDC on withdraw."
        ),
        summary=(
            "Diversified USDC vault allocation across Hyperliquid HLP, Boros vaults, "
            "and Avantis avUSDC."
        ),
        risk_description=(
            "Smart contract, bridge, and venue risks across Hyperliquid, Boros, "
            "Avantis, and BRAP routing. HLP withdrawals can be cooldown-gated and "
            "Boros withdrawals can require a later finalize step."
        ),
        gas_token_symbol="ETH",
        gas_token_id=ETH_ARB,
        deposit_token_id=USDC_ARB,
        minimum_net_deposit=MIN_NET_DEPOSIT,
        gas_maximum=0.02,
        gas_threshold=0.003,
        volatility=Volatility.MEDIUM,
        volatility_description=(
            "Stablecoin-denominated but diversified across multiple yield venues."
        ),
        directionality=Directionality.MARKET_NEUTRAL,
        directionality_description=(
            "No intended directional exposure; returns come from vault yields."
        ),
        complexity=Complexity.MEDIUM,
        complexity_description=(
            "Coordinates cross-chain bridging plus Boros and Hyperliquid vault flows."
        ),
        token_exposure=TokenExposure.STABLECOINS,
        token_exposure_description=(
            "Primarily USDC/USDT exposure across multiple market-neutral venues."
        ),
        frequency=Frequency.LOW,
        frequency_description=(
            "Update on demand or periodically to deploy idle cash and roll Boros maturities."
        ),
        return_drivers=["HLP vault yield", "Boros LP yield", "Avantis vault yield"],
        config={
            "deposit": {
                "parameters": {
                    "main_token_amount": {
                        "type": "float",
                        "description": "USDC amount on Arbitrum to deposit",
                        "minimum": MIN_NET_DEPOSIT,
                    },
                    "gas_token_amount": {
                        "type": "float",
                        "description": "Optional Arbitrum ETH top-up for strategy gas",
                        "minimum": 0.0,
                        "maximum": 0.02,
                    },
                }
            },
            "strategy": {
                "allocation_mode": "hybrid_apy | fixed",
                "weights": {"hlp": 0.3333, "boros": 0.3333, "avantis": 0.3334},
                "enabled_legs": {"hlp": True, "boros": True, "avantis": True},
                "hlp_vault_address": DEFAULT_HLP_VAULT_ADDRESS,
                "boros_token_id": 3,
                "boros_allow_isolated_only_vaults": True,
            },
        },
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        main_wallet: dict[str, Any] | None = None,
        strategy_wallet: dict[str, Any] | None = None,
        main_wallet_signing_callback=None,
        strategy_wallet_signing_callback=None,
        strategy_sign_typed_data=None,
    ) -> None:
        super().__init__(
            config=config,
            main_wallet_signing_callback=main_wallet_signing_callback,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
            strategy_sign_typed_data=strategy_sign_typed_data,
        )

        merged = dict(config or {})
        if main_wallet is not None:
            merged["main_wallet"] = main_wallet
        if strategy_wallet is not None:
            merged["strategy_wallet"] = strategy_wallet
        self.config = merged

        self.cached_apys: dict[str, float] = {}
        self.boros_has_capacity = True

        strat_addr = (self.config.get("strategy_wallet") or {}).get("address")
        main_addr = (self.config.get("main_wallet") or {}).get("address")
        adapter_config = {
            "main_wallet": self.config.get("main_wallet") or None,
            "strategy_wallet": self.config.get("strategy_wallet") or None,
            "strategy": self.config,
            "boros_adapter": self.config.get("boros_adapter") or {},
        }

        self.balance_adapter = BalanceAdapter(
            adapter_config,
            main_sign_callback=self.main_wallet_signing_callback,
            strategy_sign_callback=self.strategy_wallet_signing_callback,
            main_wallet_address=main_addr,
            strategy_wallet_address=strat_addr,
        )
        self.brap_adapter = BRAPAdapter(
            adapter_config,
            sign_callback=self.strategy_wallet_signing_callback,
            wallet_address=strat_addr,
        )
        self.boros_adapter = BorosAdapter(
            config=adapter_config,
            sign_callback=self.strategy_wallet_signing_callback,
            wallet_address=strat_addr,
        )
        self.avantis_adapter = AvantisAdapter(
            config=adapter_config,
            sign_callback=self.strategy_wallet_signing_callback,
            wallet_address=strat_addr,
        )
        self.hyperliquid_adapter = HyperliquidAdapter(
            config=self.config,
            sign_callback=self.strategy_wallet_signing_callback,
            sign_typed_data_callback=self.strategy_sign_typed_data,
            wallet_address=strat_addr,
        )

        self.hlp_vault_address = str(
            self.config.get("hlp_vault_address") or DEFAULT_HLP_VAULT_ADDRESS
        )
        self.boros_token_id = int(self.config.get("boros_token_id") or 3)
        self.boros_allow_isolated_only_vaults = bool(
            self.config.get("boros_allow_isolated_only_vaults", True)
        )
        self.allocation_mode = str(
            self.config.get("allocation_mode") or "hybrid_apy"
        ).lower()
        self.fixed_weights = dict(self.config.get("weights") or {})
        self.enabled_legs = {
            "hlp": bool((self.config.get("enabled_legs") or {}).get("hlp", True)),
            "boros": bool((self.config.get("enabled_legs") or {}).get("boros", True)),
            "avantis": bool(
                (self.config.get("enabled_legs") or {}).get("avantis", True)
            ),
        }
        self.apy_overrides = dict(self.config.get("apy_overrides") or {})
        self.usdt_address: str | None = None

    async def setup(self) -> None:
        try:
            usdt = await TOKEN_CLIENT.get_token_details(USDT_ARB)
            self.usdt_address = str(usdt.get("address")) if usdt else None
        except Exception:
            self.usdt_address = None

    @staticmethod
    async def policies() -> list[str]:
        return []

    @property
    def strategy_wallet_address(self) -> str:
        return self._get_strategy_wallet_address()

    def _hlp_enabled(self) -> bool:
        return bool(
            self.enabled_legs.get("hlp")
            and self.hlp_vault_address
            and getattr(self.hyperliquid_adapter, "_exchange", None) is not None
        )

    def _boros_enabled(self) -> bool:
        return bool(self.enabled_legs.get("boros"))

    def _avantis_enabled(self) -> bool:
        return bool(self.enabled_legs.get("avantis"))

    @staticmethod
    def _format_boros_vault_view(vault: BorosVault) -> dict[str, Any]:
        return {
            "market_id": int(vault.market_id),
            "amm_id": int(vault.amm_id),
            "symbol": str(vault.symbol or ""),
            "apy": float(vault.apy or 0.0) if vault.apy is not None else None,
            "collateral": vault.collateral_symbol,
            "expiry": vault.expiry,
            "tvl": vault.tvl,
            "tvl_usd": vault.tvl_usd,
            "available": vault.available_tokens,
            "available_usd": vault.available_usd,
            "deposited": vault.user_deposit_tokens,
            "deposited_usd": vault.user_deposit_usd,
            "is_expired": bool(vault.is_expired),
            "is_isolated_only": bool(vault.is_isolated_only),
        }

    async def _pick_boros_vault_for_deposit(
        self,
        *,
        amount_tokens: float,
        allow_isolated_only: bool | None = None,
    ) -> tuple[BorosVault | None, float | None]:
        if allow_isolated_only is None:
            allow_isolated_only = self.boros_allow_isolated_only_vaults

        ok, user_vaults = await self.boros_adapter.get_vaults_summary(
            account=self.strategy_wallet_address
        )
        if ok and isinstance(user_vaults, list):
            for vault in user_vaults:
                if not self.boros_adapter.is_vault_open_for_deposit(
                    vault,
                    allow_isolated_only=allow_isolated_only,
                ):
                    continue
                if self.boros_adapter.estimate_user_vault_value_tokens(vault) < 1.0:
                    continue
                capacity = self.boros_adapter.estimate_vault_capacity_tokens(vault)
                if capacity is not None and capacity < MIN_BOROS_USD:
                    continue
                return vault, capacity

        ok, best = await self.boros_adapter.best_yield_vault(
            token_id=self.boros_token_id,
            amount_tokens=float(amount_tokens),
            min_tenor_days=3.0,
            allow_isolated_only=allow_isolated_only,
        )
        if not ok:
            return None, None
        if best is None or not isinstance(best, BorosVault):
            return None, None
        return best, self.boros_adapter.estimate_vault_capacity_tokens(best)

    async def _fetch_apys(
        self,
        *,
        deposit_amount_usdc: float | None = None,
    ) -> dict[str, float]:
        apy_hlp = float(self.apy_overrides.get("hlp") or 0.0)
        if self._hlp_enabled():
            ok, hlp = await self.hyperliquid_adapter.get_hlp_apys(
                self.hlp_vault_address
            )
            if ok and isinstance(hlp, dict):
                apy_hlp = float(hlp.get("apy7d") or apy_hlp or 0.0)

        apy_avantis = float(self.apy_overrides.get("avantis") or 0.0)
        if self._avantis_enabled():
            ok, avantis = await self.avantis_adapter.fetch_trailing_apy()
            if ok and isinstance(avantis, dict):
                apy_avantis = float(
                    avantis.get("jr_apy") or avantis.get("sr_apy") or apy_avantis or 0.0
                )

        apy_boros = float(self.apy_overrides.get("boros") or 0.0)
        self.boros_has_capacity = False
        if self._boros_enabled():
            probe_amount = float(deposit_amount_usdc or 1000.0)
            best, capacity = await self._pick_boros_vault_for_deposit(
                amount_tokens=probe_amount
            )
            if best is not None and best.apy is not None:
                apy_boros = float(best.apy)
                self.boros_has_capacity = True
                if capacity is not None and capacity < MIN_BOROS_USD:
                    self.boros_has_capacity = False

        self.cached_apys = {
            "apy_hlp": apy_hlp,
            "apy_boros": apy_boros if self.boros_has_capacity else 0.0,
            "apy_avantis": apy_avantis,
        }
        return dict(self.cached_apys)

    def _compute_weights(
        self,
        apys: dict[str, float],
        *,
        total_value: float,
    ) -> tuple[float, float, float]:
        enabled = {
            "hlp": self._hlp_enabled(),
            "boros": self._boros_enabled() and self.boros_has_capacity,
            "avantis": self._avantis_enabled(),
        }
        viable = {
            "hlp": enabled["hlp"]
            and (total_value < 1 or total_value / 3 >= MIN_HLP_USD),
            "boros": enabled["boros"]
            and (total_value < 1 or total_value / 3 >= MIN_BOROS_USD),
            "avantis": enabled["avantis"],
        }
        active = [leg for leg, is_active in viable.items() if is_active]
        if not active:
            return 0.0, 0.0, 1.0

        if self.allocation_mode == "fixed":
            provided = {
                leg: float(self.fixed_weights.get(leg) or 0.0)
                for leg in ("hlp", "boros", "avantis")
                if viable.get(leg)
            }
            total = sum(provided.values())
            if total > 0:
                return (
                    float(provided.get("hlp", 0.0) / total),
                    float(provided.get("boros", 0.0) / total),
                    float(provided.get("avantis", 0.0) / total),
                )

        stable_per_venue = 0.5 / len(active)
        apy_values = {
            leg: max(float(apys.get(f"apy_{leg}") or 0.0), 0.0) for leg in active
        }
        total_apy = sum(apy_values.values())
        if total_apy <= 0:
            apy_weights = {leg: 0.5 / len(active) for leg in active}
        else:
            apy_weights = {leg: (apy_values[leg] / total_apy) * 0.5 for leg in active}

        weights = {
            leg: (stable_per_venue + apy_weights.get(leg, 0.0))
            if viable.get(leg)
            else 0.0
            for leg in ("hlp", "boros", "avantis")
        }
        return weights["hlp"], weights["boros"], weights["avantis"]

    async def _get_inventory(self) -> Inventory:
        ok, arb_usdc_raw = await self.balance_adapter.get_vault_wallet_balance(USDC_ARB)
        ok_base, base_usdc_raw = await self.balance_adapter.get_vault_wallet_balance(
            USDC_BASE
        )
        ok_usdt, usdt_raw = await self.balance_adapter.get_vault_wallet_balance(
            USDT_ARB
        )
        ok_eth_arb, eth_arb_raw = await self.balance_adapter.get_vault_wallet_balance(
            ETH_ARB
        )
        ok_eth_base, eth_base_raw = await self.balance_adapter.get_vault_wallet_balance(
            ETH_BASE
        )

        usdc_arb_idle = from_erc20_raw(arb_usdc_raw if ok else 0, 6)
        usdc_base_idle = from_erc20_raw(base_usdc_raw if ok_base else 0, 6)
        usdt_arb_idle = from_erc20_raw(usdt_raw if ok_usdt else 0, 6)
        eth_arb_idle = from_erc20_raw(eth_arb_raw if ok_eth_arb else 0, 18)
        eth_base_idle = from_erc20_raw(eth_base_raw if ok_eth_base else 0, 18)

        hlp_equity = 0.0
        hlp_wait_ms: int | None = None
        hlp_in_cooldown = False
        hlp_withdrawable_now = 0.0
        hl_perp_idle = 0.0
        if self._hlp_enabled():
            ok_hlp, hlp_status = await self.hyperliquid_adapter.get_hlp_status(
                self.hlp_vault_address,
                user_address=self.strategy_wallet_address,
            )
            if ok_hlp and isinstance(hlp_status, dict):
                hlp_equity = float(hlp_status.get("equity") or 0.0)
                hlp_wait_ms = int(hlp_status.get("wait_ms") or 0)
                hlp_in_cooldown = bool(hlp_status.get("in_cooldown"))
                hlp_withdrawable_now = float(hlp_status.get("withdrawable_now") or 0.0)

            ok_state, hl_state = await self.hyperliquid_adapter.get_user_state(
                self.strategy_wallet_address
            )
            if ok_state and isinstance(hl_state, dict):
                hl_perp_idle = float(
                    self.hyperliquid_adapter.get_perp_margin_amount(hl_state)
                )

        avantis_value_usdc = 0.0
        ok_pos, pos = await self.avantis_adapter.position(
            account=self.strategy_wallet_address
        )
        if ok_pos and isinstance(pos, dict):
            avantis_value_usdc = float(pos.get("value_usdc") or 0.0)

        boros_vaults: list[BorosVault] = []
        boros_vault_value_usd = 0.0
        boros_vault_reported_value_usd = 0.0
        ok_vaults, vaults = await self.boros_adapter.get_vaults_summary(
            account=self.strategy_wallet_address
        )
        if ok_vaults and isinstance(vaults, list):
            boros_vaults = list(vaults)
            boros_vault_reported_value_usd = sum(
                self.boros_adapter.estimate_user_vault_value_usd(vault)
                or self.boros_adapter.estimate_user_vault_value_tokens(vault)
                for vault in boros_vaults
            )
            # Preserve existing allocation semantics; expose marked USD separately.
            boros_vault_value_usd = sum(
                self.boros_adapter.estimate_user_vault_value_tokens(vault)
                for vault in boros_vaults
            )

        boros_account_idle_usd = 0.0
        ok_idle, boros_idle = await self.boros_adapter.get_account_idle_balance(
            token_id=self.boros_token_id,
            account_id=0,
        )
        if ok_idle and isinstance(boros_idle, (int, float)):
            boros_account_idle_usd = float(boros_idle)

        positions_value = hlp_equity + avantis_value_usdc + boros_vault_value_usd
        unallocated_total = usdc_arb_idle + usdc_base_idle + hl_perp_idle
        total_value = (
            positions_value + unallocated_total + usdt_arb_idle + boros_account_idle_usd
        )

        return Inventory(
            usdc_arb_idle=usdc_arb_idle,
            usdc_base_idle=usdc_base_idle,
            usdt_arb_idle=usdt_arb_idle,
            eth_arb_idle=eth_arb_idle,
            eth_base_idle=eth_base_idle,
            hlp_equity=hlp_equity,
            hlp_wait_ms=hlp_wait_ms,
            hlp_in_cooldown=hlp_in_cooldown,
            hlp_withdrawable_now=hlp_withdrawable_now,
            hl_perp_idle=hl_perp_idle,
            avantis_value_usdc=avantis_value_usdc,
            boros_vault_value_usd=boros_vault_value_usd,
            boros_vault_reported_value_usd=boros_vault_reported_value_usd,
            boros_account_idle_usd=boros_account_idle_usd,
            boros_vaults=boros_vaults,
            positions_value=positions_value,
            unallocated_total=unallocated_total,
            total_value=total_value,
        )

    async def _ensure_base_gas(
        self,
        *,
        min_base_eth: float = 0.0004,
        topup_base_eth: float = 0.0015,
    ) -> tuple[bool, str]:
        inv = await self._get_inventory()
        if inv.eth_base_idle >= min_base_eth:
            return True, f"Base gas sufficient ({inv.eth_base_idle:.6f} ETH)"

        needed = max(float(topup_base_eth), float(min_base_eth) - inv.eth_base_idle)
        if inv.eth_arb_idle + EPS < needed:
            top_up = needed - inv.eth_arb_idle
            (
                ok,
                detail,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                ETH_ARB,
                top_up,
                strategy_name=self.__class__.__name__,
                skip_ledger=True,
            )
            if not ok:
                return False, f"Failed to top up Arbitrum ETH: {detail}"

        ok, result = await self.brap_adapter.swap_from_token_ids(
            from_token_id=ETH_ARB,
            to_token_id=ETH_BASE,
            from_address=self.strategy_wallet_address,
            amount=str(to_erc20_raw(needed, 18)),
            slippage=0.003,
        )
        if not ok:
            return False, f"Base gas bridge failed: {result}"

        arrived = await self.balance_adapter.wait_for_balance(
            token_id=ETH_BASE,
            min_balance=max(min_base_eth, inv.eth_base_idle + needed * 0.8),
            timeout_seconds=180,
        )
        if arrived < min_base_eth:
            return False, "Base gas bridge did not settle in time"
        return True, f"Base gas topped up to {arrived:.6f} ETH"

    async def _move_idle_to_avantis(self, amount_usdc: float) -> tuple[bool, str]:
        if amount_usdc <= EPS or not self._avantis_enabled():
            return True, "Avantis disabled or no amount"

        ok_gas, gas_msg = await self._ensure_base_gas()
        if not ok_gas:
            return False, gas_msg

        inv = await self._get_inventory()
        use_base = min(inv.usdc_base_idle, amount_usdc)
        deposited = 0.0

        if use_base > EPS:
            ok, detail = await self.avantis_adapter.deposit(
                amount=to_erc20_raw(use_base, 6)
            )
            if not ok:
                return False, f"Avantis deposit failed: {detail}"
            deposited += use_base
            amount_usdc -= use_base

        if amount_usdc <= EPS:
            return True, f"Deposited {deposited:.2f} USDC to Avantis"

        before = inv.usdc_base_idle - use_base
        ok, result = await self.brap_adapter.swap_from_token_ids(
            from_token_id=USDC_ARB,
            to_token_id=USDC_BASE,
            from_address=self.strategy_wallet_address,
            amount=str(to_erc20_raw(amount_usdc, 6)),
            slippage=0.005,
        )
        if not ok:
            return False, f"USDC bridge to Base failed: {result}"

        arrived = await self.balance_adapter.wait_for_balance(
            token_id=USDC_BASE,
            min_balance=before + amount_usdc * 0.8,
            timeout_seconds=240,
        )
        bridged = max(0.0, arrived - before)
        if bridged <= EPS:
            return False, "No bridged Base USDC arrived for Avantis deposit"

        ok, detail = await self.avantis_adapter.deposit(amount=to_erc20_raw(bridged, 6))
        if not ok:
            return False, f"Avantis deposit after bridge failed: {detail}"
        deposited += bridged
        return True, f"Deposited {deposited:.2f} USDC to Avantis"

    async def _move_idle_to_hlp(self, amount_usdc: float) -> tuple[bool, str]:
        if amount_usdc <= EPS or not self._hlp_enabled():
            return True, "HLP disabled or no amount"
        if amount_usdc < MIN_HLP_USD:
            return True, f"HLP amount below minimum ({amount_usdc:.2f} < {MIN_HLP_USD})"

        # Hyperliquid SDK rejects floats with >6 decimal places
        amount_usdc = math.floor(amount_usdc * 1e6) / 1e6

        ok, detail = await self.hyperliquid_adapter.send_usdc_to_bridge(
            amount_usdc,
            address=self.strategy_wallet_address,
        )
        if not ok:
            return False, f"Hyperliquid bridge send failed: {detail}"

        ok_wait, _ = await self.hyperliquid_adapter.wait_for_deposit(
            self.strategy_wallet_address,
            amount_usdc,
            timeout_s=180,
        )
        if not ok_wait:
            return False, "Hyperliquid deposit not observed in time"

        ok_hlp, hlp_detail = await self.hyperliquid_adapter.deposit_hlp(
            amount_usdc,
            self.hlp_vault_address,
        )
        if not ok_hlp:
            return False, f"HLP vault deposit failed: {hlp_detail}"
        return True, f"Deposited {amount_usdc:.2f} USDC to HLP"

    async def _deposit_usdt_to_boros_vault(
        self,
        *,
        market_id: int,
        amount_native: int,
        is_isolated_only: bool = False,
    ) -> tuple[bool, str]:
        if amount_native <= 0:
            return True, "No USDT to deposit"

        collateral_address = self.usdt_address
        if not collateral_address:
            return False, "USDT address not configured"

        deposit_margin = (
            self.boros_adapter.deposit_to_isolated_margin
            if is_isolated_only
            else self.boros_adapter.deposit_to_cross_margin
        )
        ok_dep, dep_res = await deposit_margin(
            collateral_address=collateral_address,
            amount_wei=int(amount_native),
            token_id=self.boros_token_id,
            market_id=int(market_id),
        )
        if not ok_dep:
            return False, f"Boros collateral deposit failed: {dep_res}"

        scaled_cash = await self.boros_adapter.unscaled_to_scaled_cash_wei(
            self.boros_token_id,
            int(amount_native),
        )
        ok_vault, vault_res = await self.boros_adapter.deposit_to_vault(
            market_id=int(market_id),
            net_cash_in_wei=int(scaled_cash),
        )
        if not ok_vault:
            return False, f"Boros vault deposit failed: {vault_res}"
        return True, f"Deposited {from_erc20_raw(amount_native, 6):.2f} USDT to Boros"

    async def _move_idle_to_boros(self, amount_usdc: float) -> tuple[bool, str]:
        if amount_usdc <= EPS or not self._boros_enabled():
            return True, "Boros disabled or no amount"

        inv = await self._get_inventory()
        total_target = amount_usdc + inv.usdt_arb_idle
        if total_target < MIN_BOROS_USD:
            return True, f"Boros amount below minimum ({total_target:.2f})"

        best, capacity = await self._pick_boros_vault_for_deposit(
            amount_tokens=total_target
        )
        if best is None:
            self.boros_has_capacity = False
            return False, "No Boros vault with sufficient capacity"

        deposit_cap = total_target
        if capacity is not None:
            deposit_cap = min(deposit_cap, capacity)
        if deposit_cap < MIN_BOROS_USD:
            self.boros_has_capacity = False
            return False, "Boros vault capacity is below the minimum deposit"

        used_usdt = min(inv.usdt_arb_idle, deposit_cap)
        deposited = 0.0
        if used_usdt > EPS:
            ok, detail = await self._deposit_usdt_to_boros_vault(
                market_id=best.market_id,
                amount_native=to_erc20_raw(used_usdt, 6),
                is_isolated_only=bool(best.is_isolated_only),
            )
            if not ok:
                return False, detail
            deposited += used_usdt

        remaining = max(0.0, deposit_cap - deposited)
        if remaining > EPS:
            ok, result = await self.brap_adapter.swap_from_token_ids(
                from_token_id=USDC_ARB,
                to_token_id=USDT_ARB,
                from_address=self.strategy_wallet_address,
                amount=str(to_erc20_raw(remaining, 6)),
                slippage=0.005,
            )
            if not ok:
                return False, f"USDC->USDT swap failed: {result}"

            inv_after = await self._get_inventory()
            new_usdt = max(0.0, inv_after.usdt_arb_idle)
            use_native = min(
                to_erc20_raw(new_usdt, 6),
                to_erc20_raw(remaining, 6),
            )
            ok, detail = await self._deposit_usdt_to_boros_vault(
                market_id=best.market_id,
                amount_native=use_native,
                is_isolated_only=bool(best.is_isolated_only),
            )
            if not ok:
                return False, detail
            deposited += from_erc20_raw(use_native, 6)

        return True, f"Deposited {deposited:.2f} USDT to Boros vault {best.symbol}"

    async def _deploy_boros_account_idle(self) -> tuple[bool, str]:
        ok_bal, balances = await self.boros_adapter.get_account_balances(
            token_id=self.boros_token_id,
            account_id=0,
        )
        if not ok_bal or not isinstance(balances, dict):
            return False, f"Failed to read Boros balances: {balances}"

        total = float(balances.get("total") or 0.0)
        if total <= 1.0:
            return True, "No Boros account idle balance"

        isolated_positions = balances.get("isolated_positions") or []
        for isolated in isolated_positions:
            market_id = isolated.get("market_id")
            balance_wei = isolated.get("balance_wei")
            if market_id is None or not balance_wei:
                continue
            await self.boros_adapter.cash_transfer(
                market_id=int(market_id),
                amount_wei=int(balance_wei),
                is_deposit=False,
            )

        ok_bal, balances = await self.boros_adapter.get_account_balances(
            token_id=self.boros_token_id,
            account_id=0,
        )
        if not ok_bal or not isinstance(balances, dict):
            return False, f"Failed to refresh Boros balances: {balances}"

        best, capacity = await self._pick_boros_vault_for_deposit(
            amount_tokens=total,
            allow_isolated_only=False,
        )
        if best is None:
            return False, "No Boros vault available for redeploy"

        cross_wei = int(balances.get("cross_wei") or 0)
        deposit_wei = cross_wei
        if capacity is not None:
            deposit_wei = min(deposit_wei, to_erc20_raw(capacity, 18))
        if deposit_wei <= 0:
            return True, "No cross-margin Boros cash to deploy"

        ok, detail = await self.boros_adapter.deposit_to_vault(
            market_id=best.market_id,
            net_cash_in_wei=int(deposit_wei),
        )
        if not ok:
            return False, f"Failed to redeploy Boros account idle: {detail}"
        return True, f"Redeployed Boros idle cash into {best.symbol}"

    async def _roll_expired_boros_vaults(self) -> list[str]:
        inv = await self._get_inventory()
        messages: list[str] = []
        now_s = int(time.time())
        for vault in inv.boros_vaults:
            lp_wei = self.boros_adapter.estimate_user_lp_balance_wei(vault)
            is_expired = bool(
                vault.is_expired
                or (vault.maturity_ts is not None and int(vault.maturity_ts) <= now_s)
            )
            if not lp_wei or not is_expired:
                continue
            ok, detail = await self.boros_adapter.withdraw_from_vault(
                market_id=vault.market_id,
                lp_to_remove_wei=int(lp_wei),
            )
            if ok:
                messages.append(f"Rolled expired Boros vault {vault.symbol}")
            else:
                messages.append(f"Failed to roll {vault.symbol}: {detail}")

        if messages:
            ok, detail = await self._deploy_boros_account_idle()
            if ok and detail:
                messages.append(detail)
        return messages

    async def _deploy_new_deposit(self, inv: Inventory) -> tuple[bool, str]:
        deployable = inv.unallocated_total + inv.usdt_arb_idle
        if deployable <= EPS:
            return True, "No idle capital to deploy"

        apys = await self._fetch_apys(deposit_amount_usdc=deployable)
        total_portfolio = inv.total_value if inv.positions_value > EPS else deployable
        w_hlp, w_boros, w_avantis = self._compute_weights(
            apys,
            total_value=total_portfolio,
        )
        target = {
            "hlp": total_portfolio * w_hlp,
            "boros": total_portfolio * w_boros,
            "avantis": total_portfolio * w_avantis,
        }
        current = {
            "hlp": inv.hlp_equity,
            "boros": inv.boros_vault_value_usd
            + inv.boros_account_idle_usd
            + inv.usdt_arb_idle,
            "avantis": inv.avantis_value_usdc,
        }
        gaps = {leg: max(target[leg] - current[leg], 0.0) for leg in target}
        total_gap = sum(gaps.values())
        if total_gap > EPS:
            alloc = {leg: deployable * gaps[leg] / total_gap for leg in gaps}
        else:
            alloc = {
                "hlp": deployable * w_hlp,
                "boros": deployable * w_boros,
                "avantis": deployable * w_avantis,
            }

        messages: list[str] = []
        for leg, mover in (
            ("avantis", self._move_idle_to_avantis),
            ("hlp", self._move_idle_to_hlp),
            ("boros", self._move_idle_to_boros),
        ):
            amount = float(alloc.get(leg) or 0.0)
            if amount <= EPS:
                continue
            ok, detail = await mover(amount)
            messages.append(detail)
            if not ok:
                logger.warning(f"{leg} deployment failed: {detail}")

        return True, "; ".join(messages) if messages else "No deployment needed"

    async def _complete_pending_withdrawal(self, inv: Inventory) -> tuple[bool, str]:
        if inv.usdt_arb_idle <= 0.5:
            return True, "No pending Boros withdrawal to complete"

        ok, result = await self.brap_adapter.swap_from_token_ids(
            from_token_id=USDT_ARB,
            to_token_id=USDC_ARB,
            from_address=self.strategy_wallet_address,
            amount=str(to_erc20_raw(inv.usdt_arb_idle, 6)),
            slippage=0.005,
        )
        if not ok:
            return False, f"USDT->USDC completion swap failed: {result}"

        usdc_arb = await self.balance_adapter.wait_for_balance(
            token_id=USDC_ARB,
            min_balance=0.5,
            timeout_seconds=180,
        )
        if usdc_arb > EPS:
            (
                ok,
                detail,
            ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                USDC_ARB,
                usdc_arb,
                strategy_name=self.__class__.__name__,
            )
            if not ok:
                return False, f"Failed to return USDC to main wallet: {detail}"
        return (
            True,
            "Completed pending Boros withdrawal and returned USDC to main wallet",
        )

    async def deposit(
        self,
        main_token_amount: float | None = None,
        gas_token_amount: float = 0.0,
        **_: Any,
    ) -> StatusTuple:
        t0 = time.monotonic()

        def _elapsed() -> str:
            return f"[{time.monotonic() - t0:.1f}s]"

        amount = float(main_token_amount or self.INFO.minimum_net_deposit)
        if amount < self.INFO.minimum_net_deposit:
            return False, f"Minimum deposit is {self.INFO.minimum_net_deposit:.2f} USDC"

        logger.info(
            f"{_elapsed()} deposit: amount={amount:.2f} USDC, gas={gas_token_amount}"
        )

        if gas_token_amount > 0:
            logger.info(
                f"{_elapsed()} deposit: moving {gas_token_amount} ETH gas to strategy wallet"
            )
            (
                ok,
                detail,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                ETH_ARB,
                float(gas_token_amount),
                strategy_name=self.__class__.__name__,
                skip_ledger=True,
            )
            if not ok:
                return False, f"Failed to move Arbitrum ETH gas: {detail}"
            logger.info(f"{_elapsed()} deposit: gas transfer ok")

        logger.info(
            f"{_elapsed()} deposit: moving {amount:.2f} USDC to strategy wallet"
        )
        (
            ok,
            detail,
        ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
            USDC_ARB,
            amount,
            strategy_name=self.__class__.__name__,
        )
        if not ok:
            return False, f"Failed to move deposit into strategy wallet: {detail}"
        logger.info(f"{_elapsed()} deposit: USDC transfer ok, calling update()")

        return await self.update()

    async def update(self) -> StatusTuple:
        t0 = time.monotonic()

        def _elapsed() -> str:
            return f"[{time.monotonic() - t0:.1f}s]"

        messages: list[str] = []
        logger.info(f"{_elapsed()} update: fetching inventory")
        inv = await self._get_inventory()
        logger.info(
            f"{_elapsed()} update: inventory — "
            f"positions_value={inv.positions_value:.2f}, "
            f"usdt_arb_idle={inv.usdt_arb_idle:.2f}, "
            f"usdc_arb_idle={inv.usdc_arb_idle:.2f}, "
            f"usdc_base_idle={inv.usdc_base_idle:.2f}, "
            f"unallocated_total={inv.unallocated_total:.2f}, "
            f"boros_account_idle_usd={inv.boros_account_idle_usd:.2f}, "
            f"hlp_equity={inv.hlp_equity:.2f}, "
            f"avantis_value_usdc={inv.avantis_value_usdc:.2f}, "
            f"boros_vault_value_usd={inv.boros_vault_value_usd:.2f}"
        )

        if inv.positions_value < 0.5 and inv.usdt_arb_idle > 0.5:
            logger.info(
                f"{_elapsed()} update: no positions but USDT idle, completing pending withdrawal"
            )
            ok, detail = await self._complete_pending_withdrawal(inv)
            messages.append(detail)
            logger.info(
                f"{_elapsed()} update: pending withdrawal ok={ok} detail={detail}"
            )
            return ok, "; ".join(messages)

        logger.info(f"{_elapsed()} update: checking Boros withdrawal status")
        ok_withdraw, status = await self.boros_adapter.get_user_withdrawal_status(
            token_id=self.boros_token_id
        )
        if (
            ok_withdraw
            and isinstance(status, dict)
            and int(status.get("start") or 0) > 0
        ):
            logger.info(f"{_elapsed()} update: finalizing Boros withdrawal")
            ok, detail = await self.boros_adapter.finalize_vault_withdrawal(
                token_id=self.boros_token_id
            )
            if ok:
                messages.append("Finalized pending Boros withdrawal")
            else:
                messages.append(f"Boros finalize not ready: {detail}")
            logger.info(f"{_elapsed()} update: Boros finalize ok={ok}")

        logger.info(f"{_elapsed()} update: rolling expired Boros vaults")
        rolled = await self._roll_expired_boros_vaults()
        messages.extend(rolled)
        logger.info(f"{_elapsed()} update: rolled {len(rolled)} vaults")

        logger.info(f"{_elapsed()} update: re-fetching inventory for Boros idle check")
        inv = await self._get_inventory()
        logger.info(
            f"{_elapsed()} update: boros_account_idle_usd={inv.boros_account_idle_usd:.2f}, boros_enabled={self._boros_enabled()}"
        )
        if inv.boros_account_idle_usd > 1.0 and self._boros_enabled():
            logger.info(f"{_elapsed()} update: deploying Boros account idle")
            ok, detail = await self._deploy_boros_account_idle()
            messages.append(detail)
            logger.info(
                f"{_elapsed()} update: deploy Boros idle ok={ok} detail={detail}"
            )
            if not ok:
                logger.warning(detail)

        logger.info(f"{_elapsed()} update: re-fetching inventory for new deposit check")
        inv = await self._get_inventory()
        deployable = inv.unallocated_total + inv.usdt_arb_idle
        logger.info(
            f"{_elapsed()} update: unallocated_total={inv.unallocated_total:.2f}, usdt_arb_idle={inv.usdt_arb_idle:.2f}, deployable={deployable:.2f}"
        )
        if deployable > EPS:
            logger.info(f"{_elapsed()} update: deploying new deposit")
            ok, detail = await self._deploy_new_deposit(inv)
            messages.append(detail)
            logger.info(f"{_elapsed()} update: deploy ok={ok} detail={detail}")
            if not ok:
                return False, "; ".join([m for m in messages if m])

        cleaned = [message for message in messages if message]
        logger.info(
            f"{_elapsed()} update: complete — {'; '.join(cleaned) if cleaned else 'no action'}"
        )
        return True, "; ".join(cleaned) if cleaned else "No action needed"

    async def withdraw(self, **kwargs: Any) -> StatusTuple:
        del kwargs
        messages: list[str] = []
        t0 = time.monotonic()

        def _elapsed() -> str:
            return f"[{time.monotonic() - t0:.1f}s]"

        logger.info(f"{_elapsed()} withdraw: fetching inventory")
        inv = await self._get_inventory()
        logger.info(
            f"{_elapsed()} withdraw: inventory fetched — "
            f"hlp_enabled={self._hlp_enabled()}, "
            f"hlp_withdrawable={inv.hlp_withdrawable_now:.2f}, "
            f"hl_perp_idle={inv.hl_perp_idle:.2f}, "
            f"usdt_arb_idle={inv.usdt_arb_idle:.2f}, "
            f"usdc_arb_idle={inv.usdc_arb_idle:.2f}, "
            f"usdc_base_idle={inv.usdc_base_idle:.2f}, "
            f"boros_vaults={len(inv.boros_vaults)}"
        )

        if self._hlp_enabled():
            if inv.hlp_withdrawable_now > EPS:
                logger.info(f"{_elapsed()} withdraw: withdrawing from HLP")
                ok, detail = await self.hyperliquid_adapter.withdraw_hlp(
                    inv.hlp_withdrawable_now,
                    self.hlp_vault_address,
                )
                messages.append(
                    "Withdrew from HLP" if ok else f"HLP withdraw failed: {detail}"
                )
                logger.info(f"{_elapsed()} withdraw: HLP withdraw ok={ok}")
                await asyncio.sleep(2)
                inv = await self._get_inventory()
            elif inv.hlp_in_cooldown:
                hours = (inv.hlp_wait_ms or 0) / 1000 / 3600
                messages.append(f"HLP still in cooldown (~{hours:.1f}h remaining)")

        if inv.hl_perp_idle > 1.0 and self._hlp_enabled():
            logger.info(
                f"{_elapsed()} withdraw: bridging {inv.hl_perp_idle:.2f} from Hyperliquid"
            )
            ok, detail = await self.hyperliquid_adapter.withdraw_from_hyperliquid(
                inv.hl_perp_idle,
                destination=self.strategy_wallet_address,
                wait_for_completion=True,
            )
            messages.append(
                "Withdrew Hyperliquid bridge balance"
                if ok
                else f"Hyperliquid bridge withdraw failed: {detail}"
            )
            logger.info(f"{_elapsed()} withdraw: Hyperliquid bridge ok={ok}")

        logger.info(f"{_elapsed()} withdraw: checking Avantis position")
        ok_pos, pos = await self.avantis_adapter.position(
            account=self.strategy_wallet_address
        )
        avantis_val = (
            float(pos.get("value_usdc") or 0.0)
            if ok_pos and isinstance(pos, dict)
            else 0.0
        )
        logger.info(f"{_elapsed()} withdraw: Avantis value_usdc={avantis_val:.2f}")
        if ok_pos and isinstance(pos, dict) and avantis_val > EPS:
            logger.info(f"{_elapsed()} withdraw: redeeming Avantis position")
            ok, detail = await self.avantis_adapter.withdraw(amount=0, redeem_full=True)
            messages.append(
                "Redeemed Avantis position"
                if ok
                else f"Avantis withdraw failed: {detail}"
            )
            logger.info(f"{_elapsed()} withdraw: Avantis redeem ok={ok}")

        for vault in inv.boros_vaults:
            lp_wei = self.boros_adapter.estimate_user_lp_balance_wei(vault)
            if not lp_wei:
                continue
            logger.info(
                f"{_elapsed()} withdraw: withdrawing Boros vault {vault.symbol} lp_wei={lp_wei}"
            )
            ok, detail = await self.boros_adapter.withdraw_from_vault(
                market_id=vault.market_id,
                lp_to_remove_wei=int(lp_wei),
            )
            messages.append(
                f"Withdrew Boros vault {vault.symbol}"
                if ok
                else f"Boros vault withdraw failed for {vault.symbol}: {detail}"
            )
            logger.info(f"{_elapsed()} withdraw: Boros vault {vault.symbol} ok={ok}")

        logger.info(f"{_elapsed()} withdraw: checking Boros account balances")
        ok_bal, balances = await self.boros_adapter.get_account_balances(
            token_id=self.boros_token_id,
            account_id=0,
        )
        if ok_bal and isinstance(balances, dict):
            cross_wei = int(balances.get("cross_wei") or 0)
            logger.info(f"{_elapsed()} withdraw: Boros cross_wei={cross_wei}")
            if cross_wei > 0:
                buffer_wei = to_erc20_raw(0.01, 18)
                unscaled = await self.boros_adapter.scaled_cash_wei_to_unscaled(
                    self.boros_token_id,
                    max(0, cross_wei - buffer_wei),
                )
                if unscaled > 0:
                    logger.info(
                        f"{_elapsed()} withdraw: withdrawing Boros collateral unscaled={unscaled}"
                    )
                    ok, detail = await self.boros_adapter.withdraw_collateral(
                        token_id=self.boros_token_id,
                        amount_native=int(unscaled),
                        account_id=0,
                    )
                    messages.append(
                        "Submitted Boros withdrawal to wallet"
                        if ok
                        else f"Boros collateral withdraw failed: {detail}"
                    )
                    logger.info(f"{_elapsed()} withdraw: Boros collateral ok={ok}")

        logger.info(f"{_elapsed()} withdraw: checking Boros withdrawal status")
        ok_withdraw, status = await self.boros_adapter.get_user_withdrawal_status(
            token_id=self.boros_token_id
        )
        if (
            ok_withdraw
            and isinstance(status, dict)
            and int(status.get("start") or 0) > 0
        ):
            logger.info(f"{_elapsed()} withdraw: finalizing Boros withdrawal")
            ok, detail = await self.boros_adapter.finalize_vault_withdrawal(
                token_id=self.boros_token_id
            )
            if ok:
                messages.append("Finalized Boros withdrawal")
                await asyncio.sleep(2)
            else:
                messages.append(f"Boros finalize pending: {detail}")
            logger.info(f"{_elapsed()} withdraw: Boros finalize ok={ok}")

        logger.info(
            f"{_elapsed()} withdraw: re-fetching inventory for pending withdrawal check"
        )
        inv = await self._get_inventory()
        logger.info(f"{_elapsed()} withdraw: usdt_arb_idle={inv.usdt_arb_idle:.2f}")
        if inv.usdt_arb_idle > 0.5:
            logger.info(
                f"{_elapsed()} withdraw: completing pending withdrawal (USDT→USDC swap + wait)"
            )
            ok, detail = await self._complete_pending_withdrawal(inv)
            messages.append(detail)
            logger.info(
                f"{_elapsed()} withdraw: pending withdrawal ok={ok} detail={detail}"
            )
            inv = await self._get_inventory()

        logger.info(f"{_elapsed()} withdraw: usdc_base_idle={inv.usdc_base_idle:.2f}")
        if inv.usdc_base_idle > 0.5:
            logger.info(f"{_elapsed()} withdraw: bridging Base USDC to Arbitrum")
            ok_gas, gas_msg = await self._ensure_base_gas()
            logger.info(f"{_elapsed()} withdraw: ensure_base_gas ok={ok_gas}")
            if ok_gas:
                logger.info(
                    f"{_elapsed()} withdraw: BRAP swap Base USDC→Arb USDC amount={inv.usdc_base_idle:.2f}"
                )
                ok, result = await self.brap_adapter.swap_from_token_ids(
                    from_token_id=USDC_BASE,
                    to_token_id=USDC_ARB,
                    from_address=self.strategy_wallet_address,
                    amount=str(to_erc20_raw(inv.usdc_base_idle, 6)),
                    slippage=0.005,
                )
                messages.append(
                    "Bridged Base USDC to Arbitrum"
                    if ok
                    else f"Base->Arbitrum USDC bridge failed: {result}"
                )
                logger.info(
                    f"{_elapsed()} withdraw: BRAP bridge ok={ok}, waiting for balance on Arb"
                )
                await self.balance_adapter.wait_for_balance(
                    token_id=USDC_ARB,
                    min_balance=0.5,
                    timeout_seconds=240,
                )
                logger.info(f"{_elapsed()} withdraw: wait_for_balance(USDC_ARB) done")
            else:
                messages.append(gas_msg)

        logger.info(f"{_elapsed()} withdraw: final inventory check")
        inv = await self._get_inventory()
        logger.info(
            f"{_elapsed()} withdraw: usdc_arb_idle={inv.usdc_arb_idle:.4f}, usdc_base_idle={inv.usdc_base_idle:.4f}"
        )
        if inv.usdc_arb_idle > EPS:
            logger.info(
                f"{_elapsed()} withdraw: transferring {inv.usdc_arb_idle:.2f} USDC Arb to main"
            )
            (
                ok,
                detail,
            ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                USDC_ARB,
                inv.usdc_arb_idle,
                strategy_name=self.__class__.__name__,
            )
            if ok:
                messages.append("Returned Arbitrum USDC to main wallet")
            else:
                messages.append(f"Failed to return Arbitrum USDC: {detail}")
            logger.info(f"{_elapsed()} withdraw: Arb USDC transfer ok={ok}")

        if inv.usdc_base_idle > EPS:
            logger.info(
                f"{_elapsed()} withdraw: transferring {inv.usdc_base_idle:.2f} USDC Base to main"
            )
            (
                ok,
                detail,
            ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                USDC_BASE,
                inv.usdc_base_idle,
                strategy_name=self.__class__.__name__,
            )
            if ok:
                messages.append("Returned Base USDC to main wallet")
            else:
                messages.append(f"Failed to return Base USDC: {detail}")
            logger.info(f"{_elapsed()} withdraw: Base USDC transfer ok={ok}")

        cleaned = [message for message in messages if message]
        logger.info(
            f"{_elapsed()} withdraw: complete — {'; '.join(cleaned) if cleaned else 'no action'}"
        )
        return True, "; ".join(
            cleaned
        ) if cleaned else "Best-effort withdrawal complete"

    async def exit(self, **kwargs: Any) -> StatusTuple:
        return await self.withdraw(**kwargs)

    async def _status(self) -> StatusDict:
        inv = await self._get_inventory()
        apys = self.cached_apys or await self._fetch_apys(
            deposit_amount_usdc=inv.total_value
        )
        return {
            "portfolio_value": float(inv.total_value),
            "net_deposit": 0.0,
            "strategy_status": {
                "usdc_arbitrum": inv.usdc_arb_idle,
                "usdc_base": inv.usdc_base_idle,
                "usdt_arbitrum": inv.usdt_arb_idle,
                "hlp_equity_usd": inv.hlp_equity,
                "hlp_wait_ms": inv.hlp_wait_ms,
                "hlp_apy7d": float(apys.get("apy_hlp") or 0.0),
                "avantis_value_usdc": inv.avantis_value_usdc,
                "avantis_apy": float(apys.get("apy_avantis") or 0.0),
                "boros_vault_value_usd": inv.boros_vault_value_usd,
                "boros_vault_reported_value_usd": inv.boros_vault_reported_value_usd,
                "boros_account_idle_usd": inv.boros_account_idle_usd,
                "boros_apy": float(apys.get("apy_boros") or 0.0),
                "boros_vaults": [
                    self._format_boros_vault_view(vault)
                    for vault in inv.boros_vaults
                    if not vault.is_expired
                ],
                "positions_value": inv.positions_value,
                "unallocated_total": inv.unallocated_total,
                "enabled_legs": {
                    "hlp": self._hlp_enabled(),
                    "boros": self._boros_enabled(),
                    "avantis": self._avantis_enabled(),
                },
            },
            "gas_available": float(inv.eth_arb_idle),
            "gassed_up": bool(inv.eth_arb_idle >= self.INFO.gas_threshold),
        }

    async def quote(self, deposit_amount: float | None = None, **_: Any) -> QuoteResult:
        amount = float(deposit_amount or 1000.0)
        apys = await self._fetch_apys(deposit_amount_usdc=amount)
        w_hlp, w_boros, w_avantis = self._compute_weights(apys, total_value=amount)
        expected_apy = (
            w_hlp * float(apys.get("apy_hlp") or 0.0)
            + w_boros * float(apys.get("apy_boros") or 0.0)
            + w_avantis * float(apys.get("apy_avantis") or 0.0)
        )
        return {
            "expected_apy": expected_apy,
            "apy_type": "blended",
            "confidence": "medium",
            "methodology": "Hybrid equal-weight plus APY-weight tilt across enabled legs",
            "components": {
                "hlp": float(apys.get("apy_hlp") or 0.0),
                "boros": float(apys.get("apy_boros") or 0.0),
                "avantis": float(apys.get("apy_avantis") or 0.0),
            },
            "deposit_amount": amount,
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "summary": (
                f"Estimated blended APY {expected_apy:.2%} with target weights "
                f"{w_hlp:.0%} HLP / {w_boros:.0%} Boros / {w_avantis:.0%} Avantis."
            ),
        }
