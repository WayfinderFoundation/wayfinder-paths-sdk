from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3._utils.events import event_abi_to_log_topic, get_event_data

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.erc721_abi import ERC721_TRANSFER_EVENT_ABI
from wayfinder_paths.core.constants.etherfi_abi import (
    ETHERFI_EETH_ABI,
    ETHERFI_LIQUIDITY_POOL_ABI,
    ETHERFI_WEETH_ABI,
    ETHERFI_WITHDRAW_REQUEST_NFT_ABI,
)
from wayfinder_paths.core.constants.etherfi_contracts import (
    ETHERFI_BY_CHAIN,
    weeth_token_by_chain_id,
)
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

_WITHDRAW_REQUEST_CREATED_EVENT_ABI = next(
    i
    for i in ETHERFI_WITHDRAW_REQUEST_NFT_ABI
    if i.get("type") == "event" and i.get("name") == "WithdrawRequestCreated"
)

_WITHDRAW_REQUEST_CREATED_TOPIC0 = HexBytes(
    event_abi_to_log_topic(_WITHDRAW_REQUEST_CREATED_EVENT_ABI)
)

_ERC721_TRANSFER_TOPIC0 = HexBytes(event_abi_to_log_topic(ERC721_TRANSFER_EVENT_ABI))


def _as_hex_bytes32(value: str | bytes | int) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        if len(b) > 32:
            raise ValueError("bytes32 too long")
        return b.rjust(32, b"\x00")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("bytes32 int must be non-negative")
        return int(value).to_bytes(32, "big")
    s = str(value).strip()
    if s.startswith("0x"):
        s = s[2:]
    b = bytes.fromhex(s)
    if len(b) > 32:
        raise ValueError("bytes32 hex too long")
    return b.rjust(32, b"\x00")


def _normalize_permit_tuple(permit: Any) -> tuple[int, int, int, bytes, bytes]:
    """
    Normalize a permit input to (value, deadline, v, r, s).

    Accepts:
    - dict-like with keys: value, deadline, v, r, s
    - tuple/list: (value, deadline, v, r, s)
    """
    if isinstance(permit, dict):
        value = int(permit["value"])
        deadline = int(permit["deadline"])
        v = int(permit["v"])
        r = _as_hex_bytes32(permit["r"])
        s = _as_hex_bytes32(permit["s"])
        return value, deadline, v, r, s

    if isinstance(permit, (tuple, list)) and len(permit) == 5:
        value = int(permit[0])
        deadline = int(permit[1])
        v = int(permit[2])
        r = _as_hex_bytes32(permit[3])
        s = _as_hex_bytes32(permit[4])
        return value, deadline, v, r, s

    raise ValueError(
        "permit must be a dict or 5-tuple/list: (value, deadline, v, r, s)"
    )


