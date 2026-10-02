"""Shared publication checks for hosted research, notebook status and evaluations."""

from typing import Any

from pydantic import ValidationError

from wayfinder_paths.core.theses.models import (
    Component,
    Construction,
    PortfolioSections,
    Proposal,
    Variant,
)
from wayfinder_paths.core.theses.quantification import allocation_key
from wayfinder_paths.core.theses.research import (
    missing_source_reads,
    validate_full_allocation,
    validate_market_capacity,
)
from wayfinder_paths.core.theses.sizing import construction_errors


def validate_proposal(
    payload: object, market_evidence: dict[str, Any] | None = None
) -> Proposal:
    errors = []
    evidence = market_evidence or {}
    construction = (
        Construction.model_validate(evidence["construction"])
        if evidence.get("construction")
        else None
    )
    errors.extend(evidence.get("draft_errors", []))
    try:
        proposal = Proposal.model_validate(payload)
    except ValidationError as exc:
        # Feedback names the failed fields without reflecting untrusted input into instructions.
        errors.extend(
            f"{'.'.join(map(str, error['loc'])) or 'proposal'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        proposal = None
    # Validate independent sections with the original strict types; never
    # construct a fake valid Proposal or silently repair invalid fields.
    sections: dict[str, list[Any]] = {"components": [], "variants": []}
    if proposal is None and isinstance(payload, dict):
        for field, model in (("components", Component), ("variants", Variant)):
            values = payload.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                try:
                    sections[field].append(model.model_validate(value))
                except ValidationError:
                    pass  # Already reported by the full schema validation above.
    checked = proposal or PortfolioSections(**sections)
    for variant in checked.variants:
        for position in variant.positions:
            if position.kind == "token":
                token = (
                    (market_evidence or {})
                    .get("onchain_tokens", {})
                    .get(position.instrument_id, {})
                )
                canonical = token.get("identity", {}).get("canonical_asset") or {}
                if canonical.get("settlement_rank") is not None and canonical.get(
                    "verification"
                ) not in {"native", "wrapped_native"}:
                    errors.append(
                        "Cash-equivalent tokens cannot fill a target allocation gap"
                    )
                if (
                    "/" in position.instrument_id
                    and not position.instrument_id.endswith("/USDC")
                ):
                    errors.append(
                        "Hyperliquid spot portfolios require USDC-quoted pairs"
                    )
                if variant.budget_usd * position.capital_bps / 10000 < 10:
                    errors.append("Spot notional must be at least $10")
            if position.kind not in {"perp", "hip3"}:
                continue
            notional = (
                variant.budget_usd * position.capital_bps / 10000 * position.leverage
            )
            if notional < 10:
                errors.append(
                    f"{variant.budget_usd}/{position.id}: perp notional must be at least $10"
                )
        errors.extend(
            construction_errors(
                variant,
                construction,
                intent=payload.get("intent") if isinstance(payload, dict) else None,
            )
        )
    if proposal is None or any(v.positions for v in checked.variants):
        errors.extend(evidence.get("assessment", {}).get("errors", []))
    if any(v.positions for v in checked.variants):
        if "assessment" in evidence:
            tokens = evidence.get("onchain_tokens", {})
            kept = {
                tokens.get(i, {}).get("token_id") or i
                for i in evidence["assessment"].get("kept_instruments", [])
            }
            unassessed = {
                p.instrument_id
                for v in checked.variants
                for p in v.positions
                if (tokens.get(p.instrument_id, {}).get("token_id") or p.instrument_id)
                not in kept
            }
            if unassessed:
                errors.append(
                    "Selected instruments missing KEEP assessment: "
                    + ", ".join(sorted(unassessed))
                )
    try:
        validate_full_allocation(checked)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        validate_market_capacity(checked, evidence, screen_capacity=False)
    except ValueError as exc:
        errors.append(str(exc))
    if missing := missing_source_reads(checked, evidence):
        errors.append(
            f"Components {', '.join(missing)}: cite a relevant source actually read with "
            "core_web_fetch (returned text required), or directly read prediction resolution "
            "rules. Reuse parent/child reads; search snippets and claimed fetches are not proof"
        )
    missing_quant = [
        str(v.budget_usd)
        for v in checked.variants
        if v.positions
        and allocation_key(v) not in evidence.get("quantified_allocations", {})
    ]
    if missing_quant:
        errors.append(
            f"Budgets {', '.join(missing_quant)}: call research_quantify_portfolio with these "
            "final allocations, then use its measured risk/coverage in your rationale. "
            "One variant per distinct allocation suffices. Missing history is an explicit "
            "limitation, not zero risk or a reason by itself to recommend cash"
        )
    if evidence.get("require_implementation_comparisons"):
        tokens = evidence.get("onchain_tokens", {})
        compared = {
            tokens.get(i, {}).get("token_id") or i
            for i in evidence.get("implementation_comparisons", {})
        }
        missing_comparisons = {
            p.instrument_id
            for v in checked.variants
            for p in v.positions
            if (tokens.get(p.instrument_id, {}).get("token_id") or p.instrument_id)
            not in compared
        }
        if missing_comparisons:
            errors.append(
                "Compare selected implementations with research_quantify_portfolio(compare_implementations=true), including closest alternatives: "
                + ", ".join(sorted(missing_comparisons))
            )
    if errors:
        raise ValueError("\n".join(dict.fromkeys(errors)))
    assert proposal is not None
    return proposal
