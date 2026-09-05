"""Mechanical evolution-campaign funnel summary for reports and activity."""

from __future__ import annotations

from typing import Any

_QUICK_REJECTION_STATUSES = {"invalid", "low_fidelity_rejected"}
_FULL_DEV_PASS_STATUSES = {
    "dev_frontier",
    "proposal_running",
    "proposal_rejected",
    "proposal_deferred",
    "paper_proposal",
    "paper_experiment",
    "probation",
    "probation_deferred",
}


def summarize_evolution_funnel(state: dict[str, Any]) -> dict[str, Any]:
    """Derive gate outcomes from durable campaign state without new writes."""
    candidates = [
        candidate
        for candidate in state.get("candidates") or []
        if isinstance(candidate, dict)
    ]
    counts = state.get("counts") or {}
    budgets = state.get("budgets") or {}
    generated = _count(counts, "generated")
    quick_evaluated = _count(counts, "quick_evaluated")
    full_dev_evaluated = _count(counts, "full_dev")
    gate_evaluated = _count(counts, "proposed")
    quick_attempts = _count(counts, "quick_attempts") or quick_evaluated
    repairs = _count(counts, "repairs")

    # A quick rejection never receives a full-dev allocation.  Checking the
    # durable allocation marker also classifies campaigns completed before
    # full_dev_at was added to candidate records.
    quick_rejected = sum(
        str(candidate.get("status") or "") in _QUICK_REJECTION_STATUSES
        and "full_dev_tune" not in candidate
        for candidate in candidates
    )
    full_dev_passed = sum(
        str(candidate.get("status") or "") in _FULL_DEV_PASS_STATUSES
        for candidate in candidates
    )
    # A specialist whose validation window lacked its declared regime was
    # evaluated but neither passed nor rejected; it waits for the regime.
    full_dev_awaiting = sum(
        candidate.get("status") == "awaiting_regime" for candidate in candidates
    )
    optuna_completed = sum(
        isinstance(candidate.get("tuning"), dict)
        and _value(candidate["tuning"].get("trials")) > 0
        for candidate in candidates
    )
    optuna_running = sum(
        candidate.get("full_dev_tune") is True
        and candidate.get("status") == "full_dev_running"
        for candidate in candidates
    )
    probation_admitted = sum(
        candidate.get("status") in {"paper_proposal", "paper_experiment", "probation"}
        for candidate in candidates
    )

    funnel = {
        "design": {
            "hypotheses": _value((state.get("design") or {}).get("hypotheses")),
            "wildcards": _value((state.get("design") or {}).get("wildcards")),
            "falsified": len(
                (state.get("design") or {}).get("falsified_hypotheses") or []
            ),
        },
        "generated": {
            "total": generated,
            "target": _optional_count(budgets, "generated"),
            "structural": sum(
                candidate.get("mutation_kind") == "structural"
                for candidate in candidates
            ),
            "parameter": sum(
                candidate.get("mutation_kind") == "parameter"
                for candidate in candidates
            ),
        },
        "quick_screen": {
            "evaluated": quick_evaluated,
            "attempts": quick_attempts,
            "attempt_cap": _optional_count(budgets, "quick_attempts"),
            "repairs": repairs,
            "passed": max(quick_evaluated - quick_rejected, 0),
            "rejected": quick_rejected,
            "pending": max(generated - quick_evaluated, 0),
        },
        "full_development": {
            "evaluated": full_dev_evaluated,
            "target": _optional_count(budgets, "full_development"),
            "passed": full_dev_passed,
            "rejected": max(
                full_dev_evaluated - full_dev_passed - full_dev_awaiting, 0
            ),
            "awaiting_regime": full_dev_awaiting,
            "running": sum(
                candidate.get("status") == "full_dev_running"
                for candidate in candidates
            ),
        },
        "optuna": {
            "eligible": sum(
                candidate.get("tuning_eligible") is True for candidate in candidates
            ),
            "previewed": sum(
                isinstance(candidate.get("tuning_preview"), dict)
                for candidate in candidates
            ),
            "selected": sum(
                candidate.get("full_dev_tune") is True for candidate in candidates
            ),
            "completed": optuna_completed,
            "minimum": _optional_count(budgets, "optuna_minimum"),
            "budget": _optional_count(budgets, "optuna"),
            "running": optuna_running,
        },
        "finalist_gate": {
            "evaluated": gate_evaluated,
            "target": _optional_count(budgets, "finalist_gate"),
            # Keep the historical key for older frontend clients while adding
            # the permanent lifecycle name used by new snapshots.
            "advanced_to_paper": probation_admitted,
            "advanced_to_probation": probation_admitted,
            "rejected": sum(
                candidate.get("status") == "proposal_rejected"
                for candidate in candidates
            ),
            "deferred": sum(
                candidate.get("status") in {"proposal_deferred", "probation_deferred"}
                for candidate in candidates
            ),
            "running": sum(
                candidate.get("status") == "proposal_running"
                for candidate in candidates
            ),
        },
    }
    if "quick_attempts" not in counts and "quick_attempts" not in budgets:
        funnel.pop("design", None)
        for key in ("attempts", "attempt_cap", "repairs"):
            funnel["quick_screen"].pop(key, None)
    return funnel


