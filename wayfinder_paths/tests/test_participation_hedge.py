from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from wayfinder_paths.jobs.participation_hedge import (
    HedgeCommand,
    HedgeCoordinator,
    HedgeLimits,
    HedgeOrder,
    HedgeSnapshot,
    PairRequest,
    guard_tick,
)


class Venue:
    """Venue truth simulator: commands do not masquerade as confirmations."""

    def __init__(self, venue: str, account: str):
        self.venue, self.account = venue, account
        self.now = 1000
        self.quantity = 0.0
        self.equity = 1000.0
        self.fees = 0.0
        self.fail_reads = False
        self.timeout_kind: str | None = None
        self.commands: list[HedgeCommand] = []
        self.orders: dict[str, HedgeOrder] = {}
        self.stops: dict[str, float] = {}
        self.finalized = True

    async def snapshot(
        self, symbol: str, commands: list[HedgeCommand]
    ) -> HedgeSnapshot:
        if self.fail_reads:
            raise ValueError("venue unavailable")
        return HedgeSnapshot(
            account=self.account,
            symbol=symbol,
            observed_at=self.now,
            quantity=self.quantity,
            mark=100,
            equity=self.equity,
            collateral=abs(self.quantity) * 100 / 2,
            free_collateral=500,
            leverage=2,
            fees_paid=self.fees,
            funding_paid=0,
            funding_received=0,
            orders=self.orders.copy(),
            stops=self.stops.copy(),
            settlement_complete=self.finalized,
        )

    async def execute(self, symbol: str, command: HedgeCommand) -> None:
        self.commands.append(command)
        if self.timeout_kind == command.kind:
            raise TimeoutError("ack lost")
        if command.kind == "entry":
            self.orders[command.id] = HedgeOrder(status="open")
        elif command.kind in {"hedge", "close"}:
            self.quantity += command.quantity
            self.orders[command.id] = HedgeOrder(
                status="filled", filled_quantity=abs(command.quantity)
            )
        elif command.kind == "stop":
            self.stops[command.id] = abs(command.quantity)
        elif command.kind == "cancel":
            if command.target in self.orders:
                self.orders[command.target].status = "cancelled"
            self.stops.pop(command.target, None)


def setup(
    tmp_path: Path,
) -> tuple[HedgeCoordinator, Venue, Venue, PairRequest, HedgeLimits]:
    primary, hedge = Venue("primary", "account-1"), Venue("hedge", "account-2")
    request = PairRequest(
        symbol="BTC",
        side="long",
        quantity=2,
        limit_price=100,
        stop_loss_pct=0.02,
        hold_seconds=60,
    )
    limits = HedgeLimits(
        symbols=["BTC"],
        max_gross_notional=500,
        max_collateral=300,
        max_leverage=2,
        max_loss=10,
        max_cost=5,
        max_slippage_bps=50,
        max_unhedged_usd=10,
        authorization_expires_at=5000,
    )
    return HedgeCoordinator(tmp_path, primary, hedge), primary, hedge, request, limits


@pytest.mark.asyncio
async def test_partial_fill_hedges_actual_not_requested_and_restart(
    tmp_path: Path,
) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.quantity = 0.5
    p.orders[p.commands[0].id].filled_quantity = 0.5
    await coordinator.step(now=1000)
    assert h.commands[0].kind == "hedge" and h.commands[0].quantity == -0.5
    restarted = HedgeCoordinator(tmp_path, p, h)
    await restarted.start("op", request, limits, now=1000)
    assert len([c for c in p.commands if c.kind == "entry"]) == 1
    await restarted.step(now=1000)
    assert len([c for c in h.commands if c.kind == "hedge"]) == 1
    assert p.stops and h.stops
    p.quantity = 1
    await restarted.step(now=1000)
    assert [c.quantity for c in h.commands if c.kind == "hedge"] == [-0.5, -0.5]


