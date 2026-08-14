from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.core.perps import ccxt_history


async def test_fetch_perp_history_uses_first_healthy_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fetch_exchange(
        exchange_id: str,
        base_symbol: str,
        interval: str,
        *,
        interval_ms: int,
        start_ms: int,
        end_ms: int,
    ) -> tuple[str, list[dict[str, float | int]]]:
        calls.append(exchange_id)
        if exchange_id == "okx":
            raise RuntimeError("451 restricted")
        return "SOL/USDT:USDT", [{"t": 0, "c": 100.0}]

    monkeypatch.setattr(ccxt_history, "_fetch_exchange_history", fetch_exchange)

    result = await ccxt_history.fetch_ccxt_perp_history(
        "SOL",
        "5m",
        interval_ms=300_000,
        start_ms=0,
        end_ms=300_000,
    )

    assert calls == ["okx", "bitget"]
    assert result.exchange_id == "bitget"
    assert result.market_symbol == "SOL/USDT:USDT"
    assert result.failures == ("okx:RuntimeError:451 restricted",)


def test_resolve_linear_perp_symbol_never_uses_spot() -> None:
    markets: dict[str, Any] = {
        "SOL/USDT": {
            "active": True,
            "spot": True,
            "swap": False,
            "base": "SOL",
            "quote": "USDT",
        },
        "SOL/USDC:USDC": {
            "active": True,
            "swap": True,
            "linear": True,
            "base": "SOL",
            "quote": "USDC",
        },
    }

    assert ccxt_history._resolve_linear_perp_symbol(markets, "SOL") == "SOL/USDC:USDC"

    with pytest.raises(ValueError, match="linear perpetual"):
        ccxt_history._resolve_linear_perp_symbol(
            {"SOL/USDT": markets["SOL/USDT"]},
            "SOL",
        )
