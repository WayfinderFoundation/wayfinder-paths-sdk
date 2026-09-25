"""Registered phases for runner tests; importable by a runtime subprocess."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compute_phase import compute_phase


@compute_phase
def score_prices(inputs: Path, outputs: Path, args: dict[str, Any]) -> dict[str, Any]:
    prices = [float(value) for value in (inputs / args["prices"]).read_text().split()]
    scaled = [price * args["scale"] for price in prices]
    (outputs / "scaled.txt").write_text("\n".join(str(value) for value in scaled))
    return {"count": len(prices), "total": sum(scaled), "undefined": float("nan")}


@compute_phase
def rejected_candidate(
    inputs: Path, outputs: Path, args: dict[str, Any]
) -> dict[str, Any]:
    (outputs / "diagnostics.txt").write_text("partial evidence")
    raise ValueError("candidate violates its contract")


def unregistered(inputs: Path, outputs: Path, args: dict[str, Any]) -> dict[str, Any]:
    return {}
