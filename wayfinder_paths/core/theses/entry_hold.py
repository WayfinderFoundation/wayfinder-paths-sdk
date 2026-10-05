from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.execution.primitives import ExecutionContext, OrderIntent


class EntryAndHold:
    """One initial decision; protective exits never cause re-entry."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params

    def decide(self, ctx: ExecutionContext) -> list[OrderIntent]:
        if ctx.strategy_state.get("entry_decided"):
            return []
        ctx.strategy_state["entry_decided"] = True
        return [
            OrderIntent(
                action="OPEN",
                venue=self.params["venue"],
                symbol=self.params["symbol"],
                side=self.params["side"],
                notional=self.params["notional"],
                bracket=self.params.get("bracket"),
                metadata={"leverage_applied": True},
            )
        ]
