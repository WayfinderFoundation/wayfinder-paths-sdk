from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from eth_utils import to_checksum_address

import wayfinder_paths.adapters.aerodrome_common as aerodrome_common
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
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
    AERODROME_SLIPSTREAM_DEFAULT_DEPLOYMENTS,
)
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.uniswap_v3_math import deadline as default_deadline
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

MAX_UINT128 = (1 << 128) - 1


def _checksum_or_zero(value: str | None) -> str:
    if not value:
        return ZERO_ADDRESS
    if str(value).lower() == ZERO_ADDRESS:
        return ZERO_ADDRESS
    return to_checksum_address(str(value))


def _resolve_amount_min(amount_min: int | None) -> int:
    # Slipstream desired amounts are max inputs, not a safe quote basis for bps mins.
    return 0 if amount_min is None else int(amount_min)


EPOCH_SPECIAL_WINDOW_SECONDS = aerodrome_common.EPOCH_SPECIAL_WINDOW_SECONDS
WEEK_SECONDS = aerodrome_common.WEEK_SECONDS


class AerodromeSlipstreamAdapter(
    aerodrome_common.AerodromeVotingRewardsMixin,
    BaseAdapter,
):
    adapter_type = "AERODROME_SLIPSTREAM"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("aerodrome_slipstream_adapter", config or {})
        self.sign_callback = sign_callback
        self.chain_id = CHAIN_ID_BASE

        entry = AERODROME_SLIPSTREAM_BY_CHAIN.get(CHAIN_ID_BASE)
        if not entry:
            raise ValueError("Aerodrome Slipstream Base deployment constants missing")
        
        self.chain_name = str(entry["chain_name"])
        self.aero = to_checksum_address(str(entry["aero"]))
        self.voter = to_checksum_address(str(entry["voter"]))
        self.voting_escrow = to_checksum_address(str(entry["voting_escrow"]))
        self.rewards_distributor = to_checksum_address(
            str(entry["rewards_distributor"])
        )
        self.weth = to_checksum_address(str(entry["weth"]))

        deployments = entry.get("deployments")
        if not isinstance(deployments, dict) or not deployments:
            raise ValueError("Aerodrome Slipstream deployment map missing")

        self.supported_deployments: dict[str, dict[str, str]] = {
            str(name): {
                str(key): to_checksum_address(str(val))
                for key, val in values.items()
                if isinstance(val, str)
            }
            for name, values in deployments.items()
            if isinstance(values, dict)
        }
        self.default_deployments: tuple[str, ...] = (
            tuple(self.config.get("deployments", []))
            if isinstance(self.config.get("deployments"), list)
            and self.config.get("deployments")
            else AERODROME_SLIPSTREAM_DEFAULT_DEPLOYMENTS
        )
        self.write_deployment = str(
            self.config.get("write_deployment") or self.default_deployments[0]
        )
        if self.write_deployment not in self.supported_deployments:
            raise ValueError(
                f"Unknown Slipstream write deployment: {self.write_deployment}"
            )

        self._variant_by_npm = {
            cfg["nonfungible_position_manager"].lower(): variant
            for variant, cfg in self.supported_deployments.items()
        }
        self._variant_by_factory = {
            cfg["pool_factory"].lower(): variant
            for variant, cfg in self.supported_deployments.items()
        }

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    def _resolve_deployments(
        self,
        deployments: Sequence[str] | None = None,
    ) -> list[str]:
        raw = list(deployments) if deployments is not None else list(self.default_deployments)
        if not raw:
            raw = list(AERODROME_SLIPSTREAM_DEFAULT_DEPLOYMENTS)

        normalized: list[str] = []
        seen: set[str] = set()
        for variant in raw:
            name = str(variant)
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

        variant = str(deployment_variant or self.write_deployment)
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
        token_id_i = int(token_id)
        if token_id_i <= 0:
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
        async with web3_from_chain_id(self.chain_id) as web3:
            for variant, pm in candidates:
                npm = web3.eth.contract(address=pm, abi=AERODROME_SLIPSTREAM_NPM_ABI)
                try:
                    owner = await npm.functions.ownerOf(token_id_i).call(
                        block_identifier=block_identifier
                    )
                except Exception:
                    continue
                matches.append((variant, pm, to_checksum_address(owner)))

        if not matches:
            raise ValueError(
                f"token_id {token_id_i} was not found in any configured position manager"
            )
        if len(matches) > 1 and position_manager is None:
            raise ValueError(
                "token_id exists in multiple Slipstream position managers; pass position_manager explicitly"
            )

        variant, pm, owner = matches[0]
        return variant, self._deployment(variant), pm, owner

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

        async with web3_from_chain_id(self.chain_id) as web3:
            nft = web3.eth.contract(address=nft_contract, abi=AERODROME_SLIPSTREAM_NPM_ABI)
            approved, approved_for_all = await asyncio.gather(
                nft.functions.getApproved(int(token_id)).call(block_identifier="pending"),
                nft.functions.isApprovedForAll(owner, operator).call(
                    block_identifier="pending"
                ),
            )
            if _checksum_or_zero(approved).lower() == operator.lower() or bool(
                approved_for_all
            ):
                return True, {}

        tx = await encode_call(
            target=nft_contract,
            abi=AERODROME_SLIPSTREAM_NPM_ABI,
            fn_name="approve",
            args=[operator, int(token_id)],
            from_address=owner,
            chain_id=self.chain_id,
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
        voter = web3.eth.contract(address=self.voter, abi=AERODROME_VOTER_ABI)
        pool = await factory.functions.getPool(
            to_checksum_address(token0),
            to_checksum_address(token1),
            int(tick_spacing),
        ).call(block_identifier=block_identifier)
        pool_addr = _checksum_or_zero(pool)
        if pool_addr == ZERO_ADDRESS:
            return ZERO_ADDRESS, ZERO_ADDRESS

        pool_contract = web3.eth.contract(
            address=pool_addr,
            abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
        )
        pool_gauge, voter_gauge = await asyncio.gather(
            pool_contract.functions.gauge().call(block_identifier=block_identifier),
            voter.functions.gauges(pool_addr).call(block_identifier=block_identifier),
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
            pool_gauge_addr
            if pool_gauge_addr != ZERO_ADDRESS
            else voter_gauge_addr
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
        voter = web3.eth.contract(address=self.voter, abi=AERODROME_VOTER_ABI)
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
        ) = await asyncio.gather(
            pool_contract.functions.token0().call(block_identifier=block_identifier),
            pool_contract.functions.token1().call(block_identifier=block_identifier),
            pool_contract.functions.gauge().call(block_identifier=block_identifier),
            pool_contract.functions.nft().call(block_identifier=block_identifier),
            pool_contract.functions.tickSpacing().call(block_identifier=block_identifier),
            pool_contract.functions.slot0().call(block_identifier=block_identifier),
            pool_contract.functions.fee().call(block_identifier=block_identifier),
            pool_contract.functions.unstakedFee().call(block_identifier=block_identifier),
            pool_contract.functions.liquidity().call(block_identifier=block_identifier),
            pool_contract.functions.stakedLiquidity().call(
                block_identifier=block_identifier
            ),
            pool_contract.functions.rewardRate().call(block_identifier=block_identifier),
            pool_contract.functions.rewardReserve().call(
                block_identifier=block_identifier
            ),
            pool_contract.functions.periodFinish().call(
                block_identifier=block_identifier
            ),
            pool_contract.functions.lastUpdated().call(block_identifier=block_identifier),
            voter.functions.gauges(pool_addr).call(block_identifier=block_identifier),
            factory.functions.getSwapFee(pool_addr).call(block_identifier=block_identifier),
            factory.functions.getUnstakedFee(pool_addr).call(
                block_identifier=block_identifier
            ),
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
        gauge = (
            pool_gauge_addr
            if pool_gauge_addr != ZERO_ADDRESS
            else voter_gauge_addr
        )

        fee_reward = ZERO_ADDRESS
        bribe_reward = ZERO_ADDRESS
        gauge_reward_token = ZERO_ADDRESS
        gauge_reward_rate = 0
        gauge_period_finish = 0
        is_alive = False

        if gauge != ZERO_ADDRESS and include_gauge_state:
            (
                fee_reward,
                bribe_reward,
                is_alive,
            ) = await asyncio.gather(
                voter.functions.gaugeToFees(gauge).call(block_identifier=block_identifier),
                voter.functions.gaugeToBribe(gauge).call(
                    block_identifier=block_identifier
                ),
                voter.functions.isAlive(gauge).call(block_identifier=block_identifier),
            )
            gauge_contract = web3.eth.contract(
                address=gauge,
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
            )
            gauge_reward_token, gauge_reward_rate, gauge_period_finish = await asyncio.gather(
                gauge_contract.functions.rewardToken().call(
                    block_identifier=block_identifier
                ),
                gauge_contract.functions.rewardRate().call(
                    block_identifier=block_identifier
                ),
                gauge_contract.functions.periodFinish().call(
                    block_identifier=block_identifier
                ),
            )

        return {
            "deployment_variant": deployment_variant,
            "cl_factory": deployment["pool_factory"],
            "position_manager": _checksum_or_zero(nft),
            "pool": pool_addr,
            "token0": to_checksum_address(token0),
            "token1": to_checksum_address(token1),
            "tick_spacing": int(tick_spacing),
            "swap_fee": int(swap_fee),
            "unstaked_fee": int(unstaked_fee),
            "pool_fee": int(pool_fee),
            "pool_unstaked_fee": int(pool_unstaked_fee),
            "gauge": gauge,
            "fee_reward": _checksum_or_zero(fee_reward),
            "bribe_reward": _checksum_or_zero(bribe_reward),
            "slot0": {
                "sqrtPriceX96": int(slot0[0]),
                "tick": int(slot0[1]),
            },
            "liquidity": int(liquidity),
            "staked_liquidity": int(staked_liquidity),
            "pool_reward_rate": int(pool_reward_rate),
            "pool_reward_reserve": int(pool_reward_reserve),
            "pool_period_finish": int(pool_period_finish),
            "pool_last_updated": int(pool_last_updated),
            "gauge_reward_token": _checksum_or_zero(gauge_reward_token),
            "gauge_reward_rate": int(gauge_reward_rate),
            "gauge_period_finish": int(gauge_period_finish),
            "is_alive": bool(is_alive),
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
        raw_pos, owner = await asyncio.gather(
            npm.functions.positions(int(token_id)).call(block_identifier=block_identifier),
            npm.functions.ownerOf(int(token_id)).call(block_identifier=block_identifier),
        )

        token0 = to_checksum_address(raw_pos[2])
        token1 = to_checksum_address(raw_pos[3])
        tick_spacing = int(raw_pos[4])
        tick_lower = int(raw_pos[5])
        tick_upper = int(raw_pos[6])
        liquidity = int(raw_pos[7])
        tokens_owed0 = int(raw_pos[10])
        tokens_owed1 = int(raw_pos[11])
        owner_addr = to_checksum_address(owner)

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
            swap_fee = int(market["swap_fee"])
            unstaked_fee = int(market["unstaked_fee"])
            slot0_dict = dict(market["slot0"])
            pool_liquidity = int(market["liquidity"])
            staked_liquidity = int(market["staked_liquidity"])
            gauge_reward_token = market["gauge_reward_token"]
            gauge_reward_rate = int(market["gauge_reward_rate"])
            gauge_period_finish = int(market["gauge_period_finish"])
            is_alive = bool(market["is_alive"])

        staked = gauge != ZERO_ADDRESS and owner_addr.lower() == gauge.lower()
        account_addr = to_checksum_address(account) if account else None
        staked_for_account: bool | None = None
        gauge_rewards_claimable: int | None = None

        if staked and account_addr and gauge != ZERO_ADDRESS:
            gauge_contract = web3.eth.contract(
                address=gauge,
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
            )
            contains, earned = await asyncio.gather(
                gauge_contract.functions.stakedContains(
                    account_addr,
                    int(token_id),
                ).call(block_identifier=block_identifier),
                gauge_contract.functions.earned(
                    account_addr,
                    int(token_id),
                ).call(block_identifier=block_identifier),
            )
            staked_for_account = bool(contains)
            gauge_rewards_claimable = int(earned) if contains else None

        return {
            "protocol": "aerodrome_slipstream",
            "chain_id": self.chain_id,
            "chain_name": self.chain_name,
            "token_id": int(token_id),
            "deployment_variant": deployment_variant,
            "position_manager": npm_address,
            "owner": owner_addr,
            "pool": pool,
            "gauge": gauge,
            "token0": token0,
            "token1": token1,
            "tick_spacing": tick_spacing,
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "liquidity": liquidity,
            "tokens_owed0": tokens_owed0,
            "tokens_owed1": tokens_owed1,
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
            "include_usd": bool(include_usd),
        }

    async def _enumerate_all_pools(
        self,
        *,
        web3: Any,
        deployments: Sequence[str],
        block_identifier: str | int = "latest",
    ) -> list[dict[str, Any]]:
        lengths: list[tuple[str, int]] = []
        for variant in deployments:
            deployment = self._deployment(variant)
            factory = web3.eth.contract(
                address=deployment["pool_factory"],
                abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
            )
            length = await factory.functions.allPoolsLength().call(
                block_identifier=block_identifier
            )
            lengths.append((variant, int(length)))

        results: list[dict[str, Any]] = []
        for variant, length in lengths:
            if length <= 0:
                continue
            deployment = self._deployment(variant)
            factory = web3.eth.contract(
                address=deployment["pool_factory"],
                abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
            )
            pools = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=self.chain_id,
                calls=[
                    Call(
                        factory,
                        "allPools",
                        args=(i,),
                        postprocess=lambda a: to_checksum_address(a),
                    )
                    for i in range(length)
                ],
                block_identifier=block_identifier,
                chunk_size=100,
            )
            for index, pool in enumerate(pools):
                results.append(
                    {
                        "deployment_variant": variant,
                        "cl_factory": deployment["pool_factory"],
                        "position_manager": deployment["nonfungible_position_manager"],
                        "pool": to_checksum_address(pool),
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

            async with web3_from_chain_id(self.chain_id) as web3:
                for variant in self._resolve_deployments(deployments):
                    deployment = self._deployment(variant)
                    factory = web3.eth.contract(
                        address=deployment["pool_factory"],
                        abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
                    )
                    spacings = (
                        [int(v) for v in tick_spacings]
                        if tick_spacings is not None
                        else [
                            int(v)
                            for v in await factory.functions.tickSpacings().call(
                                block_identifier=block_identifier
                            )
                        ]
                    )
                    if not spacings:
                        continue

                    pools = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=self.chain_id,
                        calls=[
                            Call(
                                factory,
                                "getPool",
                                args=(tA, tB, int(spacing)),
                                postprocess=lambda a: _checksum_or_zero(a),
                            )
                            for spacing in spacings
                        ],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    for spacing, pool in zip(spacings, pools, strict=True):
                        if pool == ZERO_ADDRESS:
                            continue
                        results.append(
                            {
                                "deployment_variant": variant,
                                "cl_factory": deployment["pool_factory"],
                                "position_manager": deployment[
                                    "nonfungible_position_manager"
                                ],
                                "tick_spacing": int(spacing),
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
                tick_spacings=[int(tick_spacing)],
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
            async with web3_from_chain_id(self.chain_id) as web3:
                pool_contract = web3.eth.contract(
                    address=pool_addr,
                    abi=AERODROME_SLIPSTREAM_CL_POOL_ABI,
                )
                voter = web3.eth.contract(address=self.voter, abi=AERODROME_VOTER_ABI)
                pool_gauge, voter_gauge = await asyncio.gather(
                    pool_contract.functions.gauge().call(
                        block_identifier=block_identifier
                    ),
                    voter.functions.gauges(pool_addr).call(
                        block_identifier=block_identifier
                    ),
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
                pool_gauge_addr
                if pool_gauge_addr != ZERO_ADDRESS
                else voter_gauge_addr
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
            start_i = max(0, int(start))
            deployment_names = self._resolve_deployments(deployments)

            async with web3_from_chain_id(self.chain_id) as web3:
                lengths: list[tuple[str, int]] = []
                for variant in deployment_names:
                    deployment = self._deployment(variant)
                    factory = web3.eth.contract(
                        address=deployment["pool_factory"],
                        abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
                    )
                    length = await factory.functions.allPoolsLength().call(
                        block_identifier=block_identifier
                    )
                    lengths.append((variant, int(length)))

                total = sum(length for _, length in lengths)
                if total == 0 or start_i >= total:
                    return True, {
                        "protocol": "aerodrome_slipstream",
                        "chain_id": self.chain_id,
                        "chain_name": self.chain_name,
                        "deployments": deployment_names,
                        "start": start_i,
                        "limit": limit,
                        "total": total,
                        "markets": [],
                    }

                end_i = total if limit is None else min(total, start_i + int(limit))
                selected: list[tuple[str, int, int]] = []
                cursor = 0
                for variant, length in lengths:
                    dep_start = max(0, start_i - cursor)
                    dep_end = min(length, end_i - cursor)
                    if dep_start < dep_end:
                        selected.append((variant, dep_start, dep_end))
                    cursor += length

                pool_refs: list[tuple[str, str]] = []
                for variant, dep_start, dep_end in selected:
                    deployment = self._deployment(variant)
                    factory = web3.eth.contract(
                        address=deployment["pool_factory"],
                        abi=AERODROME_SLIPSTREAM_CL_FACTORY_ABI,
                    )
                    pools = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=self.chain_id,
                        calls=[
                            Call(
                                factory,
                                "allPools",
                                args=(i,),
                                postprocess=lambda a: to_checksum_address(a),
                            )
                            for i in range(dep_start, dep_end)
                        ],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    pool_refs.extend((variant, p) for p in pools)

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
                "chain_id": self.chain_id,
                "chain_name": self.chain_name,
                "deployments": deployment_names,
                "start": start_i,
                "limit": limit,
                "total": total,
                "markets": markets,
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
        slippage_bps: int = 0,
        recipient: str | None = None,
        deadline: int | None = None,
        sqrt_price_x96: int = 0,
    ) -> tuple[bool, Any]:
        if amount0_desired <= 0 or amount1_desired <= 0:
            return False, "amounts must be positive"
        if int(tick_upper) <= int(tick_lower):
            return False, "tick_upper must be greater than tick_lower"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            _, _, npm_address = self._select_write_target(
                deployment_variant=deployment_variant,
                position_manager=position_manager,
            )
            owner = to_checksum_address(self.wallet_address)
            recipient_addr = to_checksum_address(recipient) if recipient else owner
            dl = int(deadline) if deadline is not None else default_deadline()
            a0_min = _resolve_amount_min(amount0_min)
            a1_min = _resolve_amount_min(amount1_min)

            approved0 = await ensure_allowance(
                token_address=to_checksum_address(token0),
                owner=owner,
                spender=npm_address,
                amount=int(amount0_desired),
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved0[0]:
                return approved0

            approved1 = await ensure_allowance(
                token_address=to_checksum_address(token1),
                owner=owner,
                spender=npm_address,
                amount=int(amount1_desired),
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved1[0]:
                return approved1

            params = (
                to_checksum_address(token0),
                to_checksum_address(token1),
                int(tick_spacing),
                int(tick_lower),
                int(tick_upper),
                int(amount0_desired),
                int(amount1_desired),
                int(a0_min),
                int(a1_min),
                recipient_addr,
                dl,
                int(sqrt_price_x96),
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="mint",
                args=[params],
                from_address=owner,
                chain_id=self.chain_id,
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
        slippage_bps: int = 0,
        deadline: int | None = None,
    ) -> tuple[bool, Any]:
        if amount0_desired <= 0 or amount1_desired <= 0:
            return False, "amounts must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            variant, _, npm_address, owner = await self._resolve_token_manager(
                token_id=int(token_id),
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            async with web3_from_chain_id(self.chain_id) as web3:
                npm = web3.eth.contract(
                    address=npm_address,
                    abi=AERODROME_SLIPSTREAM_NPM_ABI,
                )
                pos = await npm.functions.positions(int(token_id)).call(
                    block_identifier="latest"
                )
                token0 = to_checksum_address(pos[2])
                token1 = to_checksum_address(pos[3])

            approved0 = await ensure_allowance(
                token_address=token0,
                owner=wallet,
                spender=npm_address,
                amount=int(amount0_desired),
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved0[0]:
                return approved0

            approved1 = await ensure_allowance(
                token_address=token1,
                owner=wallet,
                spender=npm_address,
                amount=int(amount1_desired),
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved1[0]:
                return approved1

            params = (
                int(token_id),
                int(amount0_desired),
                int(amount1_desired),
                _resolve_amount_min(amount0_min),
                _resolve_amount_min(amount1_min),
                int(deadline) if deadline is not None else default_deadline(),
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="increaseLiquidity",
                args=[params],
                from_address=wallet,
                chain_id=self.chain_id,
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
        amount0_min: int = 0,
        amount1_min: int = 0,
        deadline: int | None = None,
    ) -> tuple[bool, Any]:
        if liquidity <= 0:
            return False, "liquidity must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            variant, _, npm_address, owner = await self._resolve_token_manager(
                token_id=int(token_id),
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            params = (
                int(token_id),
                int(liquidity),
                int(amount0_min),
                int(amount1_min),
                int(deadline) if deadline is not None else default_deadline(),
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="decreaseLiquidity",
                args=[params],
                from_address=wallet,
                chain_id=self.chain_id,
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
                token_id=int(token_id),
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            recipient_addr = to_checksum_address(recipient) if recipient else wallet
            params = (
                int(token_id),
                recipient_addr,
                int(amount0_max),
                int(amount1_max),
            )
            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="collect",
                args=[params],
                from_address=wallet,
                chain_id=self.chain_id,
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
                token_id=int(token_id),
                position_manager=position_manager,
            )
            wallet = to_checksum_address(self.wallet_address)
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            tx = await encode_call(
                target=npm_address,
                abi=AERODROME_SLIPSTREAM_NPM_ABI,
                fn_name="burn",
                args=[int(token_id)],
                from_address=wallet,
                chain_id=self.chain_id,
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

            async with web3_from_chain_id(self.chain_id) as web3:
                voter = web3.eth.contract(address=self.voter, abi=AERODROME_VOTER_ABI)
                gauge_contract = web3.eth.contract(
                    address=gauge_addr,
                    abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                )
                alive, nft_address = await asyncio.gather(
                    voter.functions.isAlive(gauge_addr).call(block_identifier="latest"),
                    gauge_contract.functions.nft().call(block_identifier="latest"),
                )
                if not alive:
                    return False, "Gauge is not alive (killed/dead)"

            nft_addr = to_checksum_address(nft_address)
            _, _, _, owner = await self._resolve_token_manager(
                token_id=int(token_id),
                position_manager=nft_addr,
            )
            if owner.lower() != wallet.lower():
                return False, "wallet does not currently own token_id"

            approved = await self._ensure_erc721_approval(
                nft_contract=nft_addr,
                token_id=int(token_id),
                operator=gauge_addr,
                owner=wallet,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=gauge_addr,
                abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                fn_name="deposit",
                args=[int(token_id)],
                from_address=wallet,
                chain_id=self.chain_id,
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
                args=[int(token_id)],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
                args=[int(token_id)],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
                token_id=int(token_id),
                position_manager=position_manager,
                block_identifier=block_identifier,
            )
            async with web3_from_chain_id(self.chain_id) as web3:
                pos = await self._read_position_state(
                    web3=web3,
                    deployment_variant=variant,
                    position_manager=npm_address,
                    token_id=int(token_id),
                    account=account,
                    include_usd=include_usd,
                    block_identifier=block_identifier,
                )
            return True, pos
        except Exception as exc:
            return False, str(exc)

    async def get_position(
        self,
        *,
        token_id: int,
        position_manager: str | None = None,
        account: str | None = None,
        include_usd: bool = False,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        return await self.get_pos(
            token_id=token_id,
            position_manager=position_manager,
            account=account,
            include_usd=include_usd,
            block_identifier=block_identifier,
        )

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

            async with web3_from_chain_id(self.chain_id) as web3:
                wallet_refs: list[tuple[str, str, int]] = []
                for variant in deployment_names:
                    deployment = self._deployment(variant)
                    npm_address = deployment["nonfungible_position_manager"]
                    npm = web3.eth.contract(
                        address=npm_address,
                        abi=AERODROME_SLIPSTREAM_NPM_ABI,
                    )
                    balance = await npm.functions.balanceOf(acct).call(
                        block_identifier=block_identifier
                    )
                    if int(balance) <= 0:
                        continue
                    token_ids = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=self.chain_id,
                        calls=[
                            Call(
                                npm,
                                "tokenOfOwnerByIndex",
                                args=(acct, i),
                                postprocess=int,
                            )
                            for i in range(int(balance))
                        ],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    wallet_refs.extend(
                        (variant, npm_address, int(token_id)) for token_id in token_ids
                    )

                all_pools = await self._enumerate_all_pools(
                    web3=web3,
                    deployments=deployment_names,
                    block_identifier=block_identifier,
                )
                voter = web3.eth.contract(address=self.voter, abi=AERODROME_VOTER_ABI)
                pool_to_gauge = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=self.chain_id,
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
                        str(entry["deployment_variant"]),
                        str(entry["position_manager"]),
                    )

                staked_refs: list[tuple[str, str, int]] = []
                unique_gauges = [to_checksum_address(g) for g in gauge_meta]
                for gauge_addr in unique_gauges:
                    variant, npm_address = gauge_meta[gauge_addr.lower()]
                    gauge_contract = web3.eth.contract(
                        address=gauge_addr,
                        abi=AERODROME_SLIPSTREAM_CL_GAUGE_ABI,
                    )
                    staked_len = await gauge_contract.functions.stakedLength(acct).call(
                        block_identifier=block_identifier
                    )
                    if int(staked_len) <= 0:
                        continue
                    token_ids = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=self.chain_id,
                        calls=[
                            Call(
                                gauge_contract,
                                "stakedByIndex",
                                args=(acct, i),
                                postprocess=int,
                            )
                            for i in range(int(staked_len))
                        ],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )
                    staked_refs.extend(
                        (variant, npm_address, int(token_id)) for token_id in token_ids
                    )

                refs_by_key: dict[tuple[str, int], tuple[str, str, int]] = {}
                for variant, npm_address, token_id in wallet_refs + staked_refs:
                    refs_by_key[(npm_address.lower(), int(token_id))] = (
                        variant,
                        npm_address,
                        int(token_id),
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
                        if bool(pos["staked"])
                        or int(pos["liquidity"]) > 0
                        or int(pos["tokens_owed0"]) > 0
                        or int(pos["tokens_owed1"]) > 0
                        or int(pos.get("gauge_rewards_claimable") or 0) > 0
                    ]

                ok_ids, token_ids_any = await self.get_user_ve_nfts(
                    owner=acct,
                    block_identifier=block_identifier,
                )
                if not ok_ids:
                    return False, token_ids_any
                ve_token_ids = [int(tid) for tid in token_ids_any]

                ve_items: list[dict[str, Any]] = []
                if ve_token_ids:
                    ve = web3.eth.contract(
                        address=self.voting_escrow,
                        abi=AERODROME_VOTING_ESCROW_ABI,
                    )
                    rd = web3.eth.contract(
                        address=self.rewards_distributor,
                        abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                    )
                    powers, voted_flags, claimables, used_weights, last_voted = await asyncio.gather(
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=self.chain_id,
                            calls=[
                                Call(ve, "balanceOfNFT", args=(tid,), postprocess=int)
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=self.chain_id,
                            calls=[
                                Call(ve, "voted", args=(tid,), postprocess=bool)
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=self.chain_id,
                            calls=[
                                Call(
                                    rd,
                                    "claimable",
                                    args=(tid,),
                                    postprocess=int,
                                )
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=self.chain_id,
                            calls=[
                                Call(
                                    voter,
                                    "usedWeights",
                                    args=(tid,),
                                    postprocess=int,
                                )
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=self.chain_id,
                            calls=[
                                Call(
                                    voter,
                                    "lastVoted",
                                    args=(tid,),
                                    postprocess=int,
                                )
                                for tid in ve_token_ids
                            ],
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                    )

                    votes_by_token: dict[int, dict[str, int]] = {}
                    if include_votes and all_pools:
                        slipstream_pools = [to_checksum_address(entry["pool"]) for entry in all_pools]
                        vote_values = await read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=self.chain_id,
                            calls=[
                                Call(
                                    voter,
                                    "votes",
                                    args=(tid, pool_addr),
                                    postprocess=int,
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
                                votes_by_token[tid][pool_addr] = int(vote_values[idx])
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
                            "token_id": int(tid),
                            "voting_power": int(power),
                            "voted": bool(voted),
                            "used_weight": int(used_weight),
                            "last_voted": int(voted_ts),
                            "rebase_claimable": int(claimable),
                        }
                        if include_votes:
                            item["votes"] = votes_by_token.get(int(tid), {})
                        ve_items.append(item)

            return True, {
                "protocol": "aerodrome_slipstream",
                "chain_id": self.chain_id,
                "chain_name": self.chain_name,
                "account": acct,
                "deployments": deployment_names,
                "positions": positions,
                "ve_nfts": ve_items,
                "pool_count": len(all_pools),
                "gauge_count": len(unique_gauges),
            }
        except Exception as exc:
            return False, str(exc)
