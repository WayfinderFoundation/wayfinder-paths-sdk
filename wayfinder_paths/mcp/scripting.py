from __future__ import annotations

import inspect
from typing import Any

from eth_account import Account

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.mcp.utils import find_wallet_by_label

# Known signing callback parameter names used by adapters
_SIGNING_CALLBACK_PARAMS = frozenset(
    {
        "signing_callback",
        "sign_callback",
    }
)


def _make_sign_callback(private_key: str):
    account = Account.from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


def _detect_callback_params(adapter_class: type) -> set[str]:
    try:
        sig = inspect.signature(adapter_class.__init__)
    except (ValueError, TypeError):
        return set()

    return {
        name
        for name in sig.parameters
        if name in _SIGNING_CALLBACK_PARAMS or name.endswith("_signing_callback")
    }


def get_adapter[T](
    adapter_class: type[T],
    wallet_label: str | None = None,
    *,
    config_overrides: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    config = dict(CONFIG)

    if config_overrides:
        config.update(config_overrides)

    sign_callback = None
    if wallet_label:
        wallet = find_wallet_by_label(wallet_label)
        if not wallet:
            raise ValueError(
                f"Wallet '{wallet_label}' not found in config.json. "
                "Run 'just create-wallets'."
            )

        private_key = wallet.get("private_key") or wallet.get("private_key_hex")
        if not private_key:
            raise ValueError(
                f"Wallet '{wallet_label}' is missing private_key_hex. "
                "Local signing requires a private key."
            )

        config["strategy_wallet"] = wallet
        sign_callback = _make_sign_callback(private_key)

    callback_params = _detect_callback_params(adapter_class)
    adapter_kwargs: dict[str, Any] = {"config": config}

    if sign_callback and callback_params:
        for param_name in callback_params:
            if param_name not in kwargs:
                adapter_kwargs[param_name] = sign_callback

    adapter_kwargs.update(kwargs)

    return adapter_class(**adapter_kwargs)
