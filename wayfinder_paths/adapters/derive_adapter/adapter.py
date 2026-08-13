from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from eth_abi import encode as abi_encode
from eth_account.messages import defunct_hash_message
from web3 import Web3

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter

DERIVE_MAINNET_API_BASE_URL = "https://api.lyra.finance"
DERIVE_TESTNET_API_BASE_URL = "https://api-demo.lyra.finance"
DERIVE_MAINNET_WS_URL = "wss://api.lyra.finance/ws"
DERIVE_TESTNET_WS_URL = "wss://api-demo.lyra.finance/ws"

InstrumentType = Literal["option", "perp", "erc20"]
MarginType = Literal["PM", "SM", "PM2"]

DERIVE_ORDER_REQUIRED_FIELDS = frozenset(
    {
        "amount",
        "direction",
        "instrument_name",
        "limit_price",
        "max_fee",
        "nonce",
        "signature",
        "signature_expiry_sec",
        "signer",
        "subaccount_id",
    }
)
DERIVE_ORDER_UNSIGNED_FIELDS = frozenset(
    {
        "amount",
        "direction",
        "instrument_name",
        "limit_price",
        "max_fee",
        "subaccount_id",
    }
)

DERIVE_ACTION_DETAIL_FIELDS = frozenset(
    {"nonce", "signature", "signature_expiry_sec", "signer"}
)
DERIVE_ORDERBOOK_GROUPS = frozenset({"1", "10", "100"})
DERIVE_ORDERBOOK_DEPTHS = frozenset({"1", "10", "20", "100"})
DERIVE_ACTION_SIGNATURE_EXPIRY_BUFFER_SEC = 600
DERIVE_DEFAULT_TRANSFER_MANAGER = "0x0000000000000000000000000000000000000000"
DERIVE_DEFAULT_ASSET_DECIMALS = {
    "USDC": 6,
    "USDC.E": 6,
    "ETH": 18,
    "WETH": 18,
    "BTC": 8,
    "WBTC": 8,
}

DERIVE_PROTOCOL_CONSTANTS: dict[str, dict[str, Any]] = {
    "mainnet": {
        "domain_separator": (
            "0xd96e5f90797da7ec8dc4e276260c7f3f87fedf68775fbe1ef116e996fc60441b"
        ),
        "action_typehash": (
            "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
        ),
        "deposit_module_address": "0x9B3FE5E5a3bcEa5df4E08c41Ce89C4e3Ff01Ace3",
        "trade_module_address": "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b",
        "withdrawal_module_address": "0x9d0E8f5b25384C7310CB8C6aE32C8fbeb645d083",
        "transfer_module_address": "0x01259207A40925b794C8ac320456F7F6c8FE2636",
        "cash_asset_address": "0x57B03E14d409ADC7fAb6CFc44b5886CAD2D5f02b",
        "standard_risk_manager_address": ("0x28c9ddF9A3B29c2E6a561c1BC520954e5A33de5D"),
        "portfolio_risk_manager_addresses": {
            "ETH": "0xe7cD9370CdE6C9b5eAbCe8f86d01822d3de205A0",
            "BTC": "0x45DA02B9cCF384d7DbDD7b2b13e705BADB43Db0D",
        },
        "portfolio_risk_manager_v2_addresses": {
            "ETH": "0xc755DAe3fd295A687adf3e192387163f813F0598",
            "BTC": "0xC7adAB7A2b92019098dA55Ba4C5A8C65Ae7e52DC",
        },
        "chain_id": 957,
    },
    "testnet": {
        "domain_separator": (
            "0x9bcf4dc06df5d8bf23af818d5716491b995020f377d3b7b64c29ed14e3dd1105"
        ),
        "action_typehash": (
            "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
        ),
        "deposit_module_address": "0x43223Db33AdA0575D2E100829543f8B04A37a1ec",
        "trade_module_address": "0x87F2863866D85E3192a35A73b388BD625D83f2be",
        "withdrawal_module_address": "0xe850641C5207dc5E9423fB15f89ae6031A05fd92",
        "transfer_module_address": "0x0CFC1a4a90741aB242cAfaCD798b409E12e68926",
        "cash_asset_address": "0x6caf294DaC985ff653d5aE75b4FF8E0A66025928",
        "standard_risk_manager_address": ("0x28bE681F7bEa6f465cbcA1D25A2125fe7533391C"),
        "portfolio_risk_manager_addresses": {
            "ETH": "0xDF448056d7bf3f9Ca13d713114e17f1B7470DeBF",
            "BTC": "0xbaC0328cd4Af53d52F9266Cdbd5bf46720320A20",
        },
        "portfolio_risk_manager_v2_addresses": {},
        "chain_id": 901,
    },
}


