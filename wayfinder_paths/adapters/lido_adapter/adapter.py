from __future__ import annotations

import asyncio
from typing import Any, Literal

from eth_utils import is_address, to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.lido_abi import (
    STETH_LIDO_ABI,
    WITHDRAWAL_QUEUE_ABI,
    WSTETH_ABI,
)
from wayfinder_paths.core.constants.lido_contracts import LIDO_BY_CHAIN
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


def _safe_checksum(value: Any) -> str:
    if value is None:
        return ""

    value_str = str(value)

    if is_address(value_str):
        return to_checksum_address(value_str)

    return value_str


WITHDRAWAL_MIN_WEI = 100
WITHDRAWAL_MAX_WEI = 1000 * 10**18

ReceiveAsset = Literal["stETH", "wstETH"]


def _split_withdrawal_amount(amount_wei: int) -> list[int]:
    amount_wei = int(amount_wei)
    if amount_wei < WITHDRAWAL_MIN_WEI:
        raise ValueError(
            f"Withdrawal amount must be >= {WITHDRAWAL_MIN_WEI} wei, got {amount_wei}"
        )

    parts: list[int] = []
    remaining = amount_wei
    while remaining > 0:
        chunk = min(remaining, WITHDRAWAL_MAX_WEI)
        parts.append(chunk)
        remaining -= chunk

    if len(parts) >= 2 and parts[-1] < WITHDRAWAL_MIN_WEI:
        deficit = WITHDRAWAL_MIN_WEI - parts[-1]
        if parts[-2] < deficit:
            raise ValueError(
                "Withdrawal split bug: previous chunk too small to top up last chunk"
            )
        parts[-2] -= deficit
        parts[-1] += deficit

    if any(p < WITHDRAWAL_MIN_WEI for p in parts):
        raise ValueError(
            f"Failed to split withdrawal amount into chunks >= {WITHDRAWAL_MIN_WEI} wei"
        )
    if any(p > WITHDRAWAL_MAX_WEI for p in parts):
        raise ValueError(
            f"Failed to split withdrawal amount into chunks <= {WITHDRAWAL_MAX_WEI} wei"
        )
    if sum(parts) != amount_wei:
        raise ValueError("Withdrawal split bug: chunks do not sum to original amount")
    return parts


