"""Typed, exact chart context accepted by the Fractal Scan quant tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

FractalScanKind = Literal["hyperliquid", "onchain"]

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 3_600_000,
    "2h": 2 * 3_600_000,
    "4h": 4 * 3_600_000,
    "8h": 8 * 3_600_000,
    "12h": 12 * 3_600_000,
    "1d": 86_400_000,
}
SUPPORTED_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
MAX_PATTERN_BARS = 128
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class FractalScanRequest:
    kind: FractalScanKind
    interval: str
    start_ms: int
    end_ms: int
    display_symbol: str
    market_id: str
    chart_id: str
    selected_price_min: float | None = None
    selected_price_max: float | None = None
    hl_coin: str | None = None
    chain_id: int | None = None
    token_address: str | None = None


def create_fractal_scan_request(
    *,
    kind: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    display_symbol: str,
    market_id: str,
    chart_id: str,
    selected_price_min: float | None = None,
    selected_price_max: float | None = None,
    hl_coin: str | None = None,
    chain_id: int | None = None,
    token_address: str | None = None,
) -> FractalScanRequest:
    if kind not in {"hyperliquid", "onchain"}:
        raise ValueError("kind must be hyperliquid or onchain")
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("start_ms must be non-negative and before end_ms")
    for field_name, value in (
        ("display_symbol", display_symbol),
        ("market_id", market_id),
        ("chart_id", chart_id),
    ):
        if not value.strip():
            raise ValueError(f"{field_name} is required")
    if selected_price_min is not None and selected_price_min <= 0:
        raise ValueError("selected_price_min must be positive")
    if selected_price_max is not None and selected_price_max <= 0:
        raise ValueError("selected_price_max must be positive")
    if (
        selected_price_min is not None
        and selected_price_max is not None
        and selected_price_max <= selected_price_min
    ):
        raise ValueError("selected_price_max must exceed selected_price_min")
    if kind == "hyperliquid":
        if not hl_coin or not hl_coin.strip():
            raise ValueError("hl_coin is required for Hyperliquid scans")
    else:
        if chain_id is None or chain_id <= 0:
            raise ValueError("chain_id is required for onchain scans")
        if not token_address or not EVM_ADDRESS.fullmatch(token_address):
            raise ValueError("token_address must be an exact EVM contract address")
    return FractalScanRequest(
        kind="hyperliquid" if kind == "hyperliquid" else "onchain",
        interval=interval,
        start_ms=int(start_ms),
        end_ms=int(end_ms),
        display_symbol=display_symbol.strip(),
        market_id=market_id.strip(),
        chart_id=chart_id.strip(),
        selected_price_min=selected_price_min,
        selected_price_max=selected_price_max,
        hl_coin=hl_coin.strip() if hl_coin else None,
        chain_id=chain_id,
        token_address=token_address,
    )
