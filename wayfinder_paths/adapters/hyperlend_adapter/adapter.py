from __future__ import annotations

from typing import Any, Literal

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.adapters.models import LEND, UNLEND
from wayfinder_paths.core.clients.HyperlendClient import (
    HYPERLEND_CLIENT,
    AssetsView,
    LendRateHistory,
    MarketEntry,
    StableMarketsHeadroomResponse,
)
from wayfinder_paths.core.constants.contracts import (
    HYPEREVM_WHYPE,
    HYPERLEND_POOL,
    HYPERLEND_WRAPPED_TOKEN_GATEWAY,
)
from wayfinder_paths.core.constants.hyperlend_abi import (
    POOL_ABI,
    WRAPPED_TOKEN_GATEWAY_ABI,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


class HyperlendAdapter(BaseAdapter):
    adapter_type = "HYPERLEND"

    def __init__(
        self,
        config: dict[str, Any],
        strategy_wallet_signing_callback=None,
    ) -> None:
        super().__init__("hyperlend_adapter", config)
        config = config or {}

        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

        self.ledger_adapter = LedgerAdapter()
        self.token_adapter = TokenAdapter()
        strategy_wallet = config.get("strategy_wallet") or {}
        strategy_addr = strategy_wallet.get("address")

        self.strategy_wallet_address = to_checksum_address(strategy_addr)

    async def get_stable_markets(
        self,
        *,
        required_underlying_tokens: float | None = None,
        buffer_bps: int | None = None,
        min_buffer_tokens: float | None = None,
    ) -> tuple[bool, StableMarketsHeadroomResponse | str]:
        try:
            data = await HYPERLEND_CLIENT.get_stable_markets(
                required_underlying_tokens=required_underlying_tokens,
                buffer_bps=buffer_bps,
                min_buffer_tokens=min_buffer_tokens,
            )
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_assets_view(
        self,
        *,
        user_address: str,
    ) -> tuple[bool, AssetsView | str]:
        try:
            data = await HYPERLEND_CLIENT.get_assets_view(user_address=user_address)
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, view = await self.get_assets_view(user_address=account)
        if not ok:
            return False, str(view)

        assets = view.get("assets", []) if isinstance(view, dict) else []
        if include_zero_positions:
            positions = assets
        else:
            positions = [
                a
                for a in assets
                if float(a.get("supply", 0) or 0) > 0
                or float(a.get("variable_borrow", 0) or 0) > 0
            ]

        return (
            True,
            {
                "protocol": "hyperlend",
                "account": account,
                "positions": positions,
                "accountData": view.get("account_data")
                if isinstance(view, dict)
                else {},
                "assetsView": view,
            },
        )

    async def get_market_entry(
        self,
        *,
        token: str,
    ) -> tuple[bool, MarketEntry | str]:
        try:
            data = await HYPERLEND_CLIENT.get_market_entry(token=token)
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def get_lend_rate_history(
        self,
        *,
        token: str,
        lookback_hours: int,
        force_refresh: bool | None = None,
    ) -> tuple[bool, LendRateHistory | str]:
        try:
            data = await HYPERLEND_CLIENT.get_lend_rate_history(
                token=token,
                lookback_hours=lookback_hours,
                force_refresh=force_refresh,
            )
            return True, data
        except Exception as exc:
            return False, str(exc)

    async def lend(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
        strategy_name: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"
        chain_id = int(chain_id)

        if native:
            token_addr = HYPEREVM_WHYPE
            transaction = await self._encode_call(
                target=HYPERLEND_WRAPPED_TOKEN_GATEWAY,
                abi=WRAPPED_TOKEN_GATEWAY_ABI,
                fn_name="depositETH",
                args=[HYPERLEND_POOL, strategy, 0],
                from_address=strategy,
                chain_id=chain_id,
                value=qty,
            )
        else:
            token_addr = to_checksum_address(underlying_token)
            approved = await ensure_allowance(
                token_address=token_addr,
                owner=strategy,
                spender=HYPERLEND_POOL,
                amount=qty,
                chain_id=chain_id,
                signing_callback=self.strategy_wallet_signing_callback,
            )
            if not approved[0]:
                return approved
            transaction = await self._encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="supply",
                args=[token_addr, qty, strategy, 0],
                from_address=strategy,
                chain_id=chain_id,
            )

        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )

        await self._record_pool_op(
            token_address=token_addr,
            amount=qty,
            chain_id=chain_id,
            wallet_address=strategy,
            txn_hash=txn_hash,
            strategy_name=strategy_name,
            op_type="lend",
        )

        return (True, txn_hash)

    async def unlend(
        self,
        *,
        underlying_token: str,
        qty: int,
        chain_id: int,
        native: bool = False,
        strategy_name: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"
        chain_id = int(chain_id)

        if native:
            token_addr = HYPEREVM_WHYPE
            transaction = await self._encode_call(
                target=HYPERLEND_WRAPPED_TOKEN_GATEWAY,
                abi=WRAPPED_TOKEN_GATEWAY_ABI,
                fn_name="withdrawETH",
                args=[HYPERLEND_POOL, qty, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )
        else:
            token_addr = to_checksum_address(underlying_token)
            transaction = await self._encode_call(
                target=HYPERLEND_POOL,
                abi=POOL_ABI,
                fn_name="withdraw",
                args=[token_addr, qty, strategy],
                from_address=strategy,
                chain_id=chain_id,
            )

        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        await self._record_pool_op(
            token_address=token_addr,
            amount=qty,
            chain_id=chain_id,
            wallet_address=strategy,
            txn_hash=txn_hash,
            strategy_name=strategy_name,
            op_type="unlend",
        )

        return True, txn_hash

    async def _encode_call(
        self,
        *,
        target: str,
        abi: list[dict[str, Any]],
        fn_name: str,
        args: list[Any],
        from_address: str,
        chain_id: int,
        value: int = 0,
    ) -> dict[str, Any]:
        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(address=target, abi=abi)
            try:
                data = (
                    await getattr(contract.functions, fn_name)(*args).build_transaction(
                        {"from": from_address}
                    )
                )["data"]
            except ValueError as exc:
                raise ValueError(f"Failed to encode {fn_name}: {exc}") from exc

            transaction: dict[str, Any] = {
                "chainId": int(chain_id),
                "from": to_checksum_address(from_address),
                "to": to_checksum_address(target),
                "data": data,
                "value": int(value),
            }
            return transaction

    async def _record_pool_op(
        self,
        token_address: str,
        amount: int,
        chain_id: int,
        wallet_address: str,
        txn_hash: str,
        op_type: Literal["lend", "unlend"],
        strategy_name: str | None = None,
    ):
        amount_usd = await self._calculate_amount_usd(
            token_address=token_address,
            amount=amount,
            chain_id=chain_id,
        )

        model = {"lend": LEND, "unlend": UNLEND}[op_type]

        operation_data = model(
            adapter=self.adapter_type,
            token_address=token_address,
            pool_address=HYPERLEND_POOL,
            amount=str(amount),
            amount_usd=amount_usd or 0,
            transaction_hash=txn_hash,
            transaction_chain_id=chain_id,
        )

        success, ledger_response = await self.ledger_adapter.record_operation(
            wallet_address=wallet_address,
            operation_data=operation_data,
            usd_value=amount_usd or 0,
            strategy_name=strategy_name,
        )
        if not success:
            self.logger.warning("Ledger record failed", error=ledger_response)

    async def _calculate_amount_usd(
        self,
        token_address: str,
        amount: int,
        chain_id: int,
    ) -> float | None:
        success, token_data = await self.token_adapter.get_token(
            query=token_address,
            chain_id=chain_id,
        )
        if not success or not token_data:
            self.logger.warning(
                f"Could not get token info for {token_address} on chain {chain_id}"
            )
            return None

        decimals, current_price = (
            token_data["decimals"],
            token_data["current_price"],
        )

        if decimals is None or current_price is None:
            self.logger.warning(
                f"Could not get decimal or current_price info for {token_address} on chain {chain_id}"
            )
            return None

        return current_price * float(amount) / 10 ** int(decimals)