class LidoAdapter(BaseAdapter):
    adapter_type = "LIDO"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback=None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("lido_adapter", config or {})
        self.sign_callback = sign_callback
        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    def _require_wallet(self) -> str:
        if not self.wallet_address:
            raise ValueError("strategy wallet address not configured")
        return self.wallet_address

    def _entry(self, chain_id: int) -> dict[str, str]:
        entry = LIDO_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported Lido chain_id={chain_id}")
        return entry

    async def _get_staking_state(self, *, chain_id: int) -> tuple[bool, int]:
        entry = self._entry(chain_id)
        async with web3_from_chain_id(chain_id) as web3:
            steth = web3.eth.contract(address=entry["steth"], abi=STETH_LIDO_ABI)
            paused, limit = await asyncio.gather(
                steth.functions.isStakingPaused().call(block_identifier="pending"),
                steth.functions.getCurrentStakeLimit().call(block_identifier="pending"),
            )
            return bool(paused), int(limit)

    async def stake_eth(
        self,
        *,
        amount_wei: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
        referral: str = ZERO_ADDRESS,
        receive: ReceiveAsset = "stETH",
        check_limits: bool = True,
    ) -> tuple[bool, Any]:
        """
        Stake ETH into Lido to receive stETH (rebasing) or wstETH (non-rebasing wrapper).

        Notes:
        - stETH is minted by calling `submit(referral)` on the stETH contract with `value=amount_wei`.
        - wstETH receive is implemented as submit + wrap (2 tx; approvals handled internally).
        """

        if amount_wei <= 0:
            return False, "amount_wei must be positive"

        try:
            strategy = self._require_wallet()
            entry = self._entry(chain_id)
            referral = to_checksum_address(referral or ZERO_ADDRESS)

            if check_limits:
                paused, limit = await self._get_staking_state(chain_id=chain_id)
                if paused:
                    return False, "Lido staking is paused"
                if limit == 0:
                    return False, "Lido stake limit is 0 (paused or exhausted)"
                if limit != MAX_UINT256 and amount_wei > limit:
                    return (
                        False,
                        f"amount_wei exceeds current stake limit (limit={limit})",
                    )

            if receive == "stETH":
                tx = await encode_call(
                    target=entry["steth"],
                    abi=STETH_LIDO_ABI,
                    fn_name="submit",
                    args=[referral],
                    from_address=strategy,
                    chain_id=chain_id,
                    value=amount_wei,
                )
                tx_hash = await send_transaction(tx, self.sign_callback)
                return True, tx_hash

            if receive != "wstETH":
                return False, f"Unsupported receive asset: {receive}"

            before = await get_token_balance(
                entry["steth"], chain_id, strategy, block_identifier="pending"
            )

            stake_tx = await encode_call(
                target=entry["steth"],
                abi=STETH_LIDO_ABI,
                fn_name="submit",
                args=[referral],
                from_address=strategy,
                chain_id=chain_id,
                value=amount_wei,
            )
            stake_hash = await send_transaction(stake_tx, self.sign_callback)

            try:
                after = await get_token_balance(
                    entry["steth"], chain_id, strategy, block_identifier="pending"
                )
                wrap_amount = max(0, int(after) - int(before))
                if wrap_amount <= 0:
                    return True, {"stake_tx": stake_hash, "wrap_tx": None}

                approved = await ensure_allowance(
                    token_address=entry["steth"],
                    owner=strategy,
                    spender=entry["wsteth"],
                    amount=wrap_amount,
                    chain_id=chain_id,
                    signing_callback=self.sign_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return False, (
                        f"Stake succeeded (tx={stake_hash}) but wstETH approval"
                        f" failed: {approved[1]}"
                    )

                wrap_tx = await encode_call(
                    target=entry["wsteth"],
                    abi=WSTETH_ABI,
                    fn_name="wrap",
                    args=[wrap_amount],
                    from_address=strategy,
                    chain_id=chain_id,
                )
                wrap_hash = await send_transaction(wrap_tx, self.sign_callback)
                return True, {
                    "stake_tx": stake_hash,
                    "wrap_tx": wrap_hash,
                    "steth_wrapped": wrap_amount,
                }
            except Exception as wrap_exc:  
                return False, (
                    f"Stake succeeded (tx={stake_hash}) but wrap failed: {wrap_exc}"
                )
        except Exception as exc:  
            return False, str(exc)

    async def wrap_steth(
        self,
        *,
        amount_steth_wei: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        if amount_steth_wei <= 0:
            return False, "amount_steth_wei must be positive"

        try:
            strategy = self._require_wallet()
            entry = self._entry(chain_id)

            approved = await ensure_allowance(
                token_address=entry["steth"],
                owner=strategy,
                spender=entry["wsteth"],
                amount=amount_steth_wei,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=entry["wsteth"],
                abi=WSTETH_ABI,
                fn_name="wrap",
                args=[amount_steth_wei],
                from_address=strategy,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:  
            return False, str(exc)

    async def unwrap_wsteth(
        self,
        *,
        amount_wsteth_wei: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        if amount_wsteth_wei <= 0:
            return False, "amount_wsteth_wei must be positive"

        try:
            strategy = self._require_wallet()
            entry = self._entry(chain_id)
            tx = await encode_call(
                target=entry["wsteth"],
                abi=WSTETH_ABI,
                fn_name="unwrap",
                args=[amount_wsteth_wei],
                from_address=strategy,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:  
            return False, str(exc)

    async def request_withdrawal(
        self,
        *,
        asset: ReceiveAsset,
        amount_wei: int,
        owner: str | None = None,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        """
        Request an async withdrawal from Lido.

        This transfers stETH or wstETH to the WithdrawalQueue and mints an unstETH NFT.
        """

        if amount_wei <= 0:
            return False, "amount_wei must be positive"

        try:
            strategy = self._require_wallet()
            entry = self._entry(chain_id)

            owner_addr = to_checksum_address(owner) if owner else strategy

            amounts = _split_withdrawal_amount(amount_wei)

            if asset == "stETH":
                token = entry["steth"]
                fn_name = "requestWithdrawals"
            elif asset == "wstETH":
                token = entry["wsteth"]
                fn_name = "requestWithdrawalsWstETH"
            else:
                return False, f"Unsupported asset: {asset}"

            approved = await ensure_allowance(
                token_address=token,
                owner=strategy,
                spender=entry["withdrawal_queue"],
                amount=amount_wei,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=entry["withdrawal_queue"],
                abi=WITHDRAWAL_QUEUE_ABI,
                fn_name=fn_name,
                args=[amounts, owner_addr],
                from_address=strategy,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx": tx_hash,
                "asset": asset,
                "amounts": amounts,
                "owner": owner_addr,
            }
        except Exception as exc:  
            return False, str(exc)

    async def _find_checkpoint_hints(
        self,
        *,
        chain_id: int,
        request_ids: list[int],
        web3=None,
    ) -> list[int]:
        if not request_ids:
            return []

        entry = self._entry(chain_id)
        sorted_ids = sorted(set(request_ids))

        async def _query(w3):
            queue = w3.eth.contract(
                address=entry["withdrawal_queue"], abi=WITHDRAWAL_QUEUE_ABI
            )
            last = await queue.functions.getLastCheckpointIndex().call(
                block_identifier="pending"
            )
            last_i = int(last or 0)
            if last_i < 1:
                raise ValueError("WithdrawalQueue has no checkpoints (last=0)")

            hints = await queue.functions.findCheckpointHints(
                sorted_ids, 1, last_i
            ).call(block_identifier="pending")
            return [int(h) for h in (hints or [])]

        if web3:
            return await _query(web3)
        async with web3_from_chain_id(chain_id) as w3:
            return await _query(w3)

    async def claim_withdrawals(
        self,
        *,
        request_ids: list[int],
        recipient: str | None = None,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        if not request_ids:
            return False, "request_ids cannot be empty"

        chain_id = int(chain_id)

        try:
            strategy = self._require_wallet()
            entry = self._entry(chain_id)

            sorted_ids = sorted(set(request_ids))
            hints = await self._find_checkpoint_hints(
                chain_id=chain_id, request_ids=sorted_ids
            )

            if recipient:
                recipient_addr = to_checksum_address(recipient)
                fn_name = "claimWithdrawalsTo"
                args = [sorted_ids, hints, recipient_addr]
            else:
                fn_name = "claimWithdrawals"
                args = [sorted_ids, hints]

            tx = await encode_call(
                target=entry["withdrawal_queue"],
                abi=WITHDRAWAL_QUEUE_ABI,
                fn_name=fn_name,
                args=args,
                from_address=strategy,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:  
            return False, str(exc)

    async def get_withdrawal_requests(
        self,
        *,
        account: str,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, list[int] | str]:
        chain_id = int(chain_id)
        try:
            entry = self._entry(chain_id)
            acct = to_checksum_address(account)

            async with web3_from_chain_id(chain_id) as web3:
                queue = web3.eth.contract(
                    address=entry["withdrawal_queue"], abi=WITHDRAWAL_QUEUE_ABI
                )
                ids = await queue.functions.getWithdrawalRequests(acct).call(
                    block_identifier="pending"
                )
                return True, [int(i) for i in (ids or [])]
        except Exception as exc:  
            return False, str(exc)

    async def get_withdrawal_status(
        self,
        *,
        request_ids: list[int],
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        if not request_ids:
            return True, []

        chain_id = int(chain_id)
        try:
            entry = self._entry(chain_id)

            async with web3_from_chain_id(chain_id) as web3:
                queue = web3.eth.contract(
                    address=entry["withdrawal_queue"], abi=WITHDRAWAL_QUEUE_ABI
                )
                statuses = await queue.functions.getWithdrawalStatus(request_ids).call(
                    block_identifier="pending"
                )

            out: list[dict[str, Any]] = []
            for request_id, s in zip(request_ids, statuses or [], strict=False):
                # (amountOfStETH, amountOfShares, owner, timestamp, isFinalized, isClaimed)
                out.append(
                    {
                        "request_id": request_id,
                        "amount_of_steth": int(s[0]),
                        "amount_of_shares": int(s[1]),
                        "owner": _safe_checksum(s[2]),
                        "timestamp": int(s[3]),
                        "is_finalized": bool(s[4]),
                        "is_claimed": bool(s[5]),
                    }
                )
            return True, out
        except Exception as exc:  
            return False, str(exc)

    async def get_rates(
        self,
        *,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, dict[str, Any] | str]:
        chain_id = int(chain_id)
        try:
            entry = self._entry(chain_id)
            async with web3_from_chain_id(chain_id) as web3:
                wsteth = web3.eth.contract(address=entry["wsteth"], abi=WSTETH_ABI)
                steth_per, wsteth_per = await asyncio.gather(
                    wsteth.functions.stEthPerToken().call(block_identifier="pending"),
                    wsteth.functions.tokensPerStEth().call(block_identifier="pending"),
                )
                return True, {
                    "chain_id": chain_id,
                    "wsteth": entry["wsteth"],
                    "steth_per_wsteth": int(steth_per),
                    "wsteth_per_steth": int(wsteth_per),
                }
        except Exception as exc:  
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        chain_id: int = CHAIN_ID_ETHEREUM,
        include_withdrawals: bool = True,
        include_claimable: bool = False,
        include_usd: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        chain_id = int(chain_id)
        try:
            acct = to_checksum_address(account)
            entry = self._entry(chain_id)

            async with web3_from_chain_id(chain_id) as web3:
                steth = web3.eth.contract(address=entry["steth"], abi=STETH_LIDO_ABI)
                wsteth = web3.eth.contract(address=entry["wsteth"], abi=WSTETH_ABI)

                steth_balance_coro = get_token_balance(
                    entry["steth"], chain_id, acct, web3=web3, block_identifier="pending"
                )
                steth_shares_coro = steth.functions.sharesOf(acct).call(
                    block_identifier="pending"
                )
                wsteth_balance_coro = get_token_balance(
                    entry["wsteth"], chain_id, acct, web3=web3, block_identifier="pending"
                )

                steth_balance, steth_shares, wsteth_balance = await asyncio.gather(
                    steth_balance_coro, steth_shares_coro, wsteth_balance_coro
                )
                wsteth_steth_equiv = await wsteth.functions.getStETHByWstETH(
                    int(wsteth_balance)
                ).call(block_identifier="pending")

                steth_per_token = await wsteth.functions.stEthPerToken().call(
                    block_identifier="pending"
                )

                out: dict[str, Any] = {
                    "protocol": "lido",
                    "chain_id": chain_id,
                    "account": acct,
                    "steth": {
                        "address": entry["steth"],
                        "balance_raw": int(steth_balance),
                        "shares_raw": int(steth_shares),
                    },
                    "wsteth": {
                        "address": entry["wsteth"],
                        "balance_raw": int(wsteth_balance),
                        "steth_equivalent_raw": int(wsteth_steth_equiv),
                        "steth_per_token": int(steth_per_token),
                    },
                }

                if include_usd:
                    out["usd"] = {}
                    try:
                        steth_details = await TOKEN_CLIENT.get_token_details(
                            entry["steth"], market_data=True, chain_id=chain_id
                        )
                        wsteth_details = await TOKEN_CLIENT.get_token_details(
                            entry["wsteth"], market_data=True, chain_id=chain_id
                        )
                        steth_price = float(steth_details.get("current_price") or 0.0)
                        wsteth_price = float(wsteth_details.get("current_price") or 0.0)
                        out["usd"] = {
                            "steth_price": steth_price,
                            "wsteth_price": wsteth_price,
                            "steth_value": steth_price * (int(steth_balance) / 10**18),
                            "wsteth_value": wsteth_price
                            * (int(wsteth_balance) / 10**18),
                        }
                    except Exception:  
                        pass

                if not include_withdrawals:
                    return True, out

                queue = web3.eth.contract(
                    address=entry["withdrawal_queue"], abi=WITHDRAWAL_QUEUE_ABI
                )
                request_ids = await queue.functions.getWithdrawalRequests(acct).call(
                    block_identifier="pending"
                )
                ids_list = [int(i) for i in (request_ids or [])]
                out["withdrawals"] = {
                    "withdrawal_queue": entry["withdrawal_queue"],
                    "request_ids": ids_list,
                }

                if not ids_list:
                    return True, out

                statuses = await queue.functions.getWithdrawalStatus(ids_list).call(
                    block_identifier="pending"
                )
                status_rows: list[dict[str, Any]] = []
                for rid, s in zip(ids_list, statuses or [], strict=False):
                    status_rows.append(
                        {
                            "request_id": rid,
                            "amount_of_steth": int(s[0]),
                            "amount_of_shares": int(s[1]),
                            "owner": _safe_checksum(s[2]),
                            "timestamp": int(s[3]),
                            "is_finalized": bool(s[4]),
                            "is_claimed": bool(s[5]),
                        }
                    )
                out["withdrawals"]["statuses"] = status_rows

                if include_claimable:
                    sorted_ids = sorted(set(ids_list))
                    hints = await self._find_checkpoint_hints(
                        chain_id=chain_id, request_ids=sorted_ids, web3=web3
                    )
                    claimable = await queue.functions.getClaimableEther(
                        sorted_ids, hints
                    ).call(block_identifier="pending")
                    out["withdrawals"]["claimable_ether_by_id"] = {
                        str(rid): int(val)
                        for rid, val in zip(sorted_ids, claimable or [], strict=False)
                    }

            return True, out
        except Exception as exc:  
            return False, str(exc)
