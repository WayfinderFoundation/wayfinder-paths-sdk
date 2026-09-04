"""Compact deterministic evidence for the evolution design/repair loop.

This module deliberately aggregates artifacts and simulator receipts; it does
not create a second research ontology.  Evidence citations are JSON pointers
into one immutable campaign pack, and postmortems are exact same-window diffs.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from wayfinder_paths.jobs.trade_forensics import (
    UNLABELED_EXIT_REASON,
    fill_exit_reason,
    is_stop_exit_reason,
)

if TYPE_CHECKING:
    from wayfinder_paths.jobs.execution.simulator import ExecutionBacktestResult

DIAGNOSTIC_PACK_MAX_BYTES = 24_000
RESULT_STAT_KEYS = (
    "net_return",
    "trade_count",
    "sharpe",
    "sortino",
    "max_drawdown_pct",
    "win_rate",
    "profit_factor",
    "avg_trade_pnl",
    "exposure_pct",
    "peak_notional_usd",
    "total_fees",
    "total_turnover_usd",
    "avg_trade_duration_s",
)

# Failure code -> the class of change that can fix it.  A repair outside the
# admissible class is a new idea wearing the old family's name.
REPAIR_REMEDIES: dict[str, dict[str, list[str]]] = {
    "cost_bleed": {
        "admissible": [
            "raise the minimum hold or add a position TTL",
            "add a rebalance band so small target changes do not trade",
            "add an entry cooldown per symbol",
            "trade fewer symbols",
            "cap position size so turnover falls under the fills/day budget",
        ],
        "forbidden": [
            "signal-threshold or indicator tweaks without a turnover change",
        ],
    },
    "cost_not_covered": {
        "admissible": [
            "convert the entry to a post-only resting limit at an ATR offset "
            "beyond the signal close with a one-bar expiry (`limit_price`, "
            '`time_in_force="ALO"`, `expires_after_bars=1`) and a passive '
            "take-profit: the maker fee replaces the taker round trip and the "
            "offset is price improvement; keep the fast signal, change the "
            "execution (reference: jobs/strategies/hype_passive_rsi.py)",
            "lengthen the hold or raise the target so each trade captures at "
            "least the hurdle multiple of the round-trip cost gross",
            "trade the coarser timeframe the pack validated; fewer, larger moves",
            "drop the marginal entries and keep the ones with the largest "
            "expected move",
        ],
        "forbidden": ["adding fills", "tightening targets", "removing exits"],
    },
    "no_trades": {
        "admissible": [
            "loosen the entry condition that never fires",
            "inspect the gate stack for a condition that is always false",
            "if the sequence preview shows strategy_state written but no entry, "
            "replace any ctx.bar_index age/cooldown/expiry arithmetic with "
            "ctx.bar_ordinal and ctx.bars_since(stamp)",
        ],
        "forbidden": ["adding more gates"],
    },
    "no_progress_preview": {
        "admissible": [
            "change the mechanism so the replay emits a new intent or a new state "
            "transition",
            "if a stored ctx.bar_index drives an age, cooldown or expiry, replace it "
            "with ctx.bar_ordinal and ctx.bars_since(stamp)",
        ],
        "forbidden": ["resubmitting the same state machine with different thresholds"],
    },
    "activity_below_floor": {
        "admissible": [
            "loosen the rarest entry condition",
            "widen the tradable universe within the dataset",
        ],
        "forbidden": ["adding more gates"],
    },
    "negative_after_costs": {
        "admissible": [
            "change the causal mechanism: entry timing, exit rule, or sizing",
            "keep turnover where it is; the cadence is within budget",
        ],
        "forbidden": ["renaming the family", "substituting a generic new idea"],
    },
    "negative_in_target_regime": {
        "admissible": [
            "change the mechanism that is supposed to earn inside the declared regimes",
            "tighten exits or flatten when the portfolio regime leaves the target",
        ],
        "forbidden": ["renaming the family", "substituting a generic new idea"],
    },
    "out_of_regime_loss_budget": {
        "admissible": [
            "flatten or reduce exposure when the portfolio regime leaves the "
            "declared cells",
            "tighten exits outside the target regimes",
        ],
        "forbidden": ["widening the declared regimes to hide the loss"],
    },
    "fees_erased_edge": {
        "admissible": [
            "reduce turnover: minimum hold, rebalance band, or entry cooldown",
        ],
        "forbidden": ["signal changes that raise trade count"],
    },
    "invalid_execution": {
        "admissible": ["fix exactly the named execution error"],
        "forbidden": ["changing the mechanism before it runs"],
    },
    "no_behavior_change": {
        "admissible": [
            "make a change that alters decisions on the screen window",
        ],
        "forbidden": ["cosmetic edits"],
    },
    "activity_collapse": {
        "admissible": ["restore participation before judging the mechanism"],
        "forbidden": ["adding more gates"],
    },
    "screen_slice_loss_bound": {
        "admissible": [
            "stand aside or size down in the slice's regime: gate on "
            "ctx.view.feature('macro_regime', default=0.0) (declare "
            '{"name": "macro_regime", "source": "file"}) and skip entries or cut '
            "exposure there; the book may earn nothing in that regime but may not "
            "give back more than the bound",
            "add a mechanism that earns in that regime (a regime-conditioned book "
            "gets a complexity budget per branch)",
        ],
        "forbidden": ["tuning thresholds to that slice", "removing exits"],
    },
    "screen_regime_dependent": {
        "admissible": [
            "keep the mechanism that earns gated on "
            "ctx.view.feature('macro_regime', default=0.0) (declare "
            '{"name": "macro_regime", "source": "file"}) and stand aside or add a '
            "mechanism in the other slice's regime; the book must be positive and "
            "significant as a whole and no slice may give back more than the bound",
            "change the mechanism so it earns on both screen slices",
        ],
        "forbidden": ["tuning thresholds to the recent slice"],
    },
    "screen_edge_not_significant": {
        "admissible": [
            "raise per-trade expectancy: drop marginal entries, keep the strongest",
            "change the mechanism; the current edge is inside the noise",
        ],
        "forbidden": ["adding trades to widen the sample"],
    },
    "complexity_over_budget": {
        "admissible": [
            "remove gates and thresholds that the screen never exercises",
            "collapse duplicate conditions into one mechanism",
            "keep at most three tuned dimensions",
        ],
        "forbidden": ["adding conditions to make the numbers work"],
    },
}
_DEFAULT_REMEDY = {
    "admissible": ["change the named causal mechanism in response to the evidence"],
    "forbidden": ["renaming the family", "substituting a generic new idea"],
}


def result_receipt(
    result: ExecutionBacktestResult,
    *,
    revision: str,
    objective: Mapping[str, Any] | None = None,
    behavior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist only fields needed to reproduce a behavioral comparison."""
    return {
        "revision": revision,
        "execution_valid": bool(result.validation.get("execution_valid")),
        "stats": {
            key: result.stats.get(key)
            for key in RESULT_STAT_KEYS
            if result.stats.get(key) is not None
        },
        "window": _result_window(result),
        "objective": dict(objective or {}),
        "behavior": dict(behavior or {}),
        "regime": dict(result.stats.get("regime") or {}),
        "trades": [_trade_view(row) for row in result.trades],
    }


