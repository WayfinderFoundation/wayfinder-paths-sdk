from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, validator


class AdapterRequirement(BaseModel):
    name: str = Field(
        ..., description="Adapter symbolic name (e.g., BALANCE, HYPERLIQUID)"
    )
    capabilities: list[str] = Field(default_factory=list)


class StrategyManifest(BaseModel):
    schema_version: str = Field(default="0.1")
    status: Literal["stable", "wip", "deprecated"] = Field(
        default="stable",
        description="Strategy maturity status. 'wip' strategies show warnings on execution.",
    )
    entrypoint: str = Field(
        ...,
        description="Python path to class, e.g. strategies.funding_rate_strategy.FundingRateStrategy",
    )
    name: str | None = Field(
        default=None,
        description="Unique name identifier for this strategy instance. Used to look up dedicated wallet in config.json by label.",
    )
    permissions: dict[str, Any] = Field(default_factory=dict)
    adapters: list[AdapterRequirement] = Field(default_factory=list)

    @validator("entrypoint")
    def validate_entrypoint(cls, v: str) -> str:
        if "." not in v:
            raise ValueError(
                "entrypoint must be a full import path to a Strategy class"
            )
        return v

    @validator("permissions")
    def validate_permissions(cls, v: dict) -> dict:
        if "policy" not in v:
            raise ValueError("permissions.policy is required")
        if not v["policy"]:
            raise ValueError("permissions.policy cannot be empty")
        return v

    @validator("adapters")
    def validate_adapters(cls, v: list) -> list:
        if not v:
            raise ValueError("adapters cannot be empty")
        return v


def load_strategy_manifest(path: str) -> StrategyManifest:
    with open(path) as f:
        data = yaml.safe_load(f)
    return StrategyManifest(**data)


def load_manifest(path: str) -> StrategyManifest:
    """Legacy function for backward compatibility."""
    return load_strategy_manifest(path)


def validate_manifest(manifest: StrategyManifest) -> None:
    # Simple v0.1 rules: require at least one adapter and permissions.policy
    if not manifest.adapters:
        raise ValueError("Manifest must declare at least one adapter")
    if "policy" not in manifest.permissions:
        raise ValueError("Manifest.permissions must include 'policy'")
