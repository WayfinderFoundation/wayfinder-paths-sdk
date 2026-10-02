"""Fill-driven two-venue participation, independent of LLM/strategy cadence.

Ports normalize venue truth. Missing orders are unknown, never inferred rejected.
The coordinator owns only its durable order IDs and dedicated account positions.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import Field, FiniteFloat

from wayfinder_paths.core.clients.participation_execution import digest, state_lock
from wayfinder_paths.jobs.participation import Record
from wayfinder_paths.runner.monitor_state import atomic_write_json

if TYPE_CHECKING:
    from wayfinder_paths.jobs.store import JobStore

Leg = Literal["primary", "hedge"]
LEGS: tuple[Leg, Leg] = ("primary", "hedge")


class HedgeLimits(Record):
    symbols: list[Literal["BTC", "ETH"]] = Field(min_length=1, max_length=2)
    max_gross_notional: FiniteFloat = Field(gt=0)
    max_collateral: FiniteFloat = Field(gt=0)
    max_leverage: FiniteFloat = Field(gt=0, le=3)
    max_loss: FiniteFloat = Field(gt=0)
    max_cost: FiniteFloat = Field(gt=0)
    max_slippage_bps: FiniteFloat = Field(gt=0, le=100)
    max_unhedged_usd: FiniteFloat = Field(gt=0)
    max_unhedged_seconds: int = Field(default=30, ge=10, le=60)
    entry_ttl_seconds: int = Field(default=30, ge=5, le=120)
    max_hold_seconds: int = Field(default=3600, ge=30, le=86400)
    quote_max_age_seconds: int = Field(default=10, ge=1, le=30)
    authorization_expires_at: FiniteFloat = Field(gt=0)


class PairRequest(Record):
    symbol: Literal["BTC", "ETH"]
    side: Literal["long", "short"]
    quantity: FiniteFloat = Field(gt=0)
    limit_price: FiniteFloat = Field(gt=0)
    stop_loss_pct: FiniteFloat = Field(gt=0, le=0.1)
    hold_seconds: int = Field(ge=30, le=86400)


class HedgeOrder(Record):
    status: Literal["open", "filled", "cancelled", "rejected", "unknown"]
    filled_quantity: FiniteFloat = Field(default=0, ge=0)


class HedgeSnapshot(Record):
    account: str
    symbol: str
    observed_at: FiniteFloat
    quantity: FiniteFloat  # signed base units, not notional
    mark: FiniteFloat = Field(gt=0)
    equity: FiniteFloat
    collateral: FiniteFloat = Field(ge=0)
    free_collateral: FiniteFloat = Field(ge=0)
    leverage: FiniteFloat = Field(ge=0)
    # Cumulative account charges; dedicated accounts, no unrelated activity.
    fees_paid: FiniteFloat = Field(ge=0)
    funding_paid: FiniteFloat = Field(ge=0)
    funding_received: FiniteFloat = Field(ge=0)
    orders: dict[str, HedgeOrder] = Field(default_factory=dict)
    # Confirmed reduce-only stop client ID -> protected base quantity.
    stops: dict[str, FiniteFloat] = Field(default_factory=dict)
    # Venue ledger has finalized fees/funding through the close, not merely flat.
    settlement_complete: bool = False
    unmanaged: bool = False


class HedgeCommand(Record):
    id: str
    leg: Literal["primary", "hedge"]
    kind: Literal["entry", "hedge", "close", "stop", "cancel"]
    quantity: FiniteFloat = 0  # signed, direction included
    price: FiniteFloat = 0
    target: str | None = None
    created_at: FiniteFloat


class HedgePort(Protocol):
    venue: str
    account: str

    async def snapshot(
        self, symbol: str, commands: list[HedgeCommand]
    ) -> HedgeSnapshot: ...

    async def execute(self, symbol: str, command: HedgeCommand) -> None: ...


class PairState(Record):
    identity: str
    operation_id: str
    request: PairRequest
    limits: HedgeLimits
    started_at: FiniteFloat
    initial_equity: FiniteFloat
    initial_cost: FiniteFloat
    status: Literal["entering", "active", "exiting", "settled"] = "entering"
    blocked: str | None = None
    commands: list[HedgeCommand] = Field(default_factory=list)
    unhedged_since: FiniteFloat | None = None
    last_heartbeat: FiniteFloat = 0
    cost: FiniteFloat = Field(default=0, ge=0)


class HedgeCoordinator:
    def __init__(self, directory: Path, primary: HedgePort, hedge: HedgePort) -> None:
        self.directory, self.primary, self.hedge = directory, primary, hedge
        self.path = directory / "pair.json"
        self.identity = digest([(p.venue, p.account) for p in (primary, hedge)])

    def load(self) -> PairState:
        state = PairState.model_validate_json(self.path.read_text())
        if state.identity != self.identity:
            raise ValueError("hedge venue/account identity changed")
        return state

    def save(self, state: PairState) -> None:
        atomic_write_json(self.path, state.model_dump(mode="json"))

    async def snapshots(self, state: PairState, now: float) -> dict[Leg, HedgeSnapshot]:
        result: dict[Leg, HedgeSnapshot] = {}
        started = time.monotonic()
        for name in LEGS:
            port = self.primary if name == "primary" else self.hedge
            snap = await port.snapshot(
                state.request.symbol, [c for c in state.commands if c.leg == name]
            )
            if snap.account != port.account or snap.symbol != state.request.symbol:
                raise ValueError("hedge snapshot identity mismatch")
            if (
                not -2
                <= now + time.monotonic() - started - snap.observed_at
                <= state.limits.quote_max_age_seconds
            ):
                raise ValueError("stale hedge snapshot")
            if snap.unmanaged:
                raise ValueError("unmanaged orders/positions on dedicated account")
            result[name] = snap
        if any(
            now + time.monotonic() - started - s.observed_at
            > state.limits.quote_max_age_seconds
            for s in result.values()
        ):
            raise ValueError("stale hedge snapshot")
        return result

    async def start(
        self,
        operation_id: str,
        request: PairRequest,
        limits: HedgeLimits,
        *,
        now: float | None = None,
    ) -> PairState:
        now = time.time() if now is None else now
        with state_lock(self.directory):
            if self.path.exists():
                old = self.load()
                if old.operation_id == operation_id:
                    if old.request != request or old.limits != limits:
                        raise ValueError("pair operation parameters changed")
                    return old
                if old.status != "settled" or old.blocked:
                    raise ValueError("another pair is still unresolved")
            if (
                request.symbol not in limits.symbols
                or request.hold_seconds > limits.max_hold_seconds
            ):
                raise ValueError("request exceeds approved market/holding limits")
            if now + request.hold_seconds + 120 >= limits.authorization_expires_at:
                raise ValueError("authorization expires before safe close horizon")
            state = PairState(
                identity=self.identity,
                operation_id=operation_id,
                request=request,
                limits=limits,
                started_at=now,
                initial_equity=0,
                initial_cost=0,
            )
            snaps = await self.snapshots(state, now)
            if (
                abs(request.limit_price / snaps["primary"].mark - 1) * 10000
                > limits.max_slippage_bps
            ):
                raise ValueError("entry price exceeds approved slippage bound")
            if any(
                s.quantity or s.orders or s.stops or not s.settlement_complete
                for s in snaps.values()
            ):
                raise ValueError("new pair requires flat dedicated accounts")
            gross = request.quantity * sum(s.mark for s in snaps.values())
            collateral = sum(
                request.quantity * s.mark / limits.max_leverage for s in snaps.values()
            )
            if gross > limits.max_gross_notional or collateral > limits.max_collateral:
                raise ValueError("pair exceeds capital limits")
            for snap in snaps.values():
                if (
                    snap.leverage > limits.max_leverage
                    or request.quantity * snap.mark / limits.max_leverage
                    > snap.free_collateral
                ):
                    raise ValueError(
                        "insufficient collateral or unsafe account leverage"
                    )
            state.initial_equity = sum(s.equity for s in snaps.values())
            state.initial_cost = sum(
                s.fees_paid + s.funding_paid for s in snaps.values()
            )
            self.save(state)
            quantity = request.quantity * (1 if request.side == "long" else -1)
            await self.command(
                state, "primary", "entry", quantity, request.limit_price, now=now
            )
            return state

    async def command(
        self,
        state: PairState,
        leg: Literal["primary", "hedge"],
        kind: Literal["entry", "hedge", "close", "stop", "cancel"],
        quantity: float = 0,
        price: float = 0,
        *,
        now: float,
        target: str | None = None,
    ) -> None:
        command = HedgeCommand(
            id=digest([state.operation_id, len(state.commands), leg, kind])[:32],
            leg=leg,
            kind=kind,
            quantity=quantity,
            price=price,
            target=target,
            created_at=now,
        )
        state.commands.append(command)
        self.save(state)  # write-ahead: never retry this command on an exception
        try:
            await (self.primary if leg == "primary" else self.hedge).execute(
                state.request.symbol, command
            )
        except Exception as exc:
            state.blocked = (
                f"{leg} {kind} requires reconciliation ({type(exc).__name__})"
            )
            self.save(state)

    @staticmethod
    def pending(
        state: PairState, snaps: dict[Leg, HedgeSnapshot], *, leg: Leg, kinds: set[str]
    ) -> bool:
        for cmd in state.commands:
            if cmd.leg != leg or cmd.kind not in kinds:
                continue
            order = snaps[leg].orders.get(cmd.id)
            if order is None or order.status in {"open", "unknown"}:
                return True
        return False

    async def step(
        self, *, now: float | None = None, dry_run: bool = False
    ) -> PairState:
        now = time.time() if now is None else now
        with state_lock(self.directory):
            state = self.load()
            if state.status == "settled":
                return state
            try:
                snaps = await self.snapshots(state, now)
            except Exception as exc:
                if not dry_run:
                    state.blocked = f"risk state unavailable ({type(exc).__name__})"
                    self.save(state)
                return state
            if dry_run:
                return state
            state.last_heartbeat = now
            charges = (
                sum(s.fees_paid + s.funding_paid for s in snaps.values())
                - state.initial_cost
            )
            if charges < state.cost - 1e-8:
                state.blocked = "cumulative costs regressed"
            state.cost = max(state.cost, charges)
            if state.cost >= state.limits.max_cost:
                state.blocked = "pair cost budget exhausted"
            limits = state.limits
            gross = sum(abs(s.quantity) * s.mark for s in snaps.values())
            if (
                gross > limits.max_gross_notional
                or sum(s.collateral for s in snaps.values()) > limits.max_collateral
                or any(s.leverage > limits.max_leverage for s in snaps.values())
            ):
                state.blocked = "hedge capital limit breached"
            if (
                sum(s.equity for s in snaps.values()) - state.initial_equity
                <= -limits.max_loss
            ):
                state.blocked = "pair loss limit breached"
            # All future closes require fresh, account-scoped truth. No LLM needed.
            if state.blocked or now >= min(
                state.started_at + state.request.hold_seconds,
                limits.authorization_expires_at - 120,
            ):
                state.status = "exiting"
            entry = next((c for c in state.commands if c.kind == "entry"), None)
            if entry is None:
                state.blocked, state.status = (
                    "entry was interrupted before submission",
                    "exiting",
                )
            elif (
                now - state.started_at >= limits.entry_ttl_seconds
                or state.status == "exiting"
            ):
                order = snaps["primary"].orders.get(entry.id)
                if (
                    order
                    and order.status == "open"
                    and not any(
                        c.kind == "cancel" and c.target == entry.id
                        for c in state.commands
                    )
                ):
                    await self.command(
                        state, "primary", "cancel", now=now, target=entry.id
                    )

            p, h = snaps["primary"], snaps["hedge"]
            if state.status == "active" and (p.quantity == 0) != (h.quantity == 0):
                state.blocked, state.status = "orphan hedge leg", "exiting"
            for name, snap in snaps.items():
                if snap.quantity == 0:
                    continue
                expected_sign = (1 if state.request.side == "long" else -1) * (
                    1 if name == "primary" else -1
                )
                if (
                    snap.quantity * expected_sign < 0
                    or abs(snap.quantity) > state.request.quantity + 1e-9
                ):
                    state.blocked, state.status = (
                        "unexpected position direction/size",
                        "exiting",
                    )
                stops = [
                    c
                    for c in state.commands
                    if c.leg == name
                    and c.kind == "stop"
                    and abs(c.quantity) == abs(snap.quantity)
                ]
                protected = any(
                    snap.stops.get(c.id) == abs(snap.quantity) for c in stops
                )
                if not protected and state.status != "exiting":
                    if stops:
                        if now - stops[-1].created_at >= limits.max_unhedged_seconds:
                            state.blocked, state.status = (
                                "native stop not confirmed",
                                "exiting",
                            )
                    else:
                        trigger = snap.mark * (
                            1 - state.request.stop_loss_pct
                            if snap.quantity > 0
                            else 1 + state.request.stop_loss_pct
                        )
                        await self.command(
                            state, name, "stop", -snap.quantity, trigger, now=now
                        )
            delta = p.quantity + h.quantity
            if abs(delta) * max(p.mark, h.mark) > limits.max_unhedged_usd:
                state.unhedged_since = (
                    state.unhedged_since if state.unhedged_since is not None else now
                )
                if now - state.unhedged_since >= limits.max_unhedged_seconds:
                    state.blocked, state.status = (
                        "unhedged exposure timed out",
                        "exiting",
                    )
            else:
                state.unhedged_since = None
            if (
                state.status != "exiting"
                and abs(delta) > 1e-12
                and not self.pending(state, snaps, leg="hedge", kinds={"hedge"})
            ):
                # Hedge only the actual primary fill. Never flip a hedge on a stale
                # close or double an in-flight IOC whose acknowledgement was lost.
                if delta * (1 if state.request.side == "long" else -1) > 0:
                    bound = h.mark * (
                        1 + limits.max_slippage_bps / 10000 * (-1 if delta > 0 else 1)
                    )
                    await self.command(state, "hedge", "hedge", -delta, bound, now=now)
                else:
                    state.blocked, state.status = (
                        "hedge exceeds primary exposure",
                        "exiting",
                    )
            if state.blocked:
                state.status = "exiting"
            if state.status == "exiting":
                for name, snap in snaps.items():
                    if snap.quantity and not self.pending(
                        state, snaps, leg=name, kinds={"close"}
                    ):
                        bound = snap.mark * (
                            1
                            + limits.max_slippage_bps
                            / 10000
                            * (-1 if snap.quantity > 0 else 1)
                        )
                        await self.command(
                            state, name, "close", -snap.quantity, bound, now=now
                        )
            elif p.quantity and h.quantity and state.unhedged_since is None:
                state.status = "active"
            unresolved = any(
                self.pending(state, snaps, leg=name, kinds={"entry", "hedge", "close"})
                for name in LEGS
            )
            if not p.quantity and not h.quantity and not unresolved:
                # Cancel owned native stops before releasing a dedicated account.
                for name, snap in snaps.items():
                    for cid in snap.stops:
                        if any(c.id == cid for c in state.commands) and not any(
                            c.kind == "cancel" and c.target == cid
                            for c in state.commands
                        ):
                            await self.command(
                                state, name, "cancel", now=now, target=cid
                            )
                if (
                    not p.stops
                    and not h.stops
                    and p.settlement_complete
                    and h.settlement_complete
                ):
                    state.status = "settled"
            self.save(state)
            return state


async def guard_tick(
    coordinator: HedgeCoordinator, *, store: JobStore, job_id: str
) -> dict[str, Any]:
    """Independent runner entry point: never consult the strategy's pause flag.

    The deployment must schedule this separately before allowing entry. This
    function does not create a scheduler or clear an owner's risk latch.
    """
    from wayfinder_paths.jobs.halt import request_halt

    try:
        state = await coordinator.step()
    except BlockingIOError:
        return {"status": "busy"}
    except (OSError, ValueError) as exc:
        reason = f"participation protection state unavailable ({type(exc).__name__})"
        request_halt(store, job_id, reason=reason, source="activity_risk")
        return {"status": "blocked", "reason": reason}
    if state.blocked:
        request_halt(store, job_id, reason=state.blocked, source="activity_risk")
    return {
        "status": state.status,
        "reason": state.blocked,
        "cost_paid": state.cost,
        "heartbeat": state.last_heartbeat,
    }