def _result_window(result: Any) -> dict[str, Any]:
    """Window span and starting capital; per-day economics need both."""
    curve = list(getattr(result, "equity_curve", None) or [])
    params = getattr(result, "params", None) or {}
    if not curve:
        return {}
    try:
        start = pd.Timestamp(curve[0]["timestamp"])
        end = pd.Timestamp(curve[-1]["timestamp"])
        capital = float(params.get("initial_capital") or curve[0]["equity"])
    except (KeyError, TypeError, ValueError):
        return {}
    return {
        "days": round(max((end - start).total_seconds() / 86_400.0, 0.0), 4),
        "bars": len(curve),
        "starting_equity": capital,
    }


# A trade must capture this multiple of the round-trip cost gross before the
# screen looks at anything else: at 17 bps round trip that is ~26 bps of
# move per trade. Below it the mechanism is paying to trade.
COST_HURDLE_MULTIPLE = 1.5

# Hyperliquid base maker fee; an explicit params["maker_fee_bps"] wins. A
# post-only book pays this per side and no slippage, which is the lever a
# cost-bound repair reaches for when the move is real but small.
DEFAULT_MAKER_FEE_BPS = 1.5


def maker_round_trip_bps(params: Mapping[str, Any] | None) -> float:
    explicit = (params or {}).get("maker_fee_bps")
    fee = float(explicit) if explicit is not None else DEFAULT_MAKER_FEE_BPS
    return round(2.0 * fee, 2)


