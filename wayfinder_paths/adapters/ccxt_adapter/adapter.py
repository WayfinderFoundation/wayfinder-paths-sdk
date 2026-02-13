from __future__ import annotations

from typing import Any

try:
    import ccxt.async_support as ccxt_async
except ModuleNotFoundError:  # pragma: no cover
    ccxt_async = None  # type: ignore[assignment]

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter


class CCXTAdapter(BaseAdapter):
    adapter_type = "CCXT"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        exchanges: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if ccxt_async is None:
            raise ModuleNotFoundError(
                "ccxt is not installed. Install it to use CCXTAdapter."
            )
        super().__init__("ccxt_adapter", config)

        merged = exchanges or self.config.get("ccxt") or {}

        self._exchanges: dict[str, ccxt_async.Exchange] = {}
        for exchange_id, opts in merged.items():
            exchange_cls = getattr(ccxt_async, exchange_id, None)
            if exchange_cls is None:
                raise ValueError(
                    f"Unknown exchange '{exchange_id}'. "
                    f"Must be a valid ccxt exchange id."
                )

            # See each exchange's describe() method for accepted params: https://docs.ccxt.com/#/README?id=exchange-structure
            instance = exchange_cls(opts)

            self._exchanges[exchange_id] = instance
            setattr(self, exchange_id, instance)

    async def close(self) -> None:
        for ex in self._exchanges.values():
            await ex.close()
