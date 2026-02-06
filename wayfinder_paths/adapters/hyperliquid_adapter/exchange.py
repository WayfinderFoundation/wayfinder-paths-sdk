from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal, cast

from eth_account.messages import encode_typed_data
from hyperliquid.api import API
from hyperliquid.exchange import get_timestamp_ms
from hyperliquid.info import Info
from hyperliquid.utils.signing import (
    BUILDER_FEE_SIGN_TYPES,
    SPOT_TRANSFER_SIGN_TYPES,
    USD_CLASS_TRANSFER_SIGN_TYPES,
    USER_DEX_ABSTRACTION_SIGN_TYPES,
    WITHDRAW_SIGN_TYPES,
    OrderType,
    OrderWire,
    float_to_wire,
    get_l1_action_payload,
    order_type_to_wire,
    order_wires_to_order_action,
    user_signed_payload,
)
from hyperliquid.utils.types import BuilderInfo
from loguru import logger
from web3 import Web3

from wayfinder_paths.adapters.hyperliquid_adapter.util import (
    get_price_decimals_for_hypecore_asset,
    sig_hex_to_hl_signature,
)

ARBITRUM_CHAIN_ID = "0xa4b1"
MAINNET = "Mainnet"
USER_DECLINED_ERROR = {
    "status": "err",
    "error": "User declined transaction. Please try again..",
}


