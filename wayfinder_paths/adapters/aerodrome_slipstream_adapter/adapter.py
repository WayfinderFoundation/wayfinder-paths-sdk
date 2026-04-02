from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from eth_utils import keccak, to_checksum_address

import wayfinder_paths.adapters.aerodrome_common as aerodrome_common
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.aerodrome_abi import (
    AERODROME_REWARDS_DISTRIBUTOR_ABI,
    AERODROME_VOTER_ABI,
    AERODROME_VOTING_ESCROW_ABI,
)
from wayfinder_paths.core.constants.aerodrome_slipstream_abi import (
    AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
    AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
    AERODROME_SLIPSTREAM_CL_POOL_ABI,
    AERODROME_SLIPSTREAM_NPM_ABI,
)
from wayfinder_paths.core.constants.aerodrome_slipstream_contracts import (
    AERODROME_SLIPSTREAM_BY_CHAIN,
    AERODROME_SLIPSTREAM_DEPLOYMENT_GAUGE_CAPS,
    AERODROME_SLIPSTREAM_DEPLOYMENT_INITIAL,
)
from wayfinder_paths.core.constants.base import MAX_UINT256, SECONDS_PER_YEAR
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.uniswap_v3_math import (
    MAX_UINT128,
    amounts_for_liq_inrange,
    liq_for_amounts,
    slippage_min,
    sqrt_price_x96_from_tick,
    sqrt_price_x96_to_price,
    tick_to_price_decimal,
)
from wayfinder_paths.core.utils.uniswap_v3_math import (
    deadline as default_deadline,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

SLIPSTREAM_SWAP_TOPIC0 = (
    "0x"
    + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
)


def _checksum_or_zero(value: str | None) -> str:
    if not value:
        return ZERO_ADDRESS
    if value.lower() == ZERO_ADDRESS:
        return ZERO_ADDRESS
    return to_checksum_address(value)


def _explicit_amount_min(amount_min: int | None) -> int | None:
    if amount_min is None:
        return None
    value = amount_min
    if value < 0:
        raise ValueError("amount mins must be non-negative")
    return value


def _shared_core_contracts(entry: dict[str, object]) -> dict[str, str]:
    return {
        "chain_name": entry["chain_name"],
        "aero": entry["aero"],
        "voter": entry["voter"],
        "voting_escrow": entry["voting_escrow"],
        "rewards_distributor": entry["rewards_distributor"],
        "weth": entry["weth"],
    }


class AerodromeSlipstreamAdapterConfig(TypedDict, total=False):
    deployments: Sequence[str]
    write_deployment: str


class AerodromeSlipstreamAdapter(
    aerodrome_common.AerodromeTokenHelpersMixin,
    aerodrome_common.AerodromeVotingRewardsMixin,
    BaseAdapter,
):
    adapter_type = "AERODROME_SLIPSTREAM"
    chain_id = CHAIN_ID_BASE

    def __init__(
        self,
        config: AerodromeSlipstreamAdapterConfig | None = None,
        *,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("aerodrome_slipstream_adapter", config or {})
        self.sign_callback = sign_callback

        entry = AERODROME_SLIPSTREAM_BY_CHAIN.get(CHAIN_ID_BASE)
        if not entry:
            raise ValueError("Aerodrome Slipstream Base deployment constants missing")

        self.core_contracts: dict[str, str] = _shared_core_contracts(entry)

        deployments = entry.get("deployments")
        if not isinstance(deployments, dict) or not deployments:
            raise ValueError("Aerodrome Slipstream deployment map missing")

        self.supported_deployments: dict[str, dict[str, str]] = {
            name: {
                key: to_checksum_address(val)
                for key, val in values.items()
                if isinstance(val, str)
            }
            for name, values in deployments.items()
            if isinstance(values, dict)
        }
        configured_deployments = (config or {}).get("deployments")
        self.default_deployments: list[str] = list(
            configured_deployments
            if configured_deployments is not None
            else [
                AERODROME_SLIPSTREAM_DEPLOYMENT_INITIAL,
                AERODROME_SLIPSTREAM_DEPLOYMENT_GAUGE_CAPS,
            ]
        )
        self.write_deployment = (config or {}).get(
            "write_deployment"
        ) or AERODROME_SLIPSTREAM_DEPLOYMENT_INITIAL
        if self.write_deployment not in self.supported_deployments:
            raise ValueError(
                f"Unknown Slipstream write deployment: {self.write_deployment}"
            )

        self._variant_by_npm = {
            cfg["nonfungible_position_manager"].lower(): variant
            for variant, cfg in self.supported_deployments.items()
        }

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self._token_decimals_cache: dict[str, int] = {}
        self._token_symbol_cache: dict[str, str] = {}
        self._token_price_usdc_cache: dict[str, tuple[float, float | None]] = {}

    def _resolve_deployments(
        self,
        deployments: Sequence[str] | None = None,
    ) -> list[str]:
        raw = list(deployments if deployments is not None else self.default_deployments)

        normalized: list[str] = []
        seen: set[str] = set()
        for variant in raw:
            name = variant
            if name not in self.supported_deployments:
                raise ValueError(f"Unknown Slipstream deployment: {name}")
            if name in seen:
                continue
            seen.add(name)
            normalized.append(name)
        return normalized

    def _deployment(self, variant: str) -> dict[str, str]:
        if variant not in self.supported_deployments:
            raise ValueError(f"Unknown Slipstream deployment: {variant}")
        return self.supported_deployments[variant]

    def _deployment_from_position_manager(self, position_manager: str) -> str:
        pm = to_checksum_address(position_manager)
        variant = self._variant_by_npm.get(pm.lower())
        if not variant:
            raise ValueError(f"Unknown Slipstream position manager: {pm}")
        return variant

    async def _token_decimals(self, token: str) -> int:
        return await self.token_decimals(token)

    async def token_price_usdc(self, token: str) -> float | None:
        token_addr = to_checksum_address(token)
        if token_addr == BASE_USDC:
            return 1.0

        now = time.monotonic()
        cached = self._token_price_usdc_cache.get(token_addr)
        if cached is not None:
            cached_at, cached_price = cached
            if (
                now - cached_at
                <= aerodrome_common.AERODROME_TOKEN_PRICE_USDC_TTL_SECONDS
            ):
                return cached_price

        price = await self._token_price_usdc_from_market_data(token_addr)
        self._token_price_usdc_cache[token_addr] = (time.monotonic(), price)
        return price

    def _select_write_target(
        self,
        *,
        deployment_variant: str | None = None,
        position_manager: str | None = None,
    ) -> tuple[str, dict[str, str], str]:
        if position_manager:
            pm = to_checksum_address(position_manager)
            variant = self._deployment_from_position_manager(pm)
            return variant, self._deployment(variant), pm

        variant = deployment_variant or self.write_deployment
        deployment = self._deployment(variant)
        return variant, deployment, deployment["nonfungible_position_manager"]

    async def _resolve_token_manager(
        self,
        *,
        token_id: int,
        position_manager: str | None = None,
        deployments: Sequence[str] | None = None,
        block_identifier: str | int = "latest",
    ) -> tuple[str, dict[str, str], str, str]:
        if token_id <= 0:
            raise ValueError("token_id must be positive")

        candidates: list[tuple[str, str]] = []
        if position_manager:
            pm = to_checksum_address(position_manager)
            candidates.append((self._deployment_from_position_manager(pm), pm))
        else:
            for variant in self._resolve_deployments(deployments):
                deployment = self._deployment(variant)
                candidates.append((variant, deployment["nonfungible_position_manager"]))

        matches: list[tuple[str, str, str]] = []
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            candidate_contracts = [
                (
                    variant,
                    pm,
                    web3.eth.contract(address=pm, abi=AERODROME_SLIPSTREAM_NPM_ABI),
                )
                for variant, pm in candidates
            ]
            owners = await asyncio.gather(
                *[
                    npm.functions.ownerOf(token_id).call(
                        block_identifier=block_identifier
                    )
                    for _, _, npm in candidate_contracts
                ],
                return_exceptions=True,
            )
            for (variant, pm, _), owner in zip(
                candidate_contracts, owners, strict=True
            ):
                if isinstance(owner, Exception):
                    continue
                matches.append((variant, pm, to_checksum_address(owner)))

        if not matches:
            raise ValueError(
                f"token_id {token_id} was not found in any configured position manager"
            )
        if len(matches) > 1 and position_manager is None:
            raise ValueError(
                "token_id exists in multiple Slipstream position managers; pass position_manager explicitly"
            )

        variant, pm, owner = matches[0]
        return variant, self._deployment(variant), pm, owner

    async def _current_sqrt_price_x96(
        self,
        *,
        deployment: dict[str, str],
        token0: str,
        token1: str,
        tick_spacing: int,
        initial_sqrt_price_x96: int | None = None,
    ) -> int:
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            factory = web3.eth.contract(
                address=deployment["pool_factory"],
                abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
            )
            pool = await factory.functions.getPool(
                to_checksum_address(token0),
                to_checksum_address(token1),
                tick_spacing,
            ).call(block_identifier="latest")
            pool_addr = _checksum_or_zero(pool)

            if pool_addr != ZERO_ADDRESS:
                pool_contract = web3.eth.contract(
                    address=pool_addr,
                    abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
                )
                slot0 = await pool_contract.functions.slot0().call(
                    block_identifier="latest"
                )
                return slot0[0]

        if initial_sqrt_price_x96 is not None and initial_sqrt_price_x96 > 0:
            return initial_sqrt_price_x96

        raise ValueError("amount mins are required when pool price cannot be resolved")

    async def _resolve_position_amount_mins(
        self,
        *,
        deployment: dict[str, str],
        token0: str,
        token1: str,
        tick_spacing: int,
        tick_lower: int,
        tick_upper: int,
        amount0_desired: int,
        amount1_desired: int,
        amount0_min: int | None,
        amount1_min: int | None,
        slippage_bps: int,
        initial_sqrt_price_x96: int | None = None,
    ) -> tuple[int, int]:
        explicit0 = _explicit_amount_min(amount0_min)
        explicit1 = _explicit_amount_min(amount1_min)
        if explicit0 is not None and explicit1 is not None:
            return explicit0, explicit1

        sqrt_price_x96 = await self._current_sqrt_price_x96(
            deployment=deployment,
            token0=token0,
            token1=token1,
            tick_spacing=tick_spacing,
            initial_sqrt_price_x96=initial_sqrt_price_x96,
        )
        sqrt_lower = sqrt_price_x96_from_tick(tick_lower)
        sqrt_upper = sqrt_price_x96_from_tick(tick_upper)
        liquidity = liq_for_amounts(
            sqrt_price_x96,
            sqrt_lower,
            sqrt_upper,
            amount0_desired,
            amount1_desired,
        )
        expected0, expected1 = amounts_for_liq_inrange(
            sqrt_price_x96,
            sqrt_lower,
            sqrt_upper,
            liquidity,
        )
        if expected0 <= 0 and expected1 <= 0:
            raise ValueError("could not derive non-zero amount mins from current price")

        return (
            explicit0
            if explicit0 is not None
            else slippage_min(expected0, slippage_bps),
            explicit1
            if explicit1 is not None
            else slippage_min(expected1, slippage_bps),
        )

    async def _resolve_liquidity_amount_mins(
        self,
        *,
        deployment: dict[str, str],
        token0: str,
        token1: str,
        tick_spacing: int,
        tick_lower: int,
        tick_upper: int,
        liquidity: int,
        amount0_min: int | None,
        amount1_min: int | None,
        slippage_bps: int,
    ) -> tuple[int, int]:
        explicit0 = _explicit_amount_min(amount0_min)
        explicit1 = _explicit_amount_min(amount1_min)
        if explicit0 is not None and explicit1 is not None:
            return explicit0, explicit1

        sqrt_price_x96 = await self._current_sqrt_price_x96(
            deployment=deployment,
            token0=token0,
            token1=token1,
            tick_spacing=tick_spacing,
        )
        sqrt_lower = sqrt_price_x96_from_tick(tick_lower)
        sqrt_upper = sqrt_price_x96_from_tick(tick_upper)
        expected0, expected1 = amounts_for_liq_inrange(
            sqrt_price_x96,
            sqrt_lower,
            sqrt_upper,
            liquidity,
        )
        if expected0 <= 0 and expected1 <= 0:
            raise ValueError("could not derive non-zero amount mins from current price")

        return (
            explicit0
            if explicit0 is not None
            else slippage_min(expected0, slippage_bps),
            explicit1
            if explicit1 is not None
            else slippage_min(expected1, slippage_bps),
        )

    async def _ensure_erc721_approval(
        self,
        *,
        nft_contract: str,
        token_id: int,
        operator: str,
        owner: str,
    ) -> tuple[bool, Any]:
        nft_contract = to_checksum_address(nft_contract)
        operator = to_checksum_address(operator)
        owner = to_checksum_address(owner)

        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            nft = web3.eth.contract(
                address=nft_contract, abi=AERODROME_SLIPSTREAM_NPM_ABI
            )
            approved, approved_for_all = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=CHAIN_ID_BASE,
                calls=[
                    Call(nft, "getApproved", args=(token_id,)),
                    Call(nft, "isApprovedForAll", args=(owner, operator)),
                ],
                block_identifier="pending",
            )
            if (
                _checksum_or_zero(approved).lower() == operator.lower()
                or approved_for_all
            ):
                return True, {}

        tx = await encode_call(
            target=nft_contract,
            abi=AERODROME_SLIPSTREAM_NPM_ABI,
            fn_name="approve",
            args=[operator, token_id],
            from_address=owner,
            chain_id=CHAIN_ID_BASE,
        )
        tx_hash = await send_transaction(tx, self.sign_callback)
        return True, tx_hash

    async def _pool_and_gauge_for_position(
        self,
        *,
        web3: Any,
        deployment: dict[str, str],
        token0: str,
        token1: str,
        tick_spacing: int,
        block_identifier: str | int = "latest",
    ) -> tuple[str, str]:
        factory = web3.eth.contract(
            address=deployment["pool_factory"],
            abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
        )
        voter = web3.eth.contract(
            address=self.core_contracts["voter"],
            abi=AERODROME_VOTER_ABI,
        )
        pool = await factory.functions.getPool(
            to_checksum_address(token0),
            to_checksum_address(token1),
            tick_spacing,
        ).call(block_identifier=block_identifier)
        pool_addr = _checksum_or_zero(pool)
        if pool_addr == ZERO_ADDRESS:
            return ZERO_ADDRESS, ZERO_ADDRESS

        pool_contract = web3.eth.contract(
            address=pool_addr,
            abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
        )
        pool_gauge, voter_gauge = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=CHAIN_ID_BASE,
            calls=[
                Call(pool_contract, "gauge"),
                Call(voter, "gauges", args=(pool_addr,)),
            ],
            block_identifier=block_identifier,
        )
        pool_gauge_addr = _checksum_or_zero(pool_gauge)
        voter_gauge_addr = _checksum_or_zero(voter_gauge)
        if (
            pool_gauge_addr != ZERO_ADDRESS
            and voter_gauge_addr != ZERO_ADDRESS
            and pool_gauge_addr.lower() != voter_gauge_addr.lower()
        ):
            raise ValueError(
                f"Pool gauge mismatch for {pool_addr}: pool={pool_gauge_addr} voter={voter_gauge_addr}"
            )
        return pool_addr, (
            pool_gauge_addr if pool_gauge_addr != ZERO_ADDRESS else voter_gauge_addr
        )

    async def _read_market(
        self,
        *,
        web3: Any,
        deployment_variant: str,
        pool: str,
        include_gauge_state: bool = True,
        block_identifier: str | int = "latest",
    ) -> dict[str, Any]:
        deployment = self._deployment(deployment_variant)
        pool_addr = to_checksum_address(pool)
        factory = web3.eth.contract(
            address=deployment["pool_factory"],
            abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
        )
        voter = web3.eth.contract(
            address=self.core_contracts["voter"],
            abi=AERODROME_VOTER_ABI,
        )
        pool_contract = web3.eth.contract(
            address=pool_addr,
            abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
        )

        (
            token0,
            token1,
            pool_gauge,
            nft,
            tick_spacing,
            slot0,
            pool_fee,
            pool_unstaked_fee,
            liquidity,
            staked_liquidity,
            pool_reward_rate,
            pool_reward_reserve,
            pool_period_finish,
            pool_last_updated,
            voter_gauge,
            swap_fee,
            unstaked_fee,
        ) = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=CHAIN_ID_BASE,
            calls=[
                Call(pool_contract, "token0"),
                Call(pool_contract, "token1"),
                Call(pool_contract, "gauge"),
                Call(pool_contract, "nft"),
                Call(pool_contract, "tickSpacing"),
                Call(pool_contract, "slot0"),
                Call(pool_contract, "fee"),
                Call(pool_contract, "unstakedFee"),
                Call(pool_contract, "liquidity"),
                Call(pool_contract, "stakedLiquidity"),
                Call(pool_contract, "rewardRate"),
                Call(pool_contract, "rewardReserve"),
                Call(pool_contract, "periodFinish"),
                Call(pool_contract, "lastUpdated"),
                Call(voter, "gauges", args=(pool_addr,)),
                Call(factory, "getSwapFee", args=(pool_addr,)),
                Call(factory, "getUnstakedFee", args=(pool_addr,)),
            ],
            block_identifier=block_identifier,
        )

        pool_gauge_addr = _checksum_or_zero(pool_gauge)
        voter_gauge_addr = _checksum_or_zero(voter_gauge)
        if (
            pool_gauge_addr != ZERO_ADDRESS
            and voter_gauge_addr != ZERO_ADDRESS
            and pool_gauge_addr.lower() != voter_gauge_addr.lower()
        ):
            raise ValueError(
                f"Pool gauge mismatch for {pool_addr}: pool={pool_gauge_addr} voter={voter_gauge_addr}"
            )
        gauge = pool_gauge_addr if pool_gauge_addr != ZERO_ADDRESS else voter_gauge_addr

        fee_reward = ZERO_ADDRESS
        bribe_reward = ZERO_ADDRESS
        gauge_reward_token = ZERO_ADDRESS
        gauge_reward_rate = 0
        gauge_period_finish = 0
        is_alive = False

        if gauge != ZERO_ADDRESS and include_gauge_state:
            gauge_contract = web3.eth.contract(
                address=gauge,
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
            )
            (
                fee_reward,
                bribe_reward,
                is_alive,
                gauge_reward_token,
                gauge_reward_rate,
                gauge_period_finish,
            ) = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=CHAIN_ID_BASE,
                calls=[
                    Call(voter, "gaugeToFees", args=(gauge,)),
                    Call(voter, "gaugeToBribe", args=(gauge,)),
                    Call(voter, "isAlive", args=(gauge,)),
                    Call(gauge_contract, "rewardToken"),
                    Call(gauge_contract, "rewardRate"),
                    Call(gauge_contract, "periodFinish"),
                ],
                block_identifier=block_identifier,
            )

        return {
            "deployment_variant": deployment_variant,
            "cl_factory": deployment["pool_factory"],
            "position_manager": _checksum_or_zero(nft),
            "pool": pool_addr,
            "token0": to_checksum_address(token0),
            "token1": to_checksum_address(token1),
            "tick_spacing": tick_spacing,
            "swap_fee": swap_fee,
            "unstaked_fee": unstaked_fee,
            "pool_fee": pool_fee,
            "pool_unstaked_fee": pool_unstaked_fee,
            "gauge": gauge,
            "fee_reward": _checksum_or_zero(fee_reward),
            "bribe_reward": _checksum_or_zero(bribe_reward),
            "slot0": {
                "sqrtPriceX96": slot0[0],
                "tick": slot0[1],
            },
            "liquidity": liquidity,
            "staked_liquidity": staked_liquidity,
            "pool_reward_rate": pool_reward_rate,
            "pool_reward_reserve": pool_reward_reserve,
            "pool_period_finish": pool_period_finish,
            "pool_last_updated": pool_last_updated,
            "gauge_reward_token": _checksum_or_zero(gauge_reward_token),
            "gauge_reward_rate": gauge_reward_rate,
            "gauge_period_finish": gauge_period_finish,
            "is_alive": is_alive,
        }

    async def _read_position_state(
        self,
        *,
        web3: Any,
        deployment_variant: str,
        position_manager: str,
        token_id: int,
        account: str | None = None,
        include_usd: bool = False,
        block_identifier: str | int = "latest",
    ) -> dict[str, Any]:
        deployment = self._deployment(deployment_variant)
        npm_address = to_checksum_address(position_manager)
        npm = web3.eth.contract(address=npm_address, abi=AERODROME_SLIPSTREAM_NPM_ABI)
        raw_pos, owner_addr = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=CHAIN_ID_BASE,
            calls=[
                Call(npm, "positions", args=(token_id,)),
                Call(npm, "ownerOf", args=(token_id,), postprocess=to_checksum_address),
            ],
            block_identifier=block_identifier,
        )

        token0 = to_checksum_address(raw_pos[2])
        token1 = to_checksum_address(raw_pos[3])
        tick_spacing = raw_pos[4]
        pool, gauge = await self._pool_and_gauge_for_position(
            web3=web3,
            deployment=deployment,
            token0=token0,
            token1=token1,
            tick_spacing=tick_spacing,
            block_identifier=block_identifier,
        )

        fee_reward = ZERO_ADDRESS
        bribe_reward = ZERO_ADDRESS
        swap_fee: int | None = None
        unstaked_fee: int | None = None
        slot0_dict: dict[str, int] | None = None
        pool_liquidity: int | None = None
        staked_liquidity: int | None = None
        gauge_reward_token = ZERO_ADDRESS
        gauge_reward_rate = 0
        gauge_period_finish = 0
        is_alive = False

        if pool != ZERO_ADDRESS:
            market = await self._read_market(
                web3=web3,
                deployment_variant=deployment_variant,
                pool=pool,
                include_gauge_state=True,
                block_identifier=block_identifier,
            )
            fee_reward = market["fee_reward"]
            bribe_reward = market["bribe_reward"]
            swap_fee = market["swap_fee"]
            unstaked_fee = market["unstaked_fee"]
            slot0_dict = market["slot0"]
            pool_liquidity = market["liquidity"]
            staked_liquidity = market["staked_liquidity"]
            gauge_reward_token = market["gauge_reward_token"]
            gauge_reward_rate = market["gauge_reward_rate"]
            gauge_period_finish = market["gauge_period_finish"]
            is_alive = market["is_alive"]

        staked = gauge != ZERO_ADDRESS and owner_addr.lower() == gauge.lower()
        account_addr = to_checksum_address(account) if account else None
        staked_for_account: bool | None = None
        gauge_rewards_claimable: int | None = None

        if staked and account_addr and gauge != ZERO_ADDRESS:
            gauge_contract = web3.eth.contract(
                address=gauge,
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
            )
            contains, earned = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=CHAIN_ID_BASE,
                calls=[
                    Call(
                        gauge_contract, "stakedContains", args=(account_addr, token_id)
                    ),
                    Call(gauge_contract, "earned", args=(account_addr, token_id)),
                ],
                block_identifier=block_identifier,
            )
            staked_for_account = contains
            gauge_rewards_claimable = earned if contains else None

        return {
            "protocol": "aerodrome_slipstream",
            "chain_id": CHAIN_ID_BASE,
            "chain_name": self.core_contracts["chain_name"],
            "token_id": token_id,
            "deployment_variant": deployment_variant,
            "position_manager": npm_address,
            "owner": owner_addr,
            "pool": pool,
            "gauge": gauge,
            "token0": token0,
            "token1": token1,
            "tick_spacing": tick_spacing,
            "tick_lower": raw_pos[5],
            "tick_upper": raw_pos[6],
            "liquidity": raw_pos[7],
            "tokens_owed0": raw_pos[10],
            "tokens_owed1": raw_pos[11],
            "staked": staked,
            "staked_for_account": staked_for_account,
            "gauge_rewards_claimable": gauge_rewards_claimable,
            "fee_reward": fee_reward,
            "bribe_reward": bribe_reward,
            "swap_fee": swap_fee,
            "unstaked_fee": unstaked_fee,
            "slot0": slot0_dict,
            "pool_liquidity": pool_liquidity,
            "staked_liquidity": staked_liquidity,
            "gauge_reward_token": gauge_reward_token,
            "gauge_reward_rate": gauge_reward_rate,
            "gauge_period_finish": gauge_period_finish,
            "is_alive": is_alive,
            "include_usd": include_usd,
        }

    async def _enumerate_all_pools(
        self,
        *,
        web3: Any,
        deployments: Sequence[str],
        block_identifier: str | int = "latest",
    ) -> list[dict[str, Any]]:
        factory_specs: list[tuple[str, dict[str, str], Any]] = []
        for variant in deployments:
            deployment = self._deployment(variant)
            factory_specs.append(
                (
                    variant,
                    deployment,
                    web3.eth.contract(
                        address=deployment["pool_factory"],
                        abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
                    ),
                )
            )

        lengths = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=CHAIN_ID_BASE,
            calls=[Call(factory, "allPoolsLength") for _, _, factory in factory_specs],
            block_identifier=block_identifier,
        )

        pool_call_specs: list[tuple[str, dict[str, str], int, Call]] = []
        for (variant, deployment, factory), length in zip(
            factory_specs, lengths, strict=True
        ):
            if length <= 0:
                continue
            pool_call_specs.extend(
                (
                    variant,
                    deployment,
                    i,
                    Call(
                        factory,
                        "allPools",
                        args=(i,),
                        postprocess=to_checksum_address,
                    ),
                )
                for i in range(length)
            )

        if not pool_call_specs:
            return []

        pools = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=CHAIN_ID_BASE,
            calls=[spec[3] for spec in pool_call_specs],
            block_identifier=block_identifier,
            chunk_size=100,
        )

        results: list[dict[str, Any]] = []
        for (variant, deployment, index, _), pool in zip(
            pool_call_specs, pools, strict=True
        ):
            results.append(
                {
                    "deployment_variant": variant,
                    "cl_factory": deployment["pool_factory"],
                    "position_manager": deployment["nonfungible_position_manager"],
                    "pool": pool,
                    "deployment_index": index,
                }
            )
        return results

    async def find_pools(
        self,
        *,
        tokenA: str,
        tokenB: str,
        tick_spacings: Sequence[int] | None = None,
        deployments: Sequence[str] | None = None,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            tA = to_checksum_address(tokenA)
            tB = to_checksum_address(tokenB)
            results: list[dict[str, Any]] = []

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                deployment_specs: list[tuple[str, dict[str, str], Any]] = []
                for variant in self._resolve_deployments(deployments):
                    deployment = self._deployment(variant)
                    deployment_specs.append(
                        (
                            variant,
                            deployment,
                            web3.eth.contract(
                                address=deployment["pool_factory"],
                                abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
                            ),
                        )
                    )

                if tick_spacings is None:
                    spacing_groups = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[
                            Call(factory, "tickSpacings")
                            for _, _, factory in deployment_specs
                        ],
                        block_identifier=block_identifier,
                    )
                else:
                    spacing_groups = [tick_spacings] * len(deployment_specs)

                pool_call_specs: list[tuple[str, dict[str, str], int, Call]] = []
                for (variant, deployment, factory), spacings in zip(
                    deployment_specs, spacing_groups, strict=True
                ):
                    if not spacings:
                        continue
                    pool_call_specs.extend(
                        (
                            variant,
                            deployment,
                            spacing,
                            Call(
                                factory,
                                "getPool",
                                args=(tA, tB, spacing),
                                postprocess=_checksum_or_zero,
                            ),
                        )
                        for spacing in spacings
                    )

                if pool_call_specs:
                    pools = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[spec[3] for spec in pool_call_specs],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    for (variant, deployment, spacing, _), pool in zip(
                        pool_call_specs, pools, strict=True
                    ):
                        if pool == ZERO_ADDRESS:
                            continue
                        results.append(
                            {
                                "deployment_variant": variant,
                                "cl_factory": deployment["pool_factory"],
                                "position_manager": deployment[
                                    "nonfungible_position_manager"
                                ],
                                "tick_spacing": spacing,
                                "pool": pool,
                            }
                        )

            return True, results
        except Exception as exc:
            return False, str(exc)

    async def get_pool(
        self,
        *,
        tokenA: str,
        tokenB: str,
        tick_spacing: int,
        deployment_variant: str | None = None,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            deployments = [deployment_variant] if deployment_variant else None
            ok, matches = await self.find_pools(
                tokenA=tokenA,
                tokenB=tokenB,
                tick_spacings=[tick_spacing],
                deployments=deployments,
                block_identifier=block_identifier,
            )
            if not ok:
                return False, matches
            if not matches:
                return False, "Pool does not exist"
            if len(matches) > 1 and deployment_variant is None:
                return False, (
                    "Multiple Slipstream pools matched across deployments; "
                    "pass deployment_variant or use find_pools"
                )
            return True, matches[0]
        except Exception as exc:
            return False, str(exc)

    async def get_gauge(
        self,
        *,
        pool: str,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            pool_addr = to_checksum_address(pool)
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                pool_contract = web3.eth.contract(
                    address=pool_addr,
                    abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
                )
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                pool_gauge, voter_gauge = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(pool_contract, "gauge"),
                        Call(voter, "gauges", args=(pool_addr,)),
                    ],
                    block_identifier=block_identifier,
                )

            pool_gauge_addr = _checksum_or_zero(pool_gauge)
            voter_gauge_addr = _checksum_or_zero(voter_gauge)
            if (
                pool_gauge_addr != ZERO_ADDRESS
                and voter_gauge_addr != ZERO_ADDRESS
                and pool_gauge_addr.lower() != voter_gauge_addr.lower()
            ):
                return False, "Pool gauge mismatch with voter registry"

            gauge = (
                pool_gauge_addr if pool_gauge_addr != ZERO_ADDRESS else voter_gauge_addr
            )
            if gauge == ZERO_ADDRESS:
                return False, "Gauge not found for pool"
            return True, gauge
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        start: int = 0,
        limit: int | None = 50,
        deployments: Sequence[str] | None = None,
        include_gauge_state: bool = True,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            start_i = max(0, start)
            deployment_names = self._resolve_deployments(deployments)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                factory_specs: list[tuple[str, dict[str, str], Any]] = []
                for variant in deployment_names:
                    deployment = self._deployment(variant)
                    factory_specs.append(
                        (
                            variant,
                            deployment,
                            web3.eth.contract(
                                address=deployment["pool_factory"],
                                abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
                            ),
                        )
                    )

                length_values = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(factory, "allPoolsLength")
                        for _, _, factory in factory_specs
                    ],
                    block_identifier=block_identifier,
                )
                lengths = [
                    (variant, deployment, factory, length)
                    for (variant, deployment, factory), length in zip(
                        factory_specs, length_values, strict=True
                    )
                ]

                total = sum(length for _, _, _, length in lengths)
                if total == 0 or start_i >= total:
                    return True, {
                        "protocol": "aerodrome_slipstream",
                        "chain_id": CHAIN_ID_BASE,
                        "chain_name": self.core_contracts["chain_name"],
                        "deployments": deployment_names,
                        "start": start_i,
                        "limit": limit,
                        "total": total,
                        "markets": [],
                    }

                end_i = total if limit is None else min(total, start_i + limit)
                selected: list[tuple[str, Any, int, int]] = []
                cursor = 0
                for variant, _, factory, length in lengths:
                    dep_start = max(0, start_i - cursor)
                    dep_end = min(length, end_i - cursor)
                    if dep_start < dep_end:
                        selected.append((variant, factory, dep_start, dep_end))
                    cursor += length

                pool_call_specs: list[tuple[str, Call]] = []
                for variant, factory, dep_start, dep_end in selected:
                    pool_call_specs.extend(
                        (
                            variant,
                            Call(
                                factory,
                                "allPools",
                                args=(i,),
                                postprocess=to_checksum_address,
                            ),
                        )
                        for i in range(dep_start, dep_end)
                    )

                pools = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[spec[1] for spec in pool_call_specs],
                    block_identifier=block_identifier,
                    chunk_size=100,
                )
                pool_refs = [
                    (variant, pool)
                    for (variant, _), pool in zip(pool_call_specs, pools, strict=True)
                ]

                markets = await asyncio.gather(
                    *[
                        self._read_market(
                            web3=web3,
                            deployment_variant=variant,
                            pool=pool,
                            include_gauge_state=include_gauge_state,
                            block_identifier=block_identifier,
                        )
                        for variant, pool in pool_refs
                    ]
                )

            return True, {
                "protocol": "aerodrome_slipstream",
                "chain_id": CHAIN_ID_BASE,
                "chain_name": self.core_contracts["chain_name"],
                "deployments": deployment_names,
                "start": start_i,
                "limit": limit,
                "total": total,
                "markets": markets,
            }
        except Exception as exc:
            return False, str(exc)

    async def slipstream_best_pool_for_pair(
        self,
        *,
        tokenA: str,
        tokenB: str,
        deployments: Sequence[str] | None = None,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            ok, matches = await self.find_pools(
                tokenA=tokenA,
                tokenB=tokenB,
                deployments=deployments,
                block_identifier=block_identifier,
            )
            if not ok:
                return False, matches
            if not matches:
                return False, "No Slipstream pool found for pair"

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                markets = await asyncio.gather(
                    *[
                        self._read_market(
                            web3=web3,
                            deployment_variant=match["deployment_variant"],
                            pool=match["pool"],
                            include_gauge_state=False,
                            block_identifier=block_identifier,
                        )
                        for match in matches
                    ]
                )

            best_market = max(markets, key=lambda market: market["liquidity"])
            if best_market["liquidity"] <= 0:
                return False, "Slipstream pools exist but none have liquidity > 0"
            return True, best_market
        except Exception as exc:
            return False, str(exc)

    async def slipstream_pool_state(
        self,
        *,
        pool: str,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            pool_addr = to_checksum_address(pool)
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                pool_contract = web3.eth.contract(
                    address=pool_addr,
                    abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
                )
                (
                    token0,
                    token1,
                    position_manager,
                    tick_spacing,
                    slot0,
                    liquidity,
                    fee_pips,
                    unstaked_fee_pips,
                ) = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(pool_contract, "token0", postprocess=to_checksum_address),
                        Call(pool_contract, "token1", postprocess=to_checksum_address),
                        Call(pool_contract, "nft", postprocess=to_checksum_address),
                        Call(pool_contract, "tickSpacing"),
                        Call(pool_contract, "slot0"),
                        Call(pool_contract, "liquidity"),
                        Call(pool_contract, "fee"),
                        Call(pool_contract, "unstakedFee"),
                    ],
                    block_identifier=block_identifier,
                )
            decimals0, decimals1 = await asyncio.gather(
                self._token_decimals(token0),
                self._token_decimals(token1),
            )
            deployment_variant = self._variant_by_npm.get(position_manager.lower())
            sqrt_price_x96 = slot0[0]
            price = sqrt_price_x96_to_price(
                sqrt_price_x96,
                decimals0,
                decimals1,
            )
            return True, {
                "protocol": "aerodrome_slipstream",
                "chain_id": CHAIN_ID_BASE,
                "chain_name": self.core_contracts["chain_name"],
                "deployment_variant": deployment_variant,
                "pool": pool_addr,
                "position_manager": position_manager,
                "token0": token0,
                "token1": token1,
                "sqrt_price_x96": sqrt_price_x96,
                "tick": slot0[1],
                "tick_spacing": tick_spacing,
                "liquidity": liquidity,
                "fee_pips": fee_pips,
                "unstaked_fee_pips": unstaked_fee_pips,
                "price_token1_per_token0": price,
            }
        except Exception as exc:
            return False, str(exc)

    async def slipstream_range_metrics(
        self,
        *,
        pool: str,
        tick_lower: int,
        tick_upper: int,
        amount0_raw: int,
        amount1_raw: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        if tick_lower >= tick_upper:
            return False, "tick_lower must be < tick_upper"
        if amount0_raw < 0 or amount1_raw < 0:
            return False, "amount0_raw and amount1_raw must be non-negative"

        try:
            ok, pool_state = await self.slipstream_pool_state(
                pool=pool,
                block_identifier=block_identifier,
            )
            if not ok:
                return False, pool_state

            sqrt_price_x96 = pool_state["sqrt_price_x96"]
            sqrt_lower = sqrt_price_x96_from_tick(tick_lower)
            sqrt_upper = sqrt_price_x96_from_tick(tick_upper)
            liquidity_position = liq_for_amounts(
                sqrt_price_x96,
                sqrt_lower,
                sqrt_upper,
                amount0_raw,
                amount1_raw,
            )
            amount0_now, amount1_now = amounts_for_liq_inrange(
                sqrt_price_x96,
                sqrt_lower,
                sqrt_upper,
                liquidity_position,
            )
            liquidity_total = pool_state["liquidity"]
            share_of_active_liquidity = (
                liquidity_position / liquidity_total if liquidity_total > 0 else 0.0
            )
            effective_fee_fraction_for_unstaked = (pool_state["fee_pips"] / 1e6) * (
                1.0 - pool_state["unstaked_fee_pips"] / 1e6
            )

            return True, {
                "protocol": "aerodrome_slipstream",
                "chain_id": CHAIN_ID_BASE,
                "chain_name": self.core_contracts["chain_name"],
                "deployment_variant": pool_state.get("deployment_variant"),
                "pool": pool_state["pool"],
                "position_manager": pool_state["position_manager"],
                "token0": pool_state["token0"],
                "token1": pool_state["token1"],
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "current_tick": pool_state["tick"],
                "in_range": tick_lower <= pool_state["tick"] < tick_upper,
                "sqrt_price_x96": sqrt_price_x96,
                "price_token1_per_token0": pool_state["price_token1_per_token0"],
                "liquidity_total": liquidity_total,
                "liquidity_position": liquidity_position,
                "share_of_active_liquidity": share_of_active_liquidity,
                "amount0_now": amount0_now,
                "amount1_now": amount1_now,
                "fee_pips": pool_state["fee_pips"],
                "unstaked_fee_pips": pool_state["unstaked_fee_pips"],
                "effective_fee_fraction_for_unstaked": (
                    effective_fee_fraction_for_unstaked
                ),
            }
        except Exception as exc:
            return False, str(exc)

    async def slipstream_volume_usdc_per_day(
        self,
        *,
        pool: str,
        lookback_blocks: int = 2000,
        max_logs: int = 5000,
        token0_price_usdc: float | None = None,
        token1_price_usdc: float | None = None,
    ) -> tuple[bool, Any]:
        if lookback_blocks <= 0:
            return False, "lookback_blocks must be > 0"
        if max_logs <= 0:
            return False, "max_logs must be > 0"

        try:
            ok, state = await self.slipstream_pool_state(pool=pool)
            if not ok:
                return False, state

            token0 = state["token0"]
            token1 = state["token1"]
            tasks = [
                self._token_decimals(token0),
                self._token_decimals(token1),
            ]
            if token0_price_usdc is None:
                tasks.append(self.token_price_usdc(token0))
            if token1_price_usdc is None:
                tasks.append(self.token_price_usdc(token1))
            results = list(await asyncio.gather(*tasks))

            decimals0, decimals1 = results[:2]
            next_result = 2
            price0 = token0_price_usdc
            if price0 is None:
                price0 = results[next_result]
                next_result += 1
            price1 = token1_price_usdc
            if price1 is None:
                price1 = results[next_result]

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                latest = await web3.eth.block_number
                from_block = max(0, latest - lookback_blocks)
                logs = await self._get_logs_bounded(
                    web3,
                    from_block=from_block,
                    to_block=latest,
                    address=state["pool"],
                    topics=[SLIPSTREAM_SWAP_TOPIC0],
                    max_logs=max_logs,
                )
                if not logs:
                    return True, {
                        "pool": state["pool"],
                        "volume_usdc_per_day": 0.0,
                        "swap_count": 0,
                        "seconds_covered": 0,
                    }

                block_numbers = [
                    log["blockNumber"]
                    for log in logs
                    if log.get("blockNumber") is not None
                ]
                if not block_numbers:
                    return True, {
                        "pool": state["pool"],
                        "volume_usdc_per_day": 0.0,
                        "swap_count": len(logs),
                        "seconds_covered": 0,
                    }

                block_min, block_max = min(block_numbers), max(block_numbers)
                block0, block1 = await asyncio.gather(
                    web3.eth.get_block(block_min),
                    web3.eth.get_block(block_max),
                )
                seconds_covered = max(
                    1,
                    block1["timestamp"] - block0["timestamp"],
                )

                total_usdc = 0.0
                for log in logs:
                    data = log.get("data")
                    if not data:
                        continue
                    try:
                        amount0, amount1, *_ = web3.codec.decode(
                            ["int256", "int256", "uint160", "uint128", "int24"],
                            data,
                        )
                    except Exception:
                        continue

                    value0 = float("nan")
                    value1 = float("nan")
                    if price0 is not None and math.isfinite(price0) and price0 > 0:
                        value0 = abs(amount0) / (10**decimals0) * price0
                    if price1 is not None and math.isfinite(price1) and price1 > 0:
                        value1 = abs(amount1) / (10**decimals1) * price1

                    if math.isfinite(value0) and math.isfinite(value1):
                        total_usdc += max(value0, value1)
                    elif math.isfinite(value0):
                        total_usdc += value0
                    elif math.isfinite(value1):
                        total_usdc += value1

            return True, {
                "pool": state["pool"],
                "volume_usdc_per_day": total_usdc * 86400.0 / seconds_covered,
                "swap_count": len(logs),
                "seconds_covered": seconds_covered,
            }
        except Exception as exc:
            return False, str(exc)

    async def slipstream_fee_apr_percent(
        self,
        *,
        metrics: dict[str, Any],
        volume_usdc_per_day: float,
        expected_in_range_fraction: float = 1.0,
        token0_price_usdc: float | None = None,
        token1_price_usdc: float | None = None,
    ) -> tuple[bool, Any]:
        if volume_usdc_per_day < 0:
            return False, "volume_usdc_per_day must be non-negative"

        try:
            token0 = to_checksum_address(metrics["token0"])
            token1 = to_checksum_address(metrics["token1"])
            tasks = [
                self._token_decimals(token0),
                self._token_decimals(token1),
            ]
            if token0_price_usdc is None:
                tasks.append(self.token_price_usdc(token0))
            if token1_price_usdc is None:
                tasks.append(self.token_price_usdc(token1))
            results = list(await asyncio.gather(*tasks))

            decimals0, decimals1 = results[:2]
            next_result = 2
            price0 = token0_price_usdc
            if price0 is None:
                price0 = results[next_result]
                next_result += 1
            price1 = token1_price_usdc
            if price1 is None:
                price1 = results[next_result]

            position_value_usdc: float | None = None
            if (
                price0 is not None
                and price1 is not None
                and math.isfinite(price0)
                and math.isfinite(price1)
            ):
                position_value_usdc = (
                    (metrics["amount0_now"] / (10**decimals0)) * price0
                ) + ((metrics["amount1_now"] / (10**decimals1)) * price1)
                if position_value_usdc <= 0:
                    position_value_usdc = None

            fees_per_day_usdc = 0.0
            apr_percent: float | None
            if volume_usdc_per_day <= 0:
                apr_percent = 0.0
            elif position_value_usdc is None:
                apr_percent = None
            else:
                in_range_fraction = expected_in_range_fraction
                if not metrics.get("in_range"):
                    in_range_fraction = 0.0
                fees_per_day_usdc = (
                    volume_usdc_per_day
                    * metrics["effective_fee_fraction_for_unstaked"]
                    * metrics["share_of_active_liquidity"]
                    * in_range_fraction
                )
                apr_percent = fees_per_day_usdc * 365.0 / position_value_usdc * 100.0

            return True, {
                "pool": metrics.get("pool"),
                "fee_apr_percent": apr_percent,
                "fees_per_day_usdc": fees_per_day_usdc,
                "position_value_usdc": position_value_usdc,
            }
        except Exception as exc:
            return False, str(exc)

    async def slipstream_sigma_annual_from_swaps(
        self,
        *,
        pool: str,
        lookback_blocks: int = 20_000,
        max_logs: int = 5000,
    ) -> tuple[bool, Any]:
        if lookback_blocks <= 0:
            return False, "lookback_blocks must be > 0"
        if max_logs <= 0:
            return False, "max_logs must be > 0"

        try:
            ok, state = await self.slipstream_pool_state(pool=pool)
            if not ok:
                return False, state

            decimals0, decimals1 = await asyncio.gather(
                self._token_decimals(state["token0"]),
                self._token_decimals(state["token1"]),
            )

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                latest = await web3.eth.block_number
                from_block = max(0, latest - lookback_blocks)
                logs = await self._get_logs_bounded(
                    web3,
                    from_block=from_block,
                    to_block=latest,
                    address=state["pool"],
                    topics=[SLIPSTREAM_SWAP_TOPIC0],
                    max_logs=max_logs,
                )
                if not logs:
                    return True, {
                        "pool": state["pool"],
                        "sigma_annual": None,
                        "sample_count": 0,
                        "seconds_covered": 0,
                    }

                timestamp_by_block: dict[int, int] = {}
                observations: list[tuple[int, int]] = []
                for log in logs:
                    data = log.get("data")
                    block_number = log.get("blockNumber")
                    if not data or block_number is None:
                        continue
                    try:
                        _amount0, _amount1, sqrt_price_x96, _liquidity, _tick = (
                            web3.codec.decode(
                                ["int256", "int256", "uint160", "uint128", "int24"],
                                data,
                            )
                        )
                    except Exception:
                        continue
                    block_number_i = block_number
                    observations.append((block_number_i, sqrt_price_x96))

                missing_blocks = sorted(
                    {
                        block_number_i
                        for block_number_i, _ in observations
                        if block_number_i not in timestamp_by_block
                    }
                )
                if missing_blocks:
                    blocks = await asyncio.gather(
                        *[
                            web3.eth.get_block(block_number_i)
                            for block_number_i in missing_blocks
                        ]
                    )
                    for block_number_i, block in zip(
                        missing_blocks, blocks, strict=True
                    ):
                        timestamp_by_block[block_number_i] = block["timestamp"]

                prices: list[tuple[int, float]] = []
                for block_number_i, sqrt_price_x96 in observations:
                    price = sqrt_price_x96_to_price(
                        sqrt_price_x96,
                        decimals0,
                        decimals1,
                    )
                    if price > 0:
                        prices.append((timestamp_by_block[block_number_i], price))

            if len(prices) < 5:
                return True, {
                    "pool": state["pool"],
                    "sigma_annual": None,
                    "sample_count": len(prices),
                    "seconds_covered": 0,
                }

            prices.sort(key=lambda item: item[0])
            sum_r2 = 0.0
            sum_dt = 0
            for i in range(1, len(prices)):
                timestamp0, price0 = prices[i - 1]
                timestamp1, price1 = prices[i]
                dt = timestamp1 - timestamp0
                if dt <= 0:
                    continue
                log_return = math.log(price1 / price0)
                sum_r2 += log_return * log_return
                sum_dt += dt

            if sum_dt <= 0:
                return True, {
                    "pool": state["pool"],
                    "sigma_annual": None,
                    "sample_count": len(prices),
                    "seconds_covered": 0,
                }

            sigma_per_second = math.sqrt(sum_r2 / sum_dt)
            return True, {
                "pool": state["pool"],
                "sigma_annual": sigma_per_second * math.sqrt(SECONDS_PER_YEAR),
                "sample_count": len(prices),
                "seconds_covered": sum_dt,
            }
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    @staticmethod
    def _phi(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    async def slipstream_prob_in_range_week(
        self,
        *,
        pool: str,
        tick_lower: int,
        tick_upper: int,
        sigma_annual: float,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        if tick_lower >= tick_upper:
            return False, "tick_lower must be < tick_upper"
        sigma = sigma_annual
        if not math.isfinite(sigma) or sigma <= 0:
            return False, "sigma_annual must be positive and finite"

        try:
            ok, state = await self.slipstream_pool_state(
                pool=pool,
                block_identifier=block_identifier,
            )
            if not ok:
                return False, state

            decimals0, decimals1 = await asyncio.gather(
                self._token_decimals(state["token0"]),
                self._token_decimals(state["token1"]),
            )
            price_now = state["price_token1_per_token0"]
            price_low = tick_to_price_decimal(tick_lower, decimals0, decimals1)
            price_high = tick_to_price_decimal(tick_upper, decimals0, decimals1)
            if price_now <= 0 or price_low <= 0 or price_high <= 0:
                return True, {
                    "pool": state["pool"],
                    "prob_in_range_week": None,
                }

            time_years = 7.0 / 365.0
            denom = sigma * math.sqrt(time_years)
            if denom <= 0:
                return True, {
                    "pool": state["pool"],
                    "prob_in_range_week": None,
                }

            z1 = math.log(price_low / price_now) / denom
            z2 = math.log(price_high / price_now) / denom
            prob = max(0.0, min(1.0, self._phi(z2) - self._phi(z1)))
            return True, {
                "pool": state["pool"],
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "sigma_annual": sigma,
                "prob_in_range_week": prob,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def mint_position(
        self,
        *,
        token0: str,
        token1: str,
        tick_spacing: int,
        tick_lower: int,
        tick_upper: int,
        amount0_desired: int,
        amount1_desired: int,
        deployment_variant: str | None = None,
        position_manager: str | None = None,
        amount0_min: int | None = None,
        amount1_min: int | None = None,
        slippage_bps: int = 50,
        recipient: str | None = None,
        deadline: int | None = None,
        sqrt_price_x96: int = 0,
    ) -> tuple[bool, Any]:
        if amount0_desired <= 0 or amount1_desired <= 0:
            return False, "amounts must be positive"
        if tick_upper <= tick_lower:
            return False, "tick_upper must be greater than tick_lower"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            _, deployment, npm_address = self._select_write_target(
                deployment_variant=deployment_variant,
                position_manager=position_manager,
            )
            owner = to_checksum_address(self.wallet_address)
            recipient_addr = to_checksum_address(recipient) if recipient else owner
            dl = deadline if deadline is not None else default_deadline()
            a0_min, a1_min = await self._resolve_position_amount_mins(
                deployment=deployment,
                token0=token0,
                token1=token1,
                tick_spacing=tick_spacing,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                amount0_desired=amount0_desired,
                amount1_desired=amount1_desired,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                slippage_bps=slippage_bps,
                initial_sqrt_price_x96=sqrt_price_x96,
            )

            approved0 = await ensure_allowance(
                token_address=to_checksum_address(token0),
                owner=owner,
                spender=npm_address,
                amount=amount0_desired,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved0[0]:
                return approved0

            approved1 = await ensure_allowance(
                token_address=to_checksum_address(token1),
                owner=owner,
                spender=npm_address,
                amount=amount1_desired,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved1[0]:
                return approved1

            params = (
                to_checksum_address(token0),
                to_checksum_address(token1),
                tick_spacing,
                tick_lower,
                tick_upper,
                amount0_desired,
                amount1_desired,
                a0_min,
                a1_min,
                recipient_addr,
                dl,
                sqrt_price_x96,
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="mint",
                args=[params],
                from_address=owner,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            token_id = await self._minted_erc721_token_id(
                nft_contract=npm_address,
                tx_hash=tx_hash,
                expected_to=recipient_addr,
            )
            return True, {"tx": tx_hash, "token_id": token_id}
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def increase_liquidity(
        self,
        *,
        token_id: int,
        amount0_desired: int,
        amount1_desired: int,
        position_manager: str | None = None,
        amount0_min: int | None = None,
        amount1_min: int | None = None,
        slippage_bps: int = 50,
        deadline: int | None = None,
    ) -> tuple[bool, Any]:
        if amount0_desired <= 0 or amount1_desired <= 0:
            return False, "amounts must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            variant, deployment, npm_address, owner = await self._resolve_token_manager(
                token_id=token_id,
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                npm = web3.eth.contract(
                    address=npm_address,
                    abi=AERODROME_SLIPSTREAM_NPM_ABI,
                )
                pos = await npm.functions.positions(token_id).call(
                    block_identifier="latest"
                )
                token0 = to_checksum_address(pos[2])
                token1 = to_checksum_address(pos[3])
                tick_spacing = pos[4]
                tick_lower = pos[5]
                tick_upper = pos[6]

            (
                amount0_min_resolved,
                amount1_min_resolved,
            ) = await self._resolve_position_amount_mins(
                deployment=deployment,
                token0=token0,
                token1=token1,
                tick_spacing=tick_spacing,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                amount0_desired=amount0_desired,
                amount1_desired=amount1_desired,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                slippage_bps=slippage_bps,
            )

            approved0 = await ensure_allowance(
                token_address=token0,
                owner=wallet,
                spender=npm_address,
                amount=amount0_desired,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved0[0]:
                return approved0

            approved1 = await ensure_allowance(
                token_address=token1,
                owner=wallet,
                spender=npm_address,
                amount=amount1_desired,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved1[0]:
                return approved1

            params = (
                token_id,
                amount0_desired,
                amount1_desired,
                amount0_min_resolved,
                amount1_min_resolved,
                deadline if deadline is not None else default_deadline(),
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="increaseLiquidity",
                args=[params],
                from_address=wallet,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx": tx_hash,
                "deployment_variant": variant,
                "position_manager": npm_address,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def decrease_liquidity(
        self,
        *,
        token_id: int,
        liquidity: int,
        position_manager: str | None = None,
        amount0_min: int | None = None,
        amount1_min: int | None = None,
        slippage_bps: int = 50,
        deadline: int | None = None,
    ) -> tuple[bool, Any]:
        if liquidity <= 0:
            return False, "liquidity must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            variant, deployment, npm_address, owner = await self._resolve_token_manager(
                token_id=token_id,
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                npm = web3.eth.contract(
                    address=npm_address,
                    abi=AERODROME_SLIPSTREAM_NPM_ABI,
                )
                pos = await npm.functions.positions(token_id).call(
                    block_identifier="latest"
                )
                token0 = to_checksum_address(pos[2])
                token1 = to_checksum_address(pos[3])
                tick_spacing = pos[4]
                tick_lower = pos[5]
                tick_upper = pos[6]

            (
                amount0_min_resolved,
                amount1_min_resolved,
            ) = await self._resolve_liquidity_amount_mins(
                deployment=deployment,
                token0=token0,
                token1=token1,
                tick_spacing=tick_spacing,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                liquidity=liquidity,
                amount0_min=amount0_min,
                amount1_min=amount1_min,
                slippage_bps=slippage_bps,
            )

            params = (
                token_id,
                liquidity,
                amount0_min_resolved,
                amount1_min_resolved,
                deadline if deadline is not None else default_deadline(),
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="decreaseLiquidity",
                args=[params],
                from_address=wallet,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx": tx_hash,
                "deployment_variant": variant,
                "position_manager": npm_address,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def collect_fees(
        self,
        *,
        token_id: int,
        position_manager: str | None = None,
        recipient: str | None = None,
        amount0_max: int = MAX_UINT128,
        amount1_max: int = MAX_UINT128,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            variant, _, npm_address, owner = await self._resolve_token_manager(
                token_id=token_id,
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            recipient_addr = to_checksum_address(recipient) if recipient else wallet
            params = (
                token_id,
                recipient_addr,
                amount0_max,
                amount1_max,
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="collect",
                args=[params],
                from_address=wallet,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx": tx_hash,
                "deployment_variant": variant,
                "position_manager": npm_address,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def burn_position(
        self,
        *,
        token_id: int,
        position_manager: str | None = None,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            variant, _, npm_address, owner = await self._resolve_token_manager(
                token_id=token_id,
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="burn",
                args=[token_id],
                from_address=wallet,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx": tx_hash,
                "deployment_variant": variant,
                "position_manager": npm_address,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def stake_position(
        self,
        *,
        gauge: str,
        token_id: int,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            wallet = to_checksum_address(self.wallet_address)
            gauge_addr = to_checksum_address(gauge)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                gauge_contract = web3.eth.contract(
                    address=gauge_addr,
                    abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                )
                alive, nft_address = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(voter, "isAlive", args=(gauge_addr,)),
                        Call(gauge_contract, "nft"),
                    ],
                    block_identifier="latest",
                )
                if not alive:
                    return False, "Gauge is not alive (killed/dead)"

            nft_addr = to_checksum_address(nft_address)
            _, _, _, owner = await self._resolve_token_manager(
                token_id=token_id,
                position_manager=nft_addr,
            )
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            approved = await self._ensure_erc721_approval(
                nft_contract=nft_addr,
                token_id=token_id,
                operator=gauge_addr,
                owner=wallet,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=gauge_addr,
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                fn_name="deposit",
                args=[token_id],
                from_address=wallet,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def unstake_position(
        self,
        *,
        gauge: str,
        token_id: int,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            tx = await encode_call(
                target=to_checksum_address(gauge),
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                fn_name="withdraw",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_position_rewards(
        self,
        *,
        gauge: str,
        token_id: int,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            tx = await encode_call(
                target=to_checksum_address(gauge),
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                fn_name="getReward",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_pos(
        self,
        *,
        token_id: int,
        position_manager: str | None = None,
        account: str | None = None,
        include_usd: bool = False,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            variant, _, npm_address, _ = await self._resolve_token_manager(
                token_id=token_id,
                position_manager=position_manager,
                block_identifier=block_identifier,
            )
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                pos = await self._read_position_state(
                    web3=web3,
                    deployment_variant=variant,
                    position_manager=npm_address,
                    token_id=token_id,
                    account=account,
                    include_usd=include_usd,
                    block_identifier=block_identifier,
                )
            return True, pos
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        deployments: Sequence[str] | None = None,
        include_usd: bool = False,
        include_zero_positions: bool = False,
        include_votes: bool = False,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            acct = to_checksum_address(account)
            deployment_names = self._resolve_deployments(deployments)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                deployment_specs: list[tuple[str, str, Any]] = []
                for variant in deployment_names:
                    deployment = self._deployment(variant)
                    npm_address = deployment["nonfungible_position_manager"]
                    deployment_specs.append(
                        (
                            variant,
                            npm_address,
                            web3.eth.contract(
                                address=npm_address,
                                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                            ),
                        )
                    )

                balances = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(npm, "balanceOf", args=(acct,))
                        for _, _, npm in deployment_specs
                    ],
                    block_identifier=block_identifier,
                )

                wallet_index_specs: list[tuple[str, str, Call]] = []
                for (variant, npm_address, npm), balance in zip(
                    deployment_specs, balances, strict=True
                ):
                    if balance <= 0:
                        continue
                    wallet_index_specs.extend(
                        (
                            variant,
                            npm_address,
                            Call(npm, "tokenOfOwnerByIndex", args=(acct, i)),
                        )
                        for i in range(balance)
                    )

                wallet_refs: list[tuple[str, str, int]] = []
                if wallet_index_specs:
                    wallet_token_ids = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[spec[2] for spec in wallet_index_specs],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    wallet_refs = [
                        (variant, npm_address, token_id)
                        for (variant, npm_address, _), token_id in zip(
                            wallet_index_specs, wallet_token_ids, strict=True
                        )
                    ]

                all_pools = await self._enumerate_all_pools(
                    web3=web3,
                    deployments=deployment_names,
                    block_identifier=block_identifier,
                )
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                pool_to_gauge = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(
                            voter,
                            "gauges",
                            args=(entry["pool"],),
                            postprocess=lambda a: _checksum_or_zero(a),
                        )
                        for entry in all_pools
                    ],
                    block_identifier=block_identifier,
                    chunk_size=100,
                )

                gauge_meta: dict[str, tuple[str, str]] = {}
                for entry, gauge in zip(all_pools, pool_to_gauge, strict=True):
                    if gauge == ZERO_ADDRESS:
                        continue
                    gauge_meta[gauge.lower()] = (
                        entry["deployment_variant"],
                        entry["position_manager"],
                    )

                staked_refs: list[tuple[str, str, int]] = []
                unique_gauges = [to_checksum_address(g) for g in gauge_meta]
                gauge_specs: list[tuple[str, str, str, Any]] = []
                for gauge_addr in unique_gauges:
                    variant, npm_address = gauge_meta[gauge_addr.lower()]
                    gauge_specs.append(
                        (
                            gauge_addr,
                            variant,
                            npm_address,
                            web3.eth.contract(
                                address=gauge_addr,
                                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                            ),
                        )
                    )

                staked_lengths = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(gauge_contract, "stakedLength", args=(acct,))
                        for _, _, _, gauge_contract in gauge_specs
                    ],
                    block_identifier=block_identifier,
                )

                staked_index_specs: list[tuple[str, str, Call]] = []
                for (_, variant, npm_address, gauge_contract), staked_len in zip(
                    gauge_specs, staked_lengths, strict=True
                ):
                    if staked_len <= 0:
                        continue
                    staked_index_specs.extend(
                        (
                            variant,
                            npm_address,
                            Call(gauge_contract, "stakedByIndex", args=(acct, i)),
                        )
                        for i in range(staked_len)
                    )

                if staked_index_specs:
                    staked_token_ids = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[spec[2] for spec in staked_index_specs],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    staked_refs = [
                        (variant, npm_address, token_id)
                        for (variant, npm_address, _), token_id in zip(
                            staked_index_specs, staked_token_ids, strict=True
                        )
                    ]

                refs_by_key: dict[tuple[str, int], tuple[str, str, int]] = {}
                for variant, npm_address, token_id in wallet_refs + staked_refs:
                    refs_by_key[(npm_address.lower(), token_id)] = (
                        variant,
                        npm_address,
                        token_id,
                    )

                positions = await asyncio.gather(
                    *[
                        self._read_position_state(
                            web3=web3,
                            deployment_variant=variant,
                            position_manager=npm_address,
                            token_id=token_id,
                            account=acct,
                            include_usd=include_usd,
                            block_identifier=block_identifier,
                        )
                        for variant, npm_address, token_id in refs_by_key.values()
                    ]
                )

                if not include_zero_positions:
                    positions = [
                        pos
                        for pos in positions
                        if pos["staked"]
                        or pos["liquidity"] > 0
                        or pos["tokens_owed0"] > 0
                        or pos["tokens_owed1"] > 0
                        or (pos.get("gauge_rewards_claimable") or 0) > 0
                    ]

                ok_ids, token_ids_any = await self.get_user_ve_nfts(
                    owner=acct,
                    block_identifier=block_identifier,
                )
                if not ok_ids:
                    return False, token_ids_any
                ve_token_ids = token_ids_any

                ve_items: list[dict[str, Any]] = []
                if ve_token_ids:
                    ve = web3.eth.contract(
                        address=self.core_contracts["voting_escrow"],
                        abi=AERODROME_VOTING_ESCROW_ABI,
                    )
                    rd = web3.eth.contract(
                        address=self.core_contracts["rewards_distributor"],
                        abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                    )
                    (
                        powers,
                        voted_flags,
                        claimables,
                        used_weights,
                        last_voted,
                    ) = await asyncio.gather(
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=[
                                Call(ve, "balanceOfNFT", args=(tid,))
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=[
                                Call(ve, "voted", args=(tid,)) for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=[
                                Call(
                                    rd,
                                    "claimable",
                                    args=(tid,),
                                )
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=[
                                Call(
                                    voter,
                                    "usedWeights",
                                    args=(tid,),
                                )
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=[
                                Call(
                                    voter,
                                    "lastVoted",
                                    args=(tid,),
                                )
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                    )

                    votes_by_token: dict[int, dict[str, int]] = {}
                    if include_votes and all_pools:
                        slipstream_pools = [
                            to_checksum_address(entry["pool"]) for entry in all_pools
                        ]
                        vote_values = await read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=[
                                Call(
                                    voter,
                                    "votes",
                                    args=(tid, pool_addr),
                                )
                                for tid in ve_token_ids
                                for pool_addr in slipstream_pools
                            ],
                            block_identifier=block_identifier,
                            chunk_size=200,
                        )
                        idx = 0
                        for tid in ve_token_ids:
                            votes_by_token[tid] = {}
                            for pool_addr in slipstream_pools:
                                votes_by_token[tid][pool_addr] = vote_values[idx]
                                idx += 1

                    for tid, power, voted, claimable, used_weight, voted_ts in zip(
                        ve_token_ids,
                        powers,
                        voted_flags,
                        claimables,
                        used_weights,
                        last_voted,
                        strict=True,
                    ):
                        item = {
                            "token_id": tid,
                            "voting_power": power,
                            "voted": voted,
                            "used_weight": used_weight,
                            "last_voted": voted_ts,
                            "rebase_claimable": claimable,
                        }
                        if include_votes:
                            item["votes"] = votes_by_token.get(tid, {})
                        ve_items.append(item)

            return True, {
                "protocol": "aerodrome_slipstream",
                "chain_id": CHAIN_ID_BASE,
                "chain_name": self.core_contracts["chain_name"],
                "account": acct,
                "deployments": deployment_names,
                "positions": positions,
                "ve_nfts": ve_items,
                "pool_count": len(all_pools),
                "gauge_count": len(unique_gauges),
            }
        except Exception as exc:
            return False, str(exc)

    async def get_vote_claimables(
        self,
        *,
        token_id: int,
        deployments: Sequence[str] | None = None,
        include_zero_positions: bool = False,
        include_usd_values: bool = False,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            deployment_names = self._resolve_deployments(deployments)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                all_pools = await self._enumerate_all_pools(
                    web3=web3,
                    deployments=deployment_names,
                    block_identifier=block_identifier,
                )
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                pool_to_gauge = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(
                            voter,
                            "gauges",
                            args=(entry["pool"],),
                            postprocess=_checksum_or_zero,
                        )
                        for entry in all_pools
                    ],
                    block_identifier=block_identifier,
                    chunk_size=100,
                )

                gauge_reward_contracts: dict[str, tuple[str, str]] = {}
                unique_gauges = sorted(
                    {
                        to_checksum_address(gauge)
                        for gauge in pool_to_gauge
                        if gauge != ZERO_ADDRESS
                    }
                )
                if unique_gauges:
                    reward_pairs = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[
                            item
                            for gauge in unique_gauges
                            for item in (
                                Call(
                                    voter,
                                    "gaugeToFees",
                                    args=(gauge,),
                                    postprocess=_checksum_or_zero,
                                ),
                                Call(
                                    voter,
                                    "gaugeToBribe",
                                    args=(gauge,),
                                    postprocess=_checksum_or_zero,
                                ),
                            )
                        ],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    fee_rewards = reward_pairs[0::2]
                    bribe_rewards = reward_pairs[1::2]
                    for gauge, fee_reward, bribe_reward in zip(
                        unique_gauges,
                        fee_rewards,
                        bribe_rewards,
                        strict=True,
                    ):
                        gauge_reward_contracts[gauge.lower()] = (
                            fee_reward,
                            bribe_reward,
                        )

                pool_metadata: dict[str, dict[str, Any]] = {}
                for entry, gauge in zip(all_pools, pool_to_gauge, strict=True):
                    fee_reward = ZERO_ADDRESS
                    bribe_reward = ZERO_ADDRESS
                    if gauge != ZERO_ADDRESS:
                        fee_reward, bribe_reward = gauge_reward_contracts.get(
                            gauge.lower(),
                            (ZERO_ADDRESS, ZERO_ADDRESS),
                        )
                    pool_metadata[entry["pool"].lower()] = {
                        "feeReward": fee_reward,
                        "bribeReward": bribe_reward,
                    }

                claimables = await self._get_vote_claimables(
                    token_id=token_id,
                    pool_metadata_by_address=pool_metadata,
                    web3=web3,
                    voter_contract=voter,
                    include_zero_positions=include_zero_positions,
                    include_usd_values=include_usd_values,
                    block_identifier=block_identifier,
                )

            return True, {
                "protocol": "aerodrome_slipstream",
                "chain_id": CHAIN_ID_BASE,
                "chain_name": self.core_contracts["chain_name"],
                "deployments": deployment_names,
                "tokenId": int(token_id),
                "votes": claimables,
            }
        except Exception as exc:
            return False, str(exc)
