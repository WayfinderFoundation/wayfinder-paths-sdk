from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from eth_account import Account

from wayfinder_paths.mcp.utils import find_wallet_by_label, load_config_json

# Known signing callback parameter names used by adapters
_SIGNING_CALLBACK_PARAMS = frozenset(
    {
        "strategy_wallet_signing_callback",
        "sign_callback",
        "signing_callback",
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


def _find_wallet_by_label_in_config(
    config: dict[str, Any], wallet_label: str
) -> dict[str, Any] | None:
    want = str(wallet_label).strip()
    if not want:
        return None
    wallets = config.get("wallets")
    if not isinstance(wallets, list):
        return None
    for w in wallets:
        if not isinstance(w, dict):
            continue
        if str(w.get("label", "")).strip() == want:
            return w
    return None


def get_adapter[T](
    adapter_class: type[T],
    wallet_label: str | None = None,
    *,
    config_path: str | Path | None = None,
    config_overrides: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    config = (
        load_config_json(config_path, require_exists=True)
        if config_path is not None
        else load_config_json()
    )

    if config_overrides:
        config = {**config, **config_overrides}

    sign_callback = None
    if wallet_label:
        wallet = _find_wallet_by_label_in_config(config, wallet_label)
        if wallet is None and config_path is None:
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
