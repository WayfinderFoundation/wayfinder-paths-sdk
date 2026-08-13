"""Strategy Genome v1: the finite, typed, enumerable candidate space G.

The bounded-global claim is only meaningful over a declared search space.
A genome is pure data (dict-codable, hashable); it compiles to params for
ONE generic interpreter strategy (compiler.py), so the vectorized oracle and
the production engine evaluate identical semantics.

Sizing note: spaces target 2k-20k genomes per world — large enough that a
budgeted optimizer (25-100 evaluations) cannot enumerate them, small enough
for the oracle to evaluate exhaustively over hidden continuations.

Execution semantics (shared with oracle + interpreter, v1):
- Entry: signal true on completed bar t (with confirm filter true) → fill at
  open[t+1].
- One position per symbol; signals while positioned are ignored.
- Exits are COMPLETED-BAR decisions → fill at next open (no in-bar bracket
  fills in v1 — keeps oracle/engine parity exact by construction).
- Fees: fee_bps per side.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Any

# Curated signal subset: names must exist in SIGNAL_LIBRARY (asserted in
# tests). Chosen to span families the archetype worlds plant edges in.
GENOME_SIGNALS: tuple[str, ...] = (
    "new_low_5",
    "new_high_5",
    "new_low_20",
    "new_high_20",
    "rsi14_le_30",
    "rsi14_ge_70",
    "ema_cross_up_9_50",
    "ema_cross_dn_9_50",
    "bb20_z_le_neg2",
    "bb20_z_ge_2",
    "vol_surge_up",
    "vol_surge_dn",
)

DIRECTIONS: tuple[str, ...] = ("long", "short")

# Confirm filters gate entries; "interaction edge" worlds plant mechanisms
# that ONLY pay when signal and filter co-occur.
FILTERS: tuple[str, ...] = (
    "none",
    "above_sma50",
    "below_sma50",
    "high_vol",  # ATR14 > median ATR over lookback
    "low_vol",
    "session_a",  # bar hour in [0, 8)
    "session_b",  # bar hour in [8, 16)
    "session_c",  # bar hour in [16, 24)
)

# Exit variants: (family, params). Buckets, not continua — the space must be
# finite and exactly enumerable.
EXITS: tuple[tuple[str, dict[str, Any]], ...] = tuple(
    [("fixed_time", {"hold_bars": hold}) for hold in (2, 4, 8, 16)]
    + [
        ("target_stop", {"target_pct": target, "stop_pct": stop})
        for target in (0.01, 0.02, 0.04)
        for stop in (0.01, 0.02, 0.04)
    ]
    + [("trailing", {"trail_pct": trail}) for trail in (0.01, 0.02, 0.04)]
    + [
        ("time_stop", {"hold_bars": hold, "stop_pct": stop})
        for hold in (4, 16)
        for stop in (0.01, 0.02)
    ]
)

SIZINGS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("fixed", {}),
    ("vol_target", {"target_vol": 0.01}),
)

GRAMMAR_VERSION = "strategy-genome-v1"


@dataclass(frozen=True)
class Genome:
    signal: str
    direction: str
    confirm_filter: str
    exit_family: str
    exit_params: tuple[tuple[str, Any], ...]
    sizing_family: str
    sizing_params: tuple[tuple[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "genome": True,
            "grammar_version": GRAMMAR_VERSION,
            "signal": self.signal,
            "direction": self.direction,
            "confirm_filter": self.confirm_filter,
            "exit_family": self.exit_family,
            "exit_params": dict(self.exit_params),
            "sizing_family": self.sizing_family,
            "sizing_params": dict(self.sizing_params),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Genome:
        return cls(
            signal=str(data["signal"]),
            direction=str(data["direction"]),
            confirm_filter=str(data["confirm_filter"]),
            exit_family=str(data["exit_family"]),
            exit_params=tuple(sorted((data.get("exit_params") or {}).items())),
            sizing_family=str(data["sizing_family"]),
            sizing_params=tuple(sorted((data.get("sizing_params") or {}).items())),
        )

    @property
    def genome_id(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def enumerate_genomes(
    *,
    signals: tuple[str, ...] = GENOME_SIGNALS,
    filters: tuple[str, ...] = FILTERS,
    exits: tuple[tuple[str, dict[str, Any]], ...] = EXITS,
    sizings: tuple[tuple[str, dict[str, Any]], ...] = SIZINGS,
) -> list[Genome]:
    """The complete space G for a world (worlds may restrict the subsets to
    hit their target size — restriction is recorded in the world manifest)."""
    genomes = []
    for signal, direction, confirm, (exit_family, exit_params), (
        sizing_family,
        sizing_params,
    ) in itertools.product(signals, DIRECTIONS, filters, exits, sizings):
        genomes.append(
            Genome(
                signal=signal,
                direction=direction,
                confirm_filter=confirm,
                exit_family=exit_family,
                exit_params=tuple(sorted(exit_params.items())),
                sizing_family=sizing_family,
                sizing_params=tuple(sorted(sizing_params.items())),
            )
        )
    return genomes


def grammar_hash(
    *,
    signals: tuple[str, ...] = GENOME_SIGNALS,
    filters: tuple[str, ...] = FILTERS,
    exits: tuple[tuple[str, dict[str, Any]], ...] = EXITS,
    sizings: tuple[tuple[str, dict[str, Any]], ...] = SIZINGS,
) -> str:
    """Deterministic hash of the declared space — part of the bounded claim.
    Any change to the grammar is a NEW benchmark version, never a silent
    retrofit."""
    payload = json.dumps(
        {
            "version": GRAMMAR_VERSION,
            "signals": list(signals),
            "directions": list(DIRECTIONS),
            "filters": list(filters),
            "exits": [[family, dict(sorted(params.items()))] for family, params in exits],
            "sizings": [
                [family, dict(sorted(params.items()))] for family, params in sizings
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# The default full space: 12 signals x 2 directions x 8 filters x 20 exits
# x 2 sizings = 7,680 genomes — inside the 2k-20k target band.
def default_space_size() -> int:
    return (
        len(GENOME_SIGNALS) * len(DIRECTIONS) * len(FILTERS) * len(EXITS) * len(SIZINGS)
    )