def receipt_economics(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    """Absolute, per-day trading economics of one receipt.

    Deltas against a reference cannot say "fees consumed 29% of capital in
    33 days"; the repair loop needs the absolute numbers beside the
    incumbent's to see that cadence, not signal, is the defect.
    """
    window = receipt.get("window") or {}
    days = _number(window.get("days"))
    capital = _number(window.get("starting_equity"))
    if days <= 0 or capital <= 0:
        return None
    stats = receipt.get("stats") or {}
    fills = _integer(stats.get("trade_count"))
    fees = _number(stats.get("total_fees"))
    fee_pct = fees / capital
    turnover = _number(stats.get("total_turnover_usd"))
    economics: dict[str, Any] = {
        "window_days": round(days, 3),
        "fills_per_day": round(fills / days, 4),
        "fee_pct_of_capital": round(fee_pct, 6),
        "fee_pct_of_capital_30d": round(fee_pct * 30.0 / days, 6),
        "turnover_multiple": round(turnover / capital, 4),
        "exposure_pct": round(_number(stats.get("exposure_pct")), 6),
        "avg_hold_minutes": round(_number(stats.get("avg_trade_duration_s")) / 60.0, 2),
    }
    if turnover > 0:
        # Gross capture per unit of position notional: turnover counts both
        # sides of a round trip, so the notional traded once is turnover / 2.
        # Comparable to the round-trip cost in bps, which is charged once on
        # that same notional.
        notional = turnover / 2.0
        gross = _number(stats.get("net_return")) * capital + fees
        economics["gross_bps_per_trade"] = round(gross / notional * 1e4, 2)
        nominal = _number(receipt.get("round_trip_cost_bps"))
        if nominal > 0:
            economics["round_trip_cost_bps"] = round(nominal, 2)
            economics["cost_coverage_nominal"] = round(
                economics["gross_bps_per_trade"] / nominal, 4
            )
        realized = _realized_cost(receipt.get("trades") or [], fees=fees)
        if realized is not None:
            economics["maker_fill_share"] = realized["maker_fill_share"]
            economics["realized_cost_bps_per_trade"] = round(
                realized["cost_usd"] / notional * 1e4, 2
            )
        # Coverage is judged on what the book actually paid when its fills say
        # so (a post-only book pays the maker fee and no slippage; the HYPE
        # maker starters pay ~4 bps against a 24 bps nominal taker round
        # trip), and on the nominal round trip otherwise.
        if realized is not None and economics["realized_cost_bps_per_trade"] > 0:
            economics["cost_coverage"] = round(
                economics["gross_bps_per_trade"]
                / economics["realized_cost_bps_per_trade"],
                4,
            )
            economics["cost_basis"] = "realized"
        elif nominal > 0:
            economics["cost_coverage"] = economics["cost_coverage_nominal"]
            economics["cost_basis"] = "nominal"
    return economics


def _realized_cost(
    trades: Sequence[Mapping[str, Any]], *, fees: float
) -> dict[str, float] | None:
    """Fees plus the slippage each fill actually bore, from fills that carry
    their liquidity flag; None when the receipt has no such fills."""
    costed = [row for row in trades if row.get("liquidity") is not None]
    if not costed:
        return None
    slippage = 0.0
    for row in costed:
        price = _number(row.get("reference_price")) or _number(row.get("avg_price"))
        slippage += (
            abs(_number(row.get("filled_size")) * price)
            * _number(row.get("slippage_bps_applied"))
            / 1e4
        )
    makers = sum(1 for row in costed if row.get("liquidity") == "maker")
    return {
        "cost_usd": fees + slippage,
        "maker_fill_share": round(makers / len(costed), 4),
    }


def build_diagnostic_pack(
    root: Path,
    *,
    campaign_id: str,
    created_at: str,
    baseline: Mapping[str, Any],
    historical_lessons: Mapping[str, Any],
    research_context: Mapping[str, Any],
    regime_context: Mapping[str, Any] | None = None,
    validated_signals: Mapping[str, Any] | None = None,
    research_ideation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze compact existing diagnostics with explicit plane/provenance."""
    artifacts: dict[str, Any] = {}
    attribution = _read_json(root / "results/research/attribution.json")
    if attribution:
        deltas = [
            row
            for row in attribution.get("expectation_deltas") or []
            if isinstance(row, dict) and not row.get("small_n")
        ]
        artifacts["attribution"] = {
            "plane": "historical_and_forward",
            "forward_trades": attribution.get("forward_trades"),
            "archetypes_forward": (attribution.get("forward") or {}).get("archetype")
            or {},
            "top_expectation_deltas": deltas[:6],
            "source": _provenance(root, "results/research/attribution.json"),
        }
    forensics = _read_json(root / "results/backtest/trade_forensics.json")
    if forensics.get("aggregate"):
        artifacts["trade_forensics"] = {
            "plane": "historical",
            "aggregate": forensics["aggregate"],
            "source": _provenance(root, "results/backtest/trade_forensics.json"),
        }
    regime = _read_json(root / "results/research/regime_health.json")
    if regime:
        from wayfinder_paths.jobs.regime_health import compact_regime_health

        artifacts["regime_health"] = {
            "plane": "live_and_paper",
            "report": compact_regime_health(regime),
            "source": _provenance(root, "results/research/regime_health.json"),
        }
    counterfactual = _read_json(root / "results/forward/counterfactual.json")
    if counterfactual:
        keep = (
            "available",
            "proposal_id",
            "window",
            "delta_net_pnl",
            "effects",
            "by_symbol",
            "entries_skipped_by_change",
            "entries_added_by_change",
        )
        artifacts["counterfactual"] = {
            "plane": "forward_paper_or_live",
            "report": {
                key: counterfactual[key] for key in keep if key in counterfactual
            },
            "source": _provenance(root, "results/forward/counterfactual.json"),
        }
    forward_summary = _read_json(root / "results/forward/summary.json")
    if forward_summary:
        artifacts["forward_summary"] = {
            "plane": "forward_paper_or_live",
            "report": _compact_forward_summary(forward_summary),
            "source": _provenance(root, "results/forward/summary.json"),
        }
    pack = {
        "schema_version": "1.0",
        "campaign_id": campaign_id,
        "created_at": created_at,
        "citation_contract": (
            "Grounded hypotheses cite JSON pointers into this document. "
            "Missing sections are unavailable, never inferred."
        ),
        "baseline": {"plane": "historical", **dict(baseline)},
        **artifacts,
        "prior_campaign_lessons": dict(historical_lessons),
        "research_context": dict(research_context),
        **(
            {"campaign_regime": dict(regime_context)}
            if regime_context and regime_context.get("available")
            else {}
        ),
        **(
            {"macro_regime": dict(regime_context["macro"])}
            if regime_context
            and isinstance(regime_context.get("macro"), Mapping)
            and regime_context["macro"].get("recent")
            else {}
        ),
        **({"validated_signals": dict(validated_signals)} if validated_signals else {}),
        **({"research_ideation": dict(research_ideation)} if research_ideation else {}),
    }
    return _fit_pack(pack)


def resolve_json_pointer(document: Mapping[str, Any], pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ValueError("evidence reference must be a JSON pointer")
    current: Any = document
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(key)]
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"evidence reference does not exist: {pointer}"
                ) from exc
        elif isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            raise ValueError(f"evidence reference does not exist: {pointer}")
    if isinstance(current, (dict, list)) and not current:
        raise ValueError(f"evidence reference is empty: {pointer}")
    if current is None or current == "":
        raise ValueError(f"evidence reference is empty: {pointer}")
    return current


def valid_evidence_pointers(
    document: Mapping[str, Any], *, limit: int = 96
) -> list[str]:
    """Return a bounded, deterministic menu of citeable non-empty leaves.

    The campaign pack remains the authority.  This is only a prompt-time index
    over that pack so the designer does not spend model turns guessing paths
    that are absent or present-but-empty.
    """

    pointers: list[str] = []

    def escaped(value: Any) -> str:
        return str(value).replace("~", "~0").replace("/", "~1")

    def visit(value: Any, pointer: str) -> None:
        if len(pointers) >= max(int(limit), 0):
            return
        if isinstance(value, Mapping):
            for key in sorted(value, key=str):
                visit(value[key], f"{pointer}/{escaped(key)}")
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                visit(item, f"{pointer}/{index}")
            return
        if value is not None and value != "":
            pointers.append(pointer)

    visit(document, "")
    return pointers


def build_postmortem(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    min_trades: int = 8,
    max_outside_loss_pct: float = 0.02,
    incumbent_economics: Mapping[str, Any] | None = None,
    cost_bleed_fee_multiple: float = 3.0,
    cost_bleed_fee_pct_of_capital_30d: float = 0.10,
) -> dict[str, Any]:
    """Explain what changed and why an attempt did or did not qualify."""
    candidate_trades = list(candidate.get("trades") or [])
    reference_trades = list(reference.get("trades") or [])
    candidate_signatures = {_trade_signature(row) for row in candidate_trades}
    reference_signatures = {_trade_signature(row) for row in reference_trades}
    added = candidate_signatures - reference_signatures
    removed = reference_signatures - candidate_signatures
    stats = candidate.get("stats") or {}
    ref_stats = reference.get("stats") or {}
    regime = candidate.get("regime") or {}
    reference_regime = reference.get("regime") or {}
    specialized = bool(regime.get("target_regimes"))
    trade_count = _integer(
        regime.get("target_trade_count") if specialized else stats.get("trade_count")
    )
    net_return = _number(
        regime.get("target_net_return") if specialized else stats.get("net_return")
    )
    ref_count = _integer(
        reference_regime.get("target_trade_count")
        if specialized
        else ref_stats.get("trade_count")
    )
    ref_net = _number(
        reference_regime.get("target_net_return")
        if specialized
        else ref_stats.get("net_return")
    )
    fee_delta = _number(stats.get("total_fees")) - _number(ref_stats.get("total_fees"))
    turnover_delta = _number(stats.get("total_turnover_usd")) - _number(
        ref_stats.get("total_turnover_usd")
    )
    exposure_delta = _number(stats.get("exposure_pct")) - _number(
        ref_stats.get("exposure_pct")
    )
    material_change = bool(added or removed) or any(
        abs(value) > 1e-12 for value in (fee_delta, turnover_delta, exposure_delta)
    )
    failure_codes: list[str] = []
    if not candidate.get("execution_valid"):
        failure_codes.append("invalid_execution")
    if not material_change:
        failure_codes.append("no_behavior_change")
    if trade_count <= 0:
        failure_codes.append("no_trades")
    elif trade_count < min_trades:
        failure_codes.append("activity_below_floor")
    if net_return <= 0:
        failure_codes.append(
            "negative_in_target_regime" if specialized else "negative_after_costs"
        )
    if specialized and _number(regime.get("outside_loss_pct")) > max_outside_loss_pct:
        failure_codes.append("out_of_regime_loss_budget")
    if ref_count and trade_count < max(1, int(ref_count * 0.5)):
        failure_codes.append("activity_collapse")
    realized = sum(_number(row.get("realized_pnl_delta")) for row in candidate_trades)
    fees = _number(stats.get("total_fees"))
    if realized > 0 and net_return <= 0 and fees > 0:
        failure_codes.append("fees_erased_edge")
    economics = {
        "candidate": receipt_economics(candidate),
        "reference": receipt_economics(reference),
        "incumbent": dict(incumbent_economics) if incumbent_economics else None,
    }
    # The reference is the candidate's frozen seed (an empty zero-fee scaffold
    # for de-novo slots), so the incumbent is the comparator whenever known.
    if _cost_bleed(
        economics["candidate"],
        economics["incumbent"] or economics["reference"],
        net_return=net_return,
        multiple=cost_bleed_fee_multiple,
        floor=cost_bleed_fee_pct_of_capital_30d,
    ):
        position = 1 if failure_codes[:1] == ["invalid_execution"] else 0
        failure_codes.insert(position, "cost_bleed")
    viable = bool(
        candidate.get("execution_valid")
        and material_change
        and trade_count >= min_trades
        and net_return > 0
        and (
            not specialized
            or _number(regime.get("outside_loss_pct")) <= max_outside_loss_pct
        )
    )
    postmortem: dict[str, Any] = {
        "viable": viable,
        "primary_failure": failure_codes[0] if failure_codes else None,
        "failure_codes": failure_codes,
        "behavior_diff": {
            "decisions_added": len(added),
            "decisions_removed": len(removed),
            "decisions_unchanged": len(candidate_signatures & reference_signatures),
            "material_change": material_change,
            "trade_count_delta": trade_count - ref_count,
            "exposure_pct_delta": round(exposure_delta, 8),
            "turnover_usd_delta": round(turnover_delta, 6),
            "fee_delta": round(fee_delta, 6),
            "net_return_delta": round(net_return - ref_net, 8),
            "by_symbol": _bucket_delta(candidate_trades, reference_trades, "symbol"),
            "by_side": _bucket_delta(candidate_trades, reference_trades, "side"),
        },
        "economics": economics,
        "exits": {
            "candidate": receipt_exits(candidate),
            "reference": receipt_exits(reference),
        },
    }
    if previous:
        postmortem["progress_from_previous"] = _progress(candidate, previous)
    return postmortem


def _cost_bleed(
    candidate: Mapping[str, Any] | None,
    comparator: Mapping[str, Any] | None,
    *,
    net_return: float,
    multiple: float,
    floor: float,
) -> bool:
    if not candidate or net_return > 0:
        return False
    candidate_rate = _number(candidate.get("fee_pct_of_capital_30d"))
    comparator_rate = _number((comparator or {}).get("fee_pct_of_capital_30d"))
    threshold = min(multiple * comparator_rate, floor) if comparator_rate > 0 else floor
    return candidate_rate > max(threshold, 0.01)


def preview_progress(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None
) -> bool:
    """Did the sequence preview move since the last no-trade attempt: a new
    intent, a new entry, a new state key, or one more state transition. A
    missing or skipped preview cannot veto a repair."""
    if not previous or not current or current.get("status") == "skipped":
        return True
    if previous.get("status") == "skipped":
        return True

    def transitions(report: Mapping[str, Any]) -> int:
        return sum(
            int(item.get("changes") or 0)
            for item in (report.get("state_keys") or {}).values()
        )

    return (
        int(current.get("intents_total") or 0) > int(previous.get("intents_total") or 0)
        or int(current.get("entries") or 0) > int(previous.get("entries") or 0)
        or transitions(current) > transitions(previous)
        or bool(
            set(current.get("state_keys") or {}) - set(previous.get("state_keys") or {})
        )
    )


# The screen's lower bound (log growth over a 35-day slice) must rise by
# this much before a repair counts as progress; below it the repair budget
# was being spent on fill-count noise.
SCREEN_PROGRESS_MIN_LCB_DELTA = 0.005


def attempt_made_progress(postmortem: Mapping[str, Any]) -> bool:
    """Causal progress: the behavior changed and the outcome moved the right way.

    An extra trade is not progress; for a cost-bleed candidate, cutting
    fills/day toward the budget is.
    """
    progress = postmortem.get("progress_from_previous") or {}
    if not progress:
        return False
    if progress.get("became_valid") or progress.get("became_viable"):
        return True
    if progress.get("preview_progress"):
        # The replay proved a new intent or state transition; trade-level
        # deltas are blind to a no-trade repair that moved its state machine.
        return True
    material = bool((postmortem.get("behavior_diff") or {}).get("material_change"))
    if not material:
        return False
    if progress.get("screen_lcb_delta") is not None:
        # Screened attempts are judged on the screen's own metric.
        return (
            _number(progress.get("screen_lcb_delta")) >= SCREEN_PROGRESS_MIN_LCB_DELTA
        )
    if (
        _number(progress.get("net_return_delta")) > 1e-9
        or _number(progress.get("objective_delta")) > 1e-9
    ):
        return True
    return _number(progress.get("fills_per_day_delta")) < -1e-9 and (
        postmortem.get("primary_failure") == "cost_bleed"
        or "cost_bleed" in (postmortem.get("failure_codes") or [])
    )


_SPECIALIST_REMEDY = (
    "declare target_regimes for the slice where it earns and accept the "
    "bounded-loss-outside-regime bar"
)


def build_repair_work_order(
    postmortem: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    *,
    params: Mapping[str, Any] | None = None,
    min_fills_per_day: float | None = None,
) -> dict[str, Any]:
    """Deterministic assignment for the next attempt: diagnosis, remedy class, budget."""
    policy = policy or {}
    primary = postmortem.get("primary_failure")
    codes = list(postmortem.get("failure_codes") or [])
    remedy_key = str(primary or "")
    if primary == "activity_below_floor" and "no_trades" in codes:
        # A zero-trade book is filed under the floor by the screen verdict;
        # the remedy that fits is the no-trades one.
        remedy_key = "no_trades"
    remedy = REPAIR_REMEDIES.get(remedy_key, _DEFAULT_REMEDY)
    admissible = list(remedy["admissible"])
    if remedy_key == "screen_regime_dependent" and policy.get(
        "regime_specialist_enabled"
    ):
        # The specialist bar is a route only where the campaign accepts
        # target_regimes on a slot; elsewhere it is a dead end.
        admissible.append(_SPECIALIST_REMEDY)
    economics = postmortem.get("economics") or {}
    candidate = economics.get("candidate") or {}
    comparator = economics.get("incumbent") or economics.get("reference") or {}
    comparator_label = "incumbent" if economics.get("incumbent") else "reference"
    budget: dict[str, Any] = {}
    if comparator:
        multiple = float(policy.get("max_fills_per_day_multiple") or 3.0)
        budget["incumbent_fills_per_day"] = comparator.get("fills_per_day")
        budget["max_fills_per_day"] = round(
            multiple * _number(comparator.get("fills_per_day")), 2
        )
    if min_fills_per_day is not None:
        budget["min_fills_per_day"] = float(min_fills_per_day)
    if params:
        budget["round_trip_cost_bps"] = round(
            2.0
            * (_number(params.get("fee_bps")) + _number(params.get("slippage_bps"))),
            2,
        )
        budget["maker_round_trip_bps"] = maker_round_trip_bps(params)
    if candidate.get("cost_coverage") is not None:
        budget["gross_bps_per_trade"] = candidate.get("gross_bps_per_trade")
        budget["cost_coverage"] = candidate.get("cost_coverage")
        budget["cost_hurdle_multiple"] = float(
            policy.get("cost_hurdle_multiple") or COST_HURDLE_MULTIPLE
        )
        for key in (
            "cost_basis",
            "realized_cost_bps_per_trade",
            "cost_coverage_nominal",
            "maker_fill_share",
        ):
            if candidate.get(key) is not None:
                budget[key] = candidate[key]
    return {
        "primary_failure": primary,
        "failure_codes": list(postmortem.get("failure_codes") or [])[:6],
        "diagnosis": _diagnosis(
            postmortem,
            candidate,
            comparator,
            comparator_label,
            maker_round_trip=budget.get("maker_round_trip_bps"),
        ),
        "admissible_repairs": admissible,
        "forbidden": list(remedy["forbidden"]),
        "budget": budget,
    }


def _diagnosis(
    postmortem: Mapping[str, Any],
    candidate: Mapping[str, Any],
    comparator: Mapping[str, Any],
    comparator_label: str,
    maker_round_trip: float | None = None,
) -> str:
    primary = str(postmortem.get("primary_failure") or "")
    context = postmortem.get("repair_context") or {}
    error = str(context.get("error") or "").strip()
    if primary == "invalid_execution":
        return f"Execution failed before evaluation: {error[:240] or 'see postmortem'}."
    screen = postmortem.get("screen") or {}
    if (
        primary
        in {
            "screen_regime_dependent",
            "screen_edge_not_significant",
            "screen_slice_loss_bound",
        }
        and screen
    ):
        parts = []
        for label, row in (screen.get("slices") or {}).items():
            lcb = row.get("lcb")
            mode = row.get("failure_mode") or {}
            drawdown = row.get("max_drawdown_pct")
            macro = row.get("macro_regime")
            concentration = leader_attribution_sentence(row.get("leader_attribution"))
            parts.append(
                f"{label}{f' ({macro})' if macro else ''}: net "
                f"{100 * _number(row.get('net_return')):+.1f}% on "
                f"{_integer(row.get('trade_count'))} trades"
                + (f", LCB {100 * _number(lcb):+.2f}%" if lcb is not None else "")
                + (
                    f", drawdown {100 * _number(drawdown):.1f}%"
                    if drawdown is not None
                    else ""
                )
                + (
                    f", vs seed {100 * _number(mode.get('losing_delta')):+.2f}% on its "
                    f"{_integer(mode.get('losing_days'))} losing days / "
                    f"{100 * _number(mode.get('winning_delta')):+.2f}% on its winning days"
                    if mode
                    else ""
                )
                + (f", {concentration}" if concentration else "")
            )
        facts = "; ".join(parts)
        if primary == "screen_slice_loss_bound":
            bound = 100 * _number(screen.get("max_slice_loss"))
            return (
                f"The book gave back more than {bound:.0f}% in "
                f"{', '.join(screen.get('overdrawn') or [])} ({facts}). An "
                "all-weather book may stand aside in a regime or give a little "
                "back, not this much: gate on the macro_regime column and skip "
                "entries or size down there, or add a mechanism that earns there."
            )
        if primary == "screen_regime_dependent":
            return (
                f"Disjoint screen slices disagree ({facts}). The mechanism is "
                "regime-dependent: keep it gated on the macro_regime column and "
                "add a mechanism for the other slice's regime, or change it to "
                "earn in both; each slice is judged on its own."
            )
        return (
            f"Positive as a whole but not significant at "
            f"{100 * _number(screen.get('confidence')):.0f}% ({facts}). The edge "
            "is inside the noise. Two routes clear it: strengthen expectancy, or "
            "repair the seed's losing days while staying non-inferior on its "
            "winning days."
        )
    if primary == "complexity_over_budget":
        size = context.get("complexity") or {}
        return (
            f"{_integer(size.get('comparisons'))} comparisons and "
            f"{_integer(size.get('numeric_literals'))} numeric literals against a "
            f"budget of {_integer(context.get('complexity_budget'))} comparisons. "
            "Every extra gate is a degree of freedom fitted to a 35-day slice; "
            "simplify to the mechanism that carries the hypothesis."
        )
    preview = context.get("sequence_preview") or {}
    if primary == "no_progress_preview":
        previous = context.get("previous_preview") or {}
        return (
            "The repair changed nothing the replay can see: the sequence preview "
            f"still shows {_integer(preview.get('intents_total'))} intents and "
            f"{_integer(preview.get('entries'))} entries over "
            f"{_integer(preview.get('bars_replayed'))} bars (previous attempt "
            f"{_integer(previous.get('intents_total'))} intents, "
            f"{_integer(previous.get('entries'))} entries; state froze after "
            f"{preview.get('frozen_after') or 'the first bar'}). "
            + _stuck_clock_hint(preview)
        )
    if "no_trades" in (postmortem.get("failure_codes") or []) and preview:
        return _no_trades_diagnosis(preview)
    if primary == "no_behavior_change":
        dead = [str(item) for item in context.get("dead_params") or []]
        if dead:
            return (
                f"Declared dimensions {dead} changed no decision on the screen "
                "window; they are dead knobs here. Sweep a knob that alters "
                "entries or sizing inside the window instead."
            )
        return "The edit changed no decision on the screen window."
    if not candidate:
        return (
            f"Attempt failed with {primary or 'no'} evidence beyond the failure code."
        )
    cadence = f"{_number(candidate.get('fills_per_day')):.1f} fills/day"
    if comparator:
        cadence += (
            f" vs {comparator_label} {_number(comparator.get('fills_per_day')):.1f}"
        )
    fees = (
        f"fees consumed {100 * _number(candidate.get('fee_pct_of_capital')):.0f}% of "
        f"capital in {_number(candidate.get('window_days')):.0f} days"
    )
    if comparator:
        fees += (
            f" ({comparator_label} "
            f"{100 * _number(comparator.get('fee_pct_of_capital_30d')):.1f}% per 30 days)"
        )
    if candidate.get("cost_coverage") is not None:
        hurdle = _number(
            ((postmortem.get("screen") or {}).get("cost_hurdle"))
            or COST_HURDLE_MULTIPLE
        )
        basis = str(candidate.get("cost_basis") or "nominal")
        paid = (
            _number(candidate.get("realized_cost_bps_per_trade"))
            if basis == "realized"
            else _number(candidate.get("round_trip_cost_bps"))
        )
        fees += (
            f"; each trade captured {_number(candidate.get('gross_bps_per_trade')):+.1f} "
            f"bps gross against {paid:.1f} bps {basis} cost"
        )
        if basis == "realized":
            fees += (
                f" ({_number(candidate.get('round_trip_cost_bps')):.0f} bps nominal "
                "taker round trip, maker share of fills "
                f"{100 * _number(candidate.get('maker_fill_share')):.0f}%)"
            )
        fees += (
            f" (coverage {_number(candidate.get('cost_coverage')):.2f}x, "
            f"hurdle {hurdle:.1f}x)"
        )
    exposure = f"exposure {_number(candidate.get('exposure_pct')):.2f}"
    if comparator:
        exposure += f" vs {_number(comparator.get('exposure_pct')):.2f}"
    facts = f"{cadence}; {fees}; {exposure}."
    exits = exits_sentence((postmortem.get("exits") or {}).get("candidate"))
    if exits:
        facts = f"{facts[:-1]}; {exits}."
    recent = ((postmortem.get("screen") or {}).get("slices") or {}).get("recent") or {}
    concentration = leader_attribution_sentence(recent.get("leader_attribution"))
    if concentration:
        facts = f"{facts[:-1]}; {concentration}."
    if primary == "cost_bleed":
        return (
            f"{facts} Fees exceed any plausible edge at this cadence: the "
            "cadence is the defect, not the signal. Reduce turnover toward the "
            "budget band before touching entry logic; do not drive it to zero."
        )
    if primary == "no_trades":
        if preview:
            return _no_trades_diagnosis(preview)
        return "No fills on the screen window: the gate stack never admits an entry."
    if primary == "fees_erased_edge":
        return f"{facts} Gross realized PnL was positive; fees erased it."
    if primary in {"negative_after_costs", "negative_in_target_regime"}:
        return (
            f"{facts} Turnover is within budget, so the mechanism itself is not "
            "capturing edge; change the mechanism, not the cadence."
        )
    if primary == "out_of_regime_loss_budget":
        return f"{facts} Losses outside the declared regimes exceeded the budget."
    if primary == "cost_not_covered":
        return f"{facts} {_cost_lever(candidate, screen, maker_round_trip)}"
    return facts


def _cost_lever(
    candidate: Mapping[str, Any],
    screen: Mapping[str, Any],
    maker_round_trip: float | None,
) -> str:
    """Which lever the cost gap points at: execution when the same trades
    would clear the hurdle against the maker round trip, the move otherwise.
    The HYPE maker starters came from exactly this diagnosis: a real 10-minute
    edge too small for taker fills, monetized by resting bids at an offset."""
    gross = _number(candidate.get("gross_bps_per_trade"))
    hurdle = _number(screen.get("cost_hurdle") or COST_HURDLE_MULTIPLE)
    mostly_maker = _number(candidate.get("maker_fill_share")) >= 0.5
    if (
        maker_round_trip is not None
        and not mostly_maker
        and gross >= hurdle * maker_round_trip
    ):
        return (
            "The gap is execution, not signal: the same trades clear the hurdle "
            f"against a ~{maker_round_trip:.0f} bps maker round trip, so a "
            "post-only resting entry at an offset (price improvement) with a "
            "passive take-profit is the repair; keep the signal."
        )
    return (
        "No execution style covers a move this small: the gross capture per "
        "trade must grow (longer hold, larger target, or the coarser "
        "timeframe) before the cost arithmetic can pass."
    )


def compact_postmortem(postmortem: Mapping[str, Any]) -> dict[str, Any]:
    behavior = postmortem.get("behavior_diff") or {}
    compact = {
        "viable": bool(postmortem.get("viable")),
        "primary_failure": postmortem.get("primary_failure"),
        "failure_codes": list(postmortem.get("failure_codes") or [])[:6],
        "behavior_diff": {
            key: behavior.get(key)
            for key in (
                "decisions_added",
                "decisions_removed",
                "material_change",
                "trade_count_delta",
                "exposure_pct_delta",
                "turnover_usd_delta",
                "fee_delta",
                "net_return_delta",
            )
            if behavior.get(key) is not None
        },
    }
    economics = postmortem.get("economics") or {}
    if isinstance(economics, Mapping) and economics.get("candidate"):
        compact["economics"] = {
            side: {
                key: economics[side].get(key)
                for key in (
                    "window_days",
                    "fills_per_day",
                    "fee_pct_of_capital",
                    "fee_pct_of_capital_30d",
                    "turnover_multiple",
                    "exposure_pct",
                    "gross_bps_per_trade",
                    "round_trip_cost_bps",
                    "realized_cost_bps_per_trade",
                    "maker_fill_share",
                    "cost_coverage",
                    "cost_coverage_nominal",
                    "cost_basis",
                )
            }
            for side in ("candidate", "incumbent", "reference")
            if isinstance(economics.get(side), Mapping)
        }
    exits = postmortem.get("exits") or {}
    if isinstance(exits, Mapping) and exits.get("candidate"):
        compact["exits"] = {
            side: _compact_exits(exits[side])
            for side in ("candidate", "reference")
            if isinstance(exits.get(side), Mapping)
        }
    screen = postmortem.get("screen")
    if isinstance(screen, Mapping) and screen.get("slices"):
        compact["screen"] = {
            "combined_net_return": screen.get("combined_net_return"),
            "pooled_lcb": screen.get("pooled_lcb"),
            "overdrawn": screen.get("overdrawn"),
            "max_slice_loss": screen.get("max_slice_loss"),
            "confidence": screen.get("confidence"),
            "code": screen.get("code"),
            "slices": {
                label: {
                    key: row.get(key)
                    for key in (
                        "net_return",
                        "trade_count",
                        "max_drawdown_pct",
                        "macro_regime",
                        "leader_attribution",
                        "lcb",
                        "route",
                        "failure_mode",
                    )
                    if row.get(key) is not None
                }
                for label, row in screen["slices"].items()
                if isinstance(row, Mapping)
            },
        }
    context = postmortem.get("repair_context") or {}
    repair_error = str(context.get("error") or "").strip()
    repair_context: dict[str, Any] = {}
    if repair_error:
        repair_context["error"] = repair_error[:500]
    for key in ("sequence_preview", "previous_preview"):
        if isinstance(context.get(key), Mapping):
            repair_context[key] = _compact_preview(context[key])
    if repair_context:
        compact["repair_context"] = repair_context
    return compact


def _compact_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: preview.get(key)
        for key in (
            "status",
            "bars_replayed",
            "intents_total",
            "entries",
            "by_action",
            "state_keys",
            "frozen_after",
        )
        if preview.get(key) is not None
    }


def _stuck_clock_hint(preview: Mapping[str, Any]) -> str:
    keys = ", ".join(sorted(preview.get("state_keys") or {})[:6]) or "none"
    return (
        f"strategy_state keys written: {keys}. An armed state machine that never "
        "fires usually measures age, cooldown or expiry from ctx.bar_index, "
        "which is the bounded view length and stays constant once warm, so "
        "every age reads 0; stamp ctx.bar_ordinal and measure with "
        "ctx.bars_since(stamp)."
    )


def _no_trades_diagnosis(preview: Mapping[str, Any]) -> str:
    bars = _integer(preview.get("bars_replayed"))
    status = str(preview.get("status") or "")
    if status == "armed_no_entry":
        return (
            f"No fills on the screen window. The sequence preview replayed {bars} "
            "consecutive bars with persistent state: "
            f"{_integer(preview.get('intents_total'))} intents, "
            f"{_integer(preview.get('entries'))} entries; state was written but "
            f"froze after {preview.get('frozen_after') or 'the first bar'}. "
            + _stuck_clock_hint(preview)
        )
    if status == "silent":
        return (
            "No fills on the screen window and the sequence preview saw no intents "
            f"and no strategy_state writes across {bars} consecutive bars: the "
            "gate stack never admits an entry."
        )
    if status == "entries":
        return (
            "No closed trades although the sequence preview emitted "
            f"{_integer(preview.get('entries'))} entry intents across {bars} bars: "
            "read the guard rejections (limit price, size, symbol) and the exit "
            "path before touching the entry gate."
        )
    return "No fills on the screen window: the gate stack never admits an entry."


_COMPACT_EXIT_REASONS = 6


def _compact_exits(exits: Mapping[str, Any]) -> dict[str, Any]:
    by_reason = exits.get("by_reason") or {}
    return {
        "closes": exits.get("closes"),
        "stop_share": exits.get("stop_share"),
        "by_reason": dict(list(by_reason.items())[:_COMPACT_EXIT_REASONS]),
    }


# A book's losses "concentrate" on a leader state when that state holds at
# least a fifth of them and more than its share of days by half again: the
# pilot's short book lost 28% of its losses on 5% of days (broad rally), the
# long-fade books 38–48% on 22% of days (broad selloff).
_LEADER_MATERIAL_LOSS_SHARE = 0.2
_LEADER_CONCENTRATION = 1.5


def leader_attribution_sentence(attribution: Mapping[str, Any] | None) -> str:
    """One clause per leader state whose loss share is material: '28% of
    losses on broad-rally days (5% of days)'."""
    if not attribution:
        return ""
    parts = []
    for state, name in (("rally", "broad-rally"), ("selloff", "broad-selloff")):
        cell = attribution.get(state) or {}
        loss_share = _number(cell.get("loss_share"))
        day_share = _number(cell.get("day_share"))
        if (
            loss_share >= _LEADER_MATERIAL_LOSS_SHARE
            and loss_share >= _LEADER_CONCENTRATION * day_share
        ):
            parts.append(
                f"{100 * loss_share:.0f}% of losses on {name} days "
                f"({100 * day_share:.0f}% of days)"
            )
    return "; ".join(parts)


def exits_sentence(exits: Mapping[str, Any] | None) -> str:
    """One clause an agent can act on: stop share first, then the reasons."""
    if not exits or not exits.get("closes"):
        return ""
    parts = [
        f"{reason} {_integer(cell.get('count'))} ({_number(cell.get('net_pnl')):+.2f})"
        for reason, cell in list((exits.get("by_reason") or {}).items())[:3]
    ]
    return (
        f"exits: {100 * _number(exits.get('stop_share')):.0f}% stops of "
        f"{_integer(exits.get('closes'))} closes; " + ", ".join(parts)
    )


def participation_adjusted_score(
    raw_score: float, *, trade_count: int, target_trades: int = 12
) -> float:
    if target_trades <= 0:
        return raw_score
    participation = min(1.0, max(0.0, trade_count / target_trades))
    if participation <= 0:
        return float("-inf")
    return raw_score * participation if raw_score >= 0 else raw_score / participation


def _progress(
    candidate: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    stats = candidate.get("stats") or {}
    prior = previous.get("stats") or {}
    objective = candidate.get("objective") or {}
    prior_objective = previous.get("objective") or {}
    return {
        "became_valid": bool(candidate.get("execution_valid"))
        and not bool(previous.get("execution_valid")),
        "became_viable": _receipt_viable(candidate) and not _receipt_viable(previous),
        "trade_count_delta": _integer(stats.get("trade_count"))
        - _integer(prior.get("trade_count")),
        "net_return_delta": round(
            _number(stats.get("net_return")) - _number(prior.get("net_return")), 8
        ),
        "objective_delta": round(
            _objective_score(objective) - _objective_score(prior_objective), 8
        ),
        "fills_per_day_delta": round(
            _number((receipt_economics(candidate) or {}).get("fills_per_day"))
            - _number((receipt_economics(previous) or {}).get("fills_per_day")),
            4,
        ),
    }


def _receipt_viable(receipt: Mapping[str, Any]) -> bool:
    stats = receipt.get("stats") or {}
    return bool(
        receipt.get("execution_valid")
        and _integer(stats.get("trade_count")) >= 1
        and _number(stats.get("net_return")) > 0
    )


def _objective_score(objective: Mapping[str, Any]) -> float:
    return (
        _number(objective.get("net_log_growth"))
        - _number(objective.get("downside_deviation"))
        - _number(objective.get("tail_loss"))
        - _number(objective.get("max_drawdown_pct"))
    )


def _trade_view(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") or {}
    metadata = raw.get("intent_metadata") or raw.get("metadata") or raw
    view: dict[str, Any] = {
        "timestamp": row.get("timestamp"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "filled_size": row.get("filled_size"),
        "avg_price": row.get("avg_price"),
        "fee": row.get("fee"),
        "realized_pnl_delta": row.get("realized_pnl_delta"),
        "reduce_only": bool(row.get("reduce_only")),
        "action": raw.get("intent_action") or metadata.get("action"),
    }
    # Fill-level cost provenance: realized cost is what each fill actually
    # paid, which a post-only book makes far smaller than the nominal round
    # trip.
    if raw.get("liquidity") is not None:
        view["liquidity"] = str(raw["liquidity"])
        view["slippage_bps_applied"] = _number(raw.get("slippage_bps_applied"))
        view["reference_price"] = raw.get("reference_price")
    if view["reduce_only"]:
        view["exit_reason"] = fill_exit_reason(metadata, action=view["action"])
    elif metadata.get("entry_reason"):
        view["entry_reason"] = str(metadata["entry_reason"])
    return view


def receipt_exits(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    """How the receipt's positions ended: closes by exit reason with their
    realized PnL, and the share that were protective stops.

    A design with stops on every entry still needs to know whether the stops
    fire; a 60% stop share with negative stop PnL and positive time-exit PnL
    is the difference between "widen the stop" and "the entry is wrong".
    """
    closes = [row for row in receipt.get("trades") or [] if row.get("reduce_only")]
    if not closes:
        return None
    by_reason: dict[str, dict[str, Any]] = {}
    for row in closes:
        reason = str(row.get("exit_reason") or UNLABELED_EXIT_REASON)
        cell = by_reason.setdefault(reason, {"count": 0, "net_pnl": 0.0, "wins": 0})
        pnl = _number(row.get("realized_pnl_delta"))
        cell["count"] += 1
        cell["net_pnl"] += pnl
        cell["wins"] += int(pnl > 0)
    stops = sum(
        cell["count"]
        for reason, cell in by_reason.items()
        if is_stop_exit_reason(reason)
    )
    return {
        "closes": len(closes),
        "stop_share": round(stops / len(closes), 4),
        "by_reason": {
            reason: {
                "count": cell["count"],
                "net_pnl": round(cell["net_pnl"], 6),
                "win_rate": round(cell["wins"] / cell["count"], 4),
            }
            for reason, cell in sorted(
                by_reason.items(), key=lambda item: -item[1]["count"]
            )
        },
    }


def _trade_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("timestamp") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        bool(row.get("reduce_only")),
        str(row.get("action") or ""),
        round(_number(row.get("filled_size")), 10),
    )


def _bucket_delta(
    candidate: Sequence[Mapping[str, Any]],
    reference: Sequence[Mapping[str, Any]],
    key: str,
) -> dict[str, Any]:
    def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
        buckets: defaultdict[str, dict[str, float]] = defaultdict(
            lambda: {"fills": 0.0, "pnl": 0.0, "fees": 0.0}
        )
        for row in rows:
            bucket = buckets[str(row.get(key) or "?")]
            bucket["fills"] += 1
            bucket["pnl"] += _number(row.get("realized_pnl_delta"))
            bucket["fees"] += _number(row.get("fee"))
        return dict(buckets)

    left, right = aggregate(candidate), aggregate(reference)
    output: dict[str, Any] = {}
    for name in sorted(set(left) | set(right)):
        output[name] = {
            field: round(
                left.get(name, {}).get(field, 0.0)
                - right.get(name, {}).get(field, 0.0),
                6,
            )
            for field in ("fills", "pnl", "fees")
        }
    return output


def _compact_forward_summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    keep = (
        "mode",
        "status",
        "started_at",
        "last_tick_at",
        "trade_count",
        "net_pnl",
        "strategy_net_pnl",
        "operational_net_pnl",
        "total_fees",
        "by_symbol",
    )
    return {key: doc[key] for key in keep if key in doc}


def _provenance(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        payload = path.read_bytes()
        return {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    except OSError:
        return {"path": relative, "unavailable": True}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


_LESSON_BULK_KEYS = frozenset({"postmortem", "validation_forensics"})
_LESSON_PACK_OUTCOMES = 12


def _fit_pack(pack: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(pack, default=str).encode())

    if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
        return pack
    # Preserve the current baseline and load-bearing research lists first.
    for key in (
        "trade_forensics",
        "forward_summary",
        "counterfactual",
        "regime_health",
    ):
        pack.pop(key, None)
        if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
            break
    attribution = pack.get("attribution")
    if isinstance(attribution, dict) and size() > DIAGNOSTIC_PACK_MAX_BYTES:
        attribution["top_expectation_deltas"] = list(
            attribution.get("top_expectation_deltas") or []
        )[:3]
        archetypes = attribution.get("archetypes_forward")
        if isinstance(archetypes, dict):
            attribution["archetypes_forward"] = {
                key: archetypes[key] for key in sorted(archetypes)[:8]
            }
    lessons = pack.get("prior_campaign_lessons")
    if isinstance(lessons, dict) and size() > DIAGNOSTIC_PACK_MAX_BYTES:
        # The outcomes (what was tried, how it ended, the numbers) are the
        # load-bearing part; the per-attempt postmortems and path forensics
        # are the bulk. Trim before dropping the block whole.
        lessons["outcomes"] = [
            {
                key: value
                for key, value in outcome.items()
                if key not in _LESSON_BULK_KEYS
            }
            for outcome in list(lessons.get("outcomes") or [])[:_LESSON_PACK_OUTCOMES]
        ]
    for key in ("attribution", "prior_campaign_lessons", "research_context"):
        if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
            break
        value = pack.pop(key, None)
        pack[f"{key}_truncated"] = {
            "reason": "diagnostic_pack_byte_budget",
            "sha256": hashlib.sha256(
                json.dumps(value, default=str, sort_keys=True).encode()
            ).hexdigest(),
        }
    signals = pack.get("validated_signals")
    if isinstance(signals, dict) and size() > DIAGNOSTIC_PACK_MAX_BYTES:
        # The tiers are what the designer must build on; shrink them in
        # order (near misses first, then the replicated tail) rather than
        # lose the block to the fail-closed shape below.
        for replicated_cap, near_cap in ((6, 3), (6, 0), (4, 0), (2, 0)):
            signals["replicated"] = list(signals.get("replicated") or [])[
                :replicated_cap
            ]
            signals["near_misses"] = list(signals.get("near_misses") or [])[:near_cap]
            signals["trimmed"] = {
                "reason": "diagnostic_pack_byte_budget",
                "replicated_cap": replicated_cap,
                "near_misses_cap": near_cap,
            }
            if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
                break
    if size() > DIAGNOSTIC_PACK_MAX_BYTES:
        baseline = pack.get("baseline") or {}
        pack["baseline"] = {
            key: baseline[key]
            for key in (
                "plane",
                "available",
                "reason",
                "stats",
                "objective",
                "behavior",
                "window_bars",
                "window",
                "economics",
                "round_trip_cost_bps",
                "maker_round_trip_bps",
                "failure_modes",
                "complexity",
            )
            if key in baseline
        }
    if size() > DIAGNOSTIC_PACK_MAX_BYTES:
        # Controlled receipts above are normally far below the cap. Keep a
        # final fail-closed shape so a malformed oversized artifact can never
        # bloat the one design turn or the opencode heap.
        pack = {
            "schema_version": pack.get("schema_version"),
            "campaign_id": pack.get("campaign_id"),
            "created_at": pack.get("created_at"),
            "citation_contract": pack.get("citation_contract"),
            "baseline": pack.get("baseline"),
            "pack_truncated": True,
        }
    pack["truncated_to_bytes"] = DIAGNOSTIC_PACK_MAX_BYTES
    return pack


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
