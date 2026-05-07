"""ActivePerpsStrategy — parent class for trigger-pattern perp strategies.

Subclasses declare four ClassVars and the parent wires backtest, live, and
reconcile. The strategy author writes `signal.py` (pure, vectorized) and
optionally `decide.py` (per-bar). All other lifecycle methods have sensible
defaults the subclass can override.

```python
class MyStrategy(ActivePerpsStrategy):
    REF = REF_PATH
    SIGNAL = "my_pkg.signal:compute_signal"
    DECIDE = "my_pkg.decide:decide"
    HIP3_DEXES = []
```
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, Final, final

from wayfinder_paths.core.backtesting.ref import BacktestRef, load_ref
from wayfinder_paths.core.perps.context import SignalFrame, TriggerContext
from wayfinder_paths.core.perps.handlers.protocol import MarketHandler
from wayfinder_paths.core.perps.state import SNAPSHOT_AGE_WARN_DAYS, StateStore
from wayfinder_paths.core.strategies.risk_limits import RiskLimits
from wayfinder_paths.core.strategies.Strategy import (
    StatusDict,
    StatusTuple,
    Strategy,
)

KNOWN_HIP3_DEXES: Final[set[str]] = {"xyz", "flx", "vntl", "hyna", "km"}

LOCKED_METHODS: Final[tuple[str, ...]] = ("update", "_run_trigger")


def _import_dotted(spec: str) -> Callable[..., Any]:
    """Import 'package.module:attr' or 'package.module.attr'."""
    if ":" in spec:
        module, attr = spec.split(":", 1)
    else:
        module, _, attr = spec.rpartition(".")
    if not module or not attr:
        raise ImportError(f"Invalid dotted spec {spec!r}")
    return getattr(importlib.import_module(module), attr)


class ActivePerpsStrategy(Strategy):
    """Trigger-pattern perp strategy. Subclasses are 5-line declarations."""

    # ---------- subclass-declared (required) ----------
    REF: ClassVar[Path | str]
    SIGNAL: ClassVar[str]  # "module:fn" or "module.fn"
    DECIDE: ClassVar[str | None] = None  # None ⇒ default_decide
    HIP3_DEXES: ClassVar[list[str]] = []

    # ---------- subclass shouldn't touch ----------
    _ref: BacktestRef
    _signal_fn: Callable[..., SignalFrame]
    _decide_fn: Callable[[TriggerContext], Awaitable[None]]
    _state: StateStore
    _risk_limits: RiskLimits | None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Allow abstract intermediate classes by skipping if REF isn't declared yet.
        if not hasattr(cls, "REF") or cls.REF is None:
            return

        # Validate ref is loadable.
        ref_path = Path(cls.REF) if isinstance(cls.REF, (str, Path)) else None
        if ref_path is None or not ref_path.exists():
            raise RuntimeError(
                f"{cls.__name__}.REF must point to an existing file: {cls.REF!r}"
            )

        # Validate SIGNAL importable, DECIDE if set.
        if not getattr(cls, "SIGNAL", None):
            raise RuntimeError(f"{cls.__name__}.SIGNAL is required")
        try:
            _import_dotted(cls.SIGNAL)
        except (ImportError, AttributeError) as e:
            raise RuntimeError(
                f"{cls.__name__}.SIGNAL = {cls.SIGNAL!r} not importable: {e}"
            ) from e
        if cls.DECIDE:
            try:
                _import_dotted(cls.DECIDE)
            except (ImportError, AttributeError) as e:
                raise RuntimeError(
                    f"{cls.__name__}.DECIDE = {cls.DECIDE!r} not importable: {e}"
                ) from e

        # Forbid override of @final methods.
        for name in LOCKED_METHODS:
            sub = cls.__dict__.get(name)
            base = getattr(ActivePerpsStrategy, name, None)
            if sub is not None and sub is not base:
                raise TypeError(
                    f"{cls.__name__} overrides locked method {name!r}; "
                    f"customise via signal/decide instead."
                )

        # Validate HIP3_DEXES.
        for dex in cls.HIP3_DEXES:
            if dex not in KNOWN_HIP3_DEXES:
                raise RuntimeError(
                    f"{cls.__name__}.HIP3_DEXES has unknown dex {dex!r}; "
                    f"known: {sorted(KNOWN_HIP3_DEXES)}"
                )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ref = load_ref(Path(self.REF).parent)
        self._signal_fn = _import_dotted(self.SIGNAL)
        if self.DECIDE:
            self._decide_fn = _import_dotted(self.DECIDE)
        else:
            from wayfinder_paths.core.backtesting.perps import default_decide

            self._decide_fn = default_decide
        self._state = StateStore(self._strategy_name(), "live")
        self._risk_limits = RiskLimits.load_optional(Path(self.REF).parent)

    def _strategy_name(self) -> str:
        return self.name or self.__class__.__name__

    # ---------- locked lifecycle ----------
    @final
    async def update(self) -> StatusTuple:
        await self._check_path_version()
        if self._risk_limits is not None:
            snap = await self._risk_snapshot()
            halt = self._risk_limits.check(snap)
            if halt:
                return False, f"Halted: {halt}"
        return await self._run_trigger()

    @final
    async def _run_trigger(self) -> StatusTuple:
        perp, hip3 = await self._build_handlers()
        prices, funding = await self._fetch_recent_data(perp)
        signal_frame = self._signal_fn(prices, funding, dict(self._ref.params))
        ctx = TriggerContext(
            perp=perp,
            hip3=hip3,
            params=dict(self._ref.params),
            state=self._state,
            signal=signal_frame,
            t=perp.now(),
        )
        await self._decide_fn(ctx)
        # Capture per-venue positions into state so the reconciler can replay
        # decide() with the same positions live had at this bar.
        positions_snapshot: dict[str, dict[str, dict[str, float]]] = {}
        for venue_key, handler in [
            ("perp", perp),
            *((f"hip3:{k}", h) for k, h in hip3.items()),
        ]:
            pos = await handler.get_positions()
            positions_snapshot[venue_key] = {
                sym: {
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "mark_price": p.mark_price,
                }
                for sym, p in pos.items()
            }
        self._state.set("positions", positions_snapshot)
        self._state.write_snapshot(ctx.t)
        warn = self._oldest_snapshot_warning()
        msg = "trigger ran"
        if warn:
            msg += f" — {warn}"
        return True, msg

    # ---------- overridable (defaults handle common HL case) ----------
    async def deposit(self, **kwargs: Any) -> StatusTuple:  # noqa: D401
        """Default: bridge USDC to HL primary perp (override for per-strategy flow)."""
        return False, (
            "Default deposit not implemented. Override `deposit(...)` in your subclass "
            "to handle main → strategy wallet → HL bridge."
        )

    async def withdraw(self, **kwargs: Any) -> StatusTuple:
        """Default: close all positions across declared venues, withdraw USDC from HL."""
        return False, (
            "Default withdraw not implemented. Override `withdraw(...)` in your subclass."
        )

    async def exit(self, **kwargs: Any) -> StatusTuple:
        return False, (
            "Default exit not implemented. Override `exit(...)` to transfer USDC from "
            "strategy wallet to main wallet."
        )

    async def _status(self) -> StatusDict:
        return {
            "portfolio_value": 0.0,
            "net_deposit": 0.0,
            "strategy_status": {
                "ref_hash": self._ref.produced.ref_hash,
                "venues": {
                    "perp": self._ref.venues.perp,
                    "hip3": self._ref.venues.hip3,
                },
                "last_state": self._state.snapshot(),
                "snapshot_warning": self._oldest_snapshot_warning() or "",
            },
            "gas_available": 0.0,
            "gassed_up": False,
        }

    async def quote(self) -> dict[str, Any]:
        perf = self._ref.performance
        return {
            "expected_apy": float(perf.get("apy", perf.get("annualized_return", 0.0))),
            "apy_type": str(perf.get("apy_type", "blended")),
            "as_of": self._ref.produced.at,
            "summary": (
                f"Backtested Sharpe {perf.get('sharpe', '?')} / "
                f"return {perf.get('total_return', '?')} / "
                f"DD {perf.get('max_drawdown', '?')} (ref hash "
                f"{self._ref.produced.ref_hash[:12]})"
            ),
        }

    @staticmethod
    async def policies() -> list[str]:
        return ["hyperliquid_active_perps"]

    # ---------- hooks for subclasses to override ----------
    async def _build_handlers(self) -> tuple[MarketHandler, dict[str, MarketHandler]]:
        """Construct fresh handlers per `update()`.

        Default: one `HyperliquidAdapter` keyed off the strategy wallet
        (looked up by strategy name), wrapped in a `LiveHandler` for the primary
        perp venue and one per declared HIP-3 dex. Delta Lab is wired as the
        history client so `recent_prices` / `recent_funding` work out of the box.

        **Override this when you need:**
          - a different wallet (e.g. shared with main, or a non-default label)
          - multiple adapters (e.g. perp + spot, or perp + CEX)
          - custom builder fee / dex abstraction setup before the handler is built
          - a non-Hyperliquid venue (the protocol is generic — only the default
            assumes HL)
        """
        from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
            HyperliquidAdapter,  # noqa: PLC0415
        )
        from wayfinder_paths.core.clients.DeltaLabClient import (
            DELTA_LAB_CLIENT,  # noqa: PLC0415
        )
        from wayfinder_paths.core.perps.handlers.live import (
            LiveHandler,  # noqa: PLC0415
        )
        from wayfinder_paths.mcp.scripting import get_adapter  # noqa: PLC0415

        adapter = await get_adapter(HyperliquidAdapter, self._strategy_name())
        addr = adapter.wallet_address

        perp = LiveHandler(
            adapter=adapter,
            wallet_address=addr,
            venue="perp",
            delta_lab_client=DELTA_LAB_CLIENT,
        )
        hip3 = {
            dex: LiveHandler(
                adapter=adapter,
                wallet_address=addr,
                venue=f"hip3:{dex}",
                dex=dex,
                delta_lab_client=DELTA_LAB_CLIENT,
            )
            for dex in self.HIP3_DEXES
        }
        # Pre-fetch mids so handlers can answer `mid()` synchronously during decide().
        await perp.refresh_mids()
        for h in hip3.values():
            await h.refresh_mids()
        return perp, hip3

    async def _fetch_recent_data(self, perp: MarketHandler) -> tuple[Any, Any]:
        """Pull recent prices + funding for the signal window."""
        lookback = int(self._ref.params.get("signal_lookback_bars", 256))
        symbols = self._ref.data.symbols
        prices = await perp.recent_prices(symbols, lookback)
        funding = await perp.recent_funding(symbols, lookback)
        return prices, funding

    async def _risk_snapshot(self) -> dict[str, Any]:
        """Build the snapshot dict that `RiskLimits.check` consumes. Subclasses
        override to plug in real exposure/PnL numbers."""
        return {}

    async def _check_path_version(self) -> None:
        """Compare installed path version vs `REF.produced.git_sha`. Default: no-op
        until the path-manifest plumbing lands; subclasses can opt in."""
        return None

    # ---------- internals ----------
    def _oldest_snapshot_warning(self) -> str | None:
        age = StateStore.oldest_snapshot_age_days(self._strategy_name())
        if age is None or age <= SNAPSHOT_AGE_WARN_DAYS:
            return None
        return (
            f"oldest state snapshot is {age:.0f} days old — back up "
            f".wayfinder/state/{self._strategy_name()}/ before pruning"
        )
