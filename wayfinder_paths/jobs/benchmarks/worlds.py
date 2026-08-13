"""Procedural exact-benchmark worlds.

A world is a LATENT MECHANISM (seeded): conditional drift injected after
observable price events, plus noise. It emits development paths (visible to
optimizers) and hidden continuations (oracle-only) — same mechanism, fresh
noise, so the oracle's expected utility rewards finding the MECHANISM, never
one path's luck.

Ground truth comes from the exhaustive oracle, not from the archetype's
intent: after generation, each world is CALIBRATION-ASSERTED (a null world's
U* must be ~U_null; an interaction world's best basin must use a filter; a
deceptive world's global basin must differ from its bait basin). Worlds that
fail calibration are rejected and regenerated from the next derived seed —
recorded, never silent.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

BAR_SECONDS = 3600
_EPOCH = "2026-01-01T00:00:00+00:00"

ARCHETYPES: tuple[str, ...] = (
    "smooth_optimum",
    "deceptive_multi_peak",
    "spike_vs_plateau",
    "interaction_edge",
    "session_edge",
    "cost_flip",
    "regime_switch",
    "equivalent_optima",
    "disconnected_regions",
    "null_world",
)

# >=25% of any generated suite must be null worlds (reviewer requirement:
# an optimizer that always "improves" something is overfitting).
NULL_FRACTION = 0.25

DEV_BARS = 1600
HIDDEN_BARS = 1000
HIDDEN_PATHS = 64


@dataclass
class Mechanism:
    """Conditional drift rules applied while generating a path.

    Each rule: (trigger, drift, bars, sessions, vol_state, active) —
    trigger fires on observable price events; drift is injected for the next
    `bars` bars; sessions/vol_state optionally gate the injection (that is
    how interaction/session edges exist); active is a (start_frac, end_frac)
    window enabling regime switches and dev-only luck.
    """

    rules: list[dict[str, Any]] = field(default_factory=list)
    base_vol: float = 0.004
    fee_bps: float = 4.5


@dataclass
class World:
    world_id: str
    archetype: str
    seed: int
    mechanism: Mechanism  # SEALED — never emitted to public/dev artifacts
    dev_rows: list[list[dict[str, Any]]] = field(default_factory=list)
    hidden_rows: list[list[dict[str, Any]]] = field(default_factory=list)
    calibration: dict[str, Any] = field(default_factory=dict)


def _trigger_fired(name: str, closes: list[float], highs: list[float]) -> bool:
    n = len(closes)
    if name == "drop3" and n >= 4:
        return closes[-1] / closes[-4] - 1.0 <= -0.012
    if name == "rip3" and n >= 4:
        return closes[-1] / closes[-4] - 1.0 >= 0.012
    if name == "high20" and n >= 21:
        return closes[-1] > max(closes[-21:-1])
    if name == "low20" and n >= 21:
        return closes[-1] < min(closes[-21:-1])
    if name == "range_burst" and n >= 17:
        recent = [abs(a / b - 1.0) for a, b in zip(closes[-6:], closes[-7:-1], strict=True)]
        older = [abs(a / b - 1.0) for a, b in zip(closes[-15:-6], closes[-16:-7], strict=True)]
        return sum(recent) / len(recent) > 2.2 * (sum(older) / len(older) + 1e-9)
    return False


def _generate_path(
    mechanism: Mechanism, *, bars: int, seed: int, symbol: str, dev: bool = False
) -> list[dict[str, Any]]:
    import datetime as dt

    rng = random.Random(seed)
    start = dt.datetime.fromisoformat(_EPOCH)
    closes = [100.0]
    pending: list[tuple[float, int]] = []  # (drift, bars_left)
    rows: list[dict[str, Any]] = []
    # Mild vol clustering: regime multiplier random-walks in [0.6, 1.8].
    vol_mult = 1.0
    vol_window: list[float] = []
    for index in range(bars):
        vol_mult = min(1.8, max(0.6, vol_mult * (1.0 + rng.gauss(0.0, 0.03))))
        vol_now = mechanism.base_vol * vol_mult
        vol_window.append(vol_now)
        del vol_window[:-200]
        high_vol_state = vol_now > sorted(vol_window)[len(vol_window) // 2]
        hour = index % 24
        session = hour // 8
        frac = index / bars

        drift = 0.0
        still_pending = []
        for value, left in pending:
            drift += value
            if left > 1:
                still_pending.append((value, left - 1))
        pending = still_pending

        noise = rng.gauss(0.0, vol_now)
        close = max(1.0, closes[-1] * (1.0 + drift + noise))
        closes.append(close)

        for rule in mechanism.rules:
            if rule.get("dev_only") and not dev:
                continue
            start_frac, end_frac = rule.get("active") or (0.0, 1.0)
            if not (start_frac <= frac < end_frac):
                continue
            if rule.get("sessions") is not None and session not in rule["sessions"]:
                continue
            if rule.get("vol_state") == "high" and not high_vol_state:
                continue
            if rule.get("vol_state") == "low" and high_vol_state:
                continue
            if _trigger_fired(rule["trigger"], closes, closes):
                if rng.random() < float(rule.get("probability", 1.0)):
                    pending.append(
                        (float(rule["drift"]), int(rule.get("bars", 2)))
                    )

        stamp = (start + dt.timedelta(seconds=index * BAR_SECONDS)).isoformat()
        rows.append(
            {
                "timestamp": stamp,
                "symbol": symbol,
                "open": close * 0.9995,
                "high": close * (1.0 + 0.4 * vol_now),
                "low": close * (1.0 - 0.4 * vol_now),
                "close": close,
                "volume": 100,
            }
        )
    return rows


def _mechanism_for(archetype: str, rng: random.Random) -> Mechanism:
    # Strong edges must clear the calibration edge floor (0.010 expected
    # log growth on hidden paths) with margin after fees; weak stays under it.
    strong = rng.uniform(0.0055, 0.0085)
    weak = rng.uniform(0.0012, 0.0020)
    if archetype == "smooth_optimum":
        return Mechanism(rules=[{"trigger": "drop3", "drift": strong, "bars": 3}])
    if archetype == "deceptive_multi_peak":
        return Mechanism(
            rules=[
                # Bait: obvious momentum pays a little...
                {"trigger": "high20", "drift": weak, "bars": 2},
                # ...but fading range bursts pays much more.
                {"trigger": "range_burst", "drift": -strong, "bars": 4},
            ]
        )
    if archetype == "spike_vs_plateau":
        return Mechanism(
            rules=[
                {"trigger": "drop3", "drift": strong, "bars": 3},
                # DEV-ONLY luck: injected into development paths, absent
                # from hidden continuations — the seductive fake the funnel
                # must refuse. Truth (the oracle) never sees it.
                {"trigger": "rip3", "drift": strong * 2.0, "bars": 2,
                 "dev_only": True, "probability": 0.5},
            ]
        )
    if archetype == "interaction_edge":
        return Mechanism(
            rules=[{"trigger": "high20", "drift": strong, "bars": 3,
                    "vol_state": "high", "probability": 0.35}]
        )
    if archetype == "session_edge":
        session = rng.randrange(3)
        return Mechanism(
            rules=[{"trigger": "drop3", "drift": strong * 2.4, "bars": 4,
                    "sessions": {session}}]
        )
    if archetype == "cost_flip":
        return Mechanism(
            rules=[
                # High-frequency dribble edge (fees kill it)...
                {"trigger": "rip3", "drift": weak * 0.5, "bars": 1},
                # ...slow durable edge (survives costs).
                {"trigger": "low20", "drift": strong, "bars": 6,
                 "probability": 0.35},
            ],
            fee_bps=9.0,
        )
    if archetype == "regime_switch":
        return Mechanism(
            rules=[
                {"trigger": "high20", "drift": strong, "bars": 3,
                 "active": (0.0, 0.6), "probability": 0.35},
                {"trigger": "high20", "drift": -strong, "bars": 3,
                 "active": (0.6, 1.0), "probability": 0.35},
            ]
        )
    if archetype == "equivalent_optima":
        return Mechanism(
            rules=[
                {"trigger": "drop3", "drift": strong, "bars": 3},
                {"trigger": "rip3", "drift": -strong, "bars": 3},
            ]
        )
    if archetype == "disconnected_regions":
        return Mechanism(
            rules=[
                {"trigger": "low20", "drift": strong, "bars": 4,
                 "probability": 0.35},
                {"trigger": "high20", "drift": -strong, "bars": 4,
                 "probability": 0.35},
                # The "middle" (short-lookback triggers) stays pure noise.
            ]
        )
    if archetype == "null_world":
        return Mechanism(rules=[])
    raise ValueError(f"unknown archetype {archetype!r}")


def generate_world(archetype: str, seed: int, *, max_attempts: int = 4) -> World:
    """Generate + calibration-check; reject and re-derive the seed on failure."""
    from wayfinder_paths.jobs.benchmarks.calibration import calibrate_world

    attempt_seed = seed
    last_error = ""
    for attempt in range(max_attempts):
        rng = random.Random(attempt_seed)
        mechanism = _mechanism_for(archetype, rng)
        world = World(
            world_id=f"{archetype}-{seed:06d}",
            archetype=archetype,
            seed=seed,
            mechanism=mechanism,
            dev_rows=[
                _generate_path(
                    mechanism,
                    bars=DEV_BARS,
                    seed=attempt_seed * 7 + i,
                    symbol="SYN",
                    dev=True,
                )
                for i in range(2)
            ],
            hidden_rows=[
                _generate_path(
                    mechanism,
                    bars=HIDDEN_BARS,
                    seed=attempt_seed * 104729 + 1000 + i,
                    symbol="SYN",
                )
                for i in range(HIDDEN_PATHS)
            ],
        )
        verdict = calibrate_world(world)
        world.calibration = {**verdict, "attempts": attempt + 1}
        if verdict["passed"]:
            return world
        last_error = str(verdict.get("reason"))
        attempt_seed = attempt_seed * 31 + 17
    raise RuntimeError(
        f"world {archetype}/{seed} failed calibration after {max_attempts} "
        f"attempts: {last_error}"
    )