def format_evolution_funnel(funnel: dict[str, Any]) -> str:
    """Compact one-line form for the existing Activity detail surface."""
    generated = funnel.get("generated") or {}
    quick = funnel.get("quick_screen") or {}
    full_dev = funnel.get("full_development") or {}
    optuna = funnel.get("optuna") or {}
    gate = funnel.get("finalist_gate") or {}
    design = funnel.get("design") or {}
    generated_progress = _progress(generated.get("total"), generated.get("target"))
    full_dev_progress = _progress(full_dev.get("evaluated"), full_dev.get("target"))
    optuna_budget = optuna.get("budget")
    optuna_minimum = optuna.get("minimum")
    gate_progress = _progress(gate.get("evaluated"), gate.get("target"))
    optuna_running = _value(optuna.get("running"))
    optuna_suffix = f", {optuna_running} running" if optuna_running else ""
    optuna_limits = []
    if optuna_minimum is not None:
        optuna_limits.append(f"min {_value(optuna_minimum)}")
    if optuna_budget is not None:
        optuna_limits.append(f"budget {_value(optuna_budget)}")
    limits_suffix = f"; {', '.join(optuna_limits)}" if optuna_limits else ""
    design_prefix = ""
    if any(key in design for key in ("hypotheses", "wildcards", "falsified")) and (
        _value(design.get("hypotheses"))
        or _value(design.get("wildcards"))
        or _value(design.get("falsified"))
    ):
        design_prefix = (
            f"{_value(design.get('hypotheses'))} hypotheses, "
            f"{_value(design.get('wildcards'))} wildcards, "
            f"{_value(design.get('falsified'))} falsified → "
        )
    attempt_detail = ""
    generated_noun = "generated"
    if "attempts" in quick:
        attempt_detail = (
            f"{_value(quick.get('attempts'))} attempts, "
            f"{_value(quick.get('repairs'))} repairs, "
        )
        generated_noun = "ideas"
    return (
        f"{design_prefix}"
        f"{generated_progress} {generated_noun} "
        f"({_value(generated.get('structural'))} structural, "
        f"{_value(generated.get('parameter'))} parameter) → "
        f"quick {attempt_detail}"
        f"{_value(quick.get('passed'))} pass, "
        f"{_value(quick.get('rejected'))} reject → "
        f"full dev {_value(full_dev.get('passed'))} pass, "
        f"{_value(full_dev.get('rejected'))} reject "
        f"({full_dev_progress} complete; Optuna "
        f"{_value(optuna.get('completed'))} complete "
        f"({_value(optuna.get('eligible'))} eligible, "
        f"{_value(optuna.get('previewed'))} previewed, "
        f"{_value(optuna.get('selected'))} selected"
        f"{limits_suffix}{optuna_suffix})) → "
        f"gate {gate_progress}; probation "
        f"{_value(gate.get('advanced_to_probation', gate.get('advanced_to_paper')))}"
    )


def _progress(value: Any, target: Any) -> str:
    current = _value(value)
    return f"{current}/{_value(target)}" if target is not None else str(current)


def _value(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _count(values: Any, name: str) -> int:
    return _value(values.get(name)) if isinstance(values, dict) else 0


def _optional_count(values: Any, name: str) -> int | None:
    if not isinstance(values, dict) or name not in values:
        return None
    return _value(values.get(name))