@pytest.mark.asyncio
async def test_hold_expiry_closes_and_waits_for_cost_settlement(tmp_path: Path) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.quantity = 2
    p.orders[p.commands[0].id] = HedgeOrder(status="filled", filled_quantity=2)
    await coordinator.step(now=1000)
    await coordinator.step(now=1000)
    p.now = h.now = 1061
    p.finalized = False
    state = await coordinator.step(now=1061)
    assert state.status == "exiting"
    assert p.quantity == h.quantity == 0
    assert all(c.quantity < 0 for c in p.commands if c.kind == "close")
    assert all(c.quantity > 0 for c in h.commands if c.kind == "close")
    await coordinator.step(now=1061)  # cancel stops
    assert (await coordinator.step(now=1061)).status != "settled"
    p.finalized = True
    p.fees = 0.4
    h.fees = 0.3
    state = await coordinator.step(now=1061)
    assert state.status == "settled" and state.cost == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_lost_hedge_ack_no_duplicate_then_close_recovered_fill(
    tmp_path: Path,
) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.quantity = 1
    h.timeout_kind = "hedge"
    state = await coordinator.step(now=1000)
    assert state.blocked and state.status == "exiting"
    await coordinator.step(now=1000)
    assert len([c for c in h.commands if c.kind == "hedge"]) == 1
    assert state.status != "settled"
    # Later authoritative truth says the timed-out IOC did in fact fill.
    cmd = next(c for c in h.commands if c.kind == "hedge")
    h.orders[cmd.id] = HedgeOrder(status="filled", filled_quantity=1)
    h.quantity = -1
    await coordinator.step(now=1000)
    assert h.quantity == 0
    assert any(c.kind == "close" for c in h.commands)


@pytest.mark.asyncio
async def test_stale_state_blocks_new_writes_and_preserves_native_stops(
    tmp_path: Path,
) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.quantity = 1
    await coordinator.step(now=1000)
    count = len(p.commands) + len(h.commands)
    state = await coordinator.step(now=1015)
    assert state.blocked
    assert count == len(p.commands) + len(h.commands)
    assert p.stops


@pytest.mark.asyncio
async def test_orphan_leg_exits_without_reopening_primary(tmp_path: Path) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.quantity = 1
    await coordinator.step(now=1000)
    await coordinator.step(now=1000)
    assert coordinator.load().status == "active"
    p.quantity = 0  # primary native stop executed
    state = await coordinator.step(now=1000)
    assert state.blocked == "orphan hedge leg"
    assert h.quantity == 0
    assert len([c for c in p.commands if c.kind == "entry"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loss,cost,reason", [(20, 0, "loss limit"), (0, 6, "cost budget")]
)
async def test_risk_limits_latch(
    tmp_path: Path, loss: float, cost: float, reason: str
) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.equity -= loss
    p.fees = cost
    state = await coordinator.step(now=1000)
    assert reason in state.blocked
    await coordinator.step(now=1000)
    with pytest.raises(ValueError, match="unresolved"):
        await coordinator.start("new-op", request, limits, now=1000)


@pytest.mark.asyncio
async def test_guard_corrupt_state_halts_and_does_not_submit(tmp_path: Path) -> None:
    coordinator, p, h, _, _ = setup(tmp_path)
    coordinator.path.write_text("corrupt")
    with patch("wayfinder_paths.jobs.halt.request_halt") as halt:
        result = await guard_tick(coordinator, store=object(), job_id="paused-strategy")
    assert result["status"] == "blocked"
    assert halt.call_args.kwargs["source"] == "activity_risk"
    assert not p.commands and not h.commands


@pytest.mark.asyncio
async def test_dry_run_and_identity_scope(tmp_path: Path) -> None:
    coordinator, p, h, request, limits = setup(tmp_path)
    await coordinator.start("op", request, limits, now=1000)
    p.quantity = 1
    before = coordinator.path.read_bytes()
    await coordinator.step(now=1000, dry_run=True)
    assert coordinator.path.read_bytes() == before
    assert not h.commands
    h.account = "different-owner"
    with pytest.raises(ValueError, match="identity"):
        HedgeCoordinator(tmp_path, p, h).load()


@pytest.mark.asyncio
async def test_bad_price_and_short_authorization_refused_before_entry(
    tmp_path: Path,
) -> None:
    coordinator, p, _, request, limits = setup(tmp_path)
    request.limit_price = 102
    with pytest.raises(ValueError, match="slippage"):
        await coordinator.start("op", request, limits, now=1000)
    request.limit_price = 100
    limits.authorization_expires_at = 1100
    with pytest.raises(ValueError, match="authorization"):
        await coordinator.start("op", request, limits, now=1000)
    assert not p.commands
