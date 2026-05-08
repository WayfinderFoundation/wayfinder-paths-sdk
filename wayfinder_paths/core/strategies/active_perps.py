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

import hashlib
import importlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, Final, final

import pandas as pd

from wayfinder_paths.core.backtesting.ref import BacktestRef, load_ref
from wayfinder_paths.core.perps.context import (
    SignalFrame,
    TriggerContext,
    normalize_signal,
)
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
        from wayfinder_paths.core.perps.handlers.recording import (
            RecordingHandler,  # noqa: PLC0415
        )

        raw_perp, raw_hip3 = await self._build_handlers()
        # Wrap each handler so every place_order is teed into an intents log.
        # The reconciler replays decide() against snapshotted positions and then
        # diffs replay-intents vs these recorded live-intents (strict axis) — and
        # also vs HL fills (execution-path axis).
        perp = RecordingHandler(raw_perp)
        hip3 = {k: RecordingHandler(h) for k, h in raw_hip3.items()}

        prices, funding = await self._fetch_recent_data(raw_perp)
        raw_signal = self._signal_fn(prices, funding, dict(self._ref.params))
        signal_frame = normalize_signal(
            raw_signal, fallback_columns=self._ref.data.symbols
        )

        trigger_t = perp.now()
        ctx = TriggerContext(
            perp=perp,
            hip3=hip3,
            params=dict(self._ref.params),
            state=self._state,
            signal=signal_frame,
            t=trigger_t,
        )
        await self._decide_fn(ctx)

        # Capture per-venue positions and intents into state so the reconciler
        # has the full input + output of this trigger.
        positions_snapshot: dict[str, dict[str, dict[str, float]]] = {}
        intents_snapshot: dict[str, list[dict[str, Any]]] = {}
        mids_snapshot: dict[str, dict[str, float]] = {}
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
            intents_snapshot[venue_key] = list(handler.intents)
            mids_snapshot[venue_key] = {
                sym: handler.mid(sym) for sym in self._ref.data.symbols
            }

        # Snapshot the signal row live decide() saw. Let exceptions surface —
        # a silently-empty signal_row in monitoring is worse than a hard fail.
        signal_row = signal_frame.at(trigger_t)
        signal_row_serialised = {
            str(k): (float(v) if pd.notna(v) else None) for k, v in signal_row.items()
        }

        # Hash the params live used at this trigger so reconcile can detect
        # config drift between deploys (e.g. someone bumped target_leverage).
        params_for_hash = json.dumps(
            dict(self._ref.params), sort_keys=True, default=str
        )
        params_hash = hashlib.sha256(params_for_hash.encode()).hexdigest()[:16]

        self._state.update(
            {
                "positions": positions_snapshot,
                "orders": intents_snapshot,
                "mids": mids_snapshot,
                "signal_row": signal_row_serialised,
                "trigger_ts": trigger_t.isoformat(),
                "nav": self._state.get("nav"),
                "params_hash": params_hash,
            }
        )
        self._state.write_snapshot(trigger_t)
        warn = self._oldest_snapshot_warning()
        msg = f"trigger ran ({sum(len(v) for v in intents_snapshot.values())} intents)"
        if warn:
            msg += f" — {warn}"
        return True, msg

    # ---------- overridable (defaults handle common HL case) ----------
    # Default lifecycle assumes USDC on Arbitrum bridged to/from Hyperliquid.
    # Strategies with non-standard funding flows (other chains, multi-asset
    # collateral, custom margin allocation) should override these.

    DEFAULT_USDC_TOKEN_ID: ClassVar[str] = "usd-coin-arbitrum"
    DEFAULT_GAS_TOKEN_ID: ClassVar[str] = "ethereum-arbitrum"
    DEFAULT_MIN_DEPOSIT_USDC: ClassVar[float] = 5.0  # HL minimum is $5
    DEFAULT_MAX_GAS_ETH: ClassVar[float] = 0.05
    DEFAULT_HL_DEPOSIT_TIMEOUT_S: ClassVar[int] = 180

    async def _build_balance_adapter(self) -> Any:
        """Construct a BalanceAdapter wired to (main, strategy) wallets."""
        from wayfinder_paths.adapters.balance_adapter.adapter import (
            BalanceAdapter,  # noqa: PLC0415
        )
        from wayfinder_paths.mcp.scripting import get_adapter  # noqa: PLC0415

        return await get_adapter(BalanceAdapter, "main", self._strategy_name())

    async def _build_hl_adapter(self) -> Any:
        from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
            HyperliquidAdapter,  # noqa: PLC0415
        )
        from wayfinder_paths.mcp.scripting import get_adapter  # noqa: PLC0415

        return await get_adapter(HyperliquidAdapter, self._strategy_name())

    async def deposit(self, **kwargs: Any) -> StatusTuple:
        """Default: main → strategy wallet (USDC + optional gas) → bridge to HL.

        Args (via kwargs):
            main_token_amount: USDC to deposit.
            gas_token_amount: ETH-on-Arbitrum to send to strategy wallet for tx fees
                (recommend 0.001 on first deposit; 0 on subsequent).
        """
        main_amt = float(kwargs.get("main_token_amount") or 0)
        gas_amt = float(kwargs.get("gas_token_amount") or 0)
        if main_amt < self.DEFAULT_MIN_DEPOSIT_USDC:
            return False, f"Minimum deposit is {self.DEFAULT_MIN_DEPOSIT_USDC} USDC"
        if gas_amt > self.DEFAULT_MAX_GAS_ETH:
            return False, f"Gas amount exceeds maximum {self.DEFAULT_MAX_GAS_ETH} ETH"

        balance = await self._build_balance_adapter()
        strat_addr = self._get_strategy_wallet_address()
        main_addr = self._get_main_wallet_address()
        same_wallet = main_addr.lower() == strat_addr.lower()

        # 1) Optional gas transfer.
        if gas_amt > 0 and not same_wallet:
            ok, msg = await balance.move_from_main_wallet_to_strategy_wallet(
                token_id=self.DEFAULT_GAS_TOKEN_ID,
                amount=gas_amt,
                strategy_name=self._strategy_name(),
            )
            if not ok:
                return False, f"Gas transfer failed: {msg}"

        # 2) USDC transfer (skip if main and strategy share an address).
        if not same_wallet:
            ok, msg = await balance.move_from_main_wallet_to_strategy_wallet(
                token_id=self.DEFAULT_USDC_TOKEN_ID,
                amount=main_amt,
                strategy_name=self._strategy_name(),
            )
            if not ok:
                return False, f"USDC transfer failed: {msg}"

        # 3) Bridge to Hyperliquid.
        from wayfinder_paths.core.constants.contracts import (
            HYPERLIQUID_BRIDGE,  # noqa: PLC0415
        )

        usdc_decimals = 6
        usdc_raw = int(main_amt * (10**usdc_decimals))
        strategy_wallet = self.config.get("strategy_wallet")
        ok, tx = await balance.send_to_address(
            token_id=self.DEFAULT_USDC_TOKEN_ID,
            amount=usdc_raw,
            from_wallet=strategy_wallet,
            to_address=HYPERLIQUID_BRIDGE,
            signing_callback=self.strategy_wallet_signing_callback,
        )
        if not ok:
            return False, f"HL bridge tx failed: {tx}"

        # 4) Wait for HL to credit the deposit.
        hl = await self._build_hl_adapter()
        ok, _ = await hl.wait_for_deposit(
            address=strat_addr,
            expected_increase=main_amt,
            timeout_s=self.DEFAULT_HL_DEPOSIT_TIMEOUT_S,
            poll_interval_s=10,
        )
        if not ok:
            return True, (
                f"Deposit tx sent ({tx}); HL credit not yet visible — call status() to verify."
            )
        return True, f"Deposited {main_amt} USDC to HL (tx {tx})"

    async def withdraw(self, **kwargs: Any) -> StatusTuple:
        """Default: close all open positions (reduce-only) across declared venues,
        then withdraw USDC from HL → strategy wallet on Arbitrum.

        Funds are left on the strategy wallet; call `exit()` to move to main wallet.
        """
        perp, hip3 = await self._build_handlers()

        # 1) Close all open positions on every venue.
        closed = 0
        errors: list[str] = []
        for venue_key, handler in [
            ("perp", perp),
            *((f"hip3:{k}", h) for k, h in hip3.items()),
        ]:
            try:
                positions = await handler.get_positions()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{venue_key} get_positions: {e}")
                continue
            for sym, pos in positions.items():
                if pos.size == 0:
                    continue
                side = "sell" if pos.size > 0 else "buy"
                result = await handler.place_order(
                    sym,
                    side,
                    abs(pos.size),
                    "market",
                    reduce_only=True,
                )
                if result.ok:
                    closed += 1
                else:
                    errors.append(f"{venue_key}/{sym}: {result.error}")

        # 2) Withdraw all USDC margin from HL → strategy wallet on Arbitrum.
        hl = await self._build_hl_adapter()
        strat_addr = self._get_strategy_wallet_address()
        ok, state = await hl.get_user_state(strat_addr)
        margin = 0.0
        if ok and isinstance(state, dict):
            margin = hl.get_perp_margin_amount(state)
        if margin > 0:
            ok, raw = await hl.withdraw(amount=margin, address=strat_addr)
            if not ok:
                errors.append(f"HL withdraw: {raw}")

        msg = f"Closed {closed} position(s); withdrew {margin:.2f} USDC from HL to strategy wallet"
        if errors:
            return False, f"{msg}; errors: {'; '.join(errors[:5])}"
        return True, msg

    async def exit(self, **kwargs: Any) -> StatusTuple:
        """Default: transfer all USDC from strategy wallet → main wallet on Arbitrum."""
        balance = await self._build_balance_adapter()
        strat_addr = self._get_strategy_wallet_address()
        main_addr = self._get_main_wallet_address()
        if main_addr.lower() == strat_addr.lower():
            return (
                True,
                "Main and strategy wallets are the same address — nothing to transfer",
            )

        ok, raw = await balance.get_balance(
            wallet_address=strat_addr,
            token_id=self.DEFAULT_USDC_TOKEN_ID,
        )
        if not ok:
            return False, f"Failed to read strategy USDC balance: {raw}"
        usdc_decimals = 6
        amount = float(raw) / (10**usdc_decimals)
        if amount <= 0:
            return True, "No USDC on strategy wallet to transfer"

        ok, msg = await balance.move_from_strategy_wallet_to_main_wallet(
            token_id=self.DEFAULT_USDC_TOKEN_ID,
            amount=amount,
            strategy_name=self._strategy_name(),
        )
        if not ok:
            return False, f"USDC transfer failed: {msg}"
        return True, f"Transferred {amount:.2f} USDC to main wallet"

    async def reconcile(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        no_fills: bool = False,
        write_report: bool = True,
    ) -> dict[str, Any]:
        """Replay decide() over the recorded live snapshots and diff against the
        captured live intents + (optionally) HL fills.

        Returns a structured report (see `core/perps/reconciler.py`). When
        `write_report` is true, also writes a JSON file to
        `<strategy_dir>/reconciliation/<run_ts>.json`.

        Args:
            start: ISO date — defaults to 30 days ago.
            end: ISO date — defaults to today.
            no_fills: skip the live HL fills fetch (offline replay only).
            write_report: persist report to disk.
        """
        from wayfinder_paths.core.perps.reconciler import (
            reconcile_strategy,  # noqa: PLC0415
        )

        return await reconcile_strategy(
            strategy_dir=Path(self.REF).parent,
            strategy_name=self._strategy_name(),
            start=start,
            end=end,
            no_fills=no_fills,
            write_report=write_report,
        )

    async def _status(self) -> StatusDict:
        risk_warning = ""
        if self._risk_limits is None:
            risk_warning = (
                f"No risk_limits.json found in {Path(self.REF).parent} — strategy will run "
                "without drawdown / exposure / loss caps. Add a risk_limits.json next to "
                "backtest_ref.json to enable opt-in halts."
            )
            self.logger.warning(risk_warning)

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
                "risk_warning": risk_warning,
            },
            "gas_available": 0.0,
            "gassed_up": False,
        }

    async def quote(self, **kwargs: Any) -> dict[str, Any]:
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