class EtherfiAdapter(BaseAdapter):
    """
    ether.fi ETH liquid restaking adapter (Ethereum mainnet core flow).

    Core actions:
    - Stake ETH -> eETH via LiquidityPool.deposit() payable (returns shares)
    - Wrap eETH -> weETH via WeETH.wrap / wrapWithPermit
    - Request withdrawal (async) via LiquidityPool.requestWithdraw / requestWithdrawWithPermit
      (mints a WithdrawRequest NFT to the recipient/owner)
    - Claim withdrawal via WithdrawRequestNFT.claimWithdraw(tokenId) once finalized
    """

    adapter_type = "ETHERFI"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("etherfi_adapter", config or {})
        self.sign_callback = sign_callback
        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    def _entry(self, chain_id: int) -> dict[str, str]:
        entry = ETHERFI_BY_CHAIN.get(chain_id)
        if not entry:
            raise ValueError(f"Unsupported ether.fi chain_id={chain_id} (mainnet only)")
        return entry


    async def _parse_request_id_from_receipt(
        self, *, chain_id: int, tx_hash: str, expected_owner: str | None = None
    ) -> int | None:
        entry = self._entry(chain_id)
        nft_addr = entry["withdraw_request_nft"].lower()
        expected_owner_l = expected_owner.lower() if expected_owner else None

        async with web3_from_chain_id(chain_id) as web3:
            receipt = await web3.eth.get_transaction_receipt(tx_hash)
            logs = (receipt or {}).get("logs") or []

            transfer_mint_id: int | None = None

            for log in logs if isinstance(logs, list) else []:
                try:
                    if (log.get("address") or "").lower() != nft_addr:
                        continue
                    topics = log.get("topics") or []
                    if not topics:
                        continue

                    topic0 = HexBytes(topics[0])

                    # Prefer the protocol-specific event if present.
                    if topic0 == _WITHDRAW_REQUEST_CREATED_TOPIC0:
                        evt = get_event_data(
                            web3.codec,
                            _WITHDRAW_REQUEST_CREATED_EVENT_ABI,
                            log,
                        )
                        rid = (evt.get("args") or {}).get("requestId")
                        if rid is None:
                            continue
                        if expected_owner_l:
                            recipient = (evt.get("args") or {}).get("recipient")
                            if (
                                not recipient
                                or to_checksum_address(recipient).lower()
                                != expected_owner_l
                            ):
                                continue
                        return int(rid)

                    # Fallback: parse ERC721 mint (Transfer from ZERO_ADDRESS).
                    if topic0 == _ERC721_TRANSFER_TOPIC0 and transfer_mint_id is None:
                        evt = get_event_data(
                            web3.codec,
                            ERC721_TRANSFER_EVENT_ABI,
                            log,
                        )
                        args = evt.get("args") or {}
                        from_addr = args.get("from")
                        to_addr = args.get("to")
                        token_id = args.get("tokenId")
                        if not from_addr or not to_addr or token_id is None:
                            continue
                        if (
                            to_checksum_address(from_addr).lower()
                            != ZERO_ADDRESS.lower()
                        ):
                            continue
                        if expected_owner_l and to_checksum_address(
                            to_addr
                        ).lower() != (expected_owner_l):
                            continue
                        transfer_mint_id = int(token_id)
                except Exception:
                    continue

            if transfer_mint_id is not None:
                return transfer_mint_id

        return None

    async def _is_paused(self, *, chain_id: int) -> bool:
        entry = self._entry(chain_id)
        async with web3_from_chain_id(chain_id) as web3:
            lp = web3.eth.contract(
                address=entry["liquidity_pool"], abi=ETHERFI_LIQUIDITY_POOL_ABI
            )
            return await lp.functions.paused().call(block_identifier="pending")

    @require_wallet
    async def stake_eth(
        self,
        *,
        amount_wei: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
        referral: str | None = None,
        check_paused: bool = True,
    ) -> tuple[bool, Any]:
        """Stake ETH via ether.fi LiquidityPool.deposit() payable (returns shares)."""
        if amount_wei <= 0:
            return False, "amount_wei must be positive"

        try:
            entry = self._entry(chain_id)

            if check_paused and await self._is_paused(chain_id=chain_id):
                return False, "ether.fi LiquidityPool is paused"

            if referral:
                ref = to_checksum_address(referral)
                fn_name = "deposit(address)"
                args: list[Any] = [ref]
            else:
                fn_name = "deposit()"
                args = []

            tx = await encode_call(
                target=entry["liquidity_pool"],
                abi=ETHERFI_LIQUIDITY_POOL_ABI,
                fn_name=fn_name,
                args=args,
                from_address=self.wallet_address,
                chain_id=chain_id,
                value=amount_wei,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def wrap_eeth(
        self,
        *,
        amount_eeth: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
        approval_amount: int = MAX_UINT256,
    ) -> tuple[bool, Any]:
        """Wrap eETH -> weETH (requires eETH allowance to weETH contract)."""
        if amount_eeth <= 0:
            return False, "amount_eeth must be positive"

        try:
            entry = self._entry(chain_id)

            approved = await ensure_allowance(
                token_address=entry["eeth"],
                owner=self.wallet_address,
                spender=entry["weeth"],
                amount=amount_eeth,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=approval_amount,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=entry["weeth"],
                abi=ETHERFI_WEETH_ABI,
                fn_name="wrap",
                args=[amount_eeth],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def wrap_eeth_with_permit(
        self,
        *,
        amount_eeth: int,
        permit: Any,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        """Wrap eETH -> weETH using WeETH.wrapWithPermit (single tx, no approval)."""
        if amount_eeth <= 0:
            return False, "amount_eeth must be positive"

        try:
            entry = self._entry(chain_id)
            permit_tuple = _normalize_permit_tuple(permit)

            tx = await encode_call(
                target=entry["weeth"],
                abi=ETHERFI_WEETH_ABI,
                fn_name="wrapWithPermit",
                args=[amount_eeth, permit_tuple],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def unwrap_weeth(
        self,
        *,
        amount_weeth: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        """Unwrap weETH -> eETH (burns weETH, transfers eETH to caller)."""
        if amount_weeth <= 0:
            return False, "amount_weeth must be positive"

        try:
            entry = self._entry(chain_id)
            tx = await encode_call(
                target=entry["weeth"],
                abi=ETHERFI_WEETH_ABI,
                fn_name="unwrap",
                args=[amount_weeth],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def request_withdraw(
        self,
        *,
        amount_eeth: int,
        recipient: str | None = None,
        chain_id: int = CHAIN_ID_ETHEREUM,
        approval_amount: int = MAX_UINT256,
        include_request_id: bool = True,
    ) -> tuple[bool, Any]:
        """
        Request an async withdrawal: transfers eETH into WithdrawRequestNFT escrow and mints an NFT.

        `recipient` receives the WithdrawRequest NFT and will be the only address able to claim later.
        """
        if amount_eeth <= 0:
            return False, "amount_eeth must be positive"

        try:
            entry = self._entry(chain_id)
            rcpt = to_checksum_address(recipient) if recipient else self.wallet_address

            approved = await ensure_allowance(
                token_address=entry["eeth"],
                owner=self.wallet_address,
                spender=entry["liquidity_pool"],
                amount=amount_eeth,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=approval_amount,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=entry["liquidity_pool"],
                abi=ETHERFI_LIQUIDITY_POOL_ABI,
                fn_name="requestWithdraw",
                args=[rcpt, amount_eeth],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)

            request_id: int | None = None
            if include_request_id:
                try:
                    request_id = await self._parse_request_id_from_receipt(
                        chain_id=chain_id,
                        tx_hash=tx_hash,
                        expected_owner=rcpt,
                    )
                except Exception:
                    request_id = None

            return True, {
                "tx": tx_hash,
                "recipient": rcpt,
                "amount_eeth": amount_eeth,
                "request_id": request_id,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def request_withdraw_with_permit(
        self,
        *,
        owner: str | None = None,
        amount_eeth: int,
        permit: Any,
        chain_id: int = CHAIN_ID_ETHEREUM,
        include_request_id: bool = True,
    ) -> tuple[bool, Any]:
        """
        Request an async withdrawal using LiquidityPool.requestWithdrawWithPermit (single tx).

        Notes:
        - The on-chain signature is `requestWithdrawWithPermit(_owner, _amount, _permit)`.
        - The WithdrawRequest NFT is expected to be minted to `_owner` (no separate recipient param).
        """
        if amount_eeth <= 0:
            return False, "amount_eeth must be positive"

        try:
            entry = self._entry(chain_id)
            owner_addr = to_checksum_address(owner) if owner else self.wallet_address
            permit_tuple = _normalize_permit_tuple(permit)

            tx = await encode_call(
                target=entry["liquidity_pool"],
                abi=ETHERFI_LIQUIDITY_POOL_ABI,
                fn_name="requestWithdrawWithPermit",
                args=[owner_addr, amount_eeth, permit_tuple],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)

            request_id: int | None = None
            if include_request_id:
                try:
                    request_id = await self._parse_request_id_from_receipt(
                        chain_id=chain_id,
                        tx_hash=tx_hash,
                        expected_owner=owner_addr,
                    )
                except Exception:
                    request_id = None

            return True, {
                "tx": tx_hash,
                "owner": owner_addr,
                "amount_eeth": amount_eeth,
                "request_id": request_id,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_withdraw(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
    ) -> tuple[bool, Any]:
        """Claim a finalized withdrawal: burns WithdrawRequest NFT and receives ETH."""
        if token_id < 0:
            return False, "token_id must be non-negative"

        try:
            entry = self._entry(chain_id)
            tx = await encode_call(
                target=entry["withdraw_request_nft"],
                abi=ETHERFI_WITHDRAW_REQUEST_NFT_ABI,
                fn_name="claimWithdraw",
                args=[token_id],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_claimable_withdraw(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
        block_identifier: int | str = "pending",
    ) -> tuple[bool, int | str]:
        """Return the currently claimable ETH amount for a withdraw request tokenId."""
        try:
            entry = self._entry(chain_id)
            async with web3_from_chain_id(chain_id) as web3:
                nft = web3.eth.contract(
                    address=entry["withdraw_request_nft"],
                    abi=ETHERFI_WITHDRAW_REQUEST_NFT_ABI,
                )
                finalized = await nft.functions.isFinalized(token_id).call(
                    block_identifier=block_identifier
                )
                if not finalized:
                    return True, 0
                amt = await nft.functions.getClaimableAmount(token_id).call(
                    block_identifier=block_identifier
                )
                return True, int(amt or 0)
        except Exception as exc:
            return False, str(exc)

    async def is_withdraw_finalized(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_ETHEREUM,
        block_identifier: int | str = "pending",
    ) -> tuple[bool, bool | str]:
        """Return whether a withdraw request (tokenId) is finalized."""
        try:
            entry = self._entry(chain_id)
            async with web3_from_chain_id(chain_id) as web3:
                nft = web3.eth.contract(
                    address=entry["withdraw_request_nft"],
                    abi=ETHERFI_WITHDRAW_REQUEST_NFT_ABI,
                )
                finalized = await nft.functions.isFinalized(token_id).call(
                    block_identifier=block_identifier
                )
                return True, finalized
        except Exception as exc:
            return False, str(exc)

    async def get_pos(
        self,
        *,
        account: str | None = None,
        chain_id: int = CHAIN_ID_ETHEREUM,
        block_identifier: int | str = "pending",
        include_shares: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "account (or wallet_address) is required"

        try:
            entry = self._entry(chain_id)

            async with web3_from_chain_id(chain_id) as web3:
                eeth = web3.eth.contract(address=entry["eeth"], abi=ETHERFI_EETH_ABI)
                weeth = web3.eth.contract(address=entry["weeth"], abi=ETHERFI_WEETH_ABI)
                lp = web3.eth.contract(
                    address=entry["liquidity_pool"], abi=ETHERFI_LIQUIDITY_POOL_ABI
                )

                calls = [
                    Call(eeth, "balanceOf", args=(acct,), postprocess=int),
                    Call(weeth, "balanceOf", args=(acct,), postprocess=int),
                    Call(weeth, "getRate", postprocess=int),
                ]
                if include_shares:
                    calls.append(Call(eeth, "shares", args=(acct,), postprocess=int))
                calls.append(Call(lp, "getTotalPooledEther", postprocess=int))

                res = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=calls,
                    block_identifier=block_identifier,
                )

                eeth_balance = res[0]
                weeth_balance = res[1]
                weeth_rate = res[2]

                idx = 3
                eeth_shares = res[idx] if include_shares else None
                idx += 1 if include_shares else 0
                total_pooled = res[idx]

                weeth_eeth_equiv = 0
                if weeth_balance > 0:
                    weeth_eeth_equiv = (
                        await weeth.functions.getEETHByWeETH(weeth_balance).call(
                            block_identifier=block_identifier
                        )
                        or 0
                    )

                return True, {
                    "protocol": "etherfi",
                    "chain_id": chain_id,
                    "account": acct,
                    "contracts": {
                        "liquidity_pool": entry["liquidity_pool"],
                        "eeth": entry["eeth"],
                        "weeth": entry["weeth"],
                        "withdraw_request_nft": entry["withdraw_request_nft"],
                    },
                    "eeth": (
                        {
                            "balance_raw": eeth_balance,
                            "shares_raw": eeth_shares,
                        }
                        if eeth_shares is not None
                        else {"balance_raw": eeth_balance}
                    ),
                    "weeth": {
                        "balance_raw": weeth_balance,
                        "eeth_equivalent_raw": weeth_eeth_equiv,
                        "rate": weeth_rate,
                    },
                    "liquidity_pool": {
                        "total_pooled_ether": total_pooled,
                    },
                }
        except Exception as exc:
            return False, str(exc)