class DeriveAdapter(BaseAdapter):
    adapter_type = "DERIVE"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable[[dict[str, Any]], Awaitable[bytes]] | None = None,
        sign_hash_callback: Callable[[str], Awaitable[str]] | None = None,
        sign_message_callback: Callable[[str], Awaitable[str]] | None = None,
        wallet_address: str | None = None,
        derive_wallet_address: str | None = None,
        api_base_url: str | None = None,
        ws_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        http_timeout_s: float = 30.0,
    ) -> None:
        super().__init__("derive_adapter", config)

        derive_config = self.config.get("derive") or {}
        if not isinstance(derive_config, dict):
            derive_config = {}

        network = str(derive_config.get("network", "mainnet")).lower()
        use_testnet = network in {"testnet", "demo", "api-demo"}
        self.network = "testnet" if use_testnet else "mainnet"

        self.api_base_url = (
            api_base_url
            or derive_config.get("api_base_url")
            or (
                DERIVE_TESTNET_API_BASE_URL
                if use_testnet
                else DERIVE_MAINNET_API_BASE_URL
            )
        )
        self.ws_url = (
            ws_url
            or derive_config.get("ws_url")
            or (DERIVE_TESTNET_WS_URL if use_testnet else DERIVE_MAINNET_WS_URL)
        )
        self.wallet_address = wallet_address or derive_config.get("wallet_address")
        self.derive_wallet_address = (
            derive_wallet_address
            or derive_config.get("derive_wallet_address")
            or self.wallet_address
        )
        self.sign_callback = sign_callback
        self.sign_hash_callback = sign_hash_callback
        self.sign_message_callback = sign_message_callback
        self._http = http_client or httpx.AsyncClient(
            base_url=self.api_base_url,
            timeout=httpx.Timeout(http_timeout_s),
        )
        self.protocol_constants = self._protocol_constants(derive_config)

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        private: bool = False,
        auth_wallet: str | None = None,
    ) -> tuple[bool, Any]:
        headers: dict[str, str] | None = None
        if private:
            ok, auth_headers = await self._auth_headers(wallet=auth_wallet)
            if not ok:
                return False, auth_headers
            headers = auth_headers

        try:
            response = await self._http.post(path, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        if not isinstance(data, dict):
            return False, f"Unexpected Derive response: {type(data).__name__}"
        if data.get("error") is not None:
            return False, data["error"]
        if "result" not in data:
            return False, f"Derive response missing result: {data}"
        return True, data["result"]

    async def _auth_headers(
        self, *, wallet: str | None = None
    ) -> tuple[bool, dict[str, str] | str]:
        target_wallet = wallet or self.derive_wallet_address
        if not target_wallet:
            return (
                False,
                "wallet or derive_wallet_address is required for Derive private endpoints",
            )

        timestamp = str(int(time.time() * 1000))
        if self.sign_message_callback is not None:
            signature = await self.sign_message_callback(timestamp)
        elif self.sign_hash_callback is not None:
            digest = defunct_hash_message(text=timestamp)
            signature = await self.sign_hash_callback(f"0x{bytes(digest).hex()}")
        else:
            return (
                False,
                "sign_message_callback or sign_hash_callback is required for Derive private endpoints",
            )

        return (
            True,
            {
                "X-LyraWallet": str(target_wallet),
                "X-LyraTimestamp": timestamp,
                "X-LyraSignature": str(signature),
            },
        )

    async def get_time(self) -> tuple[bool, int | str]:
        ok, result = await self._post("/public/get_time", {})
        if not ok:
            return False, result
        if not isinstance(result, int):
            return False, f"Unexpected get_time result: {result}"
        return True, result

    async def get_instruments(
        self,
        *,
        currency: str,
        instrument_type: InstrumentType = "option",
        expired: bool = False,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, result = await self._post(
            "/public/get_instruments",
            {
                "currency": currency.upper(),
                "instrument_type": instrument_type,
                "expired": expired,
            },
        )
        if not ok:
            return False, result
        if not isinstance(result, list):
            return False, f"Unexpected get_instruments result: {type(result).__name__}"
        return True, result

    async def list_options(
        self, *, currency: str, expired: bool = False
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        return await self.get_instruments(
            currency=currency,
            instrument_type="option",
            expired=expired,
        )

    async def list_option_expiries(
        self, *, currency: str, expired: bool = False
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, instruments = await self.list_options(currency=currency, expired=expired)
        if not ok:
            return False, instruments

        expiries: dict[int, int] = {}
        for instrument in instruments:
            details = instrument.get("option_details") or {}
            expiry = details.get("expiry")
            if isinstance(expiry, int):
                expiries[expiry] = expiries.get(expiry, 0) + 1

        return True, [
            {
                "expiry": expiry,
                "expiry_date": self.expiry_date(expiry),
                "instrument_count": count,
            }
            for expiry, count in sorted(expiries.items())
        ]

    async def get_tickers(
        self,
        *,
        instrument_type: InstrumentType,
        currency: str | None = None,
        expiry_date: str | int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        if instrument_type == "option" and (currency is None or expiry_date is None):
            return False, "currency and expiry_date are required for option tickers"

        payload: dict[str, Any] = {"instrument_type": instrument_type}
        if currency is not None:
            payload["currency"] = currency.upper()
        if expiry_date is not None:
            payload["expiry_date"] = str(expiry_date)

        ok, result = await self._post("/public/get_tickers", payload)
        if not ok:
            return False, result
        if not isinstance(result, dict):
            return False, f"Unexpected get_tickers result: {type(result).__name__}"
        tickers = result.get("tickers")
        if not isinstance(tickers, dict):
            return False, f"Unexpected get_tickers tickers: {type(tickers).__name__}"
        return True, tickers

    async def get_option_tickers(
        self,
        *,
        currency: str,
        expiry_date: str | int,
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self.get_tickers(
            instrument_type="option",
            currency=currency,
            expiry_date=expiry_date,
        )

    async def get_ticker(
        self, *, instrument_name: str
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, result = await self._post(
            "/public/get_ticker",
            {"instrument_name": instrument_name},
        )
        if not ok:
            return False, result
        if not isinstance(result, dict):
            return False, f"Unexpected get_ticker result: {type(result).__name__}"
        return True, result

    async def create_account_with_secret(
        self,
        *,
        secret: str,
        wallet: str | None = None,
        code: str | int | None = None,
        scw_owner: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_wallet = wallet or self.derive_wallet_address or self.wallet_address
        if not target_wallet:
            return False, "wallet, derive_wallet_address, or wallet_address is required"

        payload: dict[str, Any] = {"secret": secret, "wallet": target_wallet}
        if code is not None:
            payload["code"] = code
        if scw_owner is not None:
            payload["scw_owner"] = scw_owner
        return await self._post("/public/create_account_with_secret", payload)

    async def get_account(
        self, *, wallet: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        target_wallet = wallet or self.derive_wallet_address
        if not target_wallet:
            return False, "wallet or derive_wallet_address is required"
        return await self._post(
            "/private/get_account",
            {"wallet": target_wallet},
            private=True,
            auth_wallet=target_wallet,
        )

    async def get_subaccounts(
        self, *, wallet: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        target_wallet = wallet or self.derive_wallet_address
        if not target_wallet:
            return False, "wallet or derive_wallet_address is required"
        return await self._post(
            "/private/get_subaccounts",
            {"wallet": target_wallet},
            private=True,
            auth_wallet=target_wallet,
        )

    async def get_subaccount(
        self, *, subaccount_id: int
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/get_subaccount",
            {"subaccount_id": subaccount_id},
            private=True,
        )

    async def ensure_subaccount(
        self,
        *,
        wallet: str | None = None,
        preferred_subaccount_id: int | None = None,
        create_account_if_missing: bool = False,
        account_secret: str | None = None,
        account_code: str | int | None = None,
        scw_owner: str | None = None,
        create_if_missing: bool = False,
        amount: str | int | Decimal = "0",
        asset_name: str = "USDC",
        margin_type: MarginType = "SM",
        currency: str | None = None,
        asset_address: str | None = None,
        manager_address: str | None = None,
        asset_decimals: int | None = None,
        signer: str | None = None,
        nonce: int | None = None,
        signature_expiry_sec: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_wallet = wallet or self.derive_wallet_address
        if not target_wallet:
            return False, "wallet or derive_wallet_address is required"

        ok, subaccounts = await self.get_subaccounts(wallet=target_wallet)
        account_request: dict[str, Any] | None = None
        if not ok:
            if not create_account_if_missing:
                return False, subaccounts
            if not account_secret:
                return (
                    False,
                    "account_secret is required when create_account_if_missing=True",
                )
            ok, created_account = await self.create_account_with_secret(
                secret=account_secret,
                wallet=target_wallet,
                code=account_code,
                scw_owner=scw_owner,
            )
            if not ok:
                return False, created_account
            account_request = created_account
            ok, subaccounts = await self.get_subaccounts(wallet=target_wallet)
            if not ok:
                return False, {
                    "error": subaccounts,
                    "account": account_request,
                    "wallet": target_wallet,
                }
        if not isinstance(subaccounts, dict):
            return (
                False,
                f"Unexpected get_subaccounts result: {type(subaccounts).__name__}",
            )

        ids = subaccounts.get("subaccount_ids") or []
        if not isinstance(ids, list):
            return False, "Unexpected get_subaccounts subaccount_ids"

        if preferred_subaccount_id is not None and preferred_subaccount_id in ids:
            return True, {
                "status": "exists",
                "created": False,
                "subaccount_id": preferred_subaccount_id,
                "subaccount_ids": ids,
                "wallet": target_wallet,
                **({"account": account_request} if account_request else {}),
            }
        if preferred_subaccount_id is None and ids:
            return True, {
                "status": "exists",
                "created": False,
                "subaccount_id": ids[0],
                "subaccount_ids": ids,
                "wallet": target_wallet,
                **({"account": account_request} if account_request else {}),
            }
        if not create_if_missing:
            return False, {
                "error": "No matching Derive subaccount found",
                "subaccount_ids": ids,
                "wallet": target_wallet,
                **({"account": account_request} if account_request else {}),
            }

        ok, created = await self.create_subaccount(
            wallet=target_wallet,
            amount=amount,
            asset_name=asset_name,
            margin_type=margin_type,
            currency=currency,
            asset_address=asset_address,
            manager_address=manager_address,
            asset_decimals=asset_decimals,
            signer=signer,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
        )
        if not ok:
            return False, created
        return True, {
            "status": "create_requested",
            "created": True,
            "subaccount_id": None,
            "subaccount_ids": ids,
            "wallet": target_wallet,
            "request": created,
            **({"account": account_request} if account_request else {}),
        }

    async def get_positions(
        self, *, subaccount_id: int
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/get_positions",
            {"subaccount_id": subaccount_id},
            private=True,
        )

    async def get_open_orders(
        self, *, subaccount_id: int
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/get_open_orders",
            {"subaccount_id": subaccount_id},
            private=True,
        )

    async def get_deposit_history(
        self,
        *,
        subaccount_id: int,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        payload = self._history_payload(
            subaccount_id=subaccount_id,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        return await self._post(
            "/private/get_deposit_history",
            payload,
            private=True,
        )

    async def get_withdrawal_history(
        self,
        *,
        subaccount_id: int,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        payload = self._history_payload(
            subaccount_id=subaccount_id,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        return await self._post(
            "/private/get_withdrawal_history",
            payload,
            private=True,
        )

    async def get_margin(
        self,
        *,
        subaccount_id: int,
        simulated_position_changes: list[dict[str, Any]] | None = None,
        simulated_collateral_changes: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        payload: dict[str, Any] = {"subaccount_id": subaccount_id}
        if simulated_position_changes is not None:
            payload["simulated_position_changes"] = simulated_position_changes
        if simulated_collateral_changes is not None:
            payload["simulated_collateral_changes"] = simulated_collateral_changes
        return await self._post("/private/get_margin", payload, private=True)

    async def create_subaccount(
        self,
        *,
        amount: str | int | Decimal = "0",
        asset_name: str = "USDC",
        margin_type: MarginType = "SM",
        wallet: str | None = None,
        currency: str | None = None,
        asset_address: str | None = None,
        manager_address: str | None = None,
        asset_decimals: int | None = None,
        signer: str | None = None,
        nonce: int | None = None,
        signature_expiry_sec: int | None = None,
        signature: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_wallet = wallet or self.derive_wallet_address
        if not target_wallet:
            return False, "wallet or derive_wallet_address is required"

        ok, signed = await self._deposit_action_details(
            amount=amount,
            asset_name=asset_name,
            subaccount_id=0,
            asset_address=asset_address,
            manager_address=manager_address,
            asset_decimals=asset_decimals,
            margin_type=margin_type,
            currency=currency,
            owner=target_wallet,
            signer=signer,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
            signature=signature,
        )
        if not ok:
            return False, signed

        payload: dict[str, Any] = {
            "amount": self._amount_to_string(amount),
            "asset_name": asset_name.upper(),
            "margin_type": margin_type,
            "wallet": target_wallet,
            **signed,
        }
        if currency is not None:
            payload["currency"] = currency.upper()
        return await self._post(
            "/private/create_subaccount",
            payload,
            private=True,
            auth_wallet=target_wallet,
        )

    async def deposit_collateral(
        self,
        *,
        subaccount_id: int,
        amount: str | int | Decimal,
        asset_name: str = "USDC",
        asset_address: str | None = None,
        manager_address: str | None = None,
        asset_decimals: int | None = None,
        margin_type: MarginType = "SM",
        currency: str | None = None,
        owner: str | None = None,
        signer: str | None = None,
        nonce: int | None = None,
        signature_expiry_sec: int | None = None,
        signature: str | None = None,
        is_atomic_signing: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_owner = owner or self.derive_wallet_address
        ok, signed = await self._deposit_action_details(
            amount=amount,
            asset_name=asset_name,
            subaccount_id=subaccount_id,
            asset_address=asset_address,
            manager_address=manager_address,
            asset_decimals=asset_decimals,
            margin_type=margin_type,
            currency=currency,
            owner=target_owner,
            signer=signer,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
            signature=signature,
        )
        if not ok:
            return False, signed

        payload: dict[str, Any] = {
            "amount": self._amount_to_string(amount),
            "asset_name": asset_name.upper(),
            "subaccount_id": subaccount_id,
            **signed,
        }
        if is_atomic_signing:
            payload["is_atomic_signing"] = True
        return await self._post(
            "/private/deposit",
            payload,
            private=True,
            auth_wallet=target_owner,
        )

    async def withdraw_collateral(
        self,
        *,
        subaccount_id: int,
        amount: str | int | Decimal,
        asset_name: str = "USDC",
        asset_address: str | None = None,
        asset_decimals: int | None = None,
        owner: str | None = None,
        signer: str | None = None,
        nonce: int | None = None,
        signature_expiry_sec: int | None = None,
        signature: str | None = None,
        is_atomic_signing: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_owner = owner or self.derive_wallet_address
        resolved_asset = self._asset_address(asset_name, asset_address)
        if not resolved_asset:
            return False, "asset_address is required for this Derive asset"
        decimals = self._asset_decimals(asset_name, asset_decimals)

        try:
            module_data = self._withdraw_module_data(
                amount=amount,
                asset_address=resolved_asset,
                asset_decimals=decimals,
            )
        except ValueError as exc:
            return False, str(exc)

        ok, signed = await self._action_details(
            subaccount_id=subaccount_id,
            module_address=self.protocol_constants["withdrawal_module_address"],
            module_data=module_data,
            owner=target_owner,
            signer=signer,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
            signature=signature,
        )
        if not ok:
            return False, signed

        payload: dict[str, Any] = {
            "amount": self._amount_to_string(amount),
            "asset_name": asset_name.upper(),
            "subaccount_id": subaccount_id,
            **signed,
        }
        if is_atomic_signing:
            payload["is_atomic_signing"] = True
        return await self._post(
            "/private/withdraw",
            payload,
            private=True,
            auth_wallet=target_owner,
        )

    async def transfer_erc20(
        self,
        *,
        subaccount_id: int,
        recipient_subaccount_id: int,
        amount: str | int | Decimal,
        asset_name: str = "USDC",
        asset_address: str | None = None,
        asset_sub_id: int = 0,
        owner: str | None = None,
        signer: str | None = None,
        recipient_signer: str | None = None,
        nonce: int | None = None,
        recipient_nonce: int | None = None,
        signature_expiry_sec: int | None = None,
        recipient_signature_expiry_sec: int | None = None,
        sender_details: dict[str, Any] | None = None,
        recipient_details: dict[str, Any] | None = None,
        manager_if_new_account: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_owner = owner or self.derive_wallet_address
        resolved_asset = self._asset_address(asset_name, asset_address)
        if not resolved_asset:
            return False, "asset_address is required for this Derive asset"

        if sender_details is None:
            try:
                sender_module_data = self._sender_transfer_erc20_module_data(
                    recipient_subaccount_id=recipient_subaccount_id,
                    asset_address=resolved_asset,
                    asset_sub_id=asset_sub_id,
                    amount=amount,
                    manager_if_new_account=(
                        manager_if_new_account or DERIVE_DEFAULT_TRANSFER_MANAGER
                    ),
                )
            except ValueError as exc:
                return False, str(exc)
            ok, sender_details = await self._action_details(
                subaccount_id=subaccount_id,
                module_address=self.protocol_constants["transfer_module_address"],
                module_data=sender_module_data,
                owner=target_owner,
                signer=signer,
                nonce=nonce,
                signature_expiry_sec=signature_expiry_sec,
            )
            if not ok:
                return False, sender_details
        else:
            ok, error = self._validate_action_details(sender_details)
            if not ok:
                return False, error

        if recipient_details is None:
            ok, recipient_details = await self._action_details(
                subaccount_id=recipient_subaccount_id,
                module_address=self.protocol_constants["transfer_module_address"],
                module_data=b"",
                owner=target_owner,
                signer=recipient_signer or signer,
                nonce=recipient_nonce,
                signature_expiry_sec=(
                    recipient_signature_expiry_sec or signature_expiry_sec
                ),
            )
            if not ok:
                return False, recipient_details
        else:
            ok, error = self._validate_action_details(recipient_details)
            if not ok:
                return False, error

        payload = {
            "subaccount_id": subaccount_id,
            "recipient_subaccount_id": recipient_subaccount_id,
            "sender_details": sender_details,
            "recipient_details": recipient_details,
            "transfer": {
                "address": Web3.to_checksum_address(resolved_asset),
                "amount": self._amount_to_string(amount),
                "sub_id": asset_sub_id,
            },
        }
        return await self._post(
            "/private/transfer_erc20",
            payload,
            private=True,
            auth_wallet=target_owner,
        )

    async def sign_order(
        self,
        order: dict[str, Any],
        *,
        asset_address: str,
        asset_sub_id: int,
        owner: str | None = None,
        signer: str | None = None,
        recipient_id: int | None = None,
        nonce: int | None = None,
        signature_expiry_sec: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, error = self._validate_unsigned_order(order)
        if not ok:
            return False, error

        direction = str(order["direction"]).lower()
        if direction not in {"buy", "sell"}:
            return False, "direction must be 'buy' or 'sell'"

        subaccount_id = int(order["subaccount_id"])
        try:
            module_data = self._trade_module_data(
                asset_address=asset_address,
                asset_sub_id=asset_sub_id,
                limit_price=order["limit_price"],
                amount=order["amount"],
                max_fee=order["max_fee"],
                recipient_id=recipient_id or subaccount_id,
                is_bid=direction == "buy",
            )
        except ValueError as exc:
            return False, str(exc)

        ok, signed = await self._action_details(
            subaccount_id=subaccount_id,
            module_address=self.protocol_constants["trade_module_address"],
            module_data=module_data,
            owner=owner or self.derive_wallet_address,
            signer=signer,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
        )
        if not ok:
            return False, signed

        signed_order = dict(order)
        signed_order["direction"] = direction
        signed_order.update(signed)
        return True, signed_order

    async def place_order(
        self,
        order: dict[str, Any],
        *,
        asset_address: str,
        asset_sub_id: int,
        dry_run: bool = True,
        owner: str | None = None,
        signer: str | None = None,
        recipient_id: int | None = None,
        nonce: int | None = None,
        signature_expiry_sec: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, signed_order = await self.sign_order(
            order,
            asset_address=asset_address,
            asset_sub_id=asset_sub_id,
            owner=owner,
            signer=signer,
            recipient_id=recipient_id,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
        )
        if not ok:
            return False, signed_order
        return await self.submit_order(signed_order, dry_run=dry_run)

    async def debug_order(
        self, order: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, error = self._validate_signed_order(order)
        if not ok:
            return False, error
        return await self._post("/private/order_debug", dict(order), private=True)

    async def submit_order(
        self,
        order: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, error = self._validate_signed_order(order)
        if not ok:
            return False, error

        path = "/private/order_debug" if dry_run else "/private/order"
        return await self._post(path, dict(order), private=True)

    async def cancel_order(
        self,
        *,
        instrument_name: str,
        order_id: str,
        subaccount_id: int,
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/cancel",
            {
                "instrument_name": instrument_name,
                "order_id": order_id,
                "subaccount_id": subaccount_id,
            },
            private=True,
        )

    @staticmethod
    def _validate_signed_order(order: dict[str, Any]) -> tuple[bool, str]:
        missing = sorted(DERIVE_ORDER_REQUIRED_FIELDS.difference(order))
        if missing:
            return False, f"missing signed Derive order fields: {', '.join(missing)}"
        return True, ""

    @staticmethod
    def _validate_unsigned_order(order: dict[str, Any]) -> tuple[bool, str]:
        missing = sorted(DERIVE_ORDER_UNSIGNED_FIELDS.difference(order))
        if missing:
            return False, f"missing unsigned Derive order fields: {', '.join(missing)}"
        return True, ""

    @staticmethod
    def expiry_date(expiry_timestamp: int) -> str:
        return datetime.fromtimestamp(expiry_timestamp, UTC).strftime("%Y%m%d")

    @staticmethod
    def new_order_nonce(now_ms: int | None = None) -> int:
        timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return timestamp_ms * 1000 + secrets.randbelow(1000)

    @staticmethod
    def new_action_nonce(now_ms: int | None = None) -> int:
        return DeriveAdapter.new_order_nonce(now_ms=now_ms)

    @staticmethod
    def orderbook_channel(
        instrument_name: str,
        *,
        group: str = "1",
        depth: str = "10",
    ) -> str:
        if group not in DERIVE_ORDERBOOK_GROUPS:
            raise ValueError("group must be one of: 1, 10, 100")
        if depth not in DERIVE_ORDERBOOK_DEPTHS:
            raise ValueError("depth must be one of: 1, 10, 20, 100")
        return f"orderbook.{instrument_name}.{group}.{depth}"

    def _protocol_constants(self, derive_config: dict[str, Any]) -> dict[str, Any]:
        constants = dict(DERIVE_PROTOCOL_CONSTANTS[self.network])
        for key in (
            "domain_separator",
            "action_typehash",
            "deposit_module_address",
            "trade_module_address",
            "withdrawal_module_address",
            "transfer_module_address",
            "cash_asset_address",
            "standard_risk_manager_address",
            "chain_id",
        ):
            if derive_config.get(key) is not None:
                constants[key] = derive_config[key]

        for key in (
            "portfolio_risk_manager_addresses",
            "portfolio_risk_manager_v2_addresses",
        ):
            merged = dict(constants.get(key) or {})
            override = derive_config.get(key)
            if isinstance(override, dict):
                merged.update({str(k).upper(): v for k, v in override.items()})
            constants[key] = merged
        return constants

    def _asset_address(self, asset_name: str, asset_address: str | None) -> str | None:
        if asset_address:
            return asset_address
        if asset_name.upper() == "USDC":
            return str(self.protocol_constants["cash_asset_address"])
        asset_addresses = self.config.get("derive", {}).get("asset_addresses")
        if isinstance(asset_addresses, dict):
            configured = asset_addresses.get(asset_name.upper())
            if isinstance(configured, str):
                return configured
        return None

    @staticmethod
    def _asset_decimals(asset_name: str, asset_decimals: int | None) -> int:
        if asset_decimals is not None:
            return asset_decimals
        return DERIVE_DEFAULT_ASSET_DECIMALS.get(asset_name.upper(), 18)

    def _manager_address(
        self,
        *,
        margin_type: MarginType,
        currency: str | None,
        manager_address: str | None,
    ) -> str | None:
        if manager_address:
            return manager_address
        if margin_type == "SM":
            return str(self.protocol_constants["standard_risk_manager_address"])
        if not currency:
            return None
        manager_map_key = (
            "portfolio_risk_manager_v2_addresses"
            if margin_type == "PM2"
            else "portfolio_risk_manager_addresses"
        )
        manager_map = self.protocol_constants.get(manager_map_key) or {}
        return manager_map.get(currency.upper())

    async def _deposit_action_details(
        self,
        *,
        amount: str | int | Decimal,
        asset_name: str,
        subaccount_id: int,
        asset_address: str | None,
        manager_address: str | None,
        asset_decimals: int | None,
        margin_type: MarginType,
        currency: str | None,
        owner: str | None,
        signer: str | None,
        nonce: int | None,
        signature_expiry_sec: int | None,
        signature: str | None,
    ) -> tuple[bool, dict[str, Any] | str]:
        resolved_asset = self._asset_address(asset_name, asset_address)
        if not resolved_asset:
            return False, "asset_address is required for this Derive asset"
        resolved_manager = self._manager_address(
            margin_type=margin_type,
            currency=currency,
            manager_address=manager_address,
        )
        if not resolved_manager:
            return False, "manager_address is required for this margin_type/currency"

        try:
            module_data = self._deposit_module_data(
                amount=amount,
                asset_address=resolved_asset,
                manager_address=resolved_manager,
                asset_decimals=self._asset_decimals(asset_name, asset_decimals),
            )
        except ValueError as exc:
            return False, str(exc)

        return await self._action_details(
            subaccount_id=subaccount_id,
            module_address=self.protocol_constants["deposit_module_address"],
            module_data=module_data,
            owner=owner,
            signer=signer,
            nonce=nonce,
            signature_expiry_sec=signature_expiry_sec,
            signature=signature,
        )

    async def _action_details(
        self,
        *,
        subaccount_id: int,
        module_address: str,
        module_data: bytes,
        owner: str | None,
        signer: str | None,
        nonce: int | None,
        signature_expiry_sec: int | None,
        signature: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        target_owner = owner or self.derive_wallet_address
        target_signer = signer or self.wallet_address or target_owner
        if not target_owner:
            return False, "owner or derive_wallet_address is required"
        if not target_signer:
            return False, "signer or wallet_address is required"

        action_nonce = nonce if nonce is not None else self.new_action_nonce()
        expiry = (
            signature_expiry_sec
            if signature_expiry_sec is not None
            else int(time.time()) + DERIVE_ACTION_SIGNATURE_EXPIRY_BUFFER_SEC
        )
        if expiry <= int(time.time()) + 300:
            return False, "signature_expiry_sec must be more than 5 minutes from now"

        details = {
            "nonce": action_nonce,
            "signature_expiry_sec": expiry,
            "signer": Web3.to_checksum_address(target_signer),
        }
        if signature is not None:
            details["signature"] = self._ensure_0x(signature)
            return True, details

        if self.sign_hash_callback is None:
            return False, "sign_hash_callback is required for Derive action signing"

        try:
            action_hash = self.signed_action_hash(
                subaccount_id=subaccount_id,
                nonce=action_nonce,
                module_address=module_address,
                module_data=module_data,
                signature_expiry_sec=expiry,
                owner=target_owner,
                signer=target_signer,
                domain_separator=self.protocol_constants["domain_separator"],
                action_typehash=self.protocol_constants["action_typehash"],
            )
        except ValueError as exc:
            return False, str(exc)

        signature_value = await self.sign_hash_callback(action_hash)
        details["signature"] = self._ensure_0x(signature_value)
        return True, details

    @staticmethod
    def _validate_action_details(details: dict[str, Any]) -> tuple[bool, str]:
        missing = sorted(DERIVE_ACTION_DETAIL_FIELDS.difference(details))
        if missing:
            return False, f"missing Derive action detail fields: {', '.join(missing)}"
        return True, ""

    @staticmethod
    def _history_payload(
        *,
        subaccount_id: int,
        start_timestamp: int | None,
        end_timestamp: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"subaccount_id": subaccount_id}
        if start_timestamp is not None:
            payload["start_timestamp"] = start_timestamp
        if end_timestamp is not None:
            payload["end_timestamp"] = end_timestamp
        return payload

    @staticmethod
    def _amount_to_string(amount: str | int | Decimal) -> str:
        return str(amount)

    @staticmethod
    def _scale_amount(amount: str | int | Decimal, decimals: int) -> int:
        try:
            decimal_amount = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal amount: {amount}") from exc
        scaled = decimal_amount.scaleb(decimals)
        if scaled != scaled.to_integral_value():
            raise ValueError(
                f"amount {amount} has more precision than {decimals} decimals"
            )
        if scaled < 0:
            raise ValueError("amount must be non-negative")
        return int(scaled)

    @classmethod
    def _deposit_module_data(
        cls,
        *,
        amount: str | int | Decimal,
        asset_address: str,
        manager_address: str,
        asset_decimals: int,
    ) -> bytes:
        return abi_encode(
            ["uint256", "address", "address"],
            [
                cls._scale_amount(amount, asset_decimals),
                Web3.to_checksum_address(asset_address),
                Web3.to_checksum_address(manager_address),
            ],
        )

    @classmethod
    def _withdraw_module_data(
        cls,
        *,
        amount: str | int | Decimal,
        asset_address: str,
        asset_decimals: int,
    ) -> bytes:
        return abi_encode(
            ["address", "uint"],
            [
                Web3.to_checksum_address(asset_address),
                cls._scale_amount(amount, asset_decimals),
            ],
        )

    @classmethod
    def _trade_module_data(
        cls,
        *,
        asset_address: str,
        asset_sub_id: int,
        limit_price: str | int | Decimal,
        amount: str | int | Decimal,
        max_fee: str | int | Decimal,
        recipient_id: int,
        is_bid: bool,
    ) -> bytes:
        return abi_encode(
            ["address", "uint", "int", "int", "uint", "uint", "bool"],
            [
                Web3.to_checksum_address(asset_address),
                asset_sub_id,
                cls._scale_amount(limit_price, 18),
                cls._scale_amount(amount, 18),
                cls._scale_amount(max_fee, 18),
                recipient_id,
                is_bid,
            ],
        )

    @classmethod
    def _sender_transfer_erc20_module_data(
        cls,
        *,
        recipient_subaccount_id: int,
        asset_address: str,
        asset_sub_id: int,
        amount: str | int | Decimal,
        manager_if_new_account: str,
    ) -> bytes:
        return abi_encode(
            ["(uint,address,(address,uint,int)[])"],
            [
                (
                    recipient_subaccount_id,
                    Web3.to_checksum_address(manager_if_new_account),
                    [
                        (
                            Web3.to_checksum_address(asset_address),
                            asset_sub_id,
                            cls._scale_amount(amount, 18),
                        )
                    ],
                )
            ],
        )

    @staticmethod
    def signed_action_hash(
        *,
        subaccount_id: int,
        nonce: int,
        module_address: str,
        module_data: bytes,
        signature_expiry_sec: int,
        owner: str,
        signer: str,
        domain_separator: str,
        action_typehash: str,
    ) -> str:
        domain_separator_bytes = DeriveAdapter._bytes32(
            domain_separator, "domain_separator"
        )
        action_typehash_bytes = DeriveAdapter._bytes32(
            action_typehash, "action_typehash"
        )
        action_hash = Web3.keccak(
            abi_encode(
                [
                    "bytes32",
                    "uint",
                    "uint",
                    "address",
                    "bytes32",
                    "uint",
                    "address",
                    "address",
                ],
                [
                    action_typehash_bytes,
                    subaccount_id,
                    nonce,
                    Web3.to_checksum_address(module_address),
                    Web3.keccak(module_data),
                    signature_expiry_sec,
                    Web3.to_checksum_address(owner),
                    Web3.to_checksum_address(signer),
                ],
            )
        )
        typed_data_hash = Web3.keccak(
            b"\x19\x01" + domain_separator_bytes + bytes(action_hash)
        )
        return "0x" + bytes(typed_data_hash).hex()

    @staticmethod
    def _bytes32(value: str, field_name: str) -> bytes:
        try:
            raw = bytes.fromhex(value.removeprefix("0x"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be 32 bytes hex") from exc
        if len(raw) != 32:
            raise ValueError(f"{field_name} must be 32 bytes hex")
        return raw

    @staticmethod
    def _ensure_0x(value: str) -> str:
        return value if value.startswith("0x") else f"0x{value}"
