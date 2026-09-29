"""One-shot execution simulation. No jobs runtime or network adapters are loaded."""

from .engine import EngineState, TickResult, run_tick
from .primitives import (
    BracketEngine,
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    ExecutionTrace,
    FillEvent,
    OrderIntent,
    PositionLedger,
    RestingOrder,
    StateSnapshot,
    TradeCapacity,
)
from .purity import PurityViolation
from .simulator import BacktestBroker, PreparedExecutionDataset, simulate_execution
from .validation import validate_execution_trace
from .venues import NativeProtectionResult, VenueCapabilities, VenueState

__all__ = [
    "BacktestBroker",
    "BracketEngine",
    "CompletedBarsView",
    "EngineState",
    "ExecutionContext",
    "ExecutionSpec",
    "ExecutionTrace",
    "FillEvent",
    "NativeProtectionResult",
    "OrderIntent",
    "PositionLedger",
    "PreparedExecutionDataset",
    "PurityViolation",
    "RestingOrder",
    "StateSnapshot",
    "TickResult",
    "TradeCapacity",
    "VenueCapabilities",
    "VenueState",
    "run_tick",
    "simulate_execution",
    "validate_execution_trace",
]
