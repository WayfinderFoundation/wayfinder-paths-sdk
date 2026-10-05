"""The research boundary: proposals describe intent, never executable transactions."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUDGETS = (100, 1000, 10000, 100000)
Budget = Literal[100, 1000, 10000, 100000]
Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[\w:./-]+$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Construction(Contract):
    """Agent-inferred objective, not an evaluator-supplied asset hint."""

    mode: Literal["directional", "matched_relative"]
    benchmark: Text | None = None
    benchmark_instrument_id: Identifier | None = None
    benchmark_direction: Literal["long", "short"] | None = None

    @model_validator(mode="after")
    def validate_benchmark(self) -> Self:
        if self.mode == "directional":
            if any(
                (self.benchmark, self.benchmark_instrument_id, self.benchmark_direction)
            ):
                raise ValueError("Directional construction has no benchmark leg")
        elif not self.benchmark or not self.benchmark_direction:
            raise ValueError(
                "Relative construction requires a named benchmark and direction"
            )
        return self

    @property
    def intent(self) -> Literal["absolute", "relative"]:
        return "relative" if self.mode == "matched_relative" else "absolute"


class Component(Contract):
    id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=120)]
    rationale: Text
    counterargument: Text
    invalidation: Text
    evidence: Annotated[list[Text], Field(min_length=1, max_length=6)]


class Position(Contract):
    id: Identifier
    component_id: Identifier
    kind: Literal["token", "perp", "hip3", "prediction"]
    # Resolved SDK lookup id, Hyperliquid asset_name, or Polymarket outcome token id.
    instrument_id: Identifier
    symbol: Annotated[str, Field(min_length=1, max_length=120)]
    direction: Literal["long", "short", "yes", "no"]
    capital_bps: Annotated[int, Field(gt=0, le=10000, strict=True)]
    leverage: Annotated[float, Field(ge=1, le=2)] = 1
    rationale: Text
    stop_loss_pct: Annotated[float, Field(gt=0, lt=1)] | None = None
    take_profit_pct: Annotated[float, Field(gt=0, le=10)] | None = None

    @model_validator(mode="after")
    def validate_instrument(self) -> Self:
        if self.kind == "prediction":
            if self.direction not in {"yes", "no"} or not self.instrument_id.isdigit():
                raise ValueError(
                    "Prediction positions require an exact outcome token and YES/NO"
                )
        elif self.direction not in {"long", "short"}:
            raise ValueError("Only prediction positions have YES/NO directions")
        if self.kind == "token" and self.direction != "long":
            raise ValueError("Spot tokens cannot be shorted")
        if self.kind in {"token", "prediction"}:
            if self.leverage != 1:
                raise ValueError("Spot and outcome shares must be fully funded")
            if self.stop_loss_pct is not None or self.take_profit_pct is not None:
                raise ValueError("Protective orders are supported only on perps")
        if self.kind == "hip3" and ":" not in self.instrument_id:
            raise ValueError("HIP-3 requires a dex-qualified asset name")
        if self.kind == "perp" and not self.instrument_id.endswith("-USDC"):
            raise ValueError("Regular perps require their canonical asset name")
        return self


class Variant(Contract):
    budget_usd: Budget
    rationale: Text
    positions: Annotated[list[Position], Field(max_length=12)]
    cash_bps: Annotated[int, Field(ge=0, le=10000, strict=True)]

    @model_validator(mode="after")
    def validate_allocations(self) -> Self:
        if sum(p.capital_bps for p in self.positions) + self.cash_bps != 10000:
            raise ValueError("Capital and cash must sum to 10000 bps")
        if sum(p.capital_bps * p.leverage for p in self.positions) > 20000:
            raise ValueError("Gross exposure cannot exceed 2x budget")
        ids = [p.id for p in self.positions]
        instruments = [p.instrument_id for p in self.positions]
        if len(ids) != len(set(ids)) or len(instruments) != len(set(instruments)):
            raise ValueError("Duplicate positions or instruments")
        return self


class ProposalMetadata(Contract):
    schema_version: Literal[1] = 1
    title: Annotated[str, Field(min_length=1, max_length=120)]
    interpretation: Text
    intent: Literal["absolute", "relative"]
    assumptions: Annotated[list[Text], Field(min_length=1, max_length=8)]


class Proposal(ProposalMetadata):
    components: Annotated[list[Component], Field(min_length=1, max_length=8)]
    variants: Annotated[list[Variant], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def validate_variants(self) -> Self:
        if sorted(v.budget_usd for v in self.variants) != list(BUDGETS):
            raise ValueError(
                "Exactly one independently constructed variant per budget is required"
            )
        ids = [c.id for c in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate thesis components")
        if any(p.component_id not in ids for v in self.variants for p in v.positions):
            raise ValueError("Unknown thesis component")
        return self


class PortfolioSections(Contract):
    """Validated sections for diagnostics, never a publishable proposal."""

    components: list[Component] = []
    variants: list[Variant] = []


class DraftUpdate(Contract):
    """Small parent-only updates; the transcript remains the sole draft store."""

    metadata: ProposalMetadata | None = None
    components: Annotated[list[Component], Field(max_length=8)] = []
    remove_components: Annotated[list[Identifier], Field(max_length=8)] = []
    variant: Variant | None = None
    variant_budgets: (
        Annotated[
            list[Budget],
            Field(
                min_length=1,
                max_length=4,
                description="Apply this exact variant to these budgets, including variant.budget_usd. Omit for one budget. Use only when allocation and rationale are justified for every listed budget; each is still validated separately.",
            ),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def validate_update(self) -> Self:
        if self.variant_budgets is not None:
            if self.variant is None:
                raise ValueError("variant_budgets requires a variant")
            if self.variant.budget_usd not in self.variant_budgets:
                raise ValueError("variant_budgets must include variant.budget_usd")
            if len(set(self.variant_budgets)) != len(self.variant_budgets):
                raise ValueError("variant_budgets must be unique")
        if not (
            self.metadata or self.components or self.remove_components or self.variant
        ):
            raise ValueError("Provide metadata, components or one budget variant")
        ids = [c.id for c in self.components]
        if len(set(ids)) != len(ids) or set(ids) & set(self.remove_components):
            raise ValueError(
                "Update each component once; do not update and remove it together"
            )
        return self
