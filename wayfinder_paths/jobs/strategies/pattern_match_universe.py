"""Shadow-first liquid-universe Pattern Match strategy for 15-minute jobs.

The runner invokes ``decide`` and scores every market every 15 minutes.
Shadow brackets and lane health update every tick; only the hourly phase used
by the frozen research protocol can admit a new candidate.  Orders are
disabled by default.  Setting
``allow_orders=true`` is an explicit operator action and still cannot bypass
calibration, liquidity, confidence, lane-feedback, position-count, or bracket
gates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.quant.pattern_match_universe import (
    INTERVAL,
    MAX_FUNDING_AGE,
    LaneGate,
    MarketCalibration,
    PatternDecision,
    PatternMatcherConfig,
    evaluate_lane_feedback,
    score_latest_pattern,
)

CALIBRATION_PATH = Path(__file__).with_name("pattern_match_universe_calibration.json")
STATE_KEY = "pattern_match_universe_v1"


class PatternMatchUniverseStrategy:
    default_params: dict[str, Any] = {
        "symbols": [],
        "venue": "hyperliquid",
        "warmup_bars": 10_012,
        "minimum_history_bars": 10_000,
        "signal_every_bars": 4,
        # Feed timestamps label the completed bar's close.  Offset 3 is the
        # :45 decision corresponding to the research query bar opened at :30.
        "signal_offset": 3,
        "minimum_volume_24h_usd": 5_000_000.0,
        "minimum_resolved_lane_trades": 50,
        "feedback_window": 10,
        "minimum_recent_lane_mean_bps": 0.0,
        "max_positions": 4,
        "notional_usd": 100.0,
        "minimum_equity_fraction": 0.0,
        "allow_orders": False,
        "native_round_trip_cost_bps": 9.0,
        "hip3_round_trip_cost_bps": 1.8,
        "history_limit": 10_000,
        "rerank_pool": 256,
        "top_matches": 63,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**self.default_params, **(params or {})}
        self.warmup_bars = int(self.params["warmup_bars"])
        self.minimum_history_bars = int(self.params["minimum_history_bars"])
        if not 0 < self.minimum_history_bars <= self.warmup_bars:
            raise ValueError(
                "minimum_history_bars must be positive and no greater than warmup_bars"
            )
        self.bundle = load_calibration_bundle(self.params.get("calibration_path"))
        self.calibrations = {
            str(symbol): MarketCalibration.from_mapping(value)
            for symbol, value in (self.bundle.get("markets") or {}).items()
        }
        self.matcher_config = PatternMatcherConfig(
            history_limit=int(self.params["history_limit"]),
            rerank_pool=int(self.params["rerank_pool"]),
            top_matches=int(self.params["top_matches"]),
            minimum_history_bars=self.minimum_history_bars,
        )

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        state = ctx.strategy_state.setdefault(
            STATE_KEY,
            {
                "pending_shadows": {},
                "lane_history": {},
                "recent_resolutions": [],
                "latest": {},
                "live_owned": {},
            },
        )
        symbols = [
            str(symbol)
            for symbol in (
                self.params.get("universe_symbols") or self.params.get("symbols") or []
            )
        ]
        intents = self._maintain_positions(ctx, state)
        for symbol in symbols:
            frame = ctx.view.symbol_frame(symbol)
            self._advance_shadow(symbol, frame, state)

        candidate_phase = ctx.every_n_bars(
            int(self.params["signal_every_bars"]),
            offset=int(self.params["signal_offset"]),
        )
        decision_time = (
            ctx.view.timestamps[-1].isoformat()
            if ctx.view.timestamps
            else ctx.timestamp
        )

        candidates: list[PatternDecision] = []
        for symbol in symbols:
            frame = ctx.view.symbol_frame(symbol)
            decision = self._score_symbol(symbol, frame, decision_time)
            latest = decision.to_dict()
            latest["candidate_phase"] = candidate_phase
            latest["model_actionable"] = decision.actionable
            if decision.actionable and not candidate_phase:
                latest.update(
                    {
                        "actionable": False,
                        "reason": "off_validated_hourly_phase",
                    }
                )
            elif decision.actionable and symbol in state["pending_shadows"]:
                latest.update({"actionable": False, "reason": "shadow_position_active"})
            elif decision.actionable and symbol in ctx.ledger.positions:
                latest.update({"actionable": False, "reason": "position_active"})
            elif decision.actionable:
                gate = self._lane_gate(symbol, decision, state)
                latest["lane_gate"] = {
                    "allowed": gate.allowed,
                    "reason": gate.reason,
                    "resolved_count": gate.resolved_count,
                    "recent_count": gate.recent_count,
                    "recent_mean_bps": gate.recent_mean_bps,
                }
                self._open_shadow(symbol, decision, state)
                if gate.allowed:
                    candidates.append(decision)
            state["latest"][symbol] = latest

        if not candidate_phase or not bool(self.params["allow_orders"]):
            return intents
        available = max(
            0,
            int(self.params["max_positions"])
            - len(ctx.ledger.positions)
            - sum(1 for item in intents if item["action"] == "OPEN"),
        )
        ranked = sorted(
            candidates,
            key=lambda item: -float(
                item.forecast.similarity_margin_product if item.forecast else 0.0
            ),
        )
        for decision in ranked[:available]:
            intent = self._open_intent(ctx, decision)
            if intent is None:
                continue
            intents.append(intent)
            state["live_owned"][decision.symbol] = {
                "query_time": decision.query_time,
                "direction": decision.direction,
            }
        return intents

    def _score_symbol(
        self, symbol: str, frame: pd.DataFrame, decision_time: str
    ) -> PatternDecision:
        query_time = _frame_last_timestamp(frame) or decision_time
        calibration = self.calibrations.get(symbol)
        if calibration is None:
            return PatternDecision(symbol, query_time, False, "uncalibrated")
        if len(frame) < self.minimum_history_bars:
            return PatternDecision(symbol, query_time, False, "insufficient_history")
        volume_24h = _volume_24h_usd(frame)
        if volume_24h is None:
            return PatternDecision(symbol, query_time, False, "missing_volume")
        if volume_24h <= float(self.params["minimum_volume_24h_usd"]):
            return PatternDecision(symbol, query_time, False, "below_liquidity_floor")
        return score_latest_pattern(
            symbol,
            frame,
            calibration,
            config=self.matcher_config,
        )

    def _lane_gate(
        self,
        symbol: str,
        decision: PatternDecision,
        state: dict[str, Any],
    ) -> LaneGate:
        direction = int(decision.direction or 0)
        lane = _lane_key(symbol, direction)
        history = self._lane_history(state, lane, decision.query_time)
        return evaluate_lane_feedback(
            [],
            symbol=symbol,
            direction=direction,
            as_of=str(decision.query_time),
            minimum_resolved=int(self.params["minimum_resolved_lane_trades"]),
            recent_window=int(self.params["feedback_window"]),
            minimum_recent_mean_bps=float(self.params["minimum_recent_lane_mean_bps"]),
            seed_resolved_count=int(history.get("resolved_count") or 0),
            seed_recent_returns=[
                float(value) for value in history.get("recent_net_returns") or []
            ],
        )

    def _lane_history(
        self, state: dict[str, Any], lane: str, query_time: str | None
    ) -> dict[str, Any]:
        current = state["lane_history"].get(lane)
        if current is not None:
            return current
        seed = (self.bundle.get("lane_seeds") or {}).get(lane) or {}
        seed_as_of = seed.get("as_of")
        if seed_as_of and query_time and _utc(query_time) >= _utc(seed_as_of):
            current = {
                "resolved_count": int(seed.get("resolved_count") or 0),
                "recent_net_returns": [
                    float(value) for value in seed.get("recent_net_returns") or []
                ],
                "seed_as_of": str(seed_as_of),
            }
        else:
            current = {"resolved_count": 0, "recent_net_returns": []}
        state["lane_history"][lane] = current
        return current

    def _open_shadow(
        self, symbol: str, decision: PatternDecision, state: dict[str, Any]
    ) -> None:
        state["pending_shadows"][symbol] = {
            "symbol": symbol,
            "direction": int(decision.direction or 0),
            "query_time": decision.query_time,
            "stop_distance": float(decision.stop_distance or 0.0),
            "take_distance": float(decision.take_distance or 0.0),
            "entry_time": None,
            "entry_price": None,
            "bars_held": 0,
            "funding_return": 0.0,
            "funding_complete": True,
            "path_complete": True,
            "last_checked": decision.query_time,
        }

    def _advance_shadow(
        self, symbol: str, frame: pd.DataFrame, state: dict[str, Any]
    ) -> None:
        shadow = state["pending_shadows"].get(symbol)
        if shadow is None or frame.empty:
            return
        ordered = frame.sort_values("timestamp")
        last_checked = _utc(shadow["last_checked"])
        unseen = ordered[pd.to_datetime(ordered["timestamp"], utc=True) > last_checked]
        for row in unseen.itertuples(index=False):
            timestamp = _utc(row.timestamp)
            elapsed_bars = int((timestamp - last_checked) / INTERVAL)
            if elapsed_bars != 1:
                shadow["path_complete"] = False
            if shadow["entry_price"] is None:
                shadow["entry_time"] = shadow["last_checked"]
                shadow["entry_price"] = float(row.open)
            shadow["bars_held"] = int(shadow["bars_held"]) + max(1, elapsed_bars)
            shadow["last_checked"] = timestamp.isoformat()
            last_checked = timestamp
            try:
                payment = float(row.funding_payment_rate)
                observed_at = _utc(row.funding_observed_at)
                funding_age = timestamp - observed_at
            except (AttributeError, TypeError, ValueError):
                payment = float("nan")
                funding_age = MAX_FUNDING_AGE + pd.Timedelta(seconds=1)
            if (
                not np.isfinite(payment)
                or pd.isna(funding_age)
                or funding_age < pd.Timedelta(0)
                or funding_age > MAX_FUNDING_AGE
            ):
                shadow["funding_complete"] = False
            else:
                shadow["funding_return"] = (
                    float(shadow["funding_return"]) - int(shadow["direction"]) * payment
                )
            outcome = _shadow_outcome(shadow, row)
            if outcome is not None:
                reason, gross = outcome
                self._resolve_shadow(shadow, timestamp, reason, gross, state)
                state["pending_shadows"].pop(symbol, None)
                return

    def _resolve_shadow(
        self,
        shadow: dict[str, Any],
        timestamp: pd.Timestamp,
        reason: str,
        gross_return: float,
        state: dict[str, Any],
    ) -> None:
        symbol = str(shadow["symbol"])
        direction = int(shadow["direction"])
        cost_bps = (
            float(self.params["hip3_round_trip_cost_bps"])
            if ":" in symbol
            else float(self.params["native_round_trip_cost_bps"])
        )
        funding_complete = bool(shadow.get("funding_complete", True))
        path_complete = bool(shadow.get("path_complete", True))
        feedback_eligible = funding_complete and path_complete
        net_return = None
        if feedback_eligible:
            net_return = (
                float(gross_return)
                + float(shadow["funding_return"])
                - cost_bps / 10_000
            )
            lane = _lane_key(symbol, direction)
            history = self._lane_history(state, lane, timestamp.isoformat())
            history["resolved_count"] = int(history.get("resolved_count") or 0) + 1
            recent = [
                *[float(value) for value in history.get("recent_net_returns") or []],
                net_return,
            ][-int(self.params["feedback_window"]) :]
            history["recent_net_returns"] = recent
        resolution = {
            "symbol": symbol,
            "direction": direction,
            "query_time": shadow["query_time"],
            "entry_time": shadow["entry_time"],
            "exit_time": timestamp.isoformat(),
            "exit_reason": reason,
            "bars_held": int(shadow["bars_held"]),
            "gross_return": float(gross_return),
            "funding_return": float(shadow["funding_return"]),
            "net_return": net_return,
            "funding_complete": funding_complete,
            "path_complete": path_complete,
            "feedback_eligible": feedback_eligible,
        }
        state["recent_resolutions"] = [
            *(state.get("recent_resolutions") or []),
            resolution,
        ][-200:]

    def _maintain_positions(
        self, ctx: ExecutionContext, state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        intents: list[dict[str, Any]] = []
        for symbol in list(state["live_owned"]):
            position = ctx.ledger.positions.get(symbol)
            if position is None:
                state["live_owned"].pop(symbol, None)
                continue
            if position.bars_held < 95:
                continue
            intents.append(
                {
                    "action": "CLOSE",
                    "venue": str(self.params["venue"]),
                    "symbol": symbol,
                    "side": "sell" if position.side == "long" else "buy",
                    "size": position.size,
                    "reduce_only": True,
                    "metadata": {"exit_reason": "pattern_match_24h_timeout"},
                }
            )
            state["live_owned"].pop(symbol, None)
        return intents

    def _open_intent(
        self, ctx: ExecutionContext, decision: PatternDecision
    ) -> dict[str, Any] | None:
        notional = float(self.params["notional_usd"])
        equity_fraction = float(self.params["minimum_equity_fraction"])
        if equity_fraction > 0:
            notional = max(notional, mark_to_market_equity(ctx) * equity_fraction)
        if notional <= 0 or decision.direction not in {-1, 1}:
            return None
        return {
            "action": "OPEN",
            "venue": str(self.params["venue"]),
            "symbol": decision.symbol,
            "side": "buy" if decision.direction == 1 else "sell",
            "notional": notional,
            "bracket": {
                "stop_loss_pct": float(decision.stop_distance or 0.0),
                "take_profit_pct": float(decision.take_distance or 0.0),
                "policy": "conservative",
            },
            "metadata": {
                "entry_reason": "pattern_match_high_confidence_lane_pass",
                "query_time": decision.query_time,
                "directional_vote": decision.directional_vote,
                "normalized_steepness": decision.normalized_steepness,
            },
        }


def load_calibration_bundle(path: str | Path | None = None) -> dict[str, Any]:
    location = Path(path) if path else CALIBRATION_PATH
    payload = json.loads(location.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError(f"unsupported Pattern Match calibration: {location}")
    if not isinstance(payload.get("markets"), Mapping):
        raise ValueError(f"Pattern Match calibration has no markets: {location}")
    return payload


def _shadow_outcome(shadow: Mapping[str, Any], row: Any) -> tuple[str, float] | None:
    direction = int(shadow["direction"])
    entry = float(shadow["entry_price"])
    stop = float(shadow["stop_distance"])
    take = float(shadow["take_distance"])
    if direction == 1:
        stop_hit = float(row.low) / entry - 1.0 <= -stop
        take_hit = float(row.high) / entry - 1.0 >= take
    else:
        stop_hit = float(row.high) / entry - 1.0 >= stop
        take_hit = float(row.low) / entry - 1.0 <= -take
    if stop_hit:
        return ("both_stop" if take_hit else "stop", -stop)
    if take_hit:
        return ("take", take)
    if int(shadow["bars_held"]) >= 96:
        return ("null", direction * (float(row.close) / entry - 1.0))
    return None


def _volume_24h_usd(frame: pd.DataFrame) -> float | None:
    if len(frame) < 96:
        return None
    recent = frame.tail(96)
    if "volume_quote" in recent:
        values = pd.to_numeric(recent["volume_quote"], errors="coerce")
    elif "volume" in recent:
        values = pd.to_numeric(recent["volume"], errors="coerce") * pd.to_numeric(
            recent["close"], errors="coerce"
        )
    else:
        return None
    total = float(values.sum(min_count=96))
    return total if np.isfinite(total) else None


def _frame_last_timestamp(frame: pd.DataFrame) -> str | None:
    if frame.empty or "timestamp" not in frame:
        return None
    return _utc(frame["timestamp"].iloc[-1]).isoformat()


def _lane_key(symbol: str, direction: int) -> str:
    return f"{symbol}|{'long' if direction == 1 else 'short'}"


def _utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def build_strategy(
    params: dict[str, Any] | None = None,
) -> PatternMatchUniverseStrategy:
    return PatternMatchUniverseStrategy(params)


__all__ = [
    "PatternMatchUniverseStrategy",
    "build_strategy",
    "load_calibration_bundle",
]
