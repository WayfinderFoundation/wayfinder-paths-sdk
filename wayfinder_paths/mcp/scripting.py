from __future__ import annotations

import inspect
from typing import Any

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.utils.wallets import make_sign_callback
from wayfinder_paths.mcp.utils import find_wallet_by_label


def _resolve_wallet(label: str) -> tuple[Any, str]:
    wallet = find_wallet_by_label(label)
    if not wallet:
        raise ValueError(
            f"Wallet '{label}' not found in config.json. Run 'just create-wallets'."
        )
    private_key = wallet.get("private_key") or wallet.get("private_key_hex")
    if not private_key:
        raise ValueError(
            f"Wallet '{label}' is missing private_key_hex. "
            "Local signing requires a private key."
        )
    return make_sign_callback(private_key), wallet["address"]


def get_adapter[T](
    adapter_class: type[T],
    wallet_label: str | None = None,
    strategy_wallet_label: str | None = None,
    *,
    config_overrides: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    config = dict(CONFIG)
    if config_overrides:
        config.update(config_overrides)

    adapter_kwargs: dict[str, Any] = {"config": config}

    if wallet_label:
        sign_cb, address = _resolve_wallet(wallet_label)
        params = set(inspect.signature(adapter_class.__init__).parameters)

        if "sign_callback" in params:
            adapter_kwargs["sign_callback"] = sign_cb
            if "wallet_address" in params:
                adapter_kwargs["wallet_address"] = address

        elif "main_sign_callback" in params:
            adapter_kwargs["main_sign_callback"] = sign_cb
            if "main_wallet_address" in params:
                adapter_kwargs["main_wallet_address"] = address

            if "strategy_sign_callback" not in kwargs:
                if not strategy_wallet_label:
                    raise ValueError(
                        f"{adapter_class.__name__} requires a strategy wallet. "
                        "Pass strategy_wallet_label."
                    )
                strategy_cb, strategy_addr = _resolve_wallet(strategy_wallet_label)
                adapter_kwargs["strategy_sign_callback"] = strategy_cb
                if "strategy_wallet_address" in params:
                    adapter_kwargs["strategy_wallet_address"] = strategy_addr

        else:
            raise ValueError(
                f"{adapter_class.__name__} does not accept a signing callback."
            )

    adapter_kwargs.update(kwargs)
    return adapter_class(**adapter_kwargs)
