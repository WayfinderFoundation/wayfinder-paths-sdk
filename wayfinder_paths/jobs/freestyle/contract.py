from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from wayfinder_paths.jobs.execution.primitives import OrderIntent

# Venues the freestyle runtime can quote and fill (paper through the venue's
# paper broker, live through its real broker). `onchain` is spot: token ids
# bought and sold through the swap router, long-only. Lending, yield and
# other DeFi actions are reads (ctx.defi_yield), not venues.
SUPPORTED_VENUES: frozenset[str] = frozenset(
    {
        "hyperliquid",
        "hyperliquid_spot",
        "polymarket",
        "hyperliquid_prediction",
        "onchain",
    }
)
# Spot venues hold tokens: long-only, no limit orders. `onchain` symbols are
# token ids on any chain; `hyperliquid_spot` symbols are pairs like HYPE/USDC.
SPOT_VENUES: frozenset[str] = frozenset({"onchain", "hyperliquid_spot"})
OPEN_KINDS: frozenset[str] = frozenset({"market", "limit", "buy"})
CLOSE_KINDS: frozenset[str] = frozenset({"close", "sell", "redeem"})
ACTION_KINDS: frozenset[str] = OPEN_KINDS | CLOSE_KINDS


@dataclass(frozen=True)
class FreestyleSpec:
    """Optional module-level ``SPEC`` an author declares. Everything is a
    ceiling the runtime enforces before an action reaches a venue."""

    venues: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    max_notional_per_tick: float | None = None
    max_loss_usd: float | None = None
    halt_when: dict[str, float] = field(default_factory=dict)
    quote_interval: str = "5m"
    custom_risk_acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_any(cls, value: Any) -> FreestyleSpec:
        match value:
            case FreestyleSpec():
                return value
            case Mapping():
                data = dict(value)
                return cls(
                    venues=tuple(str(v) for v in data.get("venues") or ()),
                    symbols=tuple(str(s) for s in data.get("symbols") or ()),
                    max_notional_per_tick=_float_or_none(
                        data.get("max_notional_per_tick")
                    ),
                    max_loss_usd=_float_or_none(data.get("max_loss_usd")),
                    halt_when={
                        str(k): float(v)
                        for k, v in (data.get("halt_when") or {}).items()
                    },
                    quote_interval=str(data.get("quote_interval") or "5m"),
                    custom_risk_acknowledged=bool(data.get("custom_risk_acknowledged")),
                )
            case None:
                return cls()
            case _:
                raise TypeError(
                    f"SPEC must be a FreestyleSpec or mapping, got {type(value)}"
                )


@dataclass
class ActionResult:
    status: str  # filled | resting | rejected | refused | skipped
    reason: str | None = None
    intent: dict[str, Any] | None = None
    fill: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"filled", "resting"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "intent": self.intent,
            "fill": self.fill,
        }


def normalize_action(
    action: Mapping[str, Any], *, position_side: str | None = None
) -> OrderIntent:
    """Turn the author's action dict into an engine ``OrderIntent``.

    ``kind`` market/limit/buy open a position; close/sell/redeem reduce one.
    Sides are ``long``/``short`` for perps and ``buy`` for outcome tokens; a
    close takes the opposite side of the held position so paper slippage
    runs against the exit like a real fill.
    """
    data = dict(action)
    venue = str(data.get("venue") or "").strip().lower()
    kind = str(data.get("kind") or "market").strip().lower()
    symbol = str(data.get("symbol") or "").strip()
    if not venue:
        raise ValueError("action needs a venue")
    if venue not in SUPPORTED_VENUES:
        raise ValueError(
            f"venue {venue!r} is not supported by the freestyle runtime "
            f"(supported: {sorted(SUPPORTED_VENUES)}); lending and yield actions "
            "are reads, not venues"
        )
    if kind not in ACTION_KINDS:
        raise ValueError(
            f"action kind must be one of {sorted(ACTION_KINDS)}, got {kind!r}"
        )
    if not symbol:
        raise ValueError("action needs a symbol")
    metadata: dict[str, Any] = {
        "freestyle_kind": kind,
        "tag": data.get("tag"),
    }
    if data.get("max_loss") is not None:
        metadata["max_loss"] = float(data["max_loss"])
    if kind in CLOSE_KINDS:
        if position_side is None:
            raise ValueError(f"no open position in {symbol} to close")
        exit_side = "sell" if position_side == "long" else "buy"
        metadata["exit_reason"] = str(data.get("reason") or f"freestyle_{kind}")
        return OrderIntent(
            action="CLOSE",
            venue=venue,
            symbol=symbol,
            side=exit_side,
            size=_float_or_none(data.get("size")),
            notional=_float_or_none(data.get("notional")),
            reduce_only=True,
            client_order_id=str(
                data.get("client_order_id") or f"fs-{uuid.uuid4().hex[:8]}"
            ),
            metadata=metadata,
        )
    side = str(data.get("side") or ("buy" if venue == "polymarket" else "long")).lower()
    if side in {"buy"}:
        side = "long"
    if side in {"sell"}:
        side = "short"
    if side not in {"long", "short"}:
        raise ValueError(
            f"side must be long/short (or buy/sell), got {data.get('side')!r}"
        )
    if venue in SPOT_VENUES:
        if side == "short":
            raise ValueError(
                f"{venue} is spot: it holds tokens and cannot short; "
                "'sell' closes what you hold"
            )
        if kind == "limit":
            raise ValueError(
                f"{venue} swaps fill at market; limit orders are not supported"
            )
    size = _float_or_none(data.get("size"))
    notional = _float_or_none(data.get("notional"))
    if (size is None or size <= 0) and (notional is None or notional <= 0):
        raise ValueError("an opening action needs a positive size or notional")
    limit_price = _float_or_none(data.get("limit_price")) if kind == "limit" else None
    if kind == "limit" and limit_price is None:
        raise ValueError("a limit action needs limit_price")
    return OrderIntent(
        action="OPEN",
        venue=venue,
        symbol=symbol,
        side=side,
        size=size,
        notional=notional,
        reduce_only=False,
        client_order_id=str(
            data.get("client_order_id") or f"fs-{uuid.uuid4().hex[:8]}"
        ),
        limit_price=limit_price,
        metadata=metadata,
    )


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
