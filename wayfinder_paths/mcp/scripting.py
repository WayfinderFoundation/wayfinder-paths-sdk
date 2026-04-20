from __future__ import annotations

import inspect
from typing import Any

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.utils.signing import build_signing_callbacks


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
        params = set(inspect.signature(adapter_class.__init__).parameters)
        signing = await build_signing_callbacks(wallet_label)

        if "signing" in params:
            adapter_kwargs["signing"] = signing
            if "wallet_address" in params:
                adapter_kwargs["wallet_address"] = signing.address

        elif "main_signing" in params:
            adapter_kwargs["main_signing"] = signing
            if "main_wallet_address" in params:
                adapter_kwargs["main_wallet_address"] = signing.address

            if "strategy_signing" not in kwargs:
                if not strategy_wallet_label:
                    raise ValueError(
                        f"{adapter_class.__name__} requires a strategy wallet. "
                        "Pass strategy_wallet_label."
                    )
                strategy_signing = await build_signing_callbacks(strategy_wallet_label)
                adapter_kwargs["strategy_signing"] = strategy_signing
                if "strategy_wallet_address" in params:
                    adapter_kwargs["strategy_wallet_address"] = strategy_signing.address

        else:
            raise ValueError(
                f"{adapter_class.__name__} does not accept signing callbacks."
            )

    adapter_kwargs.update(kwargs)
    return adapter_class(**adapter_kwargs)
