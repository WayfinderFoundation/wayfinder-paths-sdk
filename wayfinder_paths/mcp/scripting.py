from __future__ import annotations

import inspect
from typing import Any

import wayfinder_paths.core.utils.wallets as wallet_utils
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.utils.wallets import (
    get_wallet_sign_hash_callback,
    get_wallet_sign_typed_data_callback,
    get_wallet_signing_callback,
)


async def get_adapter[T](
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
        sign_cb, address = await get_wallet_signing_callback(wallet_label)
        params = set(inspect.signature(adapter_class.__init__).parameters)

        if "sign_callback" in params:
            adapter_kwargs["sign_callback"] = sign_cb
            if "wallet_address" in params:
                adapter_kwargs["wallet_address"] = address
            if "sign_hash_callback" in params:
                hash_cb, _ = await get_wallet_sign_hash_callback(wallet_label)
                adapter_kwargs["sign_hash_callback"] = hash_cb
            if "sign_typed_data_callback" in params:
                typed_cb, _ = await get_wallet_sign_typed_data_callback(wallet_label)
                adapter_kwargs["sign_typed_data_callback"] = typed_cb
            if "private_key" in params:
                wallet = await wallet_utils.find_wallet_by_label(wallet_label)
                if wallet:
                    private_key = wallet_utils.get_private_key(wallet)
                    if private_key:
                        adapter_kwargs["private_key"] = private_key

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
                strategy_cb, strategy_addr = await get_wallet_signing_callback(
                    strategy_wallet_label
                )
                adapter_kwargs["strategy_sign_callback"] = strategy_cb
                if "strategy_wallet_address" in params:
                    adapter_kwargs["strategy_wallet_address"] = strategy_addr

        else:
            raise ValueError(
                f"{adapter_class.__name__} does not accept a signing callback."
            )

    adapter_kwargs.update(kwargs)
    return adapter_class(**adapter_kwargs)
