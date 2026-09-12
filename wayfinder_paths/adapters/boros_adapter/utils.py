"""Boros-specific utilities (tick math, parsing helpers, conversions)."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

BOROS_TICK_BASE = 1.0001


def tick_from_rate(
    rate: float, tick_step: int, *, round_down: bool, base: float = BOROS_TICK_BASE
) -> int:
    """Convert APR rate to Boros limitTick."""
    if tick_step <= 0:
        tick_step = 1
    ln_base = math.log(base)
    if rate >= 0:
        if rate == 0:
            return 0
        raw = math.log1p(rate) / (tick_step * ln_base)
        return int(math.floor(raw) if round_down else math.ceil(raw))
    # Negative rate
    raw = math.log1p(-rate) / (tick_step * ln_base)
    return -int(math.floor(raw) if round_down else math.ceil(raw))


def rate_from_tick(tick: int, tick_step: int, base: float = BOROS_TICK_BASE) -> float:
    """Convert Boros limitTick to APR rate."""
    if tick_step <= 0:
        tick_step = 1
    p = base ** (abs(int(tick)) * int(tick_step))
    r = p - 1
    return r if tick >= 0 else -r


def normalize_apr(value: Any) -> float | None:
    """Normalize various APR encodings to decimal.

    Handles: decimal (0.1115), percent (11.15), bps (1115), 1e18-scaled.
    """
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None

    if x == 0:
        return None
    # 1e18-scaled decimal
    if x > 1e9:
        return x / 1e18
    # bps (1115 = 11.15%)
    if x > 1000:
        return x / 10_000.0
    # percent (11.15 = 11.15%)
    if x > 1:
        return x / 100.0
    # already decimal
    return x


def cash_wei_to_float(value_wei: Any) -> float:
    """Convert Boros cash units (1e18) to float."""
    if value_wei is None:
        return 0.0
    try:
        return float(Decimal(str(value_wei)) / Decimal(1e18))
    except Exception:
        return 0.0


def parse_market_name_maturity(market_name: str) -> datetime | None:
    """Parse maturity date from a Boros market name.

    Market names follow the pattern: {PAIR}-{VENUE}-{TYPE}-{YYMMDD}
    Examples:
      BTCUSDT-BN-T-260327  → 2026-03-27 UTC
      BTCUSDC-HL-$-260327  → 2026-03-27 UTC
      ETHUSDT-BN-T-260327  → 2026-03-27 UTC

    Returns a UTC datetime at midnight on the maturity date, or None if
    the name does not match the expected pattern.
    """
    if not market_name:
        return None
    m = re.search(r"-(\d{6})$", market_name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return dt


def parse_market_name_maturity_ts(market_name: str) -> int | None:
    """Like parse_market_name_maturity but returns a Unix timestamp (int) or None."""
    dt = parse_market_name_maturity(market_name)
    return int(dt.timestamp()) if dt is not None else None


def market_id_from_market_acc(market_acc: str) -> int | None:
    """Parse a Boros `marketAcc` into a market_id (last 3 bytes)."""
    if not market_acc or len(market_acc) < 8:
        return None
    try:
        market_id = int(market_acc[-6:], 16)
    except ValueError:
        return None
    if market_id == 0xFFFFFF:
        return None
    return market_id


def account_id_from_market_acc(market_acc: str) -> int | None:
    """Parse a Boros `marketAcc` into an account id."""
    raw = _market_acc_hex(market_acc)
    if raw is None:
        return None
    try:
        return int(raw[40:42], 16)
    except ValueError:
        return None


def token_id_from_market_acc(market_acc: str) -> int | None:
    """Parse a Boros `marketAcc` into a token id."""
    raw = _market_acc_hex(market_acc)
    if raw is None:
        return None
    try:
        return int(raw[42:46], 16)
    except ValueError:
        return None


def is_cross_market_acc(market_acc: str) -> bool:
    """Return True when `marketAcc` points at Boros cross margin."""
    raw = _market_acc_hex(market_acc)
    if raw is None:
        return False
    try:
        return int(raw[-6:], 16) == 0xFFFFFF
    except ValueError:
        return False


def build_market_acc_hex(
    *,
    address: str,
    account_id: int,
    token_id: int,
    market_id: int,
) -> str:
    """Build a packed Boros `marketAcc` hex string."""
    addr = str(address).lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    if len(addr) != 40:
        raise ValueError("address must be a 20-byte hex address")
    try:
        int(addr, 16)
    except ValueError as exc:
        raise ValueError("address must be hex encoded") from exc
    return (
        "0x"
        + addr
        + int(account_id).to_bytes(1, "big").hex()
        + int(token_id).to_bytes(2, "big").hex()
        + int(market_id).to_bytes(3, "big").hex()
    )


def _market_acc_hex(market_acc: str) -> str | None:
    if not market_acc:
        return None
    raw = str(market_acc).lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    # root(20 bytes) + accountId(1) + tokenId(2) + marketId(3)
    if len(raw) != 52:
        return None
    return raw