class Exchange:
    def __init__(
        self,
        info: Info,
        util: Util,
        sign_callback: Callable[..., Awaitable[str]],
        signing_type: Literal["eip712", "local"],
    ):
        self.info = info
        self.api = API()
        self.sign_callback = sign_callback
        self.signing_type = signing_type

    def _create_hypecore_order_actions(
        self,
        asset_id: int,
        is_buy: bool,
        price: float,
        size: float,
        reduce_only: bool,
        order_type: OrderType,
        builder: BuilderInfo | None = None,
        cloid: str | None = None,
    ):
        order: OrderWire = {
            "a": asset_id,
            "b": is_buy,
            "p": float_to_wire(price),
            "s": float_to_wire(size),
            "r": reduce_only,
            "t": order_type_to_wire(order_type),
        }
        if cloid is not None:
            order["c"] = cloid
        return order_wires_to_order_action([order], builder)

    async def place_market_order(
        self,
        asset_id: int,
        is_buy: bool,
        slippage: float,
        size: float,
        address: str,
        reduce_only: bool = False,
        builder: BuilderInfo | None = None,
        cloid: str | None = None,
    ):
        asset_name = self.info.asset_to_coin[asset_id]
        mids = self.info.all_mids()
        midprice = float(mids[asset_name])

        if slippage >= 1 or slippage < 0:
            return {"error": f"slippage must be in [0, 1), got {slippage}"}

        price = midprice * ((1 + slippage) if is_buy else (1 - slippage))
        price = round(
            float(f"{price:.5g}"),
            get_price_decimals_for_hypecore_asset(self.info, asset_id),
        )
        order_actions = self._create_hypecore_order_actions(
            asset_id,
            is_buy,
            price,
            size,
            reduce_only,
            {"limit": {"tif": "Ioc"}},
            builder,
            cloid,
        )
        return await self.sign_and_broadcast_hypecore(order_actions, address)

    async def place_limit_order(
        self,
        asset_id: int,
        is_buy: bool,
        price: float,
        size: float,
        address: str,
        builder: BuilderInfo | None = None,
        cloid: str | None = None,
    ):
        order_actions = self._create_hypecore_order_actions(
            asset_id,
            is_buy,
            price,
            size,
            False,
            {"limit": {"tif": "Gtc"}},
            builder,
            cloid,
        )
        return await self.sign_and_broadcast_hypecore(order_actions, address)

    async def place_trigger_order(
        self,
        asset_id: int,
        is_buy: bool,
        trigger_price: float,
        size: float,
        address: str,
        tpsl: Literal["tp", "sl"],
        is_market: bool = True,
        limit_price: float | None = None,
        builder: BuilderInfo | None = None,
    ):
        order_type = {
            "trigger": {"triggerPx": trigger_price, "isMarket": is_market, "tpsl": tpsl}
        }
        price = trigger_price if is_market else (limit_price or trigger_price)
        order_actions = self._create_hypecore_order_actions(
            asset_id, is_buy, price, size, True, order_type, builder
        )
        return await self.sign_and_broadcast_hypecore(order_actions, address)

    async def cancel_order(self, asset_id: int, order_id: int, address: str):
        order_actions = {
            "type": "cancel",
            "cancels": [
                {
                    "a": asset_id,
                    "o": int(order_id),
                }
            ],
        }
        return await self.sign_and_broadcast_hypecore(order_actions, address)

    async def update_leverage(
        self, asset: int, leverage: int, is_cross: bool, address: str
    ):
        order_actions = {
            "type": "updateLeverage",
            "asset": asset,
            "isCross": is_cross,
            "leverage": leverage,
        }
        return await self.sign_and_broadcast_hypecore(order_actions, address)

    async def update_isolated_margin(self, asset: int, delta_usdc: float, address: str):
        """
        Add/remove USDC margin on an existing ISOLATED position.
        Works for both longs & shorts. Positive = add, negative = remove.
        """
        ntli = int(round(delta_usdc * 1_000_000))
        order_actions = {
            "type": "updateIsolatedMargin",
            "asset": asset,
            "isBuy": delta_usdc >= 0,
            "ntli": ntli,
        }
        return await self.sign_and_broadcast_hypecore(order_actions, address)

    async def withdraw(self, amount: float, address: str):
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "destination": address,
            "amount": str(amount),
            "time": nonce,
            "type": "withdraw3",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:Withdraw", WITHDRAW_SIGN_TYPES, action
        )
        if not (sig := await self.sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    async def spot_transfer(
        self,
        signature_chain_id: int,
        destination: str,
        token: str,
        amount: str,
        address: str,
    ):
        nonce = get_timestamp_ms()
        action = {
            "type": "spotSend",
            "hyperliquidChain": MAINNET,
            "signatureChainId": hex(signature_chain_id),
            "destination": destination,
            "token": token,
            "amount": amount,
            "time": nonce,
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:SpotSend", SPOT_TRANSFER_SIGN_TYPES, action
        )
        if not (sig := await self.sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    async def usd_class_transfer(self, amount: float, address: str, to_perp: bool):
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "amount": str(amount),
            "toPerp": to_perp,
            "nonce": nonce,
            "type": "usdClassTransfer",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:UsdClassTransfer",
            USD_CLASS_TRANSFER_SIGN_TYPES,
            action,
        )
        if not (sig := await self.sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    async def set_dex_abstraction(self, address: str, enabled: bool):
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "user": address.lower(),
            "enabled": enabled,
            "nonce": nonce,
            "type": "userDexAbstraction",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:UserDexAbstraction",
            USER_DEX_ABSTRACTION_SIGN_TYPES,
            action,
        )
        if not (sig := await self.sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    async def approve_builder_fee(self, builder: str, max_fee_rate: str, address: str):
        nonce = get_timestamp_ms()
        action = {
            "hyperliquidChain": MAINNET,
            "signatureChainId": ARBITRUM_CHAIN_ID,
            "maxFeeRate": max_fee_rate,
            "builder": builder,
            "nonce": nonce,
            "type": "approveBuilderFee",
        }
        payload = user_signed_payload(
            "HyperliquidTransaction:ApproveBuilderFee", BUILDER_FEE_SIGN_TYPES, action
        )
        if not (sig := await self.sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    async def sign(
        self, payload: str, action: dict, address: str
    ) -> dict[str, Any] | None:
        if self.signing_type == "eip712":
            sig_hex = await self.sign_callback(payload)
            if not sig_hex:
                return None
            return sig_hex_to_hl_signature(sig_hex)

        payload = encode_typed_data(full_message=payload)
        result = await self.sign_callback(action, payload, address)
        return cast(dict[str, Any] | None, result)

    def _broadcast_hypecore(self, action, nonce, signature):
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
        }
        logger.info(f"Broadcasting Hypecore payload: {payload}")
        return self.api.post("/exchange", payload)

    async def sign_and_broadcast_hypecore(self, action, address):
        nonce = get_timestamp_ms()
        payload = payload = get_l1_action_payload(action, None, nonce, None, True)
        if not (sig := await self.sign(payload, action, address)):
            return USER_DECLINED_ERROR
        return self._broadcast_hypecore(action, nonce, sig)

    def _hypecore_get_user_transfers(
        self,
        user_address: str,
        from_timestamp_ms: int,
        type: Literal["deposit", "withdraw"],
    ) -> dict[str, Decimal]:
        data = self.api.post(
            "/info",
            {
                "type": "userNonFundingLedgerUpdates",
                "user": Web3.to_checksum_address(user_address),
                "startTime": int(from_timestamp_ms),
            },
        )
        res = {}
        for u in sorted(data, key=lambda x: x.get("time", 0)):
            delta = u.get("delta")
            if delta and delta.get("type") == type:
                res[u["hash"]] = Decimal(str(delta["usdc"]))
        return res

    def hypecore_get_user_deposits(
        self, user_address: str, from_timestamp_ms: int
    ) -> dict[str, Decimal]:
        return self._hypecore_get_user_transfers(
            user_address=user_address,
            from_timestamp_ms=from_timestamp_ms,
            type="deposit",
        )

    def hypecore_get_user_withdrawals(
        self, user_address: str, from_timestamp_ms: int
    ) -> dict[str, Decimal]:
        return self._hypecore_get_user_transfers(
            user_address=user_address,
            from_timestamp_ms=from_timestamp_ms,
            type="withdraw",
        )
