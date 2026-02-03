from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger


@dataclass
class OPAConfig:
    max_iterations_per_tick: int = 4
    max_steps_per_iteration: int = 5
    max_total_steps_per_tick: int = 15


@dataclass
class PlanStep[TOp: Enum]:
    op: TOp
    priority: int
    key: str
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __repr__(self) -> str:
        return f"PlanStep({self.op.name}, priority={self.priority}, key={self.key!r})"


@dataclass
class Plan[TOp: Enum]:
    steps: list[PlanStep[TOp]] = field(default_factory=list)
    desired_state: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.steps)

    def __len__(self) -> int:
        return len(self.steps)


class OPALoopMixin[TInventory, TOp: Enum](ABC):
    @property
    @abstractmethod
    def opa_config(self) -> OPAConfig: ...

    @abstractmethod
    async def observe(self) -> TInventory: ...

    @abstractmethod
    def plan(self, inventory: TInventory) -> Plan[TOp]: ...

    @abstractmethod
    async def execute_step(
        self, step: PlanStep[TOp], inventory: TInventory
    ) -> tuple[bool, str]: ...

    @abstractmethod
    def get_inventory_changing_ops(self) -> set[TOp]: ...

    async def on_loop_start(self) -> tuple[bool, str] | None:
        return None

    async def on_step_executed(
        self, step: PlanStep[TOp], success: bool, message: str
    ) -> None:
        return None

    def should_stop_early(
        self, inventory: TInventory, iteration: int
    ) -> tuple[bool, str] | None:
        return None

    async def on_loop_end(
        self, success: bool, messages: list[str], total_steps: int
    ) -> None:
        return None

    async def run_opa_loop(self) -> tuple[bool, str, bool]:
        loop_logger = logger.bind(loop="opa")

        setup_result = await self.on_loop_start()
        if setup_result is not None:
            return (*setup_result, False)

        total_steps = 0
        messages: list[str] = []
        rotated = False
        config = self.opa_config

        try:
            for iteration in range(config.max_iterations_per_tick):
                loop_logger.debug(
                    f"OPA iteration {iteration + 1}/{config.max_iterations_per_tick}"
                )

                # OBSERVE
                try:
                    inventory = await self.observe()
                except Exception as e:
                    loop_logger.error(f"Observe failed: {e}")
                    return (False, f"Failed to observe: {e}", rotated)

                stop_result = self.should_stop_early(inventory, iteration)
                if stop_result is not None:
                    await self.on_loop_end(stop_result[0], messages, total_steps)
                    return (*stop_result, rotated)

                # PLAN
                try:
                    plan = self.plan(inventory)
                except Exception as e:
                    loop_logger.error(f"Plan failed: {e}")
                    return (False, f"Failed to plan: {e}", rotated)

                if not plan.steps:
                    loop_logger.debug("Plan is empty, nothing to do")
                    break

                loop_logger.debug(f"Plan has {len(plan.steps)} steps")

                # ACT - execute steps up to limit
                steps_this_iteration = 0
                for step in plan.steps[: config.max_steps_per_iteration]:
                    if total_steps >= config.max_total_steps_per_tick:
                        loop_logger.warning(
                            f"Hit max total steps ({config.max_total_steps_per_tick})"
                        )
                        break

                    loop_logger.info(f"Executing step: {step.op.name} ({step.reason})")

                    try:
                        success, msg = await self.execute_step(step, inventory)
                    except Exception as e:
                        success = False
                        msg = f"Step {step.op.name} raised exception: {e}"
                        loop_logger.error(msg)

                    await self.on_step_executed(step, success, msg)
                    messages.append(f"{step.op.name}: {msg}")
                    total_steps += 1
                    steps_this_iteration += 1

                    if step.params.get("is_rotation"):
                        rotated = True

                    # Re-observe after inventory-changing ops (failed steps likely didn't change anything)
                    if success and step.op in self.get_inventory_changing_ops():
                        loop_logger.debug(
                            f"Step {step.op.name} changes inventory, re-observing"
                        )
                        break

                if steps_this_iteration == 0:
                    break

        except Exception as e:
            loop_logger.error(f"OPA loop failed: {e}")
            await self.on_loop_end(False, messages, total_steps)
            return (False, f"OPA loop error: {e}", rotated)

        final_message = "; ".join(messages) if messages else "No action needed"
        loop_logger.info(f"OPA loop complete: {total_steps} steps executed")

        await self.on_loop_end(True, messages, total_steps)
        return (True, final_message, rotated)
