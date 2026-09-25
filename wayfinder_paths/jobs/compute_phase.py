"""Registered pure computations that any configured backtest runner may execute."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

PhaseFunction = Callable[[Path, Path, dict[str, Any]], Any]

_PHASES: dict[str, PhaseFunction] = {}


def phase_name(function: Callable[..., Any]) -> str:
    return f"{function.__module__}:{function.__qualname__}"


def compute_phase[F: PhaseFunction](function: F) -> F:
    """Register ``function(inputs, outputs, args) -> JSON``.

    A phase reads only its packed inputs directory and JSON arguments and
    writes only its outputs directory, so it runs the same locally or on a
    remote runner. The runner imports it by name, so it must live at module
    level inside ``wayfinder_paths``.
    """
    name = phase_name(function)
    if "<locals>" in name or not name.startswith("wayfinder_paths."):
        raise ValueError(f"Compute phases must be module-level SDK functions: {name}")
    _PHASES[name] = function
    return function


def resolve_phase(name: str) -> PhaseFunction:
    module, _, attribute = name.partition(":")
    if not module.startswith("wayfinder_paths.") or not attribute:
        raise ValueError(f"Unknown compute phase: {name}")
    importlib.import_module(module)
    try:
        return _PHASES[name]
    except KeyError as exc:
        raise ValueError(f"Unregistered compute phase: {name}") from exc
