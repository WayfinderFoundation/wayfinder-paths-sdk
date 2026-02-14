from __future__ import annotations

import asyncio
from typing import Any, Literal

from aiocache import Cache
from eth_utils import to_checksum_address
from eth_utils.abi import collapse_if_tuple

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.base import MANTISSA, MAX_UINT256, SECONDS_PER_YEAR
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import (
    BASE_WETH,
    MOONWELL_COMPTROLLER,
    MOONWELL_M_USDC,
    MOONWELL_REWARD_DISTRIBUTOR,
    MOONWELL_VIEWS,
    MOONWELL_WELL_TOKEN,
)
from wayfinder_paths.core.constants.moonwell_abi import (
    COMPTROLLER_ABI,
    MOONWELL_VIEWS_ABI,
    MTOKEN_ABI,
    REWARD_DISTRIBUTOR_ABI,
    WETH_ABI,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

CHAIN_NAME = "base"


def _timestamp_rate_to_apy(rate: float) -> float:
    return (1 + rate) ** SECONDS_PER_YEAR - 1


class MoonwellAdapter(BaseAdapter):
    adapter_type = "MOONWELL"

    # ---------------------------
    # Multicall decoding helpers
    # ---------------------------

    @staticmethod
    def _chunks(seq: list[Any], n: int) -> list[list[Any]]:
        return [seq[i : i + n] for i in range(0, len(seq), n)]

    @staticmethod
    def _fn_abi(
        contract: Any, fn_name: str, *, inputs_len: int | None = None
    ) -> dict[str, Any]:
        for item in contract.abi or []:
            if item.get("type") != "function":
                continue
            if item.get("name") != fn_name:
                continue
            if inputs_len is not None and len(item.get("inputs") or []) != inputs_len:
                continue
            return item
        raise ValueError(f"Function ABI not found: {fn_name} (inputs_len={inputs_len})")

    @staticmethod
    def _decode(web3: Any, fn_abi: dict[str, Any], data: bytes) -> tuple[Any, ...]:
        output_types = [
            collapse_if_tuple(o)
            for o in (fn_abi.get("outputs") or [])
            if isinstance(o, dict)
        ]
        if not output_types:
            return ()
        return tuple(web3.codec.decode(output_types, data))

    async def _multicall_chunked(
        self,
        *,
        multicall: MulticallAdapter,
        calls: list[Any],
        chunk_size: int,
    ) -> list[bytes]:
        """
        Execute multicall in chunks.

        If a chunk reverts, fall back to executing calls one-by-one so we can salvage
        partial results (returning b"" for failed calls).
        """
        out: list[bytes] = []
        for chunk in self._chunks(calls, max(1, int(chunk_size))):
            if not chunk:
                continue
            try:
                res = await multicall.aggregate(chunk)
                out.extend(list(res.return_data))
                continue
            except Exception:  # noqa: BLE001 - fall back to individual calls
                for call in chunk:
                    try:
                        r = await multicall.aggregate([call])
                        out.append(r.return_data[0] if r.return_data else b"")
                    except Exception:  # noqa: BLE001
                        out.append(b"")
        return out

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        strategy_wallet_signing_callback=None,
    ) -> None:
        super().__init__("moonwell_adapter", config)
        cfg = config or {}

        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

        self.chain_id = CHAIN_ID_BASE
        self.chain_name = CHAIN_NAME
        self.comptroller_address = MOONWELL_COMPTROLLER
        self.reward_distributor_address = MOONWELL_REWARD_DISTRIBUTOR
        self.m_usdc = MOONWELL_M_USDC

        strategy_addr = (cfg.get("strategy_wallet") or {}).get("address")
        self.strategy_wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )
        self._cache = Cache(Cache.MEMORY)

    async def lend(
        self,
        *,
        mtoken: str,
        underlying_token: str,
        amount: int,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        mtoken = to_checksum_address(mtoken)
        underlying_token = to_checksum_address(underlying_token)

        approved = await ensure_allowance(
            token_address=underlying_token,
            owner=strategy,
            spender=mtoken,
            amount=amount,
            chain_id=CHAIN_ID_BASE,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )
        if not approved[0]:
            return approved

        transaction = await encode_call(
            target=mtoken,
            abi=MTOKEN_ABI,
            fn_name="mint",
            args=[amount],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return (True, txn_hash)

    async def unlend(
        self,
        *,
        mtoken: str,
        amount: int,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        mtoken = to_checksum_address(mtoken)

        transaction = await encode_call(
            target=mtoken,
            abi=MTOKEN_ABI,
            fn_name="redeem",
            args=[amount],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return (True, txn_hash)

    async def borrow(
        self,
        *,
        mtoken: str,
        amount: int,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        mtoken = to_checksum_address(mtoken)

        transaction = await encode_call(
            target=mtoken,
            abi=MTOKEN_ABI,
            fn_name="borrow",
            args=[amount],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return (True, txn_hash)

    async def repay(
        self,
        *,
        mtoken: str,
        underlying_token: str,
        amount: int,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        mtoken = to_checksum_address(mtoken)
        underlying_token = to_checksum_address(underlying_token)

        approved = await ensure_allowance(
            token_address=underlying_token,
            owner=strategy,
            spender=mtoken,
            amount=amount,
            chain_id=CHAIN_ID_BASE,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )
        if not approved[0]:
            return approved

        # max uint256 avoids balance calculation race conditions
        repay_amount = MAX_UINT256 if repay_full else amount

        transaction = await encode_call(
            target=mtoken,
            abi=MTOKEN_ABI,
            fn_name="repayBorrow",
            args=[repay_amount],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return (True, txn_hash)

    async def set_collateral(
        self,
        *,
        mtoken: str,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        mtoken = to_checksum_address(mtoken)

        transaction = await encode_call(
            target=MOONWELL_COMPTROLLER,
            abi=COMPTROLLER_ABI,
            fn_name="enterMarkets",
            args=[[mtoken]],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )

        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                comptroller = web3.eth.contract(
                    address=MOONWELL_COMPTROLLER, abi=COMPTROLLER_ABI
                )
                is_member = await comptroller.functions.checkMembership(
                    strategy, mtoken
                ).call(block_identifier="pending")

                if not is_member:
                    self.logger.error(
                        f"set_collateral verification failed: account {strategy} "
                        f"is not a member of market {mtoken} after enterMarkets call"
                    )
                    return (
                        False,
                        f"enterMarkets succeeded but account is not a member of market {mtoken}",
                    )
        except Exception as e:
            self.logger.warning(f"Could not verify market membership: {e}")

        return (True, txn_hash)

    async def is_market_entered(
        self,
        *,
        mtoken: str,
        account: str | None = None,
    ) -> tuple[bool, bool | str]:
        try:
            acct = (
                to_checksum_address(account)
                if account
                else self.strategy_wallet_address
            )
            if not acct:
                return False, "strategy wallet address not configured"
            mtoken = to_checksum_address(mtoken)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                comptroller = web3.eth.contract(
                    address=MOONWELL_COMPTROLLER, abi=COMPTROLLER_ABI
                )
                is_member = await comptroller.functions.checkMembership(
                    acct, mtoken
                ).call(block_identifier="pending")
                return True, bool(is_member)
        except Exception as exc:
            return False, str(exc)

    async def remove_collateral(
        self,
        *,
        mtoken: str,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        mtoken = to_checksum_address(mtoken)

        transaction = await encode_call(
            target=MOONWELL_COMPTROLLER,
            abi=COMPTROLLER_ABI,
            fn_name="exitMarket",
            args=[mtoken],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return (True, txn_hash)

    async def claim_rewards(
        self,
        *,
        min_rewards_usd: float = 0.0,
    ) -> tuple[bool, dict[str, int] | str]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        rewards = await self._get_outstanding_rewards(strategy)

        if not rewards:
            return True, {}

        if min_rewards_usd > 0:
            total_usd = await self._calculate_rewards_usd(rewards)
            if total_usd < min_rewards_usd:
                return True, {}

        transaction = await encode_call(
            target=MOONWELL_COMPTROLLER,
            abi=COMPTROLLER_ABI,
            fn_name="claimReward",
            args=[strategy],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
        )
        await send_transaction(transaction, self.strategy_wallet_signing_callback)
        return True, rewards

    async def _get_outstanding_rewards(self, account: str) -> dict[str, int]:
        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                contract = web3.eth.contract(
                    address=MOONWELL_REWARD_DISTRIBUTOR, abi=REWARD_DISTRIBUTOR_ABI
                )

                all_rewards = await contract.functions.getOutstandingRewardsForUser(
                    account
                ).call(block_identifier="pending")

                rewards: dict[str, int] = {}
                for mtoken_data in all_rewards:
                    if len(mtoken_data) >= 2:
                        for reward_info in mtoken_data[1]:
                            if len(reward_info) >= 2:
                                token_addr, total_reward, *_ = reward_info
                                if total_reward > 0:
                                    key = f"{CHAIN_NAME}_{token_addr}"
                                    rewards[key] = rewards.get(key, 0) + total_reward
                return rewards
        except Exception:
            return {}

    async def _calculate_rewards_usd(self, rewards: dict[str, int]) -> float:
        total_usd = 0.0
        for token_key, amount in rewards.items():
            token_data = await TOKEN_CLIENT.get_token_details(
                token_key, market_data=True
            )
            if token_data:
                price = (
                    token_data.get("price_usd")
                    or token_data.get("price")
                    or token_data.get("current_price")
                    or 0
                )
                decimals = token_data.get("decimals", 18)
                total_usd += (amount / (10**decimals)) * price
        return total_usd

    # ------------------------------------------------------------------ #
    # Public API - Position & Market Data                                 #
    # ------------------------------------------------------------------ #

    async def get_full_user_state(
        self,
        *,
        account: str | None = None,
        include_rewards: bool = True,
        include_usd: bool = False,
        include_apy: bool = False,
        include_zero_positions: bool = False,
        multicall_chunk_size: int = 240,
        block_identifier: int | str | None = None,  # multicall ignores block id
    ) -> tuple[bool, dict[str, Any] | str]:
        _ = block_identifier  # reserved for future per-call block pinning
        acct = to_checksum_address(account) if account else self.strategy_wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                multicall = MulticallAdapter(chain_id=self.chain_id, web3=web3)

                comptroller = web3.eth.contract(
                    address=self.comptroller_address, abi=COMPTROLLER_ABI
                )
                rewards_contract = web3.eth.contract(
                    address=self.reward_distributor_address,
                    abi=REWARD_DISTRIBUTOR_ABI,
                )

                # --- Stage 1: global reads (batched)
                calls_stage1: list[Any] = [
                    multicall.build_call(
                        self.comptroller_address,
                        comptroller.encode_abi("getAllMarkets", args=[]),
                    ),
                    multicall.build_call(
                        self.comptroller_address,
                        comptroller.encode_abi("getAssetsIn", args=[acct]),
                    ),
                    multicall.build_call(
                        self.comptroller_address,
                        comptroller.encode_abi("getAccountLiquidity", args=[acct]),
                    ),
                ]
                if include_rewards:
                    calls_stage1.append(
                        multicall.build_call(
                            self.reward_distributor_address,
                            rewards_contract.encode_abi(
                                "getOutstandingRewardsForUser", args=[acct]
                            ),
                        )
                    )

                ret1 = await self._multicall_chunked(
                    multicall=multicall,
                    calls=calls_stage1,
                    chunk_size=multicall_chunk_size,
                )

                abi_all = self._fn_abi(comptroller, "getAllMarkets", inputs_len=0)
                abi_assets = self._fn_abi(comptroller, "getAssetsIn", inputs_len=1)
                abi_liq = self._fn_abi(comptroller, "getAccountLiquidity", inputs_len=1)

                all_markets = (
                    self._decode(web3, abi_all, ret1[0] or b"")[0]
                    if ret1 and ret1[0]
                    else []
                )
                assets_in = (
                    self._decode(web3, abi_assets, ret1[1] or b"")[0]
                    if len(ret1) > 1 and ret1[1]
                    else []
                )
                liq_tuple = (
                    self._decode(web3, abi_liq, ret1[2] or b"")
                    if len(ret1) > 2 and ret1[2]
                    else (0, 0, 0)
                )
                error, liquidity, shortfall = (
                    int(liq_tuple[0]),
                    int(liq_tuple[1]),
                    int(liq_tuple[2]),
                )

                entered = {str(a).lower() for a in (assets_in or [])}

                rewards: dict[str, int] = {}
                if include_rewards:
                    raw_rewards = ret1[3] if len(ret1) > 3 else b""
                    if raw_rewards:
                        abi_rewards = self._fn_abi(
                            rewards_contract,
                            "getOutstandingRewardsForUser",
                            inputs_len=1,
                        )
                        decoded = self._decode(web3, abi_rewards, raw_rewards)
                        try:
                            all_rewards = decoded[0]
                            for mtoken_data in all_rewards:
                                if len(mtoken_data) < 2:
                                    continue
                                token_rewards = mtoken_data[1] or []
                                for reward_info in token_rewards:
                                    if len(reward_info) < 2:
                                        continue
                                    token_addr = reward_info[0]
                                    total_reward = int(reward_info[1])
                                    if total_reward <= 0:
                                        continue
                                    key = f"{self.chain_name}_{token_addr}"
                                    rewards[key] = rewards.get(key, 0) + total_reward
                        except Exception:  # noqa: BLE001
                            rewards = await self._get_outstanding_rewards(acct)
                    else:
                        rewards = await self._get_outstanding_rewards(acct)

                # --- Stage 2: per-market reads (batched)
                market_calls: list[Any] = []
                market_meta: list[str] = []

                for m in all_markets or []:
                    mtoken = to_checksum_address(str(m))
                    mtoken_contract = web3.eth.contract(address=mtoken, abi=MTOKEN_ABI)
                    market_meta.append(mtoken)

                    market_calls.extend(
                        [
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi("balanceOf", args=[acct]),
                            ),
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi(
                                    "exchangeRateStored", args=[]
                                ),
                            ),
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi(
                                    "borrowBalanceStored", args=[acct]
                                ),
                            ),
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi("underlying", args=[]),
                            ),
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi("decimals", args=[]),
                            ),
                            multicall.build_call(
                                self.comptroller_address,
                                comptroller.encode_abi("markets", args=[mtoken]),
                            ),
                        ]
                    )

                ret2 = await self._multicall_chunked(
                    multicall=multicall,
                    calls=market_calls,
                    chunk_size=multicall_chunk_size,
                )

                sample_mtoken = web3.eth.contract(address=self.m_usdc, abi=MTOKEN_ABI)
                abi_bal = self._fn_abi(sample_mtoken, "balanceOf", inputs_len=1)
                abi_exch = self._fn_abi(
                    sample_mtoken, "exchangeRateStored", inputs_len=0
                )
                abi_borrow = self._fn_abi(
                    sample_mtoken, "borrowBalanceStored", inputs_len=1
                )
                abi_under = self._fn_abi(sample_mtoken, "underlying", inputs_len=0)
                abi_dec = self._fn_abi(sample_mtoken, "decimals", inputs_len=0)
                abi_mkts = self._fn_abi(comptroller, "markets", inputs_len=1)

                positions: list[dict[str, Any]] = []

                stride = 6
                for i, mtoken in enumerate(market_meta):
                    base = i * stride
                    if base + (stride - 1) >= len(ret2):
                        break

                    try:
                        bal_c = (
                            int(self._decode(web3, abi_bal, ret2[base + 0])[0])
                            if ret2[base + 0]
                            else 0
                        )
                        exch = (
                            int(self._decode(web3, abi_exch, ret2[base + 1])[0])
                            if ret2[base + 1]
                            else 0
                        )
                        borrow = (
                            int(self._decode(web3, abi_borrow, ret2[base + 2])[0])
                            if ret2[base + 2]
                            else 0
                        )
                        underlying = (
                            to_checksum_address(
                                str(self._decode(web3, abi_under, ret2[base + 3])[0])
                            )
                            if ret2[base + 3]
                            else None
                        )
                        mdec = (
                            int(self._decode(web3, abi_dec, ret2[base + 4])[0])
                            if ret2[base + 4]
                            else 18
                        )
                        mkts = (
                            self._decode(web3, abi_mkts, ret2[base + 5])
                            if ret2[base + 5]
                            else (False, 0)
                        )
                        is_listed = bool(mkts[0])
                        collateral_factor = float(int(mkts[1])) / MANTISSA
                    except Exception:  # noqa: BLE001 - skip malformed markets
                        continue

                    supplied_underlying = (bal_c * exch) // MANTISSA if exch else 0

                    has_supply = bal_c > 0
                    has_borrow = borrow > 0
                    if not include_zero_positions and not (has_supply or has_borrow):
                        continue

                    row: dict[str, Any] = {
                        "mtoken": mtoken,
                        "underlying": underlying,
                        "enteredAsCollateral": mtoken.lower() in entered,
                        "isListed": is_listed,
                        "collateralFactor": collateral_factor,
                        "mTokenDecimals": int(mdec),
                        "mTokenBalance": int(bal_c),
                        "exchangeRate": int(exch),
                        "suppliedUnderlying": int(supplied_underlying),
                        "borrowedUnderlying": int(borrow),
                    }

                    if include_apy:
                        try:
                            ok_s, apy_s = await self.get_apy(
                                mtoken=mtoken,
                                apy_type="supply",
                                include_rewards=True,
                            )
                            row["apySupply"] = apy_s if ok_s else None
                        except Exception:  # noqa: BLE001
                            row["apySupply"] = None

                        try:
                            ok_b, apy_b = await self.get_apy(
                                mtoken=mtoken,
                                apy_type="borrow",
                                include_rewards=True,
                            )
                            row["apyBorrow"] = apy_b if ok_b else None
                        except Exception:  # noqa: BLE001
                            row["apyBorrow"] = None

                    positions.append(row)

                out: dict[str, Any] = {
                    "protocol": "moonwell",
                    "chainId": int(self.chain_id),
                    "account": acct,
                    "accountLiquidity": {
                        "error": error,
                        "liquidity": int(liquidity),
                        "shortfall": int(shortfall),
                    },
                    "positions": positions,
                    "rewards": rewards,
                }

                if include_usd:
                    total_supplied_usd = 0.0
                    total_borrowed_usd = 0.0

                    for r in positions:
                        u = r.get("underlying")
                        if not u:
                            continue
                        key = f"{self.chain_name}_{u}"
                        td = await TOKEN_CLIENT.get_token_details(key, market_data=True)
                        if not td:
                            continue
                        price = (
                            td.get("price_usd")
                            or td.get("price")
                            or td.get("current_price")
                        )
                        dec = int(td.get("decimals", 18))
                        if price is None:
                            continue
                        total_supplied_usd += (
                            r["suppliedUnderlying"] / (10**dec)
                        ) * float(price)
                        total_borrowed_usd += (
                            r["borrowedUnderlying"] / (10**dec)
                        ) * float(price)

                    out["totalsUsd"] = {
                        "supplied": total_supplied_usd,
                        "borrowed": total_borrowed_usd,
                        "net": total_supplied_usd - total_borrowed_usd,
                    }
                    if include_rewards and rewards:
                        out["rewardsUsd"] = await self._calculate_rewards_usd(rewards)

                return True, out

        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        include_apy: bool = True,
        include_rewards: bool = True,
        include_usd: bool = False,
        multicall_chunk_size: int = 240,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                multicall = MulticallAdapter(chain_id=self.chain_id, web3=web3)
                views = web3.eth.contract(
                    address=MOONWELL_VIEWS, abi=MOONWELL_VIEWS_ABI
                )

                markets_info = await views.functions.getAllMarketsInfo().call(
                    block_identifier="pending"
                )
                if not markets_info:
                    return True, []

                # Build a filtered list of (market_info, market_address) pairs to ensure
                # markets_info and market_addrs always have matching lengths.
                filtered_markets = [
                    (m, to_checksum_address(str(m[0])))
                    for m in markets_info
                    if m and len(m) > 0 and m[0]
                ]
                if not filtered_markets:
                    return True, []

                markets_info = [m for m, _ in filtered_markets]
                market_addrs = [addr for _, addr in filtered_markets]
                # Fetch market metadata (symbol/underlying/decimals) via multicall.
                meta_calls: list[Any] = []
                for mtoken in market_addrs:
                    mtoken_contract = web3.eth.contract(address=mtoken, abi=MTOKEN_ABI)
                    meta_calls.extend(
                        [
                            multicall.build_call(
                                mtoken, mtoken_contract.encode_abi("symbol", args=[])
                            ),
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi("underlying", args=[]),
                            ),
                            multicall.build_call(
                                mtoken,
                                mtoken_contract.encode_abi("decimals", args=[]),
                            ),
                        ]
                    )

                ret_meta = await self._multicall_chunked(
                    multicall=multicall,
                    calls=meta_calls,
                    chunk_size=multicall_chunk_size,
                )

                sample_mtoken = web3.eth.contract(address=self.m_usdc, abi=MTOKEN_ABI)
                abi_symbol = self._fn_abi(sample_mtoken, "symbol", inputs_len=0)
                abi_under = self._fn_abi(sample_mtoken, "underlying", inputs_len=0)
                abi_dec = self._fn_abi(sample_mtoken, "decimals", inputs_len=0)

                meta_stride = 3
                metadata: dict[str, dict[str, Any]] = {}
                for i, mtoken in enumerate(market_addrs):
                    base = i * meta_stride
                    if base + (meta_stride - 1) >= len(ret_meta):
                        break
                    symbol = (
                        str(self._decode(web3, abi_symbol, ret_meta[base + 0])[0])
                        if ret_meta[base + 0]
                        else ""
                    )
                    underlying = (
                        to_checksum_address(
                            str(self._decode(web3, abi_under, ret_meta[base + 1])[0])
                        )
                        if ret_meta[base + 1]
                        else None
                    )
                    mdec = (
                        int(self._decode(web3, abi_dec, ret_meta[base + 2])[0])
                        if ret_meta[base + 2]
                        else 18
                    )
                    metadata[mtoken.lower()] = {
                        "symbol": symbol,
                        "underlying": underlying,
                        "mTokenDecimals": int(mdec),
                    }

                # Map underlying token -> oracle price mantissa (Comptroller style 1e(36 - decimals)).
                token_price_mantissa: dict[str, int] = {}
                for info, mtoken in zip(markets_info, market_addrs, strict=True):
                    md = metadata.get(mtoken.lower()) or {}
                    u = md.get("underlying")
                    if isinstance(u, str):
                        try:
                            token_price_mantissa[u.lower()] = int(info[7])
                        except Exception:  # noqa: BLE001
                            continue

                markets: list[dict[str, Any]] = []
                for info, mtoken in zip(markets_info, market_addrs, strict=True):
                    md = metadata.get(mtoken.lower()) or {}
                    underlying = md.get("underlying")

                    try:
                        is_listed = bool(info[1])
                        collateral_factor = float(int(info[6])) / MANTISSA
                        underlying_price = int(info[7])
                        total_supply = int(info[8])
                        total_borrows = int(info[9])
                        cash = int(info[11])
                        exchange_rate = int(info[12])
                        borrow_rate = int(info[15])
                        supply_rate = int(info[16])
                        incentives = info[17] or []
                    except Exception:  # noqa: BLE001 - skip malformed markets
                        continue

                    row: dict[str, Any] = {
                        "mtoken": mtoken,
                        "symbol": md.get("symbol", ""),
                        "underlying": underlying,
                        "mTokenDecimals": md.get("mTokenDecimals", 18),
                        "isListed": is_listed,
                        "collateralFactor": collateral_factor,
                        "exchangeRate": exchange_rate,
                        "totalSupply": total_supply,
                        "totalBorrows": total_borrows,
                        "cash": cash,
                    }

                    supply_underlying_raw = (
                        (int(total_supply) * int(exchange_rate)) // MANTISSA
                        if exchange_rate
                        else 0
                    )

                    if include_apy:
                        base_supply_apy = _timestamp_rate_to_apy(
                            int(supply_rate) / MANTISSA
                        )
                        base_borrow_apy = _timestamp_rate_to_apy(
                            int(borrow_rate) / MANTISSA
                        )
                        row["baseSupplyApy"] = base_supply_apy
                        row["baseBorrowApy"] = base_borrow_apy

                        supply_rewards_apr = 0.0
                        borrow_rewards_apr = 0.0
                        incentives_out: list[dict[str, Any]] = []

                        if include_rewards and incentives:
                            denom_supply = int(supply_underlying_raw) * int(
                                underlying_price
                            )
                            denom_borrow = int(total_borrows) * int(underlying_price)

                            for inc in incentives:
                                try:
                                    token = to_checksum_address(str(inc[0]))
                                    supply_speed = int(inc[1])
                                    borrow_speed = int(inc[2])
                                except Exception:  # noqa: BLE001
                                    continue

                                token_price = token_price_mantissa.get(token.lower())

                                inc_row: dict[str, Any] = {
                                    "token": token,
                                    "supplyEmissionsPerSec": supply_speed,
                                    "borrowEmissionsPerSec": borrow_speed,
                                }

                                if (
                                    token_price
                                    and denom_supply > 0
                                    and supply_speed > 0
                                ):
                                    inc_supply_apr = float(
                                        (supply_speed * SECONDS_PER_YEAR * token_price)
                                        / denom_supply
                                    )
                                    inc_row["supplyRewardsApy"] = inc_supply_apr
                                    supply_rewards_apr += inc_supply_apr
                                else:
                                    inc_row["supplyRewardsApy"] = 0.0

                                if (
                                    token_price
                                    and denom_borrow > 0
                                    and borrow_speed > 0
                                ):
                                    inc_borrow_apr = float(
                                        (borrow_speed * SECONDS_PER_YEAR * token_price)
                                        / denom_borrow
                                    )
                                    # Borrow incentives reduce net borrow APY (cost)
                                    inc_row["borrowRewardsApy"] = -inc_borrow_apr
                                    borrow_rewards_apr += inc_borrow_apr
                                else:
                                    inc_row["borrowRewardsApy"] = 0.0

                                incentives_out.append(inc_row)

                        row["rewardSupplyApy"] = supply_rewards_apr
                        row["rewardBorrowApy"] = -borrow_rewards_apr
                        row["supplyApy"] = base_supply_apy + supply_rewards_apr
                        row["borrowApy"] = base_borrow_apy - borrow_rewards_apr
                        row["incentives"] = incentives_out

                    if include_usd:
                        try:
                            row["totalSupplyUsd"] = (
                                (
                                    float(supply_underlying_raw)
                                    * float(underlying_price)
                                    / 1e36
                                )
                                if underlying_price
                                else None
                            )
                        except Exception:  # noqa: BLE001
                            row["totalSupplyUsd"] = None
                        try:
                            row["totalBorrowsUsd"] = (
                                (float(total_borrows) * float(underlying_price) / 1e36)
                                if underlying_price
                                else None
                            )
                        except Exception:  # noqa: BLE001
                            row["totalBorrowsUsd"] = None

                    markets.append(row)

                return True, markets
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_pos(
        self,
        *,
        mtoken: str,
        account: str | None = None,
        include_usd: bool = False,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        mtoken = to_checksum_address(mtoken)
        account = (
            to_checksum_address(account) if account else self.strategy_wallet_address
        )
        if not account:
            return False, "strategy wallet address not configured"
        block_id = block_identifier if block_identifier is not None else "pending"

        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                mtoken_contract = web3.eth.contract(address=mtoken, abi=MTOKEN_ABI)
                rewards_contract = web3.eth.contract(
                    address=MOONWELL_REWARD_DISTRIBUTOR,
                    abi=REWARD_DISTRIBUTOR_ABI,
                )

                bal = await mtoken_contract.functions.balanceOf(account).call(
                    block_identifier=block_id
                )
                exch = await mtoken_contract.functions.exchangeRateStored().call(
                    block_identifier=block_id
                )
                borrow = await mtoken_contract.functions.borrowBalanceStored(
                    account
                ).call(block_identifier=block_id)
                underlying = await mtoken_contract.functions.underlying().call(
                    block_identifier=block_id
                )
                rewards = await rewards_contract.functions.getOutstandingRewardsForUser(
                    mtoken, account
                ).call(block_identifier=block_id)
        except Exception as exc:
            return False, str(exc)

        try:
            reward_balances = self._process_rewards(rewards)

            mtoken_key = f"{CHAIN_NAME}_{mtoken}"
            underlying_key = f"{CHAIN_NAME}_{underlying}"

            balances: dict[str, int] = {mtoken_key: bal}
            balances.update(reward_balances)

            if borrow > 0:
                balances[underlying_key] = -borrow

            result: dict[str, Any] = {
                "balances": balances,
                "mtoken_balance": bal,
                "underlying_balance": (bal * exch) // MANTISSA,
                "borrow_balance": borrow,
                "exchange_rate": exch,
                "underlying_token": underlying,
            }

            if include_usd:
                usd_balances = await self._calculate_usd_balances(
                    balances, underlying_key
                )
                result["usd_balances"] = usd_balances

            return True, result
        except Exception as exc:
            return False, str(exc)

    def _process_rewards(self, rewards: list) -> dict[str, int]:
        result: dict[str, int] = {}
        for reward_info in rewards:
            if len(reward_info) >= 2:
                token_addr, total_reward, *_ = reward_info
                if total_reward > 0:
                    key = f"{CHAIN_NAME}_{token_addr}"
                    result[key] = total_reward
        return result

    async def _calculate_usd_balances(
        self, balances: dict[str, int], underlying_key: str
    ) -> dict[str, float | None]:
        tokens = list(set(balances.keys()) | {underlying_key})
        token_details = await asyncio.gather(
            *[TOKEN_CLIENT.get_token_details(key, market_data=True) for key in tokens]
        )
        token_data = dict(zip(tokens, token_details, strict=True))

        usd_balances: dict[str, float | None] = {}
        for token_key, bal in balances.items():
            data = token_data.get(token_key)
            if data and (
                price := data.get("price_usd")
                or data.get("price")
                or data.get("current_price")
            ):
                usd_balances[token_key] = (
                    bal / (10 ** data.get("decimals", 18))
                ) * price
            else:
                usd_balances[token_key] = None

        return usd_balances

    async def get_collateral_factor(
        self,
        *,
        mtoken: str,
    ) -> tuple[True, float] | tuple[False, str]:
        mtoken = to_checksum_address(mtoken)

        cache_key = f"cf_{mtoken}"
        if cached := await self._cache.get(cache_key):
            return True, cached

        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                contract = web3.eth.contract(
                    address=MOONWELL_COMPTROLLER, abi=COMPTROLLER_ABI
                )

                # markets() returns (isListed, collateralFactorMantissa)
                result = await contract.functions.markets(mtoken).call(
                    block_identifier="pending"
                )
                is_listed, collateral_factor_mantissa = result

                if not is_listed:
                    return False, f"Market {mtoken} is not listed"

                collateral_factor = collateral_factor_mantissa / MANTISSA
                await self._cache.set(cache_key, collateral_factor, ttl=3600)

                return True, collateral_factor
        except Exception as exc:
            return False, str(exc)

    async def get_apy(
        self,
        *,
        mtoken: str,
        apy_type: Literal["supply", "borrow"] = "supply",
        include_rewards: bool = True,
    ) -> tuple[bool, float | str]:
        mtoken = to_checksum_address(mtoken)

        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                mtoken_contract = web3.eth.contract(address=mtoken, abi=MTOKEN_ABI)
                reward_distributor = web3.eth.contract(
                    address=MOONWELL_REWARD_DISTRIBUTOR,
                    abi=REWARD_DISTRIBUTOR_ABI,
                )

                if apy_type == "supply":
                    rate_per_timestamp = (
                        await mtoken_contract.functions.supplyRatePerTimestamp().call(
                            block_identifier="pending"
                        )
                    )
                    mkt_config = []
                    total_value = 0
                    if include_rewards:
                        mkt_config = (
                            await reward_distributor.functions.getAllMarketConfigs(
                                mtoken
                            ).call(block_identifier="pending")
                        )
                        total_supply = (
                            await mtoken_contract.functions.totalSupply().call(
                                block_identifier="pending"
                            )
                        )
                        exch = (
                            await mtoken_contract.functions.exchangeRateStored().call(
                                block_identifier="pending"
                            )
                        )
                        # Convert mToken supply -> underlying supply for rewards APR denominator
                        total_value = (
                            (int(total_supply) * int(exch)) // MANTISSA if exch else 0
                        )
                else:
                    rate_per_timestamp = (
                        await mtoken_contract.functions.borrowRatePerTimestamp().call(
                            block_identifier="pending"
                        )
                    )
                    mkt_config = []
                    total_value = 0
                    if include_rewards:
                        mkt_config = (
                            await reward_distributor.functions.getAllMarketConfigs(
                                mtoken
                            ).call(block_identifier="pending")
                        )
                        total_value = (
                            await mtoken_contract.functions.totalBorrows().call(
                                block_identifier="pending"
                            )
                        )

                apy = _timestamp_rate_to_apy(rate_per_timestamp / MANTISSA)

                if include_rewards and total_value > 0:
                    rewards_apr = await self._calculate_rewards_apr(
                        mtoken, mkt_config, total_value, apy_type
                    )
                    if apy_type == "supply":
                        apy += rewards_apr
                    else:
                        # Borrow incentives reduce net borrow APY (cost)
                        apy -= rewards_apr

                return True, apy
        except Exception as exc:
            return False, str(exc)

    async def _calculate_rewards_apr(
        self,
        mtoken: str,
        mkt_config: list,
        total_value: int,
        apy_type: str,
    ) -> float:
        try:
            well_config = None
            for config in mkt_config:
                if (
                    len(config) >= 2
                    and config[1].lower() == MOONWELL_WELL_TOKEN.lower()
                ):
                    well_config = config
                    break

            if not well_config:
                return 0.0

            # Moonwell Base Multi-Reward Distributor MarketConfig has
            # supplyEmissionsPerSec and borrowEmissionsPerSec as the last 2 fields.
            if apy_type == "supply":
                well_rate = int(well_config[-2])
            else:
                well_rate = int(well_config[-1])
            if well_rate < 0:
                well_rate = -well_rate

            if well_rate == 0:
                return 0.0

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                mtoken_contract = web3.eth.contract(address=mtoken, abi=MTOKEN_ABI)
                underlying_addr = await mtoken_contract.functions.underlying().call(
                    block_identifier="pending"
                )

            well_data, underlying_data = await asyncio.gather(
                TOKEN_CLIENT.get_token_details(
                    f"{CHAIN_NAME}_{MOONWELL_WELL_TOKEN}", market_data=True
                ),
                TOKEN_CLIENT.get_token_details(
                    f"{CHAIN_NAME}_{underlying_addr}", market_data=True
                ),
            )

            well_price = (
                well_data.get("price_usd")
                or well_data.get("price")
                or well_data.get("current_price")
                or 0
                if well_data
                else 0
            )
            underlying_price = (
                underlying_data.get("price_usd")
                or underlying_data.get("price")
                or underlying_data.get("current_price")
                or 0
                if underlying_data
                else 0
            )
            underlying_decimals = (
                underlying_data.get("decimals", 18) if underlying_data else 18
            )

            if not well_price or not underlying_price:
                return 0.0

            total_value_usd = (
                total_value / (10**underlying_decimals)
            ) * underlying_price

            if total_value_usd == 0:
                return 0.0

            # rewards_apr = well_price * emissions_per_second * seconds_per_year / total_value_usd
            rewards_apr = (
                well_price * (well_rate / MANTISSA) * SECONDS_PER_YEAR / total_value_usd
            )

            return rewards_apr
        except Exception:
            return 0.0

    async def get_borrowable_amount(
        self,
        *,
        account: str | None = None,
    ) -> tuple[bool, int | str]:
        account = (
            to_checksum_address(account) if account else self.strategy_wallet_address
        )
        if not account:
            return False, "strategy wallet address not configured"

        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                contract = web3.eth.contract(
                    address=MOONWELL_COMPTROLLER, abi=COMPTROLLER_ABI
                )

                (
                    error,
                    liquidity,
                    shortfall,
                ) = await contract.functions.getAccountLiquidity(account).call(
                    block_identifier="pending"
                )

                if error != 0:
                    return False, f"Comptroller error: {error}"

                if shortfall > 0:
                    return False, f"Account has shortfall: {shortfall}"

                return True, liquidity
        except Exception as exc:
            return False, str(exc)

    async def max_withdrawable_mtoken(
        self,
        *,
        mtoken: str,
        account: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        mtoken = to_checksum_address(mtoken)
        account = (
            to_checksum_address(account) if account else self.strategy_wallet_address
        )
        if not account:
            return False, "strategy wallet address not configured"

        try:
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                comptroller = web3.eth.contract(
                    address=MOONWELL_COMPTROLLER, abi=COMPTROLLER_ABI
                )
                mtoken_contract = web3.eth.contract(address=mtoken, abi=MTOKEN_ABI)

                bal_raw, exch_raw, cash_raw, m_dec, u_addr = await asyncio.gather(
                    mtoken_contract.functions.balanceOf(account).call(
                        block_identifier="pending"
                    ),
                    mtoken_contract.functions.exchangeRateStored().call(
                        block_identifier="pending"
                    ),
                    mtoken_contract.functions.getCash().call(
                        block_identifier="pending"
                    ),
                    mtoken_contract.functions.decimals().call(
                        block_identifier="pending"
                    ),
                    mtoken_contract.functions.underlying().call(
                        block_identifier="pending"
                    ),
                )

                if bal_raw == 0 or exch_raw == 0:
                    return True, {
                        "cTokens_raw": 0,
                        "cTokens": 0.0,
                        "underlying_raw": 0,
                        "underlying": 0.0,
                        "bounds_raw": {"collateral_cTokens": 0, "cash_cTokens": 0},
                        "exchangeRate_raw": int(exch_raw),
                        "mToken_decimals": int(m_dec),
                        "underlying_decimals": None,
                    }

                u_data = await TOKEN_CLIENT.get_token_details(f"{CHAIN_NAME}_{u_addr}")
                u_dec = u_data.get("decimals", 18) if u_data else 18

                # Binary search: largest cTokens you can redeem without shortfall
                lo, hi = 0, int(bal_raw)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    (
                        err,
                        _liq,
                        short,
                    ) = await comptroller.functions.getHypotheticalAccountLiquidity(
                        account, mtoken, mid, 0
                    ).call(block_identifier="pending")
                    if err != 0:
                        return False, f"Comptroller error {err}"
                    if short == 0:
                        lo = mid
                    else:
                        hi = mid - 1

                c_by_collateral = lo

                # Pool cash bound (convert underlying cash -> cToken capacity)
                c_by_cash = (int(cash_raw) * MANTISSA) // int(exch_raw)

                redeem_c_raw = min(c_by_collateral, int(c_by_cash))

                # Final underlying you actually receive (mirror Solidity floor)
                under_raw = (redeem_c_raw * int(exch_raw)) // MANTISSA

                return True, {
                    "cTokens_raw": int(redeem_c_raw),
                    "cTokens": redeem_c_raw / (10 ** int(m_dec)),
                    "underlying_raw": int(under_raw),
                    "underlying": under_raw / (10 ** int(u_dec)),
                    "bounds_raw": {
                        "collateral_cTokens": int(c_by_collateral),
                        "cash_cTokens": int(c_by_cash),
                    },
                    "exchangeRate_raw": int(exch_raw),
                    "mToken_decimals": int(m_dec),
                    "underlying_decimals": int(u_dec),
                    "conversion_factor": redeem_c_raw / under_raw
                    if under_raw > 0
                    else 0,
                }
        except Exception as exc:
            return False, str(exc)

    async def wrap_eth(
        self,
        *,
        amount: int,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        transaction = await encode_call(
            target=BASE_WETH,
            abi=WETH_ABI,
            fn_name="deposit",
            args=[],
            from_address=strategy,
            chain_id=CHAIN_ID_BASE,
            value=amount,
        )
        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return (True, txn_hash)
